"""meta-eval：用知识库种子案例验证 review 召回率（需真 API key）。

业界无 review 召回率公开基准，用项目自己的真实踩坑案例集做自建评测：
为每条种子案例构造一个"含该 bug 的方案片段"探针，跑 review_plan（真 LLM），统计：
- 该 bug 是否被至少一个模型标出（召回）
- 是否引用对 case_ref
- consensus/majority 命中情况

需配 OPENAI_API_KEY / ANTHROPIC_API_KEY / ARK_API_KEY。手动跑：
    uv run --extra dev python scripts/meta_eval.py

结果用于迭代 prompt 模板 / reviewer checklist。v2 同源数据驱动 Review Memory + 模型可信度。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
UNITY_PROJECT = ROOT.parent.parent  # ROOT=Tools/brain-region-mcp → 上两级 = 游戏项目根

from brainregion.adapters.unity import UnityAdapter
from brainregion.core import ReviewDocument
from brainregion.core.engine import ReviewEngine
from brainregion.core.stages import build_default_pipeline
from brainregion.knowledge import YamlKnowledgeProvider
from brainregion.providers import LiteLLMBackend

# 每条种子案例的"含 bug 方案"探针（人工构造，模拟真实会写出的错误代码/方案）。
# 这些探针对应 framework 随包的【通用】案例。项目特定案例（自家网络同步设计等）在本地
# <项目>/.brain-region/knowledge/，不在公开库——用本项目做 meta-eval 时另起本地脚本追加。
PROBES = {
    "ECS-BURST-001": "在 ISystem.OnUpdate 里调 [BurstCompile] static void Foo(MyStruct s) 按值传 struct。",
    "ECS-BURST-002": "在 [BurstCompile] 方法里读 static bool Enable 这个运行时开关字段决定是否执行。",
    "ECS-BURST-003": "在 Burst job 里用 Stopwatch.GetTimestamp() 做高精度计时。",
    "NET-PREDICT-TICK": "在 prediction system 里直接读 IInputComponentData 的字段判断当前输入状态。",
    "NET-GHOSTBIT-QUERY": "用 SystemAPI.Query<MyGhostEnabledBitComp>() 遍历带 [GhostEnabledBit] 的组件。",
    "ECS-STRUCT-001": "托管 OnUpdate 里 foreach 遍历 DynamicBuffer 的同时 EntityManager.CreateEntity。",
    "ECS-STRUCT-002": "if (query == null) query = GetEntityQuery(...); 然后 query.ToEntityArray()。",
    "ECS-STRUCT-004": "OnUpdate 里 GetSingleton<T>() 但 OnCreate 没配 RequireForUpdate<T>()。",
    "FF-001": "FlowField cost field 只从渲染方块 buffer 取数据生成，不查物理 collider。",
}


async def main() -> None:
    a = UnityAdapter(str(UNITY_PROJECT))
    dirs = [a.knowledge_dir()]
    dirs.extend(path for path in a.local_knowledge_dirs() if path.exists())
    kp = YamlKnowledgeProvider(dirs)
    eng = ReviewEngine(
        adapter=a, backend=LiteLLMBackend(), knowledge=kp,
        pipeline=build_default_pipeline(),
    )
    panel = ["claude-opus-4-8", "gpt-5"]
    results = []
    for case_id, probe in PROBES.items():
        doc = ReviewDocument.markdown(probe)
        ctx = await eng.review(doc, panel=panel, dimensions=["ecs_perf", "netcode", "safety"])
        r = ctx.report
        all_c = r.consensus + r.majority
        # 召回：有 finding 的 case_ref 命中，或 consensus/majorory 非空（语义命中需人工复核）
        ref_hit = any(c.case_ref == case_id for c in all_c)
        results.append(
            {"case": case_id, "ref_hit": ref_hit, "consensus": len(r.consensus),
             "majority": len(r.majority), "summary": r.summary}
        )
        print(f"{case_id}: ref_hit={ref_hit} consensus={len(r.consensus)} majority={len(r.majority)} | {r.summary}")

    ref_recalled = sum(1 for x in results if x["ref_hit"])
    flagged = sum(1 for x in results if x["consensus"] or x["majority"])
    n = len(results)
    print(f"\n=== meta-eval 汇总 ({n} 探针) ===")
    print(f"case_ref 精确命中: {ref_recalled}/{n} = {ref_recalled/n:.0%}")
    print(f"有共识/多数发现(语义命中需人工复核): {flagged}/{n} = {flagged/n:.0%}")
    print("\n注：ref_hit 是精确指标（模型填对 case_ref）；语义命中需人工看 finding 是否对应 bug。")


if __name__ == "__main__":
    asyncio.run(main())
