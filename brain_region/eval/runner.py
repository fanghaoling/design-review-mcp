"""评测 runner：编排三变体运行 → 盲评 → 指标 → sanity（含负对照）。

隔离（核心）：不走 server.review_document（共享缓存+共享 feedback DB）。直接 _build_engine 拿
ReviewEngine，engine.review(reliability={}, retrieve_top_k=variant, max_cost_usd=<大值>)。
garbage 变体建独立 engine 并 swap .knowledge 为 GarbageKnowledgeProvider。延迟 perf_counter 自测
（代码无内置）。

负对照 sanity：retrieve_garbage 的有用性应 ≤ retrieve_off ≤ retrieve_on。违反 → 尺子/judge 可疑。
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timezone

from ..core.document import ReviewDocument
from ..server import _build_engine, _normalize_panel, _resolve_endpoints
from . import store
from .judge import judge_task
from .knowledge import GarbageKnowledgeProvider
from .metadata import defaults_hash, git_sha, knowledge_hash, reviewer_hash
from .schema import EvalCaseRecord, EvalLedgerEntry, VariantSpec

logger = logging.getLogger("brain_region.eval.runner")

# bootstrap 默认三变体（off:on:garbage）
DEFAULT_VARIANTS = [
    VariantSpec("retrieve_off", 0, garbage=False),
    VariantSpec("retrieve_on", 5, garbage=False),
    VariantSpec("retrieve_garbage", 5, garbage=True),
]


def build_engines(adapter, dd: dict, variants: list[VariantSpec]):
    """base engine（真知识）供 off/on；garbage 建独立 engine 并注入垃圾 provider。

    返回 (engines_map, backend)。judge 复用 base.backend。
    """
    base = _build_engine(adapter, dd)
    engines: dict[str, object] = {}
    for v in variants:
        if v.garbage:
            ge = _build_engine(adapter, dd)
            ge.knowledge = GarbageKnowledgeProvider(ge.knowledge)
            engines[v.name] = ge
        else:
            engines[v.name] = base
    return engines, base.backend


def _build_doc(task) -> ReviewDocument:
    inp = task.input or {}
    dtype = inp.get("document_type", "markdown")
    content = inp.get("content", "")
    files = inp.get("files")
    if dtype == "code" and files:
        return ReviewDocument.code(files)
    return ReviewDocument(type=dtype, content=content, files=files)


async def run_variant(engine, doc, panel, dimensions, variant: VariantSpec,
                      effort, max_cost_usd, run_id, task_id) -> EvalCaseRecord:
    t0 = time.perf_counter()
    try:
        ctx = await engine.review(
            doc, panel=panel, dimensions=dimensions,
            retrieve_top_k=variant.retrieve_top_k, reliability={},
            max_cost_usd=max_cost_usd, effort=effort,
        )
        dt = (time.perf_counter() - t0) * 1000.0
        rep = ctx.report
        rep_dict = rep.to_dict()
        ind_count = sum(len(v) for v in (rep_dict.get("individual") or {}).values())
        return EvalCaseRecord(
            run_id=run_id, task_id=task_id, variant=variant.name,
            report_summary={
                "consensus": len(rep.consensus), "majority": len(rep.majority),
                "individual": ind_count, "failed": len(rep.failed_models),
                "panel_status": dict(rep.panel_status),
                "risk_level": (rep.risk or {}).get("overall_level"),
            },
            retrieved_case_ids=list(rep.retrieved_cases),
            cost={
                "inference_usd": (rep.usage or {}).get("cost_usd"),
                "estimated_usd": (rep.budget or {}).get("estimated_usd"),
                "total_tokens": (rep.usage or {}).get("total_tokens", 0),
            },
            latency_ms=round(dt, 1),
            outputs_json=json.dumps(rep_dict, ensure_ascii=False, default=str),
        )
    except Exception as e:  # noqa: BLE001 — 单变体失败不阻断整 run
        dt = (time.perf_counter() - t0) * 1000.0
        logger.warning("run_variant 失败 task=%s variant=%s: %s", task_id, variant.name, e)
        return EvalCaseRecord(
            run_id=run_id, task_id=task_id, variant=variant.name,
            latency_ms=round(dt, 1), error=f"{type(e).__name__}: {e}",
        )


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return round(s[k], 1)


def compute_summary(records: list, judgements: list, variants: list[VariantSpec]) -> dict:
    per_variant: dict[str, dict] = {}
    for v in variants:
        recs = [r for r in records if r.variant == v.name]
        jdgs = [j for j in judgements if j.variant == v.name]
        useful = sum(int((j.scores or {}).get("useful", 0) or 0) for j in jdgs)
        total = len(jdgs)
        inference = sum(float((r.cost or {}).get("inference_usd") or 0) for r in recs)
        lat = [float(r.latency_ms or 0) for r in recs]
        per_variant[v.name] = {
            "n": len(recs),
            "useful_advice_total": useful,
            "useful_advice_rate": round(useful / total, 3) if total else 0.0,
            "cost_per_useful_advice": round(inference / useful, 6) if useful else None,
            "inference_cost_usd": round(inference, 6),
            "latency_p50_ms": _pct(lat, 0.5),
            "latency_p95_ms": _pct(lat, 0.95),
            "mean_overall": round(
                statistics.mean([float((j.scores or {}).get("overall", 0) or 0) for j in jdgs]) if jdgs else 0,
                3,
            ),
        }
    return {"per_variant": per_variant}


def sanity_check(records: list, judgements: list, variants: list[VariantSpec]) -> dict:
    """errors=结构性失败（代码 bug）；warnings=负对照观察（judge 噪声，不强 fail）。"""
    errors: list[str] = []
    warnings: list[str] = []
    by_name = {v.name: v for v in variants}

    # 结构性：off 不该检索到 case；garbage 该有 case（随机）
    for r in records:
        v = by_name.get(r.variant)
        if v is None:
            continue
        if v.name == "retrieve_off" and v.retrieve_top_k == 0 and r.retrieved_case_ids:
            errors.append(f"task={r.task_id} retrieve_off 却检索到 case {r.retrieved_case_ids}（top_k=0 未生效）")
        if v.garbage and not r.retrieved_case_ids and not r.error:
            warnings.append(f"task={r.task_id} retrieve_garbage 无 case（知识库可能为空）")
        if r.cost and (r.cost.get("inference_usd") is None) and not r.error:
            warnings.append(f"task={r.task_id} variant={r.variant} inference_usd 为 None（litellm 无该模型单价，ISS-003）")

    # 负对照排序：garbage ≤ off ≤ on（按 mean_overall；缺失变体跳过）
    summary = compute_summary(records, judgements, variants)["per_variant"]
    seq = []
    for name in ("retrieve_garbage", "retrieve_off", "retrieve_on"):
        if name in summary:
            seq.append((name, summary[name]["mean_overall"]))
    if len(seq) >= 2:
        vals = [x[1] for x in seq]
        if any(a > b + 0.01 for a, b in zip(vals, vals[1:])):  # garbage>off>on 任一违反
            warnings.append(
                f"负对照排序异常 mean_overall: {seq}（期望 garbage ≤ off ≤ on；judge 噪声或 rubric 需调）"
            )

    # 盲评解析失败
    parse_fails = [j for j in judgements if "parse" in (j.reason or "").lower() or not j.scores]
    if parse_fails:
        warnings.append(f"{len(parse_fails)} 条盲评解析失败/空（judge 输出非 JSON，可能需降 temperature）")

    return {"errors": errors, "warnings": warnings}


async def run_eval(
    tasks: list, variants: list[VariantSpec], judge_entries: list[dict],
    backend, engines: dict, dd: dict, adapter, rubric_text: str, rubric_hash: str,
    run_id: str, effort=None, max_cost_usd: float = 1.0, panel_override: list | None = None,
) -> tuple[list, list, EvalLedgerEntry]:
    endpoint_ids = set((_resolve_endpoints(dd.get("endpoints") or {}) or {}).keys())
    records: list[EvalCaseRecord] = []
    judgements: list = []

    for task in tasks:
        doc = _build_doc(task)
        panel_src = panel_override if panel_override is not None else (
            (task.input or {}).get("panel") or dd.get("panel") or []
        )
        panel = _normalize_panel(panel_src, endpoint_ids, dd.get("endpoints"))
        dimensions = (task.input or {}).get("dimensions") or dd.get("dimensions") or []
        variant_outputs: dict[str, str] = {}
        for v in variants:
            rec = await run_variant(
                engines[v.name], doc, panel, dimensions, v, effort, max_cost_usd, run_id, task.id,
            )
            records.append(rec)
            store.record_case(rec)
            variant_outputs[v.name] = rec.outputs_json
        for je in judge_entries:
            try:
                js = await judge_task(
                    backend, je, rubric_text, rubric_hash, run_id, task.id, variant_outputs,
                )
                for j in js:
                    store.record_judgement(j)
                    judgements.append(j)
            except Exception as e:  # noqa: BLE001
                logger.warning("judge_task 失败 task=%s judge=%s: %s", task.id, je.get("label"), e)

    summary = compute_summary(records, judgements, variants)
    summary["sanity"] = sanity_check(records, judgements, variants)

    # metadata hash（可追溯）
    k_hash = knowledge_hash(engines[variants[0].name].knowledge)
    entry = EvalLedgerEntry(
        run_id=run_id,
        date=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        git_sha=git_sha(),
        variants=[v.name for v in variants],
        judge_models=[je["model"] for je in judge_entries],
        rubric_hash=rubric_hash,
        knowledge_hash=k_hash,
        reviewer_hash=reviewer_hash(adapter, next(iter(tasks)).input.get("dimensions") if tasks else []),
        defaults_hash=defaults_hash(dd),
        n_tasks=len(tasks),
        summary=summary,
    )
    store.record_run(entry)
    return records, judgements, entry


def make_run_id() -> str:
    """确定性 run_id（不用随机/时间戳的不稳定部分——仍带时间便于人读，hash 兜底唯一）。"""
    # datetime 可用（不是 Workflow 脚本环境）；加 4 位 sha 兜底碰撞
    import hashlib
    stamp = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
    salt = hashlib.sha256(stamp.encode()).hexdigest()[:4]
    return f"{stamp}-{salt}"
