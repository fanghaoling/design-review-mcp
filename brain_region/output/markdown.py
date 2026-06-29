"""Markdown 渲染器（人读）。"""
from __future__ import annotations

from ..core.report import CanonicalFinding, ReviewReport

_SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def render(report: ReviewReport) -> str:
    lines: list[str] = [f"# BrainRegion Review — {report.adapter}", ""]
    lines.append(
        f"**文档类型:** {report.document_type} | **风险:** {report.risk.get('overall_level', '?')} "
        f"| **panel:** {', '.join(report.panel)}"
    )
    if report.project_version:
        lines.append(
            "**项目版本:** "
            + ", ".join(f"{k}={v}" for k, v in report.project_version.items())
        )
    lines.append("")

    if report.failed_models:
        lines.append("## ⚠️ 失败模型")
        lines += [f"- {m['model']}: {m['error']}" for m in report.failed_models]
        lines.append("")

    ps = report.panel_status or {}
    if ps.get("complete", True):
        lines.append("## ✅ Consensus（全模型同意）")
    else:
        lines.append(
            f"## ✅ Consensus（⚠️ panel 不完整 {ps.get('ran')}/{ps.get('requested')}，"
            "未做交叉验证）"
        )
    lines += [_finding_md(c) for c in report.consensus] or ["_(无)_"]
    lines.append("")

    lines.append("## 🔶 Majority（多数同意）")
    lines += [_finding_md(c) for c in report.majority] or ["_(无)_"]
    lines.append("")

    lines.append("## 🔵 Individual（单模型）")
    if report.individual:
        for model, fs in report.individual.items():
            for f in fs:
                lines.append(f"- **[{f.severity}]** ({model}) `{f.location}` {f.title}")
                lines.append(f"  - evidence: {f.evidence_quote}")
                lines.append(f"  - 建议: {f.suggestion}")
    else:
        lines.append("_(无)_")
    lines.append("")

    if report.knowledge_hit:
        lines.append("## 📚 命中知识库")
        lines.append(", ".join(report.knowledge_hit))
        lines.append("")

    lines.append("## 汇总")
    lines.append(report.summary)
    lines.append("")
    lines.append(
        f"**Usage:** {report.usage.get('total_tokens', 0)} tokens, "
        f"${report.usage.get('cost_usd', 0)}"
    )
    return "\n".join(lines)


def _finding_md(c: CanonicalFinding) -> str:
    icon = _SEV_ICON.get(c.severity, "⚪")
    s = f"- {icon} **[{c.severity}]** `{c.location}` {c.canonical_title}"
    tail = f"_(置信度 {c.calibrated_confidence}; 标注: {', '.join(c.flagged_by)}"
    if c.case_ref:
        tail += f"; 案例 {c.case_ref}"
    tail += ")_"
    s += "  " + tail
    s += f"\n  - evidence: {c.evidence_quote}"
    s += f"\n  - 建议: {c.suggestion}"
    return s
