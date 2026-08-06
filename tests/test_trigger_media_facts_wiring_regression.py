"""engine 必须把精确的结构事实交给 trigger，而不是让它反解压平后的文本。

背景：裸媒体门（`media_only_no_text`）解决的是业主抱怨的「一个人发一张图片它就说话」。
但门判「用户有没有打字」如果靠反解文本，会两头都错：

1. 图片 summary 是**没有右边界的自由文本**（日志里真实出现过 `image:哎呦，你干嘛～`），
   而 core/engine.py:1022 的 normalize_text 把 app.py:1487 拼进去的换行压掉了 ——
   于是「表情包 + 用户喊 yuki」被判成裸媒体而沉默，直接违反业主第一条诉求。
2. 换用 `_extract_multimodal_user_text` 也不行：它的 `image:\\s*\\S+` 里那个 `\\s*`
   会把冒号后的**下一个词**吃掉。`image:[image] yukiko 看看` 先被方括号正则压成
   `image: yukiko 看看`，再被这条吃成 `看看` —— 别名没了，喊 yukiko 也不回。
   实测过两种，所以最终改成按 message.text 的换行精确切。
3. 反过来，残渣也会害事：`image:[image]` 留下裸 `image:` 时 has_user_text 变 True，
   裸媒体门失效，实测 active_session 窗口内发裸图仍然会说话。

本文件锁的是 engine ↔ trigger 这条接线的契约，用 app.py:1481-1487 的真实拼法构造输入。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timezone

from app_helpers import _build_multimodal_text
from core.engine import YukikoEngine
from core.trigger import TriggerEngine, TriggerInput
from utils.text import normalize_text

_IMAGE_SEG = [{"type": "image", "data": {"url": "https://gchat.qpic.cn/download?x=1"}}]
# summary 取日志里真实出现过的自由文本，不是造的
_STICKER_SEG = [{"type": "image", "data": {"summary": "哎呦，你干嘛～"}}]
_RECORD_SEG = [{"type": "record", "data": {"url": "/tmp/a.amr"}}]


def _compose(segments: list[dict], typed: str, voice: str = "") -> str:
    """复刻 app.py:1481-1487 的拼法，包括那个关键的换行。"""

    media_event = _build_multimodal_text(segments, mentioned=False) if segments else ""
    if voice:
        media_event = f"{media_event}\n[语音内容] {voice}"
    if media_event and typed:
        return f"{media_event}\n{typed}"
    return media_event or typed


class MediaFactsWiringTests(unittest.TestCase):
    def _evaluate(
        self,
        segments: list[dict],
        typed: str,
        *,
        voice: str = "",
        active_session: bool = False,
    ):
        raw = _compose(segments, typed, voice)
        media_types = [seg["type"] for seg in segments]
        user_text = YukikoEngine._user_typed_text_for_trigger(raw) if media_types else normalize_text(raw)
        trigger = TriggerEngine(
            {
                "ai_listen_enable": True,
                "ai_listen_keyword_enable": True,
                "ai_listen_min_keyword_hits": 1,
            },
            {"name": "YuKiKo", "nicknames": ["yuki", "yukiko"]},
        )
        if active_session:
            trigger.activate_session("group:1", "1001", False)
        result = trigger.evaluate(
            TriggerInput(
                conversation_id="group:1",
                user_id="1001",
                text=normalize_text(raw),
                mentioned=False,
                is_private=False,
                timestamp=datetime.now(UTC),
                bot_id="bot",
                media_types=media_types,
                user_text=user_text,
                has_user_text=bool(user_text),
                trace_id="wiring-test",
            ),
            recent_messages=[],
            memory_keywords=[],
        )
        return result, user_text

    def test_bare_image_stays_silent_through_the_media_gate(self) -> None:
        result, user_text = self._evaluate(_IMAGE_SEG, "")
        self.assertEqual(user_text, "")
        self.assertFalse(result.should_handle)
        # 必须是裸媒体门拦的，不能是碰巧落到 not_directed ——
        # 后者在 active_session 窗口里会漏。
        self.assertEqual(result.reason, "media_only_no_text")

    def test_bare_image_stays_silent_even_inside_active_session(self) -> None:
        """业主原话「一个人发一张图片，他就说话」。实测漏点就在这里。"""

        result, _ = self._evaluate(_IMAGE_SEG, "", active_session=True)
        self.assertFalse(result.should_handle)
        self.assertEqual(result.reason, "media_only_no_text")

    def test_sticker_with_free_text_summary_plus_alias_still_replies(self) -> None:
        """业主第一条诉求：喊 yuki 就要回。summary 是自由文本时曾被沉默。"""

        result, user_text = self._evaluate(_STICKER_SEG, "yuki 看这个")
        self.assertEqual(user_text, "yuki 看这个")
        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "name_call")

    def test_bare_image_plus_alias_keeps_the_alias(self) -> None:
        """`image:[image] yukiko 看看` 曾被正则吃成 `看看`，别名丢失。"""

        result, user_text = self._evaluate(_IMAGE_SEG, "yukiko 看看")
        self.assertIn("yukiko", user_text)
        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "name_call")

    def test_alias_inside_summary_alone_is_not_a_name_call(self) -> None:
        """别人发的表情包 summary 里带 yuki，用户自己没打字 —— 不该被当成在叫它。"""

        result, user_text = self._evaluate([{"type": "image", "data": {"summary": "yuki chan"}}], "")
        self.assertEqual(user_text, "")
        self.assertFalse(result.should_handle)
        self.assertEqual(result.reason, "media_only_no_text")

    def test_voice_transcript_counts_as_user_text(self) -> None:
        """语音转写就是用户说的话，不能被裸媒体门吃掉。"""

        result, user_text = self._evaluate(_RECORD_SEG, "", voice="yuki 你在吗")
        self.assertIn("yuki", user_text)
        self.assertTrue(result.should_handle)
        self.assertEqual(result.reason, "name_call")

    def test_voice_transcript_without_alias_is_not_media_only(self) -> None:
        result, user_text = self._evaluate(_RECORD_SEG, "", voice="今天天气怎么样")
        self.assertIn("今天天气", user_text)
        self.assertNotEqual(result.reason, "media_only_no_text")

    def test_plain_text_message_is_untouched(self) -> None:
        result, user_text = self._evaluate([], "有人知道怎么装 python 吗")
        self.assertEqual(user_text, "有人知道怎么装 python 吗")
        self.assertNotEqual(result.reason, "media_only_no_text")


class UserTypedTextExtractionTests(unittest.TestCase):
    """直接钉切分函数本身，与 trigger 判定解耦。"""

    def test_drops_only_the_first_placeholder_line(self) -> None:
        raw = "MULTIMODAL_EVENT user sent multimodal message: image:[image]\n这是什么"
        self.assertEqual(YukikoEngine._user_typed_text_for_trigger(raw), "这是什么")

    def test_keeps_voice_transcript_line(self) -> None:
        raw = "MULTIMODAL_EVENT user sent multimodal message: record:/tmp/a.amr\n[语音内容] 你好啊\n"
        self.assertIn("你好啊", YukikoEngine._user_typed_text_for_trigger(raw))

    def test_placeholder_only_yields_empty(self) -> None:
        raw = "MULTIMODAL_EVENT user sent multimodal message: image:[image]"
        self.assertEqual(YukikoEngine._user_typed_text_for_trigger(raw), "")

    def test_free_text_summary_is_not_mistaken_for_user_text(self) -> None:
        raw = "MULTIMODAL_EVENT user sent multimodal message: image:哎呦，你干嘛～"
        self.assertEqual(YukikoEngine._user_typed_text_for_trigger(raw), "")

    def test_at_variant_prefix_is_also_dropped(self) -> None:
        raw = "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: image:[image]\n看看这个"
        self.assertEqual(YukikoEngine._user_typed_text_for_trigger(raw), "看看这个")

    def test_non_placeholder_text_survives_whole(self) -> None:
        self.assertEqual(YukikoEngine._user_typed_text_for_trigger("就是普通一句话"), "就是普通一句话")

    def test_empty_input(self) -> None:
        for value in ("", None, "\n\n"):
            with self.subTest(repr(value)):
                self.assertEqual(YukikoEngine._user_typed_text_for_trigger(value), "")


if __name__ == "__main__":
    unittest.main()
