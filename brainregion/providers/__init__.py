"""ModelBackend：调用层实现协议 + 默认 LiteLLMBackend。"""
from __future__ import annotations

from .base import ModelBackend, ModelResponse
from .litellm import LiteLLMBackend

__all__ = ["ModelBackend", "ModelResponse", "LiteLLMBackend"]
