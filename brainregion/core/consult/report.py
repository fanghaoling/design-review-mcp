"""Structured reports for external consultation tools."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class ConsultRequest:
    """User-provided problem statement and optional supporting context."""

    problem: str
    context: str = ""
    files: dict[str, str] = field(default_factory=dict)
    logs: str = ""
    attempts: list[str] = field(default_factory=list)
    goal: str = ""
    current_attempt: str = ""
    why_stuck: str = ""
    question: str = ""
    desired_output: str = ""
    constraints: list[str] = field(default_factory=list)


@dataclass
class ConsultAdvice:
    """One consultant/model response after parsing and normalization."""

    id: str
    model: str
    consultant: str
    summary: str = ""
    likely_causes: list[str] = field(default_factory=list)
    next_experiments: list[str] = field(default_factory=list)
    solution_options: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommended_plan: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class ConsultReport:
    """Stable MCP-facing consultation result."""

    consultation_id: str = ""
    summary: str = ""
    likely_causes: list[str] = field(default_factory=list)
    next_experiments: list[str] = field(default_factory=list)
    solution_options: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    recommended_plan: list[str] = field(default_factory=list)
    confidence: float = 0.0
    individual: list[ConsultAdvice] = field(default_factory=list)
    failed_models: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    guard: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "consultation_id": self.consultation_id,
            "summary": self.summary,
            "likely_causes": list(self.likely_causes),
            "next_experiments": list(self.next_experiments),
            "solution_options": list(self.solution_options),
            "risks": list(self.risks),
            "disagreements": list(self.disagreements),
            "recommended_plan": list(self.recommended_plan),
            "confidence": self.confidence,
            "individual": [a.to_dict() for a in self.individual],
            "failed_models": list(self.failed_models),
            "usage": dict(self.usage),
            "budget": dict(self.budget),
            "guard": dict(self.guard),
            "routing": dict(self.routing),
        }
