# 关键词触发清除 — 未完成问题清单

> 规则：**只有彻底解决、且有验证证据，才能划掉。** 部分完成的一律保持未勾选，并在条目里写清剩下什么。
> 分支 `refactor/prompt-driven-intent`。基线 `b38cc06`，随时可回滚。

## 目标（我的理解，错了请纠正）

YuKiKo 要成为一个**由模型判断驱动的 QQ 群聊/私聊机器人**，不是命令响应器。

- **意图识别 100% 来自模型读 prompt**。代码里不许有「消息含某词 → 做某事」。
- **能力以一个大 menu 呈现**：模型读分区目录，自己决定进哪个分区、调哪个 toolcall；
  走错了自己调 `navigate_section` 纠正。像神经网络那样从 prompt 学会用工具，而不是被 if-else 牵着走。
- **完全信任模型**。结构事实（有没有图片段、有没有 URL、有没有 @机器人、权限级别）只作为
  **提示喂给模型**，绝不作为覆盖模型判断的代码。
- **安全判断也归模型**：什么算高风险/NSFW 由模型读 prompt 决定，不靠词表；
  但不可逆操作的**确认握手机制保留**（确认是执行前的一次握手，不是权限门禁）。
- 显式命令契约（`/yukibot` 热重载等）保留 —— 那是用户主动敲的命令，不是 AI 猜意图。
- 人格/好感度系统让它像个有性格的伙伴，而不是工具箱。

---

## A. 核心目标：删除 98 个关键词符号（进度 0/98）

替代设施（20 分区菜单）已就位并验证，但**一个关键词符号都还没删**。

- [ ] **A1. `core/admin.py` 模糊命令层** — `_FUZZY_COMMAND_MAP:18`（60+ 中文词）、
      `_fuzzy_match_command:405`（前缀双向匹配 + difflib `cutoff=0.6`）、`_suggest_commands`。
      `_TOP`/`_SUB` 显式命令**保留**。删前必须确认 ~50 个 admin 动作都能经 `bot_selfconfig`
      分区的 toolcall 到达，否则加白/忽略用户/行为模式/表情包扫描等能力会直接消失。
- [ ] **A2. `core/engine.py` 前置门（38 个符号）** — 最大的一处是
      `_detect_bot_strategy_directive:3732`，它在正常路由**之前**就用「闭嘴/安静/活跃」关键词旁路。
      还有 `_should_prefer_router_for_plain_text`、`should_interrupt_previous_task` 的 `task_like`、
      以及 `_looks_like_explicit_request` 等一族。
- [ ] **A3. `core/engine.py` `_self_check_decision:3502` 否决层** — 逐分支处理：
      关键词分支删除、语义搬进对应分区 `instructions`；结构信号分支改成喂给模型的 evidence。
      **不许留任何能覆盖模型判断的代码。**
- [ ] **A4. `core/router.py`（1 个符号 + prompt）** — `_fast_path_decision:513`、
      `_fallback_media_decision_without_model`、`_parse_decision` 里的 `router_override` 分支。
      **配套主战场：`core/system_prompts.py:115` `router_system_prompt` 里硬编码了第二套意图分类器
      （枚举整个 action 家族），它在和 PromptNavigator 抢同一个决策权，必须削成
      只判断 should_handle + confidence。**
- [ ] **A5. `core/trigger.py`（4 个符号）** — `_looks_like_explicit_bot_request`、
      `_looks_like_explicit_memory_declare`、`_explicit_request_signal_from_cues`、
      `_explicit_request_signal`。trigger 只保留注意力/防噪门控（@、私聊、回复、活跃会话、白名单、限流）。
- [ ] **A6. `core/agent.py` 已死的强制工具子树（白送的删除）** — `run()` 在 `:841-842` 声明
      `forced_media_tool` / `force_tool_first` 但**全文件无任何赋值**，导致 `:934`、`:1298`、
      `:1358`、`:1619` 四处消费分支永远为假。`_should_force_image_tool_first:4846`、
      `_select_forced_video_tool` 等整族主链路零入口，只有测试当纯函数调。
- [ ] **A7. `core/agent.py` 仍活着的 4 条关键词路径** — 这几条是真迁移，不是清理：
      `_rewrite_download_tool_if_needed:4658`（模型选完工具后本地改工具名）、
      `_normalize_tool_args:3726`（语义猜参数默认值）、`_fallback_tool_on_failure:4415`、
      `_navigator_timeout_fallback_tool:2275`（本地 if-chain 直接决定工具）。
      **`_navigator_timeout_tool_retry:2443` 要保留** —— 它发起第二次真实 LLM 调用并硬校验
      `tool_name not in domain_tools`，符合目标架构；只需把 `:2507-2515` 硬编码的两条分区策略
      文案搬进对应 section 的 `instructions`。
- [ ] **A8. `core/tools.py`（11 个符号）** — `_detect_query_type`、`_should_auto_web_analysis`、
      `_looks_like_media_request` 等。注意：`ToolExecutor.execute()` 按 action **字符串**分派
      不是关键词启发式，别误删。其中一批是「双重死亡」（读的配置 key 如 `image_request_cues`
      在 `config/` 里不存在 → 恒 False），属于白送的删除。
- [ ] **A9. 工具内语义猜测（13 个符号）** — `core/tools_video.py` 6 个、`core/tools_vision.py` 5 个、
      `core/agent_tools_utility.py` 2 个（`_STICKER_SEND_CUES`、`_STICKER_MANAGEMENT_CUES`）、
      以及 `core/agent_tools_media.py` 的 `analyze_image` 从词表猜 analyze-all / 当前vs引用目标。
      做法：改成**显式工具参数**，工具返回 `missing_arg` / `ambiguous_target` 而不是猜。
- [ ] **A10. YAML 里的关键词表** — `config/templates/master.template.yml` 的 trigger / routing /
      self_check / tool_hints 等段里的词表与阈值。`_strip_heuristic_prompt_lists`
      （`core/config_templates.py:24`）只作用于 `_built_in_prompts_defaults()` 的返回值，
      没有作用于 `load_prompts_template()` 和 `prompt_loader.reload()` —— 存在绕过路径。

## B. 交付缺口（阻塞 A 组开工）

- [ ] **B1. `coverage-map.md` 未生成** — 「每个待删符号 → 承接它语义的哪句 prompt」的映射表。
      **这是 A 组的开工前提**：没有它就无法确认删掉某个符号后能力不会悄悄消失。
      现状：5 个 notes 文件只有 3 个（缺 `qq-write`、`search-fetch`）。
- [ ] **B2. `doctrine-audit.md` 未生成** — 逐行检查菜单里有没有作者偷偷写进「如果消息包含X」
      这类字面词表。菜单里也不许有词表，否则只是把词表从代码搬到 YAML。
- [ ] **B3. 近义工具可区分性未验证** — `search_media` vs `search_web_media`、
      `smart_download` vs `search_download_resources`、几个音乐入口、几条图片分析路径。
      要确认**只靠菜单措辞**模型能分清。真区分不了的，是该合并的信号。

## C. 已知 bug（与关键词迁移无关，但已确认存在）

- [ ] **C1. `get_cookies` 凭证外泄** — `core/agent_tools_napcat.py:2204` 把 QQ Cookie 经
      `_compact_data()`（`core/agent.py:2098`）放进 `tool_result.data` 喂回 LLM
      （`:2126`），再可能被 `final_answer` 复述到群里。已排除在菜单外，但工具本身仍注册可调。
- [ ] **C2. `mode` 是死配置** — `PromptNavigatorConfig.mode` 被解析并打印给模型
      （`模式: local_prefilter_llm_review`），但全仓无任何代码分支读它。要么删，要么给它真实含义。
- [ ] **C3. `_strip_heuristic_prompt_lists` 有绕过路径** — 见 A10，需在
      `load_prompts_template()` 和 `prompt_loader.reload()` 也调用。
- [ ] **C4. `.venv` 是 Python 3.14，与项目声明不符** — `pyproject.toml` 写 `>=3.11`、ruff 目标
      `py312`。`requirements.txt` 的 `pilk` / `bilix` / `f2` 在 3.14 无 wheel，
      `scripts/deploy.py` 装不上，只能手装 `pytest`+`httpx`+`PyYAML`。
      **导致大部分测试套件无法 import（缺 nonebot），全量验证做不了。** 建议改用 3.12 建 venv。

## D. 测试（A 组的验收标准）

- [ ] **D1. 关键词行为测试需重写** — 约 55 个测试文件里，断言关键词行为的必须按既定套路改：
      **保留旧测试名、把断言反转**（`tests/test_prompt_navigator.py:60-151` 已有 8 个范例）。
      重点：`test_local_intent_heuristic_regression.py`、`test_config_and_trigger_regression.py`、
      `test_router_media_fallback_regression.py`、`test_engine_bot_strategy_regression.py`。
- [ ] **D2. 结果契约测试必须继续通过** — 安全、高风险确认、工具泄漏、弱模型保护那几套是
      **护栏**，不是待改对象：`test_high_risk_ban_guard_regression.py`、
      `test_safety_profile_regression.py`、`test_tool_call_leak_regression.py`、
      `test_weak_model_protection.py`、`test_image_nsfw_guard_regression.py`。
- [ ] **D3. 沙箱环境失败需区分** — `test_local_intent_heuristic_regression.py::
      test_video_unsupported_message_lists_all_supported_platforms` 在本机恒失败，
      原因是沙箱 DNS 把所有域名解析进保留段 `198.18.0.0/15`，SSRF 护栏先拦下。
      **已确认基线上同样失败，非代码缺陷。** 真机上应通过 —— 需在真机复核一次。

---

## 已完成（有验证证据）

- [x] **建立版本控制基线** — 282 文件，确认无密钥入库；分支 `refactor/prompt-driven-intent`。
- [x] **多行 prompt 不再被压平** — `load_prompt_navigator_config` 对
      `instructions`/`when_to_use`/`failure_policy`/`root_prompt` 改用 `strip()`；
      `normalize_text`（`re.sub(r"\s+"," ")`）只留给 id 和工具名。
      *证据：3 行 instructions 实测保留 3 行；现网菜单 instructions 中位 48 行。*
- [x] **分区工具列表不再截断** — 删掉 `tools[:12]`。
      *证据：`qq_admin_social` 全部工具进目录，实测 missing=none。*
- [x] **视频封面不再被误判成图片** — `_collect_segment_kinds` 运算符优先级修正。
      *证据：带 `data.image` 封面的 video 段实测得 `['video']`；真图片/语音段分类不变。*
- [x] **删除写错命名空间的死开关** — `core/tools.py` 里 `global _TOOLS_HEURISTIC_CUES_ENABLED`
      改不到 `tools_types.py` 那个真开关，且无人 import。
- [x] **菜单扩到 20 分区 / 178 工具可达** — 原先 79/190 可达，111 个工具模型完全够不着
      （`set_group_admin`、整条 chat_history 线、群文件、Qzone、好感度打卡均无归属）。
      新增 8 分区。9 个不可逆工具按决策留在菜单内并写明不可逆 + 确认握手。
      目录改成「能力摘要 + 工具数」，从 11241 字符降到 2936。
      *证据：validator 对活注册表 0 error 0 warning；50 passed；ruff 与基线逐条相同。*
- [x] **三处真相同步** — Python payload / `master.template.yml` / 运行时 `prompt_loader`
      输出逐字段一致。修正了「`prompts.yml` 是 gitignore 运行时状态」这个误判
      （它其实被 git 跟踪），并修掉 `general_chat` 把「闭嘴」指向 `qq_admin_social`
      而 `admin_command` 已搬去 `bot_selfconfig` 的错误指引。
      *证据：20 分区逐字段三方比对无差异。*

---

## 预算现状（未达标，已如实记录）

方案 B′ 预估 `render_system_block()` 2299 字符，**实测起始态 5813、20 区中位 6013、
最重 `bot_selfconfig` 9554**，比基线 4515 高约 29%。

原因：预估假设的 instructions 远比实际写出来的单薄。实际是中位 48 行的语义化说明，
正是「死写好 prompt」要的东西。压到 2299 就得把 prompt 砍回单薄，本末倒置。

已做的真实削减：目录只放能力摘要（完整 `when_to_use` 留给进入分区后）、
fallback 不进目录（`render_active_section_block` 已单独打印当前区退路）。

按单位能力算效率更高（5813 换 20 区/178 工具/丰满说明 vs 4515 换 12 区/79 工具/单薄说明），
但绝对值确实涨了。**若要求绝对值不超基线，需要你明确取舍。**
