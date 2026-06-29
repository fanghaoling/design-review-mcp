"""知识库：版本匹配 + retrieve + 渲染。"""
from __future__ import annotations

from brainregion.knowledge import (
    Case,
    YamlKnowledgeProvider,
    constraint_ok,
    render_for_prompt,
    version_matches,
)


def test_constraint_ok():
    assert constraint_ok(">=1.4,<1.5", "1.4.6")
    assert not constraint_ok(">=1.4,<1.5", "1.5.0")
    assert constraint_ok("*", "1.0.0")
    assert constraint_ok("", "1.0.0")
    assert constraint_ok("=1.4.6", "1.4.6")
    assert not constraint_ok(">=1.5", "1.4.6")


def test_version_matches():
    assert version_matches({"entities": ">=1.4,<1.5"}, {"entities": "1.4.6"})
    assert not version_matches({"entities": ">=1.4,<1.5"}, {"entities": "1.5.0"})
    assert version_matches({}, {"entities": "1.4.6"})  # 无约束=通用
    assert version_matches({"entities": ">=1.4"}, {})  # 项目缺版本不过滤


def test_render_for_prompt():
    out = render_for_prompt(
        [Case(id="X", title="t", category="c", bad_pattern="b", recommended_pattern="r")]
    )
    assert "X" in out and "b" in out and "r" in out
    assert render_for_prompt([]) == "(无命中的历史踩坑案例。)"


def test_yaml_provider_retrieve(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text(
        "- id: T-1\n  title: test\n  triggers: [Burst, BC1064]\n"
        "  category: ecs_perf\n  bad_pattern: b\n  recommended_pattern: r\n",
        encoding="utf-8",
    )
    kp = YamlKnowledgeProvider(tmp_path)
    assert len(kp.list_cases()) == 1
    hit = kp.retrieve("这里 Burst 触发 BC1064", {})
    assert [c.id for c in hit] == ["T-1"]
    assert kp.retrieve("完全无关的内容xyz", {}) == []


def test_yaml_provider_anti_triggers_suppress_cross_domain(tmp_path):
    # ISS-006：同词跨域误命中（game "dormant" 撞 brain-region "dormant"）靠 anti_triggers 降噪
    f = tmp_path / "k.yaml"
    f.write_text(
        "- id: GP-DORMANT\n  title: enemy dormancy\n  triggers: [dormant]\n"
        "  anti_triggers: [brain, cognitive, DMN]\n"
        "  category: gameplay\n  bad_pattern: b\n  recommended_pattern: r\n",
        encoding="utf-8",
    )
    kp = YamlKnowledgeProvider(tmp_path)
    # 同词但跨域（brain-region 文本命中 anti_trigger "brain"）→ 不召回
    assert kp.retrieve("dormant memory in the brain region", {}) == []
    # 真正相关（无 anti 词）→ 召回
    assert [c.id for c in kp.retrieve("enemy goes dormant", {})] == ["GP-DORMANT"]


def test_yaml_provider_version_filter(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text(
        "- id: OLD\n  title: old\n  version: {entities: '>=1.0,<1.4'}\n"
        "  triggers: [X]\n  category: c\n  bad_pattern: b\n  recommended_pattern: r\n"
        "- id: NEW\n  title: new\n  version: {entities: '>=1.4'}\n"
        "  triggers: [X]\n  category: c\n  bad_pattern: b\n  recommended_pattern: r\n",
        encoding="utf-8",
    )
    kp = YamlKnowledgeProvider(tmp_path)
    hit = kp.retrieve("X 命中", {"entities": "1.4.6"})
    assert [c.id for c in hit] == ["NEW"]  # OLD 被 1.4.6 过滤掉


def test_brain_region_local_knowledge_dir_overlays_legacy_dir(tmp_path):
    from brainregion.adapters.generic import GenericAdapter
    from brainregion.server import _knowledge_dirs

    legacy = tmp_path / ".design-review" / "knowledge"
    brain_region = tmp_path / ".brain-region" / "knowledge"
    legacy.mkdir(parents=True)
    brain_region.mkdir(parents=True)

    dirs = _knowledge_dirs(GenericAdapter(tmp_path))

    assert dirs[-2:] == [legacy, brain_region]
