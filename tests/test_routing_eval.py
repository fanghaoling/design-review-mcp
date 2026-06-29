"""routing eval 单测（不联网、不调模型——wake_gate 是规则）。

覆盖：假阴性兜底降 missed-wake、sentinel-only 任务（full 兜住 / no_defense 漏）、
metrics 数学、sanity（兜底退化告警 / gold 缺失 error）。
"""
from __future__ import annotations

from pathlib import Path

from brainregion.eval.cli import load_tasks
from brainregion.eval.routing import (
    DEFAULT_ROUTING_VARIANTS,
    RoutingRecord,
    compute_routing_summary,
    routing_sanity,
    run_routing_eval,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "brainregion" / "eval" / "routing_fixtures"


def _rec(task_id: str, variant: str, gold: list[str], woken: list[str]) -> RoutingRecord:
    g, w = set(gold), set(woken)
    return RoutingRecord(
        run_id="t",
        task_id=task_id,
        variant=variant,
        gold_regions=sorted(g),
        woken=sorted(w),
        hit=sorted(g & w),
        false_wake=sorted(w - g),
        missed=sorted(g - w),
        metrics_status="scored" if g else "unscored",
    )


def test_defense_reduces_missed_wake():
    tasks = load_tasks(str(_FIXTURES))
    recs = run_routing_eval(tasks, DEFAULT_ROUTING_VARIANTS, run_id="t")
    pv = compute_routing_summary(recs)["per_variant"]
    assert pv["full"]["missed_wake_rate"] <= pv["no_defense"]["missed_wake_rate"]


def test_sentinel_only_task_full_catches_no_defense_misses():
    tasks = load_tasks(str(_FIXTURES))
    recs = run_routing_eval(tasks, DEFAULT_ROUTING_VARIANTS, run_id="t")
    by = {(r.task_id, r.variant): r for r in recs}
    # sqli/越权 是 sentinel_keywords 不是 triggers → security 不被 retrieve；只有 full 的 sentinel 兜住
    assert by[("rt-002-security-sentinel", "full")].hit == ["security"]
    assert by[("rt-002-security-sentinel", "no_defense")].missed == ["security"]


def test_summary_metrics_math():
    recs = [_rec("t1", "full", ["a", "b"], ["a", "b", "c"])]  # hit a,b；false_wake c
    m = compute_routing_summary(recs)["per_variant"]["full"]
    assert m["precision"] == round(2 / 3, 3)
    assert m["recall"] == 1.0
    assert m["missed_wake_rate"] == 0.0
    assert m["false_wake_rate"] == round(1 / 3, 3)


def test_sanity_warns_when_defense_not_helping():
    # no_defense 全抓住，full 漏一个（兜底退化）→ 告警
    recs = [
        _rec("t1", "no_defense", ["a"], ["a"]),
        _rec("t1", "full", ["a", "b"], ["a"]),
    ]
    sanity = routing_sanity(recs, compute_routing_summary(recs))
    assert any("未降 missed-wake" in w for w in sanity["warnings"])


def test_unscored_records_flagged_as_error():
    rec = RoutingRecord(
        run_id="t", task_id="t1", variant="full",
        gold_regions=[], woken=[], hit=[], false_wake=[], missed=[],
        metrics_status="unscored",
    )
    sanity = routing_sanity([rec], compute_routing_summary([rec]))
    assert sanity["errors"]  # gold 缺失 → 测不出，必须报错
