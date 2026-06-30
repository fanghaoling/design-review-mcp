"""stats.py 单测：bootstrap 估计量层 + 守卫 + wilson + seed 独立流 + 估计量方向。"""
from __future__ import annotations

from brainregion.eval.stats import (
    adaptive_B,
    bootstrap_statistic,
    cost_ratio_stat,
    missed_critical_delta_stat,
    seed_for,
    useful_delta_stat,
    wilson_lower,
)


def _row(d_cost, d_useful, r_cost, r_useful, d_total=5, r_total=5, d_missed=0, r_missed=0):
    return {
        "default": {"cost": d_cost, "useful": d_useful, "total_advice": d_total, "missed_critical": d_missed},
        "routed": {"cost": r_cost, "useful": r_useful, "total_advice": r_total, "missed_critical": r_missed},
    }


def test_wilson_lower_bounds():
    assert wilson_lower(0, 10) == 0.0
    assert 0.0 < wilson_lower(7, 10) < 0.7      # 小样本下界远低于点估计 0.7
    assert wilson_lower(10, 10) > 0.5           # 全对下界仍 <1 但较高
    assert wilson_lower(5, 0) == 0.0            # n=0


def test_adaptive_B():
    assert adaptive_B(5) == 2000
    assert adaptive_B(20) == 4000
    assert adaptive_B(50) == 10000
    assert adaptive_B(300) == 10000             # 上限


def test_seed_independent_per_metric():
    assert seed_for("run", "cost_ratio") != seed_for("run", "useful_delta")
    assert seed_for("run", "m", "v1") != seed_for("run", "m", "v2")
    assert seed_for("run", "m") == seed_for("run", "m")     # 确定性


def test_cost_ratio_aggregate_direction():
    # default 1.0/2, routed 0.8/4 → (0.8/4)/(1.0/2)=0.4（<1 = routed 更便宜，吸收 GPT Blocker 1）
    assert cost_ratio_stat([_row(1.0, 2, 0.8, 4)], "default", "routed") == 0.4
    # useful=0 → None
    assert cost_ratio_stat([_row(1, 0, 1, 1)], "default", "routed") is None
    assert cost_ratio_stat([_row(1, 1, 1, 0)], "default", "routed") is None
    # cost=0（litellm 无价格表的 endpoint 模型，如 glm/deepseek-v4-flash）→ 不除零，返回 None
    assert cost_ratio_stat([_row(0, 2, 0, 4)], "default", "routed") is None   # 两臂 cost=0
    assert cost_ratio_stat([_row(0, 2, 1, 4)], "default", "routed") is None   # control cost=0（分母→曾 ZeroDivisionError）
    assert cost_ratio_stat([_row(1, 2, 0, 4)], "default", "routed") is None   # treatment cost=0


def test_useful_delta_and_missed():
    assert useful_delta_stat([_row(1, 2, 1, 4)], "default", "routed") == 0.4   # 4/5 - 2/5
    assert missed_critical_delta_stat([_row(1, 2, 1, 2, 0, 5, 0, 1)], "default", "routed") == 1
    # total_advice=0 → useful_delta None
    assert useful_delta_stat([_row(1, 2, 1, 4, 0, 0)], "default", "routed") is None


def test_bootstrap_constant_ci_equals_point():
    # 恒定 task → 无方差 → CI = point（吸收 GPT Blocker 1：聚合层，非 per-task ratio）
    rows = [_row(0.01, 2, 0.005, 2) for _ in range(10)]
    r = bootstrap_statistic(rows, lambda rs: cost_ratio_stat(rs, "default", "routed"),
                            seed=seed_for("run", "cost_ratio"))
    assert r["point"] == 0.5
    assert r["low"] == 0.5 and r["high"] == 0.5
    assert r["effective_rate"] == 1.0
    assert {"p5", "p25", "p50", "p75", "p95"} <= set(r["quantiles"])


def test_bootstrap_empty_and_degenerate():
    assert bootstrap_statistic([], lambda rs: 1.0, seed=1)["point"] is None            # 空
    assert bootstrap_statistic([{"x": 1}], lambda rs: 1.0, seed=1)["point"] is None     # n<2
    # Σuseful=0 → 点估计 None → CI 无意义
    rows = [_row(1, 0, 1, 0) for _ in range(3)]
    assert bootstrap_statistic(rows, lambda rs: cost_ratio_stat(rs, "default", "routed"),
                               seed=1)["point"] is None


def test_bootstrap_seed_reproducible():
    rows = [_row(0.01 * i, i + 1, 0.005 * i, i + 2) for i in range(1, 11)]

    def _crs(rs):
        return cost_ratio_stat(rs, "default", "routed")

    a = bootstrap_statistic(rows, _crs, seed=42)
    b = bootstrap_statistic(rows, _crs, seed=42)
    assert (a["point"], a["low"], a["high"]) == (b["point"], b["low"], b["high"])


def test_bootstrap_rejects_invalid_B():
    import pytest
    with pytest.raises(ValueError):
        bootstrap_statistic([_row(1, 1, 1, 1), _row(1, 1, 1, 1)], lambda rs: 1.0, B=0, seed=1)
