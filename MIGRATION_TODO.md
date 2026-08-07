# 关键词触发清除 — 未完成问题清单

> **历史 checklist / archive，不是当前 handoff。** 当前工作树、测试结果、live 边界和下一步命令统一看 [`TAKEOVER-2026-08-07.md`](TAKEOVER-2026-08-07.md)。本文件保留旧的拆项、推理和证据快照；其中的计数、行号、提交基线与“已完成/未完成”不得脱离当时日期直接引用。

> 规则：**只有彻底解决、且有验证证据，才能划掉。** 部分完成的一律保持未勾选，并在条目里写清剩下什么。
> 分支 `refactor/prompt-driven-intent`。基线 `b38cc06`，随时可回滚。
>
> **2026-08-05 全文对代码复核过一遍**（基线 `b38cc06` 以来 48 个提交）。
> 现状 **18 项未开 / 33 项已完成**（另有 4 条 `~~划掉~~` 的历史条目，是留档不是待办）。
> 此前的记账落后于代码：A3 / A6 / A10 / A11 / C2 / C3 / D3 / E1-1 / E1-2
> 都是「早已做完但没勾」，本轮据提交与实测补勾。新增 C8（`close_session` 没有出口）。
> 其中 **A2 与 A7 从「未动」改为「大头已做、剩尾巴」，仍不勾**，剩什么写在条目里。
>
> **本文件里所有 `.py` 行号一律当过期看，只按符号名 grep。** `37f60d4` 与 `af6fe30`
> 让 `core/engine.py`（现 ~7.4k 行）和 `core/agent.py`（现 ~5.4k 行）各净减约 1100 行，
> 行号整体漂移上千。这两个文件是密集相邻的 `def`，按旧行号切片编辑**不会语法报错，
> 只会静默改错函数**。`core/knowledge.py` 一类没被清理波及的文件行号仍可信，
> 所以下面用到行号的地方都标了出处。

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

## A. 核心目标：删除 98 个关键词符号

> **2026-08-05 复核**：A3–A11 全部落地，只剩 A1（admin 模糊命令层）与 A2 的尾巴。
> 「一个关键词符号都还没删」这句已过期 —— `core/engine.py` 与 `core/agent.py` 各净减约
> 1100 行（`37f60d4`、`af6fe30`）。
> **本节所有 `.py` 行号一律视为过期**，只按符号名 grep。那两个提交让行号整体漂移了上千行。

- [ ] **A1. `core/admin.py` 模糊命令层** — 唯一没动的一项。`_FUZZY_COMMAND_MAP`（60+ 中文词）、
      `_fuzzy_match_command`（前缀双向匹配 + difflib `cutoff=0.6`）、`_suggest_commands` 全部仍在
      HEAD（*实测：`grep -n _FUZZY_COMMAND_MAP core/admin.py` 有命中*）。
      `_TOP`/`_SUB` 显式命令**保留**。删前必须确认 ~50 个 admin 动作都能经 `bot_selfconfig`
      分区的 toolcall 到达，否则加白/忽略用户/行为模式/表情包扫描等能力会直接消失。
      E0-2（`998f4d6`）已把 `white_add`/`white_rm`/`white_list` 三个 canonical 名补进 `_SUB`，
      这是本条的前置条件，已满足。
- [ ] **A2. `core/engine.py` 前置门 —— 大头已删，剩两个符号** — 已删（`37f60d4`）：
      `_detect_bot_strategy_directive`（「闭嘴/安静/活跃」关键词旁路整个路由）、
      `_should_prefer_router_for_plain_text`、`_should_ignore_passive_multimodal_turn`。
      *验证：三者在 HEAD 全仓零命中；`core/engine.py:1676` 一带留有说明为什么删的墓碑注释。*
      **剩余两个**：`_looks_like_explicit_request`（仍被 `router_low_confidence` 分支当例外用）、
      `should_interrupt_previous_task` 里的 `task_like`（仍决定跨用户是否打断）。
      两者都在**回退 router 侧**，agent 路径不经过 —— 优先级因此低于 A1。
      **风险已记账**：`_detect_bot_strategy_directive` 删除时连带失去了「非超管说闭嘴 →
      `trigger.close_session()`」这个副作用，而 `close_session` 至今**没有对应 toolcall**
      （*实测：`grep -rn close_session` 只有 `core/trigger.py` 的定义*），
      所以「叫它闭嘴」这条能力目前是断的，模型没有工具可以关会话。
- [x] **A3. `core/engine.py` `_self_check_decision` 否决层已删除** — 已修（`37f60d4`）。
      连同 `_normalize_decision_with_tool_policy` 一起整体删除，13 条本地规则一条不留。
      *验证：`grep -rn _self_check_decision core/` 只剩两条墓碑注释
      （`core/engine.py` 的 config 解析处与 router 之后的原址），无任何活代码。*
      「群聊别乱回」改由模型读分区说明后用空文本 `final_answer` 表达；结构事实与社交时序
      作为 evidence 喂给模型。检索工具缺参改由 `core/agent.py` 的 `_missing_required_tool_args`
      回喂 `missing_required_args:*` 让模型自己补。
      配套：五个 `self_check.*` 配置键与 `at_other_not_for_bot_hard` 硬否决已在 `7e5e83e` 从
      **三处真相全部撤掉**（`config/templates/master.template.yml`、`core/config_templates.py`、
      `webui/src/pages/config/config-schema.ts`）。
      *验证：全仓 `self_check` 仅剩注释、WebUI 面板 code、以及 `plugins/connect_cli.py` 里
      同名无关的 `self_check_on_setup`。*
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

- [x] **A6. `core/agent.py` 强制工具子树已清空** — 分两步做完。
      第一步 `e89086e` 删 4 处死分支共 **98 行**：pre-LLM 合成工具调用、direct-text 拦截、
      改写模型选中的工具、final_answer 强制路径，以及孤立的 `forced_media_tool_consumed`。
      第二步 `af6fe30` 删掉当时还留着的 7 个辅助函数（`_select_forced_media_tool`、
      `_should_force_tool_first`、`_select_forced_video_tool`、`_should_force_voice_tool_first`、
      `_select_forced_web_tool`、`_should_force_image_tool_first`、
      `_should_force_local_video_tool_first`）。
      *验证：`hasattr(AgentLoop, "_should_force_image_tool_first")` → False；
      `grep` 这 7 个名字在 `core/` 零命中，只在测试里以「断言它不存在」的形式出现。*
      **契约按计划保留、判据换人**：`tests/test_tool_call_leak_regression.py` 与
      `tests/test_agent_smoke.py` 改成对 navigator 断言
      （`_preselect` 落 `multimodal_media` 且该区可见工具含分析工具），
      `tests/test_tool_call_leak_regression.py:333` 直接 `assertFalse(hasattr(...))`。
      `scripts/agent_deep_selfcheck.py` 的 `agent.force_tool_first_image` 一项也换成了
      `agent.image_evidence_reaches_navigator`。*实测：该脚本 total=29 pass=29 fail=0。*

- [ ] **A7. `core/agent.py` 关键词路径 —— 4 条剩 1 条** — 已删（`af6fe30`）：
      `_rewrite_download_tool_if_needed`、`_fallback_tool_on_failure`、
      `_navigator_timeout_fallback_tool`。*验证：三者在 HEAD 全仓零命中。*
      **剩余 `_normalize_tool_args`（仍在，语义猜参数默认值）** —— 它按 `tool_name` 分支，
      用 `_rebuild_query_with_context`、`_infer_lookup_keyword`、`_infer_split_video_mode`、
      `_infer_video_time_hints`、`_infer_frame_count_hint` 从原文猜 `query`/`keyword`/`mode`/
      `start_seconds` 等默认值，只在参数为空时填（`_set_if_empty`）。
      迁移方向与 A9 相同：改为工具 schema 必填 + 模型显式声明，工具只校验不猜。
      **注意这个函数是踩过雷的地方** —— 曾把 `_looks_like_file_send_request` 当孤儿删掉，
      漏了这里的调用点，主流程无 try/except 保护，`smart_download`/`download_file` 每次都抛
      `AttributeError`。删任何被它引用的辅助函数前先 grep 整个仓库。
      **`_navigator_timeout_tool_retry` 要保留** —— 它发起第二次真实 LLM 调用并硬校验
      `tool_name not in domain_tools`，符合目标架构；只需把里面硬编码的两条分区策略
      文案搬进对应 section 的 `instructions`（行号已漂移，按符号名定位）。
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

- [x] **A10. YAML 里的关键词表已清除，绕过路径已封** — 已修（`562a3bd`）。
      模板的 trigger / routing / self_check / prompt_control / tool_hints / agent 各段词表与阈值
      全部移除（多数在此之前就已因读点被删而成孤儿）。
      `strip_heuristic_prompt_lists`（原私有 `_strip_heuristic_prompt_lists`）现在**三条路径都调**：
      `_built_in_prompts_defaults()`、`load_prompts_template()`、`prompt_loader.reload()`
      （*实测：`core/prompt_loader.py:84` 是新增的那处*）。
      剪枝会把 merge 标记为 changed，键真正离盘而不是每次开机重读。
      *实测：往 `config/prompts.yml` 注入 `legacy_test_cues` 与 `vision.image_question_cues`
      后 reload，日志 `prompt_cue_lists_pruned | count=2 | keys=...`，两键从文件消失。*
      判据只认「键名以 `_cues`/`_patterns`/`_regexes`/`_tokens` 结尾且值是 list」，
      同名但值为散文的键不动。*复核：模板里 grep `cues`/`keywords` 只剩「不要照词表对」这类
      指导语散文，无 list。*

- [x] **A11. 侦察新发现的 4 个符号已处理完（2 删 2 留，留的有理由）** —
      **删**：`core/memory.py _detect_topic_category`（5 张硬编码词表，`25244e3`）、
      `core/agent_tools_knowledge.py _looks_like_harmful_knowledge_payload`
      （8 词脏词表 + 「以后你叫」/「叫他」组合，`83efd95`，安全判断改归模型、机制保留）。
      *验证：两个名字在 HEAD 全仓零命中，只剩说明为什么删的注释。*
      **留**：`core/memory.py _detect_language_style` —— **这条纠正我自己的归类**。
      它的字符串常量池里零实词（只有 docstring、三个返回标签、`\s+` 和两个标点），
      量的全是排版：emoji 码位区间、颜文字形状、重复标点、感叹号数、拉丁 token 比例、
      压缩长度、中文句读数。*实测：三条语义不同、标点相同的句子全返回 `casual`；
      同一句改排版则 `casual` → `slang`。* 读的是形式不是意图，属结构事实。
      `core/knowledge_updater.py _looks_like_tool_echo`（`_TOOL_ECHO_CUES` 现为
      `("[cq:", '"tool"', '"tool_result"')`）—— cue 是工具回显的**结构标记**，
      且 CQ 码进抽取器只会产出垃圾知识。仍是活代码，由 `block_tool_echo` 控制。

## B. 交付缺口（阻塞 A 组开工）

- [x] **B1. `coverage-map.md` 已补齐** — 由并行 codegraph 会话产出，
      `.migration/coverage-map.md` **327 行**；5 份域 notes 也齐了
      （`notes-core-ops` 152 / `notes-media` 256 / `notes-qq-read` 130 / `notes-qq-write` 252 /
      `notes-search-fetch` 271 行，另有 `notes-from-codegraph` 91 行）。
      *注意：`.migration/` 已 gitignore，是生成物，可能随时被清掉。*
      **它自认的缺口**：A11 那 4 个符号 5 份 notes 全没覆盖（但 A11 现已独立做完）。
- [ ] **B2. `doctrine-audit.md` 仍未生成** — 逐行检查菜单里有没有作者偷偷写进「如果消息包含X」
      这类字面词表。菜单里也不许有词表，否则只是把词表从代码搬到 YAML。
      *实测：`ls .migration/ | grep -i doctrine` 无命中。*
      **A10 只是封住了「list 型词表键」这一类**（判据是键名后缀 + 值为 list），
      写在 `instructions` 散文里的「如果消息包含X」它抓不到 —— 这正是 B2 要人来看的部分，
      所以 A10 完成**不等于** B2 完成。
- [ ] **B3. 近义工具可区分性未验证** — `search_media` vs `search_web_media`、
      `smart_download` vs `search_download_resources`、几个音乐入口、几条图片分析路径。
      要确认**只靠菜单措辞**模型能分清。真区分不了的，是该合并的信号。

## C. 已知 bug（与关键词迁移无关，但已确认存在）

- [x] **C1. `get_cookies` 凭证外泄** — 已修（`db81dc9`），本条此前漏勾。
      三个凭证工具（`get_cookies` / `get_credentials` / `get_csrf_token`）**全部**指向
      `_handle_napcat_credential_probe`（`core/agent_tools_napcat.py`，定义处行号会漂），
      三条注册项引用同一个 handler，无绕过。`data` 只含白名单构造的存在性摘要。
      *2026-08-05 复验：`grep -n _handle_napcat_credential_probe` 得 1 处定义 + 3 处注册引用，
      结论不变。* 顺带一提，这三个工具**不在任何分区里**，模型经 `scoped_tools()` 拿不到
      （见「预算现状」末节的 8 个够不着的工具）—— 这层额外保护是不是刻意的，没人写明。
      *一个并行 codegraph 会话报告此处与我的清单冲突 —— 冲突源是我的记账过期，不是代码。
      已复验注册路径确认无第二条通路。*

- [x] **C6. `update check` 会真的执行更新** — 已修（`6d9be55`）。
      `_act_update` 把 `check`/`status`/`查看` 解析成 `check_only`，但该变量**只用于决定超时和标题**，
      从不进 `cmd_args` —— 脚本收到裸 `update`，跑完整 git pull + 装依赖 + 建 webui + 热重载。
      `restart_mode` / `allow_dirty` 同样被丢弃。
      不对称之处最容易漏看：`--check-only` 在白名单里，所以直接写 flag 是真检查，
      而友好别名反而执行真更新。**用户以为在查看状态，实际在生产环境升级。**
      *实测 argv：`check`/`status`/`查看` → `["update","--check-only"]` @120s；
      `restart` → `--restart`；`force` → `--allow-dirty`；显式 flag 不重复追加。*

- [x] **C7. `_handle_get_group_file_url` 重复定义** — 已修（`9e81cce`）。
      `core/agent_tools_napcat.py` 里 `:1942` 与 `:4105` 两份同名定义，Python 取后者，
      **前者是静默死代码：改它无任何效果，测试也不报错。** 两处注册都解析到 `:4105`。
      死的那份反而更严谨，已把它的 `file_id.strip()` 和「url 缺失不覆盖 display」搬进存活版本。
      *该文件 ~4300 行同形 handler，同名遮蔽是这里的真实风险面。*
      *2026-08-05 复验：`grep -n "def _handle_get_group_file_url"` 只剩 1 处定义，未回归。*

- [x] **C2. 死配置 `mode` 已删** — 已修（`562a3bd`）。`PromptNavigatorConfig.mode` 原先被解析并
      每回合打印给模型（`模式: local_prefilter_llm_review`），全仓无任何分支读它，
      而那个值还反向暗示「本地已预筛、模型只需复核」—— 与目标架构正好相反。
      *实测：`hasattr(cfg, "mode")` → False；`render_system_block` 输出里
      `模式:` 与 `local_prefilter` 均不出现。* 原址留了注释防止有人重新引入。
- [x] **C3. `strip_heuristic_prompt_lists` 绕过路径已封** — 已修（`562a3bd`），见 A10 的实测证据。
      `load_prompts_template()` 与 `prompt_loader.reload()` 两处都补上了调用
      （`core/prompt_loader.py:84`）。这条是当时那批里最要紧的一项：因为
      `_merge_with_defaults` 只回填不覆盖，不封的话**升级安装会永久保留自己那份词表**。
- [x] **C4. `.venv` 已重建为 Python 3.11.15** — 原 3.14 装不上 nonebot，大部分测试无法 import。
      现在全量测试可跑。当时是 680 passed / 10 skipped / 1 failed；
      **2026-08-05 实测：`3067695` 时 921 passed / 12 skipped / 0 failed，
      一小时后 985 passed / 12 skipped / 0 failed**（那个 1 failed 见 D3，已修）。
      *passed 计数一天内从 680 走到 985，别当常量用 —— 不变量是 `0 failed`。*
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

- [ ] **C8. 「叫它闭嘴」现在没有出口** — A2 删 `_detect_bot_strategy_directive` 时连带失去了
      「非超管说闭嘴 → `trigger.close_session()`」这个副作用，而 `close_session` **至今没有
      对应的 toolcall**。*实测：`grep -rn close_session` 只有 `core/trigger.py:258` 的定义，
      加两处测试里说明该副作用已消失的注释，无任何注册工具指向它。*
      按目标架构这条该由模型调工具完成，不是靠词表旁路 —— 所以修法是**加工具**，
      不是把词表加回来。删得对，但能力断了，得补。
      业主要观察话痨行为时这条尤其要紧：出问题时用户最自然的反应就是「闭嘴」，
      而现在这句话除了让模型自己决定少说话之外没有任何机制效果。
      止血阀仍在：`routing.non_directed_min_confidence` 调 0
      （`non_directed_threshold_disabled` 分支，实测是活代码）。

## D. 测试（A 组的验收标准）

- [ ] **D1. 关键词行为测试 —— 四个重点文件已改完，随 A1/A2 尾巴还会再动** —
      套路是**保留旧测试名、把断言反转**（`tests/test_prompt_navigator.py:60-151` 有 8 个范例）。
      四个重点文件全部已随对应清理提交改过：
      `test_local_intent_heuristic_regression.py`（32 例）、
      `test_engine_bot_strategy_regression.py`（9 例，`37f60d4` 改了 328 行）、
      `test_router_media_fallback_regression.py`（3 例）、
      `test_config_and_trigger_regression.py`（12 例，`3f9fb32`）。
      另加 `test_tool_call_leak_regression.py` 与 `test_agent_smoke.py`（`af6fe30`，
      判据换成对 navigator 断言）。
      **剩余**：A1 删 `_FUZZY_COMMAND_MAP` 时 admin 侧测试还要再来一轮，所以本条
      要等 A 组全完才能勾。
- [ ] **D2. 结果契约测试必须继续通过** — 安全、高风险确认、工具泄漏、弱模型保护那几套是
      **护栏**，不是待改对象：`test_high_risk_ban_guard_regression.py`、
      `test_safety_profile_regression.py`、`test_tool_call_leak_regression.py`、
      `test_weak_model_protection.py`、`test_image_nsfw_guard_regression.py`。
      *2026-08-05 实测这五个文件 71 passed。* 本条是长期约束，不勾。
- [x] **D3. 那个「沙箱 DNS 失败」其实是真 bug，已修** — 已修（`94ea836`）。
      `test_local_intent_heuristic_regression.py::test_video_unsupported_message_lists_all_supported_platforms`
      从会话开始就红，此前（本文件与 HANDOFF 都）记为「沙箱 DNS 问题、非代码缺陷、真机应通过」——
      **这个归因是错的**。透明代理（Clash fake-IP）把所有域名解析进 RFC 2544 基准测试段
      `198.18.0.0/15`，Python 的 `ipaddress` 判其为 private，于是 SSRF 护栏拒绝**一切外部域名**
      —— 实测 bilibili、peps.python.org 全被拦，视频解析在这种网络下完全不可用。
      而那个段不携带任何目的地信息（正因如此才被选作 fake-IP 池），既不证明目标是私网也不证明是公网。
      改为：解析结果全部落在该段时忽略解析结果、依据已通过的主机名检查放行；
      真实私网会解析到 `10/8`、`192.168/16`、`127/8`、`169.254/16`，都不在该段；
      混合结果仍然拒绝。同时撤掉了此前的临时办法 `allow_private_network: true`
      —— 那个开关让 `_is_safe_public_http_url` 直接返回 True，等于整道护栏关闭，
      实测连 `127.0.0.1` 和 `169.254.169.254`（云元数据）一起放行。
      *实测：该测试单跑 1 passed；新增 `tests/test_ssrf_fake_ip_regression.py` 14 例覆盖
      IPv4/IPv6 包裹识别、混合结果、字面私网、内网 TLD、非 HTTP scheme。*
      **教训记在这里**：「基线上同样失败」只能证明不是本次改动引入的，**不能证明不是 bug**。

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
- [x] **菜单扩到 20 分区** — 原先只有 79 个工具可达，111 个模型完全够不着
      （`set_group_admin`、整条 chat_history 线、群文件、Qzone、好感度打卡均无归属）。
      新增 8 分区。9 个不可逆工具按决策留在菜单内并写明不可逆 + 确认握手。
      目录改成「能力摘要 + 工具数」，从 11241 字符降到 2936。
      *证据：validator 对活注册表 0 error 0 warning；50 passed；ruff 与基线逐条相同。*
      **可达数按当时口径记为 178/190，2026-08-05 重测口径不同、结论一致**：
      20 分区共声明 179 个工具名，其中 **171 个能解析到真实注册工具**，
      8 个要插件加载才存在；另有 8 个已注册工具不在任何分区（明细见「预算现状」末节）。
      当时的 190 是「含插件的名字总数」，179 是「不含插件的注册数」，两个口径别混用。
- [x] **三处真相同步** — Python payload / `master.template.yml` / 运行时 `prompt_loader`
      输出逐字段一致。修正了「`prompts.yml` 是 gitignore 运行时状态」这个误判
      （它其实被 git 跟踪），并修掉 `general_chat` 把「闭嘴」指向 `qq_admin_social`
      而 `admin_command` 已搬去 `bot_selfconfig` 的错误指引。
      *证据：20 分区逐字段三方比对无差异。*

---

## 预算现状（2026-08-05 重新测量：不是一线杠杆，不必为它砍 prompt）

**结论先说**：此前把「比基线高 29%」当成未达标项，是拿字符数当预算、又假设模型每回合看全部工具，
两个前提都不对。按 token 实测，**每回合工具面开销最重约 9.4K、中位约 6.4K**，
而单分区工具数最多 20，稳稳落在「工具太多导致选择退化」的门槛以下。
**`instructions` 不需要为预算瘦身**，该盯的是别的（见下「真正的成本在哪」）。
*（没有把它换算成「占上下文窗口百分之几」—— 那要知道所服务模型的真实窗口，
本仓配置里只有输出侧 `max_tokens` 和 `max_context_messages: 50`，窗口大小未落盘，
我不替它编一个数。要判断是否吃紧，看的是这里的绝对值加上 `max_context_messages` 撑起的历史长度。）*

### 测量方法

`.venv/bin/python` 建 registry（`register_builtin_tools` + `register_enhanced_tools` +
`register_sticker_tools`，与 `YukikoEngine.__init__` 同三步），
取 `AgentToolRegistry.get_schemas_for_native_tools()` 的返回值序列化后计 token。
本机没有 tokenizer 依赖，另建隔离 venv 装 `tiktoken` 用 `cl100k_base` 计数（不动项目 venv）。
**注意 `cl100k_base` 对中文偏高估**，所以下面的数字是上界，不是精确值。
测于 HEAD `3067695` 的工作区 + 当时并行会话未提交的改动（那批已落为 `70f0f11`，
内容是给 schema 加 `input_examples`，把分区 schema 抬高了约 4%）。
**所以这些数字是「加了 examples 之后」的**，比 `3067695` 本身略高。
重测请重跑同样三步建 registry，不要只调 `register_builtin_tools`（会少 18 个工具）。

### 实测数字

| 项 | 实测 |
|---|---|
| 注册工具数（不含插件） | **179**（`github_enable=false` 时 177） |
| 插件另注册 | 12 个工具名（wayback 3 / self_learning 5 / connect_cli 2 / newapi 1 / example 2） |
| 全量 schema token（179 个相加） | **24,881** —— 但**从不整套发出** |
| 单工具 schema | 均值 139 / 中位 **111** / 最大 553（`analyze_image`）/ 最小 36（`can_send_image`） |
| **单分区 schema token（每回合真实付这个）** | 最大 **2,834**（`sticker_emoji`）/ 中位 **1,991** / 最小 669（`general_chat`） |
| 单分区工具数 | 最大 **20**（`qq_relations`）/ 中位 12 / 最小 3（`general_chat`）—— 声明数最多的是 `web_research` 的 22 |
| `render_system_block()` | 最重 `bot_selfconfig` 9,475 字符 / **7,395 token**；中位 5,981 字符 / 4,440 token |
| `instructions` 行数 | 中位 **48** / 最大 123（`bot_selfconfig`）/ 最小 21 |

**每回合总开销** = `render_system_block` + 该分区 schema：
最重约 **9.4K token**（`bot_selfconfig`），中位约 **6.4K token**。

**线上日志交叉验证**（`navigator_tool_scope`，三份日志共 109 回合）：
模型每回合实际拿到 **3–18 个工具 schema**，分布 3 个×51 回合、11 个×31 回合、17–18 个×9 回合。
**一次也没有出现过 179。** `scoped_tools()` 的硬闸是真在工作的。

### 此前那些字符数并没有错，只是不该当预算读

起始态 5813 / 中位 6013 / 最重 9554 —— 我复测得 6005 / 5981 / 9475，同一量级（差异来自这几天的
prompt 编辑）。问题在于：**字符不是 token，而且分区制下每回合只付一个分区**。
「比基线 4515 高 29%」比的是「20 区丰满说明」对「12 区单薄说明」，
换来的是可达工具从 79 涨到 171，这笔交易本来就该做。

### 真正的成本在哪（如果以后要省，从这里省）

1. **`bot_selfconfig` 的 instructions 123 行 / 4,004 token** —— 是第二名（`general_chat` 2,612）
   的 1.5 倍，也是它把该区总开销顶到 9.4K 的原因。这一个分区值得单独看，不是全体。
2. **分区目录（20 行摘要）每回合都付** —— 已经收缩过一轮（只放能力摘要 + 工具数，
   完整 `when_to_use` 留到进区后；fallback 不进目录）。
3. **schema 里的 examples** —— 并行会话正在加，单工具最大已到 553 token。加之前先看这张表。

### 反过来，两个真实的可达性缺口（比预算重要）

- **8 个已注册工具不在任何分区里，模型够不着**：`can_send_image`、`can_send_record`、
  `get_cookies`、`get_credentials`、`get_csrf_token`、`get_mini_app_ark`、
  `get_robot_uin_range`、`nc_get_rkey`。前五个属能力探测/凭证类，可能是刻意的；
  但**没有任何地方写明是刻意的**，需要业主确认后在此记账。
- **8 个分区声明了但插件不加载就不存在的工具**：`wayback_lookup`、`wayback_extract`、
  `wayback_timeline`、`cli_status`、`learn_from_web`、`list_my_skills`、`newapi_manage`、
  `send_devlog`。插件在跑时它们真实可用（线上 `wayback_lookup` 已成功 3 次），
  但插件被禁用时菜单会指向不存在的工具。
- **`cli_invoke` 不在任何分区**（`cli_status` 在 `web_research`）——
  它在 `core/agent.py` 的权限白名单里、也有 `PromptHint`，但分区目录里没有，
  模型经 `scoped_tools()` 拿不到。*实测：遍历 20 分区的 tools 列表零命中。*

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
      （沿用同文件 `user_profiles_pruned` / `media_memory_pruned` 的 `removed=%d` 约定），
      失败改为 `embeddings_prune_failed` + `warning` + `exc_info`，不再静默。
      *复核：两条日志都在 `core/memory.py` 的 `_cleanup_daily_data` 一带，仍在。*
      *行为验证：400 天前的 5 条，默认 0 时全留；设 7 时全删并打审计行。*
- [ ] ~~**E0-3-old. 无审计的 7 天硬删除**~~ — `DELETE FROM embeddings WHERE created_at < ?`（7 天），
      包在 `except Exception: pass` 里，**不记录删了多少条、失败也完全静默**。
      由 `_cleanup_daily_data` 经快照时机驱动，即**正常聊天就会触发**。
      与愿景 2「永久知识库」直接冲突。*已实测确认调用链。*
      *（原文记的 `:2657-2667` / `:2637` / `:2634` 已漂移；现址在 `_cleanup_daily_data`
      内的 `DELETE FROM embeddings WHERE created_at < ?` 一带，按符号名找。）*

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

- [x] **E1-1. 三套衰减公式已接进读路径** — 已修（`83efd95`）。
      公式本身早就存在（版本快照表、confidence 字段、衰减/新鲜度/强化三式）但从未被调用：
      `_entry_rank` 的第二项原来是裸 confidence，与陈旧程度、召回热度都无关。
      现在 `_effective_score = confidence × 时间衰减 × 召回强化`
      （`_DECAY_PER_DAY` / `_DECAY_FLOOR` / `_REINFORCE_PER_HIT` / `_REINFORCE_CEIL`），
      经 `_entry_rank` → `_rerank_entries` 真正参与排序；
      元组比较语义不变（纠错优先 → 有效分 → 时间兜底）。
      *验证：`core/knowledge.py` 里 `_rerank_entries` 被两条检索路径调用（`:518`、`:531`），
      不是死代码。* 公式刻意复用 `core/memory.py` 那套唯一已实现的模型，不发明第二套。
- [x] **E1-2. 写入质量门已就位并接线** — 已修（`83efd95`）。**此前记为「缺失」是记账过期。**
      HEAD 实测四件都在，且都在活路径上：
      `upsert_conflict_checked`（`core/knowledge.py:297`，写入总入口）、
      `_find_content_duplicate`（`:756`，去重，由 `:333` 调用）、
      `_CONTRADICTION_MARGIN = 0.9`（`:70`，矛盾比对判据，`:378` 使用）、
      `access_count` 字段（`:88` dataclass + `:203` 迁移 ALTER TABLE + `:547` 召回时 +1）、
      `list_stale`（`:556`，陈旧判定，默认阈值 `_STALE_AFTER_DAYS`）。
      **调用链实测非死代码**：`core/engine.py:5637` → `KnowledgeUpdater.update_from_turn_async`
      → `_persist_candidates`（`core/knowledge_updater.py:483`）→ `upsert_conflict_checked`
      （`:405`），返回 `action` 分流成 `inserted`/`updated`/`duplicate`/`disputed`/`rejected` 五种计数。
      `core/agent_tools_knowledge.py` 的 `learn_knowledge` 侧也走同一入口
      （`getattr(kb, "upsert_conflict_checked", ...)`，注释明写不用裸 `kb.add`；
      该文件正被并行会话改动，按符号名 grep 而不是行号）。
      语义刻意偏向不删：条目只会被取代或降权，不会销毁，与 `dc7aebd`（保留期默认永不删）一致，
      清理决策写进 `STREAM_KNOWLEDGE`。
      **与 A11 合并提交的要求已履行** —— `83efd95` 同时删掉了
      `_looks_like_harmful_knowledge_payload`。
- [ ] **E1-3. schema 迁移框架** — 保证升级不静默丢数据；与 `yukiko backup` 的交互要明确。

### E2. 自建 skill / 工具叠加

- [ ] **E2-1. 声明式 skill 定义 + boot loader + 分区可见性绑定** —
      侦察确认 registry 运行时**完全可变**，工具叠加**零机制成本**（已实跑验证）。
      **安全边界（必须守住）**：skill 只能是「按顺序调用已注册工具 + 传参」的声明式编排，
      **不含新代码**。允许模型生成可执行代码 = 任意代码执行，一个被越狱的模型就能拿到 shell。

### E3. 每日日记 + 分离审计流

- [ ] **E3-1. journal 表作为结构化真相源** — 侦察确认素材采集、快照渲染、注入范式全就位，
      缺的是结构化存储 + 用模型自省替代模板串 + 回流（约 10 行）。
- [ ] **E3-2. tool-call 审计流埋点 —— 仍零写入者** — 基础设施已就位（`core/audit.py`，`8530e1c`），
      `engine.audit`（`core/engine.py:94`）已暴露，`AdminEngine` 与 `MemoryEngine` 都拿到了。
      **剩余**：在工具执行处调 `audit.write(STREAM_TOOL_CALLS, ...)`。
      *实测：全仓 `audit.write` 只有两个调用点（`core/memory.py:1926` 写 memory_writes、
      `core/admin.py:262` 写 group_ops），`STREAM_TOOL_CALLS` 除定义与流名白名单外零引用。*
      建议记录：工具名、所在分区、参数校验是否通过、ok/error、耗时、trace_id。
      **不要记原始工具输出**，记大小即可。
      现成的埋点位置：`core/agent.py` 已经在打 `agent_tool_call` / `agent_tool_result`
      两行结构化日志（线上日志里可见），把同一批字段旁路进审计流即可，不用另找注入点。
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
      **三个已踩过的坑必须绕开**（行号已漂移，按符号名定位）：
      ① `_merge_with_defaults`（`core/prompt_loader.py:43`）只回填缺失 key、**从不覆盖已有 key**
      —— 这语义本身是对的（保护手改），但意味着写好的新 prompt 到不了老装机；
      ② `config/prompts.yml` **被 git 跟踪**（*实测：`git check-ignore` 不命中、
      `git ls-files` 命中*），不是运行时状态；
      ③ 模板压过 Python payload —— `load_prompts_template()`（`core/config_templates.py:661`）
      只要模板里的 `prompts` 段非空就返回它，`_built_in_prompts_defaults()` 仅作兜底。
      所以只改 Python payload 会「本机行为正确、全新安装少东西」，反之亦然。
      C3 修好后还多一层：合并结果会再过一遍词表剪枝（`core/prompt_loader.py:84`），
      写进 `prompts.yml` 的 `*_cues` 类 list 键会被删掉并落盘。
      每次自编辑必须可回滚、可审计，且明确哪些段不许模型碰。

### E5. 施工顺序约束（同一文件不允许两波并发）

- [ ] **E5-1. 干净可立即并行的文件** — `core/knowledge.py`、`core/agent_tools_registry.py`、
      `core/prompt_loader.py` 不在关键词清除范围内，E 组可与 A 组全程并行。
      （这是长期约束，不是待办，不勾。）
- [ ] **E5-2. 必须串行的文件** — 现在只剩 `core/admin.py`（A1）与 `core/agent.py`（A7 的
      `_normalize_tool_args`）。`core/engine.py` 的 A2/A3 已做完，该文件不再是串行瓶颈
      —— 但它仍被别的工作流频繁改动，动之前先 `git status`。
      （长期约束，不勾。）
- [ ] **E5-3. `prompts.yml` 追加必须串行** — E1/E3/E4 只做「已存在分区的 tools 列表追加」，
      指定 E2 独占结构性变更（新增分区）。
- [ ] **E5-4. `.migration/vision-plan.md` 第 3 节未写完**（卡在 `SECTION_3_PLACEHOLDER`）—
      五项愿景的逐条数据模型与文件落点细节缺失，开工前需补。
