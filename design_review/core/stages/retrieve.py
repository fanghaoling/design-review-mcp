"""RetrieveStage：从知识库 retrieve 相关案例（版本过滤）。

Pipeline 第 1 步。读项目版本 → 在文档正文上关键词 retrieve top_k 案例。
"""
from __future__ import annotations

from ..pipeline import PipelineContext


class RetrieveStage:
    name = "retrieve"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.project_version = ctx.adapter.read_version()
        text = ctx.document.content or ""
        if ctx.document.files:
            text += "\n" + "\n".join(ctx.document.files.values())
        ctx.retrieved_cases = ctx.knowledge.retrieve(
            text, ctx.project_version, ctx.retrieve_top_k
        )
        return ctx
