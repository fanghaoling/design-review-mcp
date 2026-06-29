"""wake_gate 单测（mock，不联网）。

覆盖 review_plan 双强 panel 审核后吸收的点：
- 三层非塌缩（escalate 用 confidence，≠ retrieve 的 score/min_score）——审核②
- sentinel：中文风险词兜底 + 只唤醒 registry 内 region ——审核①⑤
- shadow 提升（真唤醒，非纯观测）——审核③⑥
- gold/metrics_status：missed-wake 硬门槛可量化、不伪装 0-漏——审核④
- 输入校验——审核⑦
- 只读——trace.models_called=False
"""
from __future__ import annotations

import pytest

from brainregion.core.regions import RegionDefinition
from brainregion.core.wake import wake_gate


# ---------- 三层非塌缩（审核②）----------

def test_three_layers_do_not_collapse():
    # alpha：1 个 text 命中 → score 2 → conf 0.25（< escalate 0.5）→ retrieved 不 escalate
    # beta：2 个 text 命中 → score 4 → conf 0.5（>= escalate）→ retrieved 且 escalate
    regions = [
        RegionDefinition(id="alpha", name="Alpha", triggers=["aaa"]),
        RegionDefinition(id="beta", name="Beta", triggers=["bbb", "ccc"]),
    ]
    out = wake_gate(problem="aaa bbb ccc", regions=regions, sentinel=False)
    act = out["activated_regions"]
    retrieved_ids = {r["id"] for r in act["retrieved"]}
    assert "alpha" in retrieved_ids and "beta" in retrieved_ids
    assert "alpha" not in act["escalated"]      # 低置信 retrieve 不升档
    assert "beta" in act["escalated"]
    assert set(act["escalated"]) < retrieved_ids  # escalate 严格 ⊊ retrieved


# ---------- sentinel：中文兜底 + registry 校验（审核①⑤）----------

def test_sentinel_wakes_missing_region_with_chinese_keyword():
    # security 无 triggers（不会被 retrieve），但 sentinel_keyword "漏洞" 命中 → sentinel wake
    regions = [RegionDefinition(id="security", name="Security", sentinel_keywords=["漏洞", "injection"])]
    out = wake_gate(problem="这里有个 漏洞", regions=regions, sentinel=True)
    act = out["activated_regions"]
    assert "security" in act["woken"]
    assert any(h["region"] == "security" for h in out["trace"]["sentinel_hits"])
    assert act["reasons"]["security"].startswith("sentinel fallback")
    # sentinel 唤醒的置信度被压低（标记为兜底）
    assert act["confidence"]["security"] <= 0.3


def test_sentinel_only_wakes_registered_regions():
    # 所有 woken id 必须在 registry 内（结构保证：sentinel 表只含已加载 region）
    regions = [RegionDefinition(id="security", name="Security", sentinel_keywords=["漏洞"])]
    out = wake_gate(problem="漏洞 concurrency race", regions=regions, sentinel=True)
    region_ids = {r.id for r in regions}
    assert set(out["activated_regions"]["woken"]) <= region_ids


def test_sentinel_disabled_does_not_wake():
    regions = [RegionDefinition(id="security", name="Security", sentinel_keywords=["漏洞"])]
    out = wake_gate(problem="漏洞", regions=regions, sentinel=False)
    assert "security" not in out["activated_regions"]["woken"]
    assert out["trace"]["sentinel_hits"] == []


# ---------- shadow 提升（真唤醒，审核③⑥）----------

def test_shadow_promotes_near_threshold_and_observes_far():
    # ddd：text 命中 score 2 conf 0.25；fff：仅 file-path 命中 score 1 conf 0.125
    # escalate_confidence=0.5, shadow_wake_threshold=0.2 → ddd(0.25) 提升、fff(0.125) 仅观测
    regions = [
        RegionDefinition(id="ddd", name="D", triggers=["ddd"]),
        RegionDefinition(id="fff", name="F", triggers=["fff"]),
    ]
    out = wake_gate(
        problem="ddd",
        files={"fff.py": "ignored content"},
        escalate_confidence=0.5,
        shadow_wake_threshold=0.2,
        regions=regions,
        sentinel=False,
    )
    act = out["activated_regions"]
    assert "ddd" in act["woken"]            # 提升进 woken
    assert "fff" not in act["woken"]        # 远阈值不唤醒
    shadow_by_id = {s["id"]: s for s in act["shadow"]}
    assert shadow_by_id["ddd"]["promoted"] is True
    assert shadow_by_id["fff"]["promoted"] is False
    assert out["trace"]["shadow_promoted"] >= 1


# ---------- gold / metrics_status（审核④）----------

def test_gold_scores_missed_wake():
    regions = [RegionDefinition(id="alpha", name="Alpha", triggers=["aaa"])]
    # escalate_confidence=0.2 → alpha(conf 0.25) 被 woken；gold 含 alpha + 未 woken 的 beta → missed=[beta]
    out = wake_gate(
        problem="aaa",
        regions=regions,
        sentinel=False,
        gold_regions=["alpha", "beta"],
        escalate_confidence=0.2,
    )
    metrics = out["wake_metrics"]
    assert metrics["metrics_status"] == "scored"
    assert "alpha" in metrics["hit"]
    assert metrics["missed"] == ["beta"]


def test_no_gold_is_unscored_not_zero_missed():
    regions = [RegionDefinition(id="alpha", name="Alpha", triggers=["aaa"])]
    out = wake_gate(problem="aaa", regions=regions, sentinel=False)
    metrics = out["wake_metrics"]
    assert metrics["metrics_status"] == "unscored"
    assert metrics["missed"] == []  # unscored：空集是"未评"不是"0 漏"


# ---------- 输入校验（审核⑦）----------

@pytest.mark.parametrize("kwargs", [
    {"escalate_confidence": 1.5},
    {"escalate_confidence": -0.1},
    {"escalate_confidence": float("nan")},
    {"escalate_confidence": "high"},
    {"top_k": 0},
    {"top_k": 100},
    {"shadow_top_n": -1},
])
def test_input_validation_rejects_bad_params(kwargs):
    with pytest.raises(ValueError):
        wake_gate(problem="x", **kwargs)


# ---------- 只读 ----------

def test_read_only_trace():
    regions = [RegionDefinition(id="alpha", name="Alpha", triggers=["aaa"])]
    out = wake_gate(problem="aaa", regions=regions, sentinel=False)
    assert out["trace"]["models_called"] is False
    assert out["trace"]["reverse_wake_triggered"] is False
    assert out["trace"]["strategy"] == "wake_gate_rule_v1"


# ---------- suggested_actions 基于woken集（含 sentinel 唤醒）----------

def test_sentinel_woken_region_gets_action():
    # planning 被 sentinel 唤醒 → 应产出 plan_task action（_build_actions 基于 woken 集）
    regions = [RegionDefinition(id="planning", name="Planning", sentinel_keywords=["路线图"])]
    out = wake_gate(problem="这是路线图", regions=regions, sentinel=True, goal="ship it")
    assert "planning" in out["activated_regions"]["woken"]
    tools = [a["tool"] for a in out["suggested_actions"]]
    assert "plan_task" in tools
