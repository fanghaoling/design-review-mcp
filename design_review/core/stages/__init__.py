"""内置 Pipeline Stage + 默认 pipeline 构造。

9 个 Stage 顺序：retrieve → context → prompt → review → parse → dedup → normalize → consensus → score。
dedup（v1.2 F2）在 parse 后模型内去重；normalize（v1.2 F1）做 case_ref 跨维度合并。
v2 可 pipeline.insert(DebateStage(), before="normalize")。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..pipeline import Pipeline
from .consensus import ConsensusStage
from .context import ContextStage
from .dedup import DedupStage
from .mediate import MediateStage
from .normalize import NormalizeStage
from .parse import ParseStage
from .prompt import PromptStage
from .retrieve import RetrieveStage
from .review import ReviewStage
from .score import ScoreStage

__all__ = [
    "RetrieveStage",
    "ContextStage",
    "PromptStage",
    "ReviewStage",
    "ParseStage",
    "MediateStage",
    "DedupStage",
    "NormalizeStage",
    "ConsensusStage",
    "ScoreStage",
    "CORE_REVIEWERS_DIR",
    "build_default_pipeline",
]

CORE_REVIEWERS_DIR = Path(__file__).resolve().parent.parent / "reviewers"


def build_default_pipeline(
    *,
    normalizer: dict | None = None,
    threshold: int = 2,
    core_reviewers_dir: str | Path | None = None,
    default_dimensions: list[str] | None = None,
    policy: Any = None,
) -> Pipeline:
    """构造默认 pipeline。

    normalizer: PanelEntry{model, endpoint_id}（v1.6 与 panel 统一 schema，可走中转）。
    policy: PrivacyPolicy（v1.7，非 None 时在 Parse 后插 MediateStage；transform 在 engine 层 pipeline 外）。
    """
    crd = Path(core_reviewers_dir) if core_reviewers_dir else CORE_REVIEWERS_DIR
    stages = [
        RetrieveStage(),
        ContextStage(),
        PromptStage(crd, default_dimensions=default_dimensions),
        ReviewStage(),
        ParseStage(),
    ]
    if policy is not None:
        stages.append(MediateStage())
    stages += [
        DedupStage(),
        NormalizeStage(normalizer),
        ConsensusStage(threshold),
        ScoreStage(),
    ]
    return Pipeline(stages)
