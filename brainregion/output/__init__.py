"""ReportRenderer：Markdown / JSON / SARIF 输出。"""
from __future__ import annotations

from . import json as json_renderer
from . import markdown, sarif
from .base import ReportRenderer
from ..core.report import ReviewReport

_RENDERERS = {
    "markdown": markdown.render,
    "json": json_renderer.render,
    "sarif": sarif.render,
}


def render(report: ReviewReport, fmt: str = "json") -> str:
    """按格式渲染报告。"""
    r = _RENDERERS.get(fmt)
    if r is None:
        raise ValueError(f"未知 output_format: {fmt}，可用: {sorted(_RENDERERS)}")
    return r(report)


def formats() -> list[str]:
    return sorted(_RENDERERS)


__all__ = ["ReportRenderer", "render", "formats", "markdown", "sarif"]
