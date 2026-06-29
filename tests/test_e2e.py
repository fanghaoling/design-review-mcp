"""端到端：mock ModelBackend 跑完整 8-Stage pipeline（不调网）。

验证 retrieve → context → prompt → review → parse → normalize → consensus → score 全链路，
含 canonical 归一（同义合并）+ 校准置信度 + 知识库命中。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


from brainregion.adapters.unity import UnityAdapter
from brainregion.core import ReviewDocument
from brainregion.core.engine import ReviewEngine
from brainregion.core.stages import build_default_pipeline
from brainregion.knowledge import YamlKnowledgeProvider
from brainregion.providers.base import ModelResponse

# 真项目根（读真 manifest + 用包内 knowledge 种子）
# tests/test_e2e.py → parents: [tests, brain-region-mcp, Tools, My project]
UNITY_PROJECT = Path(__file__).resolve().parents[3]


class MockBackend:
    async def complete(self, *, model, system, user, temperature=0.3, top_p=0.95, max_tokens=4096, effort=None, endpoint_id=None):
        if "归一化引擎" in system:
            m = re.search(r"```json\s*(\[.*?\])\s*```", user, re.DOTALL)
            items = json.loads(m.group(1)) if m else []
            bc = [
                it["id"]
                for it in items
                if "BC1064" in it.get("title", "") or "按值传" in it.get("title", "")
            ]
            other = [it["id"] for it in items if it["id"] not in bc]
            groups = []
            if bc:
                groups.append(
                    {"canonical_title": "Burst BC1064：struct 按值传", "dimension": "ecs_perf",
                     "severity": "high", "finding_ids": bc}
                )
            if other:
                groups.append(
                    {"canonical_title": "其它问题", "dimension": "ecs_perf",
                     "severity": "medium", "finding_ids": other}
                )
            return ModelResponse(model=model, content=json.dumps({"groups": groups}))
        # review：两模型都报 BC1064（措辞不同，测归一合并），gpt 多报一条 individual
        if "gpt" in model:
            issues = [
                {"dimension": "ecs_perf", "severity": "high", "title": "struct 按值传导致 BC1064",
                 "evidence_quote": "Foo(MyStruct s)", "location": "line 10", "suggestion": "用 in",
                 "confidence": 0.9, "case_ref": "ECS-BURST-001"},
                {"dimension": "ecs_perf", "severity": "medium", "title": "热点循环堆分配",
                 "evidence_quote": "new List", "location": "line 20", "suggestion": "预分配",
                 "confidence": 0.6},
            ]
        else:
            issues = [
                {"dimension": "ecs_perf", "severity": "high", "title": "BC1064：managed struct 按值传",
                 "evidence_quote": "Foo(MyStruct s)", "location": "line 10", "suggestion": "in 参数 out 返回",
                 "confidence": 0.85, "case_ref": "ECS-BURST-001"},
            ]
        return ModelResponse(
            model=model, content=json.dumps({"issues": issues}),
            usage={"total_tokens": 100}, cost_usd=0.001,
        )


async def test_e2e_consensus_and_knowledge_hit():
    a = UnityAdapter(str(UNITY_PROJECT))
    kp = YamlKnowledgeProvider(a.knowledge_dir())
    eng = ReviewEngine(
        adapter=a, backend=MockBackend(), knowledge=kp,
        pipeline=build_default_pipeline(),
    )
    doc = ReviewDocument.markdown(
        "在 Burst OnUpdate 里调 static void Foo(MyStruct s) 按值传 struct"
    )
    ctx = await eng.review(doc, panel=["gpt-5", "claude-opus-4-8"], dimensions=["ecs_perf"])
    r = ctx.report

    # canonical 归一：两模型不同措辞合并成一个 consensus
    assert any("BC1064" in c.canonical_title for c in r.consensus), "BC1064 应进 consensus"
    assert set(r.consensus[0].flagged_by) == {"gpt-5", "claude-opus-4-8"}
    assert r.consensus[0].calibrated_confidence > 0.5
    # 知识库命中
    assert "ECS-BURST-001" in r.knowledge_hit
    # 仅 gpt 报的"堆分配"进 individual
    assert "gpt-5" in r.individual
    # 风险=high（有 high severity consensus）
    assert r.risk["overall_level"] == "high"
