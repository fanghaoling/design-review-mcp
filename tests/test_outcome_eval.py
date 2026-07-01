"""Outcome eval 单测（mock，不联网）。

覆盖：region→consultants 映射 + 回退、advice 脱敏、compute_outcome_summary 数学、
evaluate_gate GO/NO_GO/INCONCLUSIVE、run_outcome_eval 端到端（monkeypatch 引擎+judge backend）。
"""
from __future__ import annotations

import json

import pytest

from brainregion.eval import outcome
from brainregion.eval.outcome import (
    DEFAULT_OUTCOME_VARIANTS,
    GateConfig,
    OutcomeRecord,
    OutcomeVariant,
    _resolve_variant_consultants,
    compute_outcome_summary,
    consultants_for_regions,
    evaluate_gate,
    run_outcome_eval,
)
from brainregion.eval.schema import BlindJudgement, EvalTask
from brainregion.providers.base import ModelResponse


# ---------- region → consultants 映射 ----------

def test_consultants_for_regions_union_dedup():
    # debugging ∪ security ∪ unity_ecs，去重保序
    assert consultants_for_regions(["debugging", "security"]) == ["debugger", "challenge", "critic"]
    # performance 的 critic 与 security 的 critic 去重
    assert consultants_for_regions(["performance", "security"]) == ["performance", "critic", "challenge"]


def test_consultants_for_regions_empty_for_no_specialist():
    # memory/research/review 无 specialist → 空（不掺 fallback）
    assert consultants_for_regions(["memory"]) == []
    assert consultants_for_regions(["research", "review"]) == []
    assert consultants_for_regions([]) == []


def test_resolve_variant_consultants_default_vs_routed_vs_fallback():
    dd = {"consult_consultants": ["debugger", "architect", "critic"]}
    default = OutcomeVariant("default", "default")
    routed = OutcomeVariant("routed", "routed")

    # default → 静态默认面板，source=default
    cons, src = _resolve_variant_consultants(default, ["debugging"], dd)
    assert cons == ["debugger", "architect", "critic"] and src == "default"

    # routed + 有 specialist 映射 → routed
    cons, src = _resolve_variant_consultants(routed, ["debugging", "security"], dd)
    assert cons == ["debugger", "challenge", "critic"] and src == "routed"

    # routed + 空并集（memory）→ 回退默认，source=fallback
    cons, src = _resolve_variant_consultants(routed, ["memory"], dd)
    assert cons == ["debugger", "architect", "critic"] and src == "fallback"

    # dd 无 consult_consultants → 回退内置默认
    cons, src = _resolve_variant_consultants(routed, ["memory"], {})
    assert cons == ["debugger", "architect", "critic"]


def test_resolve_variant_consultants_wake_all_reserved():
    with pytest.raises(NotImplementedError):
        _resolve_variant_consultants(OutcomeVariant("wake_all", "wake_all"), [], {})


def test_resolve_variant_consultants_additive():
    """routed_additive = base ∪ region 专题，base 在前、去重保序（ISS-009/formal NO_GO 修复）。"""
    dd = {"consult_consultants": ["debugger", "critic"]}  # 对齐项目 config 基座
    additive = OutcomeVariant("routed_additive", "routed_additive")

    # performance→[performance,critic]：critic 在 base，去重 → base + performance
    cons, src = _resolve_variant_consultants(additive, ["performance"], dd)
    assert cons == ["debugger", "critic", "performance"] and src == "routed_additive"

    # debugging→[debugger]：debugger 在 base，去重 → 仅 base（专题被 base 吸收）
    cons, src = _resolve_variant_consultants(additive, ["debugging"], dd)
    assert cons == ["debugger", "critic"] and src == "routed_additive"

    # security→[challenge,critic]：critic 去重 → base + challenge
    cons, src = _resolve_variant_consultants(additive, ["security"], dd)
    assert cons == ["debugger", "critic", "challenge"]

    # planning→[architect,test_designer,critic]：critic 去重 → base + architect + test_designer
    cons, src = _resolve_variant_consultants(additive, ["planning"], dd)
    assert cons == ["debugger", "critic", "architect", "test_designer"]

    # 空 region（memory/无 woken）→ 仅 base（不回退标签，仍是 routed_additive）
    cons, src = _resolve_variant_consultants(additive, ["memory"], dd)
    assert cons == ["debugger", "critic"] and src == "routed_additive"
    cons, src = _resolve_variant_consultants(additive, [], dd)
    assert cons == ["debugger", "critic"]


# ---------- advice 脱敏 ----------

def test_desensitize_advice_strips_identity(monkeypatch):
    from brainregion.eval.judge import desensitize_advice

    report = {
        "summary": "isolate the boundary",
        "likely_causes": ["config drift"],
        "next_experiments": ["smallest repro"],
        "solution_options": ["guard + fake backend"],
        "risks": ["secret leak"],
        "recommended_plan": ["start with MVP"],
        "individual": [
            {"id": "c0", "model": "claude-opus-4-8", "consultant": "debugger",
             "summary": "sub", "likely_causes": ["x"]},
        ],
        "routing": {"panel_source": "consult_panel", "consultants_source": "mode"},
        "panel": ["claude-opus-4-8"],
        "usage": {"cost_usd": 0.01},
        "budget": {"estimated_usd": 0.01},
        "guard": {"sent_chars": 100},
    }
    out = desensitize_advice(report)
    assert len(out) >= 1
    flat = json.dumps(out, ensure_ascii=False)
    # 保留建议实质
    assert "config drift" in flat and "smallest repro" in flat
    # 剥离身份线索（防盲破）
    assert "claude-opus-4-8" not in flat
    assert "debugger" not in flat
    assert "panel_source" not in flat and "consultants_source" not in flat


def test_desensitize_advice_falls_back_to_individual_when_no_aggregate():
    from brainregion.eval.judge import desensitize_advice

    report = {
        "summary": "", "likely_causes": [], "next_experiments": [],
        "solution_options": [], "risks": [], "recommended_plan": [],
        "individual": [
            {"model": "m", "consultant": "c", "summary": "only individual", "likely_causes": ["c1"]},
        ],
    }
    out = desensitize_advice(report)
    assert len(out) == 1
    assert out[0]["summary"] == "only individual"
    assert "model" not in out[0] and "consultant" not in out[0]


# ---------- compute_outcome_summary 数学 ----------

def _rec(variant, *, inference, useful_judges, wake_metrics=None, consultants=None, woken=None):
    jdgs = [BlindJudgement(run_id="r", task_id="t", judge_id="j", judge_model="m",
                           rubric_hash="h", variant=variant, scores=s) for s in useful_judges]
    rec = OutcomeRecord(
        run_id="r", task_id="t", variant=variant,
        report_summary={"advice_count": 1, "failed_count": 0},
        wake={
            "strategy": variant, "mapping_source": "routed" if variant == "routed" else "default",
            "consultants": consultants or [], "woken": woken or [],
            "wake_metrics": wake_metrics or {}, "shadow_promoted": 0,
        },
        cost={"inference_usd": inference, "estimated_usd": inference, "total_tokens": 10},
        latency_ms=100.0,
    )
    return rec, jdgs


def test_compute_outcome_summary_cost_per_useful_and_missed_wake():
    # 2 任务，每变体：default useful=2 cost=0.02；routed useful=4 cost=0.02
    records, judgements = [], []
    for _ in range(2):
        r1, j1 = _rec("default", inference=0.02, useful_judges=[{"useful": 2, "overall": 4, "missed_critical": 0}])
        r2, j2 = _rec("routed", inference=0.02, useful_judges=[{"useful": 4, "overall": 5, "missed_critical": 0}],
                      wake_metrics={"missed": [], "hit": ["debugging"], "false_wake": []},
                      consultants=["debugger"], woken=["debugging"])
        records += [r1, r2]
        judgements += j1 + j2

    s = compute_outcome_summary(records, judgements, DEFAULT_OUTCOME_VARIANTS)
    pv = s["per_variant"]
    # cost_per_useful = inference/useful：default 0.02/2=0.01；routed 0.02/4=0.005
    assert pv["default"]["cost_per_useful_advice"] == 0.01
    assert pv["routed"]["cost_per_useful_advice"] == 0.005
    assert pv["routed"]["useful_advice_rate"] >= pv["default"]["useful_advice_rate"]
    # missed_wake_rate：routed 的 gold={debugging}（hit），missed=0 → 0.0
    assert pv["routed"]["missed_wake_rate"] == 0.0
    assert pv["routed"]["missed_critical_total"] == 0


def test_compute_outcome_summary_useful_zero_yields_none():
    r1, j1 = _rec("default", inference=0.01, useful_judges=[{"useful": 0}])
    r2, j2 = _rec("routed", inference=0.01, useful_judges=[{"useful": 0}])
    s = compute_outcome_summary([r1, r2], j1 + j2, DEFAULT_OUTCOME_VARIANTS)
    assert s["per_variant"]["default"]["cost_per_useful_advice"] is None


# ---------- evaluate_gate ----------

def _make_run(n, d_cost, d_useful, r_cost, r_useful, total_advice=5, d_missed=0, r_missed=0, judge_id="j"):
    """构造 n 个 task 的 default+routed records + 1-judge judgements（每 task 每 variant 一条）。"""
    records, judgements = [], []
    for i in range(n):
        tid = f"t{i}"
        for variant, cost, useful, missed in (
            ("default", d_cost, d_useful, d_missed), ("routed", r_cost, r_useful, r_missed)):
            records.append(OutcomeRecord(
                run_id="r", task_id=tid, variant=variant,
                report_summary={"advice_count": total_advice, "failed_count": 0},
                wake={"strategy": variant, "mapping_source": "routed" if variant == "routed" else "default",
                      "consultants": [], "woken": [],
                      "wake_metrics": {"missed": [], "hit": [variant], "false_wake": []},
                      "shadow_promoted": 0},
                cost={"inference_usd": cost, "estimated_usd": cost, "total_tokens": 10},
                latency_ms=100.0))
            judgements.append(BlindJudgement(
                run_id="r", task_id=tid, judge_id=judge_id, judge_model="m",
                rubric_hash="h", variant=variant,
                scores={"useful": useful, "missed_critical": missed, "overall": 4}))
    return records, judgements


def test_gate_go():
    # n=35（>formal_min_n=30，非 pilot）；routed 便宜一半、useful 非劣、missed 不增 → GO
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=2)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-go", calibration_ok=True)
    assert g["decision"] == "GO", g["reasons"]


def test_gate_useful_absolute_delta():
    # routed useful=4 > default useful=2，n=10 → 绝对 delta=(4-2)*10=20（rate 可能因 total 稀释，绝对值才是直读）
    recs, jdgs = _make_run(10, d_cost=0.01, d_useful=2, r_cost=0.01, r_useful=4)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-abs", calibration_ok=True)
    assert g["diagnostics"]["useful_absolute_delta"] == 20


def test_gate_pilot_prefix():
    # n=20（<formal_min_n=30）+ GO 信号 → pilot_GO（不宣称可信闸门）
    recs, jdgs = _make_run(20, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=2)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-pilot", calibration_ok=True)
    assert g["decision"] == "pilot_GO", g["reasons"]
    assert g["diagnostics"]["pilot"] is True


def test_gate_no_go_cost():
    # routed 更贵 → cost_ratio CI low>0.85 → NO_GO
    recs, jdgs = _make_run(35, d_cost=0.005, d_useful=2, r_cost=0.01, r_useful=2)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-nogo", calibration_ok=True)
    assert g["decision"] == "NO_GO"
    assert any("cost_ratio" in r for r in g["reasons"])


def test_gate_no_go_useful():
    # routed useful 更低 → useful_delta CI high<0 → NO_GO（OR 语义，单指标确定失败）
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=4, r_cost=0.005, r_useful=1)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-nogo-u", calibration_ok=True)
    assert g["decision"] == "NO_GO"
    assert any("useful_delta" in r for r in g["reasons"])


def test_gate_no_go_missed_critical():
    # routed 多漏关键 → missed_critical_delta CI low>0 → NO_GO
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=2, d_missed=0, r_missed=2)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-nogo-mc", calibration_ok=True)
    assert g["decision"] == "NO_GO"
    assert any("missed_critical" in r for r in g["reasons"])


def test_gate_inconclusive_small_n():
    recs, jdgs = _make_run(2, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=2)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-small", calibration_ok=True)
    assert g["decision"] == "INCONCLUSIVE"


def test_gate_inconclusive_bootstrap_none():
    # routed useful=0 everywhere → Σuseful=0 → cost_ratio None → INCONCLUSIVE
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=0)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-none", calibration_ok=True)
    assert g["decision"] == "INCONCLUSIVE"


def test_gate_calibration_required():
    # 校准 artifact 缺失/未达标 → CALIBRATION_REQUIRED（前置，覆盖一切）
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=2)
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="run-nc", calibration_ok=False)
    assert g["decision"] == "CALIBRATION_REQUIRED"


def test_gate_respects_custom_config():
    # 默认 cost_ratio=0.85：ratio=0.5 → GO；更严 cost_ratio=0.3：CI low=0.5>0.3 → NO_GO
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=2, r_cost=0.005, r_useful=2)
    assert evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="r1",
                         calibration_ok=True)["decision"] == "GO"
    g = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="r1",
                      cfg=GateConfig(cost_ratio=0.3), calibration_ok=True)
    assert g["decision"] == "NO_GO"


def test_gate_cost_primary_false_for_coverage_treatment():
    # 覆盖型 treatment（memory/additive）：两臂 cost 持平 → cost_ratio≈1.0。
    # 默认 cost_primary=True：CI low=1.0>0.85 → NO_GO（cost 错配，additive/memory 两例证明）。
    # cost_primary=False：cost 不进判定 → useful 非劣 + missed 不增 → GO。
    recs, jdgs = _make_run(35, d_cost=0.01, d_useful=2, r_cost=0.01, r_useful=2)
    g_cost = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="r-cp1", calibration_ok=True)
    assert g_cost["decision"] == "NO_GO"
    assert any("cost_ratio" in r for r in g_cost["reasons"])
    g_cov = evaluate_gate(recs, jdgs, DEFAULT_OUTCOME_VARIANTS, run_id="r-cp0",
                          cfg=GateConfig(cost_primary=False), calibration_ok=True)
    assert g_cov["decision"] == "GO", g_cov["reasons"]
    assert not any("cost_ratio" in r for r in g_cov["reasons"])
    assert any("覆盖型" in r for r in g_cov["reasons"])


# ---------- run_outcome_eval 端到端（mock）----------


class _FakeJudgeBackend:
    """judge backend：返回固定 X/Y JSON 评分。"""

    async def complete(self, *, model, system, user, temperature=0.1, max_tokens=2048, effort=None, endpoint_id=None):
        content = json.dumps({
            "X": {"useful": 3, "correct": 3, "harmful": 0, "missed_critical": 0, "overall": 4},
            "Y": {"useful": 2, "correct": 2, "harmful": 0, "missed_critical": 1, "overall": 3},
        })
        return ModelResponse(model=model, content=content, cost_usd=0.001)


class _FakeConsultEngine:
    """consult 引擎：忽略 panel/consultants，返回固定 ConsultReport。"""

    def __init__(self):
        self.backend = _FakeJudgeBackend()

    async def consult(self, request, *, panel, consultants, max_cost_usd=None, effort=None, consultation_id=None, context_blocks=None):
        from brainregion.core.consult.report import ConsultAdvice, ConsultReport
        return ConsultReport(
            consultation_id="c-test",
            summary="isolate the failing boundary",
            likely_causes=["race on DB reconnect"],
            next_experiments=["smallest reproduction under load"],
            solution_options=["add guard + fake-backend test"],
            risks=["external calls may leak secrets"],
            recommended_plan=["start with the smallest repro"],
            individual=[ConsultAdvice(id="c0", model="fake-model",
                                      consultant=(consultants[0] if consultants else "debugger"),
                                      summary="isolate the boundary")],
            usage={"total_tokens": 42, "cost_usd": 0.002},
            budget={"estimated_usd": 0.002, "jobs_run": 1, "jobs_total": 1, "exhausted": False},
        )


@pytest.mark.asyncio
async def test_run_outcome_eval_mock_end_to_end(monkeypatch, tmp_path):
    # 隔离 eval DB 到 tmp（避免污染本地 .brain-region/eval/eval.db）
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    # mock consult 引擎（含 judge backend）
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: _FakeConsultEngine())

    tasks = [
        EvalTask(id="oc-mock-1", task_type="consult",
                 input={"problem": "Flaky test: intermittent race condition in CI, never local. Bug around DB reconnect.",
                        "why_stuck": "can't reproduce locally", "question": "likely races?"},
                 gold_regions=["debugging"]),
        EvalTask(id="oc-mock-2", task_type="consult",
                 input={"problem": "SQL injection and 越权 risk in a REST endpoint before launch.",
                        "question": "where are the real risks?"},
                 gold_regions=["security"]),
    ]
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]

    records, judgements, entry, gate = await run_outcome_eval(
        tasks, DEFAULT_OUTCOME_VARIANTS, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-mock",
        max_cost_usd=0.5, require_calibration=False,
    )

    # 每 task × 2 variant = 4 records；每 task × 1 judge × 2 variant = 4 judgements
    assert len(records) == 4
    assert len(judgements) == 4
    # routed 的 consultants 来自 wake_gate 派生（debugging→[debugger], security→[challenge,critic]）
    routed_recs = [r for r in records if r.variant == "routed"]
    consultants_by_task = {r.task_id: set(r.wake["consultants"]) for r in routed_recs}
    assert "debugger" in consultants_by_task["oc-mock-1"]
    assert "challenge" in consultants_by_task["oc-mock-2"]
    # default 恒为静态默认面板
    assert all(r.wake["mapping_source"] == "default" for r in records if r.variant == "default")
    # wake_gate 只调一次/任务：两变体共用同一 woken（routed 与 default 的 woken 相同）
    by_task = {}
    for r in records:
        by_task.setdefault(r.task_id, {})[r.variant] = set(r.wake["woken"])
    for tid in by_task:
        assert by_task[tid]["default"] == by_task[tid]["routed"]
    # gate 结构完整（CI-aware：n=2 → INCONCLUSIVE；有 diagnostics/hard_gates/reasons）
    assert gate["decision"] in {"GO", "NO_GO", "INCONCLUSIVE", "pilot_GO", "pilot_NO_GO", "CALIBRATION_REQUIRED"}
    assert "diagnostics" in gate and "hard_gates" in gate and gate["reasons"]
    # entry 入 ledger
    assert entry.run_id == "run-mock" and entry.n_tasks == 2
    assert entry.knowledge_hash == "" and entry.reviewer_hash == ""  # consult 无知识库/reviewer


@pytest.mark.asyncio
async def test_run_outcome_eval_judge_shuffle_is_deterministic(monkeypatch, tmp_path):
    """同 task_id 两次跑，盲打乱映射应一致（_seed(task_id) 确定性）。"""
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    calls: list[str] = []

    class _CapturingBackend(_FakeJudgeBackend):
        async def complete(self, **kw):
            calls.append(kw["user"])
            return await super().complete(**kw)

    class _Engine:
        def __init__(self):
            self.backend = _CapturingBackend()

        async def consult(self, request, *, panel, consultants, **kw):
            from brainregion.core.consult.report import ConsultAdvice, ConsultReport
            return ConsultReport(summary="s", individual=[ConsultAdvice(id="c0", model="m", consultant="x")],
                                 usage={"cost_usd": 0.001})

    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: _Engine())
    tasks = [EvalTask(id="oc-det", task_type="consult", input={"problem": "flaky race condition bug"},
                      gold_regions=["debugging"])]
    je = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]
    await run_outcome_eval(tasks, DEFAULT_OUTCOME_VARIANTS, je, {}, "", "h", "run-1", max_cost_usd=0.5,
                           require_calibration=False)
    await run_outcome_eval(tasks, DEFAULT_OUTCOME_VARIANTS, je, {}, "", "h", "run-2", max_cost_usd=0.5,
                           require_calibration=False)
    # 两次的 judge user prompt（含打乱后的标签顺序）应完全一致
    assert calls[0] == calls[1]
