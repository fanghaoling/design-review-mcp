"""PromptStage：按 reviewer(独立 system_prompt+采样) × panel × document_type 渲染 prompts。

Pipeline 第 3 步。每个维度从 yaml 加载角色（adapter 优先，回退 core 通用），与 panel 做笛卡尔积，
每 job 独立 temperature/top_p/max_tokens（B4）。强制 evidence_quote + 输出 JSON schema。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..pipeline import PipelineContext
from ..reviewers.loader import load_reviewer
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


@dataclass
class CompressedDocument:
    """_compress_document 返回（v1.8）：压缩元数据 + 压缩后 content。报告显示压缩比，debug 有用。"""

    content: str
    mode: str
    original_chars: int
    compressed_chars: int


_FENCED_RE = re.compile(r"```[\s\S]*?```")


def _compress_document(content: str, mode: str, min_chars: int = 50) -> CompressedDocument:
    """按 context_mode 压缩方案文本（v1.8 通用，不解析 markdown AST：保留 headings/list 行、
    去 fenced code、段落截断——对 txt/json 也 work）。未知 mode / 压缩后过短 → fallback full
    （防 code 主体去 code 后变空）。
    """
    original = content or ""
    olen = len(original)
    if mode == "full" or not original:
        return CompressedDocument(original, "full", olen, olen)
    if mode == "minimal":
        lines = original.split("\n")
        headings = [line for line in lines if line.lstrip().startswith("#")]
        # first_para：第一个非 heading、非空段（跳标题段取实质内容，否则 split[0] 常是 heading 重复）
        paras = original.split("\n\n")
        first_para = next((p for p in paras if p.strip() and not p.strip().startswith("#")), "")
        result = ("\n".join(headings) + ("\n\n" + first_para if first_para else "")).strip()
    elif mode == "compressed":
        no_code = _FENCED_RE.sub("", original)
        kept = []
        for line in no_code.split("\n"):
            s = line.lstrip()
            if not s:
                continue
            if s.startswith("#") or s.startswith(("- ", "* ")):
                kept.append(line)  # 保留 headings/list（LLM 依赖结构）
            else:
                kept.append(line[:200])  # 段落截断长行
        result = "\n".join(kept)
    else:  # 未知 mode fallback full
        return CompressedDocument(original, "full", olen, olen)
    if len(result.strip()) < min_chars:  # 下限保护
        return CompressedDocument(original, "full", olen, olen)
    return CompressedDocument(result, mode, olen, len(result))


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
                # v1.8 按 ctx.context_modes 压缩该维度 document（per-dimension 策略）
                mode = ctx.context_modes.get(dim, "full")
                comp = _compress_document(ctx.document.content or "", mode, ctx.min_compressed_chars)
                if dim not in ctx.context_compression:
                    ctx.context_compression[dim] = {
                        "mode": comp.mode,
                        "original_chars": comp.original_chars,
                        "compressed_chars": comp.compressed_chars,
                        "ratio": round(comp.compressed_chars / max(comp.original_chars, 1), 3),
                    }
                ctx.prompts.append(
                    {
                        "model": entry["model"],  # 上游真名（传 litellm）
                        "label": entry["label"],  # 身份标识（贯穿 Finding.model/flagged_by）
                        "endpoint_id": entry.get("endpoint_id"),  # None=官方走 env
                        "dimension": dim,
                        "system": _system(role, ctx.context),
                        "user": _user(ctx.document, role, dim, schema_text, comp.content if mode != "full" else None),
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


def _user(document, role: dict, dim: str, schema_text: str, content_override: str | None = None) -> str:
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
        # v1.8: content_override 是按 context_mode 压缩后的方案（None=原样 full）
        parts.append("## 待审方案/文档\n" + (content_override if content_override is not None else (document.content or "")))
    parts.append(
        "## 输出格式（严格 JSON，单对象含 issues 数组；无问题则 issues 为空数组）\n```json\n"
        + _OUTPUT_TEMPLATE
        + "\n```"
    )
    parts.append("## 每条 issue 须满足的 finding schema\n```json\n" + schema_text + "\n```")
    return "\n\n".join(parts)
