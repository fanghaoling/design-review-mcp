"""ReviewReport 标准 schema + Finding / CanonicalFinding 数据结构。

ReviewReport 是框架对外的稳定输出契约：所有 ReportRenderer（Markdown/JSON/SARIF）
都基于它渲染。字段设计参考 GPT 第三轮审查建议（Metadata/Findings/Consensus/
Evidence/Risk/KnowledgeHit/Usage/Cost）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    """单个模型单条发现（ParseStage 从 LLM JSON 解析出）。

    evidence_quote 强制非空（parsing 层丢弃空引用）；case_ref 为命中知识库案例 id。
    """

    model: str
    dimension: str
    severity: str  # high | medium | low
    title: str  # 原始措辞（模型自己的描述）
    evidence_quote: str
    location: str  # file:line 或 段落引用
    suggestion: str
    confidence: float  # 模型自报 0~1
    case_ref: str | None = None
    id: str = ""  # v2 评审内稳定 id "{label}-{seq}"，ParseStage 填；Review Memory 标记用
    attachments: list = field(default_factory=list)  # v1.7 FindingAttachment[{source,type,payload}]，immutable 附加
    deduped_ids: list = field(default_factory=list)  # v2 断链点A：被去重 finding 的 id 挂代表上，mark_finding 反查


@dataclass
class CanonicalFinding:
    """归一化后的标准发现（NormalizeStage 产出，ConsensusStage 聚类用）。

    多个语义相同的 Finding 合并成一个 CanonicalFinding。canonical_title 是一句话
    标准标题（LLM 归一），flagged_by 记录哪些模型标了它，source_findings 保留原始
    供溯源与 evidence 选择。
    """

    canonical_title: str
    dimension: str
    severity: str
    evidence_quote: str  # 代表性 evidence（取 source 之一）
    location: str
    suggestion: str
    case_ref: str | None
    flagged_by: list[str] = field(default_factory=list)
    source_findings: list[Finding] = field(default_factory=list)
    # ConsensusStage / ScoreStage 填充：
    bucket: str = ""  # consensus | majority | individual
    calibrated_confidence: float = 0.0


@dataclass
class ReviewReport:
    """审查报告标准 schema（所有 renderer 的输入）。"""

    document_type: str
    adapter: str
    project_version: dict[str, str] = field(default_factory=dict)
    panel: list[str] = field(default_factory=list)
    failed_models: list[dict] = field(default_factory=list)  # [{model, error}]
    retrieved_cases: list[str] = field(default_factory=list)  # case ids
    consensus: list[CanonicalFinding] = field(default_factory=list)
    majority: list[CanonicalFinding] = field(default_factory=list)
    individual: dict[str, list[Finding]] = field(default_factory=dict)  # model -> findings
    knowledge_hit: list[str] = field(default_factory=list)  # 命中的 case ids
    budget: dict = field(default_factory=dict)  # {max_usd, estimated_usd, jobs_run, jobs_total, exhausted}
    usage: dict = field(default_factory=dict)  # {total_tokens, cost_usd}
    summary: str = ""
    risk: dict = field(default_factory=dict)  # {overall_level, top_risks}；v3 细化
    privacy: dict = field(default_factory=dict)  # v1.7 {policy, coverage, missing_topics, trusted}
    context_compression: dict = field(default_factory=dict)  # v1.8 {dim: {mode, original_chars, compressed_chars, ratio}}
    panel_status: dict = field(default_factory=dict)  # {requested, ran, complete}；panel 不完整（裁剪/失败）→ complete=False（ISS-001）

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的 dict（JSONRenderer / MCP 工具返回用）。"""
        import dataclasses

        def _finding(f: Finding) -> dict:
            return dataclasses.asdict(f)

        return {
            "document_type": self.document_type,
            "adapter": self.adapter,
            "project_version": dict(self.project_version),
            "panel": list(self.panel),
            "failed_models": list(self.failed_models),
            "retrieved_cases": list(self.retrieved_cases),
            "consensus": [dataclasses.asdict(c) for c in self.consensus],
            "majority": [dataclasses.asdict(c) for c in self.majority],
            "individual": {k: [_finding(f) for f in v] for k, v in self.individual.items()},
            "knowledge_hit": list(self.knowledge_hit),
            "budget": dict(self.budget),
            "usage": dict(self.usage),
            "summary": self.summary,
            "risk": dict(self.risk),
            "privacy": dict(self.privacy),
            "context_compression": dict(self.context_compression),
            "panel_status": dict(self.panel_status),
        }
