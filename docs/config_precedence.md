# Design Review Config Precedence

[English](config_precedence.md) | [简体中文](config_precedence.zh-CN.md)

Defaults are layered as:

```text
builtin < global config < project config < env < explicit tool args
```

Use global config for user or machine defaults that should survive project switches:

- `panel`
- `endpoints`
- `api_key_env`
- `max_cost_usd`
- `effort`
- `timeout`
- `normalizer_model`
- shared `privacy_policy` defaults

Global config lookup:

```text
DESIGN_REVIEW_CONFIG, if set
$CODEX_HOME/design_review_config.json
~/.codex/design_review_config.json
~/.config/design-review/config.json
```

Project-local config is still supported through the historical project-root environment variable:

```text
$UNITY_PROJECT_ROOT/Assets/Generated/AIGenerated/design_review_config.json
```

For general projects, point `UNITY_PROJECT_ROOT` at the project root you want reviewed.
Project config overrides global config. Dict values merge recursively, so project config can add one local endpoint,
one `context_modes` entry, or one `model_reliability_prior.custom` entry without duplicating the whole global file.
Lists and scalar values replace the lower layer.

For endpoint protocol examples, see [endpoint_config.md](endpoint_config.md).
