"""Parse and normalize consultant JSON output."""
from __future__ import annotations

import json

from ..stages.parse import extract_json_object
from .report import ConsultAdvice

_LIST_FIELDS = ("likely_causes", "next_experiments", "solution_options", "risks", "recommended_plan")


def _clamp_confidence(value) -> float:
    try:
        conf = float(value)
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, min(1.0, conf))


def _string_list(value, *, max_items: int = 8) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    out: list[str] = []
    for item in values:
        if isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        text = " ".join(text.split())
        if text and text not in out:
            out.append(text[:1200])
        if len(out) >= max_items:
            break
    return out


def parse_advice(content: str, *, model: str, consultant: str, advice_id: str) -> ConsultAdvice | None:
    obj = extract_json_object(content or "")
    if obj is None:
        return None
    summary = str(obj.get("summary") or "").strip()[:1600]
    advice = ConsultAdvice(
        id=advice_id,
        model=model,
        consultant=consultant,
        summary=summary,
        confidence=_clamp_confidence(obj.get("confidence", 0.0)),
    )
    for field in _LIST_FIELDS:
        setattr(advice, field, _string_list(obj.get(field)))
    if not advice.summary:
        first = (
            advice.recommended_plan[:1]
            or advice.next_experiments[:1]
            or advice.solution_options[:1]
            or advice.likely_causes[:1]
        )
        advice.summary = first[0] if first else "模型返回了结构化 JSON，但没有给出摘要。"
    return advice
