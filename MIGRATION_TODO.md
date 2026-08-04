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
- [x] **A4. router 不再选工具族** — 已修（`a060e6e` + `a4dcacf`）。
      **prompt 侧**：`core/system_prompts.py` 的 `router_system_prompt` 原先枚举整个 action 家族
      并用散文教关键词映射（「点歌 播歌 action=music_play」「画图请求 action=generate_image」）
      —— 那是第二套意图分类器，与分区目录抢同一决策权，而且它看不到 section instructions
      和工具 schema，判断依据比 Agent 少。严格模式下收缩为只判断
      `should_handle` / `ignore|reply` / `confidence`，**4108 → 668 字符**。
      安全按你的决策改为 `should_handle=true action=reply` 交后续环节，router 不再自出 `moderate`。
      **代码侧**：`_parse_decision` 里两条「见图就强制 `action=search` + 硬塞
      `method=media.analyze_image`」的覆盖已按严格模式门控。它们靠结构信号触发（非关键词），
      但替模型决定了工具 —— 而 navigator 已做得更好：`_preselect` 读同一个图片段，
      起始分区落 `multimodal_media`、`analyze_image` 进可见工具、
      `image_url` 等作为**可被模型否决的**结构证据。
      *实测：带图片段 → `multimodal_media` + evidence `['image_url','message_or_reply_media','url']`；无图 → `general_chat`。*
      **保留**（逐个查过）：`_contains_explicit_adult_intent`（带词边界的结构令牌 `/nsfw`、`r18`）、
      `_is_passive_multimodal_event`（OneBot 段占位符正则）、
      `_looks_like_bot_address`（`bot_aliases` 身份匹配，与 trigger 同类）、
      `_fallback_media_decision_without_model`（只在模型不可用时跑、纯结构信号，
      正是你选的「只保留结构信号兜底」）。
      非 navigator 部署走 else 分支保持原样。
      *发现：`test_router_media_fallback_regression.py` 测的是 no-model 兜底
      （reason `fallback_direct_media_no_model`），**并未覆盖这两条覆盖分支** ——
      新增 `tests/test_router_override_scope_regression.py` 4 例补上。*

- [x] **A5. `core/trigger.py` 已改为只做注意力门控** — 已修（`3f9fb32`）。
      删除 `_looks_like_explicit_bot_request`、`_looks_like_explicit_memory_declare`、
      `_explicit_request_signal_from_cues`、`_explicit_request_signal`，以及死包装 `_should_open_ai_probe`。
      新增 `_structural_request_signal`：只给四类客观定位符打分（显式命令令牌 / URL / 视频号 / 文件扩展名）。
      昵称别名匹配**保留** —— 那是身份识别不是意图猜测。
      *实测：「点歌 热水澡」「帮我看看这个」「你能不能搜一下」「记住我叫小明」全部 0.00；
      `https://b23.tv/...` 与 `BV1xx411c7mD` 得 0.70，`/music` 得 1.30。*

- [ ] **A6. `core/agent.py` 强制工具子树** — 已删 4 处死分支共 **98 行**（`e89086e`）：
      pre-LLM 合成工具调用、direct-text 拦截、改写模型选中的工具、final_answer 强制路径，
      以及孤立的 `forced_media_tool_consumed`。*验证：零残留引用；全量 680 passed 与删前逐字一致。*
      **剩余**：7 个辅助函数仍在（`_select_forced_media_tool`、`_should_force_tool_first`、
      `_select_forced_video_tool`、`_should_force_voice_tool_first`、`_select_forced_web_tool`、
      `_should_force_image_tool_first`、`_should_force_local_video_tool_first`），
      因为 `tests/test_tool_call_leak_regression.py`（16 个测试里 7 个）、`tests/test_agent_smoke.py`
      和 `scripts/agent_deep_selfcheck.py` 仍当纯函数直接调。
      这 7 个测试断言的契约是「有媒体 + 短问句 → 必须先调工具，不能纯文本作答」——
      **契约要保留，但实现须改由 navigator 承担**（断言改成 `_preselect` 命中
      `multimodal_media` 且该区 tools 含分析工具），改完才能删函数。

- [ ] **A7. `core/agent.py` 仍活着的 4 条关键词路径** — 这几条是真迁移，不是清理。
      **行号已因 `e89086e` 删除 98 行而全部漂移 −98，以下为 HEAD 实测值：**
      `_rewrite_download_tool_if_needed:4560`（模型选完工具后本地改工具名）、
      `_normalize_tool_args:3628`（语义猜参数默认值）、`_fallback_tool_on_failure:4317`、
      `_navigator_timeout_fallback_tool:2177`（本地 if-chain 直接决定工具）。
      **危险**：这一带全是相邻的 `def`，按旧行号做切片编辑不会语法报错，只会**静默改错函数**。
      **`_navigator_timeout_tool_retry:2345` 要保留** —— 它发起第二次真实 LLM 调用并硬校验
      `tool_name not in domain_tools`，符合目标架构；只需把 `:2408-2416` 硬编码的两条分区策略
      文案搬进对应 section 的 `instructions`。
- [x] **A8. `core/tools.py` 自由文本意图猜测已清除** — 已修（`d63e624` + `8fc9398`）。
      删除 `_looks_like_image_request`、`_looks_like_image_send_request`、`_looks_like_media_request`、
      `_looks_like_local_file_request`、`_looks_like_local_media_request`、
      `_looks_like_deep_web_analysis_request`、`_should_auto_web_analysis`。
      `_contains_self_avatar_cue` 收缩为只认 `/avatar target=self|me` 令牌
      （*实测：「我的头像」「看看我的头像」False，令牌 True*）—— 顺带修掉一个真 bug：
      词表恒空导致带令牌时解析不出调用者 QQ、掉进「给我一个 QQ 号」兜底。
      **`_detect_query_type` 刻意保留** —— 它在 `core/tools_search.py:42`、模型选定搜索工具**之后**才跑，
      只喂 `_apply_query_type_hints` 改写查询串，属治理文档保留清单里的「工具内排序」。
      `execute()` 的 action 字符串分派同样未动（那不是关键词启发式）。
      *注：`core/engine.py` 里有同名独立副本，属 A2，未受影响。*

- [x] **A9. 工具内语义猜测已转为显式参数** — 已修（`dfc4460` + `d154d30`）。
      `core/agent_tools_utility.py`：`_STICKER_SEND_CUES`(15 词) + `_STICKER_MANAGEMENT_CUES`(17 词)
      换成模型必填的 `turn_goal=send|manage`，工具只校验声明。
      *实测三条契约：管理类原文 + `send` 不再被否决；发送类原文 + `manage` 被拒；
      不声明则返回 `missing_arg:turn_goal` 而不猜。模型声明双向压过原文。*
      `core/tools_video.py`：`_pick_video_duration_limit` 签名由 `query` 改为 `duration_scene`，
      删 `_looks_like_video_send_request`、`_looks_like_douyin_search_request`，
      新增 `analyze_content` / `depth` / `output_mode`。
      *原实现猜反了方向：「发我/下载」放宽到 send 档，而真正的分析问句只拿 default 档。*
      `core/tools_vision.py`：删 `_looks_like_vision_web_lookup_request`、
      `_looks_like_analyze_all_images_request` 及其零引用的唯一调用者 `_analyze_image_from_message`；
      `_has_animated_image_hint` 保留结构半边（`sub_type` / `.gif` / `data.summary`）。
      `core/agent_tools_media.py`（本波无人认领，核实 vision 跨文件笔记时发现）：
      删掉 17×11 叉乘的 `inferred_analyze_all`，删掉猜「当前图 vs 引用图」的三张词表 —— 两处都有图时
      改返回 `ambiguous_target` 要求填 `target_message_id`；并补上 vision 侧已开始读却不存在的
      `web_lookup_on_uncertain` / `is_animated` schema 属性（**不补则新行为是死的**）。
      *tests/test_tools_video_explicit_args.py 22 passed。*

- [ ] **A10. YAML 里的关键词表** — `config/templates/master.template.yml` 的 trigger / routing /
      self_check / tool_hints 等段里的词表与阈值。`_strip_heuristic_prompt_lists`
      （`core/config_templates.py:24`）只作用于 `_built_in_prompts_defaults()` 的返回值，
      没有作用于 `load_prompts_template()` 和 `prompt_loader.reload()` —— 存在绕过路径。

- [ ] **A11. 侦察新发现、A1–A10 均未收录的关键词符号** —
      `core/memory.py:995 _detect_language_style`、`core/memory.py:1044 _detect_topic_category`
      （内嵌 16 词 `tech_kw` / 15 词 `game_kw` 硬编码表）、
      `core/knowledge_updater.py:118 _looks_like_tool_echo`（读类级 `_TOOL_ECHO_CUES:26`，
      **未受 `heuristic_rules_enable` 门控**，是活代码；但其 cue 是 `[cq:` / `"tool"` / `http://`
      这类**结构标记**而非语义意图词，归类偏结构信号，迁移时按结构信号处理）、
      `core/agent_tools_knowledge.py:774 _looks_like_harmful_knowledge_payload`
      （8 词脏词表 + 「以后你叫」/「叫他」组合）。
      *全部经我实测确认存在于 HEAD。*

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
      `_compact_data()`（`core/agent.py:5341`，调用点 `:2001`，喂回 LLM `:2023-2034`）放进 `tool_result.data` 喂回 LLM
      （`:2126`），再可能被 `final_answer` 复述到群里。已排除在菜单外，但工具本身仍注册可调。
- [ ] **C2. `mode` 是死配置** — `PromptNavigatorConfig.mode` 被解析并打印给模型
      （`模式: local_prefilter_llm_review`），但全仓无任何代码分支读它。要么删，要么给它真实含义。
- [ ] **C3. `_strip_heuristic_prompt_lists` 有绕过路径** — 见 A10，需在
      `load_prompts_template()` 和 `prompt_loader.reload()` 也调用。
- [x] **C4. `.venv` 已重建为 Python 3.11.15** — 原 3.14 装不上 nonebot，大部分测试无法 import。
      现在全量测试可跑：**680 passed / 10 skipped / 1 failed**（那 1 个是 D3 的沙箱 DNS 问题）。
      *注：本机无 3.12，用 3.11.15 满足 `requires-python = ">=3.11"`；ruff 的 `target-version = py312`
      只影响 lint 规则，与运行时无关。*
- [ ] ~~**C4-old. `.venv` 是 Python 3.14，与项目声明不符**~~ — `pyproject.toml` 写 `>=3.11`、ruff 目标
      `py312`。`requirements.txt` 的 `pilk` / `bilix` / `f2` 在 3.14 无 wheel，
      `scripts/deploy.py` 装不上，只能手装 `pytest`+`httpx`+`PyYAML`。
      **导致大部分测试套件无法 import（缺 nonebot），全量验证做不了。** 建议改用 3.12 建 venv。

- [x] **C5. `requirements.txt` 依赖冲突已解** — 已修（`3c9cd38`）。
      原先第 5 行钉 `PyYAML==6.0.3`，而 `f2==0.0.1.7` 死钉 `pyyaml==6.0.2`（等号），
      pip 直接 `ResolutionImpossible` —— **任何人 clone 后都装不上依赖**。
      「升 f2」这条路不通：`0.0.1.7` 已是最新（`pip index versions` 确认 0.0.1.0–0.0.1.7），
      其 wheel METADATA 写明 `Requires-Dist: pyyaml==6.0.2`。故改钉 `PyYAML==6.0.2` 并留注释。
      安全依据：仓库只用 `yaml.safe_load`(17 处) / `yaml.safe_dump`(19 处)，6.0.x 内稳定。
      `f2` 会把 `m3u8` 6.0.0 降到 3.6.0，但仓库无任何代码 import 它
      （`core/tools_video.py` 那三处只是对 URL 里 `.m3u8` 做字符串匹配），不受影响。
      *验证：用全新 python3.11 venv 从零安装成功（不是靠已装好的环境），
      `f2.apps.douyin.handler.DouyinHandler` 与 `AwemeIdFetcher`（`core/video_analyzer.py:604-605`
      实际用的两个符号）均可 import；抖音解析路径首次进入可测状态。*

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

---

## E. 自我进化能力（愿景实现）

> 侦察结论：**五项愿景没有一项需要从零重建**，地基比预期好。详见 `.migration/vision-plan.md`
> 与 `.migration/vision-*.md`（7 份子系统侦察，约 320KB）。
> 成熟度：自改 prompt 70% · 知识库净化 40% · 自建 skill 55% · 每日日记 35% · 分离审计 33%。

### E0. 正在发生的数据丢失（优先于任何新功能）

- [x] **E0-1. 日志滚动冲掉群操作痕迹** — 已修（`8530e1c`）。
      `services/logger.py` 从 2MB×3（8MB 硬上限）提到 16MB×4，且**文本日志降级为调试尾巴**：
      需要长期留存的结构化痕迹改走 `core/audit.py` 的按天 JSONL 流，不再依赖滚动文件。
- [x] **E0-2. `admin_command` 教模型写一个解析不出的命令** — `core/agent_tools_admin.py:43-45`
      的 schema 写明支持 `white_add` / `white_rm`，但这两个字符串以前只是 `_SUB` 的**值**、不是**键**，
      `_fuzzy_match_command` 对它们 difflib 相似度≈0 → 返回「未知命令」。
      **模型照着自己的工具 schema 写反而失败**，加白/拉黑经 agent 通路根本不可达。
      已修（`998f4d6`）：三个 canonical 名补进 `_SUB`。这一步在 A1 删 `_FUZZY_COMMAND_MAP` 之前是必须的，
      否则中文别名消失后该能力彻底断链。*验证：6 种写法全部解析；680 passed。*
- [x] **E0-3. 无审计的 7 天硬删除** — 已修（`dc7aebd`）。
      新增 `memory.embedding_retention_days`，**默认 0 = 永不删除**（两处真相都加了）。
      启用保留期时删除会报 `embeddings_pruned | removed=%d | cutoff=%s | retention_days=%d`
      （沿用 `core/memory.py:275` 既有约定），失败改为 `warning` + `exc_info` 不再静默。
      *行为验证：400 天前的 5 条，默认 0 时全留；设 7 时全删并打审计行。*
- [ ] ~~**E0-3-old. 无审计的 7 天硬删除**~~ — `core/memory.py:2657-2667`
      `DELETE FROM embeddings WHERE created_at < ?`（7 天），包在 `except Exception: pass` 里，
      **不记录删了多少条、失败也完全静默**。由 `_cleanup_daily_data`（`:2637`）经 `:2634` 的
      快照时机驱动，即**正常聊天就会触发**。与愿景 2「永久知识库」直接冲突。
      *已实测确认调用链。*

### E0 续：埋点过程中暴露的既有缺陷

- [x] **E0-4. 写盘失败仍回报成功** — 已修（`f04367d`）。
      新增 `_with_persist_warning`，六处 return 全部包裹。内存状态确实改了所以不算失败，
      但 JSON 没落盘、重启即回滚，回复现在会说明。
      *实测：把 `whitelist_groups.json` 换成目录后，回复为
      「已加白本群 777\n注意：本次改动没能写入磁盘，重启后会丢失…」，审计记 `persisted=False`。*
- [ ] ~~**E0-4-old. 写盘失败仍回报成功**~~ — `_save_white` / `_save_ignore` / `_save_runtime_policy`
      原先返回 `None` 且把写盘异常吞在 `_log.debug` 里。现已改为返回 `bool` 并记
      `persisted: false`（`409f1a2`），**但用户侧回复文案仍说成功**。
      *实测：把 `whitelist_groups.json` 换成目录后，用户看到「已加白本群 777」而审计记 `persisted=False`。*
      需要让回复文案跟随 `persisted` 结果。
- [x] **E0-5. 群管理员可清除全局忽略且无法自行恢复** — 已修（`f04367d`）。
      `remove_ignored_user` 的升级分支加了 `is_super_admin` 门；群管理员收到明确提示去找超管。
      `is_super_admin` 在未配 `super_users` 时返回 True，所以未配置的部署不受影响。
      *实测：群管(20002)清除被拒且 `999` 仍在 `_ignored_global`；超管(10001)仍放行并记
      `escalated_to_global=true` / `irreversible=false`。*
      *一个既有测试的契约随之改变：原来断言该升级被**记录为** irreversible（如实审计一个漏洞），
      现改名为 `..._cannot_clear_global_for_group_admin`，同样的执行者与场景，
      改为断言被拒、全局项存活、且不留变更记录。*
- [ ] ~~**E0-5-old. 群管理员可清除全局忽略**~~ — `remove_ignored_user` 的 group scope
      回落会把 **global** 忽略一并清掉，而恢复 global 需要超管权限。
      *实测：群管（非超管）清除后审计记 `irreversible=true` + `escalated_to_global=true`；
      同一调用由超管执行记 `false`。* 审计已能看见，但权限判定本身要修。

### E1. 自我净化 + 永久知识库

- [ ] **E1-1. 把已写好但没接线的三套衰减公式接上** — 侦察发现置信度衰减/新鲜度公式已存在但未被调用。
- [ ] **E1-2. 写入质量门** — 去重、矛盾比对决策、`access_count` 字段、陈旧判定。
      落点在 `core/knowledge.py` 写入路径 + `core/knowledge_updater.py`。
      **须与该文件的关键词清理（`_looks_like_tool_echo`、A11）合并为一个提交**，否则互相踩。
- [ ] **E1-3. schema 迁移框架** — 保证升级不静默丢数据；与 `yukiko backup` 的交互要明确。

### E2. 自建 skill / 工具叠加

- [ ] **E2-1. 声明式 skill 定义 + boot loader + 分区可见性绑定** —
      侦察确认 registry 运行时**完全可变**，工具叠加**零机制成本**（已实跑验证）。
      **安全边界（必须守住）**：skill 只能是「按顺序调用已注册工具 + 传参」的声明式编排，
      **不含新代码**。允许模型生成可执行代码 = 任意代码执行，一个被越狱的模型就能拿到 shell。

### E3. 每日日记 + 分离审计流

- [ ] **E3-1. journal 表作为结构化真相源** — 侦察确认素材采集、快照渲染、注入范式全就位，
      缺的是结构化存储 + 用模型自省替代模板串 + 回流（约 10 行）。
- [ ] **E3-2. tool-call 审计流埋点** — 基础设施已就位（`core/audit.py`，`8530e1c`），
      `engine.audit` 已暴露。**剩余**：在工具执行处调 `audit.write(STREAM_TOOL_CALLS, ...)`。
- [x] **E3-3. group-op 审计流已埋点** — 已修（`409f1a2`）。
      `_audit_group_op` 写 `STREAM_GROUP_OPS`，覆盖加白/拉黑、忽略/恢复用户、高危确认策略、行为模式。
      每条带 `actor_id` / `group_id` / `target_user_id` / `scope` / `change{before,after}` /
      `persisted` / `irreversible`，可按字段查。被拒/无变化/非法命令刻意不写 —— 该流必须读起来是变更史。
      engine 两处构造已接线（`:102` 启动、`:849` 热重载传同一个 trail 使流跨重载连续）。
      *实测：4 次操作产出 4 条记录，before/after 正确，落在单个
      `storage/audit/group_ops/<date>.jsonl`；21 个新测试通过。*
- [x] **E3-0. 审计基础设施** — `core/audit.py`（`8530e1c`）：五条独立流
      （tool_calls / memory_writes / group_ops / prompt_edits / knowledge），
      按天分文件 `storage/audit/<stream>/YYYY-MM-DD.jsonl`，写失败不影响主流程但必告警，
      未知流名拒绝并告警一次，超长字段截断保留原长。config `audit.enable` 默认 true。
      *验证：分流建档、字段级读回、5000 字符截断、未知流拒绝、不可写目录返回 False 不抛。*
- [ ] **E3-4. 三条流里 memory 那条已经做对了，当模板复用** —— 不要重造。

### E4. 自改 prompt

- [ ] **E4-1. bot 可调的编辑工具 + 不污染 git 的 overlay 层 + 独立审计** —
      热重载、超管门、`dry_run`、高危确认、`bot_selfconfig` 分区全就位（70%）。
      **三个已踩过的坑必须绕开**：`_merge_with_defaults` 只回填不修剪、
      `config/prompts.yml` 被 git 跟踪、模板在 `config_templates.py:593-596` 压过 Python payload。
      每次自编辑必须可回滚、可审计，且明确哪些段不许模型碰。

### E5. 施工顺序约束（同一文件不允许两波并发）

- [ ] **E5-1. 干净可立即并行的文件** — `core/knowledge.py`、`core/agent_tools_registry.py`、
      `core/prompt_loader.py` 不在关键词清除范围内，E 组可与 A 组全程并行。
- [ ] **E5-2. 必须串行的文件** — `core/engine.py`（A2/A3）、`core/agent.py`（A6/A7）、
      `core/admin.py`（A1）。行区间虽不重叠，但按同文件规则串行。
- [ ] **E5-3. `prompts.yml` 追加必须串行** — E1/E3/E4 只做「已存在分区的 tools 列表追加」，
      指定 E2 独占结构性变更（新增分区）。
- [ ] **E5-4. `.migration/vision-plan.md` 第 3 节未写完**（卡在 `SECTION_3_PLACEHOLDER`）—
      五项愿景的逐条数据模型与文件落点细节缺失，开工前需补。
