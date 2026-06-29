"""ReviewDocument：被审查文档的抽象。

document_type 决定 PromptStage 用哪个模板。v1 内置 markdown（设计/方案）与 code
（代码实现）；adr/rfc/config 为预留类型，复用 markdown 模板即可，日后按需特化。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DocumentType = Literal["markdown", "code", "adr", "rfc", "config"]


@dataclass
class ReviewDocument:
    """一份待审查文档。

    Attributes:
        type: 文档类型，影响 prompt 模板。
        content: 文档正文（markdown/adr/rfc/config 用）。
        files: 代码文件映射 {路径: 源码}（code 模式用）。
    """

    type: DocumentType
    content: str = ""
    files: dict[str, str] | None = None

    @classmethod
    def markdown(cls, content: str) -> "ReviewDocument":
        return cls(type="markdown", content=content)

    @classmethod
    def code(cls, files: dict[str, str]) -> "ReviewDocument":
        return cls(type="code", files=dict(files))
