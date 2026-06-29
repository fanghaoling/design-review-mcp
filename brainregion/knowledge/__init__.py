"""KnowledgeProvider：项目知识库（历史踩坑案例）检索。"""
from __future__ import annotations

from .base import Case, KnowledgeProvider, constraint_ok, version_matches
from .yaml_provider import YamlKnowledgeProvider, extract_keyword_hits, render_for_prompt

__all__ = [
    "Case",
    "KnowledgeProvider",
    "constraint_ok",
    "version_matches",
    "YamlKnowledgeProvider",
    "extract_keyword_hits",
    "render_for_prompt",
]
