"""KnowledgeProvider 协议 + Case + 版本匹配。

知识库 = 项目历史踩坑的结构化案例。retrieve 按关键词命中 + 项目版本过滤；
render_for_prompt 压缩成 prompt 友好格式（只 title/bad/rec/category + id，丢 source/history）。

v1 用 YamlKnowledgeProvider（关键词 retrieve）；v3 可换 VectorKnowledgeProvider（embedding），
retrieve 接口不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Case:
    """一条历史踩坑案例。"""

    id: str
    title: str
    triggers: list[str] = field(default_factory=list)
    anti_triggers: list[str] = field(default_factory=list)  # 任一命中→不召回（跨域同词降噪，ISS-006）
    category: str = ""
    bad_pattern: str = ""
    recommended_pattern: str = ""
    version: dict[str, str] = field(default_factory=dict)  # {entities: ">=1.4,<1.5"}
    source: str = ""  # 给人追溯（如 MEMORY.md#xxx），不进 prompt


@runtime_checkable
class KnowledgeProvider(Protocol):
    def retrieve(
        self, text: str, project_version: dict[str, str] | None = None, top_k: int = 5
    ) -> list[Case]: ...

    def list_cases(self) -> list[Case]: ...

    def add_case(self, case: Case) -> None: ...


_VER_RE = re.compile(r"\d+")
_CONSTRAINT_RE = re.compile(r"(>=|<=|>|<|=)?\s*([\d.]+)")


def _parse_ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in _VER_RE.findall(v or ""))


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def constraint_ok(constraint: str, actual: str) -> bool:
    """单个约束（如 ">=1.4,<1.5"）对实际版本（如 "1.4.6"）是否满足。空/* 视为通过。"""
    if not constraint or constraint.strip() == "*":
        return True
    av = _parse_ver(actual)
    for part in constraint.split(","):
        part = part.strip()
        if not part:
            continue
        m = _CONSTRAINT_RE.match(part)
        if not m:
            continue
        op = m.group(1) or "="
        bv = _parse_ver(m.group(2))
        a, b = _pad(av, bv)
        if op == ">=" and not a >= b:
            return False
        if op == "<=" and not a <= b:
            return False
        if op == ">" and not a > b:
            return False
        if op == "<" and not a < b:
            return False
        if op == "=" and not a == b:
            return False
    return True


def version_matches(case_version: dict[str, str], project_version: dict[str, str]) -> bool:
    """案例版本约束是否匹配项目当前版本。无约束=通用=True；项目缺某包版本=不过滤（保守不排除）。"""
    if not case_version:
        return True
    for pkg, c in case_version.items():
        if not c or c.strip() == "*":
            continue
        actual = project_version.get(pkg)
        if actual is None:
            continue
        if not constraint_ok(c, actual):
            return False
    return True
