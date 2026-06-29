"""SARIF 2.1.0 渲染器（进 GitHub Action CI / IDE）。

把 consensus + majority 映射成 SARIF results（high→error, medium→warning, low→note）。
"""
from __future__ import annotations

import json

from ..core.report import ReviewReport

_LEVEL = {"high": "error", "medium": "warning", "low": "note"}


def render(report: ReviewReport) -> str:
    results: list[dict] = []
    for c in report.consensus + report.majority:
        loc = c.location.split(":")[0] if ":" in c.location else c.location
        results.append(
            {
                "ruleId": c.dimension,
                "level": _LEVEL.get(c.severity, "note"),
                "message": {
                    "text": f"{c.canonical_title}\n\n证据: {c.evidence_quote}\n建议: {c.suggestion}"
                },
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": loc}}}
                ],
                "properties": {
                    "calibrated_confidence": c.calibrated_confidence,
                    "flagged_by": c.flagged_by,
                    "case_ref": c.case_ref,
                    "bucket": c.bucket,
                },
            }
        )
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "brain-region-mcp", "version": "0.1.0"}},
                "results": results,
            }
        ],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)
