"""点歌语音无法播放修复的回归测试。

根因：QQ 语音消息必须是 silk 编码，且单条限 60s。app.py 原来把 mp3
直接作为 record 发送，NapCat 不保证自动转码，导致点歌语音点开无法播放。

修复：`_silk_encode_for_record_sync` 在发送前把源音频转成 silk 并截断，
覆盖完整直发 / 分段 / 兜底三个发送点。这里验证转换 helper 的行为。
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import _silk_encode_for_record, _silk_encode_for_record_sync

_FFMPEG = shutil.which("ffmpeg")
_APP_PY = Path(__file__).resolve().parent.parent / "app.py"


class VoiceSilkRegressionTests(unittest.TestCase):
    def _make_mp3(self, path: Path, seconds: int = 2) -> None:
        if not _FFMPEG:
            self.skipTest("ffmpeg not available")
        subprocess.run(
            [
                _FFMPEG, "-y", "-f", "lavfi",
                "-i", f"sine=frequency=440:duration={seconds}",
                "-ac", "1", "-ar", "24000", "-b:a", "64k",
                str(path),
            ],
            capture_output=True,
            check=True,
        )

    def test_existing_valid_silk_reused(self) -> None:
        """不截断时输入 silk 直接复用，不做二次编码。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "demo.silk"
            p.write_bytes(b"\x02" * 500)
            out = _silk_encode_for_record_sync(p, 0)
            self.assertEqual(out, p)

    def test_short_mp3_converted_to_silk(self) -> None:
        """短 mp3 应被转成 QQ 可播放的 silk。"""
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "sine.mp3"
            self._make_mp3(mp3)
            out = _silk_encode_for_record_sync(mp3, 60)
            self.assertIsNotNone(out, "mp3 应成功转成 silk")
            self.assertEqual(out.suffix, ".silk")
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 256)

    def test_missing_file_returns_none(self) -> None:
        """不存在的输入返回 None（调用方回退原文件）。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = _silk_encode_for_record_sync(Path(tmp) / "nope.mp3", 60)
            self.assertIsNone(out)

    def test_none_input_returns_none(self) -> None:
        self.assertIsNone(_silk_encode_for_record_sync(None, 60))

    def test_async_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "demo.silk"
            p.write_bytes(b"\x02" * 500)
            out = asyncio.run(_silk_encode_for_record(p, 0))
            self.assertEqual(out, p)

    def test_concurrent_encode_same_audio_no_collision(self) -> None:
        """同一音频并发编码（不同群同歌）不互相覆盖：都用唯一临时文件 + os.replace 原子落盘。"""
        if not _FFMPEG:
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "sine.mp3"
            self._make_mp3(mp3, seconds=4)
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(_silk_encode_for_record_sync, mp3, 60)
                f2 = pool.submit(_silk_encode_for_record_sync, mp3, 60)
                out1, out2 = f1.result(), f2.result()
            self.assertIsNotNone(out1, "并发编码线程 1 不应失败回退 mp3")
            self.assertIsNotNone(out2, "并发编码线程 2 不应失败回退 mp3")
            # 返回的是稳定 silk 路径（供后续复用），不是临时名。
            self.assertEqual(out1, mp3.with_suffix(".silk"))
            self.assertEqual(out2, mp3.with_suffix(".silk"))
            self.assertTrue(out1.exists())
            self.assertGreater(out1.stat().st_size, 256)

    def test_encode_cleans_up_temp_files(self) -> None:
        """编码后不残留 .silk_src.pcm / 点号开头临时 silk（finally + os.replace）。"""
        if not _FFMPEG:
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "sine.mp3"
            self._make_mp3(mp3)
            out = _silk_encode_for_record_sync(mp3, 60)
            self.assertIsNotNone(out)
            leftover_pcm = list(Path(tmp).glob("*.silk_src.pcm"))
            self.assertEqual(leftover_pcm, [], "pcm 临时文件应在 finally 清理")
            leftover_tmp_silk = list(Path(tmp).glob(".*.silk"))
            self.assertEqual(leftover_tmp_silk, [], "silk 临时文件应被 os.replace 或清理")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["sine.mp3", "sine.silk"])

    def test_returns_stable_silk_path_for_reuse(self) -> None:
        """原子落盘后返回稳定 path（audio.with_suffix('.silk')），music 层复用不落临时名。"""
        if not _FFMPEG:
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "sine.mp3"
            self._make_mp3(mp3)
            out = _silk_encode_for_record_sync(mp3, 60)
            self.assertEqual(out, mp3.with_suffix(".silk"))


class VoiceSendSilkTimingRegressionTests(unittest.TestCase):
    """锁语音发送优化（review Medium）：

    b. silk/pcm 用临时文件 + os.replace 原子落盘，避免同音频并发互相覆盖。

    （优化 a —— 完整文件转 silk 只在"将直发"分支内执行——随点歌语音编排迁入
    core.response_delivery._send_voice（E4），行为判据见
    tests/test_response_delivery_regression.py::test_long_audio_without_full_first_does_not_pre_encode_source。）
    """

    @staticmethod
    def _app_source() -> str:
        return _APP_PY.read_text(encoding="utf-8")

    def test_silk_encode_uses_unique_temp_names(self) -> None:
        src = self._app_source()
        # 编码函数体内应生成带 uuid 的临时 silk/pcm 名，并用 os.replace 原子落盘。
        self.assertRegex(src, r"tmp_silk\s*=\s*audio_path\.with_name\(f\"\..*\.silk\"\)")
        self.assertRegex(src, r"tmp_pcm\s*=\s*audio_path\.with_name\(f\"\..*\.silk_src\.pcm\"\)")
        self.assertIn("os.replace(tmp_silk, silk_path)", src)
        # 旧固定路径产物应消失。
        self.assertNotIn('with_suffix(".silk_src.pcm")', src)


if __name__ == "__main__":
    unittest.main()
