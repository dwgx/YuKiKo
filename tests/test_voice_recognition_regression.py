"""语音识别回归：QQ 语音是腾讯 SILK v3，不是 mp3 也不是 amr。

线上实测：9/9 次语音回合都以「语音转录结果为空，可能是静音或无法识别」收场，
模型据此让用户重录。真实原因有两层，而旧实现把两层都压成同一句话：

1. 字节是 SILK v3（头 `0x02` + `#!SILK_V3`），ffmpeg 没有 silk demuxer，
   落盘时的 `.mp3` 扩展名是骗人的，探测分只有 1，转码必然失败；
   失败后旧代码还把 SILK 原样当 wav 喂给 Whisper，于是拿到空转写。
2. ASR 引擎（openai-whisper）压根没装，ImportError 被吞成同样的空转写。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.agent_tools import _handle_analyze_voice

# utils.media 的三个新函数在测试体内 import：模块级 import 会让基线跑成
# collect-time ImportError，看不到逐条 FAILED，也就证明不了行为契约是红的。

# 腾讯 SILK v3 头：单字节 0x02 前缀 + "#!SILK_V3"。
_TENCENT_SILK_HEADER = b"\x02#!SILK_V3"


def _write_tencent_silk(path: Path) -> Path:
    path.write_bytes(_TENCENT_SILK_HEADER + b"\x13\x00" + b"\x00" * 64)
    return path


class SilkContainerSniffTests(unittest.IsolatedAsyncioTestCase):
    def test_should_report_silk_when_bytes_are_tencent_silk_despite_mp3_suffix(self) -> None:
        from utils.media import sniff_audio_container

        with tempfile.TemporaryDirectory() as tmpdir:
            # 落盘名故意用 .mp3，复现 QQ 语音缓存的真实形态。
            voice = _write_tencent_silk(Path(tmpdir) / "voice.mp3")
            self.assertEqual(sniff_audio_container(voice), "silk")

    def test_should_report_empty_container_when_file_is_missing(self) -> None:
        from utils.media import sniff_audio_container

        self.assertEqual(sniff_audio_container(Path("/nonexistent/nope.mp3")), "")

    async def test_should_decode_silk_via_pilk_instead_of_feeding_it_to_ffmpeg(self) -> None:
        from utils.media import extract_audio_detailed

        with tempfile.TemporaryDirectory() as tmpdir:
            voice = _write_tencent_silk(Path(tmpdir) / "voice.mp3")
            wav = Path(tmpdir) / "voice.wav"
            ffmpeg_calls: list[list[str]] = []

            async def fake_run_ffmpeg(args: list[str], **kwargs: object) -> tuple[bool, str]:
                _ = kwargs
                ffmpeg_calls.append(args)
                Path(args[-1]).write_bytes(b"RIFF0000WAVE")
                return True, ""

            def fake_pilk_decode(src: str, pcm: str, **kwargs: object) -> int:
                _ = (src, kwargs)
                Path(pcm).write_bytes(b"\x00\x01" * 512)
                return 1

            with (
                patch("utils.media.run_ffmpeg", new=AsyncMock(side_effect=fake_run_ffmpeg)),
                patch("pilk.decode", new=fake_pilk_decode),
            ):
                result, reason = await extract_audio_detailed(voice, wav)

            self.assertEqual(reason, "")
            self.assertEqual(result, str(wav))
            self.assertEqual(len(ffmpeg_calls), 1)
            # 关键契约：ffmpeg 拿到的是解好的裸 PCM，不是 SILK 原文件。
            self.assertIn("s16le", ffmpeg_calls[0])
            self.assertNotIn(str(voice), ffmpeg_calls[0])

    async def test_should_report_not_silk_when_header_is_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            junk = Path(tmpdir) / "junk.mp3"
            junk.write_bytes(b"\x00" * 128)
            from utils.media import decode_silk_to_wav

            result, reason = await decode_silk_to_wav(junk, Path(tmpdir) / "out.wav")

        self.assertIsNone(result)
        self.assertEqual(reason, "not_silk")


class AnalyzeVoiceFailureContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 语音落盘走 _VOICE_CACHE_DIR，指到临时目录，别往 storage/cache/voice 塞 fixture。
        # create=True：旧实现里没有这个常量，但 setUp 不该是红的来源 —— 这几条
        # 断言的是行为契约，必须让它们红在 assert 上，而不是红在 patch 找不到符号上。
        self._cache = tempfile.TemporaryDirectory(prefix="cc-yk-L2-voice-")
        self.addCleanup(self._cache.cleanup)
        patcher = patch(
            "core.agent_tools_media._VOICE_CACHE_DIR",
            Path(self._cache.name) / "voice",
            create=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 旧实现无视该常量、直写 storage/cache/voice；基线跑动时别留垃圾。
        self._real_cache = Path("storage/cache/voice")
        before = set(self._real_cache.glob("*")) if self._real_cache.is_dir() else set()
        self.addCleanup(self._drop_files_created_in_real_cache, before)

    def _drop_files_created_in_real_cache(self, before: set[Path]) -> None:
        if not self._real_cache.is_dir():
            return
        for leftover in set(self._real_cache.glob("*")) - before:
            leftover.unlink(missing_ok=True)

    def _record_context(self, path: str, *, api_call: object = None) -> dict[str, object]:
        # 真实 record 段同时给 file / path / url；path 是 QQ 容器里的本机原始文件。
        return {
            "raw_segments": [
                {"type": "record", "data": {"file": "voice.amr", "path": path, "url": path}}
            ],
            "reply_media_segments": [],
            "api_call": api_call,
            "trace_id": "000001-11-testtrace",
        }

    async def test_should_say_engine_unavailable_instead_of_silence_when_asr_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            voice = Path(tmpdir) / "voice.amr"
            voice.write_bytes(b"#!AMR\n" + b"\x00" * 64)
            engine_missing = {
                "text": "",
                "score": -999,
                "pass": "engine_missing",
                "reason": "asr_engine_missing",
            }
            with (
                patch(
                    "utils.media.transcribe_audio_enhanced",
                    new=AsyncMock(return_value=engine_missing),
                ),
                patch("utils.media.extract_audio", new=AsyncMock(return_value="v.wav")),
            ):
                result = await _handle_analyze_voice({}, self._record_context(str(voice)))

        self.assertFalse(result.ok)
        self.assertIn("voice_engine_unavailable", result.error)
        # 旧行为是把「引擎没装」说成「可能是静音」，让用户白重录。
        self.assertNotIn("静音", result.display)
        self.assertNotIn("没录上", result.display)

    async def test_should_prefer_local_record_path_over_asking_napcat_to_transcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            voice = Path(tmpdir) / "voice.amr"
            voice.write_bytes(b"#!AMR\n" + b"\x00" * 64)
            napcat = AsyncMock(return_value={"url": "https://example.com/should-not-be-used"})
            transcribed = {"text": "本地直读成功", "formatted_text": "本地直读成功", "score": -0.4}
            with (
                patch("core.agent_tools_media.call_napcat_api", new=napcat),
                patch(
                    "utils.media.transcribe_audio_enhanced",
                    new=AsyncMock(return_value=transcribed),
                ),
                patch("utils.media.extract_audio", new=AsyncMock(return_value="v.wav")),
            ):
                result = await _handle_analyze_voice(
                    {}, self._record_context(str(voice), api_call=lambda *a, **k: None)
                )

        self.assertTrue(result.ok, result.display)
        self.assertEqual(result.data.get("text"), "本地直读成功")
        # NapCat 的 get_record 在本机是 spawn EPERM 死的，本地文件可读时不该去打它。
        napcat.assert_not_awaited()

    async def test_should_not_leak_tool_name_or_retcode_in_what_it_tells_the_user(self) -> None:
        """业主实测抱怨过「analyze_image 又超时了」这种回复：display 会被模型抄进群。"""
        action_failed = RuntimeError(
            "ActionFailed | retcode=1200 | 文件转换失败: spawn EPERM | api=get_record"
        )
        with (
            patch(
                "core.agent_tools_media.call_napcat_api", new=AsyncMock(side_effect=action_failed)
            ),
            patch("utils.media.download_file", new=AsyncMock(side_effect=action_failed)),
        ):
            result = await _handle_analyze_voice(
                {},
                {
                    "raw_segments": [
                        {
                            "type": "record",
                            "data": {
                                "file": "voice.amr",
                                "path": "",
                                "url": "https://multimedia.nt.qq.com.cn/download?fileid=x",
                            },
                        }
                    ],
                    "reply_media_segments": [],
                    "api_call": lambda *a, **k: None,
                    "trace_id": "000001-12-testtrace",
                },
            )

        self.assertFalse(result.ok)
        for leaked in ("retcode", "get_record", "whisper", "pip", "EPERM", "ActionFailed"):
            self.assertNotIn(leaked, result.display)
            self.assertNotIn(leaked, result.error)

    async def test_should_stop_trying_other_sources_once_engine_is_known_missing(self) -> None:
        """引擎不在时，再换一份字节回来也转不出字：不该白花下载和 WS 往返。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            voice = Path(tmpdir) / "voice.amr"
            voice.write_bytes(b"#!AMR\n" + b"\x00" * 64)
            download = AsyncMock(return_value=True)
            napcat = AsyncMock(return_value={"url": "https://example.com/another"})
            engine_missing = {"text": "", "score": -999, "pass": "engine_missing"}
            context = {
                "raw_segments": [
                    {
                        "type": "record",
                        "data": {
                            "file": "voice.amr",
                            "path": str(voice),
                            "url": "https://multimedia.nt.qq.com.cn/download?fileid=x",
                        },
                    }
                ],
                "reply_media_segments": [],
                "api_call": lambda *a, **k: None,
                "trace_id": "000001-13-testtrace",
            }
            with (
                patch("utils.media.download_file", new=download),
                patch("core.agent_tools_media.call_napcat_api", new=napcat),
                patch(
                    "utils.media.transcribe_audio_enhanced",
                    new=AsyncMock(return_value=engine_missing),
                ),
                patch("utils.media.extract_audio", new=AsyncMock(return_value="v.wav")),
            ):
                result = await _handle_analyze_voice({}, context)

        self.assertFalse(result.ok)
        self.assertIn("voice_engine_unavailable", result.error)
        download.assert_not_awaited()
        napcat.assert_not_awaited()

    async def test_should_say_decode_failed_when_bytes_cannot_be_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            junk = Path(tmpdir) / "voice.amr"
            junk.write_bytes(b"\x00" * 256)
            transcribe = AsyncMock(return_value={"text": "不该被调用"})
            with (
                patch("utils.media.extract_audio", new=AsyncMock(return_value=None)),
                patch("utils.media.transcribe_audio_enhanced", new=transcribe),
            ):
                result = await _handle_analyze_voice({}, self._record_context(str(junk)))

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "voice_decode_failed")
        # 解不开就不该把原始字节当 wav 硬喂给 ASR —— 那只会得到假的空转写。
        transcribe.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
