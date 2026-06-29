"""ProjectAdapter 协议：项目特定逻辑的接入点。

core 项目无关；所有项目特定（版本读取、领域 context、约定、reviewer、knowledge）进 adapter。
v1 内置 UnityAdapter / GenericAdapter；日后加 RustAdapter / CppAdapter / WebAdapter 只加 adapter 包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProjectAdapter(Protocol):
    """项目适配器协议。"""

    name: str

    def read_context(self) -> str:
        """项目领域知识（如 CLAUDE.md 的架构段 + 历史踩坑摘要）。"""
        ...

    def read_version(self) -> dict[str, str]:
        """项目关键依赖版本（如 {entities: "1.4.6", netcode: "1.10.0"}），供知识库版本过滤。"""
        ...

    def read_convention(self) -> str:
        """编码约定摘要。"""
        ...

    def reviewers_dir(self) -> Path:
        """该项目特定 reviewer 角色 yaml 目录（与 core 通用 reviewer 合并）。"""
        ...

    def knowledge_dir(self) -> Path:
        """该项目知识库（历史踩坑案例 yaml）目录。"""
        ...

    def local_knowledge_dir(self) -> Path:
        """项目本地知识库目录（项目特定/敏感案例，gitignore），叠加在 knowledge_dir 通用库上。

        用于放不适合随开源 framework 公开的内容（如自家网络同步设计）。可不存在。
        """
        ...

    def local_knowledge_dirs(self) -> list[Path]:
        """项目本地知识库目录列表；BrainRegion 新目录可叠加 legacy 目录。"""
        ...
