from __future__ import annotations

import unittest
from collections import OrderedDict
from datetime import UTC, datetime

from core.engine import YukikoEngine
from core.engine_types import EngineMessage


class _FakeAdmin:
    """白名单判断的最小 mock：只暴露 handle_message 切片读取的三个成员。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        non_whitelist_mode: str = "silent",
        whitelisted: set[int] | None = None,
    ) -> None:
        self.enabled = enabled
        self.non_whitelist_mode = non_whitelist_mode
        self._whitelisted = whitelisted or set()

    def is_group_whitelisted(self, group_id: int) -> bool:
        return group_id in self._whitelisted


def _engine(
    *,
    seen_max: int = 200,
    admin: _FakeAdmin | None = None,
) -> YukikoEngine:
    engine = YukikoEngine.__new__(YukikoEngine)
    engine._seen_message_ids: OrderedDict[str, float] = OrderedDict()
    engine._seen_message_ids_max = seen_max
    engine.admin = admin if admin is not None else _FakeAdmin()
    return engine


def _message(
    *,
    message_id: str = "",
    timestamp: datetime | None = None,
    is_private: bool = False,
    group_id: int = 0,
    mentioned: bool = False,
    text: str = "",
    raw_segments: list[dict[str, object]] | None = None,
) -> EngineMessage:
    return EngineMessage(
        conversation_id="group:1",
        user_id="2",
        text=text,
        message_id=message_id,
        timestamp=timestamp or datetime.now(UTC),
        is_private=is_private,
        group_id=group_id,
        mentioned=mentioned,
        raw_segments=raw_segments or [],
    )


class HandleMessageDedupSliceTests(unittest.TestCase):
    """_deduplicate_message：等价于原内联去重块的返回值与副作用时序。"""

    def test_should_return_none_and_skip_state_when_no_message_id(self) -> None:
        engine = _engine()
        result = engine._deduplicate_message(_message())
        self.assertIsNone(result)
        self.assertEqual(len(engine._seen_message_ids), 0)

    def test_should_record_new_id_and_return_none(self) -> None:
        engine = _engine()
        msg = _message(message_id="m1", timestamp=datetime(2026, 8, 8, 0, 0, tzinfo=UTC))
        result = engine._deduplicate_message(msg)
        self.assertIsNone(result)
        self.assertEqual(engine._seen_message_ids["m1"], datetime(2026, 8, 8, 0, 0, tzinfo=UTC).timestamp())

    def test_should_return_duplicate_ignore_without_double_record(self) -> None:
        engine = _engine()
        engine._seen_message_ids["m1"] = 1.0
        result = engine._deduplicate_message(_message(message_id="m1"))
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "ignore")
        self.assertEqual(result.reason, "duplicate_message")
        self.assertEqual(len(engine._seen_message_ids), 1)

    def test_should_prune_oldest_ids_beyond_max(self) -> None:
        engine = _engine(seen_max=2)
        engine._deduplicate_message(_message(message_id="a"))
        engine._deduplicate_message(_message(message_id="b"))
        engine._deduplicate_message(_message(message_id="c"))
        self.assertEqual(list(engine._seen_message_ids.keys()), ["b", "c"])


class HandleMessageWhitelistSliceTests(unittest.TestCase):
    """_whitelist_ignore_reason：纯读，逐分支等价。"""

    def test_should_return_none_for_private_message(self) -> None:
        engine = _engine(admin=_FakeAdmin())
        self.assertIsNone(engine._whitelist_ignore_reason(_message(is_private=True)))

    def test_should_return_none_when_admin_disabled(self) -> None:
        engine = _engine(admin=_FakeAdmin(enabled=False))
        self.assertIsNone(engine._whitelist_ignore_reason(_message(group_id=1)))

    def test_should_return_none_when_group_whitelisted(self) -> None:
        engine = _engine(admin=_FakeAdmin(whitelisted={1}))
        self.assertIsNone(engine._whitelist_ignore_reason(_message(group_id=1)))

    def test_should_return_silent_reason_when_not_whitelisted(self) -> None:
        engine = _engine(admin=_FakeAdmin(non_whitelist_mode="silent"))
        self.assertEqual(
            engine._whitelist_ignore_reason(_message(group_id=1)),
            "group_not_whitelisted",
        )

    def test_should_return_not_mentioned_reason_in_whisper_mode(self) -> None:
        engine = _engine(admin=_FakeAdmin(non_whitelist_mode="mention"))
        self.assertEqual(
            engine._whitelist_ignore_reason(_message(group_id=1)),
            "group_not_whitelisted_not_mentioned",
        )

    def test_should_return_none_when_not_whitelisted_but_mentioned(self) -> None:
        engine = _engine(admin=_FakeAdmin(non_whitelist_mode="mention"))
        self.assertIsNone(
            engine._whitelist_ignore_reason(_message(group_id=1, mentioned=True))
        )


class HandleMessageTriggerMediaSliceTests(unittest.TestCase):
    """_trigger_media_context：纯计算，media_types 与 user_text 取值正确。"""

    def test_should_fall_back_to_plain_text_when_no_media_types(self) -> None:
        engine = _engine()
        msg = _message(text="hello")
        media_types, user_text = engine._trigger_media_context(msg, "hello")
        self.assertEqual(media_types, [])
        self.assertEqual(user_text, "hello")

    def test_should_extract_media_types_and_skip_non_dict_segments(self) -> None:
        engine = _engine()
        msg = _message(
            text="MULTIMODAL_EVENT\nhello",
            raw_segments=[{"type": "image"}, "junk", {"type": "voice"}],
        )
        media_types, user_text = engine._trigger_media_context(msg, "MULTIMODAL_EVENT hello")
        self.assertEqual(media_types, ["image", "voice"])
        self.assertEqual(user_text, "hello")

    def test_should_use_message_text_for_user_text_when_media_present(self) -> None:
        engine = _engine()
        msg = _message(
            text="MULTIMODAL_EVENT\n用户打了两行字",
            raw_segments=[{"type": "image"}],
        )
        media_types, user_text = engine._trigger_media_context(msg, "用户打了两行字")
        self.assertEqual(media_types, ["image"])
        self.assertEqual(user_text, "用户打了两行字")

    def test_should_normalize_media_type_lowercase(self) -> None:
        engine = _engine()
        msg = _message(text="x", raw_segments=[{"type": "Image"}])
        media_types, _ = engine._trigger_media_context(msg, "x")
        self.assertEqual(media_types, ["image"])


if __name__ == "__main__":
    unittest.main()
