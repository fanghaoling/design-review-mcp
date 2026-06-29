"""v2.2 prior.py：_convert (r,κ)→(α,β) + load mode 三态（none/builtin/custom）。"""
from __future__ import annotations

from brainregion import prior


def test_convert_r_kappa_to_alpha_beta():
    """r=0.55 κ=10 → α=5.5 β=4.5。"""
    out = prior._convert({"gpt-4o": {"planner": {"r": 0.55, "kappa": 10}}})
    assert out == {("gpt-4o", "planner"): (5.5, 4.5)}


def test_convert_skips_invalid():
    """非法条目静默跳过（best-effort）：kappa≤0 跳、r 缺失用默认、非 dict 跳。"""
    out = prior._convert({
        "a": {"x": {"r": 0.5, "kappa": 0}},   # kappa≤0 跳
        "b": {"y": {"kappa": 10}},             # r 缺失 → 默认 0.5 → (5.0, 5.0)
        "c": {"z": "nope"},                    # 非 dict 跳
        "d": {"w": {"r": 0.6, "kappa": 8}},    # 合法 → (4.8, 3.2)
    })
    assert out == {("b", "y"): (5.0, 5.0), ("d", "w"): (4.8, 3.2)}


def test_convert_empty():
    assert prior._convert({}) == {}
    assert prior._convert(None) == {}


def test_load_mode_none():
    assert prior.load({"mode": "none"}) == {}


def test_load_mode_builtin(monkeypatch):
    monkeypatch.setattr(prior, "_builtin", lambda: {("x", "y"): (1.0, 1.0)})
    assert prior.load({"mode": "builtin"}) == {("x", "y"): (1.0, 1.0)}


def test_load_mode_custom_overrides_builtin(monkeypatch):
    """custom = builtin + custom 覆盖。"""
    monkeypatch.setattr(prior, "_builtin", lambda: {("gpt-4o", "planner"): (5.5, 4.5)})
    out = prior.load({"mode": "custom", "custom": {"gpt-4o": {"planner": {"r": 0.5, "kappa": 12}}}})
    assert out == {("gpt-4o", "planner"): (6.0, 6.0)}  # r=0.5 精确避浮点；custom 覆盖 builtin


def test_load_default_mode_is_builtin(monkeypatch):
    """无 mode 字段默认 builtin。"""
    monkeypatch.setattr(prior, "_builtin", lambda: {("x", "y"): (1.0, 1.0)})
    assert prior.load({}) == {("x", "y"): (1.0, 1.0)}


def test_builtin_cached():
    """_builtin 模块级缓存（多次调用返回同一对象，启动加载一次非每次 review 读）。"""
    a = prior._builtin()
    b = prior._builtin()
    assert a is b


def test_builtin_reads_preset_template_empty_today():
    """preset 今天 priors:{} → builtin 空（=v2.1 行为）。等阶段2 official 填入才非空。"""
    # 不 monkeypatch，验证真实 preset 文件状态
    assert prior._builtin() == {}
