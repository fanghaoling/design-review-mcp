"""盲评 judge：脱敏 → 确定性打乱 → backend.complete → 解析。

脱敏（防身份泄漏，对抗复盘最关键一条）：给 judge 前剥离 case_ref / knowledge_hit / 标题里的
[CASE-ID] 前缀 / retrieved_cases——只留 severity/title/evidence/suggestion。否则 retrieve_on/
retrieve_garbage 的输出带案例引用，judge 一眼认出变体身份。

盲靠 prompt 层打乱：变体名 → [X,Y,Z] 标签，按 task_id 确定性 seed（同任务可复现），记录映射。

多 judge-ready：judge_task 接收单个 judge_entry，runner 对 judge 列表循环（MVP len=1）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re

from ..core.stages.parse import extract_json_object
from .schema import BlindJudgement

logger = logging.getLogger("brainregion.eval.judge")

_CASE_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")

# 默认期望 judge 返回的字段；scores 是自由 dict，rubric 可多回 precision/recall/novelty/...（预留）
RUBRIC_DEFAULT = """你是盲评评审。下面给出对【同一份待审查内容】的若干份候选审查输出，标签为 X / Y / Z（已打乱，你不知道哪个是哪种方法）。

对每一份输出，独立打分，输出严格 JSON：
{"X": {"useful": int, "correct": int, "harmful": int, "missed_critical": int, "overall": int}, "Y": {...}, "Z": {...}}

字段含义：
- useful：有价值的建议条数（主指标）
- correct：正确建议条数（正确 ≠ 被采纳）
- harmful：错误/有害建议条数
- missed_critical：本应指出却遗漏的关键问题数（硬门槛方向）
- overall：整体质量 1-5

可选额外字段（能填就填，填不出可省略）：precision, recall, novelty, coverage, conflict, redundancy（0-1 浮点）。
只看建议实质（severity/title/evidence/suggestion），不要猜测输出来自哪种方法。"""


def _clean_title(title: str) -> str:
    return _CASE_PREFIX_RE.sub("", title or "").strip()


def desensitize(report_dict: dict) -> list[dict]:
    """把 report 拍平成干净 finding 列表：剥离 case_ref / 知识库引用 / [CASE-ID] 标题前缀。

    只留 severity/title/evidence/suggestion——judge 据此打分，看不到变体身份线索。
    """
    out: list[dict] = []
    for bucket in ("consensus", "majority"):
        for cf in report_dict.get(bucket, []) or []:
            out.append({
                "severity": cf.get("severity", ""),
                "title": _clean_title(cf.get("canonical_title") or cf.get("title", "")),
                "evidence": cf.get("evidence_quote", ""),
                "suggestion": cf.get("suggestion", ""),
            })
    for model, fs in (report_dict.get("individual") or {}).items():
        for f in fs or []:
            out.append({
                "severity": f.get("severity", ""),
                "title": _clean_title(f.get("title", "")),
                "evidence": f.get("evidence_quote", ""),
                "suggestion": f.get("suggestion", ""),
            })
    return out


def _seed(task_id: str) -> int:
    return int(hashlib.sha256((task_id or "").encode("utf-8")).hexdigest()[:8], 16)


def _labels(n: int) -> list[str]:
    return [chr(ord("X") + i) for i in range(n)]


def _build_user(labeled: dict[str, list[dict]], task_context: str = "") -> str:
    parts = []
    if task_context:
        parts.append(f"【待审查内容摘要】\n{task_context.strip()}\n")
    for label, findings in labeled.items():
        parts.append(f"=== 输出 {label} ===")
        if not findings:
            parts.append("（无建议）")
        for i, f in enumerate(findings, 1):
            parts.append(
                f"{i}. [{f['severity']}] {f['title']}\n   evidence: {f['evidence']}\n   建议: {f['suggestion']}"
            )
    parts.append("\n请按 schema 输出各标签的 JSON 评分。")
    return "\n".join(parts)


async def judge_task(
    backend, judge_entry: dict, rubric_text: str, rubric_hash: str,
    run_id: str, task_id: str, variant_outputs: dict, task_context: str = "",
) -> list[BlindJudgement]:
    """对一个任务的各变体输出盲评，返回每个变体一条 BlindJudgement。

    variant_outputs: {variant_name: outputs_json_str}（来自 EvalCaseRecord.outputs_json）。
    task_context: 待审查内容摘要——给 judge 看"被审查的是什么"，才能判 missing_critical/相关性。
    """
    variants = list(variant_outputs.keys())
    # 1. 脱敏
    desens: dict[str, list[dict]] = {}
    for v in variants:
        try:
            rep = json.loads(variant_outputs[v]) if variant_outputs[v] else {}
        except Exception:  # noqa: BLE001
            rep = {}
        desens[v] = desensitize(rep)
    # 2. 确定性打乱 variant → label，记录映射
    order = list(variants)
    random.Random(_seed(task_id)).shuffle(order)
    labels = _labels(len(order))
    label_to_variant = dict(zip(labels, order))
    labeled = {lab: desens[v] for lab, v in label_to_variant.items()}
    # 3. 调 judge
    user = _build_user(labeled, task_context)
    resp = await backend.complete(
        model=judge_entry["model"],
        system=rubric_text or RUBRIC_DEFAULT,
        user=user,
        temperature=0.1,
        max_tokens=2048,
        endpoint_id=judge_entry.get("endpoint_id"),
    )
    judge_cost = float(resp.cost_usd) if getattr(resp, "cost_usd", None) else 0.0
    # 4. 解析 → 还原变体
    raw = extract_json_object(resp.content) if resp.ok else None
    results: list[BlindJudgement] = []
    for lab, v in label_to_variant.items():
        scores = (raw or {}).get(lab) if isinstance(raw, dict) else None
        if not isinstance(scores, dict):
            scores = {}
        results.append(BlindJudgement(
            run_id=run_id, task_id=task_id,
            judge_id=judge_entry["label"], judge_model=judge_entry["model"],
            rubric_hash=rubric_hash, variant=v, blind=True,
            scores=scores, reason="" if resp.ok else (resp.error or "parse_failed"),
            judge_cost_usd=judge_cost,
        ))
    return results
