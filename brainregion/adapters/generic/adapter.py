"""GenericAdapter：通用 ProjectAdapter，不绑定特定技术栈。

任何项目开箱基础可用：尝试读 CLAUDE.md 常见段（不强求存在），无项目特定 reviewer/knowledge
（回退到 core 通用 reviewer + 空知识库）。日后有项目特定需求时换专用 adapter。
"""
from __future__ import annotations

import os
from pathlib import Path

from .._common import read_claude_md


class GenericAdapter:
    name = "generic"

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(
            project_root or os.environ.get("UNITY_PROJECT_ROOT", ".")
        )
        self._dir = Path(__file__).resolve().parent

    def read_version(self) -> dict[str, str]:
        return {}

    def read_context(self) -> str:
        return read_claude_md(
            self.project_root, ["Architecture", "Overview", "Project Overview"]
        )

    def read_convention(self) -> str:
        return read_claude_md(self.project_root, ["Code Conventions", "Conventions"])

    def reviewers_dir(self) -> Path:
        return self._dir / "reviewers"  # 可能不存在 → 回退 core 通用

    def knowledge_dir(self) -> Path:
        return self._dir / "knowledge"  # 可能不存在 → 空知识库

    def local_knowledge_dir(self) -> Path:
        """Legacy 项目本地知识库目录（项目特定案例，gitignore）。"""
        return self.project_root / ".design-review" / "knowledge"

    def local_knowledge_dirs(self) -> list[Path]:
        """项目本地知识库目录；旧目录先加载，新 BrainRegion 目录后加载用于覆盖。"""
        return [
            self.project_root / ".design-review" / "knowledge",
            self.project_root / ".brain-region" / "knowledge",
        ]
