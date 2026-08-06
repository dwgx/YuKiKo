"""静音语音不能被幻觉成一整段捏造中文发进群。

这是比「听不了」严重得多的失败模式：它以 ok=True + `【语音内容】` 报出去，
用户看不出是编的。实测（6s 全零静音 WAV，同一份文件跑两次，未开 VAD）：
    run0 -> '你不是说我会上课吗我会上课我会上课…'（109 字）
    run1 -> '我只想要你和我一起去…'（120 字，重复 11 遍）
6s 低噪 -> '这次的节奏,请不吝点赞、订阅、转发、打赏…'（Whisper 训练数据污染的经典形态）

另一条同源问题：Pass-3 给模型喂了 initial_prompt 引导词
（"这是一段日常群聊语音，包含二次元、原神、游戏、技术梗等网络常用语。"），
模型在没有真实语音时会把它原文吐回来。那段字是**仓里代码写的**，
却会以 data.text / display 的身份被当成"用户说的话"发进群 ——
它走的是工具输出，任何 prompt 层禁令都挡不住。

三道防线，本文件逐条锁定：
  1. 所有 pass 都开 vad_filter（非语音段先被切掉，没语音就没 segments）
  2. 复读机判定（去重字符占比 / n-gram 重复次数，纯结构度量，不看内容）
  3. 引导词回声判定（字符集重合比）
"""

from __future__ import annotations

import re
import unittest

# 这些符号故意**不在模块级 import** —— 未修的基线上它们不存在，
# 模块级 import 会让整个文件收集失败（ImportError），
# 连下面那条不依赖新符号的行为级真跑用例都跑不到，红证据就废了。
# 复核明确指出过「基线红是 AttributeError 而非行为断言失败」证据强度不足。


def _guards():
    from utils.media import (
        _GUIDE_ECHO_MIN_OVERLAP_RATIO,
        _HALLUCINATION_MAX_UNIQUE_RATIO,
        _looks_like_repetition_hallucination,
        _transcript_echoes_guide,
    )

    return (
        _looks_like_repetition_hallucination,
        _transcript_echoes_guide,
        _HALLUCINATION_MAX_UNIQUE_RATIO,
        _GUIDE_ECHO_MIN_OVERLAP_RATIO,
    )

_PASS3_GUIDE = "这是一段日常群聊语音，包含二次元、原神、游戏、技术梗等网络常用语。"

# 实测抓到的真实幻觉输出
_REAL_HALLUCINATIONS = (
    "我只想要你和我一起去" * 11,
    "你不是说我会上课吗" + "我会上课" * 22,
    "这次的节奏,请不吝点赞、订阅、转发、打赏、打赏支持明镜与点点栏目的支持明镜与点点栏目的"
    "支持明镜与点点栏目的支持明镜与点点栏目的支持明镜与点点栏目的",
)

# 实测三条真实群语音的转写结果
_REAL_SPEECH = (
    "吃米饭必须配西瓜,西瓜都得抛饭吃",
    "消瘦了 咱弟妹少女",
    "当然他马上比神仙还聪明他直接算命你知道吗他算到你明天会有一个屁股降临",
)


class RepetitionHallucinationTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.is_hallucination,
            self.echoes_guide,
            self.max_unique_ratio,
            self.min_overlap_ratio,
        ) = _guards()

    def test_should_flag_the_real_hallucinations_captured_from_silence(self) -> None:
        for text in _REAL_HALLUCINATIONS:
            with self.subTest(text[:24]):
                self.assertTrue(
                    self.is_hallucination(text),
                    f"这段是实测从静音输入里抓到的幻觉，必须拦: {text[:40]}",
                )

    def test_should_not_flag_real_speech(self) -> None:
        """误伤真人说话比漏掉幻觉更糟 —— 那会让能听懂的语音也变成听不了。"""

        for text in _REAL_SPEECH:
            with self.subTest(text[:24]):
                self.assertFalse(
                    self.is_hallucination(text),
                    f"实测真实群语音被误判成幻觉: {text}",
                )

    def test_should_leave_short_text_alone(self) -> None:
        """短句样本太少，判不了 —— 宁可放过，别误伤"嗯""好的"这类。"""

        for text in ("嗯", "好的", "知道了", "哈哈哈哈", "在的在的"):
            with self.subTest(text):
                self.assertFalse(self.is_hallucination(text))

    def test_should_flag_low_unique_character_ratio(self) -> None:
        text = "啊" * 40
        stripped = re.sub(r"\s+", "", text)
        ratio = len(set(stripped)) / len(stripped)
        self.assertLessEqual(ratio, self.max_unique_ratio)
        self.assertTrue(self.is_hallucination(text))

    def test_should_flag_a_repeated_ngram_even_when_characters_are_varied(self) -> None:
        """去重占比不低、但某个短语反复出现 —— 复读机的另一种形态。"""

        text = "今天天气真不错" + "我们一起去公园" * 5
        self.assertTrue(self.is_hallucination(text))

    def test_empty_and_whitespace_are_not_hallucinations(self) -> None:
        for text in ("", "   ", "\n\n", None):
            with self.subTest(repr(text)):
                self.assertFalse(self.is_hallucination(text))


class GuideEchoTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.is_hallucination,
            self.echoes_guide,
            self.max_unique_ratio,
            self.min_overlap_ratio,
        ) = _guards()

    def test_should_flag_the_pass3_guide_echoed_back(self) -> None:
        echoed = _PASS3_GUIDE * 4
        self.assertTrue(
            self.echoes_guide(echoed, _PASS3_GUIDE),
            "引导词被原文抄回来必须拦 —— 那是代码写的字，不是用户说的话",
        )

    def test_should_flag_a_partially_mangled_echo(self) -> None:
        """模型会改标点、丢字，所以判定不能靠字面相等。"""

        mangled = "这是一段日常群聊语音 包含二次元 原神 游戏 技术梗等网络常用语"
        self.assertTrue(self.echoes_guide(mangled, _PASS3_GUIDE))

    def test_should_not_flag_real_speech_against_the_guide(self) -> None:
        for text in _REAL_SPEECH:
            with self.subTest(text[:20]):
                self.assertFalse(
                    self.echoes_guide(text, _PASS3_GUIDE),
                    f"真实语音被当成引导词回声: {text}",
                )

    def test_should_not_flag_when_there_is_no_guide(self) -> None:
        """Pass-1 / Pass-2 不传 initial_prompt，那条判定不该介入。"""

        for guide in ("", None, "短"):
            with self.subTest(repr(guide)):
                self.assertFalse(self.echoes_guide(_REAL_SPEECH[0], guide))

    def test_threshold_is_a_ratio_between_zero_and_one(self) -> None:
        self.assertGreater(self.min_overlap_ratio, 0.0)
        self.assertLess(self.min_overlap_ratio, 1.0)


@unittest.skipUnless(
    __import__("os").environ.get("YUKIKO_ASR_E2E"),
    "真跑用例：需要 faster-whisper 权重（本机 HF 缓存已有 small），设 YUKIKO_ASR_E2E=1 启用",
)
class RealEngineSilenceTests(unittest.TestCase):
    """行为级证据：真的跑一遍引擎，静音必须出空文本。

    上面那些单测在基线上是 ImportError（函数不存在），证据强度弱。
    这一条不依赖新符号，纯粹断言"静音进去、空字出来"，
    所以在未修的基线上会因为幻觉出文本而真红。
    实测未修基线：6s 全零静音 -> '我只想要你和我一起去'×11（120 字）。
    """

    def _silent_wav(self, tmp, seconds: float = 6.0):
        import wave
        from pathlib import Path

        path = Path(tmp) / "silence.wav"
        with wave.open(str(path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(16000)
            fh.writeframes(b"\x00\x00" * int(seconds * 16000))
        return path

    def test_silence_yields_no_transcript(self) -> None:
        import asyncio
        from tempfile import TemporaryDirectory

        from utils.media import transcribe_audio_enhanced

        with TemporaryDirectory() as tmp:
            wav = self._silent_wav(tmp)
            result = asyncio.run(transcribe_audio_enhanced(wav, language="zh"))
        text = str(result.get("text", "") or "")
        self.assertEqual(
            text, "", f"静音被幻觉成了文字，这段会以【语音内容】进群: {text[:80]!r}"
        )

    def test_real_speech_still_transcribes(self) -> None:
        """反向保护：拦幻觉不能把能听懂的语音也拦掉。"""

        import asyncio
        import glob
        from pathlib import Path
        from tempfile import TemporaryDirectory

        samples = sorted(glob.glob("storage/cache/voice/*.mp3"))[:1]
        if not samples:
            self.skipTest("storage/cache/voice 下没有真实语音样本")

        import pilk

        from utils.media import transcribe_audio_enhanced

        with TemporaryDirectory() as tmp:
            wav = Path(tmp) / "real.wav"
            pilk.silk_to_wav(samples[0], str(wav), rate=16000)
            result = asyncio.run(transcribe_audio_enhanced(wav, language="zh"))
        self.assertTrue(
            str(result.get("text", "") or "").strip(),
            "真实语音转不出文字了 —— 幻觉守卫误伤",
        )


class VadFilterIsAlwaysOnTests(unittest.TestCase):
    def test_every_transcribe_call_enables_vad_filter(self) -> None:
        """三个 pass 都必须开 VAD。漏一个，那个 pass 就会在静音上产幻觉，
        而多 pass 计分会挑分最高的那个 —— 幻觉往往分还不低。"""

        import asyncio
        import wave
        from pathlib import Path
        from tempfile import TemporaryDirectory

        import utils.media as media

        seen: list[dict] = []

        class _Info:
            language = "zh"
            language_probability = 1.0
            duration = 1.0

        class _StubModel:
            def transcribe(self, path, **kwargs):
                seen.append(kwargs)
                return iter(()), _Info()

        # 模型缓存在 _whisper_models[(size, device, compute_type)]，
        # 预先塞替身就不会真去加载权重（那会下载几百 MB）。
        spec = (
            media._ASR_DEFAULT_MODEL_SIZE,
            media._ASR_DEFAULT_DEVICE,
            media._ASR_DEFAULT_COMPUTE_TYPE,
        )
        saved = media._whisper_models.get(spec)
        media._whisper_models[spec] = _StubModel()
        try:
            with TemporaryDirectory() as tmp:
                wav = Path(tmp) / "a.wav"
                with wave.open(str(wav), "wb") as fh:
                    fh.setnchannels(1)
                    fh.setsampwidth(2)
                    fh.setframerate(16000)
                    fh.writeframes(b"\x00\x00" * 16000)
                asyncio.run(media.transcribe_audio_enhanced(wav, language="zh"))
        finally:
            if saved is None:
                media._whisper_models.pop(spec, None)
            else:
                media._whisper_models[spec] = saved

        self.assertTrue(seen, "一个 pass 都没跑到，测试构造有问题")
        self.assertGreaterEqual(len(seen), 3, "三个 pass 都该被检查到")
        for kwargs in seen:
            self.assertTrue(
                kwargs.get("vad_filter"),
                f"这一趟没开 vad_filter: {sorted(kwargs)}",
            )


if __name__ == "__main__":
    unittest.main()
