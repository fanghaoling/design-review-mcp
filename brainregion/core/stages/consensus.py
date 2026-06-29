"""ConsensusStage：canonical findings 按模型同意数分档（consensus/majority/individual）。

Pipeline 第 7 步。基于 NormalizeStage 的 flagged_by 计数：
- consensus：所有成功模型都标（且 >= threshold）
- majority：2+ 但非全部
- individual：单个模型
按 severity 排序。
"""
from __future__ import annotations

import logging
from collections import defaultdict

from ..pipeline import PipelineContext
from ..report import Finding

logger = logging.getLogger("brainregion.stage.consensus")

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class ConsensusStage:
    name = "consensus"

    def __init__(self, threshold: int = 2) -> None:
        self.threshold = threshold

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        # 成功模型 = 有 finding 的 ∪ response ok 的（含 ok 但无 finding 的模型）
        successful = {f.model for f in ctx.findings}
        successful |= {it["model"] for it in ctx.responses if it["response"].ok}
        num_models = len(successful) or 1
        threshold = min(self.threshold, num_models)

        consensus = []
        majority = []
        individual_map: dict[str, list[Finding]] = defaultdict(list)

        # consensus/majority 需要至少 2 个成功模型才有"多模型同意"语义。
        # 预算裁剪/失败只剩 1 个成功模型时，不能把它标成 consensus（ISS-001）。
        multi = num_models >= 2

        for cf in ctx.canonical_findings:
            count = len(cf.flagged_by)
            if multi and count == num_models and count >= threshold:
                cf.bucket = "consensus"
                consensus.append(cf)
            elif multi and count >= 2:
                cf.bucket = "majority"
                majority.append(cf)
            else:
                cf.bucket = "individual"
                for f in cf.source_findings:
                    individual_map[f.model].append(f)

        consensus.sort(key=lambda c: _SEVERITY_ORDER.get(c.severity, 9))
        majority.sort(key=lambda c: _SEVERITY_ORDER.get(c.severity, 9))
        ctx.consensus = consensus
        ctx.majority = majority
        ctx.individual = dict(individual_map)
        return ctx
