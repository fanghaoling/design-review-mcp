# Endpoint 配置

[English](endpoint_config.md) | [简体中文](endpoint_config.zh-CN.md)

`design-review-mcp` 同时支持官方 LiteLLM 模型字符串和自定义中转站 endpoint。

建议一个 endpoint 只对应一种网络协议。如果同一个中转站同时提供 OpenAI 兼容和 Anthropic 兼容 API，
请拆成不同的 endpoint ID。

## OpenAI 兼容中转站

适用于 `/v1/chat/completions`，认证方式通常是 `Authorization: Bearer ...`。

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

## Anthropic 兼容中转站

适用于 `/v1/messages`，认证方式通常是 `x-api-key` 和 `anthropic-version`。

Anthropic 兼容 endpoint 的 `base_url` 应写站点根地址。LiteLLM 会自动拼接 `/v1/messages`。

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

## Panel 写法

`panel` 支持：

- `"endpoints"`：展开所有 endpoint 下声明的所有模型。
- `"endpoint_id"`：展开某个 endpoint 下声明的所有模型。
- `"endpoint_id/model"`：通过某个 endpoint 调用单个模型。
- `"gpt-4o"` 或其它 LiteLLM 字符串：直接走官方 provider 环境变量。

## 常见错误

- `Empty or invalid response` 并返回 HTML 页面：OpenAI 兼容 endpoint 的 `base_url` 通常缺少 `/v1`。
- `Invalid URL (POST /v1/v1/messages)`：Anthropic 兼容 endpoint 的 `base_url` 多写了 `/v1`，应改成站点根地址。
- `No permission to access auto group`：请求已经到达中转站，但当前 key 没有访问该模型/分组的权限。
