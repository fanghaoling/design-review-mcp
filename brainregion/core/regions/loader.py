"""Region registry and deterministic router.

The MVP is intentionally local and side-effect free: it ranks regions by
explicit YAML triggers and returns a small trace. It does not call models,
read memory, or change review/consult/planner behavior.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REGIONS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RegionDefinition:
    """Static brain region definition loaded from YAML."""

    id: str
    name: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    negative_triggers: list[str] = field(default_factory=list)
    # sentinel_keywords：wake gate 假阴性兜底用（命中但未被 retrieve/escalate → sentinel wake）。
    # 含中文同义词；无声明时 wake gate 用内置 fallback（仅限已知 region）。见 core/wake/gate.py。
    sentinel_keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"region YAML must be an object: {path}")
    return data


def load_region(name: str, regions_dir: str | Path = REGIONS_DIR) -> RegionDefinition:
    """Load one region definition by id/name."""
    d = Path(regions_dir)
    path = d / f"{name}.yaml"
    if not path.exists():
        available = list_regions(d)
        raise ValueError(f"unknown region: {name!r}; available: {available}")
    data = _load_yaml(path)
    rid = str(data.get("id") or path.stem).strip()
    if not rid:
        raise ValueError(f"region id cannot be empty: {path}")
    return RegionDefinition(
        id=rid,
        name=str(data.get("name") or rid).strip(),
        description=str(data.get("description") or "").strip(),
        triggers=_as_str_list(data.get("triggers")),
        negative_triggers=_as_str_list(data.get("negative_triggers")),
        sentinel_keywords=_as_str_list(data.get("sentinel_keywords")),
    )


def list_regions(regions_dir: str | Path = REGIONS_DIR) -> list[str]:
    """List available region ids."""
    d = Path(regions_dir)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_regions(regions_dir: str | Path = REGIONS_DIR) -> list[RegionDefinition]:
    """Load all region definitions."""
    return [load_region(name, regions_dir) for name in list_regions(regions_dir)]


def _normalize(text: str) -> str:
    folded = str(text or "").casefold()
    return " ".join(re.sub(r"[\W_]+", " ", folded, flags=re.UNICODE).split())


def _contains(normalized_haystack: str, trigger: str) -> bool:
    needle = _normalize(trigger)
    if not needle:
        return False
    return needle in normalized_haystack


def _match_triggers(region: RegionDefinition, *, text: str, file_text: str) -> tuple[int, list[dict]]:
    score = 0
    matches: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for trigger in region.triggers:
        if _contains(text, trigger):
            key = (trigger, "text")
            if key not in seen:
                seen.add(key)
                score += 2
                matches.append({"trigger": trigger, "source": "text", "weight": 2})
        if file_text and _contains(file_text, trigger):
            key = (trigger, "files")
            if key not in seen:
                seen.add(key)
                score += 1
                matches.append({"trigger": trigger, "source": "files", "weight": 1})

    for trigger in region.negative_triggers:
        if _contains(text, trigger) or (file_text and _contains(file_text, trigger)):
            score -= 3
            matches.append({"trigger": trigger, "source": "negative", "weight": -3})

    return score, matches


def _candidate(region: RegionDefinition, score: int, matches: list[dict]) -> dict:
    positive = [m for m in matches if m["weight"] > 0]
    negative = [m for m in matches if m["weight"] < 0]
    reasons: list[str] = []
    if positive:
        reasons.append(f"matched {len(positive)} trigger(s)")
    if negative:
        reasons.append(f"penalized by {len(negative)} negative trigger(s)")
    return {
        "id": region.id,
        "name": region.name,
        "description": region.description,
        "score": score,
        "confidence": round(min(1.0, max(0.0, score / 8.0)), 3),
        "matched_triggers": [m for m in matches if m["weight"] > 0],
        "negative_triggers": [m for m in matches if m["weight"] < 0],
        "reasons": reasons,
    }


def route_regions(
    *,
    goal: str = "",
    problem: str = "",
    context: str = "",
    files: dict[str, str] | None = None,
    top_k: int = 3,
    min_score: int = 2,
    regions: list[RegionDefinition] | None = None,
    regions_dir: str | Path = REGIONS_DIR,
) -> dict:
    """Rank regions by deterministic trigger matches.

    File contents are intentionally ignored. File paths are weak metadata only.
    """
    if top_k <= 0:
        raise ValueError("top_k must be greater than 0")
    if min_score < 0:
        raise ValueError("min_score must be non-negative")

    loaded_regions = regions if regions is not None else load_regions(regions_dir)
    text_parts = [goal, problem, context]
    raw_text = "\n".join(part for part in text_parts if part)
    text = _normalize(raw_text)
    file_paths = [str(path) for path in (files or {}).keys()]
    file_text = _normalize("\n".join(file_paths))

    candidates: list[dict] = []
    for region in loaded_regions:
        score, matches = _match_triggers(region, text=text, file_text=file_text)
        if score > 0 or matches:
            candidates.append(_candidate(region, score, matches))

    candidates.sort(key=lambda item: (-item["score"], item["id"]))
    positive = [item for item in candidates if item["score"] >= min_score]
    selected = positive[:top_k]
    no_match_reason = ""
    if not selected:
        if not any([goal, problem, context, file_paths]):
            no_match_reason = "empty_input"
        elif not positive:
            no_match_reason = "below_min_score"

    return {
        "selected": selected,
        "candidates": candidates,
        "trace": {
            "strategy": "deterministic_keyword_v1",
            "top_k": top_k,
            "min_score": min_score,
            "input": {
                "text_chars": len(raw_text),
                "file_paths": len(file_paths),
                "file_contents_used": False,
            },
            "available_regions": len(loaded_regions),
            "no_match_reason": no_match_reason,
        },
    }
