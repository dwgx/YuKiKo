"""Phase 2 平台主路径：长音频分段发送 + 发送保护回归测试。

锁三件事（对应 core/platform/run_primary.py 的 `_wire_bridge` / `_send_voice_response` /
`_build_send_guard`）：
1. 长音频（>voice_send_max_seconds）走分段：切段 → 每段转 silk → 逐条发 record。
2. 短音频仍走单条 silk record。
3. 发送保护：token-bucket 限流在超过窗口限制时等待；bot 暂停 / 群熔断时跳过发送；
   发送失败会触发按群熔断与 bot 暂停标记。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.engine_types import EngineResponse
from core.platform.components import MessageChain, Plain
from core.platform.run_primary import (
    _build_send_guard,
    _maybe_mark_platform_send_block,
    _wire_bridge,
)


class _FakeDispatcher:
    def next_seq(self, conversation_id: str) -> int:
        return 1

    def pending_count(self, conversation_id: str) -> int:
        return 0


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent_chains: list[MessageChain] = []
        self.send_count = 0
        self.send_result = True

    async def send_by_session(self, session_id: str, chain: MessageChain) -> bool:
        self.send_count += 1
        self.sent_chains.append(chain)
        return self.send_result


class _FakeEngine:
    def __init__(self, config: dict, response: EngineResponse) -> None:
        self.config = config
        self._response = response

    async def handle_message(self, payload):
        return self._response


def _group_event(group_id: int, text: str = "hi") -> dict:
    return {
        "conversation_id": f"group:{group_id}",
        "user_id": "10001",
        "group_id": group_id,
        "message_id": f"m-{group_id}",
        "is_private": False,
        "text": text,
        "chain": MessageChain([Plain(text)]),
    }


def _record_files(chain: MessageChain) -> list[str]:
    return [c.file for c in chain.components if type(c).__name__ == "Record"]


class PlatformPrimaryVoiceSplitTests(unittest.IsolatedAsyncioTestCase):
    """长音频分段决策：超过 voice_max_seconds 时按段切分逐条发 record。"""

    async def test_long_audio_is_split_into_multiple_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            long_mp3 = Path(tmp) / "song.mp3"
            long_mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(
                action="reply", reason="test", audio_file=str(long_mp3)
            )
            engine = _FakeEngine(
                {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                response,
            )
            adapter = _FakeAdapter()
            _wire_bridge(engine, adapter, {"bot_id": "123"})

            part_silk = Path(tmp) / "song.part1.silk"
            part2_silk = Path(tmp) / "song.part2.silk"
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch(
                    "app._split_voice_audio_file",
                    new=AsyncMock(return_value=[part_silk, part2_silk]),
                ),
                patch("app._silk_encode_for_record", new=AsyncMock(side_effect=lambda p, s: p)),
            ):
                await adapter.message_handler(_group_event(424201, "点歌"))

            # 无文本时静态内容不发送；长音频分两段 → 共 2 次发送。
            self.assertEqual(adapter.send_count, 2)
            voice_files = [
                f
                for chain in adapter.sent_chains
                for f in _record_files(chain)
            ]
            self.assertEqual(len(voice_files), 2)
            self.assertTrue(all(f.startswith("file://") for f in voice_files))
            self.assertTrue(all(".silk" in f for f in voice_files))

    async def test_short_audio_sends_single_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            short_mp3 = Path(tmp) / "clip.mp3"
            short_mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(
                action="reply", reason="test", audio_file=str(short_mp3)
            )
            engine = _FakeEngine(
                {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                response,
            )
            adapter = _FakeAdapter()
            _wire_bridge(engine, adapter, {"bot_id": "123"})

            silk = Path(tmp) / "clip.silk"
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=30.0),
                patch("app._silk_encode_for_record", new=AsyncMock(return_value=silk)),
            ):
                await adapter.message_handler(_group_event(424202, "点歌"))

            # 无文本时静态内容不发送；短音频单条 record。
            self.assertEqual(adapter.send_count, 1)
            voice_files = [
                f
                for chain in adapter.sent_chains
                for f in _record_files(chain)
            ]
            self.assertEqual(len(voice_files), 1)
            self.assertIn(".silk", voice_files[0])


class PlatformPrimarySendGuardTests(unittest.IsolatedAsyncioTestCase):
    """发送保护：限流等待 + 暂停/熔断跳过 + 失败标记。"""

    async def test_rate_limit_waits_when_window_exceeded(self) -> None:
        engine = _FakeEngine(
            {
                "send_rate": {
                    "enable": True,
                    "max_per_window": 2,
                    "window_seconds": 60,
                    "warn_threshold": 2,
                }
            },
            EngineResponse(action="reply", reason="test"),
        )
        adapter = _FakeAdapter()
        guard_send = _build_send_guard(
            engine, adapter, conversation_id="group:777001", group_id=777001, bot_id="999001"
        )
        with patch("core.platform.run_primary._platform_sleep", new=AsyncMock()) as sleep_mock:
            ok1 = await guard_send(MessageChain([Plain("a")]))
            ok2 = await guard_send(MessageChain([Plain("b")]))
            ok3 = await guard_send(MessageChain([Plain("c")]))

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertTrue(ok3)
        # 窗口容量 2，第三条触发等待；消息仍被发送（限流是节流不是丢弃）。
        self.assertEqual(adapter.send_count, 3)
        self.assertGreaterEqual(sleep_mock.await_count, 1)
        self.assertGreater(sleep_mock.await_args.args[0], 0)

    async def test_bot_suspended_skips_send(self) -> None:
        engine = _FakeEngine(
            {"send_rate": {"enable": False}},
            EngineResponse(action="reply", reason="test"),
        )
        adapter = _FakeAdapter()
        # 暂停检查在 guard 构建时从 app 导入，需在构建前打桩。
        with patch("app._check_bot_send_suspended", return_value=(True, "test_suspend")):
            guard_send = _build_send_guard(
                engine, adapter, conversation_id="group:777002", group_id=777002, bot_id="999002"
            )
            ok = await guard_send(MessageChain([Plain("a")]))
        self.assertFalse(ok)
        self.assertEqual(adapter.send_count, 0)

    async def test_group_blocked_skips_send(self) -> None:
        engine = _FakeEngine(
            {"send_rate": {"enable": False}},
            EngineResponse(action="reply", reason="test"),
        )
        adapter = _FakeAdapter()
        with patch("app._check_group_send_block", return_value=(True, "test_block")):
            guard_send = _build_send_guard(
                engine, adapter, conversation_id="group:777003", group_id=777003, bot_id="999003"
            )
            ok = await guard_send(MessageChain([Plain("a")]))
        self.assertFalse(ok)
        self.assertEqual(adapter.send_count, 0)

    async def test_rejected_send_marks_group_block(self) -> None:
        engine = _FakeEngine(
            {"send_rate": {"enable": False}},
            EngineResponse(action="reply", reason="test"),
        )
        adapter = _FakeAdapter()
        adapter.send_result = False
        guard_send = _build_send_guard(
            engine, adapter, conversation_id="group:777004", group_id=777004, bot_id="999004"
        )
        with patch(
            "core.platform.run_primary._mark_platform_send_failure"
        ) as mark_mock:
            ok = await guard_send(MessageChain([Plain("a")]))
        self.assertFalse(ok)
        mark_mock.assert_called_once_with(777004, "999004", "platform_send_rejected")

    async def test_rate_limit_error_text_marks_299_block(self) -> None:
        mark_block = MagicMock()
        suspend = MagicMock()
        with (
            patch("app._mark_group_send_block", mark_block),
            patch("app._suspend_bot_send", suspend),
        ):
            _maybe_mark_platform_send_block(777005, "999005", '"result": 299 rate limit')
        self.assertEqual(mark_block.call_count, 1)
        suspend.assert_called_once()


if __name__ == "__main__":
    unittest.main()
