"""点歌语音无法播放修复的回归测试。

根因：QQ 语音消息必须是 silk 编码，且单条限 60s。app.py 原来把 mp3
直接作为 record 发送，NapCat 不保证自动转码，导致点歌语音点开无法播放。

修复：`_silk_encode_for_record_sync` 在发送前把源音频转成 silk 并截断，
覆盖完整直发 / 分段 / 兜底三个发送点。这里验证转换 helper 的行为。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import _silk_encode_for_record, _silk_encode_for_record_sync

_FFMPEG = shutil.which("ffmpeg")


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


if __name__ == "__main__":
    unittest.main()
