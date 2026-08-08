"""fan out review 修复的回归测试。

覆盖 review subagents 发现并修复的问题：
1. super_admin 清单双源漂移（agent.py 改从 registry 同源）。
2. delete_message 普通用户自助撤回加跨群校验（防猜 message_id 跨群删）。
3. qzone QZoneMood 新增 forward_count 字段 + like_count 读真实点赞。
4. silk 编码用 pilk 正确 tencent 签名（encode(pcm, silk, 24000, 24000, True)）。
"""
from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import _silk_encode_for_record_sync
from core.agent import AgentLoop
from core.agent_tools_napcat import _handle_delete_message
from core.agent_tools_registry import AgentToolRegistry

_FFMPEG = shutil.which("ffmpeg")


def _run(coro):
    return asyncio.run(coro)


class SuperAdminSyncTests(unittest.TestCase):
    """agent.py 的 _super_admin_tools 必须与 registry 同源。"""

    def test_super_admin_tools_synced_from_registry(self) -> None:
        src = inspect.getsource(AgentLoop.__init__)
        self.assertIn(
            "self._super_admin_tools = set(AgentToolRegistry._SUPER_ADMIN_TOOLS)",
            src,
            "agent.py 的 super_admin 清单应改从 registry 同源，不能再手维护",
        )

    def test_registry_super_admin_contains_recent_additions(self) -> None:
        for tool in ("set_qq_profile", "get_qzone_profile", "get_qzone_photos"):
            self.assertIn(tool, AgentToolRegistry._SUPER_ADMIN_TOOLS)


class DeleteMessageCrossGroupTests(unittest.TestCase):
    """普通用户自助撤回必须校验消息所在群 == 当前会话群。"""

    def test_cross_group_self_recall_rejected(self) -> None:
        calls: list[str] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append(api)
            if api == "get_msg":
                return {"data": {"message_id": kwargs.get("message_id"), "sender": {"user_id": 100}, "group_id": 999}}
            return {}

        context = {
            "api_call": fake_api_call,
            "permission_level": "user",
            "bot_id": "100",
            "group_id": "1",
        }
        result = _run(_handle_delete_message({"message_id": 123}, context))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:cross_group")
        self.assertNotIn("delete_msg", calls)

    def test_same_group_self_recall_allowed(self) -> None:
        calls: list[str] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append(api)
            if api == "get_msg":
                return {"data": {"message_id": kwargs.get("message_id"), "sender": {"user_id": 100}, "group_id": 1}}
            return {}

        context = {
            "api_call": fake_api_call,
            "permission_level": "user",
            "bot_id": "100",
            "group_id": "1",
        }
        result = _run(_handle_delete_message({"message_id": 123}, context))
        self.assertTrue(result.ok)
        self.assertIn("delete_msg", calls)


class QZoneMoodPayloadTests(unittest.TestCase):
    """QZoneMood 的点赞/转发字段语义正确。"""

    def test_payload_includes_forward_count(self) -> None:
        from core.agent_tools_web import _qzone_mood_payload
        from core.qzone import QZoneMood

        mood = QZoneMood(tid="1", content="x", comment_count=3, like_count=5, forward_count=2)
        payload = _qzone_mood_payload(mood)
        self.assertEqual(payload["like_count"], 5)
        self.assertEqual(payload["forward_count"], 2)


class SilkTencentHeaderTests(unittest.TestCase):
    """silk 编码必须产出 tencent 头（0x02），否则 QQ 端播放质量低。"""

    def _make_mp3(self, path: Path) -> None:
        if not _FFMPEG:
            self.skipTest("ffmpeg not available")
        subprocess.run(
            [
                _FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-ac", "1", "-ar", "24000", "-b:a", "64k", str(path),
            ],
            capture_output=True,
            check=True,
        )

    def test_encoded_silk_has_tencent_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "sine.mp3"
            self._make_mp3(mp3)
            out = _silk_encode_for_record_sync(mp3, 60)
            self.assertIsNotNone(out, "mp3 应成功转成 silk")
            head = out.read_bytes()[:1]
            self.assertEqual(head, b"\x02", "silk 应为 tencent 版本头（0x02）")


class PlatformMediaSendTests(unittest.TestCase):
    """run_primary 平台路径的媒体发送（_resolve_record_ref）。"""

    def test_record_b64_becomes_base64_ref(self) -> None:
        from core.engine_types import EngineResponse
        from core.platform.run_primary import _resolve_record_ref

        resp = EngineResponse(action="reply", reason="test", record_b64="c2lsaw==")
        ref = _run(_resolve_record_ref(resp, 60))
        self.assertEqual(ref, "base64://c2lsaw==")

    def test_audio_file_converted_to_silk_ref(self) -> None:
        from core.engine_types import EngineResponse
        from core.platform.run_primary import _resolve_record_ref

        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "sine.mp3"
            if _FFMPEG:
                subprocess.run(
                    [
                        _FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ac", "1", "-ar", "24000", "-b:a", "64k", str(mp3),
                    ],
                    capture_output=True,
                    check=True,
                )
            else:
                mp3.write_bytes(b"\xff" * 2048)  # 无效 mp3：silk 转换失败 → 回退空
            resp = EngineResponse(action="reply", reason="test", audio_file=str(mp3))
            ref = _run(_resolve_record_ref(resp, 60))
            if _FFMPEG:
                self.assertTrue(ref.startswith("file://"), f"应产出 file:// silk 引用，实际 {ref!r}")
                self.assertIn(".silk", ref)
            else:
                self.assertEqual(ref, "")

    def test_no_audio_returns_empty(self) -> None:
        from core.engine_types import EngineResponse
        from core.platform.run_primary import _resolve_record_ref

        resp = EngineResponse(action="reply", reason="test", reply_text="hi")
        self.assertEqual(_run(_resolve_record_ref(resp, 60)), "")

    def test_voice_max_seconds_reads_config(self) -> None:
        from core.platform.run_primary import _platform_voice_max_seconds

        class _E:
            config = {"bot": {"voice_send_max_seconds": 45}}

        self.assertEqual(_platform_voice_max_seconds(_E()), 45)


if __name__ == "__main__":
    unittest.main()
