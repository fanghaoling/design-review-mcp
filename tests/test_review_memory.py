"""v2 Review Memory + 模型可信度：finding_feedback 表 + model_reliability + score 加权
+ parse 填 id + dedup 断链点 A + _rebuild_report 补填 id + mark_finding 反查。

不调网。每个测试用 isolated_db fixture 独立 SQLite（tmp dir 作 UNITY_PROJECT_ROOT，不污染真实 db）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from design_review import reviews_db
from design_review.core.pipeline import PipelineContext
from design_review.core.report import CanonicalFinding, Finding
from design_review.core.stages.dedup import DedupStage
from design_review.core.stages.parse import ParseStage
from design_review.core.stages.score import _calibrate, _reliability_factor
from design_review.providers.base import ModelResponse


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个测试独立 SQLite（tmp dir 作 UNITY_PROJECT_ROOT，不污染真实 db）。"""
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    reviews_db._connect()  # 触发建表（reviews + finding_feedback）
    return tmp_path


class _MockAdapter:
    name = "mock"

    def reviewers_dir(self):
        return Path(".")


def _finding(model="gpt-4o", dimension="planner", confidence=0.8, fid=""):
    return Finding(
        id=fid, model=model, dimension=dimension, severity="high", title="t",
        evidence_quote="q", location="l", suggestion="s", confidence=confidence,
    )


# ===== record_feedback =====

def test_record_feedback_upsert(isolated_db):
    reviews_db.record_feedback(finding_id="gpt-4o-0", params_hash="h1", label="gpt-4o",
                               dimension="planner", decision="accepted", note="真实发现")
    reviews_db.record_feedback(finding_id="gpt-4o-0", params_hash="h1", label="gpt-4o",
                               dimension="planner", decision="rejected", note="改主意")  # UPSERT 覆盖
    rows = reviews_db._connect().execute("SELECT decision, note FROM finding_feedback").fetchall()
    assert len(rows) == 1
    assert rows[0]["decision"] == "rejected"


def test_record_feedback_invalid_args(isolated_db):
    with pytest.raises(ValueError):
        reviews_db.record_feedback(finding_id="x-0", params_hash="h", label="x",
                                   dimension="planner", decision="maybe")  # decision 枚举
    with pytest.raises(ValueError):
        reviews_db.record_feedback(finding_id="", params_hash="h", label="x",
                                   dimension="planner", decision="accepted")  # 空 finding_id
    with pytest.raises(ValueError):
        reviews_db.record_feedback(finding_id="x-0", params_hash="h", label="",
                                   dimension="planner", decision="accepted")  # 空 label


# ===== model_reliability（(label, dimension) 维度）=====

def test_model_reliability_empty_labels(isolated_db):
    assert reviews_db.model_reliability([]) == {}


def test_model_reliability_small_sample_skipped(isolated_db):
    """4 条（<5）→ 不进结果，调用方 .get(key,1.0) 兜底 1.0（向后兼容）。"""
    for i in range(4):
        reviews_db.record_feedback(finding_id=f"gpt-4o-{i}", params_hash="h", label="gpt-4o",
                                   dimension="planner", decision="accepted")
    assert reviews_db.model_reliability(["gpt-4o"]) == {}


def test_model_reliability_laplace_all_accepted(isolated_db):
    """10 全 accepted → (10+2)/(10+4) = 0.857（Beta(2,2) 不达 1）。"""
    for i in range(10):
        reviews_db.record_feedback(finding_id=f"gpt-4o-{i}", params_hash="h", label="gpt-4o",
                                   dimension="planner", decision="accepted")
    rel = reviews_db.model_reliability(["gpt-4o"])
    assert rel[("gpt-4o", "planner")] == pytest.approx(0.857, abs=0.002)


def test_model_reliability_laplace_all_rejected(isolated_db):
    """10 全 rejected → (0+2)/14 = 0.143（不归 0，保留可见性）。"""
    for i in range(10):
        reviews_db.record_feedback(finding_id=f"gpt-4o-{i}", params_hash="h", label="gpt-4o",
                                   dimension="planner", decision="rejected")
    rel = reviews_db.model_reliability(["gpt-4o"])
    assert rel[("gpt-4o", "planner")] == pytest.approx(0.143, abs=0.002)


def test_model_reliability_dimension_separated(isolated_db):
    """同模型不同维度分开统计（GPT Blocker：Claude planner 强/security 弱不平均化）。"""
    for i in range(6):
        reviews_db.record_feedback(finding_id=f"c-p{i}", params_hash="h", label="claude",
                                   dimension="planner", decision="accepted")
        reviews_db.record_feedback(finding_id=f"c-s{i}", params_hash="h", label="claude",
                                   dimension="security", decision="rejected")
    rel = reviews_db.model_reliability(["claude"])
    assert rel[("claude", "planner")] >= 0.8   # planner 强（6 accepted → 0.8）
    assert rel[("claude", "security")] < 0.3  # security 弱


def test_model_reliability_missing_label_absent(isolated_db):
    """panel 含 label 但无 feedback → 不进结果（调用方 .get 兜底 1.0）。"""
    assert reviews_db.model_reliability(["unknown-model"]) == {}


# ===== score _reliability_factor（温和区间 [0.75,1.15]）=====

def test_reliability_factor_empty_is_one():
    assert _reliability_factor([_finding()], None) == 1.0
    assert _reliability_factor([_finding()], {}) == 1.0


def test_reliability_factor_missing_key_one():
    """reliability 有但不命中 (model,dim) → 1.0（找不到证据不惩罚）。"""
    rel = {("other", "planner"): 0.3}
    assert _reliability_factor([_finding(model="gpt-4o")], rel) == 1.0


def test_reliability_factor_clamps_low():
    """reliability 极低 → 钳到 0.75（温和，不压没 confidence/consensus）。"""
    rel = {("gpt-4o", "planner"): 0.01}
    assert _reliability_factor([_finding()], rel) == 0.75


def test_reliability_factor_clamps_high():
    """reliability 异常 >1.15 → 钳到 1.15（防御；正常 model_reliability ≤1.0 不触发）。"""
    rel = {("gpt-4o", "planner"): 2.0}
    assert _reliability_factor([_finding()], rel) == 1.15


def test_calibrate_backward_compat_empty_reliability():
    """reliability=None vs {} → calibrated 逐字节相同（v1.8 公式）。"""
    def _cf():
        return CanonicalFinding(
            canonical_title="t", dimension="planner", severity="high", evidence_quote="q",
            location="l", suggestion="s", case_ref=None, bucket="consensus",
            source_findings=[_finding(confidence=0.8)],
        )
    assert _calibrate(_cf(), set()) == _calibrate(_cf(), set(), reliability={})


# ===== parse 填 id =====

def test_parse_fills_id_per_label():
    ctx = PipelineContext(document=None, adapter=_MockAdapter(), backend=None, knowledge=None)
    ctx.responses = [
        {"model": "gpt-4o", "dimension": "planner", "response": ModelResponse(
            model="gpt-4o",
            content='{"issues":[{"dimension":"planner","severity":"high","title":"a",'
                    '"evidence_quote":"q","location":"l","suggestion":"s","confidence":0.8}]}')},
        {"model": "gpt-4o", "dimension": "safety", "response": ModelResponse(
            model="gpt-4o",
            content='{"issues":[{"dimension":"safety","severity":"medium","title":"b",'
                    '"evidence_quote":"q","location":"l","suggestion":"s","confidence":0.5}]}')},
    ]
    ctx.retrieved_cases = []
    asyncio.run(ParseStage().process(ctx))
    assert [f.id for f in ctx.findings] == ["gpt-4o-0", "gpt-4o-1"]  # label+seq，跨 dimension 递增


# ===== dedup 断链点 A（deduped_ids）=====

def test_dedup_lower_conf_dropped_id_carried():
    """同模型相似 finding 去重，被丢的 id 挂代表 deduped_ids。"""
    f1 = _finding(model="glm", dimension="planner", confidence=0.9, fid="glm-0")
    f1.title = "内存泄漏 bug"
    f2 = _finding(model="glm", dimension="planner", confidence=0.7, fid="glm-1")
    f2.title = "内存泄漏 bug 换皮"
    ctx = PipelineContext(document=None, adapter=_MockAdapter(), backend=None, knowledge=None)
    ctx.findings = [f1, f2]
    asyncio.run(DedupStage().process(ctx))
    assert len(ctx.findings) == 1
    assert ctx.findings[0].id == "glm-0"        # confidence 高的代表保留
    assert ctx.findings[0].deduped_ids == ["glm-1"]  # 被丢 id 挂代表


def test_dedup_higher_conf_replaces_inherits_old_id():
    """f2 confidence 高取代代表 → 旧代表 id 带到 f2.deduped_ids。"""
    f1 = _finding(model="glm", dimension="planner", confidence=0.7, fid="glm-0")
    f1.title = "内存泄漏"
    f2 = _finding(model="glm", dimension="planner", confidence=0.9, fid="glm-1")
    f2.title = "内存泄漏 bug"
    ctx = PipelineContext(document=None, adapter=_MockAdapter(), backend=None, knowledge=None)
    ctx.findings = [f1, f2]
    asyncio.run(DedupStage().process(ctx))
    assert ctx.findings[0].id == "glm-1"        # f2 取代
    assert ctx.findings[0].deduped_ids == ["glm-0"]  # 旧代表 id 带到新代表


# ===== _rebuild_report 旧缓存补填 id =====

def test_rebuild_report_fills_missing_id():
    """旧缓存 finding 无 id → _rebuild_report 就地补填 f"{model}-{idx}"。"""
    from design_review.server import _rebuild_report
    old = {
        "document_type": "markdown", "adapter": "unity", "project_version": {},
        "panel": ["gpt-4o"], "failed_models": [], "retrieved_cases": [],
        "consensus": [], "majority": [],
        "individual": {"gpt-4o": [{"model": "gpt-4o", "dimension": "planner", "severity": "high",
                                    "title": "t", "evidence_quote": "q", "location": "l",
                                    "suggestion": "s", "confidence": 0.8}]},
        "knowledge_hit": [], "usage": {}, "summary": "", "risk": {},
    }
    rep = _rebuild_report(old)
    assert rep.individual["gpt-4o"][0].id == "gpt-4o-0"
    # v2 修 bug：privacy/context_compression 补回
    assert rep.privacy == {}
    assert rep.context_compression == {}


# ===== mark_finding 反查 =====

def test_mark_finding_with_params_hash(isolated_db):
    from design_review.server import mark_finding
    report = {
        "document_type": "markdown", "adapter": "unity", "project_version": {},
        "panel": ["gpt-4o"], "failed_models": [], "retrieved_cases": [],
        "consensus": [], "majority": [],
        "individual": {"gpt-4o": [{"id": "gpt-4o-0", "model": "gpt-4o", "dimension": "planner",
                                    "severity": "high", "title": "t", "evidence_quote": "q",
                                    "location": "l", "suggestion": "s", "confidence": 0.8}]},
        "knowledge_hit": [], "usage": {}, "summary": "", "risk": {},
    }
    reviews_db.record(params_hash="abc123def", report_dict=report, adapter="unity", panel=["gpt-4o"])

    res = mark_finding(finding_id="gpt-4o-0", decision="rejected", params_hash="abc123def")
    assert res["ok"] is True
    assert res["label"] == "gpt-4o"
    assert res["dimension"] == "planner"
    assert res["cache_invalidated"] is True


def test_mark_finding_finds_via_deduped_ids(isolated_db):
    """断链点 A 反查：finding 在 deduped_ids 里（被去重）也能 mark。"""
    from design_review.server import mark_finding
    report = {
        "document_type": "markdown", "adapter": "unity", "project_version": {},
        "panel": ["glm"], "failed_models": [], "retrieved_cases": [],
        "consensus": [{"canonical_title": "t", "dimension": "planner", "severity": "high",
                        "evidence_quote": "q", "location": "l", "suggestion": "s", "case_ref": None,
                        "flagged_by": ["glm"], "bucket": "consensus", "calibrated_confidence": 0.8,
                        "source_findings": [{"id": "glm-0", "model": "glm", "dimension": "planner",
                                              "severity": "high", "title": "t", "evidence_quote": "q",
                                              "location": "l", "suggestion": "s", "confidence": 0.9}],
                        "deduped_ids": ["glm-1"]}],
        "majority": [],
        "individual": {"glm": []},
        "knowledge_hit": [], "usage": {}, "summary": "", "risk": {},
    }
    reviews_db.record(params_hash="h1", report_dict=report, adapter="unity", panel=["glm"])
    res = mark_finding(finding_id="glm-1", decision="rejected", params_hash="h1")  # glm-1 在 deduped_ids
    assert res["ok"] is True


def test_mark_finding_invalid_inputs(isolated_db):
    from design_review.server import mark_finding
    with pytest.raises(ValueError):
        mark_finding(finding_id="no-seq-here", decision="accepted")  # 格式错（无 -数字 结尾）
    with pytest.raises(ValueError):
        mark_finding(finding_id="gpt-4o-0", decision="maybe")  # decision 枚举


def test_mark_finding_not_found(isolated_db):
    from design_review.server import mark_finding
    with pytest.raises(ValueError):
        mark_finding(finding_id="gpt-4o-0", decision="accepted")  # 无 review 含此 id，未传 params_hash


# ===== lookup_review_by_finding 扫三 bucket（断链点 B）=====

def test_lookup_finds_in_majority_bucket(isolated_db):
    """断链点 B：finding 在 majority（非 individual）也能反查到。"""
    report = {
        "document_type": "markdown", "adapter": "unity", "project_version": {},
        "panel": ["gpt-4o"], "failed_models": [], "retrieved_cases": [],
        "consensus": [],
        "majority": [{"canonical_title": "t", "dimension": "safety", "severity": "medium",
                       "evidence_quote": "q", "location": "l", "suggestion": "s", "case_ref": None,
                       "flagged_by": ["gpt-4o"], "bucket": "majority", "calibrated_confidence": 0.5,
                       "source_findings": [{"id": "gpt-4o-0", "model": "gpt-4o", "dimension": "safety",
                                             "severity": "medium", "title": "t", "evidence_quote": "q",
                                             "location": "l", "suggestion": "s", "confidence": 0.5}],
                       "deduped_ids": []}],
        "individual": {"gpt-4o": []},
        "knowledge_hit": [], "usage": {}, "summary": "", "risk": {},
    }
    reviews_db.record(params_hash="h1", report_dict=report, adapter="unity", panel=["gpt-4o"])
    phash, label, dim = reviews_db.lookup_review_by_finding("gpt-4o-0")
    assert phash == "h1"
    assert label == "gpt-4o"
    assert dim == "safety"
