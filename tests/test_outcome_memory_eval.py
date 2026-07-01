"""M3：outcome eval memory A/B 单变量接线（context_blocks plumbing + gate control/treatment）。"""
from __future__ import annotations

import json

import pytest

from brainregion.eval import outcome
from brainregion.eval.cli import load_tasks
from brainregion.eval.outcome import DEFAULT_OUTCOME_VARIANTS, OutcomeVariant, run_outcome_eval
from brainregion.eval.schema import EvalTask
from brainregion.providers.base import ModelResponse


class _FakeJudgeBackend:
    async def complete(self, *, model, system, user, temperature=0.1, max_tokens=2048, effort=None, endpoint_id=None):
        content = json.dumps({
            "X": {"useful": 3, "correct": 3, "harmful": 0, "missed_critical": 0, "overall": 4},
            "Y": {"useful": 2, "correct": 2, "harmful": 0, "missed_critical": 1, "overall": 3},
        })
        return ModelResponse(model=model, content=content, cost_usd=0.001)


class _CapturingEngine:
    """记录每次 consult 收到的 context_blocks（按调用序），供按 variant 断言。"""

    def __init__(self) -> None:
        self.calls: list[list] = []  # 每次 consult 的 context_blocks
        self.backend = _FakeJudgeBackend()

    async def consult(self, request, *, panel, consultants, max_cost_usd=None, effort=None,
                      consultation_id=None, context_blocks=None):
        from brainregion.core.consult.report import ConsultAdvice, ConsultReport

        self.calls.append(list(context_blocks or []))
        return ConsultReport(
            consultation_id="c",
            summary="s",
            individual=[ConsultAdvice(id="c0", model="m", consultant="debugger", summary="s")],
            usage={"cost_usd": 0.001},
        )


def _seed_task() -> EvalTask:
    return EvalTask(
        id="oc-mem-1", task_type="consult",
        input={"problem": "intermittent deadlock between two locks, stuck threads under load",
               "why_stuck": "can't reproduce in tests", "question": "lock ordering fix?"},
        gold_regions=["debugging"],
        seed_memory=[{
            "summary": "两把锁死锁：固定全局锁序",
            "details": "lockA→lockB 与 lockB→lockA 并存；固定全局锁序消除死锁。",
            "triggers": ["deadlock", "lock", "stuck"],
            "region": "debugging",
        }],
    )


MEMORY_VARIANTS = [
    OutcomeVariant("routed", "routed"),
    OutcomeVariant("routed_memory", "routed", inject_memory=True),
]


@pytest.mark.asyncio
async def test_memory_inject_plumbs_context_blocks_only_to_treatment(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    engine = _CapturingEngine()
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: engine)
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]

    await run_outcome_eval(
        [_seed_task()], MEMORY_VARIANTS, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-mem",
        max_cost_usd=0.5, require_calibration=False,
    )

    # 1 task × 2 variant = 2 consult 调用，顺序 = variants 顺序（routed 先, routed_memory 后）
    assert len(engine.calls) == 2
    assert engine.calls[0] == []                      # control(routed) 不注入
    treat = engine.calls[1]                           # treatment(routed_memory)
    assert len(treat) == 1                            # seed 命中 → 召回 1 块
    assert "锁序" in (treat[0].content + treat[0].title)
    assert treat[0].framing == "data" and treat[0].source == "memory"


@pytest.mark.asyncio
async def test_gate_treatment_is_routed_memory_when_memory_present(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    captured: dict = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return {"decision": "pilot_INCONCLUSIVE", "diagnostics": {}, "hard_gates": {}, "reasons": ["stub"]}

    monkeypatch.setattr(outcome, "evaluate_gate", _spy)
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: _CapturingEngine())
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]

    await run_outcome_eval(
        [_seed_task()], MEMORY_VARIANTS, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-mem",
        max_cost_usd=0.5, require_calibration=False,
    )
    assert captured.get("control") == "routed"
    assert captured.get("treatment") == "routed_memory"  # 修 gate 静默：memory arm 到达判定
    assert captured.get("cfg").cost_primary is False     # 覆盖型：cost 不当 primary（同 additive 教训）


@pytest.mark.asyncio
async def test_gate_defaults_when_no_memory(monkeypatch, tmp_path):
    """非 memory run（DEFAULT_OUTCOME_VARIANTS）→ evaluate_gate 不传 control/treatment（用默认 default/routed）。"""
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    captured: dict = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return {"decision": "pilot_INCONCLUSIVE", "diagnostics": {}, "hard_gates": {}, "reasons": ["stub"]}

    monkeypatch.setattr(outcome, "evaluate_gate", _spy)
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: _CapturingEngine())
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]

    await run_outcome_eval(
        [_seed_task()], DEFAULT_OUTCOME_VARIANTS, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-nomem",
        max_cost_usd=0.5, require_calibration=False,
    )
    assert "control" not in captured and "treatment" not in captured  # 走 evaluate_gate 默认值


@pytest.mark.asyncio
async def test_memory_absent_seed_injects_nothing(monkeypatch, tmp_path):
    """task 无 seed_memory → routed_memory 也不注入（纯负对照：memory 缺席 → 无效应）。"""
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    engine = _CapturingEngine()
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: engine)
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]
    task = EvalTask(id="t", task_type="consult",
                    input={"problem": "unrelated research question"}, gold_regions=["research"])  # 无 seed

    await run_outcome_eval(
        [task], MEMORY_VARIANTS, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-neg",
        max_cost_usd=0.5, require_calibration=False,
    )
    assert engine.calls == [[], []]  # 两 variant 都无注入


def test_seed_memory_loaded_from_fixtures(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        "- id: oc-x\n  task_type: consult\n  input:\n    problem: p\n"
        "  gold_regions: [debugging]\n"
        "  seed_memory:\n"
        "    - summary: s1\n      details: d1\n      triggers: [a, b]\n      region: debugging\n",
        encoding="utf-8",
    )
    tasks = load_tasks(str(tmp_path))
    assert len(tasks) == 1
    assert tasks[0].seed_memory and tasks[0].seed_memory[0]["summary"] == "s1"
    assert tasks[0].seed_memory[0]["triggers"] == ["a", "b"]
