from __future__ import annotations

import json
from pathlib import Path

from design_review import defaults


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_global_config_is_loaded_from_explicit_path(tmp_path, monkeypatch):
    global_config = tmp_path / "global.json"
    _write_json(
        global_config,
        {
            "panel": ["openai/gpt-4.1-mini"],
            "max_cost_usd": 0.25,
        },
    )
    monkeypatch.setenv("DESIGN_REVIEW_CONFIG", str(global_config))

    got = defaults.get_all()

    assert got["panel"]["value"] == ["openai/gpt-4.1-mini"]
    assert got["panel"]["source"] == "global_config"
    assert got["max_cost_usd"]["value"] == 0.25


def test_project_config_overrides_and_merges_global_config(tmp_path, monkeypatch):
    global_config = tmp_path / "global.json"
    _write_json(
        global_config,
        {
            "panel": ["global-model"],
            "endpoints": {
                "global": {
                    "provider": "openai",
                    "base_url": "https://global.example/v1",
                    "api_key_env": "GLOBAL_KEY",
                    "models": ["global-model"],
                }
            },
            "context_modes": {"visionary": "compressed"},
        },
    )
    project_config = tmp_path / "Assets" / "Generated" / "AIGenerated" / "design_review_config.json"
    _write_json(
        project_config,
        {
            "panel": ["project-model"],
            "endpoints": {
                "project": {
                    "provider": "anthropic",
                    "base_url": "https://project.example",
                    "api_key_env": "PROJECT_KEY",
                    "models": ["project-model"],
                }
            },
            "context_modes": {"feasibility": "minimal"},
        },
    )
    monkeypatch.setenv("DESIGN_REVIEW_CONFIG", str(global_config))

    got = defaults.get_all()

    assert got["panel"]["value"] == ["project-model"]
    assert got["panel"]["source"] == "project_config"
    assert sorted(got["endpoints"]["value"]) == ["global", "project"]
    assert got["context_modes"]["value"] == {
        "visionary": "compressed",
        "feasibility": "minimal",
    }


def test_env_still_overrides_config(tmp_path, monkeypatch):
    global_config = tmp_path / "global.json"
    _write_json(global_config, {"timeout": 180})
    monkeypatch.setenv("DESIGN_REVIEW_CONFIG", str(global_config))
    monkeypatch.setenv("DESIGN_REVIEW_DEFAULT_TIMEOUT", "30")

    got = defaults.get_all()

    assert got["timeout"] == {"value": 30.0, "source": "env"}


def test_apply_only_uses_non_none_explicit_overrides(tmp_path, monkeypatch):
    global_config = tmp_path / "global.json"
    _write_json(global_config, {"retrieve_top_k": 9, "output_format": "markdown"})
    monkeypatch.setenv("DESIGN_REVIEW_CONFIG", str(global_config))

    got = defaults.apply(retrieve_top_k=None, output_format=None)

    assert got["retrieve_top_k"] == 9
    assert got["output_format"] == "markdown"
