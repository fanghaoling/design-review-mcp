"""Layered defaults for design-review.

Precedence:
    builtin < global config < project config < env < explicit overrides

Environment value overrides use ``DESIGN_REVIEW_DEFAULT_<KEY>``, for example
``DESIGN_REVIEW_DEFAULT_PANEL``.

Global config is loaded from ``DESIGN_REVIEW_CONFIG`` when set; otherwise from
``$CODEX_HOME/design_review_config.json``, ``~/.codex/design_review_config.json``,
or ``~/.config/design-review/config.json``.

Project config stays at
``$UNITY_PROJECT_ROOT/Assets/Generated/AIGenerated/design_review_config.json``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("design_review.defaults")

_ENV_PREFIX = "DESIGN_REVIEW_DEFAULT_"
_BUILTINS = {
    "panel": ["claude-opus-4-8", "gpt-5"],
    "dimensions": [],
    "temperature": 0.3,
    "max_tokens": 4096,
    "consensus_threshold": 2,
    "retrieve_top_k": 5,
    "timeout": 90,
    "normalizer_model": "claude-opus-4-8",
    "output_format": "json",
    "effort": None,
    "max_cost_usd": None,
    "endpoints": {},
    "privacy_policy": None,
    "context_modes": {},
    "min_compressed_chars": 50,
    "model_reliability_prior": {"mode": "builtin", "custom": {}},
}


def _project_config_path() -> Path:
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    return Path(root) / "Assets" / "Generated" / "AIGenerated" / "design_review_config.json"


def _global_config_paths() -> list[Path]:
    explicit = os.environ.get("DESIGN_REVIEW_CONFIG")
    if explicit:
        return [Path(explicit)]

    paths: list[Path] = []
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        paths.append(Path(codex_home) / "design_review_config.json")
    paths.append(Path.home() / ".codex" / "design_review_config.json")

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        paths.append(Path(xdg_config) / "design-review" / "config.json")
    else:
        paths.append(Path.home() / ".config" / "design-review" / "config.json")

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def _load_json_config(path: Path) -> dict[str, Any]:
    p = path.expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ignoring invalid design review config %s: %s", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _merge_value(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merge_value(merged.get(key), value)
        return merged
    return override


def _apply_config_layer(result: dict[str, dict[str, Any]], cfg: dict[str, Any], source: str, path: Path | None) -> None:
    for key, value in cfg.items():
        if key not in _BUILTINS:
            continue
        old_value = result[key]["value"]
        result[key] = {
            "value": _merge_value(old_value, value),
            "source": source,
        }
        if path is not None:
            result[key]["path"] = str(path.expanduser())


def _coerce(key: str, val: str):
    if key in ("temperature", "timeout", "max_cost_usd"):
        try:
            return float(val)
        except ValueError:
            return val
    if key in ("max_tokens", "consensus_threshold", "retrieve_top_k", "min_compressed_chars"):
        try:
            return int(val)
        except ValueError:
            return val
    return val


def get_all() -> dict:
    """Return ``{key: {value, source}}`` with layered config provenance."""
    result = {key: {"value": value, "source": "builtin"} for key, value in _BUILTINS.items()}

    for path in _global_config_paths():
        _apply_config_layer(result, _load_json_config(path), "global_config", path)

    project_path = _project_config_path()
    _apply_config_layer(result, _load_json_config(project_path), "project_config", project_path)

    for key in _BUILTINS:
        ev = os.environ.get(f"{_ENV_PREFIX}{key.upper()}")
        if ev is not None:
            result[key] = {"value": _coerce(key, ev), "source": "env"}
    return result


def apply(**overrides) -> dict:
    """Merge builtin < global config < project config < env < explicit overrides."""
    merged = {key: value["value"] for key, value in get_all().items()}
    for key, value in overrides.items():
        if value is not None and key in merged:
            merged[key] = value
    return merged
