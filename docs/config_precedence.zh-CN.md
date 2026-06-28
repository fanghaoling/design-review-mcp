# Design Review 配置优先级

[English](config_precedence.md) | [简体中文](config_precedence.zh-CN.md)

默认值按以下顺序合并：

```text
builtin < global config < project config < env < explicit tool args
```

全局配置适合放跨项目复用的用户/机器级默认值：

- `panel`
- `endpoints`
- `api_key_env`
- `max_cost_usd`
- `effort`
- `timeout`
- `normalizer_model`
- 通用 `privacy_policy`

全局配置查找顺序：

```text
DESIGN_REVIEW_CONFIG, if set
$CODEX_HOME/design_review_config.json
~/.codex/design_review_config.json
~/.config/design-review/config.json
```

项目本地配置仍支持通过历史兼容的项目根目录变量加载：

```text
$UNITY_PROJECT_ROOT/Assets/Generated/AIGenerated/design_review_config.json
```

对通用项目来说，把 `UNITY_PROJECT_ROOT` 指向要审查的项目根目录即可。
项目配置覆盖全局配置。字典类型会递归合并，所以项目配置可以只增加一个本地 endpoint、一个
`context_modes` 条目或一个 `model_reliability_prior.custom` 条目，不需要复制整份全局配置。
列表和标量值会直接替换低优先级配置。

Endpoint 协议示例见 [endpoint_config.zh-CN.md](endpoint_config.zh-CN.md)。
