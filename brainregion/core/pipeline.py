"""Pipeline + Stage + PipelineContext：可插拔的审查流水线。

Pipeline = list[Stage]，engine 顺序执行每个 Stage 的 process(ctx)。Stage 原地修改
PipelineContext 并返回。v2 可 pipeline.insert(DebateStage(), before="normalize") 零改其它。

PipelineContext 贯穿所有 Stage，承载：装配的 adapter/backend/knowledge、调用参数、
以及各 Stage 逐步填充的中间产物（retrieved_cases/context/prompts/responses/findings/
canonical/report）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class PipelineContext:
    """贯穿 Pipeline 各 Stage 的可变上下文。"""

    # 装配注入（Any 避免 core 反向依赖 providers/knowledge/adapters）：
    document: Any  # ReviewDocument
    adapter: Any  # ProjectAdapter
    backend: Any  # ModelBackend
    knowledge: Any  # KnowledgeProvider
    # 调用参数（v1.6: panel 是 PanelEntry dict 列表 {label, model, endpoint_id}，不含 credential）：
    panel: list[dict] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    retrieve_top_k: int = 5
    extra_context: str = ""
    effort: str | None = None  # low/medium/high/xhigh/max；None=各模型默认。Claude/OpenAI-o 才生效
    max_cost_usd: float | None = None  # per-review 总成本上限；None=无上限
    # v1.7 隐私策略：
    policy: Any = None  # PrivacyPolicy（None=不脱敏，PromptStage 拿原文）
    original_document: Any = None  # 原文（Mediate 看全文）；ctx.document 是 effective（脱敏摘要 or 原文）
    privacy_meta: dict = field(default_factory=dict)  # {policy, coverage, missing_topics, trusted}
    # v1.8 发散/可行性维度：
    context_modes: dict = field(default_factory=dict)  # {dim: full|compressed|minimal}
    context_compression: dict = field(default_factory=dict)  # {dim: {mode, original_chars, compressed_chars, ratio}}
    parse_failed: list = field(default_factory=list)  # ParseStage 解析失败的 model label
    min_compressed_chars: int = 50  # v1.8 _compress_document 下限
    reliability: dict = field(default_factory=dict)  # v2 {(label,dim):0~1} 模型可信度，server 注入；空→全1.0 不加权
    # 各 Stage 填充：
    project_version: dict[str, str] = field(default_factory=dict)
    retrieved_cases: list = field(default_factory=list)
    context: str = ""  # ContextStage 聚合的 ground-truth context
    prompts: list[dict] = field(default_factory=list)  # PromptStage: {model, dimension, system, user, temperature, top_p, max_tokens}
    responses: list[dict] = field(default_factory=list)  # ReviewStage: ModelResponse_dict | {model, error}
    findings: list = field(default_factory=list)  # ParseStage: Finding
    canonical_findings: list = field(default_factory=list)  # NormalizeStage: CanonicalFinding
    consensus: list = field(default_factory=list)  # ConsensusStage
    majority: list = field(default_factory=list)
    individual: dict = field(default_factory=dict)
    # ReviewStage 预算裁剪结果（ScoreStage 进 report.budget）：
    budget_exhausted: bool = False
    jobs_run: int = 0
    jobs_total: int = 0
    estimated_cost_usd: float = 0.0
    report: Any = None  # ScoreStage: ReviewReport


@runtime_checkable
class Stage(Protocol):
    """Pipeline 步骤协议。实现为带 name 属性 + async process(ctx)->ctx 的任意类。"""

    name: str

    async def process(self, ctx: PipelineContext) -> PipelineContext: ...


class Pipeline:
    """有序 Stage 列表。"""

    def __init__(self, stages: list[Stage] | None = None) -> None:
        self.stages: list[Stage] = list(stages or [])

    def append(self, stage: Stage) -> "Pipeline":
        self.stages.append(stage)
        return self

    def insert(self, stage: Stage, before: str | None = None) -> "Pipeline":
        """在名为 before 的 Stage 前插入（before=None 则末尾）。v2 DebateStage 用。"""
        if before is None:
            self.stages.append(stage)
            return self
        idx = next(
            (i for i, s in enumerate(self.stages) if getattr(s, "name", None) == before),
            len(self.stages),
        )
        self.stages.insert(idx, stage)
        return self

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        for stage in self.stages:
            ctx = await stage.process(ctx)
        return ctx
