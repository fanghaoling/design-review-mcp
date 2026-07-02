"""Programmatic advice diagnostic（Phase 2A.5 非冗余 memory 研究实验）。

纯函数、不调模型。**仅作 diagnostic**（GPT：关键词钝，不进 gate 判定）：
- check_advice_signal：constraint_violation（是否触 must_not 约束）/ applies_context（是否含 must_any）。
- detect_memory_cite：数引用 memory 的短语（**debug，非 memory-use 证据**——模型会用 memory 但不写引用词）。

「memory 是否真被用」靠 4 臂里的 IRRELEVANT 对照判，不靠 cite。
"""
from __future__ import annotations

import json

from .judge import desensitize_advice

# 引用 memory 的典型短语（中/英）——仅 debug 计数。
_CITE_PHRASES = (
    "根据项目", "之前试过", "我们以前", "由于项目", "历史经验", "之前失败", "项目约定",
    "已知", "踩过", "项目里", "项目之前", "previously tried", "project's", "known constraint",
    "we tried", "we previously", "historically", "past attempt", "our project",
)


def _advice_text(report_dict: dict) -> str:
    """desensitize_advice → 拼成可关键词检索的文本（调用方负责 lower）。"""
    parts: list[str] = []
    for a in desensitize_advice(report_dict):
        for v in (a.get("summary"), a.get("likely_causes"), a.get("next_experiments"),
                  a.get("solution_options"), a.get("risks"), a.get("recommended_plan")):
            if v:
                parts.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return " ".join(parts)


def check_advice_signal(report_dict: dict, gold_check: dict) -> dict:
    """关键词 diagnostic：constraint_violation（must_not 命中任一=1）、applies_context（must_any 命中=1）。

    gold_check: {must_not_contain_any:[...], must_contain_any:[...]}，大小写不敏感。
    无 gold_check → 全 0。纯 diagnostic（不进 GO/NO_GO）。
    """
    if not gold_check:
        return {"constraint_violation": 0, "applies_context": 0, "hits": {}}
    t = _advice_text(report_dict).lower()
    must_not = [str(k).lower() for k in (gold_check.get("must_not_contain_any") or [])]
    must_any = [str(k).lower() for k in (gold_check.get("must_contain_any") or [])]
    not_hits = [k for k in must_not if k and k in t]
    any_hits = [k for k in must_any if k and k in t]
    return {
        "constraint_violation": 1 if not_hits else 0,
        "applies_context": 1 if (not must_any or any_hits) else 0,
        "hits": {"must_not_contain_any": not_hits, "must_contain_any": any_hits},
    }


def detect_memory_cite(report_dict: dict) -> dict:
    """数引用 memory 的短语（debug，非 memory-use 证据）。"""
    t = _advice_text(report_dict).lower()
    return {"cite_count": sum(1 for p in _CITE_PHRASES if p.lower() in t)}
