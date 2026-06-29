"""v5.5 评测 harness 测试（mock engine+backend，不联网）。

覆盖：schema 序列化、GarbageKnowledgeProvider、metadata hash、judge 脱敏+打乱+解析、
store SQLite+JSONL、compute_summary（useful=0 不除零）、sanity（负对照 + 结构性 error）、
1 个 mock e2e 跑通三变体。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from brainregion.eval import judge, knowledge, metadata, runner, store
from brainregion.eval.schema import (
    BlindJudgement, EvalCaseRecord, EvalLedgerEntry, EvalTask, VariantSpec,
)
from brainregion.providers.base import ModelResponse


# ---------- schema ----------

def test_schema_dataclasses_roundtrip():
    t = EvalTask(id="t1", input={"content": "x"})
    assert t.task_type == "review" and t.frozen is True
    rec = EvalCaseRecord(run_id="r", task_id="t1", variant="retrieve_on",
                         cost={"inference_usd": 0.1, "estimated_usd": 0.08})
    assert rec.cost["inference_usd"] == 0.1
    j = BlindJudgement(run_id="r", task_id="t1", judge_id="j", judge_model="m",
                       rubric_hash="h", variant="retrieve_on", scores={"useful": 3})
    assert j.scores["useful"] == 3 and j.blind is True


# ---------- GarbageKnowledgeProvider ----------

class _Case:
    def __init__(self, cid, title):
        self.id, self.title = cid, title


class _FakeProvider:
    def __init__(self, cases):
        self._cases = cases

    def list_cases(self):
        return self._cases


def test_garbage_provider_ignores_query_and_is_deterministic():
    cases = [_Case(f"C-{i}", f"t{i}") for i in range(20)]
    g = knowledge.GarbageKnowledgeProvider(_FakeProvider(cases))
    a = g.retrieve("anything", {}, 5)
    b = g.retrieve("anything", {}, 5)  # 同 text → 同 seed → 同采样
    assert len(a) == 5
    assert [c.id for c in a] == [c.id for c in b]  # 确定性
    different = g.retrieve("different text", {}, 5)
    # 不同 text 大概率不同堆（不强制，但 seed 不同）
    assert len(different) == 5


def test_garbage_provider_empty():
    assert knowledge.GarbageKnowledgeProvider(_FakeProvider([])).retrieve("x", {}, 5) == []


# ---------- metadata ----------

def test_metadata_hashes_deterministic():
    p = _FakeProvider([_Case("C-1", "t1"), _Case("C-2", "t2")])
    assert metadata.knowledge_hash(p) == metadata.knowledge_hash(p)
    assert metadata.defaults_hash({"a": 1, "b": 2}) == metadata.defaults_hash({"b": 2, "a": 1})
    assert metadata.rubric_hash("abc") == metadata.rubric_hash("abc")
    assert metadata.rubric_hash("abc") != metadata.rubric_hash("abd")


def test_reviewer_hash_changes_with_dims(tmp_path):
    class _A:
        def reviewers_dir(self):
            return tmp_path
    a = _A()
    h1 = metadata.reviewer_hash(a, ["safety"])
    h2 = metadata.reviewer_hash(a, ["performance"])
    assert h1 != h2  # dimensions 变 → hash 变


# ---------- judge desensitize ----------

def test_desensitize_strips_case_ref_and_prefix():
    report = {
        "consensus": [{"canonical_title": "[GP-ENEMY-DORMANT] 敌人休眠", "severity": "high",
                       "evidence_quote": "q", "suggestion": "s", "case_ref": "GP-ENEMY-DORMANT"}],
        "majority": [],
        "individual": {"m1": [{"title": "plain title", "severity": "medium",
                               "evidence_quote": "q", "suggestion": "s"}]},
        "knowledge_hit": ["GP-ENEMY-DORMANT"],
        "retrieved_cases": ["GP-ENEMY-DORMANT"],
    }
    out = judge.desensitize(report)
    titles = [f["title"] for f in out]
    assert "敌人休眠" in titles  # [CASE-ID] 前缀被剥
    assert all("GP-ENEMY" not in t for t in titles)
    assert all("case_ref" not in f for f in out)  # 无 case_ref 字段
    assert len(out) == 2


# ---------- judge_task shuffle + parse + unshuffle ----------

class _FakeBackend:
    def __init__(self, content):
        self._content = content

    async def complete(self, **kw):
        return ModelResponse(model="fake-judge", content=self._content, cost_usd=0.012)


def test_judge_task_shuffles_and_unshuffles():
    task_id = "t-shuffle"
    # 构造两份输出
    rep_on = {"consensus": [{"canonical_title": "good", "severity": "high",
                             "evidence_quote": "q", "suggestion": "s"}], "majority": [],
              "individual": {}}
    rep_off = {"consensus": [], "majority": [], "individual": {}}
    outputs = {"retrieve_on": json.dumps(rep_on), "retrieve_off": json.dumps(rep_off)}

    # 复刻 judge 内部 shuffle 得到 label→variant 映射，据此构造 judge 返回。
    # 关键：order 必须从 outputs.keys() 取（与 judge_task 的 list(variant_outputs.keys()) 一致）
    import hashlib
    import random
    seed = int(hashlib.sha256(task_id.encode()).hexdigest()[:8], 16)
    order = list(outputs.keys())
    random.Random(seed).shuffle(order)
    labels = ["X", "Y"]
    label_to_variant = dict(zip(labels, order))
    # on 给 useful=5，off 给 useful=1
    resp = {lab: {"useful": 5 if label_to_variant[lab] == "retrieve_on" else 1, "overall": 4}
            for lab in labels}

    je = {"label": "judge-1", "model": "fake-judge", "endpoint_id": None}
    results = asyncio.run(judge.judge_task(
        _FakeBackend(json.dumps(resp)), je, "rubric", "rhash", "run-1", task_id, outputs))

    by_variant = {j.variant: j for j in results}
    assert by_variant["retrieve_on"].scores["useful"] == 5
    assert by_variant["retrieve_off"].scores["useful"] == 1
    assert all(j.blind for j in results)
    assert all(j.judge_cost_usd == 0.012 for j in results)


def test_judge_task_parse_failure_recorded():
    je = {"label": "j", "model": "m", "endpoint_id": None}
    results = asyncio.run(judge.judge_task(  # judge 返回非 JSON
        _FakeBackend("not json"), je, "r", "h", "run", "t", {"retrieve_off": "{}"}
    ))
    assert len(results) == 1
    assert "parse" in results[0].reason.lower() or results[0].scores == {}


# ---------- store（isolated SQLite）----------

@pytest.fixture
def iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def test_store_roundtrip_and_export(iso_db, tmp_path):
    entry = EvalLedgerEntry(run_id="run-x", date="2026-06-29", git_sha="abc",
                            variants=["retrieve_off"], judge_models=["m"], rubric_hash="rh",
                            knowledge_hash="kh", reviewer_hash="vh", defaults_hash="dh",
                            n_tasks=2, summary={"per_variant": {}})
    store.record_run(entry)
    rec = EvalCaseRecord(run_id="run-x", task_id="t1", variant="retrieve_off",
                         retrieved_case_ids=[], cost={"inference_usd": 0.05}, latency_ms=120.0,
                         outputs_json="{}")
    store.record_case(rec)
    j = BlindJudgement(run_id="run-x", task_id="t1", judge_id="j", judge_model="m",
                       rubric_hash="rh", variant="retrieve_off", scores={"useful": 2})
    store.record_judgement(j)

    out = tmp_path / "export.jsonl"
    n = store.export_jsonl("run-x", out)
    assert n == 3  # 1 run + 1 case + 1 judgement
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    kinds = {json.loads(line)["kind"] for line in lines}
    assert kinds == {"run", "case", "judgement"}


# ---------- compute_summary / sanity ----------

def _rec(variant, useful_cost=0.1, lat=100.0, cases=None, err=""):
    return EvalCaseRecord(run_id="r", task_id="t", variant=variant,
                          retrieved_case_ids=cases or [], cost={"inference_usd": useful_cost},
                          latency_ms=lat, error=err)


def _jdg(variant, useful, overall):
    return BlindJudgement(run_id="r", task_id="t", judge_id="j", judge_model="m",
                          rubric_hash="h", variant=variant, scores={"useful": useful, "overall": overall})


def test_compute_summary_useful_zero_no_divzero():
    variants = [VariantSpec("retrieve_off", 0), VariantSpec("retrieve_on", 5)]
    recs = [_rec("retrieve_off"), _rec("retrieve_on")]
    jdgs = [_jdg("retrieve_off", 0, 2), _jdg("retrieve_on", 0, 2)]  # useful 全 0
    s = runner.compute_summary(recs, jdgs, variants)["per_variant"]
    assert s["retrieve_off"]["cost_per_useful_advice"] is None  # 不除零
    assert s["retrieve_on"]["useful_advice_rate"] == 0.0


def test_sanity_negative_control_and_structural_error():
    variants = [VariantSpec("retrieve_off", 0), VariantSpec("retrieve_on", 5),
                VariantSpec("retrieve_garbage", 5, garbage=True)]
    # off 却检索到 case → 结构性 error
    recs = [_rec("retrieve_off", cases=["LEAK"]), _rec("retrieve_on", cases=["OK"]),
            _rec("retrieve_garbage", cases=["R1"])]
    # 负对照违反：garbage overall 高于 on
    jdgs = [_jdg("retrieve_off", 1, 2), _jdg("retrieve_on", 3, 3), _jdg("retrieve_garbage", 5, 5)]
    sanity = runner.sanity_check(recs, jdgs, variants)
    assert any("retrieve_off 却检索到 case" in e for e in sanity["errors"])
    assert any("负对照排序异常" in w for w in sanity["warnings"])


def test_sanity_clean_when_ordered():
    variants = [VariantSpec("retrieve_off", 0), VariantSpec("retrieve_on", 5),
                VariantSpec("retrieve_garbage", 5, garbage=True)]
    recs = [_rec("retrieve_off", cases=[]), _rec("retrieve_on", cases=["OK"]),
            _rec("retrieve_garbage", cases=["R1"])]
    jdgs = [_jdg("retrieve_off", 1, 2), _jdg("retrieve_on", 4, 4), _jdg("retrieve_garbage", 0, 1)]
    sanity = runner.sanity_check(recs, jdgs, variants)
    assert sanity["errors"] == []
    assert not any("负对照" in w for w in sanity["warnings"])


# ---------- parse_variants (cli) ----------

def test_parse_variants():
    from brainregion.eval import cli as eval_cli
    vs = eval_cli.parse_variants("retrieve_off:0,retrieve_on:5,retrieve_garbage:5g")
    assert vs[0].name == "retrieve_off" and vs[0].retrieve_top_k == 0 and vs[0].garbage is False
    assert vs[1].retrieve_top_k == 5
    assert vs[2].garbage is True and vs[2].retrieve_top_k == 5


# ---------- e2e（mock engine + backend）----------

class _FakeReport:
    """最小 report 替身：实现 run_variant 用到的属性。"""

    def __init__(self, retrieved, consensus_titles):
        from brainregion.core.report import ReviewReport, CanonicalFinding, Finding
        cfs = [CanonicalFinding(canonical_title=t, dimension="d", severity="high",
                                evidence_quote="q", location="l", suggestion="s", case_ref=None,
                                flagged_by=["m"], source_findings=[
                                    Finding(model="m", dimension="d", severity="high", title=t,
                                            evidence_quote="q", location="l", suggestion="s",
                                            confidence=0.8)]) for t in consensus_titles]
        self._report = ReviewReport(
            document_type="markdown", adapter="generic", panel=["m"],
            consensus=cfs, retrieved_cases=list(retrieved),
            usage={"total_tokens": 100, "cost_usd": 0.05},
            budget={"estimated_usd": 0.04, "jobs_run": 1, "jobs_total": 1, "exhausted": False},
            panel_status={"requested": 1, "ran": 1, "complete": True},
            risk={"overall_level": "medium"},
        )

    def __getattr__(self, name):
        return getattr(self._report, name)


class _FakeEngine:
    def __init__(self, mode, knowledge_provider):
        self.mode = mode
        self.knowledge = knowledge_provider

    async def review(self, doc, *, retrieve_top_k, **kw):
        if self.mode == "garbage":
            return type("Ctx", (), {"report": _FakeReport(["RAND"], ["random advice"])})()
        if retrieve_top_k == 0:  # off
            return type("Ctx", (), {"report": _FakeReport([], [])})()
        return type("Ctx", (), {"report": _FakeReport(["REAL"], ["relevant advice"])})()  # on


def test_run_eval_e2e_three_variants(iso_db):
    variants = [VariantSpec("retrieve_off", 0), VariantSpec("retrieve_on", 5),
                VariantSpec("retrieve_garbage", 5, garbage=True)]
    tasks = [EvalTask(id="t1", input={"content": "doc", "document_type": "markdown"})]
    engines = {
        "retrieve_off": _FakeEngine("branch", _FakeProvider([])),
        "retrieve_on": _FakeEngine("branch", _FakeProvider([])),
        "retrieve_garbage": _FakeEngine("garbage", _FakeProvider([])),
    }
    backend = _FakeBackend(json.dumps({
        "X": {"useful": 1, "overall": 3}, "Y": {"useful": 1, "overall": 3},
        "Z": {"useful": 1, "overall": 3},
    }))
    judge_entries = [{"label": "j1", "model": "fake-judge", "endpoint_id": None}]

    class _Adapter:
        name = "generic"

        def reviewers_dir(self):
            return Path("/nonexistent")

    records, judgements, entry = asyncio.run(runner.run_eval(
        tasks, variants, judge_entries, backend, engines, {}, _Adapter(),
        "rubric", "rhash", "run-e2e", effort=None, max_cost_usd=1.0,
    ))

    assert len(records) == 3  # 3 变体 × 1 任务
    off = next(r for r in records if r.variant == "retrieve_off")
    on = next(r for r in records if r.variant == "retrieve_on")
    garb = next(r for r in records if r.variant == "retrieve_garbage")
    assert off.retrieved_case_ids == []           # off 无 case（结构性）
    assert on.retrieved_case_ids == ["REAL"]      # on 检索到
    assert garb.retrieved_case_ids == ["RAND"]    # garbage 随机
    assert len(judgements) == 3                    # 1 任务 × 1 judge × 3 变体
    assert entry.n_tasks == 1
    assert entry.variants == ["retrieve_off", "retrieve_on", "retrieve_garbage"]
    assert entry.summary["sanity"]["errors"] == []  # 结构性无 error
