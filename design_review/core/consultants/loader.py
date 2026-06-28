"""Consultant role YAML loader."""
from __future__ import annotations

from pathlib import Path

from ..reviewers.loader import list_reviewers, load_reviewer

CONSULTANTS_DIR = Path(__file__).resolve().parent


def load_consultant(name: str, consultants_dir: str | Path = CONSULTANTS_DIR) -> dict:
    return load_reviewer(name, consultants_dir, fallback_dir=consultants_dir)


def list_consultants(consultants_dir: str | Path = CONSULTANTS_DIR) -> list[str]:
    return list_reviewers(consultants_dir)
