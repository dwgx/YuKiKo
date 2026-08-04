"""group_ops 审计流埋点（MIGRATION_TODO E3-3）。

这些记录是「自改可审计」的凭据，所以断言的重点不是「写了一条日志」，
而是每条记录都能按字段查询：改了什么 / 哪个群 / 目标是谁 / 谁改的 / 改完什么状态 /
是否不可逆。断言字段而不是断言文案，就是为了让契约不依赖提示语措辞。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.admin import (
    AUDIT_BEHAVIOR_MODE_SET,
    AUDIT_BEHAVIOR_PARAM_SET,
    AUDIT_HIGH_RISK_CONFIRM_SET,
    AUDIT_IGNORE_ADD,
    AUDIT_IGNORE_REMOVE,
    AUDIT_SUBJECT_BEHAVIOR,
    AUDIT_SUBJECT_HIGH_RISK,
    AUDIT_SUBJECT_IGNORED,
    AUDIT_SUBJECT_WHITELIST,
    AUDIT_WHITELIST_ADD,
    AUDIT_WHITELIST_REMOVE,
    AdminEngine,
)
from core.audit import STREAM_GROUP_OPS, AuditTrail

_GROUP_ID = 1075046273
_SUPER_USER = "10001"
_GROUP_ADMIN = "20002"
_TARGET_USER = "3862205188"

_CONFIG: dict[str, Any] = {
    "admin": {
        "enable": True,
        "super_users": [_SUPER_USER],
        "whitelist_groups": [_GROUP_ID],
    }
}


class _FakeEngine:
    """行为模式只改内存里的 engine.config，测试只需要这一个面。"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {
            "trigger": {"ai_listen_enable": True},
            "routing": {"min_confidence": 0.58},
        }

    def refresh_runtime_policy_components(self, reason: str = "") -> None:
        return None


class _AuditFixture:
    def __init__(self, tmp: str, *, enable: bool = True, attach: bool = True) -> None:
        self.dir = Path(tmp)
        self.trail = AuditTrail(self.dir / "audit", enable=enable)
        self.admin = AdminEngine(
            config=_CONFIG,
            storage_dir=self.dir,
            audit=self.trail if attach else None,
        )
        self.engine = _FakeEngine()

    def rows(self) -> list[dict[str, Any]]:
        return self.trail.read(STREAM_GROUP_OPS, limit=200)

    def only(self, event: str) -> dict[str, Any]:
        matches = [r for r in self.rows() if r.get("event") == event]
        if len(matches) != 1:
            raise AssertionError(f"expected exactly 1 {event!r} record, got {len(matches)}")
        return matches[0]


class GroupOpsAuditRecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_whitelist_add_records_group_actor_and_resulting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            reply = await fx.admin.handle_command(
                text="/yuki 加白", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            self.assertIn("已加白本群", reply or "")
            row = fx.only(AUDIT_WHITELIST_ADD)
            self.assertEqual(row["subject"], AUDIT_SUBJECT_WHITELIST)
            self.assertEqual(row["group_id"], _GROUP_ID)
            self.assertEqual(row["actor_id"], _SUPER_USER)
            self.assertTrue(row["state"]["whitelisted"])
            self.assertTrue(row["persisted"])
            self.assertFalse(row["irreversible"])
            self.assertIn("ts", row)

    async def test_whitelist_remove_records_before_and_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text="/yuki 拉黑", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            row = fx.only(AUDIT_WHITELIST_REMOVE)
            self.assertEqual(row["change"]["whitelisted"], {"before": True, "after": False})
            self.assertFalse(row["state"]["whitelisted"])
            self.assertEqual(row["state"]["whitelist_size"], 0)

    async def test_ignore_add_records_target_user_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text=f"/yuki 忽略用户 {_TARGET_USER}",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            row = fx.only(AUDIT_IGNORE_ADD)
            self.assertEqual(row["subject"], AUDIT_SUBJECT_IGNORED)
            self.assertEqual(row["target_user_id"], _TARGET_USER)
            self.assertEqual(row["actor_id"], _GROUP_ADMIN)
            self.assertEqual(row["scope"], "group")
            self.assertEqual(row["group_id"], _GROUP_ID)
            self.assertEqual(row["change"]["ignored"], {"before": False, "after": True})
            self.assertTrue(row["persisted"])

    async def test_global_ignore_records_global_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text=f"/yuki 忽略用户 {_TARGET_USER} global",
                user_id=_SUPER_USER,
                group_id=_GROUP_ID,
                sender_role="owner",
            )

            row = fx.only(AUDIT_IGNORE_ADD)
            self.assertEqual(row["scope"], "global")
            self.assertEqual(row["state"]["global_count"], 1)

    async def test_group_scoped_unignore_cannot_clear_global_for_group_admin(self) -> None:
        """群管理员用本群 scope 解忽略时，兜底分支原本会把全局忽略也清掉
        （core/admin.py remove_ignored_user 的 elif 分支），而他无权再设回全局。

        这个测试原先断言该升级被标记为 irreversible —— 那是在如实记录一个漏洞。
        漏洞已在 E0-5 堵上：非超管走不到升级分支，所以现在断言的是「拦住了」，
        场景与执行者身份保持不变。
        """
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            fx.admin.add_ignored_user(_TARGET_USER, group_id=0, scope="global", actor_id=_SUPER_USER)

            reply = await fx.admin.handle_command(
                text=f"/yuki 恢复用户 {_TARGET_USER}",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            self.assertIn("超级管理员", reply or "")
            # 状态未被改动，不只是回复被拒。
            self.assertIn(_TARGET_USER, fx.admin._ignored_global)
            self.assertEqual(
                [r for r in fx.rows() if r.get("event") == AUDIT_IGNORE_REMOVE],
                [],
                "被拒的操作不该留下变更记录",
            )

    async def test_same_escalation_by_super_admin_is_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            fx.admin.add_ignored_user(_TARGET_USER, group_id=0, scope="global", actor_id=_SUPER_USER)

            await fx.admin.handle_command(
                text=f"/yuki 恢复用户 {_TARGET_USER}",
                user_id=_SUPER_USER,
                group_id=_GROUP_ID,
                sender_role="owner",
            )

            row = fx.only(AUDIT_IGNORE_REMOVE)
            self.assertTrue(row["change"]["escalated_to_global"])
            self.assertFalse(row["irreversible"])

    async def test_disabling_high_risk_confirmation_is_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text="/yuki 高风险确认 off group",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            row = fx.only(AUDIT_HIGH_RISK_CONFIRM_SET)
            self.assertEqual(row["subject"], AUDIT_SUBJECT_HIGH_RISK)
            self.assertEqual(row["group_id"], _GROUP_ID)
            self.assertEqual(row["actor_id"], _GROUP_ADMIN)
            self.assertEqual(row["scope"], "group")
            self.assertFalse(row["state"]["high_risk_confirmation_required"])
            self.assertTrue(row["state"]["disables_confirmation"])
            self.assertEqual(row["state"]["effective_source"], "group")
            self.assertTrue(row["persisted"])

    async def test_reset_to_default_records_effective_value_not_just_none(self) -> None:
        """把本群 override 删掉之后是变严还是变松取决于全局值，
        只存 after=None 事后查不出来，所以必须记 effective_required。"""
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            fx.admin.set_high_risk_confirmation_policy(
                required=False, scope="global", group_id=0, actor_id=_SUPER_USER
            )

            await fx.admin.handle_command(
                text="/yuki 高风险确认 default group",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            rows = [r for r in fx.rows() if r["event"] == AUDIT_HIGH_RISK_CONFIRM_SET]
            reset_row = rows[-1]
            self.assertIsNone(reset_row["change"]["high_risk_confirmation_required"]["after"])
            # 本群 override 被删除后落回全局的 False：净效果仍然是「不需要确认」。
            self.assertFalse(reset_row["state"]["effective_required"])
            self.assertEqual(reset_row["state"]["effective_source"], "global")
            self.assertTrue(reset_row["state"]["disables_confirmation"])

    async def test_behavior_mode_records_runtime_only_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text="/yuki 冷漠",
                user_id=_SUPER_USER,
                group_id=_GROUP_ID,
                sender_role="member",
                engine=fx.engine,
            )

            row = fx.only(AUDIT_BEHAVIOR_MODE_SET)
            self.assertEqual(row["subject"], AUDIT_SUBJECT_BEHAVIOR)
            self.assertEqual(row["state"]["mode"], "cold")
            self.assertEqual(row["actor_id"], _SUPER_USER)
            self.assertEqual(row["group_id"], _GROUP_ID)
            # 行为模式不落盘，用 None 区别于「写盘失败」的 False。
            self.assertIsNone(row["persisted"])
            self.assertTrue(row["change"]["trigger"]["before"]["ai_listen_enable"])
            self.assertFalse(row["change"]["trigger"]["after"]["ai_listen_enable"])

    async def test_behavior_param_records_section_key_and_value_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text="/yuki 行为 接话门槛 0.9",
                user_id=_SUPER_USER,
                group_id=_GROUP_ID,
                sender_role="member",
                engine=fx.engine,
            )

            row = fx.only(AUDIT_BEHAVIOR_PARAM_SET)
            self.assertEqual(row["change"]["config_section"], "routing")
            self.assertEqual(row["change"]["config_key"], "min_confidence")
            self.assertEqual(row["change"]["value"], {"before": 0.58, "after": 0.9})
            self.assertEqual(row["state"]["value"], 0.9)

    async def test_write_failure_is_recorded_as_not_persisted(self) -> None:
        """落盘失败以前是 _log.debug 后静默继续，审计必须能区分
        「改了内存」和「存住了」。"""
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            # 把目标文件变成目录，write_text 必然失败。
            fx.admin._white_path.unlink(missing_ok=True)
            fx.admin._white_path.mkdir(parents=True, exist_ok=True)

            reply = await fx.admin.handle_command(
                text="/yuki 加白", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            self.assertIn("已加白本群", reply or "")
            row = fx.only(AUDIT_WHITELIST_ADD)
            self.assertFalse(row["persisted"])


class GroupOpsAuditNoOpTests(unittest.IsolatedAsyncioTestCase):
    """没有真的改动状态时不该产生记录 —— 审计流要能当「变更史」读，
    掺进被拒绝和无效的尝试就没法直接回答「本群状态被谁改成了什么」。"""

    async def test_permission_denied_writes_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            reply = await fx.admin.handle_command(
                text=f"/yuki 忽略用户 {_TARGET_USER} global",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            self.assertIn("权限不足", reply or "")
            self.assertEqual(fx.rows(), [])

    async def test_unignoring_absent_user_writes_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            reply = await fx.admin.handle_command(
                text=f"/yuki 恢复用户 {_TARGET_USER}",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            self.assertIn("不在忽略列表", reply or "")
            self.assertEqual(fx.rows(), [])

    async def test_invalid_behavior_param_writes_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            reply = await fx.admin.handle_command(
                text="/yuki 行为 接话门槛 abc",
                user_id=_SUPER_USER,
                group_id=_GROUP_ID,
                sender_role="member",
                engine=fx.engine,
            )

            self.assertIn("参数值无效", reply or "")
            self.assertEqual(fx.rows(), [])

    async def test_read_only_listing_writes_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text="/yuki 白名单", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            self.assertEqual(fx.rows(), [])


class GroupOpsAuditOptionalTrailTests(unittest.IsolatedAsyncioTestCase):
    """AdminEngine 可以不带 trail 构造（测试、scripts、以及 engine 尚未注入时），
    审计缺失绝不能变成功能缺失。"""

    async def test_admin_without_trail_still_mutates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp, attach=False)
            self.assertIsNone(fx.admin.audit)

            reply = await fx.admin.handle_command(
                text=f"/yuki 忽略用户 {_TARGET_USER}",
                user_id=_GROUP_ADMIN,
                group_id=_GROUP_ID,
                sender_role="admin",
            )

            self.assertIn("已忽略用户", reply or "")
            self.assertTrue(fx.admin.is_user_ignored(_TARGET_USER, group_id=_GROUP_ID))
            self.assertFalse((fx.dir / "audit").exists())

    async def test_positional_construction_still_supported(self) -> None:
        """engine.py 现在是 AdminEngine(config, storage_dir) 位置传参，
        新参数必须是可选的第三个参数，不能破坏既有调用。"""
        with tempfile.TemporaryDirectory() as tmp:
            admin = AdminEngine(_CONFIG, Path(tmp))
            self.assertIsNone(admin.audit)
            self.assertTrue(admin.is_group_whitelisted(_GROUP_ID))

    async def test_disabled_trail_writes_nothing_but_keeps_working(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp, enable=False)

            reply = await fx.admin.handle_command(
                text="/yuki 加白", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            self.assertIn("已加白本群", reply or "")
            self.assertEqual(fx.rows(), [])

    async def test_trail_survives_reinstantiation_like_hot_reload(self) -> None:
        """YukikoEngine.reload_config() 会重建 AdminEngine 而不重建 AuditTrail。
        把同一个 trail 传给新实例后，审计必须继续写进同一条流。"""
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            await fx.admin.handle_command(
                text="/yuki 加白", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )
            self.assertEqual(len(fx.rows()), 1)

            reloaded = AdminEngine(_CONFIG, fx.dir, fx.trail)
            await reloaded.handle_command(
                text="/yuki 拉黑", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            events = [r["event"] for r in fx.rows()]
            self.assertEqual(events, [AUDIT_WHITELIST_ADD, AUDIT_WHITELIST_REMOVE])


class GroupOpsAuditStreamRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_go_to_group_ops_stream_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            await fx.admin.handle_command(
                text="/yuki 加白", user_id=_SUPER_USER, group_id=_GROUP_ID, sender_role="member"
            )

            self.assertEqual(len(fx.rows()), 1)
            for other in ("tool_calls", "memory_writes", "prompt_edits", "knowledge"):
                self.assertEqual(fx.trail.read(other, limit=10), [], f"leaked into {other}")

    async def test_every_record_carries_the_queryable_field_set(self) -> None:
        """审计的价值在于按字段查询，所以字段集合本身就是契约。"""
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            for text in (
                "/yuki 加白",
                f"/yuki 忽略用户 {_TARGET_USER}",
                f"/yuki 恢复用户 {_TARGET_USER}",
                "/yuki 高风险确认 off group",
                "/yuki 冷漠",
                "/yuki 行为 接话门槛 0.9",
                "/yuki 拉黑",
            ):
                await fx.admin.handle_command(
                    text=text,
                    user_id=_SUPER_USER,
                    group_id=_GROUP_ID,
                    sender_role="owner",
                    engine=fx.engine,
                )

            rows = fx.rows()
            self.assertEqual(len(rows), 7)
            required = {
                "ts",
                "event",
                "subject",
                "actor_id",
                "group_id",
                "target_user_id",
                "scope",
                "state",
                "change",
                "persisted",
                "irreversible",
            }
            for row in rows:
                self.assertTrue(
                    required.issubset(row.keys()),
                    f"{row.get('event')} missing {required - set(row.keys())}",
                )
                self.assertEqual(row["group_id"], _GROUP_ID)
                self.assertEqual(row["actor_id"], _SUPER_USER)


class GlobalIgnoreEscalationPermissionTests(unittest.IsolatedAsyncioTestCase):
    """MIGRATION_TODO E0-5：群管理员不得清除自己无法恢复的全局忽略。

    群 scope 解忽略解不到时会兜底去清全局忽略。设置全局忽略需要超管，所以放行
    群管理员做这件事，等于让他删掉一条只有超管能恢复的规则。
    """

    def test_group_admin_cannot_clear_global_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            fx.admin.add_ignored_user("999", scope="global", actor_id=_SUPER_USER)

            ok, message = fx.admin.remove_ignored_user(
                "999", group_id=_GROUP_ID, actor_id="20002"
            )

            self.assertFalse(ok)
            self.assertIn("超级管理员", message)
            # 关键断言：状态没被改动，不只是回复被拒。
            self.assertIn("999", fx.admin._ignored_global)
            self.assertEqual(
                [r for r in fx.rows() if r.get("event") == AUDIT_IGNORE_REMOVE],
                [],
                "被拒的操作不该留下变更记录",
            )

    def test_super_admin_may_still_escalate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            fx.admin.add_ignored_user("999", scope="global", actor_id=_SUPER_USER)

            ok, _ = fx.admin.remove_ignored_user(
                "999", group_id=_GROUP_ID, actor_id=_SUPER_USER
            )

            self.assertTrue(ok)
            self.assertNotIn("999", fx.admin._ignored_global)
            row = fx.only(AUDIT_IGNORE_REMOVE)
            self.assertTrue(row["change"]["escalated_to_global"])
            # 超管有权恢复，所以对执行者不构成不可逆。
            self.assertFalse(row["irreversible"])


class PersistFailureReplyTests(unittest.IsolatedAsyncioTestCase):
    """MIGRATION_TODO E0-4：写盘失败不得谎报成功。

    内存状态确实改了，所以不能说失败；但没落盘、重启即回滚，用户必须知道。
    """

    @staticmethod
    def _break_writes(fx: _AuditFixture, filename: str) -> None:
        # 用目录占位使 json 写入必然失败。
        (fx.dir / filename).mkdir(parents=True, exist_ok=True)

    async def test_whitelist_add_warns_when_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            self._break_writes(fx, "whitelist_groups.json")

            reply = await fx.admin._dispatch(
                "white_add", "", _SUPER_USER, 777, "owner", fx.engine, None
            )

            self.assertIn("777", reply)
            self.assertIn("没能写入磁盘", reply)
            self.assertFalse(fx.only(AUDIT_WHITELIST_ADD)["persisted"])

    async def test_successful_write_has_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)

            reply = await fx.admin._dispatch(
                "white_add", "", _SUPER_USER, 777, "owner", fx.engine, None
            )

            self.assertIn("777", reply)
            self.assertNotIn("没能写入磁盘", reply)
            self.assertTrue(fx.only(AUDIT_WHITELIST_ADD)["persisted"])

    def test_ignore_add_warns_when_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx = _AuditFixture(tmp)
            self._break_writes(fx, "ignored_users.json")

            ok, message = fx.admin.add_ignored_user(
                "999", group_id=_GROUP_ID, actor_id=_SUPER_USER
            )

            # 内存已改，所以仍是成功；但必须带上会丢失的提示。
            self.assertTrue(ok)
            self.assertIn("没能写入磁盘", message)
            self.assertFalse(fx.only(AUDIT_IGNORE_ADD)["persisted"])


if __name__ == "__main__":
    unittest.main()
