"""NormalizeStage：LLM 归一 findings → canonical findings（B2 + v1.2 F1 跨维度合并）。

两段归一：
  ① LLM 归一 pass：所有 findings（去 evidence 细节，带 case_ref）交给主审模型，按语义
     把同措辞/同维度的合并成一个 canonical 组（防"重复Spawn"/"实例化"/"双生成"漏报）。
  ② case_ref 确定性跨维度合并（F1，meta-eval 暴露的核心缺口）：LLM 归一只看 title+dimension，
     同一 bug 从 safety/netcode/planner 三个角度看会被拆成不同 canonical 组，导致 4 模型全命中
     的 bug 反而进不了 consensus 桶。case_ref 是知识库给的 ground-truth 簇键——case_ref 相同的
     canonical 强制合并成一条（canonical_title 用案例标题，dimension 用案例 category），
     跨维度不再碎裂。

归一失败则降级为每 finding 一组（不阻塞 pipeline）。
"""
from __future__ import annotations

import json
import logging
import re

from ..pipeline import PipelineContext
from ..report import CanonicalFinding, Finding

logger = logging.getLogger("brainregion.stage.normalize")

_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_SEV = {"high": 0, "medium": 1, "low": 2}


def _build_prompt(findings: list[Finding]) -> tuple[str, str]:
    items = [
        {
            "id": i,
            "title": f.title,
            "dimension": f.dimension,
            "severity": f.severity,
            "case_ref": f.case_ref,
        }
        for i, f in enumerate(findings)
    ]
    system = (
        "你是审查发现的归一化引擎。把语义相同的发现合并成一个 canonical 组。"
        "输出严格 JSON：{\"groups\":[{\"canonical_title\":str, \"dimension\":str, "
        "\"severity\":str, \"finding_ids\":[int,...]}]}。"
        "canonical_title 是一句话标准标题。规则："
        "1) 同义不同措辞（如'重复Spawn'/'重复实例化'/'双生成'）必须合并；"
        "2) case_ref 相同（指向同一历史踩坑案例 id）的发现必须合并到同一组，即使 dimension 不同——它们是同一根因；"
        "3) 不要丢掉任何 finding_id。"
    )
    user = "findings:\n```json\n" + json.dumps(items, ensure_ascii=False, indent=2) + "\n```"
    return system, user


def _parse_groups(content: str) -> list[dict]:
    m = _BLOCK_RE.search(content or "")
    cand = m.group(1) if m else (content or "").strip()
    try:
        obj = json.loads(cand)
        gs = obj.get("groups") if isinstance(obj, dict) else None
        return [g for g in gs if isinstance(g, dict)] if isinstance(gs, list) else []
    except Exception:  # noqa: BLE001
        return []


def _merge_case_ref_group(cfs: list[CanonicalFinding], case_title: str, case_cat: str) -> CanonicalFinding:
    """把共享 case_ref 的多条 canonical 合并成一条（F1）。

    canonical_title 用案例标题（跨模型一致）；dimension 用案例 category；
    severity 取最高；evidence/location/suggestion 取最高 severity（并列最多 source）代表；
    flagged_by 取并集；source_findings 全部保留供溯源。
    """
    rep = sorted(cfs, key=lambda c: (_SEV.get(c.severity, 9), -len(c.source_findings)))[0]
    # 只在强信号（多模型，或多条 canonical 指向同一 case）时才用案例标题作 canonical_title。
    # 单条/单模型的 case_ref 可能来自检索误命中（如游戏案例 GP-ENEMY-DORMANT 命中"dormant"词面），
    # 此时用案例标题会把无关标题顶到 finding 上（ISS-004）→ 退回 LLM 生成的 rep 标题（反映真实内容）。
    distinct_models = {f.model for c in cfs for f in (c.source_findings or [])}
    strong = len(distinct_models) >= 2 or len(cfs) >= 2
    if case_title and strong:
        title = f"[{rep.case_ref}] {case_title}"
    else:
        title = rep.canonical_title
    return CanonicalFinding(
        canonical_title=title,
        dimension=case_cat or rep.dimension,
        severity=rep.severity,
        evidence_quote=rep.evidence_quote,
        location=rep.location,
        suggestion=rep.suggestion,
        case_ref=rep.case_ref,
        flagged_by=sorted({m for c in cfs for m in c.flagged_by}),
        source_findings=[f for c in cfs for f in c.source_findings],
    )


def merge_canonical_by_case_ref(
    canonical: list[CanonicalFinding], retrieved_cases: list
) -> list[CanonicalFinding]:
    """LLM 归一结果再做 case_ref 确定性跨维度合并（F1）。

    无 case_ref 的原样保留；有 case_ref 的按 case_ref 分组合并。
    """
    case_meta = {c.id: (c.title, c.category) for c in (retrieved_cases or [])}
    out: list[CanonicalFinding] = []
    groups: dict[str, list[CanonicalFinding]] = {}
    group_order: list[str] = []
    for cf in canonical:
        if cf.case_ref:
            if cf.case_ref not in groups:
                groups[cf.case_ref] = []
                group_order.append(cf.case_ref)
            groups[cf.case_ref].append(cf)
        else:
            out.append(cf)
    for ref in group_order:
        cfs = groups[ref]
        title, cat = case_meta.get(ref, ("", ""))
        out.append(_merge_case_ref_group(cfs, title, cat))
    merged = len(canonical) - len(out)
    if merged:
        logger.info("case_ref 跨维度合并：%d → %d（合 %d 组）", len(canonical), len(out), merged)
    return out


class NormalizeStage:
    name = "normalize"

    def __init__(self, normalizer: dict | None = None) -> None:
        # v1.6: normalizer 也用 PanelEntry{model, endpoint_id}（schema 与 panel 统一，可走中转）。
        self.normalizer = normalizer or {"model": "claude-opus-4-8", "endpoint_id": None}

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.findings:
            return ctx
        system, user = _build_prompt(ctx.findings)
        resp = await ctx.backend.complete(
            model=self.normalizer["model"],
            system=system,
            user=user,
            temperature=0.1,
            top_p=0.9,
            max_tokens=4096,
            endpoint_id=self.normalizer.get("endpoint_id"),
        )
        groups = _parse_groups(resp.content) if resp.ok else []
        if not groups:
            logger.warning("归一失败(model=%s err=%s)，降级为每 finding 一组", self.normalizer["model"], resp.error)
            groups = [
                {
                    "canonical_title": f.title,
                    "dimension": f.dimension,
                    "severity": f.severity,
                    "finding_ids": [i],
                }
                for i, f in enumerate(ctx.findings)
            ]
        canonical: list[CanonicalFinding] = []
        for g in groups:
            ids = [i for i in g.get("finding_ids", []) if isinstance(i, int)]
            src = [ctx.findings[i] for i in ids if 0 <= i < len(ctx.findings)]
            if not src:
                continue
            rep = src[0]
            # 组内 case_ref 取最常见的非空值（防 LLM 把同 case_ref 与无 case_ref 的 finding
            # 混组时 src[0] 恰为 None，导致 F1 跨维度合并失效）。
            ref_counts: dict[str, int] = {}
            for f in src:
                if f.case_ref:
                    ref_counts[f.case_ref] = ref_counts.get(f.case_ref, 0) + 1
            group_case_ref = (
                max(ref_counts, key=ref_counts.get) if ref_counts else rep.case_ref
            )
            canonical.append(
                CanonicalFinding(
                    canonical_title=g.get("canonical_title") or rep.title,
                    dimension=g.get("dimension") or rep.dimension,
                    severity=g.get("severity") or rep.severity,
                    evidence_quote=rep.evidence_quote,
                    location=rep.location,
                    suggestion=rep.suggestion,
                    case_ref=group_case_ref,
                    flagged_by=sorted({f.model for f in src}),
                    source_findings=src,
                )
            )
        # F1：case_ref 确定性跨维度合并（修 meta-eval 暴露的同 bug 碎成 N 条缺口）
        ctx.canonical_findings = merge_canonical_by_case_ref(canonical, ctx.retrieved_cases)
        return ctx
