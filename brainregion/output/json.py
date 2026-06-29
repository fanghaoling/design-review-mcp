"""JSON 渲染器（程序消费，MCP 工具默认返回）。"""
from __future__ import annotations

import json

from ..core.report import ReviewReport


def render(report: ReviewReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
