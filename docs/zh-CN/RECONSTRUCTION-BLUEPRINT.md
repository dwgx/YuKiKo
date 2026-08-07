# YuKiKo 工程拆迁改造说明书

> **版本**：v0.2（2026-08-08）
> **目标**：把 YuKiKo 拆掉重组为「QQ 群里活着的 AI」—— **AstrBot 的 QQ API 接入层 + Hermes 的记忆与错误处理 + OpenClaw 的工具/扩展性/可玩性 + WorkBuddy 的技能生态**，保留 YuKiKo 自己最强的「模型驱动意图」内核（PromptNavigator 分区菜单 + AgentLoop）。
>
> **来源**：本说明书基于 **8 份一手来源研究**（OpenClaw 记忆/技能两份、Hermes、AstrBot 平台层、WorkBuddy、横向对照 AutoGPT/LangGraph/Claude Code/Dify、OneBot/NapCat 协议、记忆最佳实践 MemGPT/LangMem/Zep/Anthropic）与 YuKiKo 现有代码盘点。v0.2 新增：横向对照结论（§4.5）、记忆最佳实践（§4.6）、OneBot/NapCat 协议附录（§9）、各阶段可执行的接口/阈值规格。
>
> **性质**：这是设计蓝图，不是已完成的代码。每一条改造都要按项目铁律落地：**先写测试、基线红、改完绿**。

> **实施进度（2026-08-08）**：Phase 0.5a/b、Phase 1a/b/c、Phase 3a/b/c、Phase 4a/b、Phase 5a/b（核心）已实现，全量测试 **1665 passed / 0 failed**。新增模块：`core/memory_promotion.py`、`core/loop_guard.py`、`core/skill_loader.py`、`core/tool_call_repair.py`、`core/context.py`、`core/recall_intent.py`、`core/mcp_client.py`、`core/agent_checkpoint.py`。Phase 2（平台拆迁）需真机验证（NapCat + 授权群），代码骨架待做。

---

## §0 结论先行

**目标架构一句话**：一个可以"活着"在 QQ 群里的 AI —— 平台可插拔（AstrBot）、记忆有层级和晋升门（OpenClaw+Hermes）、能力可以无限增长且安全可审计（SKILL.md + MCP + loop 守卫）、内核只做结构判断、语义判断全交给模型（YuKiKo 现有哲学，强化）。

**拆什么、留什么、换什么**：

| 层 | 现状（YuKiKo） | 目标 | 动作 |
|---|---|---|---|
| 平台接入 | `app.py` + NoneBot2 + OneBot V11 硬绑定 | AstrBot `Platform` 抽象 + `MessageChain` + `EventBus` | **换** |
| 事件队列 | `GroupQueueDispatcher`（per-conversation 串行 + 智能打断） | 保留 + 补 OpenClaw 的 `collect`/`drop:summarize` | **留+补** |
| 注意力门 | `TriggerEngine`（not_directed / ai_listen） | 保留，语义归模型 | **留** |
| Agent 内核 | `AgentLoop`（run / system prompt / 工具调用 / 高风险守卫） | 保留 + 补 Hermes 错误回喂自纠 | **留+改** |
| 意图分区 | `PromptNavigator`（20 分区、scoped_tools、_preselect） | **核心资产，保留强化** | **留** |
| 工具系统 | `AgentToolRegistry`（180 个硬编码工具，按分区可见） | SKILL.md 声明式技能 + 渐进式披露 + MCP 连接器 | **改+扩** |
| 记忆 | `MemoryEngine`（profile / embeddings / 知识库平铺） | OpenClaw 分层（Curated/Episodic/Knowledge）+ 晋升门 + provenance | **改** |
| 插件 | `PluginRegistry`（Plugin 类自动发现） | 保留，对齐 AstrBot `Star` 的事件钩子语义 | **留+改** |
| WebUI | `core/webui.py` + React 18 | 保留，补 skill 市场 / 记忆可视化 | **留+扩** |
| 审计 | `core/audit.py`（五条 JSONL 流） | 保留，补 OpenClaw 哈希链审计（高危操作） | **留+扩** |

---

## §1 为什么拆：现状痛点（用四个项目对照）

### 1.1 平台层是死结 —— 换 AstrBot
- 现状：`app.py` 直接 `from nonebot import on_message`、`from nonebot.adapters.onebot.v11 import Bot, Event`，平台语义焊死在接入层。
- 痛点：只能接 OneBot/NapCat，换平台等于重写 `app.py`；`MessageSegment` 进、`MessageSegment` 出，中间全是平台类型。
- 对照：AstrBot 的 `Platform` 抽象只有 6 个方法（`run/meta/terminate/send_by_session/commit_event/create_event`），`@register_platform_adapter` 注册，`PlatformManager` 管理生命周期；进出都是平台无关的 `MessageChain` 组件（Plain/Image/At/Reply/…）。**换平台 = 新增一个 adapter 文件**。

### 1.2 记忆平铺、无来源分级 —— 换 OpenClaw 分层 + Hermes 原则
- 现状：`MemoryEngine` 里 `_user_profiles` / `_history` / `_vector_buffer`(embeddings) / KnowledgeBase 是四块平铺存储，写入没有"来源可信度"分级，任何一条 `add_message` 都能进 profile/embeddings。
- 痛点：群聊里陌生人的起哄、错误话术、机器占位符会污染长期记忆（实测同一句错误话术存 37 次）；召回时 0.000 相似度也进 prompt。
- 对照：OpenClaw 按 **provenance（owner/agent/untrusted/system）** 分级，`untrusted` 来源**结构性排除**在策展层之外；cron/心跳/子 agent 会话不产生可晋升记忆；注入内容永不重新抽取。Hermes 的「结果未返回不得臆造」是反记忆污染的 prompt 原则。
- 收益：**陌生人说的话永远进不了长期策展记忆，无论被召回多少次** —— 这直接治住"群里起哄把 bot 带偏"。

### 1.3 180 个硬编码工具不可扩展 —— 换 SKILL.md 技能生态
- 现状：`AgentToolRegistry` 注册 180 个 Python 函数，每加一个能力 = 写一个 handler + schema + 进分区。
- 痛点：社区/用户不能自己加能力；工具 schema 和实现耦合；180 个工具让模型（尤其弱模型）选择退化。
- 对照：OpenClaw / WorkBuddy 用 **SKILL.md**（YAML frontmatter + markdown 指令体）声明式定义技能，兼容 Anthropic Agent Skills 开放标准（agentskills.io），通过注册表分发（ClawHub）。**渐进式披露**：启动只注入名称+描述，命中触发词才加载全文。条件门控 `requires.{bins,env,config,os}` 按环境事实过滤。
- 收益：**能力可增长、可分发、可审计**，且每回合上下文开销可控。

### 1.4 弱模型坏 JSON / 绕圈 —— 换 Hermes 错误回喂 + OpenClaw loop 守卫
- 现状：弱模型降级（haiku）时 `agent_tool_args_recovered_from_malformed_json` 高发；`_normalize_tool_args` 在代码侧猜参数（方向相反）。
- 对照：Hermes 把校验/执行异常 **traceback 原文回喂**让模型自纠（代码不修补），只设迭代上限。OpenClaw 的 post-compaction loop 守卫拦「重复 `(tool,args,result)` 三元组」防无上限烧 token。
- 收益：**弱模型也尽量自纠，而不是靠代码猜**；长会话不掉链、不烧钱。

---

## §2 目标架构蓝图

```
┌─────────────────────────────────────────────────────────────┐
│  接入层（AstrBot）                                            │
│  PlatformManager                                              │
│  ├─ AiocqhttpAdapter (OneBot/NapCat 反连 WS)                  │
│  ├─ <未来: 任意平台 adapter>                                    │
│  └─ MessageChain 组件（Plain/Image/At/Reply/…）平台无关总线     │
└──────────────────────────────┬──────────────────────────────┘
                               │ 统一 AstrMessageEvent
┌──────────────────────────────▼──────────────────────────────┐
│  内核（YuKiKo 保留强化）                                       │
│  EventBus(替换为 GroupQueueDispatcher)                        │
│   ├─ TriggerEngine 注意力门（语义判断归模型，只喂结构事实）        │
│   ├─ GroupQueueDispatcher（per-conversation 串行 + 智能打断     │
│   │    + 新增 collect/降级摘要）                                │
│   └─ AgentLoop.run(ctx)                                       │
│        ├─ PromptNavigator 分区菜单（20 区，每区 ≤20 工具）       │
│        ├─ <scratch_pad> GOAP 自由推理区（Hermes 式，只提示不解析）│
│        ├─ 工具调用：错误原文回喂自纠（Hermes 式）+ 迭代上限        │
│        └─ 高风险确认握手（保留）                                │
└──────────────┬─────────────────────────────┬────────────────┘
               │ 调用                          │ 读写
┌──────────────▼─────────────┐   ┌────────────▼────────────────┐
│  能力层                     │   │  记忆层（OpenClaw 分层）      │
│  AgentToolRegistry(保留)    │   │  Curated: _user_profiles     │
│  + SKILL.md 技能加载器       │   │    + provenance 分级 + 晋升门  │
│  + MCP 客户端               │   │  Episodic: embeddings 向量    │
│  + PluginRegistry(Star式)   │   │    + 检索双车道(0模型/recall)  │
│  + 渐进式披露(描述→详情)      │   │  Knowledge: KnowledgeBase    │
└──────────────┬─────────────┘   │    + 写时质量门(已有)           │
               │                  └────────────┬────────────────┘
               │                  ┌────────────▼────────────────┐
               │                  │  可观测                      │
               └──────────────────►  AuditTrail 流 + 哈希链审计    │
                                  │  WebUI（React 保留 + skill 市场）│
                                  └───────────────────────────────┘
```

---

## §3 现状盘点与去留决策（模块级）

### 3.1 保留（核心资产，不改行为）
- **`core/prompt_navigator.py` PromptNavigator** —— 分区菜单 + `scoped_tools` + `_preselect`，这是"模型驱动意图"的成熟实现，是 YuKiKo 与其它项目的本质差异。**只扩不改**。
- **`core/agent.py` AgentLoop** —— agent 循环、系统 prompt 装配、navigator 交互、高风险守卫。**保留主循环**，只改工具错误回喂（见 §4.2）。
- **`core/queue.py` GroupQueueDispatcher** —— per-conversation 串行 + 智能打断。**保留**，补 OpenClaw 的 `collect` 合并语义。
- **`core/trigger.py` TriggerEngine** —— 注意力门。**保留**，语义判断已归模型。
- **`core/audit.py`** —— 五条 JSONL 审计流。**保留**，补哈希链。
- **`core/memory.py` 的已有加固** —— 连接按 db_path 键控、embeddings 去重、召回 min_score 门、审计流。**全部保留**，在此基础上加分层。
- **`core/webui.py` + React 前端** —— 管理面板。**保留**。

### 3.2 换掉（平台层）
- **`app.py` 的 NoneBot 绑定**（`on_message`/`on_metaevent`/`MessageSegment`）→ AstrBot `Platform` 抽象 + `MessageChain`。`register_handlers` 换成 `PlatformManager` 注册 adapter。
- **`app_helpers.py` 的 OneBot 段渲染** → `MessageChain` 序列化。发送保护（token bucket、group send block、bot suspension）**保留**，改为 adapter 无关的中间件。

### 3.3 改造（能力层 / 记忆层）
- **工具注册** → 在 `AgentToolRegistry` 之上叠 SKILL.md 加载器（见 §4.3）。180 个内置工具保留为"内置技能"。
- **记忆** → 加 provenance 分级 + 晋升门（见 §4.1）。
- **插件** → 保留 `PluginRegistry`，对齐 AstrBot `Star` 的事件钩子（`@register_command`/`@register_regex`/`@register_llm_tool`）。

---

## §4 四个外部项目的精华融入规格

### 4.1 OpenClaw → 记忆分层 + 晋升门 + 上下文与循环守卫

**（1）记忆来源分级（provenance）—— 四个字段，由分类代码写入**
- 每条可晋升记忆携带四字段（对齐 OpenClaw `memory-schema-provenance.ts`）：
  `origin_class ∈ {owner, agent, untrusted, system}`、`session_kind ∈ {interactive, cron, heartbeat, subagent, unknown}`、`observed_at: ISO8601`、`supersedes_key: str|None`。
- 规则：`origin_class=untrusted` 或 `system` **结构性排除**出 Curated 层（preferred_name、explicit_facts、agent_policies），无论被召回多少次；只能进 Episodic 层。`session_kind` 非 `interactive` 的会话产物（cron/心跳/子 agent）**不产生可晋升候选**。
- **防搭车**：flush 时按文件级 hash 记录**最低信任**——文件含 untrusted 内容则整文件降级为 untrusted（防 untrusted 搭 trusted 文件晋升）。
- 落点：`core/memory.py` 写 profile/embeddings 时带上 `origin_class/session_kind/observed_at/supersedes_key`；`set_preferred_name` 走 `agent` 来源。
- 对齐铁律：来源是**结构事实**，不是语义判断 —— 归代码。

**（2）双层晋升门 —— 确定性门 + 模型 consolidation**
- **门 1（确定性，代码）**：评分公式（对齐 OpenClaw `short-term-promotion-utils.ts` 权重）：
  `score = 0.24*freq + 0.3*avgScore + 0.15*diversity + 0.15*recency + 0.1*consolidation + 0.06*conceptual`
  其中 `freq=log1p(signalCount)/log1p(10)`、`recency=exp(-ln2*ageDays/14)`（14 天半衰）、`consolidation=max(multiDaySpacing, groundedCount/3)`、`diversity=contextDiversity/5`。
  判据：`signalCount >= 3`、`max(unique_queries, recall_days) >= 3`、`maxAgeDays <= 30`、`score >= 0.75`。
- **门 2（模型回合）**：consolidation prompt 要求输出 JSON `{memory, operations:[{candidateKey, action: added|merged|superseded, resultEntry, priorEntries}]}`，逐候选操作、保留精确 Source 引用、禁止改写。输出校验：≤10000 字符、prior 丢失比例 ≤0.25、supersede 必须带 `supersedes_key` lineage。**失败（超时/解析/校验拒绝）回退为 append-only，永不丢记忆**。写盘用乐观并发（content hash 预检 + 原子 rename）。
- 落点：新增 `core/memory_promotion.py`：`rank_promotion_candidates()` + `consolidate_memory()` + `validate_consolidated_memory()`；晋升记录存入 `storage/memory/.dreams/`。

**（3）检索双车道成本护栏**
- **车道 1（零模型）**：排序 = `vectorWeight*vectorScore + textWeight*keywordScore`，再 `× exp(-ln2*age/30)`（30 天半衰，仅 dated 条目；profile 不衰减），再 `× (0.75 + importance*0.05)`（importance 1-10 写入时赋一次）。trigger 词注入：纯词法预过滤，强匹配 ≥0.65，每轮最多 3 条/1800 字符，仅 curated 层可自动注入。
- **车道 2（升级）**：触发条件 = 消息含**回顾意图**（时间/回顾正则）且车道 1 无强命中 → spawn 受限子 agent（`toolsAllow=[memory_search, memory_get]`、超时 + 熔断 + 缓存 TTL），输出纯文本摘要或 `NONE`。
- 落点：`AgentLoop` 里"想起什么"的查询走车道 1，命中弱才烧车道 2。`search_related` 已有 min_score 门，补 importance 乘子 + trigger 词注入。

**（4）压缩前 memory flush + 双重 loop 守卫**
- **压缩前 flush**：`totalTokens >= contextWindow - reserveTokens(16384) - softThreshold` 且未对本次 compaction 刷过 → 静默落盘本轮笔记（按 `compactionCount` 去重）。
- **实时 loop 检测**：工具调用哈希 `name:sha256(stableStringify(args))`；结果哈希剥易变字段（messageId/ts）。阈值：history=30、warning=10（注入提示）、critical=20（阻断该工具）、circuit breaker=30（全局熔断）。同 args 不同结果**不算** no-progress。
- **post-compaction guard**：压缩成功后武装 **3 次尝试窗口**，窗口内 ≥3 次相同 `(toolName, argsHash, resultHash)` 三元组 → 中止。
- 落点：`AgentLoop` 维护 `deque(maxlen=30)` 工具历史 + 新增 `core/loop_guard.py`。

**（5）上下文插件槽**
- `core/context.py` 定义 `Protocol ContextEngine`：`ingest(session_id, message) -> bool` / `assemble(session_id, messages, token_budget, available_tools) -> AssembleResult` / `compact(session_id, token_budget, force, custom_instructions) -> CompactResult` / `after_turn(session_id, messages, pre_prompt_count, token_budget)`。
- 默认实现包装现有 `MemoryEngine`；任一方法异常 → 记录 quarantine 并走降级实现，单会话可恢复。
- 落点：`YukikoEngine.handle_message` 里 ingest（入队后）→ assemble（构建 prompt 前）→ compact（溢出时）→ after_turn（收尾）。

### 4.2 Hermes → 错误回喂自纠 + scratchpad + prompt 原则

**（1）错误回喂自纠（替换代码侧猜参）—— 三类错误模板原文**
- 主循环：每轮解析 `(tool_calls, assistant_message, error)` → assistant 消息**原样追加**（含 scratchpad）→ 构造 `<tool_response>` → 重新推理；停止条件三选一：无 tool_call 且无 error（=最终回答）、达迭代上限、异常。
- **执行异常**模板：`<tool_response>\nThere was an error when executing the function: {name}\nHere's the error traceback: {e}\nPlease call this function again with correct arguments within XML tags <tool_call></tool_call>\n</tool_response>`
- **schema 校验失败**模板：`<tool_response>\nThere was an error validating function call against function signature: {name}\nHere's the error traceback: {message}\nPlease call this function again with correct arguments within XML tags <tool_call></tool_call>\n</tool_response>`
- **解析失败**模板：`<tool_response>\nThere was an error parsing function calls\nHere's the error stack trace: {error_message}\nPlease call the function again with correct syntax</tool_response>`（注意原仓库闭合标签少 `/`，落地时修正）
- **JSON 三级兜底**：`json.loads` → `ast.literal_eval`（容 Python 单引号/裸 True/None）→ markdown 块提取 ` ```json ... ``` `。YuKiKo 走工具调用路径时三级都补上。
- **迭代上限**：沿用 `max_steps` 硬上限 + 现有 `consecutive_tool_errors >= 2` 连续失败护栏，**不开无界循环**。
- **4 条警示**：① 抄模板时修正闭合标签；② `max_depth` 定为单一值（原仓库 CLI=5 与提示"10"不一致）；③ Hermes 用 `*args.values()` 按位置传参是 bug，YuKiKo 必须按参数名传关键字；④ 工具调用路径的 JSON 兜底要补 markdown 提取。
- 现状：`core/agent.py::_normalize_tool_args` 代码侧猜参数（方向相反）。改造为：`_normalize_tool_args` 降级为最后防线，优先错误回喂。
- 落点：`AgentLoop` 工具失败分支 + `core/agent.py::_build_guard_feedback_payload` + `_append_tool_result`。

**（2）`<scratch_pad>` GOAP 自由推理区 —— 只提示不解析**
- 模板（Hermes README 原文）：`<scratch_pad>\nGoal: <复述用户请求>\nActions:\n- {var} = functions.{fn}({param}={value},...)\nObservation: <工具结果摘要；无调用则 None>\nReflection: <评估 query-tool 相关性、参数完备性、任务状态>\n</scratch_pad>`
- 关键：parser **只提取 `<tool_call>`，从不读 scratchpad**；assistant 消息原样留在历史里让模型看到自己的推理轨迹。
- 落点：`PromptNavigator.render_system_block` 分区 instructions 里加"每轮先写一行当前事实+下一步，再发工具调用"，**不改解析**。

**（3）prompt 原则直接抄（`sys_prompt.yml` 原文）**
- "Don't make assumptions about what values to plug into function arguments."（缺参就问，不猜值）
- "Don't make assumptions about tool results if `<tool_response>` XML tags are not present since function hasn't been executed yet."（结果未返回不得臆造）
- "At the very first turn you don't have `<tool_results>` so you should not make up the results."
- "Please keep a running summary with analysis of previous function results and summaries from previous iterations."
- 落点：`core/agent.py::_build_system_prompt` + `config/prompts.yml`（三源同步：Python payload / `master.template.yml` / `prompts.yml`）。

### 4.3 OpenClaw + WorkBuddy → 工具/技能生态（扩展性可玩性）

**（1）SKILL.md 声明式技能（兼容 Anthropic Agent Skills / OpenClaw / WorkBuddy）**
- 目录结构：`skills/<name>/{SKILL.md, scripts/, references/, assets/}`；`SKILL.md` 是激活标记。
- frontmatter 字段（实测 372 份 WorkBuddy skill 频次）：`name`(必填,≤64,`^[a-z0-9-]+$`)、`description`(必填,≤1024)、`description_zh`(双语)、`version`、`homepage`、`user-invocable`(默认 true)、`disable-model-invocation`(默认 false)、`metadata.openclaw.{always, emoji, skillKey, os[], requires{bins,anyBins,env,config}, install[]}`。**注意：`allowed-tools` 不是一等字段**（OpenClaw 不消费，能力门控靠 `requires` + body 指令）；版本用内容哈希 `<version>sha256:<前16hex>` 而非 YAML 字段。
- 加载优先级：extra < bundled < managed < personal < project < workspace（后者覆盖前者）。
- `core/skill_loader.py` 接口：`load_skill(dir) -> Skill|None`（name/description 缺失返回 None）、`render_catalog(skills, max_chars=18000, desc_max=220)`（二分裁剪，截断输出 `⚠️ Skills truncated`）、`evaluate_requires(meta, ctx) -> bool`。
- 180 内置工具保留：每个工具生成 `SKILL.md`（name=工具名，description=工具摘要，body=调用示例），标记 `disable_model_invocation` 或仅进 runtime registry 不进提示目录。

**（2）渐进式披露（两级裁剪）**
- 第一级（OpenClaw）：启动只注入 `<available_skills>` 四元组（`<name>/<description>/<location>/<version>`），description 截到 220 字符、总预算 18000 字符；模型用 `read_skill(name)` 工具命中才读 SKILL.md 全文，`references/` 执行中再读。`always: true` 只跳过 requires 门控，**不预注入 body**。
- 第二级（WorkBuddy）：`SkillRegistry.match(msg) -> [skill_id]`，`_loaded_skill_ids: set` 会话缓存，`ToolSearch(queries, top_k=3)` 返回 `load_state: loaded|cached`，`DeferExecuteTool` 只执行已加载工具。
- 落点：系统提示注入技能索引；新增 `read_skill(name)` agent tool；内置工具 schema 走现有 `scoped_tools()` 分区，技能目录与工具 schema **分开注入**避免互相挤占。

**（3）条件门控 —— requires 判断逻辑**
- `bins`：**全要**（PATH 逐项 + X_OK 探测，PATH 变更清缓存）；`anyBins`：**任一**命中；`env`：逐个非空（`process.env` 或 `skillConfig.env` 或 `primaryEnv===env && apiKey`）；`config`：点路径 truthy（带默认值，拦原型链 key）；`os`：平台名包含。
- `always: true` 短路全部门控。
- 对齐现有：`scoped_tools()` 按 permission_level 收窄 + config-gated 工具注册（`search.tool_interface.github_enable`）。
- 落点：`skill_loader` 复用 `_tool_visible_for_permission`；`bins→ffmpeg/yt-dlp`、`env→API key 名`、`config→config.yml 点路径`。

**（4）MCP 连接器 —— 带信任门**
- `core/mcp_client.py`：读 `mcpServers` 配置（`command/args/env` 或 `{type:streamableHttp, url}`）→ `tools/list` 导入统一工具池（`mcp__{connector}__{tool}`）。
- **信任门**：生命周期 `configured → disconnected → trusted → connected`，未 trusted 不暴露工具；`connector_trust.json` 持久化。
- 落点：新增 `core/mcp_client.py` + `connector_trust.json`；工具进 `scoped_tools()` 可见集。

**（5）validate-then-repair 工具调用修复层**
- 四项通用修复（P0，纯 stdlib 零依赖）：① 剥离 null；② **字符串化 JSON 数组转真数组（必须先于④）**；③ 单键对象解包；④ 裸值包成单元素数组。P1：markdown 自动链接解包（`^\[(.+?)\]\((\1)\)$` 且解包后非 http）。P2：关系不变量（read_file 漏 offset→补 0、漏 limit→补 2000）用"扩展语义 + 透明 `_note`"而非拒绝。
- `apply_repairs(obj, schema_hints) -> (obj, [repairs])` 递归实现；快通道合法输入零开销；遥测 `tool_input_repaired/invalid:{tool}`。
- **优先级**：Hermes 错误回喂自纠优先，repair 作为第二道，`_normalize_tool_args` 猜参降为最后防线。
- 落点：`core/agent.py::_decode_tool_call_arguments` 之后包 `repair_tool_call(args, hints)`；hints 从 input_schema 的 type 提取；按 (model, tool) 计数遥测。

**（6）安全边界（Skill 不含代码）**
- 铁律：**skill 只能是声明式编排，不含可执行代码**（比 OpenClaw 更严，不拿 OpenClaw 当先例）。
- 若将来放宽到允许脚本，必须抄 OpenClaw 的最小控制栈：apply 前重跑 security scanner、hash binding、写前 rollback metadata、curator lifecycle（未用 30 天 stale / 90 天 archived）、bounded failure。
- 落点：`skill_loader` 拒绝含代码执行的技能。

### 4.4 AstrBot → QQ API 接入层

**（1）Platform 抽象 —— 最小三件套**
- `platform/base.py`：`Platform(config, event_queue)`，抽象方法 `run() -> Coroutine` + `meta() -> PlatformMetadata`；默认实现 `terminate()`、`send_by_session(session, message_chain)`、`commit_event(event)`（`event_queue.put_nowait`）、`create_event(message)`、`get_client()`、五态状态机（PENDING/RUNNING/ERROR/STOPPED）。
- `platform/registry.py`：`@register_platform_adapter("aiocqhttp")` + `platform_cls_map`。
- `platform/manager.py`：单实例（只允许一个 aiocqhttp 配置），惰性 import 适配器类 → 实例化 → `create_task(run())` + wrapper 捕获异常进 `record_error`；`reload()`/`terminate()` 先 `await inst.terminate()` 再 cancel 任务。
- 事件队列直接接 YuKiKo 现有 `GroupQueueDispatcher.submit()` 之前，**不引入** AstrBot 的 `EventBus.dispatch` 线程。

**（2）aiocqhttp adapter —— 整体照抄 AstrBot 两文件**
- `platform/adapters/aiocqhttp/adapter.py` 照抄 AstrBot `aiocqhttp_platform_adapter.py`（NapCat/Lagrange 兼容经验是硬经验，别重写）：`CQHttp(use_ws_reverse=True, access_token=...)`、`on_message("group")/on_message("private")/on_request/on_notice` → `convert_message` → `commit_event(create_event(...))`。
- 入站转换：`itertools.groupby` 按段类型分组；text→Plain、file→`get_group_file_url`/`get_private_file_url` 换 url、reply→`get_msg` 取回引用、at→`get_group_member_info` 取昵称（**第一个 @bot 不进 message_str，其余拼 `" @昵称(qq) "`**）、mface 跳过。
- 出站发送：`MessageChain` → OneBot 段（Image/Record→`base64://`、File→`file:` URI），`_dispatch_send` 按 `send_group_msg`/`send_private_msg`/兜底；forward 用 `send_group_forward_msg`；流式按 `[^。？！~…]+[。？！~…]+` 切句。
- `app.py::register_handlers`（L809）删掉，入口改由 adapter `on_message` 回调；去重 hash、`_build_conversation_id`、`private_chat_mode`、`remember_incoming_media` 前置缓存搬进新 `IngressFilter`。

**（3）MessageChain 组件 —— 只移植用到的子集**
- `platform/components.py`：Plain/Image/At/AtAll/Reply/Record/Video/File/Face/Poke/Node/Nodes + `MessageChain`（`squash_plain()` 合并连续 Plain、`get_plain_text()`）+ `ComponentTypes` 段类型映射。
- 媒体组件带 `file/url/path` 三候选源，`convert_to_base64()` 统一归一化；`Reply` 出站只发 `{type:"reply", data:{id}}`。
- 引擎侧 `EngineMessage` 保持不动或加薄适配层；转换只在适配器两侧封闭。

**（4）事件总线 —— 9 段映射而非移植**
| AstrBot 阶段 | YuKiKo 对应 |
|---|---|
| WakingCheck | `TriggerEngine` 注意力门 |
| WhitelistCheck | `core/admin.py` 群白名单 |
| SessionStatusCheck | `GroupQueueDispatcher` 会话状态 |
| RateLimit（入站） | 入站侧新加（现有 `_TokenBucket` 是出站） |
| ContentSafetyCheck | 现有安全检查 |
| PreProcess（媒体归一化/STT） | 入站媒体/语音路径（`_prepare_voice_audio_file` 等） |
| Process（插件→agent） | `_try_agent_path` |
| ResultDecorate（t2i/TTS/分段） | 语义切分（`core/chat_splitter.py`） |
| Respond（分段/流式发送） | `SendGuard`（`_get_send_bucket`/`_check_group_send_block`/`_check_bot_send_suspended` 组合） |

**（5）拆 AstrBot 的干净边界（研究结论）**
- 干净可拆：`platform/sources + PlatformManager + MessageChain 组件`。
- 耦合点（需重写）：`AstrMessageEvent` 里焊死的 LLM 依赖（`request_llm`/`ToolSet`/`Conversation`/`db_helper`）——拆出时只留平台一半（`unified_msg_origin`/`get_messages`/`send`/`send_streaming`），LLM 一半由 YuKiKo 侧实现。
- **迁移顺序**：先抄 `components.py` 子集 → 再抄 adapter 两文件 → 建 `SendGuard` 包住发送保护 → 最后把 `register_handlers` 换成 adapter 回调。

### 4.5 横向对照：AutoGPT / LangGraph / Claude Code / Dify（研究结论）

**四个框架各一段核心架构**（一手来源：AutoGPT `blocks/_base.py` 数据流 DAG；LangGraph `graph/state.py` + Pregel 运行时图状态机；Claude Code 官方文档纯 ReAct + hooks/ToolSearch；Dify `core/agent` 双轨 runner+workflow）：

| 框架 | 核心范式 | 一句话 |
|---|---|---|
| AutoGPT | 数据流 DAG | agent = 块（Block 输入/输出 schema）+ 有向边，LLM/代码/人工都是块 |
| LangGraph | 图状态机 | 状态 TypedDict + channel reduce，节点函数收/返增量，checkpoint 可恢复/时间旅行 |
| Claude Code | 纯 ReAct + 外部约束层 | 模型产工具调用→执行→回灌直到纯文本；hooks 进程内不进上下文；ToolSearch 按需加载 schema |
| Dify | 双轨 | 传统 agent runner（ReAct/FC）+ 可视化 workflow 引擎（固定节点类型），两轨互为节点 |

**对 YuKiKo 的 5 个判断**：
1. **循环范式**：AgentLoop 已是标准 ReAct，群聊短任务够用；多步条件分支/可中断恢复才值得上 graph——**不值得**用 LangGraph/AutoGPT 重写内核（破坏 180 工具/20 分区可观测性）。
2. **工具选择**：Claude Code 的 **ToolSearch 按需延迟加载 schema**（按当前上下文动态拉取）值得借鉴，是对渐进式披露的补充；**deny 即移除工具**（按群/权限把工具彻底移出上下文）值得做。
3. **状态记忆**：LangGraph 的**逐超步 checkpoint**（可恢复）正对 QQ 群超时重试场景；Claude Code 的"索引+按需读主题文件+注入限额"是轻量记忆成熟形态。
4. **人机协作**：Claude Code hooks 的 **PermissionRequest/PreToolUse 审批 + 入参改写 + 退出码语义**最值钱——可做成"高危工具调 QQ 私聊/卡片异步问管理员审批"，结构化替代 `_guard_high_risk_tool_call`。
5. **Dify workflow**：对"意图不可预测的群聊"主路径意义有限；只对固定流程型功能（每日简报）可做低代码插件，**不宜进内核**。

**落地**：① AgentLoop 加轻量 checkpoint（每步快照+超时恢复）；② hooks 生命周期模型移植为高危工具审批钩子；③ 记忆注入改"索引+按需+限额"；④ 不引入 graph 重写、Dify workflow 仅作可选插件。

### 4.6 记忆最佳实践：MemGPT / LangMem / Zep / Anthropic（研究结论）

**四个范式的核心判断**：

1. **MemGPT/Letta（虚拟上下文 + 自我编辑记忆）**：main/external 两级，模型通过函数调用编辑记忆（`core_memory_append/replace`、`rethink_memory` 整块重写）。**结论**：YuKiKo 已有同哲学工具面（memory_list/add/update/delete + 强制 note + scope 守卫），缺的是 **heartbeat 定期自整编**（`rethink_memory` 式整块重写）——`write_daily_snapshot` 是后台摘要不是记忆自整理。
2. **Zep/Graphiti（时序知识图谱）**：事实边带 valid_from/valid_until，变更作废旧边不删，provenance 回溯源 episode。**结论**：YuKiKo 的 `knowledge_store` 表（entity/relation/value + valid_from/valid_until + confidence）已是简化时序模型，但**缺自动抽取**（`knowledge_upsert` 全仓库零调用、恒空）。**不值得引入图数据库**——群聊记忆规模小、查询简单，SQLite 三元组 + 时序字段已够，重点是补自动抽取。
3. **LangMem（记忆类型学 + 双通道）**：semantic（事实）/episodic（成功经验）/procedural（行为规则）+ conscious（热路径即时写）/subconscious（后台延迟提炼、可去抖取消）。**结论**：YuKiKo 现有 = semantic ✓ + procedural ✓（agent_policies/directives），**episodic 最弱 ✗**——只有文本摘要没有"上次怎么处理有效"的结构化范例。补 episodic 收益最大（把有效回复存为 few-shot 范例）。
4. **Anthropic（最小高信号上下文）**：结构化笔记写到上下文外再拉回、compaction、渐进披露、子 agent 返回浓缩摘要。**结论**：YuKiKo 每回合自动注入 profile+KB 是"预取"方向对，但**三段拼接无 token 预算上限，应加护栏**。

**落地**：① 保留混合模式（profile 自动注入 + 写记忆走工具，正是 LangMem conscious + Anthropic 预取）；② 补 episodic 结构化范例（few-shot）；③ `knowledge_upsert` 接线自动抽取（或明确废弃恒空表）；④ daily snapshot 演进为"去抖 + 自整编"（MemGPT heartbeat 模式）；⑤ 记忆注入加 token 预算上限。

---

## §5 拆迁改造阶段路线

> 每阶段结束必须：全量 `pytest` 0 failed + `project_takeover_selfcheck.py` PASS + ruff 无新增。顺序按"风险从低到高、可回滚"排。

### Phase 0 — 地基：记忆来源分级 + 类型学补强（不改平台，纯记忆加固）
- `core/memory.py` 写 profile/embeddings 加四字段：`origin_class ∈ {owner, agent, untrusted, system}`、`session_kind ∈ {interactive, cron, heartbeat, subagent, unknown}`、`observed_at`、`supersedes_key`。
- 新增 `core/memory_promotion.py`：`rank_promotion_candidates()`（评分公式见 §4.1（2），阈值 0.75）+ `consolidate_memory()` + `validate_consolidated_memory()`（≤10000 字符、丢失比 ≤0.25、失败回退 append-only）。
- `untrusted`/`system` 结构性排除出 Curated 层；非 `interactive` 会话不产生晋升候选。
- **类型学补强**：补 episodic 结构化范例（把有效回复存 few-shot 范例）；记忆注入加 token 预算上限（§4.6）。
- 写测试：陌生人起哄进不了长期记忆；模型显式 learn 进；注入内容不重提取；episodic 范例注入。
- **验收**：全量 0 failed + 新增回归测试绿。

### Phase 1 — 自纠与守卫（不改平台，改 agent 行为）
- Hermes 三类错误回喂（§4.2 模板，修正闭合标签）+ JSON 三级兜底（含 markdown 提取）+ `consecutive_tool_errors >= 2` 连续失败护栏。
- `core/agent.py` 工具参数按**关键字传参**（不学 Hermes 的 `*args.values()` bug）；`_normalize_tool_args` 降为最后防线。
- GOAP `<scratch_pad>` 提示（纯 prompt，不解析）。
- `core/loop_guard.py`：实时检测（warning 10 / critical 20 / 熔断 30）+ post-compaction guard（窗口 3）。
- 压缩前 memory flush（按 `compactionCount` 去重）。
- 写测试：错误回喂后模型重调；重复三元组中止；上下文截断前落盘。
- **验收**：全量 0 failed + 新测试绿。

### Phase 2 — 平台拆迁（风险最高，单独一个里程碑）
- 迁移顺序：抄 `platform/components.py` 子集 → 抄 `aiocqhttp` adapter 两文件 → 建 `SendGuard` 包住发送保护 → 删 `app.py::register_handlers` 换成 adapter 回调。
- `platform/base.py` + `registry.py` + `manager.py`（单实例）；事件队列接 `GroupQueueDispatcher.submit()` 之前。
- OneBot V11 反向 WS：`/onebot/v11/ws`、握手头 `X-Self-ID` + `Authorization: Bearer <token>`、`messagePostFormat:"array"`（详见 §9 附录）。
- 发送保护（token bucket/group block/bot suspension）迁为 adapter 无关中间件；`SendGuard` 组合 `_get_send_bucket`/`_check_group_send_block`/`_check_bot_send_suspended`/语义切分。
- 入站 `IngressFilter`（去重 hash、`_build_conversation_id`、`private_chat_mode`、`remember_incoming_media`）替代 `app.py` L1460-1519。
- 这一步**不新增能力**，只换管道，行为等价。
- **验收**：机器人能收发消息与现状一致 + 全量测试（平台相关测试更新）+ 真机 smoke（需 owner 授权群号）。

### Phase 3 — 技能生态（扩展性可玩性）
- `core/skill_loader.py`：SKILL.md 加载（name/description 缺失丢弃、加载优先级）+ `render_catalog`（desc≤220、预算 18000、二分截断）+ `evaluate_requires`（bins/anyBins/env/config/os/always）+ `read_skill(name)` agent tool。
- 180 内置工具生成 SKILL.md（`disable_model_invocation` 或仅 runtime registry）。
- `core/mcp_client.py` + `connector_trust.json` 信任门（configured→disconnected→trusted→connected）。
- `core/agent.py` 包 `repair_tool_call(args, hints)`（P0 四项修复 + P1/P2，见 §4.3（5）），优先级 Hermes 回喂 > repair > `_normalize_tool_args`。
- **验收**：新技能不加代码即可注册并调用；180 内置工具照常；全量 0 failed。

### Phase 4 — 检索双车道 + 记忆可视化 + 上下文插件槽
- 车道 1：`search_related` 补 importance 乘子 + trigger 词注入（纯词法预过滤，强匹配 ≥0.65，每轮 ≤3 条/1800 字符）。
- 车道 2：消息含回顾意图且车道 1 无强命中 → spawn 受限 recall 子 agent（toolsAllow=[memory_search, memory_get]、超时+熔断+缓存 TTL），输出摘要或 `NONE`。
- `core/context.py`：`ContextEngine` Protocol（ingest/assemble/compact/after_turn）+ 默认实现包装 MemoryEngine + quarantine 降级。
- WebUI 补记忆分层可视化 + skill 市场列表。
- **验收**：全量 0 failed + WebUI 测试绿。

### Phase 5 — 插件对齐 AstrBot Star 语义 + 高危审批钩子 + 收尾
- `PluginRegistry` 对齐 `@register_command`/`@register_regex`/`@register_llm_tool` 事件钩子（AstrBot Star 语义）。
- Claude Code hooks 生命周期模型移植为高危工具审批钩子（PermissionRequest/PreToolUse，结构化替代 `_guard_high_risk_tool_call`，见 §4.5）。
- AgentLoop 加轻量 checkpoint（每步快照 + 超时恢复）。
- 清理死代码、归档旧架构文档、更新 HANDOFF。
- **验收**：全量 0 failed + 两个自检 PASS + 真机 smoke。

---

## §6 风险与兼容性

1. **平台拆迁是最大风险**：Phase 2 动 `app.py`，牵动发送保护、媒体、语音路径。必须做"行为等价替换"，分步验证（先接收入、再接发送、再媒体），每步真机 smoke。OneBot/NapCat 的坑见 §9.7（富媒体 retcode=1200 拒收、语音 silk、at 边界）。
2. **错误回喂自纠在弱模型上可能死循环**：必须有迭代上限 + 上限后回落旧逻辑（代码兜底或直接失败）。
3. **SKILL.md 生态引入"技能膨胀"**：渐进式披露必须有硬闸（description ≤220、catalog ≤18000 二分截断、`read_skill` 命中才加载），否则回归 180 工具老问题。
4. **记忆来源分级会改变现有行为**：之前"群友起哄进了记忆"的现象会消失，需要 owner 确认这是想要的（设计意图是：陌生人起哄不该影响长期人格/称呼）。
5. **测试兼容**：1548 个测试中平台相关（app.py/OneBot）和记忆相关会因行为变更更新。按项目惯例：保留测试名、反转断言，不删测试。
6. **AstrBot 依赖**：`aiocqhttp` 是否已装？`requirements.txt` 钉版本。避免引入 NoneBot 之外的平台耦合（用 AstrBot 只是拆它的 platform 层，不引它的 WebUI/agent）。
7. **混合重写的边界纪律**：明确分层归属——平台层（`platform/`、`core/message/`）+ 能力层（`core/skill_loader.py`、`core/mcp_client.py`）是**新代码**；内核（`core/agent.py`、`core/prompt_navigator.py`、`core/queue.py`、`core/engine.py::handle_message/TriggerEngine`）是**保留加固**。git 上保持"内核文件不动"纪律，动前先 `git status` 确认无别人在改。
8. **checkpoint 恢复（Phase 5）会增加每步开销**：只对超时重试场景启用，不做全量时间旅行。
9. **记忆注入 token 预算**：§4.6 指出三段拼接无上限，Phase 0 就加护栏，避免新机制引入后失控。

---

## §7 验收标准（总）

- 全量 `python -m pytest -q` → **0 failed**（通过数会涨，不写死）
- `scripts/project_takeover_selfcheck.py` → **status=PASS**
- `scripts/agent_deep_selfcheck.py` → **exit 0**
- ruff → 无新增告警
- 真机（NapCat 在线 + owner 授权群）：收/发/媒体/语音 与重构前行为一致，且群友起哄不再污染长期记忆
- 新增能力可验证：加一个 SKILL.md 技能（无 Python 代码）→ 机器人能调用

---

## §8 一手来源索引

| 项目 | 来源 |
|---|---|
| OpenClaw 记忆/晋升门 | `github.com/openclaw/openclaw`：`extensions/memory-core/src/short-term-promotion.ts`、`dreaming-consolidation.ts`、`extensions/active-memory/{trigger-recall,escalation,recall}.ts`、`packages/memory-host-sdk/src/host/memory-schema-provenance.ts`、`docs/concepts/memory-architecture.md` |
| OpenClaw SKILL/上下文 | 同上：`src/skills/loading/frontmatter.ts`、`workspace.ts`、`skill-contract.ts`、`src/shared/config-eval.ts`、`src/context-engine/types.ts`、`src/agents/{compaction-planning,tool-loop-detection}.ts`、`packages/agent-core/src/harness/compaction/compaction.ts` |
| Hermes | `github.com/NousResearch/Hermes-Function-Calling`：`functioncall.py`（recursive_loop L102-157 / 错误模板 L127-145）、`validator.py`（JSON 三级兜底）、`prompt_assets/sys_prompt.yml`、`README.md` L200-228（scratchpad） |
| AstrBot | `github.com/AstrBotDevs/AstrBot`：`astrbot/core/platform/{platform,register,manager}.py`、`sources/aiocqhttp/aiocqhttp_platform_adapter.py`、`astrbot/core/message/components.py`、`astrbot/core/event_bus.py`、`pipeline/scheduler.py` |
| WorkBuddy | `github.com/infometa/workbuddyskills`（295 skills/78 connectors）、`github.com/yinqd3/workbuddy-skills`（tool-call-repair 零依赖实现）、`github.com/adongwanai/learn-workbuddy`（24 章复刻）、workbuddy.cn 官方文档 |
| 横向对照 | AutoGPT `github.com/Significant-Gravitas/AutoGPT`、LangGraph `github.com/langchain-ai/langgraph`、Claude Code `code.claude.com/docs`（hooks/memory/agent-sdk）、Dify `github.com/langgenius/dify` |
| 记忆最佳实践 | MemGPT 论文 arXiv:2310.08560 + `github.com/lettucebot/letta`、Graphiti 论文 arXiv:2501.13956 + `github.com/getzep/zep`、`github.com/langchain-ai/langmem`、Anthropic `anthropic.com/engineering/effective-context-engineering-for-ai-agents` |
| OneBot/NapCat | OneBot 11 规范 `github.com/botuniverse/onebot-11`、NapCat 文档 `github.com/NapNeko/NapCatDocs` |

---

## §9 附录：OneBot V11 + NapCat 协议规格（Phase 2 自研 adapter 用）

> 自研 adapter 需自建 `/onebot/v11/ws` 路径的 WS server（现由 NoneBot2 onebot v11 adapter 注册），实现"事件→内部消息"与 `{action,params,echo}` 分派即可摘掉 NoneBot2。以下按一手来源（OneBot 11 规范 + NapCat 文档 + 本仓实现核对）整理。

### 9.1 传输（反向 WS）
- NapCat 作客户端连 `ws://<host>:<port>/onebot/v11/ws`，Universal 单连接承载事件+API。
- 握手头：`X-Self-ID: <bot QQ>`、`X-Client-Role: Universal`；鉴权 `Authorization: Bearer <token>` 或 `?access_token=`。
- 事件 = 服务端下行 JSON；API = 上行 `{"action","params","echo"}`，回 `{"status":"ok|failed|async","retcode":0,"data":…,"echo":…}`；`echo` 原样回，用于关联 trace_id。
- retcode：0 成功、1404 action 不存在、1400/1401/1403/1404 对应 HTTP 4xx。断线重连由 NapCat `reconnectInterval`(默认 5000ms) 驱动。
- **必须设 `messagePostFormat:"array"`**，否则事件里 message 是 CQ 字符串。

### 9.2 事件（共同字段 time/self_id/post_type/user_id）
- **message**：`message_type=group|private`；group 带 `group_id`、`sub_type=normal/anonymous/notice`、`sender{user_id,nickname,card,role}`；private 带 `sub_type=friend|group|other`。`message`=段数组、`raw_message`=原始串、`message_id`（NapCat 为哈希正整数，**非递增**）。
- **notice**：`group_upload{file{id,name,size,busid}}`、`group_admin{sub_type=set|unset}`、`group_decrease{operator_id,sub_type=leave|kick|kick_me}`、`group_increase{operator_id}`、`group_ban{duration}`、`friend_add`、`group_recall{operator_id,message_id}`、`friend_recall{message_id}`、`notify/sub_type=poke|lucky_king|honor{target_id}`。
- **request**：`request_type=friend|group`，`flag` 必填回执（`set_friend_add_request`/`set_group_add_request`）。
- **meta**：lifecycle/heartbeat。

### 9.3 发送 API
- `send_group_msg{group_id,message}` / `send_private_msg{user_id,message}` / `send_msg` → `data:{message_id}`；`delete_msg`；`set_group_card`；`get_msg`。
- NapCat 扩展：`send_group_forward_msg{group_id,messages:[node]}`、`send_forward_msg`、`send_group_sign`、`send_poke`、`get_group_msg_history`。支持 `_async`/`_rate_limited` 后缀（限流队列 500ms）。
- 所有回包统一 `{status,retcode,data,echo}`。

### 9.4 富媒体段
- 段 JSON `{"type","data"}`。image/record/video 的 `file` 收：`file://<绝对路径>`、`file://<hash数字>`（NapCat 内部资源 id）、`base64://`、`data:`、http(s)。`url` 仅收。
- **at**：`qq=<号>|all`；**at 必须独立一段，后接文本另起 text 段**（插中间乱边界）。
- **reply**：`{id}`。**forward** 收 `{id}` → `get_forward_msg`；node 发 `{id}` 或 `{user_id,nickname,content:[段数组]}`。
- image `url` 约 2h 过期（`url expired`），用 `get_image`/`get_msg` 刷新或 `nc_get_rkey` 换 rkey。`get_image{file}`→本地路径；`get_record{file,out_format}`→转码路径。视频>100MB 须走群文件。

### 9.5 文件/语音
- `get_group_file_url` 参数是 **`{file_id, group}`**（不是 group_id）、`get_private_file_url{file_id}`、`get_file{file_id|file}`、`get_group_root_files`/`get_group_files_by_folder`、`upload_group_file`。
- **语音是 silk**：ffmpeg 无 silk demuxer，接收 URL 是 raw silk → 用 `pilk.decode`（出 24kHz PCM→WAV，`utils/media.py`）；发送 NapCat 自动转 silk，但直接发 `.silk` 不可靠——先找同名 mp3/m4a/wav 替身再 `file://` 发。长音频(>60s) 须切片。

### 9.6 NapCat 接入
- 配置 `napcat/config/onebot11_<QQ>.json`（v4.5.3+ 可 `onebot11.json` 兜底）：`network.websocketClients[{name,enable,url,messagePostFormat:"array",reportSelfMessage:false,reconnectInterval:5000,token,debug,heartInterval:30000}]`。
- macOS 实际目录：`~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/NapCat/config`。**QQ 必须带 `--no-sandbox` 启动 NapCat 才加载**（loadNapCat.js 第一行查 argv），GUI 双击无效（`scripts/napcat_macos.sh`）。WebUI 6099。token 接 `.env` 的 `ONEBOT_ACCESS_TOKEN`。

### 9.7 QQ 平台坑汇总（adapter 必须处理）
1. **富媒体拒收**：`ActionFailed(retcode=1200, EventChecker Failed: rich media transfer failed)`——QQ 侧限流/校验，连发失败会连拒 19 条后暂停。adapter 须把 retcode≠0 当失败并做退避/暂停（YuKiKo 已有 `safe_send` 暂停）。
2. **语音 silk**：接收 raw silk ffmpeg 不可解，须 pilk；发送须本地文件转码。
3. **at 边界**：at 独立段，勿与文本拼接。
4. **message_id 哈希非递增**：撤回消息不可恢复。
5. **图片 URL 2h 过期**需刷新。
6. **`messagePostFormat` 必须 array**；`enableLocalFile2Url=false` 时本地文件用 `file://` 引用最稳。

---

## §10 OpenCode / Zen 对照（外部架构参考，2026-08-08）

> 用户发来 `opencode.ai/zen/go`（404）。主动解读为参考 OpenCode 架构，与 OpenClaw/Hermes 同级的对标研究。一手来源：opencode.ai 官网 / docs。

**OpenCode 核心**（开源 AI 编程代理，195k stars）：终端/IDE/桌面三种形态；**Plan mode / Build mode 双模式**（Tab 切换，Plan 只建议不执行）；`/init` 生成 `AGENTS.md` 项目指令文件；`@` 模糊搜索文件引用进 prompt；`/undo`/`/redo` 回退；任意 LLM provider。

**OpenCode Zen**（面向 coding agent 的精选模型服务）：团队测试 + 基准评测筛选的模型集合，按请求付费、零保留隐私。本质是**模型路由/质量精选**——省去用户在多 provider 间试错。

**对 YuKiKo 的可借鉴判断**：

| OpenCode 机制 | 是否借鉴 | 理由 |
|---|---|---|
| Plan/Build 双模式 | **部分借鉴** | YuKiKo 的 PromptNavigator 分区已承担"先选方向再执行"；可加"复杂任务先输出 plan 再执行"的提示强化 |
| AGENTS.md 项目指令 | **已等价** | YuKiKo 的 `CLAUDE.md`/`config/prompts.yml` + 系统提示已是指令体系，无需引入 |
| `@` 文件引用 | **不借鉴** | QQ 群聊场景无"本地项目文件"概念（除非接 group_files 工具取文件后引用） |
| Zen 模型路由 | **思路借鉴** | YuKiKo 已有多 provider failover（`served_model_state()`）；Zen 的"质量评测 + 路由"思路可用来给 `services/` 的降级链做评测排序 |
| `/undo`/`/redo` | **不借鉴** | 群聊消息不可撤回（QQ 限制），意义有限 |

**结论**：OpenCode 对 YuKiKo 的增量价值有限——YuKiKo 的分区菜单/多 provider/指令体系已覆盖其核心。唯一可借鉴的是 **Zen 的"模型质量评测"思路**（为 failover 链打分排序），以及 **Plan-first 强化**（复杂任务先规划）。不引入 OpenCode 本体。
