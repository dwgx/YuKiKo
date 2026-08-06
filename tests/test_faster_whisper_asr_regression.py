"""faster-whisper 接线回归：analyze_voice 必须真的出文字。

线上实测 analyze_voice 0/9 成功、日志里 9 次 'whisper not installed' —— 因为
utils/media.py 里 `import whisper` 指的是 openai-whisper，而它从来没进过
requirements.txt。本机装好并实测跑通的是 faster-whisper，两者 API 不同：
`transcribe()` 返回 `(segments 生成器, info)`，不迭代 segments 就不会真解码，
也没有 fp16 参数。

这里断言契约而不是识别质量：返回形状、pass 取值（「引擎不在」必须和「真静音」
可区分）、繁体转简体、模型按规格单例复用、失败被分类且异常原文不外泄。
一律用 stub 替掉 WhisperModel —— 真装载会下权重（实测首次 386.8s）。
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# utils.media 的新符号一律在测试体内 import：模块级 import 会让基线跑成
# collect 阶段错误，看不到逐条 FAILED，也就证明不了行为契约是红的。


def _segment(
    text: str,
    *,
    start: float = 0.0,
    end: float = 1.0,
    avg_logprob: float = -0.2,
    no_speech_prob: float = 0.01,
) -> SimpleNamespace:
    """仿 faster_whisper.transcribe.Segment，只带本仓读到的字段。"""
    return SimpleNamespace(
        start=start,
        end=end,
        text=text,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
    )


def _weak_segment(text: str) -> SimpleNamespace:
    """分数低到不会触发「首趟够好就短路」的段落。"""
    return _segment(text, avg_logprob=-0.9, no_speech_prob=0.5)


def _info(*, language: str = "zh", duration: float = 3.0) -> SimpleNamespace:
    return SimpleNamespace(language=language, language_probability=1.0, duration=duration)


class _StubWhisperModel:
    """记录构造参数并按剧本吐 (segments 生成器, info) 的 WhisperModel 替身。"""

    instances: list[_StubWhisperModel] = []

    def __init__(self, model_size_or_path: str, *, device: str = "auto", compute_type: str = "default"):
        self.model_size_or_path = model_size_or_path
        self.device = device
        self.compute_type = compute_type
        self.calls: list[dict] = []
        # 每趟 pass 的剧本，由 _stub_engine 注入。
        self.plans: list[list[SimpleNamespace]] = []
        self.transcribe_error: BaseException | None = None
        self.transcribe_delay: float = 0.0
        _StubWhisperModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls.append(dict(kwargs))
        if self.transcribe_delay:
            time.sleep(self.transcribe_delay)
        if self.transcribe_error is not None:
            raise self.transcribe_error
        index = min(len(self.calls) - 1, len(self.plans) - 1) if self.plans else -1
        segments = list(self.plans[index]) if index >= 0 else []
        return (seg for seg in segments), _info()


@contextlib.contextmanager
def _stub_engine(
    plans: list[list[SimpleNamespace]] | None = None,
    *,
    transcribe_error: BaseException | None = None,
    transcribe_delay: float = 0.0,
    construct_error: BaseException | None = None,
):
    """把 faster_whisper 换成 stub 模块，并清干净进程内的模型缓存。"""
    import utils.media as media

    _StubWhisperModel.instances = []

    def _factory(model_size_or_path, *, device="auto", compute_type="default", **_kwargs):
        if construct_error is not None:
            raise construct_error
        model = _StubWhisperModel(model_size_or_path, device=device, compute_type=compute_type)
        model.plans = plans or []
        model.transcribe_error = transcribe_error
        model.transcribe_delay = transcribe_delay
        return model

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _factory  # type: ignore[attr-defined]
    cache = getattr(media, "_whisper_models", None)
    saved = dict(cache) if isinstance(cache, dict) else None
    if isinstance(cache, dict):
        cache.clear()
    try:
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            yield
    finally:
        if isinstance(cache, dict):
            cache.clear()
            cache.update(saved or {})


@contextlib.contextmanager
def _wav_fixture(name: str = "voice.wav"):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / name
        path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        yield path


class TranscriptionOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_should_return_spoken_text_when_engine_produces_segments(self) -> None:
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("你好呀")]]):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["text"], "你好呀")
        self.assertNotEqual(res["pass"], "engine_missing")
        self.assertIn("formatted_text", res)
        self.assertIn("你好呀", res["formatted_text"])

    async def test_should_drain_the_lazy_segment_generator_instead_of_leaving_it_unread(self) -> None:
        """faster-whisper 的 segments 不迭代就不解码，返回生成器本身等于没转写。"""
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("前半"), _segment("后半", start=1.0, end=2.0)]]):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["text"], "前半后半")
        self.assertEqual(len(res["raw_segments"]), 2)
        self.assertIsInstance(res["raw_segments"], list)

    async def test_should_hand_back_simplified_chinese_when_engine_emits_traditional(self) -> None:
        """实测 faster-whisper 中文输出是繁体（'吃米飯必須配西瓜'），本仓一律简体。"""
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("吃米飯必須配西瓜")]]):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["text"], "吃米饭必须配西瓜")
        self.assertNotIn("飯", res["formatted_text"])

    async def test_should_timestamp_each_segment_in_the_formatted_transcript(self) -> None:
        from utils.media import transcribe_audio_enhanced

        plan = [[_weak_segment("第一句"), _segment("第二句", start=1.5, end=2.5)]]
        with _wav_fixture() as wav, _stub_engine(plan):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertIn("[1.5s - 2.5s] 第二句", res["formatted_text"])

    async def test_should_report_empty_transcript_as_silence_not_as_missing_engine(self) -> None:
        """一个字都没转出来时，调用方要能看出是「真静音」而不是「引擎没装」。"""
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[], [], []]):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["text"], "")
        self.assertNotEqual(res["pass"], "engine_missing")
        self.assertNotEqual(res["pass"], "timeout")


class ModelReuseAndSpecTests(unittest.IsolatedAsyncioTestCase):
    async def test_should_load_the_model_once_and_reuse_it_across_calls(self) -> None:
        """装载实测首次 386.8s，每回合重装一次等于语音功能不可用。"""
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("一次")]]):
            await transcribe_audio_enhanced(wav, language="zh")
            await transcribe_audio_enhanced(wav, language="zh")
            await transcribe_audio_enhanced(wav, language="zh")
            self.assertEqual(len(_StubWhisperModel.instances), 1)

    async def test_should_load_a_separate_model_when_the_requested_spec_differs(self) -> None:
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("换规格")]]):
            await transcribe_audio_enhanced(wav, model_size="small", language="zh")
            await transcribe_audio_enhanced(wav, model_size="medium", language="zh")
            sizes = [m.model_size_or_path for m in _StubWhisperModel.instances]

        self.assertEqual(sizes, ["small", "medium"])

    async def test_should_take_model_spec_from_environment_instead_of_hardcoding_it(self) -> None:
        from utils.media import transcribe_audio_enhanced

        env = {
            "YUKIKO_ASR_MODEL_SIZE": "tiny",
            "YUKIKO_ASR_DEVICE": "cpu",
            "YUKIKO_ASR_COMPUTE_TYPE": "float32",
        }
        with _wav_fixture() as wav, _stub_engine([[_segment("按配置")]]), patch.dict(os.environ, env):
            await transcribe_audio_enhanced(wav, language="zh")
            self.assertEqual(len(_StubWhisperModel.instances), 1)
            model = _StubWhisperModel.instances[0]

        self.assertEqual(model.model_size_or_path, "tiny")
        self.assertEqual(model.device, "cpu")
        self.assertEqual(model.compute_type, "float32")

    async def test_should_prefer_explicit_arguments_over_environment_defaults(self) -> None:
        from utils.media import transcribe_audio_enhanced

        env = {"YUKIKO_ASR_MODEL_SIZE": "tiny", "YUKIKO_ASR_COMPUTE_TYPE": "float32"}
        with _wav_fixture() as wav, _stub_engine([[_segment("显式优先")]]), patch.dict(os.environ, env):
            await transcribe_audio_enhanced(wav, model_size="base", compute_type="int8", language="zh")
            model = _StubWhisperModel.instances[0]

        self.assertEqual(model.model_size_or_path, "base")
        self.assertEqual(model.compute_type, "int8")

    async def test_should_not_pass_fp16_to_faster_whisper(self) -> None:
        """openai-whisper 才有 fp16；faster-whisper 收到会直接 TypeError。"""
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_weak_segment("查参数")], [], []]):
            await transcribe_audio_enhanced(wav, language="zh")
            all_kwargs = list(_StubWhisperModel.instances[0].calls)

        self.assertTrue(all_kwargs)
        for kwargs in all_kwargs:
            self.assertNotIn("fp16", kwargs)


class MultiPassScoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_should_stop_after_the_first_pass_when_it_is_already_confident(self) -> None:
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("一趟就够")]]):
            await transcribe_audio_enhanced(wav, language="zh")
            call_count = len(_StubWhisperModel.instances[0].calls)

        self.assertEqual(call_count, 1)

    async def test_should_pick_the_highest_scoring_pass_when_the_first_one_is_shaky(self) -> None:
        from utils.media import transcribe_audio_enhanced

        plans = [
            [_weak_segment("含糊一")],
            [_weak_segment("含糊二")],
            [_segment("听清了")],
        ]
        with _wav_fixture() as wav, _stub_engine(plans):
            res = await transcribe_audio_enhanced(wav, language="zh")
            call_count = len(_StubWhisperModel.instances[0].calls)

        self.assertEqual(call_count, 3)
        self.assertEqual(res["text"], "听清了")
        self.assertEqual(res["pass"], "Pass-3-PromptGuided")


class FailureClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_should_say_engine_missing_when_faster_whisper_is_not_installed(self) -> None:
        from utils.media import transcribe_audio_enhanced

        real_import = __import__

        def _blocked_import(name, *args, **kwargs):
            if name == "faster_whisper":
                raise ImportError("No module named 'faster_whisper'")
            return real_import(name, *args, **kwargs)

        with _wav_fixture() as wav, patch("builtins.__import__", side_effect=_blocked_import):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["pass"], "engine_missing")
        self.assertEqual(res["reason"], "asr_engine_missing")
        self.assertEqual(res["text"], "")

    async def test_should_say_engine_missing_when_model_weights_fail_to_load(self) -> None:
        """权重下载失败/缓存损坏也是引擎侧问题 —— 换一份音频回来照样装不上。"""
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine(construct_error=OSError("unable to open file")):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["pass"], "engine_missing")
        self.assertEqual(res["reason"], "asr_model_load_failed")

    async def test_should_not_leak_the_engine_exception_text_into_the_result(self) -> None:
        from utils.media import transcribe_audio_enhanced

        secret = "CT2 kernel not supported on this device"
        with _wav_fixture() as wav, _stub_engine([[]], transcribe_error=RuntimeError(secret)):
            res = await transcribe_audio_enhanced(wav, language="zh")

        self.assertEqual(res["pass"], "error")
        self.assertEqual(res["reason"], "transcribe_all_passes_failed")
        self.assertNotIn(secret, str(res))

    async def test_should_report_timeout_when_transcription_outruns_the_budget(self) -> None:
        from utils.media import transcribe_audio_enhanced

        with _wav_fixture() as wav, _stub_engine([[_segment("慢")]], transcribe_delay=0.5):
            res = await transcribe_audio_enhanced(wav, language="zh", timeout=0.02)

        self.assertEqual(res["pass"], "timeout")
        self.assertEqual(res["reason"], "transcribe_timeout")

    async def test_should_report_missing_file_without_touching_the_engine(self) -> None:
        from utils.media import transcribe_audio_enhanced

        with _stub_engine([[_segment("不该跑到这")]]):
            res = await transcribe_audio_enhanced(Path("/nonexistent/none.wav"), language="zh")
            self.assertEqual(_StubWhisperModel.instances, [])

        self.assertEqual(res["pass"], "none")
        self.assertEqual(res["reason"], "audio_file_missing")


class AnalyzeVoiceCallerContractTests(unittest.IsolatedAsyncioTestCase):
    """跨过 utils.media 的 mock，让 analyze_voice 真的走一遍转写函数。"""

    def _record_context(self, path: str) -> dict:
        return {
            "raw_segments": [
                {"type": "record", "data": {"file": "voice.amr", "path": path, "url": path}}
            ],
            "reply_media_segments": [],
            "api_call": None,
            "trace_id": "000001-11-asrtrace",
        }

    async def test_should_tell_the_user_what_was_said_when_the_engine_is_installed(self) -> None:
        from core.agent_tools import _handle_analyze_voice

        with tempfile.TemporaryDirectory() as tmpdir:
            voice = Path(tmpdir) / "voice.amr"
            voice.write_bytes(b"#!AMR\n" + b"\x00" * 64)
            wav = Path(tmpdir) / "voice.wav"
            wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
            with (
                _stub_engine([[_segment("消瘦了 咱弟妹少女")]]),
                patch(
                    "core.agent_tools_media._voice_bytes_to_wav",
                    new=lambda _p: _completed((str(wav), "")),
                ),
            ):
                result = await _handle_analyze_voice({}, self._record_context(str(voice)))

        self.assertTrue(result.ok, result.display)
        self.assertEqual(result.data.get("text"), "消瘦了 咱弟妹少女")
        self.assertIn("消瘦了", result.display)
        # 旧路径恒 ImportError，用户只会收到「这条语音我这边暂时听不了」。
        self.assertNotIn("听不了", result.display)


async def _identity(value):
    return value


def _completed(value):
    """给 patch 用的极简 async 替身：调用即得一个已算好结果的协程。"""
    return _identity(value)


@unittest.skipUnless(
    os.environ.get("YUKIKO_ASR_E2E"),
    "真跑要装载权重（首次下载实测 386.8s），置 YUKIKO_ASR_E2E=1 才跑",
)
class RealEngineEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_should_transcribe_a_real_qq_voice_file_into_simplified_chinese(self) -> None:
        from utils.media import decode_silk_to_wav, transcribe_audio_enhanced

        samples = sorted(Path("storage/cache/voice").glob("*"))
        sample = next((p for p in samples if p.is_file() and p.stat().st_size > 1024), None)
        if sample is None:
            self.skipTest("storage/cache/voice 下没有可用语音样本")

        with tempfile.TemporaryDirectory() as tmpdir:
            wav, reason = await decode_silk_to_wav(sample, Path(tmpdir) / "e2e.wav")
            self.assertIsNotNone(wav, f"SILK 解码失败：{reason}")
            res = await transcribe_audio_enhanced(wav, language="zh", timeout=900.0)

        self.assertTrue(res["text"], res)
        self.assertNotEqual(res["pass"], "engine_missing")
        # 繁体常用字不该出现在最终文本里。
        for traditional in ("飯", "須", "當然", "聰明"):
            self.assertNotIn(traditional, res["text"])


if __name__ == "__main__":
    unittest.main()
