"""模型调用错误分类 + 可操作反馈。

把 litellm/Provider 的原始 error 字符串映射成 {type, hint}，让 failed_models 直接告诉
用户该怎么办（余额→充值 / auth→查key / 限流→重试 / 超时→调timeout），而非一长串报错。
"""
from __future__ import annotations

import re

# (正则, 错误类型, 可操作建议)。按顺序匹配，首个命中胜出。
_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"[Ii]nsufficient balance|no resource package|[Pp]lease recharge|余额不足",
        "insufficient_balance",
        "账户余额不足或无资源包，去对应平台充值或领取资源包",
    ),
    (
        r"[Aa]uthentication|invalid api key|incorrect.*key|401|未授权",
        "auth_error",
        "API key 错误或失效，检查 .env 里对应的 *_API_KEY",
    ),
    (
        r"[Rr]ate.?limit|429|throttl|限流",
        "rate_limit",
        "限流，稍后重试或减少并发/降频",
    ),
    (
        r"[Tt]imeout|timed out|ETIMEDOUT",
        "timeout",
        "请求超时，调大 timeout 或减小 max_tokens",
    ),
    (
        r"[Uu]nsupported.?param",
        "unsupported_param",
        "模型不支持该参数（drop_params=True 已自动丢弃，靠 prompt+防御解析）",
    ),
    (
        r"[Mm]odel not found|does not exist|unknown model|404",
        "model_not_found",
        "模型名错误或无权访问，检查 panel 的 model 字符串（如 zai/glm-5.2）",
    ),
    (
        r"[Bb]ad request|400|invalid",
        "bad_request",
        "请求参数有误，检查 prompt/参数",
    ),
    (
        r"[Cc]onnect|ECONN|network|unreachable|连接",
        "network",
        "网络连接失败，检查代理/网络（github 等需走 Clash 7890）",
    ),
]


def classify_error(error_str: str) -> dict:
    """把原始 error 字符串分类成 {type, hint}。未知返回 unknown。"""
    s = error_str or ""
    for pat, etype, hint in _PATTERNS:
        if re.search(pat, s):
            return {"type": etype, "hint": hint}
    return {"type": "unknown", "hint": "未知错误，查看 error 原文或开 litellm debug"}
