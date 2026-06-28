# design-review-mcp

[English](README.md) | [简体中文](README.zh-CN.md)

`design-review-mcp` is an MCP server and CLI for adversarial design reviews. It fans out a plan, code change, or document
to one or more LLM reviewers, retrieves project knowledge, normalizes findings, and returns consensus-oriented reports.

The project is built for local engineering workflows: Unity ECS is the first-class adapter, and the core review pipeline
stays project-agnostic so other adapters can be added later.

## What It Does

- Reviews plans, code, ADRs, RFCs, markdown, and config documents.
- Supports multiple reviewer roles such as `planner`, `safety`, `architecture`, `performance`, `feasibility`, and `visionary`.
- Supports project adapters: `generic` and `unity`.
- Supports official LiteLLM model strings and custom OpenAI/Anthropic-compatible gateways.
- Merges defaults from builtin values, global config, project config, environment variables, and explicit tool arguments.
- Stores review memory locally so accepted/rejected findings can influence later confidence calibration.

## Quick Start

```bash
cd Tools/design-review-mcp
uv sync --extra dev
uv run design-review plan --text "# Plan" --output markdown
```

Run tests:

```bash
uv run pytest tests/ -q
uv run --extra dev ruff check .
```

## MCP Setup

For Codex or Claude Code, register the stdio server:

```jsonc
{
  "type": "stdio",
  "command": "uv",
  "args": [
    "run",
    "--directory",
    "D:/Unity/My Project/Unity-ECS/My project/Tools/design-review-mcp",
    "design-review-mcp"
  ],
  "env": {
    "UNITY_PROJECT_ROOT": "D:/Unity/My Project/Unity-ECS/My project",
    "DESIGN_REVIEW_CONFIG": "D:/Unity/My Project/Unity-ECS/My project/Tools/design-review-mcp/design_review_config.json"
  }
}
```

Keep local API keys in `.env`. Do not commit `.env` or `design_review_config.json`.

## Configuration

Defaults are resolved in this order:

```text
builtin < global config < project config < env < explicit tool args
```

Useful docs:

- [Config precedence](docs/config_precedence.md)
- [Endpoint configuration](docs/endpoint_config.md)

Typical local config path:

```text
Tools/design-review-mcp/design_review_config.json
```

Example endpoint split for the same gateway:

```json
{
  "endpoints": {
    "modelbridge_openai": {
      "provider": "openai",
      "base_url": "https://www.modelbridge.cloud/v1",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": ["gpt-5.5"]
    },
    "modelbridge_anthropic": {
      "provider": "anthropic",
      "base_url": "https://www.modelbridge.cloud",
      "api_key_env": "MODEBRIDGE_API_KEY",
      "models": ["claude-haiku-4-5"]
    }
  },
  "panel": ["modelbridge_openai/gpt-5.5", "modelbridge_anthropic/claude-haiku-4-5"]
}
```

## Tools

- `ping`: health check.
- `list_defaults`: show merged defaults and sources.
- `list_adapters`: show available adapters and auto-detection result.
- `list_reviewers`: show reviewer roles.
- `review_plan`: review a plan or design proposal.
- `review_code`: review source files.
- `review_document`: review markdown, code, ADR, RFC, or config content.
- `mark_finding`: record whether a finding was accepted, rejected, or partially accepted.

## Project Layout

```text
design_review/
  server.py              # MCP server entry point
  cli.py                 # design-review CLI
  core/                  # pipeline, stages, schemas, report models
  adapters/              # generic and Unity adapters
  providers/             # LLM backends
  knowledge/             # retrieval providers
  privacy/               # privacy policies
  output/                # renderers
tests/                   # pytest coverage
docs/                    # focused docs
```

## Security Notes

- Do not commit `.env`, `.env.local`, API keys, generated databases, or local `design_review_config.json` files.
- Prefer `api_key_env` over plaintext `api_key` in config files.
- `Assets/Generated/AIGenerated/design_reviews.db` is generated local data and should not be used by tests.

## License

Apache-2.0
