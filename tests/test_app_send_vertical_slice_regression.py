from __future__ import annotations

import unittest
from types import SimpleNamespace

import app as app_module
from app import (
    _classify_stale_send,
    _normalize_send_media_fields,
    _plan_reply_text_chunks,
)


def _result(**attrs) -> SimpleNamespace:
    return SimpleNamespace(**attrs)


def _payload(**attrs) -> SimpleNamespace:
    base = dict(seq=0, trace_id="trace-1", user_id="u1", is_private=False)
    base.update(attrs)
    return SimpleNamespace(**base)


class NormalizeSendMediaFieldsTests(unittest.TestCase):
    """发送结果媒体字段的统一/去重逻辑（D5 切片 1）。"""

    def test_empty_result_returns_defaults(self):
        self.assertEqual(
            _normalize_send_media_fields(_result()),
            ("", False, "", "", [], "", "", "", ""),
        )

    def test_image_url_deduped_and_prepended(self):
        fields = _normalize_send_media_fields(
            _result(image_url="z.png", image_urls=["a.png", "  ", "b.png", "z.png"])
        )
        self.assertEqual(fields[3], "z.png")
        self.assertEqual(fields[4], ["a.png", "b.png", "z.png"])

    def test_new_image_url_prepended_when_missing_from_list(self):
        fields = _normalize_send_media_fields(
            _result(image_url="new.png", image_urls=["a.png", "b.png"])
        )
        self.assertEqual(fields[3], "new.png")
        self.assertEqual(fields[4], ["new.png", "a.png", "b.png"])

    def test_image_url_promoted_from_list_when_absent(self):
        fields = _normalize_send_media_fields(_result(image_urls=["a.png", "b.png"]))
        self.assertEqual(fields[3], "a.png")
        self.assertEqual(fields[4], ["a.png", "b.png"])

    def test_non_list_image_urls_ignored(self):
        fields = _normalize_send_media_fields(_result(image_url="x.png", image_urls="not-a-list"))
        self.assertEqual(fields[4], ["x.png"])

    def test_music_actions_flag(self):
        for action in ("music_play", "music_play_by_id", "bilibili_audio_extract"):
            self.assertTrue(_normalize_send_media_fields(_result(action=action))[1])
        self.assertFalse(_normalize_send_media_fields(_result(action="reply"))[1])

    def test_reply_text_normalized_and_media_cast_to_str(self):
        fields = _normalize_send_media_fields(
            _result(reply_text="  hi  ", video_url=123, cover_url=None, record_b64="", audio_file="")
        )
        self.assertEqual(fields[2], "hi")
        self.assertEqual(fields[5], "123")
        self.assertEqual(fields[6], "")


class ClassifyStaleSendTests(unittest.TestCase):
    """被打断回合的判定（D5 切片 2）。"""

    def test_no_latest_trace_is_not_stale(self):
        latest_seq, stale, same_user, cancel, plain = _classify_stale_send(
            {"seq": 3, "user_id": "u1", "text": "hmm"},
            "",
            _payload(),
            "reply",
            "hi",
            [],
            "",
            "",
            "",
            "",
        )
        self.assertEqual(latest_seq, 3)
        self.assertFalse(stale)
        self.assertFalse(same_user)
        self.assertFalse(cancel)
        self.assertFalse(plain)

    def test_newer_seq_from_other_trace_is_stale(self):
        latest_seq, stale, same_user, cancel, plain = _classify_stale_send(
            {"seq": 5, "user_id": "u1", "text": "hmm"},
            "trace-2",
            _payload(seq=2),
            "reply",
            "hi",
            [],
            "",
            "",
            "",
            "",
        )
        self.assertTrue(stale)
        self.assertTrue(same_user)
        self.assertFalse(cancel)
        self.assertTrue(plain)

    def test_same_user_newer_turn(self):
        _, stale, same_user, _, _ = _classify_stale_send(
            {"seq": 9, "user_id": "u1", "text": "hmm"},
            "trace-2",
            _payload(seq=1),
            "reply",
            "hi",
            [],
            "",
            "",
            "",
            "",
        )
        self.assertTrue(stale)
        self.assertTrue(same_user)

    def test_cancel_text_marks_cancel_newer_turn(self):
        _, stale, _, cancel, _ = _classify_stale_send(
            {"seq": 9, "user_id": "u1", "text": "打断"},
            "trace-2",
            _payload(seq=1),
            "reply",
            "hi",
            [],
            "",
            "",
            "",
            "",
        )
        self.assertTrue(stale)
        self.assertTrue(cancel)

    def test_media_presence_defeats_stale_plain_reply(self):
        _, _, _, _, plain = _classify_stale_send(
            {"seq": 9, "user_id": "u1", "text": "hmm"},
            "trace-2",
            _payload(seq=1),
            "reply",
            "hi",
            [],
            "http://x/v.mp4",
            "",
            "",
            "",
        )
        self.assertFalse(plain)

    def test_private_message_defeats_stale_plain_reply(self):
        _, _, _, _, plain = _classify_stale_send(
            {"seq": 9, "user_id": "u1", "text": "hmm"},
            "trace-2",
            _payload(seq=1, is_private=True),
            "reply",
            "hi",
            [],
            "",
            "",
            "",
            "",
        )
        self.assertFalse(plain)

    def test_non_reply_action_defeats_stale_plain_reply(self):
        _, _, _, _, plain = _classify_stale_send(
            {"seq": 9, "user_id": "u1", "text": "hmm"},
            "trace-2",
            _payload(seq=1),
            "search",
            "hi",
            [],
            "",
            "",
            "",
            "",
        )
        self.assertFalse(plain)


def _chunk_kwargs(**overrides) -> dict:
    base = dict(
        reply_text="你好呀。今天天气不错，我们出去走走好吗。",
        video_url="",
        action="reply",
        multi_reply_enable=True,
        multi_reply_max_lines=6,
        multi_reply_max_chars=520,
        multi_reply_max_chunks=6,
        multi_reply_chat_max_lines=4,
        multi_reply_chat_max_chars=240,
        multi_reply_chat_max_chunks=4,
        video_analysis_requested=False,
        chat_split_mode="semantic",
        send_rate_enable=False,
        send_rate_max_per_window=6,
        send_rate_window_seconds=60,
        send_rate_warn_threshold=4,
        conversation_id="conv-slice-test",
        group_id=0,
    )
    base.update(overrides)
    return base


class PlanReplyTextChunksTests(unittest.TestCase):
    """回复文本的拆分/限流合并/超长重平衡（D5 切片 3）。"""

    def tearDown(self) -> None:
        app_module._SEND_RATE_BUCKETS.clear()

    def test_video_url_forces_single_chunk(self):
        text = "这条回复很长，" * 10
        chunks, rate_limited, chunk_count = _plan_reply_text_chunks(
            **_chunk_kwargs(reply_text=text, video_url="http://x/v.mp4")
        )
        self.assertEqual(chunks, [text])
        self.assertEqual(chunk_count, 1)
        self.assertFalse(rate_limited)

    def test_empty_reply_produces_no_chunks(self):
        chunks, _, chunk_count = _plan_reply_text_chunks(**_chunk_kwargs(reply_text=""))
        self.assertEqual(chunks, [])
        self.assertEqual(chunk_count, 0)

    def test_multi_reply_disabled_falls_back_to_single(self):
        text = "一句话。第二句话。"
        chunks, _, _ = _plan_reply_text_chunks(
            **_chunk_kwargs(reply_text=text, multi_reply_enable=False)
        )
        self.assertEqual(chunks, [text])

    def test_semantic_split_yields_multiple_chunks(self):
        long_text = (
            "今天我们来聊聊项目的整体架构。首先说明一下当前各模块的职责划分。"
            "然后是数据流的方向，以及异常处理的分层策略。最后总结一下后续的演进方向。"
            "如果有什么疑问，欢迎随时提出来。我会尽量把每个点讲清楚。"
            "这部分内容比较长，应该会拆成多个片段来发送。"
        )
        chunks, _, _ = _plan_reply_text_chunks(
            **_chunk_kwargs(
                reply_text=long_text,
                multi_reply_chat_max_lines=2,
                multi_reply_chat_max_chars=60,
            )
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c for c in chunks))

    def test_rate_limited_when_bucket_near_warn(self):
        bucket = app_module._TokenBucket(capacity=6, refill_seconds=60, warn_threshold=4)
        bucket.tokens = 0.0
        app_module._SEND_RATE_BUCKETS["conv:conv-slice-rate"] = bucket
        chunks, rate_limited, chunk_count = _plan_reply_text_chunks(
            **_chunk_kwargs(
                reply_text="a。b。c。d。e。f。",
                send_rate_enable=True,
                send_rate_max_per_window=6,
                send_rate_window_seconds=60,
                send_rate_warn_threshold=4,
                conversation_id="conv-slice-rate",
            )
        )
        self.assertTrue(rate_limited)
        self.assertGreaterEqual(chunk_count, 1)
        self.assertTrue(all(c for c in chunks))


if __name__ == "__main__":
    unittest.main()
