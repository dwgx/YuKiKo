"""loop_guard 模块的单元测试（unittest.TestCase，pytest 可跑）。"""

import unittest

from core.loop_guard import (
    DEFAULT_CIRCUIT_BREAKER,
    DEFAULT_CRITICAL,
    DEFAULT_HISTORY_SIZE,
    DEFAULT_POST_COMPACTION_WINDOW,
    DEFAULT_WARNING,
    LoopGuard,
    PostCompactionGuard,
    ToolCallRecord,
    hash_call,
    hash_result,
)


def make_record(name: str = "search", args: dict = None, result: object = None) -> ToolCallRecord:
    """便捷构造 ToolCallRecord：自动计算参数/结果哈希。"""
    if args is None:
        args = {"q": "default"}
    if result is None:
        result = {"count": 1}
    return ToolCallRecord(name=name, args_hash=hash_call(name, args), result_hash=hash_result(result))


class HashFunctionsTest(unittest.TestCase):
    def test_hash_call_is_deterministic_and_key_order_independent(self):
        args_a = {"x": 1, "y": [1, 2], "z": {"k": "v"}}
        args_b = {"y": [1, 2], "z": {"k": "v"}, "x": 1}  # 键顺序不同
        self.assertEqual(hash_call("search", args_a), hash_call("search", args_b))
        self.assertEqual(hash_call("search", args_a), hash_call("search", args_a))
        # 名称或参数不同 -> 哈希不同
        self.assertNotEqual(hash_call("search", args_a), hash_call("image", args_a))
        self.assertNotEqual(hash_call("search", args_a), hash_call("search", {"x": 2, "y": [1, 2], "z": {"k": "v"}}))

    def test_hash_result_strips_volatile_fields(self):
        r1 = {
            "message_id": 1,
            "data": {"id": 9, "ts": 123, "value": 1},
            "list": [{"time": 1, "x": 2}, {"id": 7}],
        }
        r2 = {
            "message_id": 99,
            "data": {"id": 7, "ts": 999, "value": 1},
            "list": [{"time": 5, "x": 2}, {"id": 8}],
        }
        # 只有易变字段不同 -> 哈希相同
        self.assertEqual(hash_result(r1), hash_result(r2))
        # 非易变字段不同 -> 哈希不同
        self.assertNotEqual(hash_result({"value": 1}), hash_result({"value": 2}))
        # 标量结果也能哈希
        self.assertEqual(hash_result(1), hash_result(1))
        self.assertNotEqual(hash_result("ok"), hash_result("fail"))


class LoopGuardTest(unittest.TestCase):
    def test_no_progress_streak_counts_only_consecutive_identical(self):
        guard = LoopGuard()
        rec_a = make_record(args={"q": "a"}, result={"n": 1})
        rec_a_same = make_record(args={"q": "a"}, result={"n": 1})
        rec_b = make_record(args={"q": "b"}, result={"n": 2})

        guard.observe(rec_a)
        self.assertEqual(guard.no_progress_streak("search", rec_a.args_hash), 1)

        # 完全相同的调用连续出现 -> 计数累计
        guard.observe(rec_a_same)
        self.assertEqual(guard.no_progress_streak("search", rec_a.args_hash), 2)

        # 换成不同参数的调用 -> 之前的计数被打破
        guard.observe(rec_b)
        self.assertEqual(guard.no_progress_streak("search", rec_a.args_hash), 0)
        self.assertEqual(guard.no_progress_streak("search", rec_b.args_hash), 1)

    def test_same_args_different_result_breaks_streak(self):
        guard = LoopGuard()
        rec_result_1 = make_record(args={"q": "a"}, result={"n": 1})
        rec_result_2 = make_record(args={"q": "a"}, result={"n": 2})
        guard.observe(rec_result_1)
        guard.observe(rec_result_2)
        # 同参数但结果变化 -> 不算 no-progress，立即断开，只余最近一次
        self.assertEqual(guard.no_progress_streak("search", rec_result_2.args_hash), 1)

    def test_veto_levels_escalate_warn_critical_circuit(self):
        guard = LoopGuard(warning=2, critical=3, circuit_breaker=5)
        rec = make_record()
        self.assertEqual(guard.veto_if_looping("search", rec.args_hash), ("ok", 0))

        guard.observe(rec)
        self.assertEqual(guard.veto_if_looping("search", rec.args_hash), ("ok", 1))
        guard.observe(rec)
        self.assertEqual(guard.veto_if_looping("search", rec.args_hash), ("warn", 2))
        guard.observe(rec)
        self.assertEqual(guard.veto_if_looping("search", rec.args_hash), ("critical", 3))
        guard.observe(rec)
        self.assertEqual(guard.veto_if_looping("search", rec.args_hash), ("critical", 4))
        guard.observe(rec)
        self.assertEqual(guard.veto_if_looping("search", rec.args_hash), ("circuit", 5))

    def test_default_constants(self):
        self.assertEqual(DEFAULT_HISTORY_SIZE, 30)
        self.assertEqual(DEFAULT_WARNING, 10)
        self.assertEqual(DEFAULT_CRITICAL, 20)
        self.assertEqual(DEFAULT_CIRCUIT_BREAKER, 30)
        self.assertEqual(DEFAULT_POST_COMPACTION_WINDOW, 3)


class PostCompactionGuardTest(unittest.TestCase):
    def test_post_compaction_guard_detects_repeated_triplet(self):
        guard = PostCompactionGuard()
        guard.arm()
        rec = make_record()
        self.assertFalse(guard.observe(rec))
        self.assertFalse(guard.observe(rec))
        # 窗口（默认 3）内第三次完全相同 -> 检测到 loop
        self.assertTrue(guard.observe(rec))
        # 之后继续重复仍然保持检测
        self.assertTrue(guard.observe(rec))

    def test_post_compaction_guard_ignored_when_not_armed(self):
        guard = PostCompactionGuard()
        rec = make_record()
        self.assertFalse(guard.observe(rec))
        self.assertFalse(guard.observe(rec))
        self.assertFalse(guard.observe(rec))
        # 武装后恢复正常检测
        guard.arm()
        self.assertFalse(guard.observe(rec))
        self.assertFalse(guard.observe(rec))
        self.assertTrue(guard.observe(rec))
        # disarm 后再次失效
        guard.disarm()
        self.assertFalse(guard.observe(rec))


if __name__ == "__main__":
    unittest.main()
