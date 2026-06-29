"""ReportRenderer 协议：把 ReviewReport 渲染成某种格式（Markdown/JSON/SARIF）。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core.report import ReviewReport


@runtime_checkable
class ReportRenderer(Protocol):
    name: str

    def render(self, report: ReviewReport) -> str: ...
