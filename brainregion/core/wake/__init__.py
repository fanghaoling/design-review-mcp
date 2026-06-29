"""Wake gate: region-routing 的 escalate + 假阴性兜底层（sidecar，只读，不调模型）。"""
from __future__ import annotations

from .gate import wake_gate

__all__ = ["wake_gate"]
