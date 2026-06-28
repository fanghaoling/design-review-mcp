# Endpoint Configuration

`design-review-mcp` supports official LiteLLM model strings and custom gateway endpoints.

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
      "models": ["claude-haiku-4-5"]
    }
  },
  "panel": ["modelbridge_anthropic/claude-haiku-4-5"]
}
```

## Panel Shortcuts

`panel` accepts:

- `"endpoints"`: expand every model declared under every endpoint.
- `"endpoint_id"`: expand every model under one endpoint.
- `"endpoint_id/model"`: run one model through one endpoint.
- `"gpt-4o"` or other LiteLLM strings: run directly through the official provider environment variables.

## Common Failures

- `Empty or invalid response` with an HTML page usually means an OpenAI-compatible `base_url` is missing `/v1`.
- `Invalid URL (POST /v1/v1/messages)` means an Anthropic-compatible `base_url` includes `/v1`; use the site root instead.
- `No permission to access auto group` comes from the gateway account/key permissions. The request reached the gateway, but the key cannot access that model/group.
