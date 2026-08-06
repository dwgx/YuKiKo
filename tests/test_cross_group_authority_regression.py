"""群管理员的权限不能跨群使用 —— 在 A 群当管理不等于能封 B 群的人。

## 缺陷（2026-08-06 子 agent 审计发现并对抗验证为 CONFIRMED/high）

`AgentLoop._resolve_permission_level` 按**消息来源群**授予 `group_admin`：

```python
if not ctx.is_private and ctx.group_id:
    role = (ctx.sender_role or "").lower()
    if ctx.is_whitelisted_group and role in ("owner", "admin"):
        return "group_admin"
```

而 `set_group_ban` / `set_group_kick` 这类 handler 的 `group_id` 是从**模型参数**
读的（`int(args.get("group_id", 0))`）。两者从不交叉校验。

后果：在 A 群当管理的人，可以让机器人对**任何**机器人也在的群执行封禁 ——
权限在 A 群赚到，作用在 B 群，而 B 群里他可能什么都不是。

`_guard_high_risk_tool_call` 兜不住：它只处理「你确定吗」的确认流程
（pending / confirm / cancel / 防参数漂移），从来不看目标群是哪个。

## 修法

在高风险守卫入口加一道跨群校验，`group_admin` 越权时直接拒绝。
`super_admin` 不受限 —— 它按设计凌驾一切。

放在确认策略**之前**：确认策略解决「你确定吗」，跨群校验解决「你没有这个群的
权限」，后者不该因为某个群把确认关掉就被放行。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.agent import AgentContext, AgentLoop


def _ctx(*, group_id: int, role: str = "admin", user_id: str = "1001") -> AgentContext:
    ctx = AgentContext(
        conversation_id=f"group:{group_id}",
        user_id=user_id,
        user_name="tester",
        group_id=group_id,
        bot_id="9999",
        is_private=False,
        mentioned=True,
        message_text="封了他",
    )
    ctx.sender_role = role  # type: ignore[attr-defined]
    ctx.is_whitelisted_group = True  # type: ignore[attr-defined]
    return ctx


def _loop(*, admin_ids: set[str] | None = None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._admin_ids = admin_ids or set()  # type: ignore[attr-defined]
    loop.high_risk_control_enable = True  # type: ignore[attr-defined]
    loop._pending_high_risk_actions = {}  # type: ignore[attr-defined]
    loop.high_risk_pending_ttl_seconds = 120  # type: ignore[attr-defined]
    return loop


class CrossGroupAuthorityIsRejectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = _loop()

    def test_group_admin_cannot_act_on_another_group(self) -> None:
        """核心回归：A 群管理 + 参数指向 B 群 = 拒绝。"""

        ctx = _ctx(group_id=111)
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True):
            reply = self.loop._cross_group_authority_error(
                ctx, "set_group_ban", {"group_id": 222, "user_id": 5}
            )
        self.assertTrue(reply, "跨群高风险操作被放行了 —— 权限提升")
        self.assertIn("跨群", reply)

    def test_group_admin_can_still_act_on_their_own_group(self) -> None:
        """反向：本群操作必须照常放行，别把正常管理一起堵死。"""

        ctx = _ctx(group_id=111)
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True):
            reply = self.loop._cross_group_authority_error(
                ctx, "set_group_ban", {"group_id": 111, "user_id": 5}
            )
        self.assertEqual(reply, "", "本群管理操作被误拦了")

    def test_super_admin_is_not_restricted(self) -> None:
        """super_admin 按设计凌驾一切，不受跨群限制。"""

        loop = _loop(admin_ids={"1001"})
        ctx = _ctx(group_id=111, user_id="1001")
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True):
            reply = loop._cross_group_authority_error(
                ctx, "set_group_ban", {"group_id": 222, "user_id": 5}
            )
        self.assertEqual(reply, "", "super_admin 被跨群校验拦住了")

    def test_plain_user_is_unaffected_by_this_check(self) -> None:
        """普通用户由既有权限门处理，这条校验不该改变他们的路径。"""

        ctx = _ctx(group_id=111, role="member")
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True):
            reply = self.loop._cross_group_authority_error(
                ctx, "set_group_ban", {"group_id": 222, "user_id": 5}
            )
        self.assertEqual(reply, "")

    def test_non_high_risk_tool_is_not_blocked(self) -> None:
        """只管高风险工具 —— 跨群查群信息之类不该被拦。"""

        ctx = _ctx(group_id=111)
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=False):
            reply = self.loop._cross_group_authority_error(
                ctx, "get_group_info", {"group_id": 222}
            )
        self.assertEqual(reply, "")

    def test_missing_or_malformed_group_id_is_not_treated_as_cross_group(self) -> None:
        """没给 group_id 或给了垃圾值时，交给 handler 自己的校验，别在这里误拦。"""

        ctx = _ctx(group_id=111)
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True):
            for args in ({}, {"group_id": 0}, {"group_id": "abc"}, {"group_id": None}):
                with self.subTest(args=args):
                    self.assertEqual(
                        self.loop._cross_group_authority_error(ctx, "set_group_ban", args),
                        "",
                    )


class GuardEntryPointRunsTheCheckTests(unittest.TestCase):
    """校验必须真的接在 _guard_high_risk_tool_call 上，否则等于没接。"""

    def test_guard_blocks_cross_group_before_confirmation_flow(self) -> None:
        """且要在确认策略之前 —— 关掉确认的群不该因此获得跨群能力。"""

        loop = _loop()
        ctx = _ctx(group_id=111)
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True), patch.object(
            AgentLoop, "_require_high_risk_confirmation_for_user", return_value=False
        ):
            reply = loop._guard_high_risk_tool_call(
                ctx=ctx,
                tool_name="set_group_ban",
                tool_args={"group_id": 222, "user_id": 5},
            )
        self.assertTrue(
            reply,
            "确认被关掉时跨群操作被放行了 —— 跨群校验必须排在确认策略之前",
        )

    def test_guard_respects_the_global_disable_switch(self) -> None:
        """high_risk_control_enable=False 时整套守卫关闭，这是既有语义，别改。"""

        loop = _loop()
        loop.high_risk_control_enable = False  # type: ignore[attr-defined]
        ctx = _ctx(group_id=111)
        with patch.object(AgentLoop, "_tool_is_high_risk", return_value=True):
            reply = loop._guard_high_risk_tool_call(
                ctx=ctx,
                tool_name="set_group_ban",
                tool_args={"group_id": 222, "user_id": 5},
            )
        self.assertEqual(reply, "")


if __name__ == "__main__":
    unittest.main()
