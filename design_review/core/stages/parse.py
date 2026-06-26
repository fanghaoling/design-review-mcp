"""ParseStage：解析 LLM JSON 输出 → Finding，强制 evidence_quote。

Pipeline 第 5 步。提 ```json 块 / 整段 json.loads → 校验 finding schema → 丢弃无
evidence_quote 的（防幻觉）。schema 不符 best-effort 丢弃并记日志。失败模型跳过。
"""
from __future__ import annotations

import json
import logging
import re

from ..pipeline import PipelineContext, Stage
from ..report import Finding
from ..schema import get_schema

logger = logging.getLogger("design_review.stage.parse")

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_schema_cache: dict | None = None


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = get_schema("finding")
    return _schema_cache


def extract_json_object(text: str) -> dict | None:
    """提 JSON 对象，4 级 fallback（越来越激进，挽回国产模型非纯 JSON 输出）：
    1) ```json 块  2) 整段 json.loads  3) 正则最外层 {.*}（跨行，说明文字+JSON）  4) "issues":[...] 数组。
    """
    if not text:
        return None
    # 1) ```json 块
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    # 2) 整段
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    # 3) 正则最外层 {.*}（跨行，捕获"说明文字 + JSON"的情况）
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    return None


_VALID_SEVERITY = {"high", "medium", "low"}


def normalize_finding(f) -> dict | None:
    """补默认 + 校验关键字段。返回补全后的 dict 或 None（丢弃无效）。

    放宽 jsonschema 严校验（best-effort），挽回输出不规范的国产模型 finding；
    但 evidence_quote + dimension + title 仍强制（防幻觉 + 保证可用）。
    """
    if not isinstance(f, dict):
        return None
    out = dict(f)
    out.setdefault("confidence", 0.5)
    out.setdefault("case_ref", None)
    out.setdefault("location", "")
    out.setdefault("suggestion", "")
    if not out.get("evidence_quote") or not out.get("dimension") or not out.get("title"):
        return None
    if out.get("severity") not in _VALID_SEVERITY:
        out["severity"] = "medium"
    try:
        out["confidence"] = float(out.get("confidence", 0.5))
    except Exception:  # noqa: BLE001
        out["confidence"] = 0.5
    return out


class ParseStage:
    name = "parse"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        for item in ctx.responses:
            r = item["response"]
            model = item["model"]
            dim = item["dimension"]
            if not r.ok:
                continue
            obj = extract_json_object(r.content)
            if obj is None:
                logger.warning("模型 %s(%s) 输出无法解析为 JSON", model, dim)
                continue
            issues = obj.get("issues") if isinstance(obj.get("issues"), list) else []
            for f in issues:
                nf = normalize_finding(f)
                if nf is None:
                    logger.info(
                        "丢弃无效 finding（缺 evidence/dimension/title）: %s/%s",
                        model,
                        str(f.get("title", ""))[:40] if isinstance(f, dict) else "?",
                    )
                    continue
                ctx.findings.append(
                    Finding(
                        model=model,
                        dimension=nf.get("dimension", dim),
                        severity=nf["severity"],
                        title=nf["title"],
                        evidence_quote=nf["evidence_quote"],
                        location=nf.get("location", ""),
                        suggestion=nf.get("suggestion", ""),
                        confidence=nf["confidence"],
                        case_ref=nf.get("case_ref"),
                    )
                )
        return ctx
