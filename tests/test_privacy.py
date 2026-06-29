"""v1.7 隐私模式：PrivacyPolicy（Off/Strict）+ Finding.attachments + mediation 调权。

不调网（mock backend）。覆盖：
- build_policy（off→None / strict→StrictPolicy / strict 无 trusted raise / 未知 raise）
- OffPolicy（transform 原样 coverage=1.0 / mediate 原样）
- StrictPolicy.transform（摘要+coverage、低 coverage raise、失败 raise 不回退明文、JSON 解析）
- StrictPolicy.mediate（附加 attachment 不改原字段、unconfirmed/rejected 不丢、失败全标 unconfirmed）
- Finding.attachments 向后兼容
- ScoreStage _mediation_factor（rejected/unconfirmed/confirmed 调权）
- MediateStage thin wrapper 调 policy.mediate
"""
from __future__ import annotations

import asyncio
import json

import pytest

from brainregion.core.document import ReviewDocument
from brainregion.core.report import Finding
from brainregion.core.stages.mediate import MediateStage
from brainregion.core.pipeline import PipelineContext
from brainregion.core.stages.score import _mediation_factor
from brainregion.privacy import OffPolicy, StrictPolicy, TransformResult, build_policy
from brainregion.providers.base import ModelResponse


_TRUSTED = {"label": "trusted", "model": "glm-5.2", "endpoint_id": "zhipu"}


class _Backend:
    """按 system 关键字分发 transform/mediate 响应。resp=None 模拟失败。"""

    def __init__(self, transform_resp=None, mediate_resp=None):
        self.transform_resp = transform_resp
        self.mediate_resp = mediate_resp
        self.calls = []

    async def complete(self, *, model, system, user, temperature=0.3, top_p=0.95, max_tokens=4096, effort=None, endpoint_id=None):
        self.calls.append({"endpoint_id": endpoint_id})
        if "中介" in system:  # mediate（先判：mediate system 含「脱敏摘要」，避免误判成 transform）
            if self.mediate_resp is None:
                return ModelResponse(model=model, content="", error="trusted down")
            return ModelResponse(model=model, content=json.dumps(self.mediate_resp))
        if "脱敏" in system:  # transform
            if self.transform_resp is None:
                return ModelResponse(model=model, content="", error="trusted down")
            return ModelResponse(model=model, content=json.dumps(self.transform_resp))
        return ModelResponse(model=model, content="{}")


# ===== build_policy =====

def test_build_policy_none_and_off():
    assert build_policy(None) is None
    assert build_policy({"policy": "off"}) is None
    assert build_policy({}) is None


def test_build_policy_strict():
    p = build_policy({"policy": "strict", "min_coverage": 0.6}, _TRUSTED)
    assert isinstance(p, StrictPolicy)
    assert p.trusted is _TRUSTED
    assert p.min_coverage == 0.6


def test_build_policy_strict_needs_trusted():
    with pytest.raises(ValueError, match="trusted"):
        build_policy({"policy": "strict"}, None)


def test_build_policy_unknown():
    with pytest.raises(ValueError, match="未知"):
        build_policy({"policy": "enterprise"}, _TRUSTED)


# ===== OffPolicy =====

def test_off_policy_passthrough():
    doc = ReviewDocument.markdown("secret")
    p = OffPolicy()
    r = asyncio.run(p.transform(doc, _Backend()))
    assert isinstance(r, TransformResult)
    assert r.document is doc and r.coverage == 1.0
    fs = [Finding(model="m", dimension="d", severity="low", title="t", evidence_quote="e", location="", suggestion="", confidence=0.5)]
    assert asyncio.run(p.mediate(fs, doc, _Backend())) is fs


# ===== StrictPolicy.transform =====

def test_strict_transform_ok():
    b = _Backend(transform_resp={"summary": "脱敏摘要", "coverage": 0.85, "missing_topics": ["Deploy"], "redacted_items": ["项目名"]})
    p = StrictPolicy(trusted=_TRUSTED, min_coverage=0.5)
    r = asyncio.run(p.transform(ReviewDocument.markdown("原文"), b))
    assert r.document.content == "脱敏摘要"
    assert r.coverage == 0.85
    assert r.missing_topics == ["Deploy"]
    assert b.calls[0]["endpoint_id"] == "zhipu"  # trusted endpoint_id 透传


def test_strict_transform_low_coverage_raise():
    b = _Backend(transform_resp={"summary": "x", "coverage": 0.3})
    p = StrictPolicy(trusted=_TRUSTED, min_coverage=0.5)
    with pytest.raises(RuntimeError, match="coverage"):
        asyncio.run(p.transform(ReviewDocument.markdown("原文"), b))


def test_strict_transform_failure_raise_no_fallback():
    """trusted 调用失败 → raise，绝不静默回退明文。"""
    b = _Backend(transform_resp=None)
    p = StrictPolicy(trusted=_TRUSTED, min_coverage=0.5)
    with pytest.raises(RuntimeError, match="不回退明文"):
        asyncio.run(p.transform(ReviewDocument.markdown("原文"), b))


def test_strict_transform_empty_summary_raise():
    b = _Backend(transform_resp={"summary": "", "coverage": 0.9})
    p = StrictPolicy(trusted=_TRUSTED, min_coverage=0.5)
    with pytest.raises(RuntimeError, match="不回退明文"):
        asyncio.run(p.transform(ReviewDocument.markdown("原文"), b))


# ===== StrictPolicy.mediate =====

def _findings(n=2):
    return [
        Finding(model="panelA", dimension="d", severity="high", title=f"t{i}",
                evidence_quote=f"摘要ev{i}", location="", suggestion=f"s{i}", confidence=0.8)
        for i in range(n)
    ]


def test_strict_mediate_appends_attachment_keeps_original():
    b = _Backend(mediate_resp=[{"id": 0, "evidence": "原文ev0", "reason": "确实", "verdict": "confirmed"},
                                {"id": 1, "evidence": "", "reason": "误报", "verdict": "rejected"}])
    p = StrictPolicy(trusted=_TRUSTED)
    fs = _findings(2)
    out = asyncio.run(p.mediate(fs, ReviewDocument.markdown("原文"), b))
    assert out[0].evidence_quote == "摘要ev0"  # 原字段不变
    assert out[0].title == "t0"
    assert out[0].attachments[0].source == "trusted"
    assert out[0].attachments[0].payload == {"evidence": "原文ev0", "reason": "确实", "verdict": "confirmed"}
    assert out[1].attachments[0].payload["verdict"] == "rejected"


def test_strict_mediate_unconfirmed_not_dropped():
    """id 未在响应里 → 标 unconfirmed，不丢弃。"""
    b = _Backend(mediate_resp=[{"id": 0, "evidence": "x", "reason": "r", "verdict": "confirmed"}])
    p = StrictPolicy(trusted=_TRUSTED)
    fs = _findings(2)
    out = asyncio.run(p.mediate(fs, ReviewDocument.markdown("原文"), b))
    assert len(out) == 2  # 没丢
    assert out[1].attachments[0].payload["verdict"] == "unconfirmed"


def test_strict_mediate_failure_all_unconfirmed():
    """trusted mediate 失败 → 全标 unconfirmed，不终止。"""
    b = _Backend(mediate_resp=None)
    p = StrictPolicy(trusted=_TRUSTED)
    fs = _findings(2)
    out = asyncio.run(p.mediate(fs, ReviewDocument.markdown("原文"), b))
    assert len(out) == 2
    assert all(f.attachments[0].payload["verdict"] == "unconfirmed" for f in out)


# ===== Finding.attachments 向后兼容 =====

def test_finding_attachments_default_empty():
    f = Finding(model="m", dimension="d", severity="low", title="t",
                evidence_quote="e", location="", suggestion="", confidence=0.5)
    assert f.attachments == []


# ===== ScoreStage _mediation_factor =====

def _cf(verdicts):
    """构造伪 CanonicalFinding，source_findings 各带一个 mediation attachment。"""
    class _Att:
        def __init__(self, v):
            self.type = "mediation"
            self.payload = {"verdict": v}
    class _F:
        def __init__(self, v):
            self.attachments = [_Att(v)] if v else []
    class _CF:
        def __init__(self, verdicts):
            self.source_findings = [_F(v) for v in verdicts]
    return _CF(verdicts)


def test_mediation_factor_confirmed_and_none():
    assert _mediation_factor(_cf(["confirmed"])) == 1.0
    assert _mediation_factor(_cf([None])) == 1.0


def test_mediation_factor_unconfirmed():
    assert _mediation_factor(_cf(["unconfirmed"])) == 0.5


def test_mediation_factor_rejected_dominates():
    """任一 rejected → 0.2（最差决定）。"""
    assert _mediation_factor(_cf(["confirmed", "rejected"])) == 0.2


# ===== MediateStage thin wrapper =====

def test_mediate_stage_calls_policy():
    fs = _findings(1)
    ctx = PipelineContext(
        document=ReviewDocument.markdown("eff"), adapter=None,
        backend=_Backend(mediate_resp=[{"id": 0, "evidence": "ev", "reason": "r", "verdict": "confirmed"}]),
        knowledge=None,
    )
    ctx.original_document = ReviewDocument.markdown("原文")
    ctx.findings = fs
    ctx.policy = StrictPolicy(trusted=_TRUSTED)
    asyncio.run(MediateStage().process(ctx))
    assert ctx.findings[0].attachments[0].payload["verdict"] == "confirmed"


def test_mediate_stage_skips_when_no_policy():
    """off 模式（pipeline 不插 MediateStage，但即使调了也跳过）。"""
    fs = _findings(1)
    ctx = PipelineContext(document=ReviewDocument.markdown("x"), adapter=None, backend=_Backend(), knowledge=None)
    ctx.findings = fs
    ctx.policy = None
    asyncio.run(MediateStage().process(ctx))
    assert fs[0].attachments == []  # 没附加
