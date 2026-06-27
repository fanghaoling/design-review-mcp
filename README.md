# design-review-mcp

AI Design Review Framework — 多模型对抗设计审查 MCP 工具。

把设计文档/代码 fan-out 给多个不同厂商大模型并行审查，结合项目知识库检索注入历史踩坑，按 canonical 归一 + 校准共识汇总，提高规划质量。

## 架构

全插件化（所有项目特定逻辑进 adapter，core 项目无关）：

| 可换层 | 协议 | 默认实现 |
|---|---|---|
| `ModelBackend`（调用层实现） | `async complete(...)` | `LiteLLMBackend` |
| `KnowledgeProvider` | `retrieve/list_cases/add_case` | `YamlKnowledgeProvider` |
| `ProjectAdapter` | `read_context/version/convention + reviewers/knowledge` | `UnityAdapter` / `GenericAdapter` |
| `ReportRenderer` | `render(ReviewReport)` | `MarkdownRenderer` / `JSONRenderer` |
| `Stage`（Pipeline 步骤） | `process(ctx)->ctx` | retrieve/context/prompt/review/parse/normalize/consensus/score |

日后加 RustAdapter/CppAdapter/WebAdapter 只加 adapter 包，不动 core。

## Review Pipeline

```
ReviewDocument → RetrieveStage → ContextStage → PromptStage → ReviewStage(fan-out)
              → ParseStage → NormalizeStage(canonical) → ConsensusStage → ScoreStage → ReviewReport
```

防冷门技术栈"共谋错误"：强制 `evidence_quote`（无引用丢弃）+ 知识库 RAG（版本过滤）+ 角色化 reviewer（独立 system_prompt+采样）+ canonical normalize（防同义漏报）+ calibrated confidence。

## 知识库（重要：框架只带通用种子，你项目的踩坑要自己加）

**工具的审查质量 = 知识库厚度。** 框架随包只带**通用**种子案例（Unity ECS/Burst/FlowField/NetCode 的 API 级 gotcha，见 `design_review/adapters/unity/knowledge/`，共十余条）。你项目**自己的**踩坑（架构决策、历史 bug、约定）不会自动有——得自己积累，这才是工具对你项目的护城河，也是别人 clone 走也拿不走的部分。

### 放哪
`<项目根>/.design-review/knowledge/*.yaml`（项目本地）。框架知识库 + 本地知识库自动叠加（本地同 id 覆盖框架）。建议把项目特定 / 敏感内容放本地并 gitignore。

### 格式
```yaml
- id: MYSYSTEM-001              # 唯一 id，finding 引用它做 case_ref（跨模型共识锚点）
  title: "一句话踩坑"
  version: {entities: ">=1.4,<2.0"}   # 版本约束（空=通用）；retrieve 按项目版本过滤
  triggers: [关键词1, 关键词2]          # retrieve 按这些词命中方案文本（大小写不敏感）
  category: ecs_perf                   # 组织用；case_ref 命中时填到 finding.dimension
  bad_pattern: "反模式描述（会进 prompt 给模型看）"
  recommended_pattern: "正解"
  source: "MEMORY.md#xxx"              # 给人追溯，不进 prompt
```

### 怎么积累
- 从你的 `MEMORY.md` / postmortem / bug tracker / 反复出现的 code review 意见转写
- 每条聚焦**一个具体可复现**的 gotcha（bad_pattern + recommended_pattern 要可操作，别写空泛原则）
- `triggers` 写**方案文本里会出现的词**（API 名、错误码、组件名、USS 属性），retrieve 才能命中——这是召回关键
- 通用 gotcha（任何同栈项目都踩）可贡献回框架；项目特定的放本地

> 用 `list_knowledge` 工具看当前加载了哪些案例；案例少或方案涉及的领域没有对应踩坑，就是该扩了。

## 安装

```bash
cd Tools/design-review-mcp
uv sync
```

## 配置

API key 走环境变量（litellm 约定）：

| 模型 | env | model 字符串 |
|---|---|---|
| OpenAI GPT-5 | `OPENAI_API_KEY` | `gpt-5` |
| Anthropic Claude | `ANTHROPIC_API_KEY` | `claude-opus-4-8` / `claude-sonnet-4-6` |
| 火山豆包 | `ARK_API_KEY` | `volcengine/<ARK_ENDPOINT_ID>` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek/deepseek-chat` |
| 智谱 GLM | `ZAI_API_KEY` | `zai/glm-4.7` |

> ⚠️ `litellm>=1.83.0`（1.82.7/1.82.8 被投毒，已 pin 排除）。

## 注册到 Claude Code

在 `~/.claude.json` 的对应项目 `mcpServers` 加：

```jsonc
"design-review": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "--directory", "<项目>/Tools/design-review-mcp", "design-review-mcp"],
  "env": {
    "UNITY_PROJECT_ROOT": "<项目>",
    "OPENAI_API_KEY": "...",
    "ANTHROPIC_API_KEY": "...",
    "ARK_API_KEY": "..."
  }
}
```

## 成本与思考强度控制（v1.5）

两个 opt-in 参数，默认都不启用（向后兼容，老调用不变）：

- **`max_cost_usd`**（单次 review 总成本上限 USD，默认 None=无上限）：预 flight 估每个 job 成本，按 panel 顺序（用户偏好序）贪心保留直到估算超预算，其余裁掉。报告 `budget.exhausted` 标记是否裁过。贵模型（Claude/GPT）走手维护价表，glm/deepseek 等用名义单价。
- **`effort`**（思考强度 low/medium/high/xhigh/max，默认 None=各模型默认）：仅 Claude（`output_config`+thinking adaptive）/ OpenAI o 系列（`reasoning_effort`）生效，其余丢弃。Claude 默认 high 较贵，routine 方案降 medium 省 token。

两个都能进 `design_review_config.json` 设默认（如 `"max_cost_usd": 0.3`），也可每次调用显式传。估的是上界（输出按 max_tokens 打满），故略保守；实际成本见报告 `usage.cost_usd`。

## 中转站 / 自定义 endpoint（v1.6）

让 panel 引用「中转站」（智谱 Anthropic 兼容端点、New API、one-api 等 OpenAI/Anthropic 兼容网关）提供的模型。在 `design_review_config.json` 声明 endpoint，panel 项用对象引用：

```jsonc
{
  "endpoints": {
    "zhipu": {
      "provider": "anthropic",                       // 兼容网关协议：openai | anthropic
      "base_url": "https://open.bigmodel.cn/api/anthropic",
      "api_key_env": "ZHIPU_KEY",                    // 优先：从该环境变量读 key
      // "api_key": "明文",                            // fallback（⚠️ 别让 config 进 git）
      "headers": {},                                  // 可选：额外头（只认 Bearer 的站 / OpenRouter）
      "timeout": 120                                  // 可选：覆盖全局 timeout（慢中转站）
    }
  },
  "panel": [
    "gpt-4o",                                         // str = 官方（litellm 原生 provider，走 env）
    "zai/glm-5.2",                                    // litellm 原生 provider，走 env
    {"endpoint": "zhipu", "model": "glm-5.2", "label": "智谱-Anthropic端点"}
  ]
}
```

**两类模型别混**：
- **兼容网关 endpoint**（`provider: openai|anthropic`）：自定义 `base_url` 的 OpenAI/Anthropic 协议兼容端点（中转站）。用 `endpoints` + panel dict 引用，litellm 按 provider 拼 `openai/` 或 `anthropic/` 前缀 + per-call `api_base`/`api_key`。
- **litellm 原生 provider**（`zai/`、`deepseek/`、`gemini/`、`bedrock/`、`vertex_ai/`...）：直接写 model 字符串走环境变量（如 `ZAI_API_KEY`），**不走 endpoint 机制**。

**安全**：`api_key` 只在 server 解析后交给 backend 持有，不进审查 pipeline、不进缓存库（`PanelEntry` 只含 `{label, model, endpoint_id}`）。优先用 `api_key_env`，明文 `api_key` 仅作 fallback 且**别让 config.json 进 git**。

**label 是模型身份标识**：报告里 `flagged_by`/`panel`/`failed_models` 用 label 显示。panel 内 label 必须唯一（撞名报错——否则 consensus 会把同名模型错误合并）。官方 str 项的 label = model 字符串本身。

**headers / timeout**：某些中转站只认 `Authorization: Bearer`（不认 litellm 默认的 `x-api-key`），用 `"headers": {"Authorization": "Bearer ..."}` 绕开，或改走它的 OpenAI 兼容端点（`provider: openai`）；慢中转站用 `"timeout"` 覆盖全局。

**升级注意**：从 v1.5 升级后首次 review 会 miss 缓存（hash 输入结构变，重跑一次即可，无数据损坏）。回滚到 v1.5 须把 panel 的 dict 项改回 str。

## 开发

```bash
uv run pytest tests/
```

## License

Apache-2.0
