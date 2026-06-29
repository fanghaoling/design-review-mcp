"""PrivacyPolicy 协议 + TransformResult/FindingAttachment 数据结构 + build_policy 工厂。

设计要点（GPT 三轮审查）：
- transform 返回 TransformResult（结构化对象，metadata 可扩展），不是元组；
- Finding immutable：trusted 只 append FindingAttachment，不改原字段（v2 Judge/Memory 复用）；
- build_policy 接收**已解析的 trusted PanelEntry**（由 server 层 _normalize_one 解析），
  避免 privacy 反向 import server 造成循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class TransformResult:
    """policy.transform 的返回（结构化，可扩展）。

    document 是 effective（脱敏摘要 or 原文）；coverage/missing_topics/redacted_items 是
    质量与脱敏元信息；metadata 留给未来 policy 放任意数据（如 RegexPolicy 的命中规则）。
    """

    document: Any  # ReviewDocument
    coverage: float = 1.0
    missing_topics: list = field(default_factory=list)
    redacted_items: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class FindingAttachment:
    """Finding 的附加信息（v1.7 immutable 核心）。

    source=产生者（trusted label / panel label / 未来 judge/human/static-analyzer）；
    type=类别（mediation/evidence/reason/...）；payload=内容。Finding.attachments 收集所有
    附加，原 panel 字段不变，溯源清晰，Finding 不随新角色膨胀。
    """

    source: str
    type: str
    payload: dict


@runtime_checkable
class PrivacyPolicy(Protocol):
    """隐私策略协议。off=None（引擎按无 policy 处理）；strict=StrictPolicy。"""

    name: str

    async def transform(self, document: Any, backend: Any) -> TransformResult:
        """pipeline 外：原文 → effective 文档（脱敏摘要）+ 质量元信息。"""
        ...

    async def mediate(
        self, findings: list, original_document: Any, backend: Any
    ) -> list:
        """Parse 后：对抗 findings（基于摘要）+ 原文 → 给每条 finding 附加 trusted attachment。"""
        ...


def build_policy(cfg: dict | None, trusted_entry: dict | None = None):
    """config privacy_policy 块 + 已解析的 trusted PanelEntry -> Policy 实例。

    None/off -> None（引擎按无 policy 处理，行为同 v1.6）。
    strict -> StrictPolicy（需要 trusted_entry；server 层先 _normalize_one 解析）。
    """
    if not cfg:
        return None
    name = (cfg.get("policy") or "off") if isinstance(cfg, dict) else "off"
    if name == "off":
        return None
    if name == "strict":
        from .strict import StrictPolicy

        if not trusted_entry:
            raise ValueError(
                "privacy_policy.policy=strict 必须配 trusted（{endpoint, model, label}）"
            )
        min_coverage = float(cfg.get("min_coverage", 0.5)) if isinstance(cfg, dict) else 0.5
        return StrictPolicy(trusted=trusted_entry, min_coverage=min_coverage)
    raise ValueError(f"未知 privacy policy: {name!r}（off | strict）")
