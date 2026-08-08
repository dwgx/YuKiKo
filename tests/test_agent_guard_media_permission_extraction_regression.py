"""E2：run 低风险提取回归测试。

锁三组从 `AgentLoop.run` 抽出的方法，保证提取前后行为等价：
1. `_record_guard_block` —— 守卫拦截统一落账（失败步 + 回馈 payload）。
2. `_check_permission_gate` —— 三级权限门（super_admin / group_admin / 点名）。
3. `_resolve_final_answer_media` —— final_answer 媒体规范化（合并 / 兜底 / silk 覆盖）。
"""
from __future__ import annotations

import unittest

from core.agent import AgentContext, AgentLoop


def _make_ctx(**overrides: object) -> AgentContext:
    fields = {
        "conversation_id": "group:1:user:10001",
        "user_id": "10001",
        "user_name": "测试",
        "group_id": 1,
        "bot_id": "99999",
        "is_private": False,
        "mentioned": False,
        "message_text": "测试消息",
        "trace_id": "t-e2",
    }
    fields.update(overrides)
    return AgentContext(**fields)  # type: ignore[arg-type]


class RecordGuardBlockTests(unittest.TestCase):
    """_record_guard_block：失败步落账 + 回馈 payload 与原内联组合等价。"""

    @staticmethod
    def _loop() -> AgentLoop:
        return AgentLoop.__new__(AgentLoop)

    def test_appends_failed_step_and_returns_payload(self) -> None:
        loop = self._loop()
        steps: list[dict] = []
        payload = loop._record_guard_block(
            ctx=_make_ctx(),
            steps=steps,
            step_idx=3,
            tool_name="search",
            error_tag="consecutive_crashes_guard",
            reason_key="consecutive_crashes_guard",
            reason_text="该工具已连续崩溃或报错，底层拒绝执行，不要再调用它。",
        )
        self.assertEqual(
            steps,
            [
                {
                    "step": 3,
                    "tool": "search",
                    "ok": False,
                    "error": "consecutive_crashes_guard",
                }
            ],
        )
        self.assertEqual(payload["tool"], "search")
        self.assertIs(payload["ok"], False)
        self.assertEqual(
            payload["error"], "该工具已连续崩溃或报错，底层拒绝执行，不要再调用它。"
        )

    def test_matches_inline_composition(self) -> None:
        """抽取后必须与「手工 append + _build_guard_feedback_payload」逐键等价。"""
        loop = self._loop()
        steps_inline: list[dict] = [
            {"step": 0, "tool": "search", "ok": True, "data": {"image_url": "http://x/1.png"}}
        ]
        steps_extracted: list[dict] = list(steps_inline)
        reason_text = "这个外部查询之前已经成功执行过，请基于已有结果继续。"

        # 内联写法（提取前的样子）
        steps_inline.append(
            {
                "step": 5,
                "tool": "search",
                "ok": False,
                "error": "duplicate_external_fact_query",
            }
        )
        payload_inline = loop._build_guard_feedback_payload(
            tool_name="search",
            steps=steps_inline,
            reason_key="duplicate_external_fact_query",
            reason_text=reason_text,
        )
        # 抽取后的写法
        payload_extracted = loop._record_guard_block(
            ctx=_make_ctx(),
            steps=steps_extracted,
            step_idx=5,
            tool_name="search",
            error_tag="duplicate_external_fact_query",
            reason_key="duplicate_external_fact_query",
            reason_text=reason_text,
        )
        self.assertEqual(steps_inline, steps_extracted)
        self.assertEqual(payload_inline, payload_extracted)

    def test_already_obtained_surfaces_prior_success(self) -> None:
        loop = self._loop()
        steps: list[dict] = [
            {
                "step": 0,
                "tool": "search",
                "ok": True,
                "display": "找到了结果",
                "data": {"image_urls": ["http://x/1.png", "http://x/2.png"]},
            }
        ]
        payload = loop._record_guard_block(
            ctx=_make_ctx(),
            steps=steps,
            step_idx=1,
            tool_name="search",
            error_tag="repeated_tool_call:2",
            reason_key="repeated_tool_call",
            reason_text="同一工具和参数重复过多，请换工具策略或直接 final_answer。",
        )
        self.assertIn("already_obtained", payload)
        self.assertEqual(payload["already_obtained"]["image_urls"][:3], ["http://x/1.png", "http://x/2.png"])

    def test_does_not_mutate_messages_or_log(self) -> None:
        """提取只落 steps，回喂 / 日志 / 熔断仍归 run 控制。"""
        loop = self._loop()
        steps: list[dict] = []
        payload = loop._record_guard_block(
            ctx=_make_ctx(),
            steps=steps,
            step_idx=0,
            tool_name="think",
            error_tag="loop_guard:warn:2",
            reason_key="loop_guard_loop",
            reason_text="检测到同一工具和参数连续空转多次，结果没有进展。",
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(payload["tool"], "think")


class PermissionGateTests(unittest.TestCase):
    """_check_permission_gate：三级权限门，None=放行，字符串=拦截原因。"""

    @staticmethod
    def _loop() -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        loop._super_admin_tools = {"ban_all"}
        loop._group_admin_tools = {"mute_user", "delete_message"}
        return loop

    def test_super_admin_tool_blocks_non_super_admin(self) -> None:
        loop = self._loop()
        reason = loop._check_permission_gate(
            _make_ctx(), "ban_all", {}, "user"
        )
        self.assertEqual(reason, "need_super_admin")
        reason = loop._check_permission_gate(
            _make_ctx(), "ban_all", {}, "group_admin"
        )
        self.assertEqual(reason, "need_super_admin")

    def test_super_admin_tool_passes_for_super_admin(self) -> None:
        loop = self._loop()
        reason = loop._check_permission_gate(
            _make_ctx(), "ban_all", {}, "super_admin"
        )
        self.assertIsNone(reason)

    def test_group_admin_tool_blocks_plain_user(self) -> None:
        loop = self._loop()
        reason = loop._check_permission_gate(
            _make_ctx(), "mute_user", {"user_id": "20002"}, "user"
        )
        self.assertEqual(reason, "need_group_admin")

    def test_group_admin_tool_passes_for_group_admin_when_addressed(self) -> None:
        loop = self._loop()
        ctx = _make_ctx(mentioned=True)
        self.assertIsNone(loop._check_permission_gate(ctx, "mute_user", {}, "group_admin"))

    def test_group_admin_tool_requires_explicit_address_for_group_admin(self) -> None:
        loop = self._loop()
        ctx = _make_ctx(mentioned=False)
        reason = loop._check_permission_gate(ctx, "mute_user", {}, "group_admin")
        self.assertEqual(reason, "explicit_bot_address_required")

    def test_delete_message_exempt_from_address_requirement(self) -> None:
        loop = self._loop()
        ctx = _make_ctx(mentioned=False)
        self.assertIsNone(loop._check_permission_gate(ctx, "delete_message", {}, "group_admin"))

    def test_super_admin_still_requires_explicit_address_for_group_tool(self) -> None:
        # 原内联代码的第三级检查（点名要求）对 super_admin 没有豁免：
        # 提取必须逐字保留这个既有行为。
        loop = self._loop()
        ctx = _make_ctx(mentioned=False)
        self.assertEqual(
            loop._check_permission_gate(ctx, "mute_user", {}, "super_admin"),
            "explicit_bot_address_required",
        )

    def test_regular_tool_passes_for_any_level(self) -> None:
        loop = self._loop()
        for perm_level in ("super_admin", "group_admin", "user"):
            self.assertIsNone(loop._check_permission_gate(_make_ctx(), "search", {}, perm_level))


class ResolveFinalAnswerMediaTests(unittest.TestCase):
    """_resolve_final_answer_media：final_answer 媒体规范化。"""

    @staticmethod
    def _loop() -> AgentLoop:
        return AgentLoop.__new__(AgentLoop)

    def test_merges_image_url_into_image_urls_front(self) -> None:
        loop = self._loop()
        media = loop._resolve_final_answer_media(
            {"text": " 看这张图 ", "image_url": "http://x/a.png", "image_urls": ["http://x/b.png", "http://x/c.png"]},
            [],
            _make_ctx(),
            0,
        )
        self.assertEqual(media["text"], "看这张图")
        self.assertEqual(media["image_url"], "http://x/a.png")
        self.assertEqual(
            media["image_urls"], ["http://x/a.png", "http://x/b.png", "http://x/c.png"]
        )

    def test_image_url_already_in_image_urls_not_duplicated(self) -> None:
        loop = self._loop()
        media = loop._resolve_final_answer_media(
            {"image_url": "http://x/a.png", "image_urls": ["http://x/a.png", "http://x/b.png"]},
            [],
            _make_ctx(),
            0,
        )
        self.assertEqual(media["image_urls"], ["http://x/a.png", "http://x/b.png"])

    def test_falls_back_to_last_success_when_empty(self) -> None:
        loop = self._loop()
        steps = [
            {"step": 0, "tool": "search_media", "ok": True, "data": {"image_urls": ["http://x/1.png", "http://x/2.png"]}}
        ]
        media = loop._resolve_final_answer_media({"text": ""}, steps, _make_ctx(), 1)
        self.assertEqual(media["image_url"], "http://x/1.png")
        self.assertEqual(media["image_urls"], ["http://x/1.png", "http://x/2.png"])

    def test_explicit_values_win_over_last_success(self) -> None:
        loop = self._loop()
        steps = [
            {"step": 0, "tool": "search_media", "ok": True, "data": {"image_urls": ["http://old/1.png"]}}
        ]
        media = loop._resolve_final_answer_media(
            {"image_url": "http://new/1.png"}, steps, _make_ctx(), 1
        )
        self.assertEqual(media["image_url"], "http://new/1.png")
        self.assertEqual(media["image_urls"], ["http://new/1.png"])

    def test_silk_overridden_by_last_non_silk(self) -> None:
        loop = self._loop()
        steps = [
            {"step": 0, "tool": "music_search", "ok": True, "data": {"audio_file": "/data/voice.mp3"}}
        ]
        media = loop._resolve_final_answer_media(
            {"audio_file": "/data/voice.silk"}, steps, _make_ctx(), 1
        )
        self.assertEqual(media["audio_file"], "/data/voice.mp3")

    def test_silk_kept_when_no_prior_audio(self) -> None:
        loop = self._loop()
        media = loop._resolve_final_answer_media(
            {"audio_file": "/data/voice.silk"}, [], _make_ctx(), 0
        )
        self.assertEqual(media["audio_file"], "/data/voice.silk")

    def test_audio_and_video_fallback_from_last_success(self) -> None:
        loop = self._loop()
        steps = [
            {"step": 0, "tool": "music_search", "ok": True, "data": {"audio_file": "/data/a.mp3", "video_url": "/tmp/vid.mp4"}}
        ]
        media = loop._resolve_final_answer_media({}, steps, _make_ctx(), 1)
        self.assertEqual(media["audio_file"], "/data/a.mp3")
        self.assertEqual(media["video_url"], "/tmp/vid.mp4")

    def test_local_video_url_sanitizes_contradictory_text(self) -> None:
        loop = self._loop()
        steps = [
            {"step": 0, "tool": "parse_video", "ok": True, "data": {"video_url": "/tmp/vid.mp4"}}
        ]
        media = loop._resolve_final_answer_media(
            {"text": "没法直接发送，只能给你路径：/tmp/vid.mp4"}, steps, _make_ctx(), 1
        )
        self.assertEqual(media["video_url"], "/tmp/vid.mp4")
        self.assertEqual(media["text"], "解析好了，正在投递视频。")

    def test_remote_video_url_leaves_text_untouched(self) -> None:
        loop = self._loop()
        steps = [
            {"step": 0, "tool": "parse_video", "ok": True, "data": {"video_url": "http://cdn/v.mp4"}}
        ]
        media = loop._resolve_final_answer_media(
            {"text": "视频在这里：http://cdn/v.mp4"}, steps, _make_ctx(), 1
        )
        self.assertEqual(media["video_url"], "http://cdn/v.mp4")
        self.assertEqual(media["text"], "视频在这里：http://cdn/v.mp4")

    def test_cover_url_passthrough(self) -> None:
        loop = self._loop()
        media = loop._resolve_final_answer_media(
            {"cover_url": "  http://x/cover.png  "}, [], _make_ctx(), 0
        )
        self.assertEqual(media["cover_url"], "http://x/cover.png")

    def test_non_list_image_urls_tolerated(self) -> None:
        loop = self._loop()
        media = loop._resolve_final_answer_media(
            {"image_urls": "http://x/a.png"}, [], _make_ctx(), 0
        )
        self.assertEqual(media["image_urls"], [])


if __name__ == "__main__":
    unittest.main()
