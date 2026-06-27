"""ContextStage：聚合 ground-truth context（B5 下沉）。

Pipeline 第 2 步。聚合 adapter.read_context + 渲染案例 + adapter.read_convention + extra_context，
供 PromptStage 注入 system。调用方无需传 context（由 adapter 内部完成）。
"""
from __future__ import annotations

from ..pipeline import PipelineContext
from ...knowledge import render_for_prompt


class ContextStage:
    name = "context"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        parts: list[str] = []
        adapter_ctx = ctx.adapter.read_context()
        if adapter_ctx:
            parts.append(adapter_ctx)
        rendered = render_for_prompt(ctx.retrieved_cases)
        parts.append("## 项目历史踩坑（ground truth，命中本文档的相关案例）\n" + rendered)
        conv = ctx.adapter.read_convention()
        if conv:
            parts.append(conv)
        if ctx.extra_context:
            parts.append(ctx.extra_context)
        ctx.context = "\n\n".join(parts)
        return ctx
