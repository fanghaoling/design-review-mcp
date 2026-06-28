# design-review-mcp

[English](README.md) | [简体中文](README.zh-CN.md)

`design-review-mcp` 是一个用于“对抗式设计审查”的 MCP server 和 CLI。它可以把方案、代码或文档分发给多个模型 reviewer，并结合项目知识库、finding 归一化和共识汇总，生成更适合工程决策的审查报告。

这个工具面向本地工程工作流：当前内置 `generic` 和 `unity` adapter，Unity ECS 是重点适配方向；核心 pipeline 保持项目无关，后续可以继续扩展到其它项目类型。

## 能做什么

- 审查方案、代码、ADR、RFC、Markdown 和配置文档。
- 支持 `planner`、`safety`、`architecture`、`performance`、`feasibility`、`visionary` 等 reviewer 角色。
- 支持 `generic` 和 `unity` 项目 adapter。
- 支持官方 LiteLLM 模型字符串，也支持 OpenAI/Anthropic 兼容中转站。
- 支持 builtin、全局配置、项目配置、环境变量、显式参数的分层默认值。
- 支持 Review Memory：标记 finding 是否采纳后，后续审查可据此调整模型可信度。

## 快速开始

```bash
cd Tools/design-review-mcp
uv sync --extra dev
uv run design-review plan --text "# Plan" --output markdown
```

运行测试：

```bash
uv run pytest tests/ -q
uv run --extra dev ruff check .
```

## MCP 配置

在 Codex 或 Claude Code 中注册 stdio server：

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

本地 API key 建议放在 `.env`。不要提交 `.env` 或 `design_review_config.json`。

## 配置

默认值优先级：

```text
builtin < global config < project config < env < explicit tool args
```

相关文档：

- [配置优先级](docs/config_precedence.zh-CN.md)
- [Endpoint 配置](docs/endpoint_config.zh-CN.md)

常用本地配置路径：

```text
Tools/design-review-mcp/design_review_config.json
```

同一个中转站如果同时提供 OpenAI 和 Anthropic 兼容接口，建议按协议拆成两个 endpoint：

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

## MCP 工具

- `ping`：健康检查。
- `list_defaults`：查看合并后的默认值和来源。
- `list_adapters`：查看可用 adapter 和自动识别结果。
- `list_reviewers`：查看 reviewer 角色。
- `review_plan`：审查方案或设计计划。
- `review_code`：审查代码文件。
- `review_document`：审查 markdown、code、ADR、RFC、config。
- `mark_finding`：记录 finding 的采纳、拒绝或部分采纳结果。

## 项目结构

```text
design_review/
  server.py              # MCP server 入口
  cli.py                 # design-review CLI
  core/                  # pipeline、stage、schema、report model
  adapters/              # generic / Unity adapter
  providers/             # LLM backend
  knowledge/             # 检索实现
  privacy/               # 隐私策略
  output/                # 输出渲染
tests/                   # pytest 覆盖
docs/                    # 聚焦文档
```

## 安全说明

- 不要提交 `.env`、`.env.local`、API key、生成数据库或本地 `design_review_config.json`。
- 配置中优先使用 `api_key_env`，避免明文 `api_key`。
- `Assets/Generated/AIGenerated/design_reviews.db` 是本地生成数据，测试不应依赖它。

## License

Apache-2.0
