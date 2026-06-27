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
        policy: Any = None,
    ) -> None:
        self.adapter = adapter
        self.backend = backend
        self.knowledge = knowledge
        self.pipeline = pipeline
        self.defaults = defaults or {}
        self.policy = policy  # v1.7 PrivacyPolicy（None=不脱敏）

    async def review(
        self,
        document: ReviewDocument,
        *,
        panel: list[dict] | None = None,  # v1.6: PanelEntry{label, model, endpoint_id}（无 credential）
        dimensions: list[str] | None = None,
        retrieve_top_k: int = 5,
        extra_context: str = "",
        effort: str | None = None,
        max_cost_usd: float | None = None,
        policy: Any = None,  # v1.7 PrivacyPolicy（None=不脱敏）
        context_modes: dict | None = None,  # v1.8 per-dimension 上下文压缩 {dim: full|compressed|minimal}
        reliability: dict | None = None,  # v2 {(label,dim):0~1} 模型可信度（server 算好纯 dict 注入，core 不依赖 reviews_db）
    ) -> PipelineContext:
        """跑一次完整审查，返回填充好的 PipelineContext（含 ctx.report）。"""
        policy = policy if policy is not None else self.policy  # v1.7 默认用 engine 装配的 policy
        raw_panel = list(panel or self.defaults.get("panel") or [])
        # v1.6: 统一成 PanelEntry dict（str→官方 entry；dict 原样，server 已 normalize endpoint_id）。
        # 让 engine 可直接被传 str 列表调用（测试/便捷），server 传 dict 也兼容。
        normalized_panel = [
            {"label": p, "model": p, "endpoint_id": None} if isinstance(p, str) else p
            for p in raw_panel
        ]
        # v1.7 隐私策略：transform 在 pipeline 外（PromptStage 拿 effective_doc 不知 strict 存在）。
        if policy is not None:
            tr = await policy.transform(document, self.backend)
            effective_doc = tr.document
            privacy_meta = {
                "policy": policy.name,
                "coverage": tr.coverage,
                "missing_topics": list(tr.missing_topics),
                "redacted_items": list(tr.redacted_items),
                "trusted": getattr(policy, "trusted", {}).get("label")
                if hasattr(policy, "trusted")
                else None,
            }
        else:
            effective_doc = document
            privacy_meta = {}
        ctx = PipelineContext(
            document=effective_doc,
            adapter=self.adapter,
            backend=self.backend,
            knowledge=self.knowledge,
            panel=normalized_panel,
            dimensions=list(dimensions or self.defaults.get("dimensions") or []),
            retrieve_top_k=retrieve_top_k,
            extra_context=extra_context,
            effort=effort or self.defaults.get("effort"),
            max_cost_usd=max_cost_usd if max_cost_usd is not None else self.defaults.get("max_cost_usd"),
            context_modes=context_modes if context_modes is not None else (self.defaults.get("context_modes") or {}),
            min_compressed_chars=int(self.defaults.get("min_compressed_chars", 50)),
            reliability=reliability or {},
            original_document=document,
            policy=policy,
            privacy_meta=privacy_meta,
        )
        await self.pipeline.run(ctx)
        return ctx
