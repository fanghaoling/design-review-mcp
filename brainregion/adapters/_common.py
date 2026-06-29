"""adapter 公共工具：从 CLAUDE.md 提取指定 ## 段落。"""
from __future__ import annotations

import re
from pathlib import Path

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def extract_md_sections(text: str, titles: list[str]) -> str:
    """按任意层级标题(#{1,6})切分，返回指定标题的段（到下一个任意标题为止），按 titles 顺序拼接。

    支持 ## 和 ### 等（CLAUDE.md 的 ECS Patterns 等在 ## Architecture 下的 ### 子段也能提取）。
    """
    lines = text.split("\n")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            current = m.group(2).strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    parts: list[str] = []
    for t in titles:
        body = "\n".join(sections.get(t, [])).strip()
        if body:
            parts.append(f"## {t}\n{body}")
    return "\n\n".join(parts)


def read_claude_md(project_root: str | Path, titles: list[str]) -> str:
    """读项目根 CLAUDE.md 的指定段；文件不存在或解析失败返回空（不抛）。"""
    p = Path(project_root) / "CLAUDE.md"
    if not p.exists():
        return ""
    try:
        return extract_md_sections(p.read_text(encoding="utf-8"), titles)
    except Exception:  # noqa: BLE001
        return ""
