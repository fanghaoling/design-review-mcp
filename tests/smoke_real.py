"""真实 LLM 冒烟测试（花钱）。从 env 读 key，不硬编码。

需 OPENAI_API_KEY。用 gpt-4o 单模型 + ecs_perf 维度，验证真 LLM 调用链路：
retrieve → context → prompt → review → parse → normalize → consensus → score。
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # brain-region-mcp/
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")  # 加载 .env（脚本直接跑时手动加载；MCP server 启动时 server.py 也会加载）
UNITY_PROJECT = ROOT.parent.parent  # My project（brain-region-mcp → Tools → My project）


async def main() -> None:
    missing = [k for k in ("OPENAI_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY", "ARK_API_KEY") if not os.environ.get(k)]
    if missing:
        print("ERROR: 未设 env:", missing)
        return
    os.environ["UNITY_PROJECT_ROOT"] = str(UNITY_PROJECT)  # 让 config/reviews_db 路径对
    from brainregion import defaults as defaults_mod
    from brainregion.adapters.unity import UnityAdapter
    from brainregion.core import ReviewDocument
    from brainregion.core.engine import ReviewEngine
    from brainregion.core.stages import build_default_pipeline
    from brainregion.knowledge import YamlKnowledgeProvider
    from brainregion.providers import LiteLLMBackend

    dd = defaults_mod.apply()
    a = UnityAdapter(str(UNITY_PROJECT))
    kp = YamlKnowledgeProvider(a.knowledge_dir())
    eng = ReviewEngine(
        adapter=a, backend=LiteLLMBackend(), knowledge=kp,
        pipeline=build_default_pipeline(normalizer_model=dd.get("normalizer_model", "gpt-4o")),
        defaults=dd,
    )
    print("panel:", dd["panel"], "| normalizer:", dd.get("normalizer_model"))
    doc = ReviewDocument.markdown(
        "## 待审方案\n"
        "在 ISystem.OnUpdate 里调 [BurstCompile] static void Foo(MyStruct s) 按值传 struct。\n"
        "请审查这段是否能通过 Burst 编译并在 Android AOT 正常运行。"
    )
    ctx = await eng.review(doc, dimensions=["ecs_perf"])  # panel=None → 用 config.json 默认面板
    r = ctx.report
    print("=== 真实 gpt-4o 冒烟结果 ===")
    print("failed_models:", r.failed_models)
    print("retrieved_cases:", r.retrieved_cases)
    print("consensus:", [(c.canonical_title, c.severity, c.case_ref, c.calibrated_confidence)
                         for c in r.consensus])
    print("individual:", {k: [f.title for f in v] for k, v in r.individual.items()})
    print("knowledge_hit:", r.knowledge_hit)
    print("usage:", r.usage, "| risk:", r.risk)


if __name__ == "__main__":
    asyncio.run(main())
