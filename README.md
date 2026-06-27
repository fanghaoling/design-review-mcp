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

## CLI（v1.1）

`design-review` 命令不依赖 MCP/Claude Code，可在终端/脚本/CI 直接跑（同一套 pipeline + adapter + 知识库）：

```bash
design-review plan path/to/plan.md --output markdown       # 审方案（文件）
cat plan.md | design-review plan -                          # stdin
design-review plan --text "# 方案..." --dimensions planner  # 直接传文本
design-review code src/a.py src/b.py --output sarif --output-file out.sarif  # 审代码 → SARIF
design-review doc rfc.md --type rfc                         # 审文档（markdown/adr/rfc/config）
```

输入：`plan`/`doc` 接文件路径 / `-`（stdin）/ `--text`；`code` 接多文件。输出 `--output json|markdown|sarif`（默认 json 整 dict；md/sarif 输出 `rendered`），`--output-file` 写文件。其余参数同 MCP 工具（`--panel`/`--dimensions`/`--effort`/`--max-cost-usd`/`--adapter`/`--retrieve-top-k`/`--timeout`）。

### SARIF + CI 集成

`--output sarif` 生成 SARIF 2.1.0（consensus/majority → results，severity→error/warning/note，带 calibrated_confidence/flagged_by/case_ref），可进 GitHub Code Scanning / IDE。在 CI 里用 design-review 审查代码（dogfooding 或审任意项目）：

```yaml
# .github/workflows/design-review.yml
on: [pull_request]
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uvx --from git+https://github.com/fanghaoling/design-review-mcp design-review code src/ --output sarif --output-file dr.sarif
        env: {OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}, ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}}
      - uses: github/codeql-action/upload-sarif@v3
        with: {sarif_file: dr.sarif}
```

> ⚠️ 真实审查要付费 API key，用 GitHub Secrets，**别硬编码进公开 repo**。没配 key 时跳过此 job——本地手动 `design-review ... --output sarif` 同样能审。

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

## 隐私模式（v1.7）

对抗审查默认把完整方案/代码 fan-out 给 panel 所有模型。若 panel 含多家中转站/厂商，代码和技术细节会泄露给所有第三方。**隐私模式**让可信中间 AI 看全文，对抗模型只看脱敏摘要，对抗 verdict 由可信 AI 补 evidence + 评估。

```jsonc
{
  "privacy_policy": {
    "policy": "strict",                          // off(默认) | strict
    "trusted": {"endpoint": "zhipu", "model": "glm-5.2", "label": "trusted"},  // 可信 AI（复用 endpoint）
    "min_coverage": 0.5                          // 摘要覆盖度低于此则 raise（防垃圾摘要）
  }
}
```

**流程**（PromptStage 不知 strict 存在）：
1. `StrictPolicy.transform`（pipeline 外）：trusted 看完整方案+context → 脱敏摘要 + `coverage`/`missing_topics`/`redacted_items`。coverage 低/失败/空 → **raise 终止，绝不静默回退明文**（否则全文泄露）。
2. PromptStage 用摘要（effective_doc），对抗模型不接触全文。
3. `StrictPolicy.mediate`（Parse 后）：trusted 看对抗 verdict + 全文 → 逐条评估，给每条 finding 附加 `FindingAttachment{source, type:"mediation", payload:{evidence, reason, verdict}}`。**Finding immutable**（原 panel 字段不变），confirmed/unconfirmed/rejected 都不丢（后两个降权留痕）。

**trade-off**：
- 质量 ≈ trusted 模型能力（智谱 glm-5.2 中等）。`min_coverage` + `missing_topics` 挡垃圾摘要，但挡不住"摘要完整对抗仍漏 bug"。换更强 trusted（真 Claude via New API）才根本提升。
- code review 受限（代码脱敏破坏语义），严格模式主要适用 **plan review**。
- 成本：trusted 多 2 次调用（transform + mediate）。

**为未来扩展**：privacy 是一级模块（`privacy/{base,off,strict}.py`），`PrivacyPolicy` Protocol + `FindingAttachment` 为 Enterprise/PII/Regex/AST/CompositePolicy 留接口，core/pipeline 不动即可加新 policy（如 `privacy/enterprise.py` 实现 Protocol + 新 attachment type）。

## 发散 + 可行性维度（v1.8）

默认维度（planner/safety/ecs_perf/architecture）都是**收敛审查**（找方案漏洞）。v1.8 加两个互补维度：

- **visionary**（发散）：不找方案对错，基于压缩上下文发散**架构演进/后续路线/横向拓展**，给参考方向。`temperature 0.6`，不 inherits base，evidence = 项目依据（非方案原文）。
- **feasibility**（可行性）：整体 **go/no-go**（值不值得做/最大阻塞/优先级三件事/何时停），与 planner 互补（planner 找设计漏洞，feasibility 评整体）。`temperature 0.3`，inherits base（evidence 引用方案原文），输出含一条「整体 verdict」finding。

**context_mode 是 per-dimension 策略**（config，非 reviewer 属性——同一 planner 也能跑 minimal 看根本问题）：
```jsonc
{
  "context_modes": {"visionary": "compressed"}  // full(默认)|compressed(去 code 保 headings)|minimal(首段)
}
```
报告 `context_compression` 显示每维度压缩比；temperature 0.6 致 JSON 解析失败的模型进 `failed_models(parse_error)`（失败可见）。

**启用**：`dimensions=["visionary","feasibility"]` + config `context_modes.visionary=compressed`（不配 context_modes 则 full，visionary 仍发散但被方案锚定）。

**发散是参考**：visionary 给「可能的方向」，你来判断要不要走，别当「必须做的事」。

## Review Memory + 模型可信度（v2）

工具会**记住你对 finding 的采纳反馈**，按 `(模型, 维度)` 历史采纳率给后续 review 的共识加权——采纳率高的模型 finding 置信度提升，低的降权。这是飞轮：用得越多，工具越懂「哪些模型的哪些维度对你更准」。

### 标记 finding

review 返回的每条 finding 带 `id`，报告带 `params_hash`。标记采纳：

```python
# MCP 工具（Claude Code 内）或 CLI 后手动
mark_finding(finding_id="gpt-4o-3", decision="rejected", params_hash="abc123…", note="误报")
mark_finding(finding_id="claude-1", decision="accepted", params_hash="abc123…", note="真实漏洞")
```

- `decision`：`accepted` | `rejected` | `partial`
- `params_hash`：从 review 返回取（未传则按 finding_id 反查最近含此 id 的 review，扫 consensus+majority+individual+deduped_ids）
- `note`：decision reason 自由文本（未来 v3 可分析误报类型）
- 标记后默认失效该 review 缓存，下次同内容审查重算 reliability

### reliability 机制

- **(label, dimension) 维度**：同模型不同维度能力差异大（Claude planner 强 / ecs_perf 弱），按 `(label, dimension)` 分开统计不平均化。dimension = reviewer 身份（planner/safety/ecs_perf/...）；reviewer prompt 大改时升 dimension 名（planner-v2）隔离历史 reliability。
- **Beta(2,2) 拉普拉斯**：`(采纳分+2)/(样本数+4)`，accepted=1/partial=0.5/rejected=0。全采纳不达 1、全拒不归 0，保留可见性。
- **小样本保护**：每 `(label,dim)` 样本 <5 → reliability=1.0（不加权，向后兼容）。冷启动期全 1.0 无害。
- **温和区间 [0.75, 1.15]**：reliability 是补充信号，不压没 confidence/consensus（consensus_factor/med 已负责激进降权）。差模型最多降到 ×0.75，好模型最多升到 ×1.15。

### 数据位置 + 隔离

反馈存 SQLite（`$UNITY_PROJECT_ROOT/Assets/Generated/AIGenerated/design_reviews.db` 的 `finding_feedback` 表），**per-project**——不同项目不同 db 文件天然隔离不互染。

### 不做 DebateStage（调研否决）

v2 路线图原含 DebateStage（多轮辩论）+ Judge。联网调研（2024-2026 顶会论文：Talk Isn't Always Cheap / If MAD is the Answer / Debate or Vote NeurIPS 2025 / Should We Be Going MAD ICML 2024）否决多轮辩论用于审查任务：MMLU 掉 9-12 分，sycophancy/过早收敛/弱模型腐蚀强模型；**投票/共识才是有效部分**（工具现有的 consensus/majority/individual）。故 v2 改做飞轮，不做辩论。

## 模型可信度先验（v2.2 warm-start）

v2 reliability 飞轮冷启动期（新用户/新项目无 feedback）全 1.0 不加权。v2.2 加**先验 warm-start**：框架支持加载先验表，冷启动就有合理初值，本地 feedback 累积后 Beta 共轭收敛到本地偏好。

**机制**（Beta-Binomial 共轭，Raykar 2010 / Efron-Morris 1973）：`reliability = (α+score)/(α+β+n)`。先验 `α=r·κ, β=(1-r)·κ`，与本地 feedback 共轭——`n=0`→先验均值，`n→∞`→本地真相。当前拉普拉斯 `(score+2)/(n+4)` 就是 `Beta(2,2)` 特例，加先验是纯增量，无先验时逐字节同 v2.1。

**`design_review_config.json` 配 mode 三态**：
```jsonc
{
  "model_reliability_prior": {
    "mode": "builtin",   // none(禁用) | builtin(官方 preset，默认) | custom(用户自填)
    "custom": {}         // mode=custom 时 {label:{dim:{r,kappa}}}，在 builtin 基础上覆盖
  }
}
```
- **`builtin`（默认）**：读 `presets/model_reliability_prior.yaml`。**今天为空 = 等同 v2.1**（零 breaking、无数值）；等阶段2 probe-task calibration 跑出 `official-prior-v1.yaml` 替换后，**所有用户配置不改自动生效**。
- **`custom`**：builtin + 用户 `custom` 覆盖。想立即用自己的经验先验（不用等官方）：
  ```jsonc
  "model_reliability_prior": {"mode": "custom", "custom": {
    "gpt-4o": {"planner": {"r": 0.55, "kappa": 10}}
  }}
  ```
  `r`=可靠性 0~1（参考 [JudgeBench](https://arxiv.org/abs/2410.12784) per-domain + 你的经验；本地 feedback 会收敛覆盖它），`kappa`=先验强度伪计数（10~20，越大冷启动锚越强）。
- **`none`**：完全禁用。

**为什么 v2.2 不自带数值**：placeholder 主观数据会过期（GPT-5→5.2→6）且易成"伪权威"。机制做完整，等阶段2 probe-task calibration（golden set + CI 跑模型算 per-(model,dim) accuracy）生成有依据的 official 先验。在那之前，`custom` 让你/团队即时用自己的经验。

## 开发

```bash
uv run pytest tests/        # 测试（mock ModelBackend，不调网）
uv run ruff check .         # lint
```

push/PR 自动触发 GitHub Actions（`.github/workflows/ci.yml`）：Python 3.10/3.11/3.12 矩阵跑 ruff + pytest。

## License

Apache-2.0
