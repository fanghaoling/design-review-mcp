"""GarbageKnowledgeProvider：负对照用——retrieve 忽略 query/version，返回随机无关 case。

bootstrap 的 retrieve_garbage 变体靠它：理论judge 分数应 ≤ retrieve_off（乱引用不该帮上忙）；
若不下降 → judge 分不清"真相关 vs 乱引用"，尺子失效（sanity 报错）。比人工挑 fixture 更硬地
防过拟合/防偏好偏置。

包一个真 provider：list_cases 透传（metadata hash 仍按真实知识算），retrieve 返随机采样。
按 query 内容做确定性 seed（同任务→同堆垃圾，可复现），跨进程稳定（用 sha256 而非 hash()）。
"""
from __future__ import annotations

import hashlib
import random


class GarbageKnowledgeProvider:
    def __init__(self, real) -> None:
        self.real = real

    def list_cases(self):
        return self.real.list_cases()

    def retrieve(self, text: str, project_version: dict | None = None, top_k: int = 5):
        cases = self.real.list_cases()
        if not cases:
            return []
        # 同任务确定性 seed（sha256 稳定，不受 PYTHONHASHSEED 影响）
        seed = int(hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        k = min(int(top_k), len(cases))
        return rng.sample(cases, k)
