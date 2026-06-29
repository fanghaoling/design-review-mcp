"""Deterministic synthesis for consultation responses."""
from __future__ import annotations

from statistics import mean

from .report import ConsultAdvice, ConsultReport


def _extend_unique(target: list[str], values: list[str], *, limit: int) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)
        if len(target) >= limit:
            return


def synthesize_report(
    *,
    consultation_id: str,
    advice: list[ConsultAdvice],
    failed_models: list[dict],
    usage: dict,
    budget: dict,
    guard: dict,
) -> ConsultReport:
    if not advice:
        return ConsultReport(
            consultation_id=consultation_id,
            summary="外援会诊未获得可用模型输出。",
            confidence=0.0,
            failed_models=failed_models,
            usage=usage,
            budget=budget,
            guard=guard,
        )

    likely_causes: list[str] = []
    next_experiments: list[str] = []
    solution_options: list[str] = []
    risks: list[str] = []
    recommended_plan: list[str] = []
    for item in advice:
        _extend_unique(likely_causes, item.likely_causes, limit=12)
        _extend_unique(next_experiments, item.next_experiments, limit=12)
        _extend_unique(solution_options, item.solution_options, limit=12)
        _extend_unique(risks, item.risks, limit=12)
        _extend_unique(recommended_plan, item.recommended_plan, limit=12)

    success_ratio = len(advice) / max(len(advice) + len(failed_models), 1)
    confidence = round(mean([a.confidence for a in advice]) * success_ratio, 3)
    summary = advice[0].summary
    if len(advice) > 1:
        summary = f"收到 {len(advice)} 条外援建议。{summary}"

    disagreements: list[str] = []
    if len(advice) > 1 and len(solution_options) > len(advice):
        disagreements.append("外援给出了多个不同方案方向，建议先按 next_experiments 验证根因后再选实现路径。")

    return ConsultReport(
        consultation_id=consultation_id,
        summary=summary,
        likely_causes=likely_causes,
        next_experiments=next_experiments,
        solution_options=solution_options,
        risks=risks,
        disagreements=disagreements,
        recommended_plan=recommended_plan,
        confidence=confidence,
        individual=advice,
        failed_models=failed_models,
        usage=usage,
        budget=budget,
        guard=guard,
    )
