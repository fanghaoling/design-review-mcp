# Endpoint Configuration

[English](endpoint_config.md) | [简体中文](endpoint_config.zh-CN.md)

BrainRegion (`brain-region-mcp`) supports official LiteLLM model strings and custom gateway endpoints.

Use one endpoint per wire protocol. If the same gateway exposes both OpenAI-compatible and Anthropic-compatible APIs,
split them into separate endpoint IDs.

## OpenAI-Compatible Gateways

Use this for `/v1/chat/completions` APIs with `Authorization: Bearer ...`.

```json
{
  "endpoints": {
    "modelbridge_openai": {
      "provider": "openai",
      "base_url": "https://www.modelbridge.cloud/v1",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": ["gpt-5.5", "gpt-5.4-mini"]
    }
  },
  "panel": ["modelbridge_openai/gpt-5.5"]
}
```

## Anthropic-Compatible Gateways

Use this for `/v1/messages` APIs with `x-api-key` and `anthropic-version`.

For Anthropic-compatible endpoints, set `base_url` to the site root. LiteLLM appends `/v1/messages`.

```json
{
  "endpoints": {
    "modelbridge_anthropic": {
      "provider": "anthropic",
      "base_url": "https://www.modelbridge.cloud",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": ["claude-haiku-4-5", "claude-opus-4-8"]
    }
  },
  "panel": ["modelbridge_anthropic/claude-opus-4-8"]
}
```

## Panel Shortcuts

`panel` accepts:

- `"endpoints"`: expand every model declared under every endpoint.
- `"endpoint_id"`: expand every model under one endpoint.
- `"endpoint_id/model"`: run one model through one endpoint.
- `"gpt-4o"` or other LiteLLM strings: run directly through the official provider environment variables.

Bare model names and endpoint references are different routes. `"claude-opus-4-8"` is treated as an official LiteLLM
Anthropic model and usually needs `ANTHROPIC_API_KEY`; `"modelbridge_anthropic/claude-opus-4-8"` routes through the
configured `modelbridge_anthropic` endpoint and uses its `MODEBRIDGE_API_KEY`. If the same model name exists under a
gateway, include the endpoint prefix when you want the gateway route.

Use `list_model_routes` to inspect how the current config will resolve a panel:

```python
list_model_routes(panel=[
    "claude-opus-4-8",
    "modelbridge_anthropic/claude-opus-4-8",
])
```

The tool only returns route metadata, endpoint declarations, model lists, and whether a key is present. It does not call
models or return API key values.

## Model Profiles

Endpoint `models` can be plain strings or objects with optional profile metadata:

```jsonc
{
  "endpoints": {
    "modelbridge_openai": {
      "provider": "openai",
      "base_url": "https://www.modelbridge.cloud/v1",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": [
        {
          "id": "gpt-5.4-mini",
          "activation_role": "sleep",
          "tier": "economy",
          "cost": "low",
          "latency": "fast",
          "tags": ["cheap", "fast"],
          "quality_score": 0.65,
          "cost_score": 0.9,
          "speed_score": 0.85
        }
      ]
    }
  }
}
```

You can also keep endpoint `models` as strings and place profiles in the top-level `model_profiles` map. Keys may be a
bare model name or an endpoint ref:

```jsonc
{
  "model_profiles": {
    "modelbridge_anthropic/claude-opus-4-8": {
      "activation_role": "awake",
      "tier": "flagship",
      "cost": "high",
      "tags": ["deep_reasoning", "architecture"],
      "quality_score": 0.98,
      "cost_score": 0.2
    }
  }
}
```

Profiles are descriptive preflight metadata for humans and schedulers. `suggest_panel` can rank configured routes from
these scores and tags, returning a `selected_panel` without calling models or automatically executing downstream tools.

## Common Failures

- `Empty or invalid response` with an HTML page usually means an OpenAI-compatible `base_url` is missing `/v1`.
- `Invalid URL (POST /v1/v1/messages)` means an Anthropic-compatible `base_url` includes `/v1`; use the site root instead.
- `No permission to access auto group` comes from the gateway account/key permissions. The request reached the gateway, but the key cannot access that model/group.
- Missing `ANTHROPIC_API_KEY` often means a bare `claude-*` model name was used instead of a `modelbridge_anthropic/...`
  endpoint reference.
