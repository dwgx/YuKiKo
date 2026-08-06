from __future__ import annotations

from datetime import datetime, timezone
import unittest

from core.config_templates import _built_in_config_defaults
from core.engine import YukikoEngine
from core.engine_types import EngineMessage
from core.sticker import _QQ_DATA_ROOTS
from core.trigger import TriggerEngine, TriggerInput


class ConfigAndTriggerRegressionTests(unittest.TestCase):
    def test_builtin_defaults_enable_high_confidence_ai_listen(self) -> None:
        defaults = _built_in_config_defaults()

        self.assertEqual(defaults["control"]["undirected_policy"], "high_confidence_only")
        self.assertTrue(defaults["bot"]["allow_non_to_me"])
        self.assertTrue(defaults["trigger"]["ai_listen_enable"])
        self.assertTrue(defaults["trigger"]["delegate_undirected_to_ai"])
        self.assertEqual(defaults["trigger"]["delegate_undirected_min_signal"], 1.0)
        self.assertTrue(defaults["bot"]["relationship_progressive_enable"])
        self.assertTrue(defaults["bot"]["kaomoji_enable"])

    def test_trigger_engine_does_not_delegate_undirected_by_default(self) -> None:
        trigger = TriggerEngine(trigger_config={}, bot_config={"name": "YuKiKo"})

        self.assertFalse(trigger.ai_listen_enable)
        self.assertFalse(trigger.delegate_undirected_to_ai)

    def test_delegate_undirected_requires_minimum_explicit_signal(self) -> None:
        trigger = TriggerEngine(
            trigger_config={
                "delegate_undirected_to_ai": True,
                "delegate_undirected_min_signal": 1.0,
            },
            bot_config={"name": "YuKiKo"},
        )
        ts = datetime.now(timezone.utc)

        low_signal = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="随便聊聊",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        low_result = trigger.evaluate(low_signal, recent_messages=[])
        self.assertFalse(low_result.should_handle)
        self.assertEqual(low_result.reason, "not_directed")

        high_signal = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="/help",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        high_result = trigger.evaluate(high_signal, recent_messages=[])
        self.assertFalse(high_result.should_handle)
        self.assertEqual(high_result.reason, "ai_router_candidate")

        # 单个裸结构定位符（文件名）+ 问句不再自己越过默认 1.0 门：
        # 问号与句长这两个语义加分位已删除，只剩结构定位符本身的 0.7。
        single_locator = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="看看这个 报告.docx 里写了什么？",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        single_result = trigger.evaluate(single_locator, recent_messages=[])
        self.assertFalse(single_result.should_handle)
        self.assertEqual(single_result.reason, "not_directed")

    def test_trigger_no_longer_wakes_on_semantic_task_cues(self) -> None:
        """trigger 只做注意力门：任何自由文本的语义线索都不得唤醒。"""
        trigger = TriggerEngine(
            trigger_config={
                "delegate_undirected_to_ai": True,
                "delegate_undirected_min_signal": 1.0,
            },
            bot_config={"name": "YuKiKo"},
        )
        ts = datetime.now(timezone.utc)

        for index, text in enumerate(
            [
                "帮我查一下明天天气",
                "点歌 热水澡",
                "帮我下载这个安装包",
                "以后叫我阿背",
                "这个视频讲了啥？",
                "你记得我叫什么吗",
            ]
        ):
            with self.subTest(text=text):
                result = trigger.evaluate(
                    TriggerInput(
                        conversation_id=f"group:semantic{index}",
                        user_id="1001",
                        text=text,
                        mentioned=False,
                        is_private=False,
                        timestamp=ts,
                    ),
                    recent_messages=[],
                )
                self.assertFalse(result.should_handle)
                self.assertEqual(result.reason, "not_directed")
                self.assertEqual(trigger._structural_request_signal(text), 0.0)

    def test_trigger_still_wakes_on_identity_and_attention_signals(self) -> None:
        """@ / 私聊 / 叫名字 / followup / 活跃会话 这些注意力信号必须继续生效。"""
        ts = datetime.now(timezone.utc)

        directed = TriggerEngine(trigger_config={}, bot_config={"name": "YuKiKo"})
        self.assertEqual(
            directed.evaluate(
                TriggerInput("group:1", "1001", "在吗", True, False, ts),
                recent_messages=[],
            ).reason,
            "directed",
        )
        self.assertEqual(
            directed.evaluate(
                TriggerInput("private:1", "1001", "在吗", False, True, ts),
                recent_messages=[],
            ).reason,
            "directed",
        )

        # 昵称/别名是身份判定，保留；但不能被"下雪"这类子串误触。
        alias = TriggerEngine(trigger_config={}, bot_config={"name": "YuKiKo"})
        self.assertEqual(
            alias.evaluate(
                TriggerInput("group:2", "1001", "雪 在吗", False, False, ts),
                recent_messages=[],
            ).reason,
            "name_call",
        )
        self.assertEqual(
            alias.evaluate(
                TriggerInput("group:3", "1001", "下雪了好冷", False, False, ts),
                recent_messages=[],
            ).reason,
            "not_directed",
        )

        followup = TriggerEngine(trigger_config={}, bot_config={"name": "YuKiKo"})
        followup.mark_reply_target("group:4", "1001", now=ts)
        self.assertEqual(
            followup.evaluate(
                TriggerInput("group:4", "1001", "继续", False, False, ts),
                recent_messages=[],
            ).reason,
            "followup_window",
        )

    def test_active_session_reaches_router_instead_of_not_directed_drop(self) -> None:
        trigger = TriggerEngine(
            trigger_config={
                "active_session_timeout_minutes": 8,
            },
            bot_config={"name": "YuKiKo"},
        )
        ts = datetime.now(timezone.utc)
        trigger.activate_session("group:901738883", "136666451", False, now=ts)

        result = trigger.evaluate(
            TriggerInput(
                conversation_id="group:901738883",
                user_id="136666451",
                text="你发送继续就行了",
                mentioned=False,
                is_private=False,
                timestamp=ts,
            ),
            recent_messages=[],
        )

        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "active_session")
        self.assertTrue(result.active_session)

    def test_memory_keywords_can_trigger_ai_listen_probe(self) -> None:
        """契约已反转：关键词命中**不再**单独放行旁听。

        原断言要求「一个人、一条消息、阈值设到 8 条/3 人/3.8 分都够不着」时，
        仅凭 memory_keyword 命中就开口。那正是业主说的「人机感」来源 ——
        靠词形命中说话，且绕过 ai_listen_min_score，等于让一个词否决整套阈值。
        现在 keyword_hits 只经 `_build_listen_score` 加分，由分数门与热度门裁决；
        `ai_listen_keyword_pass_enable=true` 可恢复旧行为。
        """

        trigger = TriggerEngine(
            trigger_config={
                "ai_listen_enable": True,
                "ai_listen_min_messages": 8,
                "ai_listen_min_unique_users": 3,
                "ai_listen_min_score": 3.8,
                "ai_listen_keyword_enable": True,
                "ai_listen_min_keyword_hits": 1,
            },
            bot_config={"name": "YuKiKo"},
        )
        ts = datetime.now(timezone.utc)

        payload = TriggerInput(
            conversation_id="group:1",
            user_id="1001",
            text="projectx 这个怎么弄",
            mentioned=False,
            is_private=False,
            timestamp=ts,
        )
        result = trigger.evaluate(
            payload,
            recent_messages=[
                "[Alice] 刚才 projectx 又报错了",
                "[Bob] projectx 的配置是不是丢了",
            ],
            memory_keywords=["projectx", "配置"],
        )
        self.assertFalse(result.should_handle)
        self.assertNotEqual(result.reason, "ai_listen_probe_memory_keyword")

        # 同一场景把开关打开就恢复旧行为，证明能力没被删掉、只是默认不用。
        legacy = TriggerEngine(
            trigger_config={
                "ai_listen_enable": True,
                "ai_listen_min_messages": 8,
                "ai_listen_min_unique_users": 3,
                "ai_listen_min_score": 3.8,
                "ai_listen_keyword_enable": True,
                "ai_listen_min_keyword_hits": 1,
                "ai_listen_keyword_pass_enable": True,
            },
            bot_config={"name": "YuKiKo"},
        )
        legacy_result = legacy.evaluate(
            payload,
            recent_messages=[
                "[Alice] 刚才 projectx 又报错了",
                "[Bob] projectx 的配置是不是丢了",
            ],
            memory_keywords=["projectx", "配置"],
        )
        self.assertTrue(legacy_result.should_handle)
        self.assertEqual(legacy_result.reason, "ai_listen_probe_memory_keyword")

    def test_mention_only_not_overridden_by_ai_listen(self) -> None:
        """mention_only policy must NOT be auto-upgraded even when ai_listen_enable is True."""
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {
            "control": {"undirected_policy": "mention_only"},
            "bot": {"allow_non_to_me": False},
            "trigger": {
                "ai_listen_enable": True,
                "delegate_undirected_to_ai": True,
            },
        }

        trigger_cfg = YukikoEngine._build_effective_trigger_config(engine)
        self.assertFalse(trigger_cfg.get("ai_listen_enable", False))
        self.assertFalse(trigger_cfg.get("allow_non_to_me", False))

    def test_high_confidence_policy_forces_ai_listen_gate(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {
            "control": {"undirected_policy": "high_confidence_only"},
            "bot": {"allow_non_to_me": False},
            "trigger": {
                "ai_listen_enable": False,
            },
        }

        trigger_cfg = YukikoEngine._build_effective_trigger_config(engine)
        self.assertTrue(trigger_cfg.get("ai_listen_enable", False))
        self.assertTrue(trigger_cfg.get("delegate_undirected_to_ai", False))

    def test_linux_qq_data_root_is_supported(self) -> None:
        normalized = {str(path).replace("\\", "/") for path in _QQ_DATA_ROOTS}
        self.assertTrue(
            any(item.endswith("/.config/QQ") for item in normalized),
            normalized,
        )

    def test_structural_video_link_can_wake_without_mention(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)

        message = EngineMessage(
            conversation_id="group:901738883",
            user_id="136666451",
            text="7.17 复制打开抖音，看看【刚满十八的老登的作品】 https://v.douyin.com/iI54zStBq0w/",
            mentioned=False,
            is_private=False,
            timestamp=datetime.now(timezone.utc),
        )

        self.assertTrue(engine._looks_like_structural_video_entrypoint(message, message.text))

    def test_structural_video_wake_does_not_match_plain_web_link(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)

        message = EngineMessage(
            conversation_id="group:901738883",
            user_id="136666451",
            text="看看 https://skiapi.dev",
            mentioned=False,
            is_private=False,
            timestamp=datetime.now(timezone.utc),
        )

        self.assertFalse(engine._looks_like_structural_video_entrypoint(message, message.text))


if __name__ == "__main__":
    unittest.main()
