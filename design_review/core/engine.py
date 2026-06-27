"""ReviewEngine：装配 adapter/backend/knowledge/pipeline，跑一次审查。

引擎本身**不依赖具体 Stage 实现**（避免 core 反向依赖 stages 子包）；完整 pipeline 由
调用方（server 装配）构造后注入。这样 v2 可注入含 DebateStage 的不同 pipeline，引擎不动。
"""
from __future__ import annotations

from typing import Any

from .document import ReviewDocument
from .pipeline import Pipeline, PipelineContext


class ReviewEngine:
    """装配各可换层并驱动 Pipeline。"""

    def __init__(
        self,
        *,
        adapter: Any,
        backend: Any,
        knowledge: Any,
        pipeline: Pipeline,
        defaults: dict | None = None,
    ) -> None:
        self.adapter = adapter
        self.backend = backend
        self.knowledge = knowledge
        self.pipeline = pipeline
        self.defaults = defaults or {}

    async def review(
        self,
        document: ReviewDocument,
        *,
        panel: list[str] | None = None,
        dimensions: list[str] | None = None,
        retrieve_top_k: int = 5,
        extra_context: str = "",
        effort: str | None = None,
        max_cost_usd: float | None = None,
    ) -> PipelineContext:
        """跑一次完整审查，返回填充好的 PipelineContext（含 ctx.report）。"""
        ctx = PipelineContext(
            document=document,
            adapter=self.adapter,
            backend=self.backend,
            knowledge=self.knowledge,
            panel=list(panel or self.defaults.get("panel") or []),
            dimensions=list(dimensions or self.defaults.get("dimensions") or []),
            retrieve_top_k=retrieve_top_k,
            extra_context=extra_context,
            effort=effort or self.defaults.get("effort"),
            max_cost_usd=max_cost_usd if max_cost_usd is not None else self.defaults.get("max_cost_usd"),
        )
        await self.pipeline.run(ctx)
        return ctx
