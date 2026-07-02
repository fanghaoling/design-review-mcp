"""Phase2A.5 程序 diagnostic 测试：check_advice_signal / detect_memory_cite / compute_memory_diagnostics + 4 臂接线。"""
from __future__ import annotations

import json

import pytest

from brainregion.eval.check import check_advice_signal, detect_memory_cite
from brainregion.eval.outcome import (
    OutcomeVariant,
    compute_memory_diagnostics,
    run_outcome_eval,
)
from brainregion.eval.schema import BlindJudgement, EvalTask
from brainregion.providers.base import ModelResponse


# ---------- check_advice_signal / detect_memory_cite ----------

def _report(summary="", solution_options=None, risks=None):
    """最小 ConsultReport dict（desensitize_advice 读 top-level _ADVICE_FIELDS）。"""
    rep = {"summary": summary}
    if solution_options is not None:
        rep["solution_options"] = solution_options
    if risks is not None:
        rep["risks"] = risks
    return rep


def test_check_violation_and_applies():
    gold = {"must_not_contain_any": ["scale=0", "丝杆"], "must_contain_any": ["disablerendering"]}
    # 建议触 must_not（Scale=0）→ violation=1；未含 must_any → applies=0
    r = check_advice_signal(_report(summary="隐藏角色用 Scale=0", solution_options=["Scale=0"]), gold)
    assert r["constraint_violation"] == 1 and r["applies_context"] == 0
    # 建议含 must_any（DisableRendering）、不触 must_not → violation=0、applies=1
    r2 = check_advice_signal(_report(summary="用 DisableRendering 隐藏", solution_options=["DisableRendering"]), gold)
    assert r2["constraint_violation"] == 0 and r2["applies_context"] == 1
    # 大小写不敏感
    assert check_advice_signal(_report(summary="use SCALE=0"), gold)["constraint_violation"] == 1


def test_check_empty_gold_check_returns_zeros():
    r = check_advice_signal(_report(summary="anything"), {})
    assert r["constraint_violation"] == 0 and r["applies_context"] == 0


def test_detect_memory_cite():
    # 含两个引用短语 → cite_count≥2（debug，非证据）
    rep = _report(summary="根据项目约定，之前试过 Scale 失败")
    assert detect_memory_cite(rep)["cite_count"] >= 2
    assert detect_memory_cite(_report(summary="用 DisableRendering"))["cite_count"] == 0


# ---------- compute_memory_diagnostics ----------

def _pj(task_id, variant, violation):
    return BlindJudgement(run_id="r", task_id=task_id, judge_id="programmatic", judge_model="rule",
                          rubric_hash="", variant=variant,
                          scores={"constraint_violation": violation, "applies_context": 1 - violation, "cite_count": 0})


def test_compute_memory_diagnostics_relevant_beats_irrelevant():
    # 5 task：RELEVANT(routed_memory) 全守约 violation=0；IRRELEVANT/OFF/STALE 全违反 violation=1。
    variants = [
        OutcomeVariant("routed", "routed"),
        OutcomeVariant("routed_memory", "routed", inject_memory=True),
        OutcomeVariant("routed_memory_irrelevant", "routed", inject_memory_irrelevant=True),
        OutcomeVariant("routed_memory_stale", "routed", inject_memory_stale=True),
    ]
    jdgs = []
    for i in range(5):
        jdgs.append(_pj(f"t{i}", "routed_memory", 0))                 # RELEVANT
        jdgs.append(_pj(f"t{i}", "routed_memory_irrelevant", 1))      # IRRELEVANT
        jdgs.append(_pj(f"t{i}", "routed", 1))                        # OFF
        jdgs.append(_pj(f"t{i}", "routed_memory_stale", 1))           # STALE
    diag = compute_memory_diagnostics(jdgs, variants)
    # 主比较：RELEVANT violation 显著 < IRRELEVANT → delta 整段 <0
    pv = diag["pairwise_delta_ci"]["relevant_vs_irrelevant"]
    assert pv["point"] == -1.0 and pv["high"] <= 0
    # STALE vs OFF 整段 ~0（STALE 不比 OFF 好，证伪）
    assert diag["pairwise_delta_ci"]["stale_vs_off"]["point"] == 0.0
    # per_arm 结构完整
    assert diag["per_arm"]["routed_memory"]["violation_rate"] == 0.0
    assert diag["per_arm"]["routed_memory_irrelevant"]["violation_rate"] == 1.0


def test_compute_memory_diagnostics_empty_when_no_programmatic():
    # 无 programmatic judgement（无 gold_check task）→ 空
    jdgs = [BlindJudgement(run_id="r", task_id="t", judge_id="llm", judge_model="gpt",
                           rubric_hash="h", variant="routed", scores={"useful": 3})]
    assert compute_memory_diagnostics(jdgs, [OutcomeVariant("routed", "routed")]) == {}


# ---------- 4 臂 run_outcome_eval 端到端（mock）----------

class _FakeJudgeBackend:
    async def complete(self, *, model, system, user, temperature=0.1, max_tokens=2048, effort=None, endpoint_id=None):
        return ModelResponse(model=model, content=json.dumps({
            "X": {"useful": 3, "overall": 4, "missed_critical": 0},
            "Y": {"useful": 2, "overall": 3, "missed_critical": 1},
        }), cost_usd=0.001)


class _FixedEngine:
    """返回固定 advice（含 must_not 关键词 → 触 violation），记录 context_blocks。"""

    def __init__(self) -> None:
        self.calls: list[list] = []
        self.backend = _FakeJudgeBackend()

    async def consult(self, request, *, panel, consultants, max_cost_usd=None, effort=None,
                      consultation_id=None, context_blocks=None):
        from brainregion.core.consult.report import ConsultAdvice, ConsultReport
        self.calls.append(list(context_blocks or []))
        return ConsultReport(
            consultation_id="c", summary="建议用 Scale=0 隐藏",  # 触 must_not["scale=0"]
            individual=[ConsultAdvice(id="c0", model="m", consultant="debugger", summary="Scale=0")],
            usage={"cost_usd": 0.001},
        )


@pytest.mark.asyncio
async def test_run_outcome_eval_4arm_synthesizes_programmatic_and_diagnostics(monkeypatch, tmp_path):
    from brainregion.eval import outcome

    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    engine = _FixedEngine()
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: engine)
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]

    task = EvalTask(
        id="t1", task_type="consult",
        input={"problem": "hide a skinned character"},
        gold_regions=["unity_ecs"],
        seed_memory=[{"summary": "用 DisableRendering", "triggers": ["hide", "character"], "region": "unity_ecs"}],
        seed_memory_irrelevant=[{"summary": "贴片机用 5 TMC2209", "triggers": ["hide"], "region": "unity_ecs"}],
        seed_memory_stale=[{"summary": "项目用 Scale 隐藏", "triggers": ["hide"], "region": "unity_ecs"}],
        gold_check={"must_not_contain_any": ["scale=0"], "must_contain_any": ["disablerendering"]},
        exp_type="Constraint",
    )
    variants = [
        OutcomeVariant("routed", "routed"),
        OutcomeVariant("routed_memory", "routed", inject_memory=True),
        OutcomeVariant("routed_memory_irrelevant", "routed", inject_memory_irrelevant=True),
        OutcomeVariant("routed_memory_stale", "routed", inject_memory_stale=True),
    ]
    _, judgements, entry, _ = await run_outcome_eval(
        [task], variants, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-4arm",
        max_cost_usd=0.5, require_calibration=False,
    )
    # 4 variant 各一次 consult；3 个 memory 臂注入了 blocks（OFF 不注入）
    assert len(engine.calls) == 4
    assert engine.calls[0] == []                      # OFF
    assert any(engine.calls[i] for i in (1, 2, 3))    # memory 臂注入
    # programmatic judgement 合成（4 variant × 1 task = 4 条）
    prog = [j for j in judgements if j.judge_id == "programmatic"]
    assert len(prog) == 4
    # 固定 advice 含 "scale=0" → 全 violation=1
    assert all(j.scores["constraint_violation"] == 1 for j in prog)
    # summary 含 memory_diagnostics + memory_instrumentation
    assert "memory_diagnostics" in entry.summary and entry.summary["memory_diagnostics"]
    assert "memory_instrumentation" in entry.summary
    assert len(entry.summary["memory_instrumentation"]) == 3  # 3 个 memory 臂各 1 条
