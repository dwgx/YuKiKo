"""超时/失败通知的抑制条件不能等于超时的高发条件。

原实现（app.py `on_dispatch_complete`）：

    if status == "cancelled" and reason in {"process_timeout", "process_error"}:
        if pending_count > 0:
            engine.logger.info("queue_error_notice_skip | ...")
            return          # 用户侧完全零反馈

判据是队列里**别的**任务有多少，与当前这个用户是否被直接指向无关。
群越活跃 pending 越大，被 @ 的人越容易被静默 —— 而群活跃恰恰是超时最容易发生的时候。

实测 trace=118886-25：pending=5 命中静默。同一 trace 的 trigger 决策是
`should=True reason=directed active=True followup=True`，
`queue_cancel_policy high_priority=True reply_to_bot=True`
—— 用户明确 @ 了机器人并发了语音，从入队到取消 120 秒全程零输出。
从用户视角这是「机器人死了」，日志里却写着「为了防刷屏故意不说」。

另外 `message_ttl_expired` 的取消（真实群里 13 次）原本根本不在通知集合里，
同样是「提了要求，什么都没收到」。

修法：防刷屏改成按会话去重；被直接指向的回合（high_priority / reply_to_bot）
无论 pending 多少都要出声；TTL 过期纳入同一套通知逻辑。

本文件分两层：
  1. AST 层 —— 证明 `on_dispatch_complete` 里那条通知分支不再以 pending_count 为门。
     这一层不依赖任何新符号，所以在未修的基线上是**行为判据红**，不是 ImportError 红。
  2. 行为层 —— 真调 `_should_send_queue_cancel_notice`，用注入的时间戳验证去重窗口。
     新符号在用例内部 import，避免基线上整个文件收集失败。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any

import app

_APP_PATH = Path("app.py")


def _gate() -> Any:
    """延迟导入新符号：模块级 import 会让基线上整个文件 ImportError，
    连上面 AST 那几条不依赖新符号的判据都跑不到。"""

    from app import _should_send_queue_cancel_notice

    return _should_send_queue_cancel_notice


def _notice_text() -> Any:
    from app import _queue_cancel_notice_text

    return _queue_cancel_notice_text


def _notifiable_reasons() -> frozenset[str]:
    from app import _QUEUE_CANCEL_NOTICE_REASONS

    return frozenset(_QUEUE_CANCEL_NOTICE_REASONS)


def _dispatch_complete_node() -> ast.AsyncFunctionDef | ast.FunctionDef:
    tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "on_dispatch_complete"
    ]
    if len(found) != 1:
        raise AssertionError(f"app.py 里 on_dispatch_complete 定义数 ={len(found)}，本守卫需要跟着改")
    return found[0]


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


def _notice_branch() -> ast.If:
    """通知分支 = `on_dispatch_complete` 里那个真正调用 _safe_send 的 if。

    用「分支里有没有 _safe_send 调用」定位，而不是匹配源码子串 ——
    子串匹配会命中本文件或注释里的同名文字。
    """

    fn = _dispatch_complete_node()
    branches = [
        node for node in ast.walk(fn) if isinstance(node, ast.If) and _calls_named(node, "_safe_send")
    ]
    if len(branches) != 1:
        raise AssertionError(f"定位到 {len(branches)} 个含 _safe_send 的分支，无法判定通知分支")
    return branches[0]


def _resolved_reason_strings(test_node: ast.expr) -> set[str]:
    """取出分支条件里出现的原因字符串。

    条件可能写成字面量集合，也可能写成模块常量名 —— 两种形状都要能读出成员，
    否则「换个写法」就能让判据永不命中。
    """

    strings = {
        node.value for node in ast.walk(test_node) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for node in ast.walk(test_node):
        if not isinstance(node, ast.Name):
            continue
        value = getattr(app, node.id, None)
        if isinstance(value, (set, frozenset, tuple, list)):
            strings.update(str(item) for item in value)
    return strings


class NoticeBranchIsNotGatedOnPendingCountTests(unittest.TestCase):
    """AST 层：不依赖新符号，未修的基线上必须红在这里。"""

    def test_notice_branch_does_not_gate_on_pending_count(self) -> None:
        branch = _notice_branch()
        offenders = [
            ast.unparse(node.test)
            for node in ast.walk(branch)
            if isinstance(node, ast.If)
            and any(
                isinstance(name, ast.Name) and name.id == "pending_count"
                for name in ast.walk(node.test)
            )
        ]
        self.assertEqual(
            offenders,
            [],
            "通知分支里还有以 pending_count 为条件的判断: "
            f"{offenders}\n"
            "队列里别的任务有多少，与当前这个用户是否需要反馈无关。"
            "群越忙 pending 越大，而群忙正是超时高发的时候 —— "
            "这个门等于「最需要说话时闭嘴」。",
        )

    def test_notice_branch_covers_ttl_expiry(self) -> None:
        branch = _notice_branch()
        reasons = _resolved_reason_strings(branch.test)
        self.assertIn(
            "process_timeout",
            reasons,
            f"通知分支条件里读不出 process_timeout，判据形状变了: {sorted(reasons)}",
        )
        self.assertIn(
            "message_ttl_expired",
            reasons,
            "TTL 过期的取消不在通知集合里 —— 真实群里出现 13 次，"
            "每次都是「用户提了要求，什么都没收到」。"
            f"当前可读出的原因: {sorted(reasons)}",
        )

    def test_notice_branch_delegates_to_a_conversation_level_gate(self) -> None:
        branch = _notice_branch()
        calls = _calls_named(branch, "_should_send_queue_cancel_notice")
        self.assertTrue(
            calls,
            "通知分支没有调用 _should_send_queue_cancel_notice —— "
            "防刷屏必须走按会话去重的判定，而不是内联 pending 比较。",
        )
        kwargs = {kw.arg for call in calls for kw in call.keywords}
        self.assertIn(
            "directed",
            kwargs,
            f"判定调用没有传 directed（被 @ / 私聊 / 回复机器人）: {sorted(kwargs)}",
        )
        self.assertIn(
            "conversation_id",
            kwargs,
            f"判定调用没有传 conversation_id，无法按会话去重: {sorted(kwargs)}",
        )

    def test_gate_signature_takes_no_pending_count(self) -> None:
        tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
        fns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_should_send_queue_cancel_notice"
        ]
        self.assertEqual(len(fns), 1, "app.py 里没有唯一的 _should_send_queue_cancel_notice 定义")
        args = fns[0].args
        names = {a.arg for a in [*args.args, *args.posonlyargs, *args.kwonlyargs]}
        self.assertNotIn(
            "pending_count",
            names,
            f"判定函数又把 pending_count 收进来了，等于把原缺陷搬了个位置: {sorted(names)}",
        )


class ClosureBindingOrderGuardTests(unittest.TestCase):
    """补充守卫（未修的基线上本来就绿）：通知分支读的是外层作用域里后赋值的名字。

    `on_dispatch_complete` 在 `high_priority` / `reply_to_bot` 赋值**之前**就被 def 出来，
    靠闭包在调用时取值。只要 `dispatcher.submit(...)` 仍排在两个赋值之后就安全；
    有人把 submit 往上挪就会变成只在真实队列路径上炸的 NameError。
    """

    def test_directedness_is_bound_before_submit(self) -> None:
        tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
        enclosing = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "on_dispatch_complete"
                for child in node.body
            )
        ]
        self.assertEqual(len(enclosing), 1, "找不到 on_dispatch_complete 的唯一外层函数")
        body = enclosing[0].body

        assigned_at: dict[str, int] = {}
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in {"high_priority", "reply_to_bot"}:
                        assigned_at.setdefault(tgt.id, stmt.lineno)
        self.assertEqual(
            sorted(assigned_at),
            ["high_priority", "reply_to_bot"],
            f"外层函数里找不到这两个赋值，守卫需要跟着改: {assigned_at}",
        )

        submits = [
            node.lineno
            for node in ast.walk(enclosing[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit"
        ]
        self.assertTrue(submits, "外层函数里找不到 dispatcher.submit(...) 调用")
        for name, lineno in assigned_at.items():
            self.assertLess(
                lineno,
                min(submits),
                f"{name} 的赋值排在 dispatcher.submit 之后 —— 回调里读它会 NameError，"
                "而且只在真实队列路径上炸",
            )


class QueueCancelNoticeDedupBehaviourTests(unittest.TestCase):
    """行为层：真调判定函数，时间戳注入，不 sleep。"""

    def setUp(self) -> None:
        state = getattr(app, "_QUEUE_CANCEL_NOTICE_LAST_AT", None)
        if isinstance(state, dict):
            state.clear()
        self.addCleanup(lambda: state.clear() if isinstance(state, dict) else None)

    def test_directed_turn_speaks_no_matter_how_busy_the_group_is(self) -> None:
        """trace=118886-25 的形状：被 @ 了，队列很忙，仍然必须出声。"""

        gate = _gate()
        # 先让同会话刚发过一条通知，去重窗口正处于生效状态。
        first, _ = gate(
            conversation_id="group:118886",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=45.0,
            now=1000.0,
        )
        self.assertTrue(first)

        allow, why = gate(
            conversation_id="group:118886",
            reason="process_timeout",
            directed=True,
            dedup_window_seconds=45.0,
            now=1001.0,
        )
        self.assertTrue(
            allow,
            f"被直接指向的回合被静默了（gate={why}）—— 这正是最需要反馈的场景",
        )
        self.assertEqual(why, "directed_turn")

    def test_undirected_flood_is_deduped_within_the_window(self) -> None:
        gate = _gate()
        first, _ = gate(
            conversation_id="group:1",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=45.0,
            now=100.0,
        )
        second, why = gate(
            conversation_id="group:1",
            reason="process_error",
            directed=False,
            dedup_window_seconds=45.0,
            now=130.0,
        )
        self.assertTrue(first)
        self.assertFalse(second, "同会话 30 秒内第二条未定向通知应被去重")
        self.assertEqual(why, "dedup_window")

    def test_undirected_notice_returns_after_the_window_elapses(self) -> None:
        gate = _gate()
        gate(
            conversation_id="group:1",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=45.0,
            now=100.0,
        )
        allow, why = gate(
            conversation_id="group:1",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=45.0,
            now=200.0,
        )
        self.assertTrue(allow, f"窗口过去了还在静默（gate={why}）")
        self.assertEqual(why, "dedup_window_elapsed")

    def test_ttl_expiry_is_notifiable(self) -> None:
        gate = _gate()
        allow, why = gate(
            conversation_id="group:2",
            reason="message_ttl_expired",
            directed=True,
            dedup_window_seconds=45.0,
            now=10.0,
        )
        self.assertTrue(allow, f"TTL 过期没有通知（gate={why}）")

    def test_silent_cancellations_stay_silent(self) -> None:
        """被新消息取代 / 空回复 是刻意沉默，不该变成报错刷屏。"""

        gate = _gate()
        for reason in ("cancelled_by_new_trace", "cancelled_by_smart_interrupt", "empty_response", "ok"):
            with self.subTest(reason=reason):
                allow, why = gate(
                    conversation_id="group:3",
                    reason=reason,
                    directed=True,
                    dedup_window_seconds=45.0,
                    now=10.0,
                )
                self.assertFalse(allow, f"{reason} 不该触发用户通知")
                self.assertEqual(why, "reason_not_notifiable")

    def test_dedup_is_per_conversation(self) -> None:
        gate = _gate()
        gate(
            conversation_id="group:a",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=45.0,
            now=100.0,
        )
        allow, why = gate(
            conversation_id="group:b",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=45.0,
            now=101.0,
        )
        self.assertTrue(allow, f"A 群的通知不该让 B 群闭嘴（gate={why}）")

    def test_zero_window_disables_dedup(self) -> None:
        gate = _gate()
        gate(
            conversation_id="group:4",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=0,
            now=100.0,
        )
        allow, _ = gate(
            conversation_id="group:4",
            reason="process_timeout",
            directed=False,
            dedup_window_seconds=0,
            now=100.1,
        )
        self.assertTrue(allow, "窗口配成 0 应等于关闭去重")

    def test_invalid_window_falls_back_to_the_default(self) -> None:
        gate = _gate()
        for bad in (None, "abc", -5):
            with self.subTest(window=bad):
                state = app._QUEUE_CANCEL_NOTICE_LAST_AT
                state.clear()
                gate(
                    conversation_id="group:5",
                    reason="process_timeout",
                    directed=False,
                    dedup_window_seconds=bad,
                    now=100.0,
                )
                allow, why = gate(
                    conversation_id="group:5",
                    reason="process_timeout",
                    directed=False,
                    dedup_window_seconds=bad,
                    now=101.0,
                )
                self.assertFalse(allow, f"坏窗口值 {bad!r} 应回落到默认窗口而不是关闭去重")
                self.assertEqual(why, "dedup_window")

    def test_every_notifiable_reason_has_its_own_text(self) -> None:
        text_for = _notice_text()
        seen: dict[str, str] = {}
        for reason in sorted(_notifiable_reasons()):
            body = text_for(reason)
            self.assertTrue(body.strip(), f"{reason} 没有通知文案")
            seen[reason] = body
        self.assertEqual(
            len(set(seen.values())),
            len(seen),
            f"不同取消原因用了同一句文案，用户分不清发生了什么: {seen}",
        )


if __name__ == "__main__":
    unittest.main()
