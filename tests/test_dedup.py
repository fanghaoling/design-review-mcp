"""DedupStage（v1.2 F2 模型内去重）+ NormalizeStage case_ref 跨维度合并（v1.2 F1）。"""
from __future__ import annotations

import asyncio

from brainregion.core import ReviewDocument
from brainregion.core.pipeline import PipelineContext
from brainregion.core.report import CanonicalFinding, Finding
from brainregion.core.stages.dedup import DedupStage
from brainregion.core.stages.normalize import merge_canonical_by_case_ref
from brainregion.knowledge.base import Case


def _finding(model, title, case_ref=None, confidence=0.8, dimension="ecs_perf"):
    return Finding(
        model=model, dimension=dimension, severity="high", title=title,
        evidence_quote="q", location="l", suggestion="s", confidence=confidence,
        case_ref=case_ref,
    )


def _ctx(findings):
    ctx = PipelineContext(
        document=ReviewDocument.markdown("x"), adapter=None, backend=None, knowledge=None
    )
    ctx.findings = findings
    return ctx


# ===== F2: DedupStage（模型内去重）=====

def test_dedup_same_case_ref_within_model():
    """同模型同 case_ref（换皮 title）→ 去成 1 条，留 confidence 最高的。"""
    ctx = _ctx([
        _finding("glm", "t1", case_ref="NET-003", confidence=0.7),
        _finding("glm", "t2 totally different words", case_ref="NET-003", confidence=0.95),
        _finding("glm", "t3", case_ref="NET-003", confidence=0.6),
    ])
    asyncio.run(DedupStage().process(ctx))
    assert len(ctx.findings) == 1
    assert ctx.findings[0].confidence == 0.95


def test_dedup_title_similar_within_model():
    """同模型无 case_ref 但 title 高度相似 → 去重。"""
    ctx = _ctx([
        _finding("glm", "ApplyDamage 用 HasComponent 判可伤导致方块不掉血"),
        _finding("glm", "ApplyDamage 用 HasComponent 判可伤方块掉血失败"),
    ])
    asyncio.run(DedupStage().process(ctx))
    assert len(ctx.findings) == 1


def test_dedup_keeps_distinct_findings():
    """不同 case_ref + 不同 title → 各自保留。"""
    ctx = _ctx([
        _finding("glm", "Burst 编译失败 BC1064 报错", case_ref="A"),
        _finding("glm", "网络同步丢包导致状态不一致", case_ref="B"),
    ])
    asyncio.run(DedupStage().process(ctx))
    assert len(ctx.findings) == 2


def test_dedup_not_cross_model():
    """跨模型同 bug 不去重（交给 NormalizeStage）。"""
    ctx = _ctx([
        _finding("gpt", "same bug title here", case_ref="NET-003"),
        _finding("glm", "same bug title here", case_ref="NET-003"),
    ])
    asyncio.run(DedupStage().process(ctx))
    assert len(ctx.findings) == 2


def test_dedup_none_case_ref_no_false_match():
    """两条都 case_ref=None 不应被 case_ref 规则误判（None==None 不算），靠 title 规则。"""
    ctx = _ctx([
        _finding("glm", "甲乙丙丁", case_ref=None),
        _finding("glm", "子丑寅卯", case_ref=None),
    ])
    asyncio.run(DedupStage().process(ctx))
    assert len(ctx.findings) == 2


# ===== F1: merge_canonical_by_case_ref（跨维度合并）=====

def _cf(title, dimension, models, case_ref=None, severity="high"):
    src = [_finding(m, title, case_ref=case_ref, dimension=dimension) for m in models]
    return CanonicalFinding(
        canonical_title=title, dimension=dimension, severity=severity,
        evidence_quote="q", location="l", suggestion="s", case_ref=case_ref,
        flagged_by=list(models), source_findings=src,
    )


def test_merge_cross_dimension_same_case_ref():
    """同 case_ref 跨 dimension（safety/netcode/planner）→ 合成 1 条，案例标题/category。"""
    cases = [Case(id="NET-003", title="伤害判定走 buffer", category="netcode")]
    canonical = [
        _cf("缺 buffer 检查", "safety", ["gpt"], case_ref="NET-003"),
        _cf("命中反模式", "netcode", ["glm"], case_ref="NET-003"),
        _cf("数据结构冲突", "planner", ["deepseek"], case_ref="NET-003"),
    ]
    out = merge_canonical_by_case_ref(canonical, cases)
    assert len(out) == 1
    merged = out[0]
    assert "NET-003" in merged.canonical_title  # 用案例标题
    assert "伤害判定走 buffer" in merged.canonical_title
    assert merged.dimension == "netcode"  # 用案例 category
    assert set(merged.flagged_by) == {"gpt", "glm", "deepseek"}  # 并集
    assert len(merged.source_findings) == 3  # 全保留


def test_merge_no_case_ref_unchanged():
    """无 case_ref 的原样保留。"""
    canonical = [
        _cf("A", "planner", ["gpt"], case_ref=None),
        _cf("B", "safety", ["glm"], case_ref=None),
    ]
    out = merge_canonical_by_case_ref(canonical, [])
    assert len(out) == 2


def test_merge_severity_takes_max():
    """合并后 severity 取最高。"""
    cases = [Case(id="X", title="x", category="ecs_perf")]
    canonical = [
        _cf("a", "ecs_perf", ["gpt"], case_ref="X", severity="low"),
        _cf("b", "ecs_perf", ["glm"], case_ref="X", severity="high"),
    ]
    out = merge_canonical_by_case_ref(canonical, cases)
    assert len(out) == 1
    assert out[0].severity == "high"


def test_merge_case_title_missing_falls_back():
    """retrieved_cases 里没有该 case（标题空）→ canonical_title 不强加 [id]，保留 LLM 标题。"""
    canonical = [_cf("LLM 给的标题", "netcode", ["gpt"], case_ref="NET-999")]
    out = merge_canonical_by_case_ref(canonical, [])  # 无 case 元数据
    assert len(out) == 1
    assert out[0].canonical_title == "LLM 给的标题"
