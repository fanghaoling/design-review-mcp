"""privacy 模块（v1.7）：隐私/脱敏策略，framework 一级能力。

非 pipeline stage：StrictPolicy 等通过 transform() 在 pipeline 外把原文变换成脱敏摘要
（PromptStage 不知 strict 存在），通过 mediate() 在 Parse 后给对抗 findings 附加 trusted
评估（Finding immutable，只 append attachment）。

为未来扩展（Enterprise/PII/Regex/AST/CompositePolicy）留 Protocol 接口，core/pipeline 不动。
"""
from .base import FindingAttachment, PrivacyPolicy, TransformResult, build_policy
from .off import OffPolicy
from .strict import StrictPolicy

__all__ = [
    "PrivacyPolicy",
    "TransformResult",
    "FindingAttachment",
    "OffPolicy",
    "StrictPolicy",
    "build_policy",
]
