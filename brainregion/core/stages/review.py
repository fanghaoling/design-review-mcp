"""ReviewStage：ModelBackend 并发 fan-out（panel × dimensions，独立采样，失败隔离）。

Pipeline 第 4 步。对 PromptStage 产出的每个 job（一个模型 × 一个维度）并发调用 backend，
单模型失败由 backend 内部隔离（返回 error），gather 不被打断。

预算（v1.5）：max_cost_usd 设了的话，预 flight 估每个 job 成本，按 jobs 原序（= panel × dim
顺序，panel 在前 = 用户偏好序）贪心保留直到累计估算超预算，其余裁掉。粗略护栏——成本数据
缺失的模型（litellm 无价，如 glm）用名义单价；实际成本以 report.usage.cost_usd 为准。
effort（v1.5）：透传给 backend，仅 Claude/OpenAI-o 生效，其余丢弃。
"""
from __future__ import annotations

import asyncio
import logging

from ..pipeline import PipelineContext

logger = logging.getLogger("brainregion.stage.review")

# 手维护的每百万 token 价格（input, output USD）——覆盖贵模型（Claude/GPT），让预算上限对它们
# 有意义。不在表里的（glm/deepseek 等 litellm 无价或便宜的）用名义单价。价格变动时更新此处。
# （实测 litellm.cost_per_token 对 gpt-4o/claude 返回 (0,0)、对 glm 抛异常，不可靠，故自维护。）
_PRICE_PER_1M: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-5": (5.0, 15.0),
    "gpt-5.5": (5.0, 15.0),  # gpt-5 系列 anchor（ISS-003：缺失曾致估算比实际低 ~21×）
}
# 不在价表里的模型名义单价 USD/job（glm/deepseek 等很便宜，保守估值）。
# 注意：未知【旗舰】模型也会落到这里 → 严重低估 → 预算护栏失效（ISS-003/ISS-001）。
# 故新接入的贵模型必须显式加进上面的价表，别依赖名义单价。
_NOMINAL_COST_USD = 0.004


def _estimate_job_cost(job: dict) -> float:
    """预估单 job 成本（USD，保守上界：输入按 len/4 估 token、输出按 max_tokens 封顶算满）。

    估的是上界（假设输出打满 max_tokens），故略高于实际——作为护栏偏保守，安全。
    实际成本以 report.usage.cost_usd 为准（litellm 从 response_cost 算）。
    """
    text = (job.get("system") or "") + (job.get("user") or "")
    in_tokens = max(1, len(text) // 4)
    out_tokens = job.get("max_tokens") or 0
    model = job["model"]
    price = _PRICE_PER_1M.get(model) or _PRICE_PER_1M.get(model.split("/")[-1])
    if price:
        in_p, out_p = price
        return in_tokens * in_p / 1_000_000 + out_tokens * out_p / 1_000_000
    return _NOMINAL_COST_USD


def select_jobs_within_budget(jobs: list[dict], budget: float) -> tuple[list[dict], float, bool]:
    """按 jobs 原序贪心保留直到累计估算超预算（break-on-exceed：保留优先模型前缀）。

    返回 (选中 jobs, 累计估算, 是否裁剪过)。
    """
    selected: list[dict] = []
    total = 0.0
    for job in jobs:
        cost = _estimate_job_cost(job)
        if total + cost > budget:
            break  # 下一个会超：停，保留已选前缀，后续全裁
        selected.append(job)
        total += cost
    return selected, total, len(selected) < len(jobs)


class ReviewStage:
    name = "review"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        jobs = list(ctx.prompts)
        ctx.jobs_total = len(jobs)

        if ctx.max_cost_usd is not None and jobs:
            jobs, est, exhausted = select_jobs_within_budget(jobs, float(ctx.max_cost_usd))
            ctx.estimated_cost_usd = est
            ctx.budget_exhausted = exhausted
            if exhausted:
                logger.info(
                    "预算裁剪：max=$%s 估=$%.4f，跑 %d/%d job",
                    ctx.max_cost_usd, est, len(jobs), ctx.jobs_total,
                )
        ctx.jobs_run = len(jobs)

        async def _one(job: dict) -> dict:
            resp = await ctx.backend.complete(
                model=job["model"],
                system=job["system"],
                user=job["user"],
                temperature=job["temperature"],
                top_p=job["top_p"],
                max_tokens=job["max_tokens"],
                effort=ctx.effort,
                endpoint_id=job.get("endpoint_id"),
            )
            # 身份标识用 label（贯穿 Finding.model/flagged_by/failed_models），防中转模型上游真名
            # （如 glm-5.2）与官方 zai/glm-5.2 撞名致 consensus 错误合并。credential 不进 pipeline。
            return {"model": job["label"], "dimension": job["dimension"], "response": resp}

        ctx.responses = await asyncio.gather(*(_one(j) for j in jobs))
        return ctx
