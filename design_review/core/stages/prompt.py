"""PromptStage：按 reviewer(独立 system_prompt+采样) × panel × document_type 渲染 prompts。

Pipeline 第 3 步。每个维度从 yaml 加载角色（adapter 优先，回退 core 通用），与 panel 做笛卡尔积，
每 job 独立 temperature/top_p/max_tokens（B4）。强制 evidence_quote + 输出 JSON schema。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..pipeline import PipelineContext, Stage
from ..reviewers.loader import list_reviewers, load_reviewer
from ..schema import get_schema

# 默认通用维度：审计划完整性 + 边界（任何项目都有价值）
_DEFAULT_CORE_DIMS = ("planner", "safety")

_OUTPUT_TEMPLATE = json.dumps(
    {
        "issues": [
            {
                "dimension": "<维度>",
                "severity": "high|medium|low",
                "title": "<一句话问题>",
                "evidence_quote": "<引用原文片段，必填，无引用不要提>",
                "location": "<file:line 或段落>",
                "suggestion": "<如何修复>",
                "confidence": 0.0,
                "case_ref": None,
            }
        ]
    },
    ensure_ascii=False,
    indent=2,
)


class PromptStage:
    name = "prompt"

    def __init__(
        self,
        core_reviewers_dir: str | Path,
        default_dimensions: list[str] | None = None,
    ) -> None:
        self.core_dir = Path(core_reviewers_dir)
        self.default_dimensions = default_dimensions

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        dims = (
            ctx.dimensions
            or self.default_dimensions
            or _auto_dimensions(ctx.adapter, self.core_dir)
        )
        reviewers = {d: _load_role(d, ctx.adapter, self.core_dir) for d in dims}
        schema_text = json.dumps(get_schema("finding"), ensure_ascii=False, indent=2)
        ctx.prompts = []
        for entry in ctx.panel:
            for dim, role in reviewers.items():
                ctx.prompts.append(
                    {
                        "model": entry["model"],  # 上游真名（传 litellm）
                        "label": entry["label"],  # 身份标识（贯穿 Finding.model/flagged_by）
                        "endpoint_id": entry.get("endpoint_id"),  # None=官方走 env
                        "dimension": dim,
                        "system": _system(role, ctx.context),
                        "user": _user(ctx.document, role, dim, schema_text),
                        "temperature": float(role.get("temperature", 0.3)),
                        "top_p": float(role.get("top_p", 0.95)),
                        "max_tokens": int(role.get("max_tokens", 4096)),
                    }
                )
        return ctx


def _auto_dimensions(adapter, core_dir: Path) -> list[str]:
    """默认维度 = core 通用核心 + adapter 全部特定。"""
    dims: list[str] = []
    for d in _DEFAULT_CORE_DIMS:
        if (core_dir / f"{d}.yaml").exists():
            dims.append(d)
    ad = adapter.reviewers_dir()
    if ad.exists():
        dims += [p.stem for p in sorted(ad.glob("*.yaml"))]
    return dims


def _load_role(dim: str, adapter, core_dir: Path) -> dict:
    """adapter reviewers_dir 优先（inherits 回退 core），没有则直接 core。"""
    ad = adapter.reviewers_dir()
    if (ad / f"{dim}.yaml").exists():
        return load_reviewer(dim, ad, fallback_dir=core_dir)
    return load_reviewer(dim, core_dir, fallback_dir=core_dir)


def _system(role: dict, context: str) -> str:
    parts = [role.get("system_prompt", "").strip()]
    if context:
        parts.append("## 项目 ground truth（以此为准，不要硬套通用教程）\n" + context)
    return "\n\n".join(p for p in parts if p)


def _user(document, role: dict, dim: str, schema_text: str) -> str:
    parts = [f"## 审查维度：{dim}"]
    checklist = role.get("focus_checklist") or []
    if checklist:
        parts.append("## 审查重点\n" + "\n".join(f"- {c}" for c in checklist))
    if document.type == "code" and document.files:
        files_block = "\n\n".join(
            f"### {p}\n```\n{c}\n```" for p, c in document.files.items()
        )
        parts.append("## 待审代码\n" + files_block)
    else:
        parts.append("## 待审方案/文档\n" + (document.content or ""))
    parts.append(
        "## 输出格式（严格 JSON，单对象含 issues 数组；无问题则 issues 为空数组）\n```json\n"
        + _OUTPUT_TEMPLATE
        + "\n```"
    )
    parts.append("## 每条 issue 须满足的 finding schema\n```json\n" + schema_text + "\n```")
    return "\n\n".join(parts)
