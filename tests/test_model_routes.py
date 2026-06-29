from __future__ import annotations

import pytest

from brainregion.server import _describe_model_routes, _normalize_panel


def test_normalize_panel_accepts_endpoint_model_objects():
    cfg = {
        "relay": {
            "models": [
                {"id": "cheap-model", "tier": "economy", "cost": "low"},
                {"model": "flagship-model", "profile": {"tier": "flagship"}},
            ]
        }
    }

    entries = _normalize_panel(["relay"], {"relay"}, cfg)
    assert entries == [
        {"label": "relay/cheap-model", "model": "cheap-model", "endpoint_id": "relay"},
        {"label": "relay/flagship-model", "model": "flagship-model", "endpoint_id": "relay"},
    ]


def test_describe_model_routes_includes_profiles_and_warnings(monkeypatch):
    monkeypatch.setenv("MODEBRIDGE_API_KEY", "secret")
    defaults = {
        "panel": ["claude-opus-4-8", "modelbridge_anthropic/claude-opus-4-8"],
        "model_profiles": {
            "modelbridge_anthropic/claude-opus-4-8": {
                "activation_role": "awake",
                "tags": ["architecture"],
                "quality_score": 0.98,
            }
        },
        "endpoints": {
            "modelbridge_anthropic": {
                "provider": "anthropic",
                "base_url": "https://www.modelbridge.cloud",
                "api_key_env": "MODEBRIDGE_API_KEY",
                "models": [
                    {
                        "id": "claude-opus-4-8",
                        "tier": "flagship",
                        "cost": "high",
                        "latency": "medium",
                    }
                ],
            }
        },
    }

    routes = _describe_model_routes(None, defaults, panel_source="test")
    bare, endpoint = routes["resolved_panel"]

    assert bare["route_type"] == "official_litellm"
    assert bare["profile"]["tier"] == "flagship"
    assert "deep_reasoning" in bare["profile"]["tags"]
    assert endpoint["route_type"] == "configured_endpoint"
    assert endpoint["profile"]["activation_role"] == "awake"
    assert endpoint["profile"]["quality_score"] == 0.98
    assert "architecture" in endpoint["profile"]["tags"]
    assert routes["endpoints"][0]["model_profiles"][0]["profile"]["tier"] == "flagship"
    assert routes["warnings"][0]["type"] == "bare_model_has_endpoint_ref"


def test_describe_model_routes_reports_missing_key_and_duplicate_endpoint_models(monkeypatch):
    monkeypatch.delenv("RELAY_KEY", raising=False)
    defaults = {
        "panel": ["endpoints"],
        "endpoints": {
            "relay_a": {
                "provider": "openai",
                "base_url": "https://a.example/v1",
                "api_key_env": "RELAY_KEY",
                "models": ["gpt-5.5"],
            },
            "relay_b": {
                "provider": "openai",
                "base_url": "https://b.example/v1",
                "api_key_env": "RELAY_KEY",
                "models": ["gpt-5.5"],
            },
        },
    }

    routes = _describe_model_routes(None, defaults, panel_source="test")
    assert routes["available_model_refs"] == ["relay_a/gpt-5.5", "relay_b/gpt-5.5"]
    assert {warning["type"] for warning in routes["warnings"]} == {
        "missing_endpoint_key",
        "model_declared_under_multiple_endpoints",
    }


def test_endpoint_model_object_requires_id_or_model():
    with pytest.raises(ValueError, match="id or model"):
        _normalize_panel(["relay"], {"relay"}, {"relay": {"models": [{"tier": "economy"}]}})
