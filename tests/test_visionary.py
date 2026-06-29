"""v1.8 发散/可行性维度：_compress_document + PromptStage 按 context_modes + reviewer 加载 + parse 失败可见。

不调网。覆盖：
- _compress_document（full/compressed 去 code 保 headings/minimal/未知 fallback/下限保护）
- PromptStage：context_modes per-dimension 压缩 + 收 context_compression
- load_reviewer：visionary（temp 0.6/无 context_mode/不 inherits base）、feasibility（inherits base/temp 0.3/verdict）
- parse 失败 → failed_models(parse_error)
"""
from __future__ import annotations

import asyncio

from brainregion.core.document import ReviewDocument
from brainregion.core.pipeline import PipelineContext
from brainregion.core.stages import CORE_REVIEWERS_DIR
from brainregion.core.stages.parse import ParseStage
from brainregion.core.stages.prompt import PromptStage, _compress_document
from brainregion.core.stages.score import ScoreStage
from brainregion.core.reviewers.loader import load_reviewer
from brainregion.providers.base import ModelResponse


class _MockAdapter:
    name = "mock"

    def reviewers_dir(self):
        return CORE_REVIEWERS_DIR


# ===== _compress_document =====

def test_compress_full():
    c = _compress_document("hello world", "full")
    assert c.content == "hello world" and c.mode == "full"


def test_compress_compressed_keeps_headings_strips_code():
    doc = "# 标题\n\n```python\nprint('secret')\n```\n\n长段落\n" + "x" * 300
    c = _compress_document(doc, "compressed")
    assert "# 标题" in c.content  # 保留 heading
    assert "secret" not in c.content  # 去 fenced code
    assert c.compressed_chars < c.original_chars  # 压缩了


def test_compress_minimal():
    doc = "# 标题\n\n第一段内容。\n\n第二段不该出现。"
    c = _compress_document(doc, "minimal", min_chars=1)
    assert "标题" in c.content  # heading 保留
    assert "第一段" in c.content  # 首段实质内容（跳过 heading 段）
    assert "第二段" not in c.content  # minimal 只首段+headings


def test_compress_unknown_mode_fallback_full():
    c = _compress_document("hello", "weird")
    assert c.mode == "full" and c.content == "hello"


def test_compress_lower_bound_fallback_full():
    """code 主体去 code 后空 → fallback full（防幻觉发散）。"""
    doc = "```\ncode only\n```"
    c = _compress_document(doc, "compressed", min_chars=50)
    assert c.mode == "full"


def test_compress_metadata():
    c = _compress_document("# h\n\n正文", "compressed", min_chars=1)
    assert c.original_chars > 0 and c.compressed_chars > 0


# ===== PromptStage 按 context_modes（per-dimension）=====

def test_prompt_stage_compresses_per_dimension():
    ctx = PipelineContext(
        document=ReviewDocument.markdown("# 方案\n\n```py\nsecret_code()\n```\n\n正文内容足够长"),
        adapter=_MockAdapter(), backend=None, knowledge=None,
    )
    ctx.context_modes = {"visionary": "compressed", "planner": "full"}
    ctx.panel = [{"label": "gpt-4o", "model": "gpt-4o", "endpoint_id": None}]
    ctx.min_compressed_chars = 1  # 小到不触发 fallback，测真实压缩
    stage = PromptStage(CORE_REVIEWERS_DIR, default_dimensions=["visionary"])
    asyncio.run(stage.process(ctx))
    visionary_job = next(j for j in ctx.prompts if j["dimension"] == "visionary")
    assert "secret_code" not in visionary_job["user"]  # compressed 去 code
    assert "visionary" in ctx.context_compression
    assert ctx.context_compression["visionary"]["mode"] == "compressed"
    assert "ratio" in ctx.context_compression["visionary"]


# ===== reviewer 加载 =====

def test_load_visionary():
    r = load_reviewer("visionary", CORE_REVIEWERS_DIR)
    assert r["temperature"] == 0.6
    assert "context_mode" not in r  # v1.8 context_mode 在 config 不在 reviewer
    assert "发散" in r["system_prompt"]


def test_load_feasibility_inherits_base():
    r = load_reviewer("feasibility", CORE_REVIEWERS_DIR)
    assert r["temperature"] == 0.3
    assert "evidence" in r["system_prompt"].lower()  # inherits base 铁律
    assert "GO" in r["system_prompt"]  # verdict


# ===== parse 失败可见性（→ failed_models parse_error）=====

def test_parse_failed_to_failed_models():
    ctx = PipelineContext(
        document=ReviewDocument.markdown("x"), adapter=_MockAdapter(), backend=None, knowledge=None,
    )
    ctx.panel = []
    ctx.responses = [
        {"model": "gpt-4o", "dimension": "planner",
         "response": ModelResponse(model="gpt-4o", content="这根本不是 JSON")},
    ]
    ctx.retrieved_cases = []
    asyncio.run(ParseStage().process(ctx))
    assert "gpt-4o" in ctx.parse_failed
    asyncio.run(ScoreStage().process(ctx))
    assert any(f.get("type") == "parse_error" for f in ctx.report.failed_models)
