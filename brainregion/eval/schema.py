"""评测 harness 数据 schema（v5.5 闸门的"尺子"数据结构）。

对齐 docs/eval_harness.zh-CN.md §3。MVP 只填必要字段，region-routing 相关字段（gold_regions /
wake trace 等）留空，前向兼容——等 v5 wake gate 长出来再填，不改 schema。

设计要点（吸收 GPT 反馈）：
- BlindJudgement 是 per-judge（judge_id/judge_model/score/reason），结构支持 N judge ensemble，
  MVP 只跑 1 个 judge。
- EvalLedgerEntry 带 metadata hash（knowledge/reviewer/defaults/rubric）+ 成本拆分，保证可追溯。
- scores 是自由 dict，预留 precision/recall/novelty/coverage/conflict/redundancy（rubric 可填可不填）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalTask:
    """一个冻结的 hold-out 任务。MVP task_type 只支持 review。"""

    id: str
    task_type: str = "review"
    difficulty: str = ""  # simple | complex | cross_domain（分层用）
    input: dict = field(default_factory=dict)
    # input = {content, document_type, files, panel, dimensions, extra_context}
    gold_regions: list[str] = field(default_factory=list)  # routing eval：本任务【应唤醒】的 region（wake 精度 ground truth）
    notes: str = ""
    frozen: bool = True


@dataclass
class VariantSpec:
    """一个评测变体。bootstrap 三变体：retrieve_off / retrieve_on / retrieve_garbage。"""

    name: str
    retrieve_top_k: int
    garbage: bool = False  # True → 注入 GarbageKnowledgeProvider（负对照）


@dataclass
class EvalCaseRecord:
    """单任务 × 单变体 的产出（append-only）。"""

    run_id: str
    task_id: str
    variant: str
    report_summary: dict = field(default_factory=dict)
    # {consensus, majority, individual, failed, panel_status, risk_level}
    retrieved_case_ids: list = field(default_factory=list)
    cost: dict = field(default_factory=dict)
    # {inference_usd, estimated_usd, total_tokens}（对账 ISS-003）
    latency_ms: float = 0.0
    outputs_json: str = ""  # 完整 report dict 的 JSON（存库，judge 读它脱敏后打分）
    error: str = ""


@dataclass
class BlindJudgement:
    """单任务 × 单 judge × 单变体 的盲评（per-judge，多 judge-ready）。"""

    run_id: str
    task_id: str
    judge_id: str
    judge_model: str
    rubric_hash: str
    variant: str  # unshuffle 后还原的真实变体名
    blind: bool = True
    scores: dict = field(default_factory=dict)
    # {useful, correct, harmful, missed_critical, overall} + 预留 precision/recall/...
    reason: str = ""
    judge_cost_usd: float = 0.0


@dataclass
class EvalLedgerEntry:
    """一次完整 run 的索引行（活资产，可 SELECT 聚合）。"""

    run_id: str
    date: str
    git_sha: str
    variants: list = field(default_factory=list)
    judge_models: list = field(default_factory=list)
    rubric_hash: str = ""
    knowledge_hash: str = ""
    reviewer_hash: str = ""
    defaults_hash: str = ""
    n_tasks: int = 0
    summary: dict = field(default_factory=dict)
    # {per_variant: {cost_per_useful_advice, useful_advice_rate, latency_p50, latency_p95}, sanity: [...]}
