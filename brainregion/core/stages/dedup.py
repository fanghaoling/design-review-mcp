"""DedupStage：模型内去重（v1.2 F2），normalize 前压缩同一模型重复发射的 finding。

防 glm-5.2 等模型对同一 bug 换皮连发多条（meta-eval 暴露：单模型对 NET-003 连发 5 条
近义 title）。去重规则（仅在**同一 model 内**生效，跨模型交给 NormalizeStage）：
  1) case_ref 相同（且非空）→ 重复；
  2) title Jaccard 相似度 ≥ 阈值 → 重复。
重复则保留 confidence 最高的代表作（不丢信息——其余会被 normalize 的 source 归并覆盖）。
跨模型去重不在本 stage：那是 NormalizeStage 的事（语义归一 + case_ref 跨维度合并）。
"""
from __future__ import annotations

import logging
import re

from ..pipeline import PipelineContext

logger = logging.getLogger("brainregion.stage.dedup")

# 词 token：英文/数字/下划线连续段，或单个中文字符。
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


def _tokens(s: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(s or "")}


def _jaccard(a: set[str], b: set[str]) -> float:
    """两个 token 集合的 Jaccard 相似度。空集返回 0。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class DedupStage:
    name = "dedup"

    def __init__(self, title_sim_threshold: float = 0.6) -> None:
        self.threshold = title_sim_threshold

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.findings:
            return ctx
        # 按 model 分组（保留首次出现顺序，去重后维持稳定顺序）
        by_model: dict[str, list] = {}
        order: list[str] = []
        for f in ctx.findings:
            if f.model not in by_model:
                by_model[f.model] = []
                order.append(f.model)
            by_model[f.model].append(f)

        out = []
        for model in order:
            kept: list = []
            for f in by_model[model]:
                ftok = _tokens(f.title)
                dup_idx = None
                for i, k in enumerate(kept):
                    # 规则1：同 case_ref（非空）
                    if f.case_ref and f.case_ref == k.case_ref:
                        dup_idx = i
                        break
                    # 规则2：title 相似
                    if _jaccard(ftok, _tokens(k.title)) >= self.threshold:
                        dup_idx = i
                        break
                if dup_idx is None:
                    kept.append(f)
                else:
                    rep = kept[dup_idx]
                    if f.confidence > rep.confidence:
                        # f 取代代表：旧代表 id + 其已收集的 deduped_ids 带到 f（断链点A 防 id 断链）
                        inherited = list(rep.deduped_ids)
                        if rep.id:
                            inherited.append(rep.id)
                        f.deduped_ids = inherited
                        kept[dup_idx] = f  # 留 confidence 更高的代表
                    else:
                        # f 被丢弃：id 挂代表 deduped_ids（断链点A，mark_finding 反查 deduped_ids）
                        if f.id:
                            rep.deduped_ids = list(rep.deduped_ids) + [f.id]
            out.extend(kept)

        dropped = len(ctx.findings) - len(out)
        if dropped:
            logger.info("模型内去重：%d → %d（去 %d 条换皮重复）", len(ctx.findings), len(out), dropped)
        ctx.findings = out
        return ctx
