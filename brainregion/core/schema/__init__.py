"""JSON schema 访问（随包发布的 finding / review-report schema）。

- finding.schema.json：约束单条 LLM 发现的输出（强制 evidence_quote），PromptStage 贴给模型、
  ParseStage 用 jsonschema 校验。
- review-report.schema.json：最终报告契约。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_SCHEMA_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def get_schema(name: str) -> dict:
    """按名加载 schema json（name 不含扩展名，如 'finding'）。"""
    path = _SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise KeyError(f"未知 schema: {name}；可用: {list_schemas()}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_schemas() -> list[str]:
    """列出可用 schema 名（不含 .schema.json 后缀）。"""
    return sorted(
        p.name.removesuffix(".schema.json") for p in _SCHEMA_DIR.glob("*.schema.json")
    )
