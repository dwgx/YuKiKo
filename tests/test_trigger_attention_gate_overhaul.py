"""L1 注意力门大修的行为契约。

业主的抱怨是「很机器人、人机、烦人」，具体到门控层就是两件事：
一个人发一张图它也说话；8 分钟活跃窗口内群里每条消息都跑一次完整 agent 回合。
这个文件钉住修完之后的行为，**每条断言在修前都是反的**。

注意：这里大量使用 app_helpers.py `_build_multimodal_text()` 生成的占位符字面量。
那是有意的 —— trigger 在 engine 不填 media_types 时靠解析这串文本认媒体段，
所以格式一改必须先在这里红，而不是让裸媒体门静默失效。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

from core.trigger import TriggerEngine, TriggerInput

# 与 app_helpers.py `_build_multimodal_text()` 逐字对齐；engine 侧 normalize_text()
# 会把媒体行与用户输入行之间的换行压成空格，所以这里也用单行拼。

_MEDIA_PREFIX = "MULTIMODAL_EVENT user sent multimodal message: "

_MEDIA_PREFIX_AT = "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: "

_BARE_IMAGE = f"{_MEDIA_PREFIX}image:[image]"

_BOT_CONFIG = {"name": "YuKiKo", "nicknames": ["yuki", "yukiko", "雪"]}


def _make_engine(**overrides: object) -> TriggerEngine:
    config: dict[str, object] = {
        "ai_listen_enable": True,
        "delegate_undirected_to_ai": True,
        "ai_listen_min_messages": 3,
        "ai_listen_min_unique_users": 2,
        "ai_listen_min_score": 2.4,
        "ai_listen_interval_seconds": 0,
        "followup_reply_window_seconds": 30,
        "followup_max_turns": 3,
    }
    config.update(overrides)
    return TriggerEngine(config, _BOT_CONFIG)


def _payload(
    text: str,
    *,
    user_id: str = "1001",
    conversation_id: str = "group:1",
    timestamp: datetime,
    mentioned: bool = False,
    is_private: bool = False,
    **extra: object,
) -> TriggerInput:
    return TriggerInput(
        conversation_id=conversation_id,
        user_id=user_id,
        text=text,
        mentioned=mentioned,
        is_private=is_private,
        timestamp=timestamp,
        **extra,
    )


class MediaOnlyGateTests(unittest.TestCase):
    """裸媒体（只有图/视频/语音段、没有文字）不该独自触发回复。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_should_stay_silent_when_bare_image_arrives_after_active_session_expired(
        self,
    ) -> None:
        """裸图 + active_session 已过 free window → 沉默。

        修前：active_session 命中即无条件 should_handle=True，
        8 分钟窗口内这个人发的每张图都跑一次完整 agent 回合。
        """

        engine = _make_engine(active_session_free_window_seconds=90)
        engine.activate_session("group:1", "1001", False, self.now)

        result = engine.evaluate(
            _payload(_BARE_IMAGE, timestamp=self.now + timedelta(seconds=200)),
            recent_messages=[],
        )

        self.assertFalse(result.should_handle)
        self.assertEqual(result.reason, "media_only_no_text")
        # active_session 仍作为证据保留，engine 侧还在读这个字段。
        self.assertTrue(result.active_session)

    def test_should_report_media_only_reason_that_engine_cannot_upgrade(self) -> None:
        """裸媒体的 reason 不能是 ai_router_candidate。

        core/engine.py 会把 ai_router_candidate 升回 should_handle=True，
        用那个 reason 等于一分钱都没省下来。
        """

        engine = _make_engine()

        result = engine.evaluate(
            _payload(_BARE_IMAGE, timestamp=self.now), recent_messages=[]
        )

        self.assertFalse(result.should_handle)
        self.assertNotEqual(result.reason, "ai_router_candidate")
        self.assertFalse(result.listen_probe)

    def test_should_stay_silent_for_bare_video_and_voice_without_transcript(
        self,
    ) -> None:
        """裸视频、裸语音（转写失败）同样不独自触发。"""

        engine = _make_engine()

        for text in (
            f"{_MEDIA_PREFIX}video:/Users/dwgx/storage/media/9eaa.mp4",
            f"{_MEDIA_PREFIX}video:https://cdn.example.com/a/b.mp4",
            f"{_MEDIA_PREFIX}record:/tmp/x.amr",
            f"{_MEDIA_PREFIX}forward",
            f"{_MEDIA_PREFIX}image:[image] | video:https://cdn.example.com/a/b.mp4",
        ):
            with self.subTest(text=text):
                result = engine.evaluate(
                    _payload(text, timestamp=self.now), recent_messages=[]
                )
                self.assertFalse(result.should_handle)
                self.assertEqual(result.reason, "media_only_no_text")

    def test_should_reply_when_mentioned_or_private_even_with_bare_image(self) -> None:
        """回归保护：@ + 裸图、私聊裸图仍然回 —— 这是用户点名要的。"""

        engine = _make_engine()

        mentioned = engine.evaluate(
            _payload(
                f"{_MEDIA_PREFIX_AT}image:[image]",
                timestamp=self.now,
                mentioned=True,
            ),
            recent_messages=[],
        )
        self.assertTrue(mentioned.should_handle)
        self.assertEqual(mentioned.reason, "directed")

        private = engine.evaluate(
            _payload(
                _BARE_IMAGE,
                conversation_id="private:1001",
                timestamp=self.now,
                is_private=True,
            ),
            recent_messages=[],
        )
        self.assertTrue(private.should_handle)
        self.assertEqual(private.reason, "directed")

    def test_should_treat_voice_transcript_as_user_text(self) -> None:
        """`[语音内容] xxx` 是用户说的话，不能被裸媒体门拦掉。"""

        engine = _make_engine()
        engine.activate_session("group:1", "1001", False, self.now)

        result = engine.evaluate(
            _payload(
                f"{_MEDIA_PREFIX}record:/tmp/x.amr [语音内容] 帮我看看这个怎么弄",
                timestamp=self.now + timedelta(seconds=10),
            ),
            recent_messages=[],
        )

        self.assertTrue(result.should_handle)
        self.assertNotEqual(result.reason, "media_only_no_text")

    def test_should_reply_when_image_comes_with_a_question(self) -> None:
        """图 + 文字不是裸媒体。这条防止我把「发图后提问」一起改哑。"""

        engine = _make_engine()
        engine.activate_session("group:1", "1001", False, self.now)

        result = engine.evaluate(
            _payload(f"{_BARE_IMAGE} 这是什么", timestamp=self.now + timedelta(seconds=5)),
            recent_messages=[],
        )

        self.assertTrue(result.should_handle)
        self.assertNotEqual(result.reason, "media_only_no_text")

    def test_should_respect_explicit_media_facts_from_engine(self) -> None:
        """engine 显式填 media_types / has_user_text 时以它为准，不依赖占位符解析。"""

        engine = _make_engine()

        declared = engine.evaluate(
            _payload(
                "看图",
                timestamp=self.now,
                media_types=["image"],
                has_user_text=False,
            ),
            recent_messages=[],
        )
        self.assertFalse(declared.should_handle)
        self.assertEqual(declared.reason, "media_only_no_text")

        has_text = engine.evaluate(
            _payload(
                _BARE_IMAGE,
                timestamp=self.now,
                media_types=["image"],
                has_user_text=True,
            ),
            recent_messages=[],
        )
        self.assertNotEqual(has_text.reason, "media_only_no_text")

    def test_should_open_the_gate_without_inventing_user_text(self) -> None:
        """`has_user_text` 是"有没有"的事实，不是内容。

        engine 说有文字、解析又切不出来时（占位符格式变了，或 summary 是
        没有右边界的自由文本），只能开门，不能把机器拼的占位符当成用户说的话 ——
        否则表情包配文里的别名又会被当成有人在叫机器人。
        """

        engine = _make_engine()
        facts = engine._resolve_message_facts(
            _payload(
                f"{_MEDIA_PREFIX}image:下雪了好冷 雪",
                timestamp=self.now,
                media_types=["image"],
                has_user_text=True,
            )
        )

        self.assertFalse(facts.is_media_only)
        self.assertEqual(facts.user_text, "")

    def test_should_read_media_facts_out_of_the_real_placeholder_format(self) -> None:
        """把 app_helpers `_build_multimodal_text()` 的真实输出形态钉住。

        实测日志里出现过的 summary 取值都在这里：空、`[动画表情]`、`[小宠物]`、
        自由文本配文，以及本地路径形式的视频 URL。
        """

        engine = _make_engine()
        cases = {
            "image:[image]": (["image"], ""),
            "image:[动画表情]": (["image"], ""),
            "image:哎呦，你干嘛～": (["image"], ""),
            "image:[image] 这是什么": (["image"], "这是什么"),
            "video:/Users/dwgx/Library/Containers/x/9eaa1f.mp4": (["video"], ""),
            "record:/tmp/a.amr [语音内容] 帮我看看": (["record"], "[语音内容] 帮我看看"),
            "forward": (["forward"], ""),
            "forward 这是什么": (["forward"], "这是什么"),
            "image:[image] | video:https://cdn.example.com/a.mp4": (
                ["image", "video"],
                "",
            ),
        }

        for tokens, (media_types, user_text) in cases.items():
            with self.subTest(tokens=tokens):
                self.assertEqual(
                    engine._split_multimodal_marker(f"{_MEDIA_PREFIX}{tokens}"),
                    (media_types, user_text),
                )

    def test_should_leave_plain_text_untouched(self) -> None:
        """没有占位符前缀的普通消息原样通过，媒体列表为空。"""

        engine = _make_engine()

        self.assertEqual(
            engine._split_multimodal_marker("帮我看看 image:这个"),
            ([], "帮我看看 image:这个"),
        )


class KeywordPoolPollutionTests(unittest.TestCase):
    """机器占位符与昵称前缀不该变成群里的「热词」。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_should_not_probe_from_tokens_of_our_own_multimodal_placeholder(
        self,
    ) -> None:
        """近 48 行里出现两次的 token 会自动升级成热词，于是一堆裸图把
        image/multimodal/sent/user 变成了「大家在聊的话题」，下一张裸图就命中。
        实测这条占媒体轮的 31/97，是「发图它就说话」最主要的单点原因。
        """

        engine = _make_engine(ai_listen_keyword_pass_enable=True)
        polluted_rows = [
            f"小明(QQ:1001): {_BARE_IMAGE}",
            f"小红(QQ:1002): {_BARE_IMAGE}",
            f"小刚(QQ:1003): {_BARE_IMAGE}",
        ]

        result = engine.evaluate(
            _payload(f"{_BARE_IMAGE} 你看", user_id="1004", timestamp=self.now),
            recent_messages=polluted_rows,
        )

        self.assertNotEqual(result.reason, "ai_listen_probe_memory_keyword")

    def test_should_not_turn_nicknames_and_qq_prefix_into_keywords(self) -> None:
        """`昵称(QQ:12345): ` 是 core/engine.py 拼的机器前缀。
        说过两次话的人的昵称、以及 token `qq`，都不该变成触发词。
        """

        engine = _make_engine(ai_listen_keyword_pass_enable=True)
        rows = ["小明(QQ:1001): 吃了吗", "小明(QQ:1001): 我先去睡了"]

        for text in ("小明 在吗", "有人在qq上吗"):
            with self.subTest(text=text):
                hits = engine._match_memory_keywords(
                    clean_text=text.lower(), recent_messages=rows, memory_keywords=[]
                )
                self.assertEqual(hits, 0)

    def test_should_not_read_bot_alias_out_of_a_sticker_summary(self) -> None:
        """QQ 表情包的 summary 会被原样塞进占位符。别人发一张配文里带
        `雪` / `yuki` 的表情包，不等于有人在叫机器人。
        """

        engine = _make_engine()

        for text in (
            f"{_MEDIA_PREFIX}image:下雪了好冷 雪",
            f"{_MEDIA_PREFIX}image:yuki chan",
        ):
            with self.subTest(text=text):
                result = engine.evaluate(
                    _payload(text, timestamp=self.now), recent_messages=[]
                )
                self.assertNotEqual(result.reason, "name_call")

    def test_should_still_answer_a_real_alias_call(self) -> None:
        """回归保护：用户自己打的字里叫名字，仍然是 name_call。"""

        engine = _make_engine()

        for text in ("yuki 在吗", f"{_BARE_IMAGE} yuki 看看这个"):
            with self.subTest(text=text):
                result = engine.evaluate(
                    _payload(text, timestamp=self.now), recent_messages=[]
                )
                self.assertTrue(result.should_handle)
                self.assertEqual(result.reason, "name_call")


class ProbeBudgetTests(unittest.TestCase):
    """旁听探测的冷却与配额只在真的探测时记账。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def _heat_up(self, engine: TriggerEngine, base: datetime) -> int:
        """把群热度顶到 min_messages / min_unique_users 之上。

        返回这几条本身触发的探测次数 —— 热度一够它自己就会探测一次，
        那一次同样占配额，不能不算。
        """

        probes = 0
        for index, uid in enumerate(("2001", "2002", "2003")):
            result = engine.evaluate(
                _payload(
                    "这个到底怎么弄",
                    user_id=uid,
                    timestamp=base + timedelta(milliseconds=100 * index),
                ),
                recent_messages=[],
            )
            if result.listen_probe and result.should_handle:
                probes += 1
        return probes

    def test_should_not_let_an_active_session_turn_eat_the_probe_cooldown(self) -> None:
        """active_session 回合不该白吃掉冷却窗口。

        修前 `_decide_ai_probe_reason` 在 evaluate 开头无条件执行，
        一命中就写 `_last_ai_probe_at`，然后 active_session 分支直接返回并写
        listen_probe=False —— 冷却被吃掉了，真正的插话机会反而被饿死。
        """

        engine = _make_engine(ai_listen_interval_seconds=600)
        engine.activate_session("group:1", "1001", False, self.now)

        # 直接铺热度，不走 evaluate —— 否则铺热度本身就探测一次并吃掉冷却，
        # 那样考的是「冷却生效」而不是「active_session 分支有没有白吃冷却」。
        for index, uid in enumerate(("2001", "2002", "2003")):
            engine._record_group_activity(
                "group:1", uid, self.now + timedelta(milliseconds=100 * index)
            )

        # 活跃用户先发一条：走 active_session，不该动探测账本。
        active = engine.evaluate(
            _payload("我再看看", timestamp=self.now + timedelta(seconds=1)),
            recent_messages=[],
        )
        self.assertEqual(active.reason, "active_session")
        self.assertNotIn("group:1", engine._last_ai_probe_at)

        # 紧接着另一个人聊同一件事：探测窗口还在，应该能插嘴。
        bystander = engine.evaluate(
            _payload(
                "对啊我也想知道怎么弄",
                user_id="2004",
                timestamp=self.now + timedelta(seconds=2),
            ),
            recent_messages=[],
        )
        self.assertTrue(bystander.should_handle)
        self.assertTrue(bystander.listen_probe)

    def test_should_not_spend_probe_budget_on_bare_media(self) -> None:
        """裸媒体在探测之前就返回，既不吃冷却也不吃配额。

        群里刷图时如果每张图都走一次探测决策，配额会被图片耗光，
        真正有人在讨论的时候反而没额度说话了。
        """

        engine = _make_engine(ai_listen_interval_seconds=600)
        for index, uid in enumerate(("2001", "2002", "2003")):
            engine._record_group_activity(
                "group:1", uid, self.now + timedelta(milliseconds=index)
            )

        media = engine.evaluate(
            _payload(_BARE_IMAGE, user_id="2004", timestamp=self.now + timedelta(seconds=1)),
            recent_messages=[],
        )
        self.assertEqual(media.reason, "media_only_no_text")
        self.assertNotIn("group:1", engine._last_ai_probe_at)
        self.assertEqual(len(engine._ai_probe_history["group:1"]), 0)

        # 配额没被图片吃掉，所以紧接着的讨论仍然能插嘴。
        discussion = engine.evaluate(
            _payload(
                "这个到底怎么弄", user_id="2005", timestamp=self.now + timedelta(seconds=2)
            ),
            recent_messages=[],
        )
        self.assertTrue(discussion.should_handle)
        self.assertTrue(discussion.listen_probe)

    def test_should_stop_probing_once_the_hourly_budget_is_spent(self) -> None:
        """每会话每小时的探测配额是硬上限。

        冷却 45s 时理论上限是 80 次/小时，而 provider 单次要 6.7~10.7 秒且高频 503。
        只靠冷却撑不住，必须有绝对配额。这里把间隔拉到冷却下限（15s）之外，
        单独考配额而不是考冷却。
        """

        engine = _make_engine(
            ai_listen_interval_seconds=15,
            ai_listen_max_probes_per_hour=6,
            busy_window_seconds=3600,
        )
        probes = self._heat_up(engine, self.now)

        for index in range(12):
            result = engine.evaluate(
                _payload(
                    "这个到底怎么弄啊",
                    user_id=f"30{index:02d}",
                    timestamp=self.now + timedelta(seconds=30 + index * 20),
                ),
                recent_messages=[],
            )
            if result.listen_probe and result.should_handle:
                probes += 1

        self.assertEqual(probes, 6)

    def test_should_refill_the_budget_after_the_hour_slides(self) -> None:
        """配额是滑动一小时窗口，不是永久封顶。"""

        engine = _make_engine(
            ai_listen_interval_seconds=15,
            ai_listen_max_probes_per_hour=2,
            busy_window_seconds=3600,
        )
        self._heat_up(engine, self.now)

        for index in range(4):
            engine.evaluate(
                _payload(
                    "这个到底怎么弄啊",
                    user_id=f"40{index:02d}",
                    timestamp=self.now + timedelta(seconds=30 + index * 20),
                ),
                recent_messages=[],
            )

        # 一小时窗口滑过去之后旧记录全部出窗，配额应该重新可用。
        later = self.now + timedelta(hours=1, minutes=5)
        refilled = self._heat_up(engine, later)

        self.assertGreater(refilled, 0)
        self.assertLessEqual(len(engine._ai_probe_history["group:1"]), 2)


class ActiveSessionDowngradeTests(unittest.TestCase):
    """active_session 从「无条件放行」降级成「短窗放行 + 之后只作证据」。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_should_keep_a_live_back_and_forth_smooth(self) -> None:
        """回归保护：free window 内的连续对话仍然顺畅放行。"""

        engine = _make_engine(active_session_free_window_seconds=90)
        engine.activate_session("group:1", "1001", False, self.now)

        result = engine.evaluate(
            _payload("那再帮我改一下", timestamp=self.now + timedelta(seconds=30)),
            recent_messages=[],
        )

        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "active_session")

    def test_should_not_reply_to_every_message_for_eight_minutes(self) -> None:
        """核心止血点：超出 free window 后，闲聊不再因为「8 分钟前说过话」而每条都回。"""

        engine = _make_engine(
            active_session_free_window_seconds=90, delegate_undirected_to_ai=False
        )
        engine.activate_session("group:1", "1001", False, self.now)

        result = engine.evaluate(
            _payload("哈哈哈", timestamp=self.now + timedelta(minutes=5)),
            recent_messages=[],
        )

        self.assertFalse(result.should_handle)
        self.assertTrue(result.active_session)

    def test_should_count_an_expired_session_as_listening_evidence(self) -> None:
        """过期 active_session 不放行，但仍是「这人不久前跟我说过话」的加分证据。"""

        engine = _make_engine(active_session_score_bonus=0.6)

        base = engine._build_listen_score("这个怎么弄", 2, 2)
        with_bonus = engine._build_listen_score(
            "这个怎么弄", 2, 2, active_session_expired=True
        )

        self.assertAlmostEqual(with_bonus - base, 0.6, places=6)


class ListenProbeStillWorksTests(unittest.TestCase):
    """删掉关键词放行之后，热度这条路必须还在 —— 业主要的插嘴能力不能一起没了。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_should_still_chime_in_when_several_people_discuss_something(self) -> None:
        """一堆人在讨论 → 仍然可以插嘴，靠热度而不是关键词。"""

        engine = _make_engine(
            ai_listen_min_messages=2,
            ai_listen_min_unique_users=2,
            ai_listen_interval_seconds=0,
        )

        reasons = []
        for index, uid in enumerate(("5001", "5002", "5003")):
            result = engine.evaluate(
                _payload(
                    "这个到底怎么弄",
                    user_id=uid,
                    timestamp=self.now + timedelta(milliseconds=500 * index),
                ),
                recent_messages=[],
            )
            reasons.append((result.should_handle, result.reason))

        self.assertTrue(
            any(handled and reason.startswith("ai_listen_probe") for handled, reason in reasons),
            f"多人讨论应能插嘴，实际={reasons}",
        )

    def test_should_stay_quiet_for_one_person_muttering_to_themselves(self) -> None:
        """一个人自言自语不构成讨论，不该插嘴。"""

        engine = _make_engine(delegate_undirected_to_ai=False)

        result = engine.evaluate(
            _payload("唉", timestamp=self.now), recent_messages=[]
        )

        self.assertFalse(result.should_handle)
        self.assertEqual(result.reason, "not_directed")


class FollowupMediaPolicyTests(unittest.TestCase):
    """followup 窗口内的裸媒体默认也不放行（保守取向）。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_should_drop_bare_media_inside_followup_window_by_default(self) -> None:
        engine = _make_engine(media_only_allow_in_followup=False)
        engine.mark_reply_target("group:1", "1001", self.now)

        result = engine.evaluate(
            _payload(_BARE_IMAGE, timestamp=self.now + timedelta(seconds=3)),
            recent_messages=[],
        )

        self.assertFalse(result.should_handle)
        self.assertEqual(result.reason, "media_only_no_text")

    def test_should_allow_bare_media_inside_followup_window_when_opted_in(self) -> None:
        """开关打开就恢复「刚回复完、紧接着发图」也应答。"""

        engine = _make_engine(media_only_allow_in_followup=True)
        engine.mark_reply_target("group:1", "1001", self.now)

        result = engine.evaluate(
            _payload(_BARE_IMAGE, timestamp=self.now + timedelta(seconds=3)),
            recent_messages=[],
        )

        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "followup_window")

    def test_should_keep_followup_working_for_plain_text(self) -> None:
        """回归保护：followup 窗口对普通文字不受影响。"""

        engine = _make_engine()
        engine.mark_reply_target("group:1", "1001", self.now)

        result = engine.evaluate(
            _payload("继续", timestamp=self.now + timedelta(seconds=3)),
            recent_messages=[],
        )

        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "followup_window")


class StructuralSignalScopeTests(unittest.TestCase):
    """结构定位符只看用户自己给的内容。"""

    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def test_should_not_treat_our_own_media_url_as_a_user_supplied_link(self) -> None:
        """占位符里的视频 URL 是我们自己从 raw_segments 拼进去的，
        不是用户贴的链接，不该拿它当「用户要我处理这个链接」的结构证据。
        """

        engine = _make_engine(delegate_undirected_min_signal=1.0)

        signal = engine._structural_request_signal(
            engine._resolve_message_facts(
                _payload(
                    f"{_MEDIA_PREFIX}video:https://cdn.example.com/a/b.mp4",
                    timestamp=self.now,
                )
            ).user_text
        )

        self.assertEqual(signal, 0.0)

    def test_should_still_see_a_link_the_user_actually_typed(self) -> None:
        """回归保护：用户自己贴的链接仍然算结构证据。"""

        engine = _make_engine()

        signal = engine._structural_request_signal(
            "帮我看看 https://www.bilibili.com/video/BV1xx411c7mD"
        )

        self.assertGreater(signal, 0.0)


if __name__ == "__main__":
    unittest.main()
