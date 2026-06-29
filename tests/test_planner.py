from __future__ import annotations

import json

import pytest

from brainregion.core.planner import PlanReport, PlanRequest, PlannerEngine, parse_plan, prepare_plan_request
from brainregion.providers.base import ModelResponse


class _PlannerBackend:
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
        if model == "bad-json":
            return ModelResponse(model=model, content="not json", usage={"total_tokens": 3}, cost_usd=0.001)
        content = {
            "summary": "Ship the planner as a thin MCP tool first.",
            "milestones": [
                {
                    "name": "MVP",
                    "goal": "Expose plan_task",
                    "tasks": ["add planner module", "add tests"],
                    "acceptance_criteria": ["tool returns JSON"],
                }
            ],
            "tasks": [
                {
                    "title": "Add plan_task",
                    "description": "Wire a single-model planning call through MCP.",
                    "dependencies": [],
                    "acceptance_criteria": ["pytest passes"],
                }
            ],
            "dependencies": ["consult panel configuration"],
            "risks": [{"risk": "schema creep", "mitigation": "keep MVP fields small"}],
            "acceptance_criteria": ["plan is actionable"],
            "test_plan": ["unit test parse and routing"],
            "open_questions": ["Should planner_panel be configured?"],
            "confidence": 0.81,
        }
        return ModelResponse(model=model, content=json.dumps(content), usage={"total_tokens": 42}, cost_usd=0.002)


def _panel(model: str) -> dict:
    return {"label": model, "model": model, "endpoint_id": None}


def test_parse_plan_from_fenced_json():
    content = """```json
    {
      "summary": "s",
      "tasks": [{"title": "implement", "acceptance_criteria": ["done"]}],
      "risks": [{"risk": "r", "mitigation": "m"}],
      "confidence": 1.2
    }
    ```"""
    plan = parse_plan(content, plan_id="plan-test", model="planner-model")
    assert plan is not None
    assert plan.plan_id == "plan-test"
    assert plan.model == "planner-model"
    assert plan.tasks[0]["id"] == "T1"
    assert plan.tasks[0]["title"] == "implement"
    assert plan.risks == [{"risk": "r", "mitigation": "m"}]
    assert plan.confidence == 1.0


def test_parse_plan_ignores_trailing_prose_after_json():
    content = """
    Here is the plan:
    {
      "summary": "Use the first parseable object.",
      "tasks": [{"title": "parse tolerant output"}],
      "acceptance_criteria": ["parsed"]
    }

    Notes: I can also explain the reasoning if needed.
    """
    plan = parse_plan(content, plan_id="plan-tail", model="strong-model")
    assert plan is not None
    assert plan.summary == "Use the first parseable object."
    assert plan.tasks[0]["title"] == "parse tolerant output"


def test_parse_plan_accepts_jsonc_wrapper_and_trailing_commas():
    content = """```jsonc
    {
      "plan": {
        // some models add JSONC comments
        "summary": "Wrapped plan",
        "tasks": [
          {"title": "unwrap me",},
        ],
        "confidence": 0.6,
      },
    }
    ```"""
    plan = parse_plan(content, plan_id="plan-wrapper", model="strong-model")
    assert plan is not None
    assert plan.summary == "Wrapped plan"
    assert plan.tasks[0]["title"] == "unwrap me"
    assert plan.confidence == 0.6


def test_parse_plan_accepts_top_level_task_array():
    content = """
    [
      {"title": "first task", "acceptance_criteria": ["done"]},
      "second task"
    ]
    """
    plan = parse_plan(content, plan_id="plan-array", model="strong-model")
    assert plan is not None
    assert plan.tasks[0]["title"] == "first task"
    assert plan.tasks[1]["title"] == "second task"


def test_parse_plan_accepts_python_literal_style_dict():
    content = """```python
    {
      'summary': 'Single quoted plan',
      'tasks': [
        {'title': 'ship tolerant parser', 'dependencies': [],},
      ],
      'confidence': 0.7,
    }
    ```"""
    plan = parse_plan(content, plan_id="plan-literal", model="strong-model")
    assert plan is not None
    assert plan.summary == "Single quoted plan"
    assert plan.tasks[0]["title"] == "ship tolerant parser"
    assert plan.confidence == 0.7


def test_prepare_plan_request_redacts_and_preserves_success_criteria():
    request = PlanRequest(
        goal="Add planner. OPENAI_API_KEY=sk-abcdef1234567890",
        constraints=["keep it small"],
        success_criteria=["tests pass"],
    )
    sanitized, meta = prepare_plan_request(request, max_input_chars=200)
    assert "sk-abcdef" not in sanitized.goal
    assert "[REDACTED]" in sanitized.goal
    assert sanitized.constraints == ["keep it small"]
    assert sanitized.success_criteria == ["tests pass"]
    assert meta["redacted_items"] >= 1


@pytest.mark.asyncio
async def test_planner_engine_uses_first_parseable_plan_after_failures():
    backend = _PlannerBackend()
    engine = PlannerEngine(backend=backend)
    report = await engine.plan(
        PlanRequest(goal="Add a Planner MVP"),
        panel=[_panel("bad-model"), _panel("bad-json"), _panel("good-model")],
        max_cost_usd=None,
        effort="low",
        plan_id="plan-engine",
    )
    got = report.to_dict()
    assert got["plan_id"] == "plan-engine"
    assert got["model"] == "good-model"
    assert got["summary"].startswith("Ship the planner")
    assert got["tasks"][0]["id"] == "T1"
    assert got["failed_models"][0]["type"] == "auth_error"
    assert got["failed_models"][1]["type"] == "parse_error"
    assert got["failed_models"][1]["diagnostics"]["output_excerpt"] == "not json"
    assert got["usage"]["total_tokens"] == 45
    assert got["usage"]["cost_usd"] == 0.003
    assert backend.calls[0]["effort"] == "low"


@pytest.mark.asyncio
async def test_planner_parse_error_diagnostics_are_redacted():
    class _BadBackend:
        async def complete(self, **kwargs):
            return ModelResponse(
                model="bad-json",
                content="not json api_key=sk-secret1234567890",
                usage={"total_tokens": 3},
                cost_usd=0.001,
            )

    engine = PlannerEngine(backend=_BadBackend())
    report = await engine.plan(PlanRequest(goal="Add diagnostics"), panel=[_panel("bad-json")])
    got = report.to_dict()
    diagnostics = got["failed_models"][0]["diagnostics"]
    assert got["failed_models"][0]["type"] == "parse_error"
    assert "sk-secret" not in diagnostics["output_excerpt"]
    assert "[REDACTED]" in diagnostics["output_excerpt"]
    assert diagnostics["content_chars"] > diagnostics["excerpt_chars"]
    assert diagnostics["redacted_items"] == 1


@pytest.mark.asyncio
async def test_plan_task_routing_metadata(monkeypatch):
    from brainregion import server

    class _FakeEngine:
        async def plan(self, *args, **kwargs):
            return PlanReport(
                plan_id="plan-route",
                summary="ok",
                tasks=[{"id": "T1", "title": "route"}],
                confidence=0.5,
                usage={"total_tokens": 1, "cost_usd": 0.0},
                budget={"jobs_run": 1, "jobs_total": 1},
                guard={},
            )

    monkeypatch.setattr(
        server._defaults_mod,
        "apply",
        lambda **kwargs: {
            "endpoints": {},
            "planner_panel": ["planner-model"],
            "consult_panel": ["consult-model"],
            "panel": ["review-model"],
            "planner_max_input_chars": 1000,
            "planner_max_cost_usd": 0.01,
            "consult_max_cost_usd": 0.02,
            "max_cost_usd": 0.03,
            "effort": None,
        },
    )
    monkeypatch.setattr(server, "_build_planner_engine", lambda dd: _FakeEngine())

    result = await server.plan_task(goal="x")
    assert result["plan_id"] == "plan-route"
    assert result["routing"]["panel_source"] == "planner_panel"
    assert result["routing"]["resolved_panel"] == ["planner-model"]
    assert result["routing"]["model_routes"][0]["route_type"] == "official_litellm"
    assert result["routing"]["route_warnings"] == []
    assert result["routing"]["strategy"] == "first_parseable_plan"

    explicit = await server.plan_task(goal="x", panel=["explicit-model"])
    assert explicit["routing"]["panel_source"] == "explicit"
    assert explicit["routing"]["resolved_panel"] == ["explicit-model"]
