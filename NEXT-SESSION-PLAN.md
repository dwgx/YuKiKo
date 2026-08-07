# 下一窗口工作计划

> **历史计划 / archive，不是当前 handoff。** 当前接手状态统一看 [`TAKEOVER-2026-08-07.md`](TAKEOVER-2026-08-07.md)。本文件保留旧会话的实验设计与推理；其中的测试计数、运行状态、待办顺序和“已修完”结论可能已被后续工作树改动取代。

> 分支 `refactor/prompt-driven-intent`,基线 `b38cc06`,当前 50 个提交。
> 测试不变量:**0 failed**(通过数会随新增测试涨,不要写死)。
> 两个自检脚本必须 exit 0:`scripts/agent_deep_selfcheck.py`、`scripts/project_takeover_selfcheck.py`。

---

## 0. 先读这一节:什么是实测的,什么不是

这个项目已经被两次「编造的审计结论」坑过。一次编造了两个不存在的配置键
(`selection_rules`、`tool_hints.multimodal_media`),一次把管线顺序写反了。
所以本文件严格区分三类信息,**动手前先看标签**。

**[实测]** = 我跑过命令、读过日志、有数字。可以直接依赖。

**[读码]** = 我读了代码得出的结论,有 file:line。可靠,但行号会漂移,改前重新 grep。

**[转述]** = 来自其它会话的报告,我没独立验证。**动手前必须自己验一遍。**

---

## 1. 已修完的(不要重做)

按提交倒序,每条都有实测证据:

| 提交 | 修了什么 | 关键证据 |
|---|---|---|
| `perf(agent)` | 5 秒 navigator cap 默认关闭 | [实测] provider 小 prompt 延迟 6.7~10.7s,5 秒必然超时;修后 5 秒超时归零 |
| `f340f3a` | 文档纠错 + 密钥脱敏 | [实测] 管线顺序原文写反;`31e0062` 有 3 行明文 token |
| `70f0f11` | `input_examples` 机制 + 12 工具 | [实测] +3.33% token;10 组示例过注册表校验 |
| `8390ef0` | `served_model_state()` | **消费者仍未接** —— 见 §3 |
| 字幕工具 | `extract_subtitle` + 元数据超时 12→30s | [实测] yt-dlp 探测 8.3~12.6s,12 秒擦边 |
| `94ea836` | SSRF 在 fake-IP 下不再拒绝一切 | [实测] 顺带修掉长期红灯的 D3 |
| `bbea828` | QWQ 强制改写拆除 | [实测] 群里已见模型自选 `owo`/`😏`/`(｀・ω・´)` |
| `6d191d2` | 兜底不再把发现类工具输出当答案 | [实测] 「可用模型: 1 个」曾被当回复发出 |
| `6e67d48` | 合成重试调用按 schema 校验必填 | [实测] `search_zhihu` 缺 `mode` |
| `2c52fe7` | tool_call 参数从畸形 JSON 恢复 | [实测] provider 返回 `{}{"url":...}` |

---

## 2. 最高优先级:自动插话(还没开始)

业主原话:「监听默认,感觉人类有需要或者自己可以帮忙觉得有意思就冒出来」。

### 现状 [实测]

```
core/trigger.py _structural_request_signal 只给四类结构定位符打分:
  显式命令令牌 +1.3 / URL +0.7 / 视频号 +0.7 / 文件扩展名 +0.7
trigger.delegate_undirected_min_signal 默认 1.0
→ 中文自然语言恒得 0.00 分,返回 reason="not_directed"
→ core/engine.py:1247-1284 转成 action="ignore"
→ 模型一个 token 都看不到
```

净效果:**只有 `/命令` 能唤醒非指向回合**。连一个裸 URL(0.7)都过不了阈值。
群里那 373 人的自然语言全部在模型之前被丢弃。

### 必须先量的成本 [未做]

删掉这道闸门意味着每条群消息都要过一次判断。开工前先回答:

1. router 是不是比 agent 更便宜的单独一次调用?`should_handle=false` 能不能在
   agent 之前短路?读 `core/engine.py:1739` 起的 `_route_with_failover` 链路。
2. 今天每条消息几次 LLM 调用?删闸门后几次?给具体数字。
3. [实测] provider 是 skiapi,小 prompt 延迟 6.7~10.7 秒,且高频 503
   (`No available accounts` / `All available accounts exhausted`)。
   373 人的群如果每条消息都打一次,这个 provider 撑不住 —— 成本天花板必须先设计好,
   不是上线后再调。

### 已存在、可复用的机制(别重造)[读码]

- `routing.non_directed_min_confidence` 默认 0.8,`ai_gate_min_confidence` 0.75
- `trigger.ai_listen_enable` / `_build_listen_score` / `_decide_ai_probe_reason_by_stats`
- 活跃会话与 followup 窗口(`followup_reply_window_seconds` 默认 20)
- `general_chat` 分区的沉默指令(已在本轮加了作用域,见 `aeed00d`)
- 行为模式:`admin_command` 的 `behavior [冷漠|安静|活跃|默认]`,
  四档分别调 `ai_listen_*` 与 `routing.*` 阈值(`core/admin.py:1616` 起)

**建议方向**:用 router 置信度当成本天花板,而不是发明新机制。让 trigger 把
`_structural_request_signal` 的分数作为**证据**传下去,不再当闸门。

### 会变红的测试 [读码]

`tests/test_config_and_trigger_regression.py` 有多处断言 `not_directed` 丢弃行为
(约 `:31-51`、`:65-77`、`:79-114`、`:116-150`、`:163`)。
项目惯例:**保留测试名和场景,反转断言** —— 契约从「trigger 丢掉它」变成
「模型收到后自己决定不回」。别删测试。

还要同步:`scripts/agent_deep_selfcheck.py:185` 设 `delegate_undirected_to_ai: False`;
`tests/test_engine_bot_strategy_regression.py:155` 断言它是 `False`。

### 防话痨的现成闸门 [读码]

真上线后话痨,最快的止血阀是把 `routing.non_directed_min_confidence` 调到 0
(`core/engine.py` 的 `non_directed_threshold_disabled` 分支)。仍在生效的还有
trigger 注意力门、`router_low_confidence`、以及行为模式的冷漠档。

---

## 3. 已就绪但没接线的三件事

### 3.1 `served_model_state()` 无消费者 [实测]

`grep -rn served_model_state core/` 为空。信号完整(provider / model / depth /
degraded / age_seconds / provider_failover),12 个测试通过,但没人读。

**为什么重要**:provider failover 与 model fallback 是两条独立降级链,
实测见过 `claude-sonnet-5 → sonnet-4-6 → haiku-4-5`。降级到 haiku 后,
调用方仍按 sonnet 的规格发完整工具集 —— 弱模型撑不住时表现为**静默吐坏 JSON**,
不是报错。

**注意不要误判**:本项目的坏 JSON 至少有两个来源。
一是 provider 返回结构性畸形 `{}{"url":...}`(已在 `core/agent.py` 修);
二是弱模型能力不足。别把两者混成一个。

消费端要加在 `AgentLoop` 构建工具载荷之前。收窄方式待定 ——
注意 [实测] 每分区工具数已 ≤20,在 Anthropic 说的 30~50 阈值内,
所以「工具太多」不是本项目的问题,别照搬那个结论。

### 3.2 `finish_reason` 无消费者 [实测]

`services/openai_compatible.py:257-266` 解析了,`grep finish_reason core/` 为空。
`_warn_if_output_truncated` 已加(`:268`),但还没在真实流量里触发过。

`max_tokens` 是 1600(`config/config.yml:20`)。
[实测] 我量了 24 条真实回复,最长 558 字符,远在限内 —— **但这个测量有系统性偏差**:
回复被截断时日志行本身也被截断,样本天然偏向完整回复。
可靠信号只有 `finish_reason == "length"`。**这条至今未被证实也未被推翻。**

### 3.3 `STREAM_TOOL_CALLS` 无写入者 [实测]

`core/audit.py` 定义了五条流,`grep STREAM_TOOL_CALLS` 只有定义处两行。
更要紧的是 **`storage/audit/` 目录在磁盘上不存在**,
所以号称已埋点的 `group_ops` / `memory_writes` 也从未真实落盘。
先查目录该在哪创建、为什么没创建,再谈埋点。

---

## 4. 群里实测发现的行为问题

[实测] 我从 143 条真实群回复里抽了约 58 条。**取样不完整**,而且我只看到机器人的
回复、没看到用户原话 —— 所以「是它挑事还是在反击」无法判断。要完整对照,
先把用户侧消息取出来配对。

### 4.1 需要业主定方向的两条

**能力边界没设。** 它对一张 JetBrains 破解脚本截图准确复述了破解流程、
点名了 `ja-netfilter`,只加一句「正式环境别用哦」配 😏。
图像识别工具会如实描述看到的一切,而 prompt 里**没有任何**关于识别到
盗版/破解/外挂内容该怎么办的指引。要不要设、设多严,是产品决定。

**被骂时情绪化回击。** 「你再这样我真要生气了哦」「哼,就闭嘴呗」。
这**是按设计的** —— `core/personality.py` 明确写「被骂/攻击:可以反击、傲娇、
装委屈,不一味道歉」。不是 bug,是设定在 373 人群里可能不合适。

### 4.2 纯实现,可以直接做

**内部技术状态进群。** 高频出现工具名和错误码:
`analyze_image 又超时了` / `packetBackend 不支持` / `工具那边只拿到了文件信息`。
最糟一条对随机群成员说「QQ空间那边接口报错了…得等帝王尬笑那边修一下代码」——
把 owner 身份和内部代码状态泄露给无关的人。
修法:在回复渲染层剥掉工具名与错误码,或在 prompt 里明确「不要把工具名和
内部错误告诉用户,只说做不到和替代方案」。

**原文复述侮辱内容。** 查精华消息时把「@某人 @某人 你妈也死了」完整复述进群
并加调侃。用户确实主动要求了,但完整复述等于又发一遍。

### 4.3 它做对的、要保住的

- 主动区分推测与核实:「这个是视觉比对出来的,没实际联网核对,你要我再搜一下确认吗」,
  然后真去搜了并报了来源
- 不编造:「工具那边只拿到了文件信息,没提取到字幕或画面细节,所以不敢瞎编讲了啥」
- 拒绝干净不说教:「我不能讨论这个哦~ 换个话题呗?」
- 被质疑时不盲从:「诚实说一句:可可萝这个方向查证是对的…但既然你说完全不对…」

---

## 5. 架构对比:OpenClaw / Hermes 那些手段

**重要边界**:以下关于外部智能体的内容**全部是 [转述]**,来自另一个会话的研究报告
(`.migration/RESEARCH-agent-architecture.md`,307 行,含 25 条被反驳结论)。
我没有独立验证过任何一条。动手前自己查原始来源。

### 5.1 报告建议、且我已实施的

**`input_examples`** —— 报告称把复杂参数准确率从 72% 提到 90%。
已实施(`70f0f11`),但**我不声称达到那个数字**,那是转述。
[实测] 成本是 +3.33% token。

**Hermes 的反思节点** —— 报告称 `<scratch_pad>` 的 GOAP 四段式
(Goal/Actions/Observation/Reflection)里,Reflection 段强制自查
「工具是否相关、必填参数是否齐备」。
我把它做成了 `think` 工具描述里的**建议**而非强制结构(`70f0f11`),
理由是强制填表违背「完全信任模型」。
**效果未证实** —— 真正拦住重复调用的仍是 `max_same_tool_call=3` 与 schema 校验。

### 5.2 报告建议、但我明确不做的

**「代码侧不修参数,把 traceback 原文丢回模型要求重调」**。
理由:本项目的畸形参数是 **provider 的结构性 bug**(`{}{"url":...}`,
空对象粘着真参数),不是模型的错。丢回给模型等于让它替 provider 补漏,
每次多烧一轮。这条我保留分歧,下一个窗口若不同意请先复现那个畸形串再讨论。

### 5.3 报告自己标注为不可信的

- 「工具超过 30~50 个准确率下降」「58 个工具吃 55K token」是 Anthropic 官方数据,
  但 [实测] 本项目每分区 ≤20 个工具、单回合最大 2,834 token,
  **不能把 55K 那个数搬到本项目头上**,progressive disclosure 不是第一顺位杠杆。
- OpenClaw 的 skill 允许执行代码(官方示例字面写 exec 跑 shell),
  本项目 E2-1 的「声明式编排、不含新代码」比它更严 —— 别拿 OpenClaw 当放宽的先例。
- OpenClaw 的那道门是「晋升门」不是「写入门」,episodic 写入是
  append-and-index、首次写入不做去重和矛盾比对。

### 5.4 我建议下一个窗口自己做的对比

不要照抄报告结论。要做的是:**拿本项目的真实日志去验证每条建议是否适用**。
具体三个可量化的问题:

1. 本项目 183 个工具里,模型真正调用过的有多少?调用分布是什么?
   如果长尾工具从未被调用过,那才是 progressive disclosure 的依据 ——
   而不是「Anthropic 说超过 50 个就降准确率」。
2. 坏 JSON 的真实来源分布:provider 畸形串 vs 输出截断 vs 模型能力。
   前者已修,后两者要靠 `finish_reason` 和 `served_model_state()` 才能分开。
3. ~~`preflight` 实测 59/59 成功但每次花 6.7~10.7 秒~~ —— **该数字是错的,已重测,见 §9。**

---

## 6. 还没查清的

**vision 的图片下载失败。** [实测] `vision_image_ref_empty` 在 yk13 出现 6 次、
yk11 出现 19 次,URL 是 QQ CDN(`gchat.qpic.cn`)。
`_download_image_as_data_uri`(`core/tools_vision.py:1159`)有**四条静默 `return ""`**
(非 200 / 空数据 / 超大 / 护栏拒),一条日志都没有,所以不知道死在哪。
我已排除尺寸(`small_image_warning` 零出现,实际图 12KB~3.9MB,上限 6MB)。

注意:`:1095` 那条「provider 在 {anthropic,gemini,skiapi} 时下载失败直接返回空串、
不回退直传 URL」看起来像 bug,但注释的理由可能是对的 ——
QQ CDN 对外不可达时,skiapi 的服务器同样拿不到那个 URL,回退只是把失败挪个位置。
**先查清下载为什么失败,再决定回退策略。**
另一条值得试的路:`core/agent_tools_napcat.py` 的 `get_image` 走 NapCat 自己的
API 拿文件,而不是直连 CDN。

**vision 的 API 失败是容量问题,不是格式问题。** [实测] 报文是
`503 Service Unavailable` + `No available accounts` / `All available accounts exhausted`。
31 成功 / 12 失败,**间歇性而非全挂**。别去改请求构造。

**图片生成是 provider 缺能力。** skiapi 的 `/v1/images/generations` 报错。
不是代码 bug,换 provider 或这个能力就是废的 —— 业主决定。

---

## 7. 工作方式(踩过的坑,别重踩)

- **绝不用 heredoc 写文件**。会让该会话的 Bash 永久静默且转录零记录。用 Write/Edit。
- **Bash/Read 会间歇性静默**。按工具失效不按会话,通常自愈。处理顺序:换工具确认范围 →
  等 → 不要原地重试同一条命令。本轮发生过多次。
- **改配置键要同时改两处真相源**(`master.template.yml` + `_built_in_config_defaults()`)。
  本轮已修三个「读取但未定义」的键:`routing.fragment_join_enable`、
  `video_resolver.metadata_timeout_seconds`、`agent.navigator_preflight_plain_text`。
  这类 bug 的症状是「升级安装行为与模板不一致」。
- **prompt 三源必须同步**:Python payload / `master.template.yml` / `config/prompts.yml`。
  模板是手工维护的折行 YAML,`safe_dump` 整写会产生 600+ 行 churn 盖住真实改动 ——
  做定点文本替换,并用「解析后与 Python 载荷比对相等」作为唯一验收标准。
  折行双引号标量的续行符 `\` 漏掉会把缩进写进 prompt 正文,本轮踩过。
- **删「孤儿」符号前 grep 整仓**(含 tests/ 和 scripts/)。本轮发现
  `scripts/agent_deep_selfcheck.py` 从 A6 起就一直崩,因为它调了被删的符号 ——
  而 CLAUDE.md 恰恰要求改 prompt/routing/agent 前先跑它。
- **新测试必须在基线上红**。`git stash push <files> -q` → 跑 → `git stash pop -q`,
  两边结果都报出来。不红的测试证明不了任何东西。
- **ruff 逐文件与基线对比**:
  `git show HEAD:<f> > /tmp/b.py && ruff check --stdin-filename <f> - < /tmp/b.py`。
- **并发 agent 的文件集必须互不重叠**。本轮四条车道各自持有独立文件,
  `core/agent.py` 与 `core/engine.py` 由主会话串行持有 —— 这两个是冲突高发区。
- **不要信 agent 报告,读 diff 和代码**。本轮四条车道里,一条报的 C8 经我验证不成立
  (能力存在且survives),一条纠正了我三处错误(token 数、vision 严重度、
  「真实群验证」的精确性)。双向都要验。

---

## 8. 一件需要业主决定的事

[实测] `31e0062` 提交里有 3 行明文 token(`WEBUI_TOKEN`、`ONEBOT_ACCESS_TOKEN`、
NapCat token)。工作副本已脱敏。

当前实际暴露面**约等于零**:`git remote -v` 为空、从未推送,
能读 git 历史的人同样能直接读 `.env`。风险只在将来推送到公开远端时成立。

历史重写不可逆,所以没做。两个选项:现在轮换 token,或者记住
「推公开远端之前必须先处理」。

---

## 17. 本轮落地结果与仍需业主决策的事

### 状态

```
1086 passed / 12 skipped / 0 failed
两个自检 exit 0（agent_deep_selfcheck 0 个 FAIL；takeover status=PASS）
ruff 已跟踪 py 文件合计 86 → 72，零新增
```

四条并发车道 + 四份独立对抗复核。**四份复核全部判 defective,共 8 条 high**,
其中 7 条本轮已修,1 条(prompt 回落)随模板同步一并解决。

### 业主五条诉求的实测结果(用合并后的真实配置跑)

| 诉求 | 场景 | 结果 |
|---|---|---|
| 喊 yuki / yukiko | 纯文字 / 表情包+喊 / 裸图+喊 | `name_call` 全部回 |
| @机器人 | — | `directed` 回 |
| 一堆人在讨论可以插嘴 | 4 人在聊 | `ai_listen_probe_heat` 回 |
| 有人求助 | 群里在聊 + 求助句 | `ai_listen_probe_heat` 回 |
| **发一张图不该说话** | 裸图 / 裸图+30 秒前说过话 / 裸图+5 分钟前说过话 / 别人的表情包 | 全部 `media_only_no_text` 沉默 |
| 单人独聊时的普通提问 | — | `not_directed` 沉默 |

关键词池污染已彻底清除:`_strip_context_row_prefix` 把机器前缀行剥成空,
实测 `image` / 昵称 / `qq` 的命中数**全部归零**(修复前 `image` 必然命中 1)。
旁听配额实测生效:20 条消息跨 17 分钟恰好 6 次,不设上限会是 20 次。

热加载:`reload_config()`(`core/engine.py:826`)会重建 `TriggerEngine` 并 `_pl.reload()`,
所以 `/yukibot` 一条命令生效,**不用重启**。

### 本轮修掉的 high

1. **裸媒体门吃掉「表情包 + 喊 yuki」**(违反第一条诉求)。根因是 `normalize_text` 压掉了
   `app.py:1487` 拼的换行,而图片 summary 是没有右边界的自由文本。
   改法:engine 用 `_user_typed_text_for_trigger()` 按 **message.text 的换行**精确切,
   把 `media_types` / `user_text` / `has_user_text` / `trace_id` 显式交给 trigger。
   **不要改用 `_extract_multimodal_user_text`** —— 它的 `image:\s*\S+` 里那个 `\s*`
   会把冒号后的下一个词吃掉,`image:[image] yukiko 看看` 被吃成 `看看`,别名没了。
   我先踩了这个坑,又踩了「`image:` 残渣让 `has_user_text=True`、裸媒体门失效、
   active_session 内发裸图照样说话」那个坑,才换成按行切。
   测试 `tests/test_trigger_media_facts_wiring_regression.py`(基线 15 红)。
2. **6 个配置键两处真相源都缺** → 已落盘,值与代码兜底逐个相符。
3. **prompt 三源不同步打红 3 个既有测试** → 用定点块替换同步,churn 只有 79/-49 行
   (整体 `safe_dump` 会产生 600+ 行盖住真实改动)。脚本做法见 §15 同类。
4. **`transcribe_audio_enhanced` 调用点无 try**,而 `utils/media.py` 只把
   `import whisper` 包在 try 里,`whisper.load_model()` 裸奔 → 异常会被
   `core/agent_tools_registry.py:588` 包成 `tool_exception: OSError: ...` 写进 `error`,
   而 `error` 回喂给模型。已补分类捕获。
5. **`_VOICE_ERROR_ALIAS` 把 `whisper_not_installed` 塞进 `error`** → 已删别名,
   三处 failure_policy 文案统一改成 `voice_engine_unavailable`。
6. **守卫回喂 payload 归属错误三连**(同源:没区分该工具最近一次成功还是失败):
   失败原因取自全量 steps 而非该工具自己的 → 别的工具的错误被说成它的;
   先成功后崩溃时抢先说「上一次调用已经成功」,真实错误整条丢掉;
   产物取「最后一次成功」与被拦那次无关。
   已改成:最近一次失败就以失败为主叙述、更早产物挂 `earlier_partial_result` 单独标出。
   测试 `tests/test_guard_feedback_attribution_regression.py`。
7. **两个 prompt 文案键只活在 Python 内置默认里** → 已落两处真相源。

### 仍需业主决策(代码侧已尽力,剩下的不是代码问题)

1. **语音要不要真出文字。** 解码这一半已打通(pilk 实测 6/6 出有声 wav),
   但 ASR 引擎不存在。三条路:faster-whisper(不拖 torch,车道推荐)/
   openai-whisper + torch(约 2.5GB)/ 打 provider 的 `/v1/audio/transcriptions`
   (需先探测 skiapi 是否提供该端点)。**现状是「诚实说听不了」而不是让用户白重录。**
2. **`core/personality.py:31`「被骂可以反击」** 在 373 人群里可能不合适,
   而且它不区分「骂的是机器人本人」和「机器人在转述别人骂别人」——
   后者正是精华消息事故的形状。这是人格设定,没动。
3. **音乐上游挂了**(`mc.alger.fun` 实测 HTTP 000,HTTPS 证书错误,且用明文 HTTP)。
   换 provider 或放弃这个能力。代码侧唯一该做的是快速失败而非烧 55 秒。
4. **`agent.vision_enabled`** 见 §16 —— 打开前必须先让那段复用 data-URI 转换。

### 未处理的 medium/low(复核提出,本轮没做)

- **B 层泄漏仍在**:`core/agent.py:5124-5131` 的 `fail_hint` 由
  `f"{step['tool']}:{step['error']}"` 拼成,喂给硬编码在 `:5445-5451` 的 system prompt
  (全文无输出卫生约束);`_build_fallback_result`(`:5100-5121`)把失败 step 的 display
  截 280 字直接当回复发出,**连 LLM 都不经过**。
  prompt 层改动压不住这两条 —— 业主在群里仍可能看到工具名。这是下一个窗口的首要项。
- `_ONCE_PER_TURN_TOOLS` 的 `repeat_limit=1` 数的是调用次数不是成功次数,
  副作用工具第一次瞬时失败后同 args 重试会被拦死(相对基线的行为回退)。
- `max_same_tool_call` 仍是 3,非副作用工具仍会真执行 3 次
  (实测 `send_face` 连发 3 个表情、`remember_user_fact` 写 4 次同一条)。
- `core/system_prompts.py:78-85` 那条「工具 display 不要转发给用户」只存在于
  `personality_system_prompt` 的 hardcoded fallback,唯一调用点是 thinking 通道,
  **agent 路径永远读不到**。同处 5 个点路径在两份 YAML 里零命中。留着是假安慰。
- `core/agent.py:1773` 的 `sticker_tool_used` 是死代码(判 `s.get("result")`,
  而 steps payload 从不写这个键),其后「表情工具跑完清空 final_answer 媒体」从未执行。
- 裸媒体仍计入群热度,刷图会把热度顶上去让之后的文字消息更易触发旁听。
- `app.py:1481` 的 `_try_extract_voice_text` 对每条带媒体消息无条件打一次
  `get_record`,而 NapCat 没有任何语音转文字 API —— 100% 白调用,且两处
  `except Exception: pass` 静默吞掉失败。

### 仓里有两个未处理的 stash

```
stash@{0}  2026-08-05 23:36  core/trigger.py +502/-46   ← L1 车道的另一个实现
stash@{1}  2026-08-05 23:15  agent_tools_media/napcat/media.py +472/-127  ← L2 的早期版本
```

都是车道做「基线红」验证时 `stash push` 后没 pop 干净的残留。
工作区里的版本与它们**不同**且测试全绿,所以我没有 pop(pop 会覆盖工作区)。
`stash@{0}` 走的是「关掉近期 token 自动升级成热词」那条路,
工作区版本走的是「剥掉机器前缀」—— 后者保留了真·复现用户词的能力。
业主若要对比可以 `git stash show -p stash@{0}`,**不要直接 pop**。

---

## 10. 日志分析方法论(先读这条,否则会把已修问题当现状)

`storage/logs/yukiko.log` **跨了一整天,期间落了多个修复提交**,而且机器人仍在运行、
日志还在增长。拿全段统计当"现状"会系统性误判。本轮我自己踩了这个坑两次:

- 先说"SSRF 误杀是找视频失败的根因" —— 实际 31 次拦截全在 05:04~06:58,
  修复提交 `94ea836` 是 16:04,之后 **0 次**。
- 又说"5 秒 navigator cap 每回合白烧" —— 54 次 5.0s 超时最后一次是 21:10:24,
  修复提交 `c19a285` 是 21:18:47,早 8 分钟。

**做法**:先 `git log --format="%ai %s"` 拿到相关修复的时间,再按该时间截断日志统计。
样本会变小(21:18 之后只剩约 700 行),那就明确说样本小,不要拿大样本的旧结论顶替。

### 按最后一个修复(21:18:47)截断后的真实现状 [实测]

```
agent 收场    15/15 = 100% agent_final_answer      ← 循环健康
trigger 判定  not_directed 46.7% / 关键词插话 30.0% / directed 16.7% / followup 6.7%
工具          analyze_image 6/7=86% | analyze_voice 0/3=0% | 其余全 100%
SSRF 误杀     0
```

`agent_fallback_repeated_tool_call` 在这个窗口 **0 次**。全日志 24 次里
**一次 search_media 都没有**(L3 车道诊断纠正了我这条),命中的是
parse_video(4)/analyze_image(3)/analyze_video(2)/split_video(2)/get_group_info(2) 等。

---

## 11. 「发一张图它就说话」的机械根因 [实测,已独立复现两次]

这不是阈值问题,是**关键词池被机器生成的占位文本自污染**。

链路:

```
app_helpers.py:964-990  _build_multimodal_text
  裸图 → 'MULTIMODAL_EVENT user sent multimodal message: image:[image]'
core/engine.py:1058     _record_runtime_group_chat(message, text)
  把上面这行原样写进群上下文缓存
core/engine.py:4753     line = f"{昵称}(QQ:{user_id}): {content}"
  再套一层机器前缀
core/trigger.py:857-868 _match_memory_keywords
  从近 48 行里取出现 ≥2 次的 token 当热词池
core/trigger.py:739-743 keyword_hits >= 1 时**直接 return**,跳过 ai_listen_min_score=2.4
```

实测热词池内容:`['image','message','multimodal','multimodal_event','qq','sent','user']`

对照实验(同一条裸图,只换上下文):

| 上下文 | 命中数 | 结果 |
|---|---|---|
| 前面有两张裸图 | 1 | **触发插话** |
| 普通中文聊天 | 0 | 不触发 |

附带损害:`昵称(QQ:xxx):` 前缀让**昵称本身和 token `qq`** 也变成热词 ——
实测 `'小明 在吗'` 与 `'有人在qq上吗'` 都会触发,`'今天下雨了'` 不会。

关键点:`core/trigger.py:739-743` 那条 `keyword_hits >= min_keyword_hits` 是**硬 return**,
在 `score < ai_listen_min_score` 之前,所以 `ai_listen_min_score=2.4` 在关键词命中时
根本不参与判断。调那个阈值不会有任何效果。

### 纠正:active_session 不是主因 [L1 车道纠正,已验证]

我原先以为 `core/trigger.py:444` 的 active_session 无条件放行会让"别人之间的对话
因为 8 分钟前有人跟它说过话而每条都回"。**不成立** —— `_session_key`
(`core/trigger.py:236-242`)群聊返回 `f"{conversation_id}:{user_id}"`,是
per (会话,用户) 而非 per 群。日志侧吻合:87 次 active_session 里 51 次是同一个人。

媒体轮的真实分布:97 个有 trigger_decision 的媒体 trace 里
keyword 31 / active_session 26 / followup 13 / directed 1。

### 业主抱怨的量化 [实测,21:18 之后窗口]

9 次关键词探测里 **7 次真的发言了**,同期被真正点名只有 5 次 ——
它主动插话比被叫说话还多。另外 2 次模型自己选择了沉默(空 final_answer),
那是设计意图在起作用,但只占 22%。

---

## 12. 语音:两段都断,第二段需要业主决策 [实测,已独立复现]

我原先的前提("NapCat spawn EPERM 是根因"、"ffmpeg 该靠探测而非扩展名")
被 L2 车道推翻,推翻是对的:

**第一段:格式。** 语音字节是**腾讯 SILK v3**,不是 amr 也不是 mp3。
`storage/cache/voice/*.mp3` 前 12 字节实测全部是 `0223 2153 494c 4b5f 5633`
= `0x02` + `#!SILK_V3`。本机 ffmpeg 的 silk demuxer 数量 = **0**,
所以"靠探测"这条路根本不存在。日志里那句
`Format mp3 detected only with low score of 1` 是 ffmpeg 探测完的结论
(探测分 1 = 没有任何 demuxer 匹配),不是扩展名 bug 的证据。

好消息:**`pilk==0.2.4` 已经钉死在 `requirements.txt:19`,而且能解开这批文件。**
我实测 3 个文件全部 `pilk.silk_to_wav(rate=24000)` 成功,
ffmpeg volumedetect 得 mean_volume -15.6 ~ -18.6 dB —— **是真人声,不是静音**。
仓里只有编码方向的 silk 代码(`core/music.py`),缺的就是"调用它解码"这一步。

**第二段:ASR 引擎不存在。** 这是 9/9 次失败的直接原因,我原先完全没看到:

```
.venv/bin/python -c "import whisper"  → ModuleNotFoundError
grep -icE "whisper|torch" requirements.txt → 0
storage/logs/yukiko.log 里 "whisper not installed, run: pip install openai-whisper" × 9
  （utils/media.py:307）
```

而且失败被伪装成用户的错:`utils/media.py:305-308` 的 ImportError 分支返回
`{"text":"", "score":-999, "pass":"error"}`,`pass` 字段**本来区分了** error 与 none,
但调用方 `core/agent_tools_media.py:1073` 只判断 `if text:`,把信号丢了,
于是每次都吐"语音转录结果为空,可能是静音或无法识别",模型据此让用户重录。

**需要业主决策**:代码里只有本地 whisper 一条路,没有 API 转写路径。
1. `pip install openai-whisper` —— 会拉 torch(2GB+),本地跑,免费
2. 新增 API 转写通道 —— 要先确认 provider 支持,是新代码
3. 先不修转写,但把"引擎没装"如实说成做不到,别再让用户重录

第 3 条是最小改动且无论如何都该做。1 和 2 是业主的选择。

---

## 13. 顺手修掉的真 bug:QZone 五个工具全废 [实测,已修]

`core/agent_tools_social.py` 的 `_make_qzone_handler` 引用了 **6 个既没导入也不在
本模块定义**的符号(`_resolve_qzone_config` / `_normalize_qzone_tool_error` /
`_safe_int` / `_qzone_profile_payload` / `_qzone_mood_payload` / `_qzone_album_payload`),
实现全在 `core/agent_tools_web.py`。线上表现:

```
analyze_qzone 失败: tool_exception: NameError: name '_resolve_qzone_config' is not defined
get_qzone_profile 失败: tool_exception: NameError: ...
```

handler 是闭包,NameError 只在**真被调用时**才抛 —— 注册、schema 校验、启动自检
全都看不出问题。ruff 在基线上就报了这 13 个 F821,而 `pyproject.toml` 的
per-file-ignores **没有**放行这个文件,所以那些告警是真的。

修法是在 handler 内一次导入全部 6 个。教训:第一版我只补了第一个符号,
测试也只跑到 cookie 检查就 return 了,于是 4 绿而另外 5 个仍是 NameError ——
所以 `tests/test_qzone_handler_nameerror_regression.py` 里加了一条
**AST 扫描函数体、逐个查符号可解析性**的测试,而不是手抄名单。

---

## 14. 音乐工具 0% 成功:上游挂了 [实测]

`music_search` 5/6 撞 28s 超时、`music_play` 5/7 撞 55s,成功率都是 **0%**。
根因不在本仓:`mc.alger.fun` 返回 503,我 curl 实测现在 HTTP 000(彻底不可达),
HTTPS 版证书错误。而且它用的是**明文 HTTP**。

代码侧唯一该做的是**快速失败**而不是烧 55 秒重试。换 provider 或放弃这个能力
是业主的决定。

---

## 15. 参数真相源补齐 [已修]

`trigger` 段有 **9 个键**代码一直在读、两处真相源都没有,所以升级安装行为与模板
不一致,WebUI 配置页也看不见,业主没法调:

```
active_session_timeout_minutes(8) / ai_listen_interval_seconds(45) /
ai_listen_keywords([]) / busy_window_seconds(60) / overload_enable(true) /
overload_min_messages(20) / overload_min_unique_users(3) /
overload_notice_cooldown_seconds(90) / overload_pause_seconds(45)
```

补进去的全部是各自的代码兜底原值,**零行为变更**,只为可见可调。
`tests/test_trigger_config_truth_sources_regression.py` 里有一条兜底测试会扫
`core/trigger.py` 所有 `trigger_config.get(...)` 键名,将来新增读取点漏补会直接红。

全仓同类问题还剩 **20 个键**(routing 15 / agent 4 / mode 1),清单见
`orphan_keys` 分析法:比对 `_built_in_config_defaults()` / `master.template.yml` /
代码里的 `.get(key, default)`。其中 `agent.vision_enabled` 尤其值得注意 —— 见 §16。

---

## 16. agent.vision_enabled:模型从没原生看过图 [实测,未改]

`core/agent.py:3605-3607` 读 `agent_cfg.get("vision_enabled", False)`,而这个键
**两处真相源都没有**,所以恒为 `False`。后果:模型从来没有原生收到过图片,
每次都得绕 `analyze_image` 工具(它因此成为第二高频工具,75 次调用)。

当前 provider 是 skiapi + `claude-sonnet-5`,**模型本身支持 vision**,能力被白白浪费。

**但不要直接打开它。** 那段代码(`core/agent.py:3609-3625`)把 `raw_segments` 里的
`url` 原样塞进 `image_url` 块,而那些是 QQ CDN 链接 —— 我 curl 实测对外不可达:

```
HTTP 400 {"retcode":-5503022,"retmsg":"appid is not supported"}   ← multimedia.nt.qq.com.cn
HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"} ← 注释里原本只记了这条
```

打开它只会让模型收到一堆死链。正确做法是让那段复用
`core/tools_vision.py` 已经跑通的 data-URI 转换 ——
实测 `source=onebot_get_image` 45 次全部成功、`source=onebot_local_file` 61 次全部成功,
而 `source=http_url` 直连 CDN 50 次全败。NapCat 那条路是通的。

顺带纠正一条我自己的误判:那 50 次 `vision_image_ref_empty | source=http_url`
**不是最终失败**,失败后会回退 NapCat。图片解析实际 59/61 成功。
`analyze_image` 真正的失败是 `tool_timeout_seconds_media`(45s)超时 ——
16:04 之后的窗口里 11 次失败有 10 次是它。

超时调参依据(实测工具耗时):跑完的 `analyze_image` p50=11s / p90=19s / max=42s,
所以 45s 正好卡在临界点。带媒体的回合总预算实测 `outer=286s inner=280s`
(`agent_timeout_budget` 日志),抬到 60~70s 不会挤爆总预算。
注意 21:18 之后的窗口里 7 次调用 0 次超时,样本太小,不足以证明超时已消失。

---

## 9. preflight 重测结果(推翻 §5.4 第 3 条的前提)

### 「59/59 成功」是错的 [实测]

那个数字只数了 `navigator_preflight_section` 一条成功日志,没数失败与静默两类。
按 trace 重新统计 `storage/logs/yukiko.log`(脚本见本节末):

```
general_chat 起步的回合            186   ← 全部跑了 preflight
├─ 选出新分区                       61   自身耗时 p50 6s / p90 12s / max 18s
├─ 撞 20s 超时上限后放弃            38   白烧 633s,随后完整 prompt 照跑
└─ 静默 return None                 87   此前零日志
真实成功率 = 61/186 ≈ 32.8%
```

### 为什么之前看不见那 87 次 [读码]

`_navigator_timeout_section_retry`(`core/agent.py`)有**三条不打日志的 `return None`**:
模型选了同一分区 / 未知分区 / JSON 解不出。三条都已经付掉了 4~20 秒的 LLM 延迟,
但在日志里完全不存在。**本轮已补 `navigator_preflight_noop`**,带 `outcome=` 与
`elapsed=`,三条各自可计数;超时那条补了 `exc=` 与 `budget=`
(`asyncio.TimeoutError` 的 `str()` 是空的,原先日志尾部是个空字段)。
回归测试:`tests/test_navigator_preflight_observability_regression.py`,基线 5 红。

### 关键结论:这个 A/B 之前无法做 [实测]

我先按「有无 preflight 日志」分组做过一次对比,结果是对照组全面更好
(87.8% vs 54.1% 成功率、19.5s vs 26.3s p50)。**那个对比是无效的**,
因为「没有 preflight 日志」不等于「没跑 preflight」。

判定方法:量 `navigator_section_selected` 到下一条事件的间隔。
无 preflight 日志的那 87 个回合,间隔 **min 8s / p50 17s,没有一个低于 4s**;
而一次成功的 preflight 自身只要 4~18s。间隔下限被抬到 8s 说明调用确实发生了。
所以那 87 个是**静默 arm**,不是对照组 —— 日志里根本没有对照组。

### preflight 恒为开,且 config.yml 里看不出来 [实测]

`config/config.yml` **没有** `agent.navigator_preflight_plain_text` 这个键。
`core/agent.py:408` 的代码兜底是 `False`,但 `core/config_manager.py:60` 走的是
`deep_merge_dict(template, raw)` —— 模板在底,所以模板的 `true` 生效。
这解释了 186/186 全部跑 preflight。**看 config.yml 会得出相反结论,别只看它。**

### 下一步:真正做这个 A/B

arm B 需要显式关闭(不能靠删键,删了会被模板补回 true):

```yaml
# config/config.yml
agent:
  navigator_preflight_plain_text: false
```

改完 `/yukibot` 热重载即可(`reload_config()` 会重建 agent 配置)。
跑够量后按 trace 统计两个 arm 的 `agent_done reason=` 分布与 `time=`。
统计脚本本轮写在 `/tmp` 未入库,重写很快:按 `trace=` 分组,
取第一条 `navigator_section_selected` 的 `section=`,再取 `agent_done` 的
`time=` 与 `reason=`,只保留 `section=general_chat` 的回合。

**我没有替业主改这个配置** —— 它改的是线上行为,且 arm B 期间要有人看着群。

### 顺带发现,未处理 [实测]

- 20s 超时上限在这台机器上**恒等于满额**:`timeout = min(20, remaining - 2)`,
  而 `remaining` 实测约 112s,所以每次失败都是整 20 秒。38 次 = 633 秒。
- `preflight_timeout` 那 38 个回合里 `agent_llm_error` 占 14 个(36.8%),
  而静默 arm 只有 2/82(2.4%)。**相关性,非因果** —— 可能是白烧 20 秒后
  撞上 skiapi 的 503 窗口,也可能是难题本来就更容易两头都失败。要判因果得看 arm B。
- 静默 arm 的下一条事件里有 35 次 `agent_tool_args_recovered_from_malformed_json`
  (占 40 次 `agent_tool_call` 之外的大头),与 §3.2 的坏 JSON 来源分布直接相关。
