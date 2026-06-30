"""纯-Python bootstrap CI + 校准统计（无 numpy 依赖）。

估计量层重采样（吸收 GPT Blocker 1）：重采 task、每次从原始配对分量
(cost, useful, total_advice, missed_critical) **重算聚合估计量**（cost_ratio / useful_delta /
missed_critical_delta），取 percentile CI。**不 bootstrap per-task ratio**——分母小时 ratio 分布有偏。

Wilson 下界只用于 calibration agreement（Bernoulli 对，吸收 GPT Blocker 2）；outcome 走 bootstrap。

守卫（吸收 review_plan C2 + GPT 小优化）：rows 空 / n<2 → point=None（调用方 → INCONCLUSIVE）；
resample 内聚合分母=0（Σuseful=0）→ 该 resample 跳过（degenerate）；effective_rate 一眼看出
CI 不稳是"有效样本太少"还是"模型真不稳"。
"""
from __future__ import annotations

import hashlib
import math
import random
import statistics
from typing import Callable

# task_row: {variant_name: {"cost": float, "useful": float, "total_advice": float, "missed_critical": float}}


def _z_for_confidence(confidence: float) -> float:
    table = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}
    return table.get(round(confidence, 2), 1.959963984540054)


def wilson_lower(k: int, n: int, confidence: float = 0.95) -> float:
    """Bernoulli 比例的 Wilson 下置信界（calibration agreement 用）。n=0 → 0.0。"""
    if n <= 0:
        return 0.0
    z = _z_for_confidence(confidence)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return max(0.0, center - half)


def seed_for(run_id: str, metric: str, variant: str = "") -> int:
    """确定性、每 (run_id, metric, variant) 独立的种子（吸收 Rec 3：独立流）。"""
    key = f"{run_id}|{metric}|{variant}"
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)


def adaptive_B(n: int) -> int:
    """B = max(2000, min(10000, n*200))（吸收 Rec 1：n=20→4000, n=50→10000）。"""
    return max(2000, min(10000, n * 200))


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = max(0, min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _empty_result(n: int, B: int) -> dict:
    return {
        "point": None, "low": None, "high": None, "mean": None, "std": None,
        "quantiles": {}, "effective_rate": 0.0, "n": n, "B": B, "degenerate": 0,
    }


def bootstrap_statistic(
    rows: list, stat_fn: Callable[[list], float | None], *,
    confidence: float = 0.95, B: int | None = None, seed: int = 0,
) -> dict:
    """估计量层 percentile bootstrap。

    rows: 每 task 的原始记录（dict）；stat_fn(resampled_rows) -> 标量估计量（聚合 cost_ratio/delta），
          返回 None/非有限 = 该样本 degenerate（如 Σuseful=0）。
    返回 {point, low, high, mean, std, quantiles, effective_rate, n, B, degenerate}。
    rows 空 / n<2 / 点估计 degenerate → point=None。
    """
    n = len(rows)
    if n < 2:
        return _empty_result(n, B or 0)
    if B is None:
        B = adaptive_B(n)
    if B <= 0:
        raise ValueError("B 必须 > 0")
    point = stat_fn(rows)
    if point is None or not math.isfinite(point):
        return _empty_result(n, B)
    rng = random.Random(seed)
    samples: list[float] = []
    degenerate = 0
    for _ in range(B):
        resampled = [rows[rng.randrange(n)] for _ in range(n)]
        s = stat_fn(resampled)
        if s is None or not math.isfinite(s):
            degenerate += 1
            continue
        samples.append(s)
    if not samples:
        return _empty_result(n, B)
    samples_sorted = sorted(samples)
    alpha = (1 - confidence) / 2
    quantiles = {
        "p5": _percentile(samples_sorted, 0.05),
        "p25": _percentile(samples_sorted, 0.25),
        "p50": _percentile(samples_sorted, 0.50),
        "p75": _percentile(samples_sorted, 0.75),
        "p95": _percentile(samples_sorted, 0.95),
    }
    usable = len(samples)
    return {
        "point": point,
        "low": _percentile(samples_sorted, alpha),
        "high": _percentile(samples_sorted, 1 - alpha),
        "mean": statistics.mean(samples),
        "std": statistics.pstdev(samples) if len(samples) > 1 else 0.0,
        "quantiles": quantiles,
        "effective_rate": round(usable / B, 4) if B else 0.0,
        "n": n,
        "B": B,
        "degenerate": degenerate,
    }


# ===== 聚合估计量（control vs treatment，variant_name 参数化，不写死 default/routed）=====

def cost_ratio_stat(rows: list, control: str, treatment: str) -> float | None:
    """(Σtreatment_cost/Σtreatment_useful) / (Σcontrol_cost/Σcontrol_useful)。<1 = treatment 更便宜。
    任一 Σuseful=0 或 Σcost=0 → None（degenerate）。cost=0 多见于 litellm 无价格表的模型
    （endpoint 中转的 glm/gpt-5.4-mini/deepseek-v4-flash 等），此时 cost ratio 无意义——
    分母（control cost/useful）若为 0 会除零，故任一 cost=0 即返回 None（诚实，gate 走 INCONCLUSIVE）。
    吸收 GPT Blocker 1：聚合层算，非 per-task ratio。"""
    c_cost = sum(r[control]["cost"] for r in rows)
    c_useful = sum(r[control]["useful"] for r in rows)
    t_cost = sum(r[treatment]["cost"] for r in rows)
    t_useful = sum(r[treatment]["useful"] for r in rows)
    if c_useful == 0 or t_useful == 0 or c_cost == 0 or t_cost == 0:
        return None
    return (t_cost / t_useful) / (c_cost / c_useful)


def useful_delta_stat(rows: list, control: str, treatment: str) -> float | None:
    """(Σt_useful/Σt_total) − (Σc_useful/Σc_total)。>0 = treatment useful 率更高。
    任一 Σtotal_advice=0 → None。"""
    c_u = sum(r[control]["useful"] for r in rows)
    c_t = sum(r[control]["total_advice"] for r in rows)
    t_u = sum(r[treatment]["useful"] for r in rows)
    t_t = sum(r[treatment]["total_advice"] for r in rows)
    if c_t == 0 or t_t == 0:
        return None
    return (t_u / t_t) - (c_u / c_t)


def missed_critical_delta_stat(rows: list, control: str, treatment: str) -> float:
    """Σtreatment_missed − Σcontrol_missed（计数差，恒有限）。>0 = treatment 多漏关键。"""
    return (sum(r[treatment]["missed_critical"] for r in rows)
            - sum(r[control]["missed_critical"] for r in rows))
