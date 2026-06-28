from __future__ import annotations

import json

import pytest

from design_review.core.consult import ConsultEngine, ConsultRequest
from design_review.core.consult.guard import prepare_request
from design_review.core.consult.parse import parse_advice
from design_review.core.consult.prompt import render_consult_prompt
from design_review.core.consultants import CONSULTANTS_DIR
from design_review.core.consultants.loader import load_consultant
from design_review.providers.base import ModelResponse


class _ConsultBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(
        self,
        *,
        model,
        system,
        user,
        temperature=0.3,
        top_p=0.95,
        max_tokens=4096,
        effort=None,
        endpoint_id=None,
    ):
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "effort": effort,
                "endpoint_id": endpoint_id,
            }
        )
        if model == "bad-model":
            return ModelResponse(model=model, error="401 invalid api key")
        content = {
            "summary": f"{model} suggests isolating the failing boundary.",
            "likely_causes": ["configuration drift"],
            "next_experiments": ["run the smallest reproduction"],
            "solution_options": ["add a guard and a fake-backend test"],
            "risks": ["external calls may leak secrets"],
            "recommended_plan": ["start with consult_problem MVP"],
            "confidence": 0.8,
        }
        return ModelResponse(model=model, content=json.dumps(content), usage={"total_tokens": 42}, cost_usd=0.002)


def _panel(model: str) -> dict:
    return {"label": model, "model": model, "endpoint_id": None}


def test_parse_advice_from_fenced_json():
    content = """```json
    {"summary":"s","likely_causes":["c"],"confidence":0.7}
    ```"""
    advice = parse_advice(content, model="m", consultant="debugger", advice_id="m/debugger-0")
    assert advice is not None
    assert advice.summary == "s"
    assert advice.likely_causes == ["c"]
    assert advice.confidence == 0.7


def test_guard_redacts_and_truncates():
    req = ConsultRequest(problem="debug this", logs="OPENAI_API_KEY=sk-abcdef1234567890\n" + "x" * 200)
    sanitized, meta = prepare_request(req, max_input_chars=80)
    assert "sk-abcdef" not in sanitized.logs
    assert "[REDACTED]" in sanitized.logs
    assert meta["redacted_items"] >= 1
    assert "logs" in meta["truncated_fields"]


def test_prompt_includes_semantic_fields():
    role = load_consultant("debugger", CONSULTANTS_DIR)
    req = ConsultRequest(
        problem="flaky tests",
        current_attempt="restarted the MCP server",
        why_stuck="tool schema still looks stale",
        question="what should I check next?",
        desired_output="one likely cause and one experiment",
    )
    _, user = render_consult_prompt(req, role)
    assert "## 当前尝试" in user
    assert "restarted the MCP server" in user
    assert "## 卡住原因" in user
    assert "## 想请外援回答的问题" in user
    assert "## 期望输出" in user


def test_consult_mode_resolution():
    from design_review.server import _resolve_consultants

    defaults = {"consult_consultants": ["debugger", "critic"], "consult_mode": None}
    assert _resolve_consultants(None, "challenge", defaults) == (["challenge", "critic"], "challenge")
    assert _resolve_consultants(["debugger"], "challenge", defaults) == (["debugger"], "challenge")
    assert _resolve_consultants(None, None, defaults) == (["debugger", "critic"], None)


@pytest.mark.asyncio
async def test_consult_engine_success_and_failed_model():
    backend = _ConsultBackend()
    engine = ConsultEngine(backend=backend, consultants_dir=CONSULTANTS_DIR)
    report = await engine.consult(
        ConsultRequest(problem="tests are flaky", logs="AssertionError"),
        panel=[_panel("good-model"), _panel("bad-model")],
        consultants=["debugger"],
        max_cost_usd=None,
        effort="low",
    )
    got = report.to_dict()
    assert got["summary"].startswith("good-model suggests")
    assert got["likely_causes"] == ["configuration drift"]
    assert got["failed_models"][0]["model"] == "bad-model"
    assert got["failed_models"][0]["type"] == "auth_error"
    assert got["usage"]["total_tokens"] == 42
    assert backend.calls[0]["effort"] == "low"


@pytest.mark.asyncio
async def test_consult_engine_rejects_unknown_consultant():
    engine = ConsultEngine(backend=_ConsultBackend(), consultants_dir=CONSULTANTS_DIR)
    with pytest.raises(ValueError, match="未知 consultant"):
        await engine.consult(
            ConsultRequest(problem="x"),
            panel=[_panel("good-model")],
            consultants=["does_not_exist"],
        )


@pytest.mark.asyncio
async def test_consult_engine_budget_trims_jobs(monkeypatch):
    from design_review.core.stages import review as review_stage

    monkeypatch.setattr(review_stage, "_estimate_job_cost", lambda job: 0.02)
    backend = _ConsultBackend()
    engine = ConsultEngine(backend=backend, consultants_dir=CONSULTANTS_DIR)
    report = await engine.consult(
        ConsultRequest(problem="x"),
        panel=[_panel("a"), _panel("b")],
        consultants=["debugger"],
        max_cost_usd=0.03,
    )
    assert len(backend.calls) == 1
    assert report.budget["jobs_total"] == 2
    assert report.budget["jobs_run"] == 1
    assert report.budget["exhausted"] is True
