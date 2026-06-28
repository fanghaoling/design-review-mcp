# 新手快速上手

面向首次配置 design-review-mcp 的用户 / AI。5 分钟跑通第一次审查。

> 完整功能（隐私模式 / 发散维度 / 成本控制 / Review Memory / 模型可信度先验）见 [README.md](README.md)。本篇只讲跑通 + 避坑。

## 1. 安装

```bash
cd Tools/design-review-mcp
uv sync
```

## 2. 配置 API key（环境变量）

通过环境变量读 key（litellm 约定）。**设完 key 必须重启进程**（见坑 #2）：

| 模型 | env 变量 | model 字符串 |
|---|---|---|
| OpenAI GPT | `OPENAI_API_KEY` | `gpt-4o` / `gpt-5` |
| Anthropic Claude | `ANTHROPIC_API_KEY` | `claude-opus-4-8` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek/deepseek-chat` |
| 智谱 GLM（官方） | `ZAI_API_KEY` | `zai/glm-5.2` |

Windows：`setx OPENAI_API_KEY "sk-xxx"`。

## 3. 注册到 Claude Code

`~/.claude.json` 对应项目 `mcpServers` 加：

```jsonc
"design-review": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "--directory", "<项目>/Tools/design-review-mcp", "design-review-mcp"],
  "env": {
    "UNITY_PROJECT_ROOT": "<项目>",
    "OPENAI_API_KEY": "...",
    "ANTHROPIC_API_KEY": "..."
  }
}
```

## 4. 第一次审查

```
review_plan(plan_text="# 我的方案...", panel=["gpt-4o", "claude-opus-4-8"])
```

结果看 `consensus`（全模型同意，优先处理）/ `majority`（2+ 模型）/ `individual`（单模型，谨慎）。

## 5. 卡住时外援会诊

当主模型没有把握、连续调试失败或需要另一个视角时，用 `consult_problem`：

```python
consult_problem(
    problem="测试在新增 MCP tool 后偶发失败",
    context="只需要外部模型给排查思路，不要修改文件。",
    logs="AssertionError: expected consult_problem in tool list",
    attempts=["重启 MCP server", "检查 FastMCP 注册表"],
    mode="debugging",
    panel=["modelbridge_openai/gpt-5.4-mini"],
    max_cost_usd=0.02,
)
```

常用 mode：

- `debugging`：根因定位。
- `architecture`：架构视角。
- `challenge`：反方挑战。
- `simplicity`：简化/YAGNI。
- `planning`：任务拆解。

---

## 中转站 / 自定义 endpoint（OpenAI/Anthropic 兼容网关）

用中转站（智谱 Anthropic 兼容端点、小米 MiMo、New API、one-api 等）省官方 key 的钱，或接入官方没覆盖的模型：

```jsonc
// design_review_config.json
{
  "endpoints": {
    "zhipu": {"provider":"anthropic", "base_url":"https://open.bigmodel.cn/api/anthropic",
              "api_key_env":"ANTHROPIC_AUTH_TOKEN", "models":["glm-5.2","glm-4.7"]},
    "mimo":  {"provider":"openai", "base_url":"https://api.xiaomimimo.com/v1",
              "api_key_env":"MIMO_API_KEY", "models":["mimo-v2.5-pro"]}
  },
  "panel": ["gpt-4o", "endpoints"]
}
```

`panel` 里的 **`"endpoints"` 是通配**——自动引上面声明的所有中转站的所有 models。**加新中转站只动 `endpoints` 块，`panel` 永远不用改。**

### panel 引用 endpoint 的写法（v2.3）

- `"endpoints"` → 通配所有中转站（推荐）
- `"zhipu"` → 全展开单家（声明里的所有 models）
- `"zhipu/glm-5.2"` → 短引用单个
- `{"endpoint":"zhipu","model":"glm-5.2","label":"自定义名"}` → 自定义 label（v1.6 原始写法）

---

## ⚠️ 常见配置坑（必看）

### #1 `api_key_env` 填的是变量名，不是 key 值

- ✅ `"api_key_env": "MIMO_API_KEY"` → server 从 `os.environ["MIMO_API_KEY"]` 读 key
- 你要先 `setx MIMO_API_KEY "sk-xxx"` 把 key 设进环境变量
- ❌ 别把 `sk-xxx` 直接填进 `api_key_env`——server 会拿 `sk-xxx` 当变量名去查环境变量，查不到
- 不想用环境变量就用 `"api_key": "sk-xxx"` 明文（但 config.json **别进 git**）

### #2 `setx` 后要重启整个 VSCode，不只 Claude Code session

MCP server 继承 VSCode 进程的 env。`setx` 设的是系统级 env，但**已经在跑的 VSCode 进程拿不到**——它启动时的 env 已经固定了。

光重启 Claude Code session 不够（session 还在旧 VSCode 进程里）。**完全退出 VSCode 再开**（任务管理器确认 `Code.exe` 都关了），新进程才读得到新设的 env。

### #3 `_resolve_endpoints` 会检查 config 里所有 endpoints（不只 panel 用的）

config 里**每个** endpoint 的 `api_key_env` 都会被检查。**任一** endpoint 的环境变量没设 → raise，**阻断所有 review**（即使 panel 根本没用那个 endpoint）。

→ 临时不想用某家中转站，**整块删掉或注释掉** `endpoints.<id>`，别只从 `panel` 移除。

### #4 litellm 原生 str 和 endpoint 是两条路，扣不同 key 的钱

- `"zai/glm-5.2"`（str）→ litellm 原生 `zai/` provider，走 `ZAI_API_KEY`（智谱官方直连）
- `endpoints` 块声明的中转站（如智谱 Anthropic 兼容端点）→ 走你指定的 key（如 `ANTHROPIC_AUTH_TOKEN`）

同一个 glm-5.2，两种路径扣不同账户的钱。想省钱要分清走哪条——`zai/` 前缀的 str 永远走官方，要走中转站必须用 endpoint。

### #5 中转站 provider 选 openai 还是 anthropic？

看中转站文档提供哪种兼容协议：
- OpenAI 兼容（`/v1/chat/completions`，`Authorization: Bearer`）→ `provider: "openai"`（如小米 MiMo）
- Anthropic 兼容（`/v1/messages`，`x-api-key`）→ `provider: "anthropic"`（如智谱 Anthropic 端点）
- 都支持就选 openai（更通用）

认证默认 `Authorization: Bearer`（openai）/ `x-api-key`（anthropic）。少数中转站只认自定义 header，加 `"headers": {...}` 绕。

---

## 模型可信度先验 / warm-start（可选，新手可跳过）

默认每个模型 reliability = 1.0（不加权），审查靠 consensus（多模型同意）+ 知识库命中。**新手不用管这块**，第一次跑通后再看。

两种给模型加 reliability 的方式：

1. **飞轮（默认，零配置）**：用 `mark_finding(finding_id, decision, params_hash)` 标 finding 采纳（accepted/rejected/partial）——review 返回的每条 finding 带 `id`，报告带 `params_hash`。工具按 `(模型, 维度)` 历史采纳率给后续 review 加权（采纳率高的模型 finding 升、低的降，温和 ×0.75~1.15）。标多了自然准。
2. **先验 warm-start（v2.2，冷启动兜底）**：新项目无 feedback 时，填先验给冷启动初值，不用从头标：
   ```jsonc
   "model_reliability_prior": {
     "mode": "custom",
     "custom": {"gpt-4o": {"planner": {"r": 0.55, "kappa": 10}}}
   }
   ```
   - `mode: "builtin"`（默认）= 读框架 preset，今天空 = 全 1.0（= 不启用，等同纯飞轮）
   - `mode: "custom"` = 你填经验先验。`r`=可靠性 0~1（觉得某模型某维度常误报就填低），`kappa`=先验强度 10~20（越大冷启动锚越强）
   - `mode: "none"` = 完全禁用
   - 本地 feedback 积累后会自动收敛覆盖先验

详见 README「模型可信度先验（v2.2 warm-start）」节。

## 完整 config 示例

```jsonc
{
  "endpoints": {
    "zhipu": {"provider":"anthropic", "base_url":"https://open.bigmodel.cn/api/anthropic",
              "api_key_env":"ANTHROPIC_AUTH_TOKEN", "models":["glm-5.2","glm-4.7"]},
    "mimo":  {"provider":"openai", "base_url":"https://api.xiaomimimo.com/v1",
              "api_key_env":"MIMO_API_KEY", "models":["mimo-v2.5-pro"]}
  },
  "panel": ["gpt-4o", "claude-opus-4-8", "endpoints"],
  "normalizer_model": "gpt-4o",
  "timeout": 180
}
```

## 排错

- **review 报 `endpoint 'X' api_key_env='Y' 环境变量未设置`** → 坑 #2/#3。检查环境变量是否设了、VSCode 是否整个重启、config 里是否有多余 endpoint。
- **review 报认证错（401）** → key 错或 provider 选错（坑 #5）。检查 `provider` 和 key 是否对应中转站要求的协议。
- **calibrated_confidence 怎么解读** → 看 README「Review Pipeline」，优先处理 `consensus` + 高 severity 的 finding。
