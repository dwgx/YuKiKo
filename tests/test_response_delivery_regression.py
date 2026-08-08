"""统一发送核心（core/response_delivery.py）回归测试。

锁五件事（对应架构收敛任务 B）：
1. 短音频 → 单条 silk record 发送。
2. 长音频 → 按段切分逐条发送 record（多条）。
3. 限流触发等待（token-bucket 窗口耗尽时 sleep）。
4. 熔断/暂停跳过（bot 暂停 / 群熔断时零发送）。
5. 图片 / 视频按媒体链发送。

全部通过 mock sender 验证：发送核心只依赖 `async def send(chain) -> bool` 窄接口。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from core.engine_types import EngineResponse
from core.platform.components import Image, MessageChain, Plain, Record, Video
from core.response_delivery import deliver_response


class _MockSender:
    """记录 sender 收到的 MessageChain，可配置发送结果。"""

    def __init__(self, result: bool = True) -> None:
        self.sent_chains: list[MessageChain] = []
        self.send_count = 0
        self.result = result

    async def send(self, chain: Any) -> bool:
        self.send_count += 1
        self.sent_chains.append(chain)
        return self.result


def _record_files(chains: list[MessageChain]) -> list[str]:
    return [
        c.file
        for chain in chains
        for c in chain.components
        if isinstance(c, Record)
    ]


class ResponseDeliveryVoiceTests(unittest.IsolatedAsyncioTestCase):
    """语音：短音频单条、长音频分段。"""

    async def test_short_audio_sends_single_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "clip.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="reply", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            silk = Path(tmp) / "clip.silk"
            silk.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=30.0),
                patch("app._silk_encode_for_record", new=AsyncMock(return_value=silk)),
            ):
                await deliver_response(
                    {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                    response,
                    sender.send,
                    conversation_id="group:101",
                    group_id=101,
                    bot_id="bot1",
                )
            self.assertEqual(sender.send_count, 1)
            records = _record_files(sender.sent_chains)
            self.assertEqual(len(records), 1)
            # NapCat 沙盒读不到 file:// 项目路径，语音直接以 base64 发送。
            self.assertTrue(records[0].startswith("base64://"))

    async def test_long_audio_splits_into_multiple_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "song.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="reply", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            part1 = Path(tmp) / "song.part1.silk"
            part2 = Path(tmp) / "song.part2.silk"
            part1.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            part2.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._split_voice_audio_file", new=AsyncMock(return_value=[part1, part2])),
                patch("app._silk_encode_for_record", new=AsyncMock(side_effect=lambda p, s: p)),
            ):
                await deliver_response(
                    {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                    response,
                    sender.send,
                    conversation_id="group:102",
                    group_id=102,
                    bot_id="bot1",
                )
            self.assertEqual(sender.send_count, 2)
            records = _record_files(sender.sent_chains)
            self.assertEqual(len(records), 2)
            # NapCat 沙盒读不到 file:// 项目路径，语音直接以 base64 发送。
            self.assertTrue(all(f.startswith("base64://") for f in records))

    async def test_voice_with_text_sends_text_then_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "clip.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(
                action="reply", reason="test", reply_text="给你发首歌", audio_file=str(mp3)
            )
            sender = _MockSender()
            silk = Path(tmp) / "clip.silk"
            silk.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=30.0),
                patch("app._silk_encode_for_record", new=AsyncMock(return_value=silk)),
            ):
                await deliver_response(
                    {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                    response,
                    sender.send,
                    conversation_id="group:103",
                    group_id=103,
                    bot_id="bot1",
                )
            self.assertEqual(sender.send_count, 2)
            self.assertEqual(sender.sent_chains[0].get_plain_text(), "给你发首歌")
            records = _record_files(sender.sent_chains[1:])
            self.assertEqual(len(records), 1)


class ResponseDeliveryMusicVoiceTests(unittest.IsolatedAsyncioTestCase):
    """点歌语音 4 特性（E4 从 app.py 迁入 _send_voice）：

    - music_force_full：长音频也整段直发，不再分段。
    - music_disable_split：禁分段，兜底裁一条单发。
    - silk 源互换：.silk 输入用 sibling mp3 当切分源；且长音频下覆盖 force_full 只走分段。
    - 音乐缓存路径推断：audio 落在 music_* 命名时按点歌策略发送。
    - 完整文件转 silk 只在"将直发"分支内执行（长音频等切片时不预编整段）。
    """

    async def test_music_force_full_sends_full_record_without_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "song.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="music_play", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            silk = Path(tmp) / "song.silk"
            silk.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._silk_encode_for_record", new=AsyncMock(return_value=silk)),
            ):
                await deliver_response(
                    {
                        "bot": {
                            "voice_send_max_seconds": 60,
                            "voice_send_music_force_full": True,
                        },
                        "send_rate": {"enable": False},
                    },
                    response,
                    sender.send,
                    conversation_id="group:401",
                    group_id=401,
                    bot_id="bot4",
                    is_music_voice_action=True,
                )
            # force_full：200s 长音频也整段直发单条，不分段。
            self.assertEqual(sender.send_count, 1)
            records = _record_files(sender.sent_chains)
            self.assertEqual(len(records), 1)
            # NapCat 沙盒读不到 file:// 项目路径，语音直接以 base64 发送。
            self.assertTrue(records[0].startswith("base64://"))

    async def test_music_disable_split_sends_single_trimmed_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "song.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="music_play", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            trimmed = Path(tmp) / "song.voice60s.mp3"
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch(
                    "app._prepare_voice_audio_file",
                    new=AsyncMock(return_value=(trimmed, 200.0, True)),
                ),
                patch("app._silk_encode_for_record", new=AsyncMock(side_effect=lambda p, s: p)),
            ):
                await deliver_response(
                    {
                        "bot": {
                            "voice_send_max_seconds": 60,
                            "voice_send_music_disable_split": True,
                        },
                        "send_rate": {"enable": False},
                    },
                    response,
                    sender.send,
                    conversation_id="group:402",
                    group_id=402,
                    bot_id="bot4",
                    is_music_voice_action=True,
                )
            # disable_split：不整段直发也不分段，兜底裁到 max_seconds 单发一条。
            self.assertEqual(sender.send_count, 1)
            records = _record_files(sender.sent_chains)
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].endswith("song.voice60s.mp3"))

    async def test_silk_source_swap_splits_sibling_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            silk = Path(tmp) / "song.silk"
            mp3 = Path(tmp) / "song.mp3"
            silk.write_bytes(b"\x02" * 2048)
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="reply", reason="test", audio_file=str(silk))
            sender = _MockSender()
            part1 = Path(tmp) / "song.part1.silk"
            part2 = Path(tmp) / "song.part2.silk"
            part1.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            part2.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            split_mock = AsyncMock(return_value=[part1, part2])
            encode_calls: list[Path] = []

            async def fake_encode(path, seconds):
                encode_calls.append(Path(path))
                return path

            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._split_voice_audio_file", new=split_mock),
                patch("app._silk_encode_for_record", new=fake_encode),
            ):
                await deliver_response(
                    {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                    response,
                    sender.send,
                    conversation_id="group:403",
                    group_id=403,
                    bot_id="bot4",
                )
            # silk 源互换：切分源是 sibling mp3 而非 silk 本体（resolve 后比较，兼容 /tmp 符号链接）。
            split_mock.assert_awaited_once_with(mp3.resolve(), segment_seconds=60, max_segments=8)
            self.assertEqual(sender.send_count, 2)
            # 直发分支不应编码整段（silk 源互换后长音频只走分段）。
            self.assertEqual(encode_calls, [part1, part2])

    async def test_silk_swap_overrides_music_force_full_for_long_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            silk = Path(tmp) / "song.silk"
            mp3 = Path(tmp) / "song.mp3"
            silk.write_bytes(b"\x02" * 2048)
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="music_play", reason="test", audio_file=str(silk))
            sender = _MockSender()
            part1 = Path(tmp) / "song.part1.silk"
            part2 = Path(tmp) / "song.part2.silk"
            part1.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            part2.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            split_mock = AsyncMock(return_value=[part1, part2])
            encode_calls: list[Path] = []

            async def fake_encode(path, seconds):
                encode_calls.append(Path(path))
                return path

            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._split_voice_audio_file", new=split_mock),
                patch("app._silk_encode_for_record", new=fake_encode),
            ):
                await deliver_response(
                    {
                        "bot": {
                            "voice_send_max_seconds": 60,
                            "voice_send_try_full_first": True,
                            "voice_send_music_force_full": True,
                        },
                        "send_rate": {"enable": False},
                    },
                    response,
                    sender.send,
                    conversation_id="group:404",
                    group_id=404,
                    bot_id="bot4",
                    is_music_voice_action=True,
                )
            # 即使 force_full + try_full_first，silk 源互换后长音频仍只走分段。
            split_mock.assert_awaited_once_with(mp3.resolve(), segment_seconds=60, max_segments=8)
            self.assertEqual(sender.send_count, 2)
            self.assertEqual(encode_calls, [part1, part2])

    async def test_music_cache_path_inference_applies_force_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "music_demo.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="reply", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            silk = Path(tmp) / "music_demo.silk"
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._silk_encode_for_record", new=AsyncMock(return_value=silk)),
            ):
                await deliver_response(
                    {
                        "bot": {
                            "voice_send_max_seconds": 60,
                            "voice_send_music_force_full": True,
                        },
                        "send_rate": {"enable": False},
                    },
                    response,
                    sender.send,
                    conversation_id="group:405",
                    group_id=405,
                    bot_id="bot4",
                )
            # 未显式标记点歌，但 music_* 命名推断为点歌 → force_full 生效：整段直发单条。
            self.assertEqual(sender.send_count, 1)
            records = _record_files(sender.sent_chains)
            self.assertEqual(len(records), 1)

    async def test_non_music_path_without_flag_still_splits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "song.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="reply", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            part1 = Path(tmp) / "song.part1.silk"
            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._split_voice_audio_file", new=AsyncMock(return_value=[part1])),
                patch("app._silk_encode_for_record", new=AsyncMock(side_effect=lambda p, s: p)),
            ):
                await deliver_response(
                    {
                        "bot": {
                            "voice_send_max_seconds": 60,
                            "voice_send_music_force_full": True,
                        },
                        "send_rate": {"enable": False},
                    },
                    response,
                    sender.send,
                    conversation_id="group:406",
                    group_id=406,
                    bot_id="bot4",
                )
            # 同名但非 music_* 命名：不推断为点歌，force_full 不生效，长音频仍分段。
            self.assertEqual(sender.send_count, 1)

    async def test_long_audio_without_full_first_does_not_pre_encode_source(self) -> None:
        """完整文件转 silk 只在"将直发"分支内执行：长音频等切片时不预编整段。"""
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "song.mp3"
            mp3.write_bytes(b"\xff" * 2048)
            response = EngineResponse(action="reply", reason="test", audio_file=str(mp3))
            sender = _MockSender()
            part = Path(tmp) / "song.part1.silk"
            part.write_bytes(b"\x02#!SILK" + b"\x00" * 500)
            encode_calls: list[Path] = []

            async def fake_encode(path, seconds):
                encode_calls.append(Path(path))
                return Path(path)

            with (
                patch("app._probe_audio_duration_seconds_sync", return_value=200.0),
                patch("app._split_voice_audio_file", new=AsyncMock(return_value=[part])),
                patch("app._silk_encode_for_record", new=fake_encode),
            ):
                await deliver_response(
                    {"bot": {"voice_send_max_seconds": 60}, "send_rate": {"enable": False}},
                    response,
                    sender.send,
                    conversation_id="group:104",
                    group_id=104,
                    bot_id="bot1",
                )
            # 默认不 try_full_first：只编码切片，不预编整段源文件。
            self.assertEqual(encode_calls, [part])


class ResponseDeliveryGuardTests(unittest.IsolatedAsyncioTestCase):
    """发送保护：限流等待 + 熔断/暂停跳过 + 失败标记。"""

    async def test_rate_limit_waits_when_window_exceeded(self) -> None:
        config = {
            "send_rate": {
                "enable": True,
                "max_per_window": 2,
                "window_seconds": 60,
                "warn_threshold": 2,
            },
            "bot": {"multi_reply_enable": True, "multi_reply_max_chars": 300, "multi_reply_max_chunks": 6},
        }
        paragraphs = [f"第{i}段" + "内容" * 50 for i in range(3)]
        response = EngineResponse(action="reply", reason="test", reply_text="\n\n".join(paragraphs))
        sender = _MockSender()
        sleep_mock = AsyncMock()
        await deliver_response(
            config,
            response,
            sender.send,
            conversation_id="group:201",
            group_id=201,
            bot_id="bot2",
            sleep_fn=sleep_mock,
        )
        # 语义拆成 3 段，全部发送；第三条触发限流等待（限流是节流不是丢弃）。
        self.assertEqual(sender.send_count, 3)
        sleep_mock.assert_awaited()
        self.assertGreater(sleep_mock.await_args.args[0], 0)

    async def test_bot_suspended_skips_all_sends(self) -> None:
        response = EngineResponse(action="reply", reason="test", reply_text="你好")
        sender = _MockSender()
        with patch("app._check_bot_send_suspended", return_value=(True, "test_suspend")):
            await deliver_response(
                {"send_rate": {"enable": False}},
                response,
                sender.send,
                conversation_id="group:202",
                group_id=202,
                bot_id="bot2",
            )
        self.assertEqual(sender.send_count, 0)

    async def test_group_blocked_skips_all_sends(self) -> None:
        response = EngineResponse(action="reply", reason="test", reply_text="你好")
        sender = _MockSender()
        with patch("app._check_group_send_block", return_value=(True, "test_block")):
            await deliver_response(
                {"send_rate": {"enable": False}},
                response,
                sender.send,
                conversation_id="group:203",
                group_id=203,
                bot_id="bot2",
            )
        self.assertEqual(sender.send_count, 0)

    async def test_rejected_send_marks_failure(self) -> None:
        response = EngineResponse(action="reply", reason="test", reply_text="你好")
        sender = _MockSender(result=False)
        mark_mock = MagicMock()
        await deliver_response(
            {"send_rate": {"enable": False}},
            response,
            sender.send,
            conversation_id="group:204",
            group_id=204,
            bot_id="bot2",
            mark_failure_fn=mark_mock,
        )
        mark_mock.assert_called_once_with(204, "bot2", "send_rejected")


class ResponseDeliveryMediaTests(unittest.IsolatedAsyncioTestCase):
    """媒体发送：图片 + 视频 + 文本语义拆分。"""

    async def test_image_and_video_send(self) -> None:
        response = EngineResponse(
            action="reply",
            reason="test",
            reply_text="看这个",
            image_url="https://example.com/a.png",
            image_urls=["https://example.com/b.png"],
            video_url="https://example.com/v.mp4",
        )
        sender = _MockSender()
        await deliver_response(
            {"send_rate": {"enable": False}},
            response,
            sender.send,
            conversation_id="group:301",
            group_id=301,
            bot_id="bot3",
        )
        # 文本 1 条 + 媒体 1 条（图片×2 + 视频×1）。
        self.assertEqual(sender.send_count, 2)
        self.assertEqual(sender.sent_chains[0].get_plain_text(), "看这个")
        media_chain = sender.sent_chains[1]
        images = [c for c in media_chain.components if isinstance(c, Image)]
        videos = [c for c in media_chain.components if isinstance(c, Video)]
        self.assertEqual(len(images), 2)
        self.assertEqual(len(videos), 1)

    async def test_text_semantic_split_multiple_sends(self) -> None:
        config = {"send_rate": {"enable": False}, "bot": {"multi_reply_enable": True}}
        text = "\n\n".join(f"段落{i}" + "内容" * 100 for i in range(3))
        response = EngineResponse(action="reply", reason="test", reply_text=text)
        sender = _MockSender()
        await deliver_response(
            config,
            response,
            sender.send,
            conversation_id="group:302",
            group_id=302,
            bot_id="bot3",
        )
        self.assertEqual(sender.send_count, 3)
        plains = [
            c.text
            for chain in sender.sent_chains
            for c in chain.components
            if isinstance(c, Plain)
        ]
        self.assertEqual(len(plains), 3)
        self.assertEqual("".join(plains), text.replace("\n", ""))


if __name__ == "__main__":
    unittest.main()
