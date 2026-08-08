"""D4 垂直切片回归：agent.run 提取的纯逻辑方法。

锁五件事（全部只读 self / 纯计算，与原内联逻辑等价）：
1. `_should_block_group_admin_tool` —— 群管理工具权限门（含自助撤回例外）。
2. `_collect_media_candidates` —— final_answer 媒体候选组装。
3. `_collect_out_of_chain_media` —— 不在工具链 / 用户原始消息里的媒体候选收集。
4. `_is_loop_guard_blocking_level` —— loop_guard critical/circuit 分类。
5. `_is_loop_guard_warn_level` —— loop_guard warn 分类。

判据落在真实调用上。
"""

from __future__ import annotations

import unittest

from core.agent import AgentContext, AgentLoop


def _ctx(
    *,
    user_id: str = "10001",
    message_text: str = "",
    reply_to_text: str = "",
    raw_segments: list[dict] | None = None,
    reply_media_segments: list[dict] | None = None,
) -> AgentContext:
    return AgentContext(
        conversation_id="group:1",
        user_id=user_id,
        user_name="测试用户",
        group_id=1,
        bot_id="yukiko",
        is_private=False,
        mentioned=False,
        message_text=message_text,
        reply_to_text=reply_to_text,
        raw_segments=raw_segments or [],
        reply_media_segments=reply_media_segments or [],
    )


def _loop(*, group_admin_tools: set[str] | None = None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._group_admin_tools = group_admin_tools or {"set_group_ban", "delete_message"}
    return loop


class ShouldBlockGroupAdminToolTests(unittest.TestCase):
    """权限门：普通用户调群管理工具 → 拦，除非命中自助撤回例外。"""

    def test_non_group_admin_tool_never_blocked(self) -> None:
        loop = _loop()
        self.assertFalse(loop._should_block_group_admin_tool(_ctx(), "search", {}, "user"))

    def test_group_admin_permission_passes(self) -> None:
        loop = _loop()
        self.assertFalse(loop._should_block_group_admin_tool(_ctx(), "set_group_ban", {}, "group_admin"))

    def test_super_admin_permission_passes(self) -> None:
        loop = _loop()
        self.assertFalse(loop._should_block_group_admin_tool(_ctx(), "set_group_ban", {}, "super_admin"))

    def test_regular_user_group_admin_tool_blocked(self) -> None:
        loop = _loop()
        self.assertTrue(loop._should_block_group_admin_tool(_ctx(), "set_group_ban", {"user_id": "888"}, "user"))

    def test_delete_message_is_self_ban_exception(self) -> None:
        # 自助撤回例外：普通用户可撤回机器人自己发的消息。
        loop = _loop()
        self.assertFalse(loop._should_block_group_admin_tool(_ctx(), "delete_message", {}, "user"))

    def test_self_ban_targeting_self_passes(self) -> None:
        loop = _loop()
        self.assertFalse(
            loop._should_block_group_admin_tool(_ctx(user_id="10001"), "set_group_ban", {"user_id": "10001"}, "user")
        )

    def test_self_ban_targeting_other_blocked(self) -> None:
        loop = _loop()
        self.assertTrue(
            loop._should_block_group_admin_tool(_ctx(user_id="10001"), "set_group_ban", {"user_id": "99999"}, "user")
        )

    def test_self_ban_no_target_treated_as_self(self) -> None:
        loop = _loop()
        self.assertFalse(loop._should_block_group_admin_tool(_ctx(user_id="10001"), "set_group_ban", {}, "user"))


class CollectMediaCandidatesTests(unittest.TestCase):
    """final_answer 媒体候选组装：去空 + 归一化，image_url 保持最前。"""

    def test_assembles_all_fields_with_image_url_first(self) -> None:
        loop = _loop()
        result = loop._collect_media_candidates(
            "https://a.example/1.jpg",
            ["https://b.example/2.jpg", "https://c.example/3.jpg"],
            "https://v.example/v.mp4",
            "https://a.example/audio.mp3",
        )
        self.assertEqual(
            result,
            [
                "https://a.example/1.jpg",
                "https://b.example/2.jpg",
                "https://c.example/3.jpg",
                "https://v.example/v.mp4",
                "https://a.example/audio.mp3",
            ],
        )

    def test_drops_blank_and_whitespace_entries(self) -> None:
        loop = _loop()
        result = loop._collect_media_candidates("", ["  ", "https://b.example/2.jpg"], "", "  ")
        self.assertEqual(result, ["https://b.example/2.jpg"])

    def test_empty_when_nothing_present(self) -> None:
        loop = _loop()
        self.assertEqual(loop._collect_media_candidates("", [], "", ""), [])


class CollectOutOfChainMediaTests(unittest.TestCase):
    """不在本回合工具链 / 用户原始消息里的媒体候选收集。"""

    def test_known_url_from_user_message_is_in_chain(self) -> None:
        loop = _loop()
        ctx = _ctx(message_text="请看这个 https://a.example/1.jpg")
        out = loop._collect_out_of_chain_media(["https://a.example/1.jpg"], [], ctx)
        self.assertEqual(out, [])

    def test_unknown_url_is_out_of_chain(self) -> None:
        loop = _loop()
        ctx = _ctx(message_text="没有链接")
        out = loop._collect_out_of_chain_media(["https://evil.example/x.jpg"], [], ctx)
        self.assertEqual(out, ["https://evil.example/x.jpg"])

    def test_url_from_tool_step_data_is_in_chain(self) -> None:
        loop = _loop()
        ctx = _ctx()
        steps = [
            {
                "tool": "parse_video",
                "ok": True,
                "data": {"video_url": "https://v.example/v.mp4"},
            }
        ]
        out = loop._collect_out_of_chain_media(["https://v.example/v.mp4"], steps, ctx)
        self.assertEqual(out, [])

    def test_unknown_local_path_is_out_of_chain(self) -> None:
        loop = _loop()
        ctx = _ctx()
        out = loop._collect_out_of_chain_media(["/tmp/nonexistent/x.jpg"], [], ctx)
        self.assertEqual(out, ["/tmp/nonexistent/x.jpg"])

    def test_known_local_path_from_tool_step_is_in_chain(self) -> None:
        loop = _loop()
        ctx = _ctx()
        steps = [
            {
                "tool": "download_file",
                "ok": True,
                "data": {"local_file": "/storage/media/ok.jpg"},
            }
        ]
        out = loop._collect_out_of_chain_media(["/storage/media/ok.jpg"], steps, ctx)
        self.assertEqual(out, [])

    def test_in_chain_duplicate_is_not_out_of_chain(self) -> None:
        # 重复的 in-chain URL 也认在链内，不进 out_of_chain。
        loop = _loop()
        ctx = _ctx(message_text="https://a.example/1.jpg")
        candidates = [
            "https://a.example/1.jpg",  # in-chain
            "https://evil.example/x.jpg",  # out-of-chain
            "https://a.example/1.jpg",  # duplicate of in-chain
        ]
        out = loop._collect_out_of_chain_media(candidates, [], ctx)
        self.assertEqual(out, ["https://evil.example/x.jpg"])

    def test_duplicate_out_of_chain_entries_are_preserved(self) -> None:
        # 保留顺序与重复项：下游日志长度依赖它们（与原内联逻辑一致）。
        loop = _loop()
        ctx = _ctx(message_text="无链接")
        candidates = [
            "https://evil.example/x.jpg",
            "https://evil.example/x.jpg",
        ]
        out = loop._collect_out_of_chain_media(candidates, [], ctx)
        self.assertEqual(
            out,
            ["https://evil.example/x.jpg", "https://evil.example/x.jpg"],
        )


class LoopGuardLevelClassificationTests(unittest.TestCase):
    """veto_if_looping 返回 level 的三分类。"""

    def test_blocking_levels(self) -> None:
        loop = _loop()
        self.assertTrue(loop._is_loop_guard_blocking_level("critical"))
        self.assertTrue(loop._is_loop_guard_blocking_level("circuit"))
        self.assertFalse(loop._is_loop_guard_blocking_level("warn"))
        self.assertFalse(loop._is_loop_guard_blocking_level("none"))

    def test_warn_level(self) -> None:
        loop = _loop()
        self.assertTrue(loop._is_loop_guard_warn_level("warn"))
        self.assertFalse(loop._is_loop_guard_warn_level("critical"))
        self.assertFalse(loop._is_loop_guard_warn_level("circuit"))


if __name__ == "__main__":
    unittest.main()
