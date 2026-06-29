"""MediateStage（v1.7，thin wrapper）：调 ctx.policy.mediate 给 findings 附加 trusted attachment。

逻辑全在 PrivacyPolicy.mediate（strict=trusted 逐条评估附加 FindingAttachment，不改原字段）。
off 模式 pipeline 不插此 stage。位置：Parse 后、Dedup 前——对抗 findings 刚解析，附加 trusted
评估后再去重/归一/共识。
"""
from __future__ import annotations

from ..pipeline import PipelineContext


class MediateStage:
    name = "mediate"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.policy is not None and ctx.findings:
            ctx.findings = await ctx.policy.mediate(
                ctx.findings, ctx.original_document, ctx.backend
            )
        return ctx
