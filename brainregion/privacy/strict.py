"""StrictPolicy：可信中间 AI 脱敏 + 中介（v1.7 严格模式）。

transform：trusted 看完整 document+context → 脱敏摘要 + coverage/missing_topics/redacted_items。
  coverage < min_coverage 或调用失败/空 → raise（**绝不静默回退明文**，否则全文泄露给对抗）。
mediate：trusted 看对抗 findings + 原文 → 逐条评估 → append FindingAttachment（不改原字段）。
  失败 → 全标 unconfirmed 不终止（审查仍产出）。

trusted 输出走 JSON schema（prompt 贴 schema + extract_json_object 防御解析），不靠纯 prompt。
"""
from __future__ import annotations

import json
import logging

from ..core.stages.parse import extract_json_object
from .base import FindingAttachment, TransformResult

logger = logging.getLogger("brainregion.privacy.strict")

_TRANSFORM_SCHEMA = json.dumps(
    {
        "summary": "脱敏方案摘要（保留所有技术决策点与逻辑结构，去掉项目专有名/内部架构/敏感数值/真实路径）",
        "coverage": "0~1 浮点，摘要对原文关键信息的覆盖度自评",
        "missing_topics": ["摘要未覆盖的主题（如 Deployment/Rollback）"],
        "redacted_items": ["被脱敏的具体项"],
    },
    ensure_ascii=False,
)

_MEDIATE_SCHEMA = json.dumps(
    [
        {
            "id": "对抗 finding 的 index（整数）",
            "evidence": "引用原文的 evidence 片段（找不到则空串）",
            "reason": "评估理由",
            "verdict": "confirmed | unconfirmed | rejected",
        }
    ],
    ensure_ascii=False,
)


def _extract_mediate_array(text: str):
    """解析 trusted mediate 输出的 JSON 数组。extract_json_object 只解析 dict，list 用这个。"""
    import re

    text = text or ""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    cand = m.group(1) if m else text
    lb = cand.find("[")
    if lb < 0:
        return None
    try:
        parsed = json.loads(cand[lb:])
        return parsed if isinstance(parsed, list) else None
    except Exception:  # noqa: BLE001
        return None


class StrictPolicy:
    name = "strict"

    def __init__(self, *, trusted: dict, min_coverage: float = 0.5) -> None:
        self.trusted = trusted  # PanelEntry{label, model, endpoint_id}
        self.min_coverage = min_coverage

    async def transform(self, document, backend) -> TransformResult:
        from ..core.document import ReviewDocument

        system = (
            "你是隐私脱敏中间人。把下方【完整方案+代码】脱敏成对抗审查用的摘要。"
            "要求：去掉项目专有名/内部架构名/敏感数值/真实路径，但**保留所有技术决策点与逻辑结构**"
            "（否则下游对抗审查会漏 bug）；输出 coverage（摘要对原文关键信息覆盖度自评 0~1）、"
            "missing_topics（没能覆盖的主题）、redacted_items（被脱敏的具体项）。严格 JSON。"
        )
        user = (
            f"【完整方案】\n{document.content or ''}\n\n"
            f"【代码文件】\n{json.dumps(document.files or {}, ensure_ascii=False)}\n\n"
            f"输出 JSON schema：\n```json\n{_TRANSFORM_SCHEMA}\n```"
        )
        resp = await backend.complete(
            model=self.trusted["model"],
            system=system,
            user=user,
            temperature=0.2,
            max_tokens=4096,
            endpoint_id=self.trusted.get("endpoint_id"),
        )
        # Sanitize 失败 = 终止不回退明文（防全文泄露给对抗）
        if not resp.ok:
            raise RuntimeError(f"StrictPolicy.transform trusted 调用失败（不回退明文）：{resp.error}")
        obj = extract_json_object(resp.content)
        if not obj:
            raise RuntimeError("StrictPolicy.transform trusted 输出无法解析为 JSON（不回退明文）")
        summary = obj.get("summary") or ""
        if not summary:
            raise RuntimeError("StrictPolicy.transform trusted 未输出 summary（不回退明文）")
        try:
            coverage = float(obj.get("coverage", 0.0) or 0.0)
        except (TypeError, ValueError):
            coverage = 0.0
        if coverage < self.min_coverage:
            raise RuntimeError(
                f"StrictPolicy.transform coverage={coverage:.2f} < min_coverage={self.min_coverage}"
                f"（摘要质量不足，终止防对抗基于垃圾摘要审查；missing={obj.get('missing_topics')}）"
            )
        logger.info("StrictPolicy.transform coverage=%.2f missing=%s", coverage, obj.get("missing_topics"))
        return TransformResult(
            document=ReviewDocument(type=document.type, content=summary, files=None),
            coverage=coverage,
            missing_topics=obj.get("missing_topics") or [],
            redacted_items=obj.get("redacted_items") or [],
        )

    async def mediate(self, findings: list, original_document, backend) -> list:
        if not findings:
            return findings
        # 对抗 findings 精简给 trusted（不带原文，trusted 已有原文）
        items = [
            {
                "id": i,
                "title": f.title,
                "dimension": f.dimension,
                "severity": f.severity,
                "evidence_quote": f.evidence_quote,
                "suggestion": f.suggestion,
            }
            for i, f in enumerate(findings)
        ]
        system = (
            "你是隐私中介。对抗模型基于【脱敏摘要】给出了下方 findings。请结合【完整原文】逐条评估："
            "confirmed（原文确有问题，补原文 evidence）/ unconfirmed（原文找不到对应 evidence，"
            "可能摘要误导，保留但标记）/ rejected（对抗误报）。**不丢弃任何 finding**。严格 JSON 数组。"
        )
        user = (
            f"【对抗 findings】\n```json\n{json.dumps(items, ensure_ascii=False)}\n```\n\n"
            f"【完整原文】\n{original_document.content or ''}\n\n"
            f"输出 JSON schema：\n```json\n{_MEDIATE_SCHEMA}\n```"
        )
        resp = await backend.complete(
            model=self.trusted["model"],
            system=system,
            user=user,
            temperature=0.2,
            max_tokens=4096,
            endpoint_id=self.trusted.get("endpoint_id"),
        )
        assessments: dict = {}
        if resp.ok:
            arr = _extract_mediate_array(resp.content)
            if isinstance(arr, list):
                for a in arr:
                    if isinstance(a, dict) and isinstance(a.get("id"), int):
                        verdict = a.get("verdict")
                        assessments[a["id"]] = {
                            "evidence": a.get("evidence") or "",
                            "reason": a.get("reason") or "",
                            "verdict": verdict if verdict in ("confirmed", "unconfirmed", "rejected") else "unconfirmed",
                        }
        else:
            logger.warning("StrictPolicy.mediate trusted 失败，全标 unconfirmed：%s", resp.error)
        # 附加 attachment，不改原字段；未评估/失败的标 unconfirmed（不丢，降权留痕）
        for i, f in enumerate(findings):
            a = assessments.get(i) or {
                "evidence": "",
                "reason": "trusted 评估缺失（调用失败或未返回该 id）",
                "verdict": "unconfirmed",
            }
            f.attachments.append(
                FindingAttachment(source=self.trusted["label"], type="mediation", payload=a)
            )
        return findings
