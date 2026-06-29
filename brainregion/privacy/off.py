"""OffPolicy：不脱敏（默认）。transform 原样返回，mediate 原样返回。

privacy_policy=None 时引擎直接不传 policy（等价 off）。此类主要给 build_policy 内部
policy="off" 显式场景 + 测试用。
"""
from __future__ import annotations

from .base import TransformResult


class OffPolicy:
    name = "off"

    async def transform(self, document, backend) -> TransformResult:
        return TransformResult(document=document)

    async def mediate(self, findings, original_document, backend) -> list:
        return findings
