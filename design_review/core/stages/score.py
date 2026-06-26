"""ScoreStage：calibrated_confidence + 组装 ReviewReport（② 校准置信度）。

Pipeline 第 8 步。calibrated = mean(model_confidence) × consensus_factor × knowledge_match。
汇总 usage/cost/failed/risk，组装标准 ReviewReport。
"""
from __future__ import annotations

import logging

from ..errors import classify_error
from ..pipeline import PipelineContext, Stage
from ..report import ReviewReport

logger = logging.getLogger("design_review.stage.score")

_CONSENSUS_FACTOR = {"consensus": 1.0, "majority": 0.7, "individual": 0.3}


def _calibrate(cf, retrieved_ids: set[str]) -> float:
    src = cf.source_findings or []
    base = sum(getattr(f, "confidence", 0.5) for f in src) / max(len(src), 1)
    factor = _CONSENSUS_FACTOR.get(cf.bucket, 0.3)
    km = 1.2 if (cf.case_ref and cf.case_ref in retrieved_ids) else 0.9
    return round(min(base * factor * km, 1.0), 3)


class ScoreStage:
    name = "score"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        retrieved_ids = {c.id for c in ctx.retrieved_cases}
        knowledge_hit: list[str] = []
        for cf in ctx.consensus + ctx.majority:
            if cf.case_ref and cf.case_ref in retrieved_ids:
                knowledge_hit.append(cf.case_ref)
            cf.calibrated_confidence = _calibrate(cf, retrieved_ids)

        failed = [
            {
                "model": it["model"],
                "error": it["response"].error,
                **classify_error(it["response"].error or ""),
            }
            for it in ctx.responses
            if not it["response"].ok
        ]
        total_tokens = 0
        cost = 0.0
        for it in ctx.responses:
            u = it["response"].usage or {}
            total_tokens += int(u.get("total_tokens", 0) or 0)
            c = it["response"].cost_usd
            if c:
                cost += float(c)

        high_count = sum(
            1 for cf in ctx.consensus + ctx.majority if cf.severity == "high"
        )
        if high_count > 0:
            overall = "high"
        elif ctx.consensus or ctx.majority:
            overall = "medium"
        else:
            overall = "low"

        ind_count = sum(len(v) for v in ctx.individual.values())
        report = ReviewReport(
            document_type=ctx.document.type,
            adapter=ctx.adapter.name,
            project_version=dict(ctx.project_version),
            panel=list(ctx.panel),
            failed_models=failed,
            retrieved_cases=sorted(retrieved_ids),
            consensus=list(ctx.consensus),
            majority=list(ctx.majority),
            individual={k: list(v) for k, v in ctx.individual.items()},
            knowledge_hit=sorted(set(knowledge_hit)),
            usage={"total_tokens": total_tokens, "cost_usd": round(cost, 6)},
            summary=f"consensus={len(ctx.consensus)} majority={len(ctx.majority)} "
            f"individual={ind_count} failed={len(failed)}",
            risk={"overall_level": overall, "high_severity_count": high_count},
        )
        ctx.report = report
        return ctx
