# 下一窗口工作计划

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
3. `preflight` 实测 59/59 成功但每次花 6.7~10.7 秒。这个交换值得吗?
   对照组:关掉 preflight,让完整 prompt 直接决策,量成功率与耗时。
   **这是本项目最该做的一次 A/B,因为两条路都是活的,数据可以直接对比。**

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
