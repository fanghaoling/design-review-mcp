"""v5.5 评测 harness（bootstrap 尺子）。

规约见 docs/eval_harness.zh-CN.md。MVP：三变体（retrieve off/on/garbage）+ 盲评 + SQLite ledger +
sanity（含负对照），用于在量 region routing 前先把尺子验证准。
"""
from __future__ import annotations

from .runner import DEFAULT_VARIANTS, build_engines, run_eval
from .schema import (
    BlindJudgement,
    EvalCaseRecord,
    EvalLedgerEntry,
    EvalTask,
    VariantSpec,
)

__all__ = [
    "DEFAULT_VARIANTS",
    "build_engines",
    "run_eval",
    "BlindJudgement",
    "EvalCaseRecord",
    "EvalLedgerEntry",
    "EvalTask",
    "VariantSpec",
]
