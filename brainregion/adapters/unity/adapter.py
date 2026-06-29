"""UnityAdapter：Unity ECS 项目的 ProjectAdapter 实现。

- read_version: 解析 Packages/manifest.json，提取 com.unity.* 包版本（key 去前缀，如 entities/netcode/physics）。
- read_context: 读 CLAUDE.md 的 ECS Patterns/Networking/Major Subsystems 段。
- read_convention: 读 CLAUDE.md 的 Code Conventions/Implementation Rules 段。
- reviewers_dir / knowledge_dir: 包内的 Unity 特定 reviewer 与知识库。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .._common import read_claude_md

logger = logging.getLogger("brainregion.adapter.unity")


class UnityAdapter:
    name = "unity"

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(
            project_root or os.environ.get("UNITY_PROJECT_ROOT", ".")
        )
        self._dir = Path(__file__).resolve().parent

    def read_version(self) -> dict[str, str]:
        manifest = self.project_root / "Packages" / "manifest.json"
        if not manifest.exists():
            logger.warning("Unity manifest 不存在: %s", manifest)
            return {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("manifest 解析失败: %s", e)
            return {}
        out: dict[str, str] = {}
        for full, ver in (data.get("dependencies") or {}).items():
            short = full[len("com.unity."):] if full.startswith("com.unity.") else full
            out[short] = ver
        return out

    def read_context(self) -> str:
        return read_claude_md(
            self.project_root, ["ECS Patterns", "Networking", "Major Subsystems"]
        )

    def read_convention(self) -> str:
        return read_claude_md(self.project_root, ["Code Conventions", "Implementation Rules"])

    def reviewers_dir(self) -> Path:
        return self._dir / "reviewers"

    def knowledge_dir(self) -> Path:
        return self._dir / "knowledge"

    def local_knowledge_dir(self) -> Path:
        """Legacy 项目本地知识库目录（项目特定/敏感案例，gitignore）。"""
        return self.project_root / ".design-review" / "knowledge"

    def local_knowledge_dirs(self) -> list[Path]:
        """项目本地知识库目录；旧目录先加载，新 BrainRegion 目录后加载用于覆盖。"""
        return [
            self.project_root / ".design-review" / "knowledge",
            self.project_root / ".brain-region" / "knowledge",
        ]
