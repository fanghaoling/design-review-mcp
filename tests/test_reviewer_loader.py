"""reviewer loader：inherits 合并 + fallback_dir + 循环检测。"""
from __future__ import annotations

from pathlib import Path

import pytest

from brainregion.core.reviewers.loader import list_reviewers, load_reviewer

CORE = (
    Path(__file__).resolve().parents[1]
    / "brainregion"
    / "core"
    / "reviewers"
)


def test_load_core_safety_inherits_base():
    r = load_reviewer("safety", CORE)
    assert r["name"] == "safety"
    assert r["temperature"] == 0.5
    assert "铁律" in r["system_prompt"]  # 继承自 base
    assert len(r["focus_checklist"]) >= 1


def test_load_with_fallback_dir(tmp_path):
    """adapter reviewer inherits base（在 core），fallback_dir 解析。"""
    (tmp_path / "custom.yaml").write_text(
        "name: custom\ninherits: base\nsystem_prompt: 自定义立场\nfocus_checklist: [x]\n",
        encoding="utf-8",
    )
    r = load_reviewer("custom", tmp_path, fallback_dir=CORE)
    assert "自定义立场" in r["system_prompt"]
    assert "铁律" in r["system_prompt"]  # base 合并进来
    assert r["temperature"] == 0.3  # base 默认值


def test_unknown_raises():
    with pytest.raises(ValueError):
        load_reviewer("nonexistent", CORE)


def test_list_reviewers_excludes_base():
    rs = list_reviewers(CORE)
    assert "base" not in rs
    assert "safety" in rs


def test_cycle_detection(tmp_path):
    (tmp_path / "a.yaml").write_text("name: a\ninherits: b\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("name: b\ninherits: a\n", encoding="utf-8")
    with pytest.raises(ValueError, match="循环继承"):
        load_reviewer("a", tmp_path)
