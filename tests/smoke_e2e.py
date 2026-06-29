"""端到端冒烟测试：mock ModelBackend 跑完整 pipeline（不调网）。

验证 8 个 Stage 全链路：retrieve → context → prompt → review → parse → normalize → consensus → score。
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brainregion.adapters.unity import UnityAdapter
from brainregion.core import ReviewDocument
from brainregion.core.engine import ReviewEngine
from brainregion.core.stages import build_default_pipeline
from brainregion.knowledge import YamlKnowledgeProvider
from brainregion.providers.base import ModelResponse


class MockBackend:
    async def complete(self, *, model, system, user, temperature=0.3, top_p=0.95, max_tokens=4096, effort=None, endpoint_id=None):
        if "归一化引擎" in system:
            m = re.search(r"```json\s*(\[.*?\])\s*```", user, re.DOTALL)
            items = json.loads(m.group(1)) if m else []
            bc = [it["id"] for it in items if "BC1064" in it.get("title", "") or "按值传" in it.get("title", "")]
            other = [it["id"] for it in items if it["id"] not in bc]
            groups = []
            if bc:
                groups.append({"canonical_title": "Burst BC1064：struct 按值传", "dimension": "ecs_perf", "severity": "high", "finding_ids": bc})
            if other:
                groups.append({"canonical_title": "其它性能问题", "dimension": "ecs_perf", "severity": "medium", "finding_ids": other})
            return ModelResponse(model=model, content=json.dumps({"groups": groups}))
        if "gpt" in model:
            issues = [
                {"dimension": "ecs_perf", "severity": "high", "title": "struct 按值传导致 BC1064", "evidence_quote": "void Foo(MyStruct s)", "location": "line 10", "suggestion": "用 in", "confidence": 0.9, "case_ref": "ECS-BURST-001"},
                {"dimension": "ecs_perf", "severity": "medium", "title": "热点循环堆分配", "evidence_quote": "new List<int>()", "location": "line 20", "suggestion": "预分配 NativeList", "confidence": 0.6},
            ]
        else:
            issues = [
                {"dimension": "ecs_perf", "severity": "high", "title": "BC1064：managed struct 按值传", "evidence_quote": "Foo(MyStruct s)", "location": "line 10", "suggestion": "in 参数 out 返回", "confidence": 0.85, "case_ref": "ECS-BURST-001"},
            ]
        return ModelResponse(model=model, content=json.dumps({"issues": issues}), usage={"total_tokens": 120}, cost_usd=0.002)


def main():
    a = UnityAdapter("d:/Unity/My Project/Unity-ECS/My project")
    kp = YamlKnowledgeProvider(a.knowledge_dir())
    eng = ReviewEngine(
        adapter=a, backend=MockBackend(), knowledge=kp,
        pipeline=build_default_pipeline(normalizer_model="claude-opus-4-8"),
    )
    doc = ReviewDocument.markdown("## 方案\n在 Burst OnUpdate 里调 static void Foo(MyStruct s) 按值传 struct。")
    ctx = asyncio.run(eng.review(doc, panel=["gpt-5", "claude-opus-4-8"], dimensions=["ecs_perf"]))
    r = ctx.report
    print("retrieved:", r.retrieved_cases)
    print("consensus:", [(c.canonical_title, c.flagged_by, c.calibrated_confidence, c.case_ref) for c in r.consensus])
    print("majority:", [(c.canonical_title, c.flagged_by) for c in r.majority])
    print("individual:", {k: [f.title for f in v] for k, v in r.individual.items()})
    print("knowledge_hit:", r.knowledge_hit)
    print("usage:", r.usage, "| risk:", r.risk)

    assert any("BC1064" in c.canonical_title for c in r.consensus), "BC1064 应进 consensus"
    assert r.consensus[0].calibrated_confidence > 0.5, "共识置信度应 >0.5"
    assert "ECS-BURST-001" in r.knowledge_hit, "应命中知识库 ECS-BURST-001"
    assert "gpt-5" in r.individual, "热点循环(仅 gpt)应进 individual"
    print("\nE2E SMOKE OK")


if __name__ == "__main__":
    main()
