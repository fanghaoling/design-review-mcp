"""routing eval：量 wake_gate 的路由精度（wake_metrics vs gold_regions），免费（不调模型/judge）。

与 review bootstrap（量建议质量）互补——这里量"该醒的醒了没"。A=no_defense（仅 escalate，关兜底）
vs B=full（sentinel+shadow 兜底）→ 测假阴性兜底是否降低 missed-wake（roadmap §5 硬门槛），
代价是 false-wake 上升。wake_gate 是规则、不调模型，故本评测零成本、确定性、可大批量跑。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..core.regions import REGIONS_DIR, RegionDefinition
from ..core.wake import wake_gate
from .runner import make_run_id


@dataclass
class RoutingVariant:
    """一个路由评测变体（wake_gate 参数集）。"""

    name: str
    sentinel: bool = True
    shadow_wake_threshold: float | None = None  # None → wake_gate 默认；>1.0 → 关 shadow 提升
    escalate_confidence: float = 0.5


# 默认两变体：no_defense（仅 escalate）vs full（sentinel+shadow 兜底）。
# no_defense 关兜底：sentinel=False + shadow_wake_threshold=escalate_confidence（提升带为空，
#   即 conf<escalate 的候选也 conf<shadow_threshold → 不提升）。不用 >1.0（校验要求 [0,1]）。
DEFAULT_ROUTING_VARIANTS = [
    RoutingVariant("no_defense", sentinel=False, shadow_wake_threshold=0.5),
    RoutingVariant("full", sentinel=True, shadow_wake_threshold=None),
]


@dataclass
class RoutingRecord:
    run_id: str
    task_id: str
    variant: str
    gold_regions: list[str]
    woken: list[str]
    hit: list[str]
    false_wake: list[str]
    missed: list[str]
    metrics_status: str
    activated_regions: dict = field(default_factory=dict)


def _task_text(task) -> tuple[str, dict[str, str]]:
    inp = task.input or {}
    problem = str(inp.get("content") or "")
    files = dict(inp.get("files") or {})
    return problem, files


def run_routing_eval(
    tasks,
    variants: list[RoutingVariant],
    *,
    run_id: str,
    regions: list[RegionDefinition] | None = None,
    regions_dir: str | Path = REGIONS_DIR,
) -> list[RoutingRecord]:
    """对每个 task × variant 跑 wake_gate，收集 wake_metrics。不调模型。"""
    records: list[RoutingRecord] = []
    for task in tasks:
        problem, files = _task_text(task)
        gold = [str(g) for g in (task.gold_regions or [])]
        for v in variants:
            out = wake_gate(
                problem=problem,
                files=files,
                escalate_confidence=v.escalate_confidence,
                shadow_wake_threshold=v.shadow_wake_threshold,
                sentinel=v.sentinel,
                gold_regions=gold,
                regions=regions,
                regions_dir=regions_dir,
            )
            metrics = out["wake_metrics"]
            records.append(
                RoutingRecord(
                    run_id=run_id,
                    task_id=task.id,
                    variant=v.name,
                    gold_regions=gold,
                    woken=list(out["activated_regions"]["woken"]),
                    hit=list(metrics["hit"]),
                    false_wake=list(metrics["false_wake"]),
                    missed=list(metrics["missed"]),
                    metrics_status=metrics["metrics_status"],
                    activated_regions=out["activated_regions"],
                )
            )
    return records


def compute_routing_summary(records: list[RoutingRecord]) -> dict:
    """按 variant 聚合 precision/recall/missed_wake_rate/false_wake_rate。

    recall = hit/gold = hit/(hit+missed)（hit∪missed=gold，不相交）；
    故 recall + missed_wake_rate = 1。
    """
    by_var: dict[str, list[RoutingRecord]] = defaultdict(list)
    for r in records:
        by_var[r.variant].append(r)
    per_variant = {}
    for name, recs in by_var.items():
        gold = sum(len(r.gold_regions) for r in recs)
        hit = sum(len(r.hit) for r in recs)
        missed = sum(len(r.missed) for r in recs)
        false_wake = sum(len(r.false_wake) for r in recs)
        woken = sum(len(r.woken) for r in recs)
        per_variant[name] = {
            "n_tasks": len(recs),
            "gold_total": gold,
            "hit_total": hit,
            "missed_total": missed,
            "false_wake_total": false_wake,
            "woken_total": woken,
            "precision": round(hit / (hit + false_wake), 3) if (hit + false_wake) else 0.0,
            "recall": round(hit / gold, 3) if gold else 0.0,
            "missed_wake_rate": round(missed / gold, 3) if gold else 0.0,
            "false_wake_rate": round(false_wake / woken, 3) if woken else 0.0,
        }
    return {"per_variant": per_variant}


def routing_sanity(records: list[RoutingRecord], summary: dict) -> dict:
    """errors=结构性（gold 缺失测不出）；warnings=兜底论题观察。"""
    errors: list[str] = []
    warnings: list[str] = []
    unscored = [r for r in records if r.metrics_status != "scored"]
    if unscored:
        errors.append(
            f"{len(unscored)} 条 routing 记录 metrics_status≠scored（gold_regions 缺失，测不出 missed-wake）"
        )
    pv = summary["per_variant"]
    if "full" in pv and "no_defense" in pv:
        if pv["full"]["missed_wake_rate"] > pv["no_defense"]["missed_wake_rate"] + 1e-9:
            warnings.append(
                f"假阴性兜底未降 missed-wake：full={pv['full']['missed_wake_rate']} "
                f"> no_defense={pv['no_defense']['missed_wake_rate']}"
            )
        if pv["full"]["false_wake_rate"] + 1e-9 < pv["no_defense"]["false_wake_rate"]:
            warnings.append(
                f"兜底未增 false-wake（异常）：full={pv['full']['false_wake_rate']} "
                f"< no_defense={pv['no_defense']['false_wake_rate']}"
            )
    return {"errors": errors, "warnings": warnings}


def make_routing_run_id() -> str:
    """routing run_id（复用 make_run_id 格式）。"""
    return make_run_id()
