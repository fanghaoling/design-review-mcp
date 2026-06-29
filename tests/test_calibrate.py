"""judge 校准测试（mock backend，不联网）。

覆盖：load_gold（seed 文件）、_to_report、calibrate agreement（正向 100% / 反向 0%）、
summarize（per_failure_mode + wrong_pairs）、judge_task 透传 task_context。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from brainregion.eval import calibrate as cal
from brainregion.eval import judge
from brainregion.providers.base import ModelResponse

_SEED = Path(__file__).resolve().parent.parent / "brainregion" / "eval" / "gold" / "review_calibration.yaml"


# ---------- load_gold / _to_report ----------

def test_load_gold_seed():
    pairs = cal.load_gold(str(_SEED))
    assert len(pairs) == 10
    fms = {p["failure_mode"] for p in pairs}
    assert fms == {"missing_critical", "harmful_advice", "vague_no_evidence", "irrelevant_noise", "redundant"}
    # report dict 结构对（desensitize 读 canonical_title）
    assert pairs[0]["good_report"]["consensus"][0]["canonical_title"]


def test_to_report_shape():
    rep = cal._to_report([{"title": "t", "severity": "high", "evidence": "e", "suggestion": "s"}])
    assert rep["consensus"][0]["canonical_title"] == "t"
    assert rep["majority"] == [] and rep["individual"] == {}


# ---------- 内容感知 mock：按 block 里 MARKER-GOOD 判 ----------

def _label_blocks(user: str) -> dict:
    blocks: dict[str, str] = {}
    for seg in user.split("=== 输出 ")[1:]:
        if seg:
            blocks[seg[0]] = seg  # seg[0] = "X"/"Y"
    return blocks


class _MarkerBackend:
    """按 user prompt 里哪个 label 含 MARKER-GOOD 打分；good_high=True 时好的得高分。"""

    def __init__(self, good_high: bool = True):
        self.good_high = good_high

    async def complete(self, **kw):
        blocks = _label_blocks(kw.get("user", ""))
        scores: dict = {}
        for label, blk in blocks.items():
            is_good = "MARKER-GOOD" in blk
            if self.good_high:
                base = 5 if is_good else 2
            else:
                base = 2 if is_good else 5  # 反向：好的反而低
            scores[label] = {"overall": base, "useful": base}
        return ModelResponse(model="mock-judge", content=json.dumps(scores))


def _inline_pairs():
    """2 对内联 gold（good 含 MARKER-GOOD），与 seed 解耦。"""
    good_rep = cal._to_report([{"title": "MARKER-GOOD 抓到关键问题", "severity": "high", "evidence": "line", "suggestion": "修"}])
    bad_rep = cal._to_report([{"title": "无关紧要", "severity": "low", "evidence": "无", "suggestion": "加注释"}])
    return [
        {"id": "t1", "failure_mode": "missing_critical", "task": "审登录接口", "good_report": good_rep, "bad_report": bad_rep, "note": ""},
        {"id": "t2", "failure_mode": "harmful_advice", "task": "审密钥存储", "good_report": good_rep, "bad_report": bad_rep, "note": ""},
    ]


def test_calibrate_perfect_agreement():
    je = {"label": "j", "model": "mock-judge", "endpoint_id": None}
    rows = asyncio.run(cal.calibrate(_inline_pairs(), _MarkerBackend(good_high=True), [je], "rubric", "rh", "run"))
    assert all(r["agreed"] for r in rows)  # good 永远 > bad
    s = cal.summarize(rows, threshold=0.7)
    assert s["agreement_rate"] == 1.0 and s["calibrated"] is True
    assert s["wrong_pairs"] == []


def test_calibrate_zero_agreement():
    je = {"label": "j", "model": "mock-judge", "endpoint_id": None}
    rows = asyncio.run(cal.calibrate(_inline_pairs(), _MarkerBackend(good_high=False), [je], "rubric", "rh", "run"))
    assert all(not r["agreed"] for r in rows)  # 反向：good 永远 < bad
    s = cal.summarize(rows, threshold=0.7)
    assert s["agreement_rate"] == 0.0 and s["calibrated"] is False
    assert len(s["wrong_pairs"]) == len(rows)


def test_summarize_per_failure_mode_and_metric():
    rows = [
        {"pair": "p1", "failure_mode": "missing_critical", "judge": "j", "metric": "overall", "good": 5, "bad": 2, "agreed": True},
        {"pair": "p1", "failure_mode": "missing_critical", "judge": "j", "metric": "useful", "good": 2, "bad": 5, "agreed": False},
        {"pair": "p2", "failure_mode": "harmful_advice", "judge": "j", "metric": "overall", "good": 5, "bad": 2, "agreed": True},
    ]
    s = cal.summarize(rows, threshold=0.7)
    assert s["agreement_rate"] == round(2 / 3, 3)
    assert s["per_failure_mode"]["missing_critical"] == 0.5
    assert s["per_failure_mode"]["harmful_advice"] == 1.0
    assert s["per_metric"]["overall"] == 1.0
    assert s["per_metric"]["useful"] == 0.0
    assert any("useful" in w for w in s["wrong_pairs"])


# ---------- judge_task 透传 task_context ----------

class _CaptureBackend:
    def __init__(self):
        self.last_user = ""

    async def complete(self, **kw):
        self.last_user = kw.get("user", "")
        return ModelResponse(model="m", content='{"X": {"overall": 3, "useful": 1}, "Y": {"overall": 3, "useful": 1}}')


def test_judge_task_passes_task_context():
    cap = _CaptureBackend()
    je = {"label": "j", "model": "m", "endpoint_id": None}
    outputs = {"a": '{"consensus":[]}', "b": '{"consensus":[]}'}
    asyncio.run(judge.judge_task(cap, je, "r", "h", "run", "tid", outputs, task_context="TASK-MARKER-123"))
    assert "TASK-MARKER-123" in cap.last_user  # task_context 进了 prompt
