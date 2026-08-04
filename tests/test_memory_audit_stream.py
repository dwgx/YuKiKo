"""memory_writes 审计流 + 保留策略留痕（MIGRATION_TODO E3-4 的 memory 半边）。

已有的 `memory_audit_log` SQLite 表不重建；这里锁的是它缺的那部分耐久性：
审计不能被 `enable_vector_memory` 一起关掉，批量删除必须留痕带条数，
以及审计能按字段查（而不是只能按 record_id）。

断言字段而不是断言文案，契约就不依赖措辞。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import core.memory as memory_module
from core.audit import STREAM_MEMORY_WRITES, AuditTrail
from core.memory import MemoryEngine


def _reset_thread_conn() -> None:
    """断开 thread-local SQLite 连接。

    `MemoryEngine._connect()` 把连接缓存在模块级 `threading.local()` 上且
    **不按 db_path 分键**，所以同线程里第二个引擎会复用第一个的库。
    每个用例都建新引擎，不断开就会互相读到对方的数据。
    """
    conn = getattr(memory_module._db_local, "conn", None)
    if conn is not None:
        conn.close()
        memory_module._db_local.conn = None


def _read_stream(base: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((base / STREAM_MEMORY_WRITES).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


class MemoryWritesAuditStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_thread_conn()

    def tearDown(self) -> None:
        _reset_thread_conn()

    def test_memory_record_lifecycle_lands_in_memory_writes_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False},
                memory_dir=root / "memory",
                audit=trail,
            )
            ok, _, payload = memory.add_memory_record(
                conversation_id="group:42",
                user_id="10001",
                role="user",
                content="我住在杭州",
                actor="agent:10001",
                note="用户显式声明",
            )
            self.assertTrue(ok)
            record_id = int(payload["id"])

            self.assertTrue(
                memory.update_memory_record(
                    record_id=record_id,
                    content="我住在杭州西湖区",
                    actor="agent:10001",
                    note="用户补充了区",
                )[0]
            )
            self.assertTrue(
                memory.delete_memory_record(
                    record_id=record_id,
                    actor="agent:10001",
                    note="用户要求删除",
                )[0]
            )

            rows = _read_stream(root / "audit")
            self.assertEqual([row["event"] for row in rows], ["add", "update", "delete"])

            add_row, update_row, delete_row = rows
            # 每条都要能按字段查：谁改的 / 改了哪条 / 属于谁 / 前后值。
            for row in rows:
                self.assertEqual(row["actor"], "agent:10001")
                self.assertEqual(row["record_id"], record_id)
                self.assertEqual(row["user_id"], "10001")
                self.assertEqual(row["conversation_id"], "group:42")
                self.assertTrue(row["note"])
                self.assertIn("ts", row)

            self.assertEqual(add_row["change"], {"before": "", "after": "我住在杭州"})
            self.assertEqual(
                update_row["change"],
                {"before": "我住在杭州", "after": "我住在杭州西湖区"},
            )
            self.assertEqual(
                delete_row["change"],
                {"before": "我住在杭州西湖区", "after": ""},
            )

    def test_preferred_name_change_lands_in_stream_with_profile_role(self) -> None:
        """改称呼不是 embeddings 行，靠 record_id=None + role=profile 承载。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False},
                memory_dir=root / "memory",
                audit=trail,
            )
            memory.add_message("group:42", "10001", "user", "在吗", user_name="小明")
            ok, _, _ = memory.set_preferred_name(
                target_user_id="10001",
                preferred_name="小明同学",
                actor="agent:10001",
                note="用户要求改称呼",
            )
            self.assertTrue(ok)

            rows = [r for r in _read_stream(root / "audit") if r["event"] == "set_preferred_name"]
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["record_id"])
            self.assertEqual(rows[0]["role"], "profile")
            self.assertEqual(rows[0]["change"]["after"], "小明同学")

    def test_compact_writes_one_stream_row_per_deleted_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False},
                memory_dir=root / "memory",
                audit=trail,
            )
            for _ in range(3):
                memory.add_message("group:42", "10001", "user", "重复的一句话", user_name="小明")
            memory._flush_vector_buffer()

            ok, _, payload = memory.compact_memory_records(
                conversation_id="group:42",
                user_id="10001",
                actor="agent:10001",
                note="去重",
                dry_run=False,
            )
            self.assertTrue(ok)
            deleted_ids = payload["deleted_ids"]
            self.assertTrue(deleted_ids)

            rows = [r for r in _read_stream(root / "audit") if r["event"] == "compact_delete"]
            self.assertEqual(len(rows), len(deleted_ids))
            self.assertEqual(
                sorted(int(r["record_id"]) for r in rows),
                sorted(int(i) for i in deleted_ids),
            )

    def test_stream_survives_vector_memory_disabled(self) -> None:
        """核心回归：关掉向量记忆会静默关掉 SQLite 审计，JSONL 必须还在。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False, "enable_vector_memory": False},
                memory_dir=root / "memory",
                audit=trail,
            )
            memory.add_message("group:42", "10001", "user", "在吗", user_name="小明")
            self.assertTrue(
                memory.set_preferred_name(
                    target_user_id="10001",
                    preferred_name="小明同学",
                    actor="agent:10001",
                    note="改称呼",
                )[0]
            )

            _, sql_total = memory.list_memory_audit_logs(limit=50)
            self.assertEqual(sql_total, 0, "SQLite 审计本就被 enable_vector_memory 短路")

            rows = _read_stream(root / "audit")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "set_preferred_name")

    def test_engine_without_audit_trail_still_writes_sqlite_audit(self) -> None:
        """audit=None（测试/脚本直接构造）必须退化成改动前的行为，不能炸。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False},
                memory_dir=Path(tmp) / "memory",
            )
            self.assertIsNone(memory.audit)
            ok, _, _ = memory.add_memory_record(
                conversation_id="group:42",
                user_id="10001",
                role="user",
                content="我住在杭州",
                actor="agent:10001",
                note="n",
            )
            self.assertTrue(ok)
            _, total = memory.list_memory_audit_logs(limit=50)
            self.assertEqual(total, 1)


class MemoryAuditLogFieldQueryTests(unittest.TestCase):
    """owner 的要求是「可按字段查」，原先只能按 record_id 过滤。"""

    def setUp(self) -> None:
        _reset_thread_conn()

    def tearDown(self) -> None:
        _reset_thread_conn()

    def _seed(self, memory: MemoryEngine) -> list[int]:
        ids: list[int] = []
        for index, user_id in enumerate(["10001", "10001", "20002"]):
            _, _, payload = memory.add_memory_record(
                conversation_id="group:42",
                user_id=user_id,
                role="user",
                content=f"事实{index}",
                actor=f"agent:{user_id}",
                note="n",
            )
            ids.append(int(payload["id"]))
        memory.update_memory_record(
            record_id=ids[0], content="事实0改", actor="webui:admin", note="改一下"
        )
        memory.set_preferred_name(
            target_user_id="10001", preferred_name="小明", actor="agent:10001", note="改称呼"
        )
        return ids

    def test_audit_logs_filter_by_action_actor_user_and_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False}, memory_dir=Path(tmp) / "memory"
            )
            ids = self._seed(memory)

            _, total_all = memory.list_memory_audit_logs(limit=50)
            self.assertEqual(total_all, 5)

            rows, total = memory.list_memory_audit_logs(action="add", limit=50)
            self.assertEqual(total, 3)
            self.assertEqual({r["action"] for r in rows}, {"add"})

            rows, total = memory.list_memory_audit_logs(actor="webui:admin", limit=50)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["action"], "update")

            rows, total = memory.list_memory_audit_logs(user_id="20002", limit=50)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["user_id"], "20002")

            rows, total = memory.list_memory_audit_logs(role="profile", limit=50)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["action"], "set_preferred_name")

            # record_id 这条老路径不能被新过滤器破坏。
            rows, total = memory.list_memory_audit_logs(record_id=ids[0], limit=50)
            self.assertEqual(total, 2)
            self.assertEqual({r["action"] for r in rows}, {"add", "update"})

    def test_audit_logs_filter_by_time_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False}, memory_dir=Path(tmp) / "memory"
            )
            self._seed(memory)

            _, total = memory.list_memory_audit_logs(since="2099-01-01", limit=50)
            self.assertEqual(total, 0)

            _, total = memory.list_memory_audit_logs(since="1970-01-01", limit=50)
            self.assertEqual(total, 5)

            _, total = memory.list_memory_audit_logs(until="1970-01-01", limit=50)
            self.assertEqual(total, 0)

    def test_action_and_user_filters_combine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False}, memory_dir=Path(tmp) / "memory"
            )
            self._seed(memory)
            rows, total = memory.list_memory_audit_logs(
                action="add", user_id="10001", limit=50
            )
            self.assertEqual(total, 2)
            self.assertEqual({r["user_id"] for r in rows}, {"10001"})


class MemoryRetentionAuditTests(unittest.TestCase):
    """保留策略删除必须可配、可关、留痕带条数，且不吞异常。"""

    def setUp(self) -> None:
        _reset_thread_conn()

    def tearDown(self) -> None:
        _reset_thread_conn()

    def test_embedding_retention_prune_is_audited_with_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": True, "embedding_retention_days": 7},
                memory_dir=root / "memory",
                audit=trail,
            )
            stale = datetime.now(UTC) - timedelta(days=30)
            for index in range(3):
                memory.add_message(
                    "group:42", "10001", "user", f"很久以前{index}", timestamp=stale
                )
            memory._flush_vector_buffer()
            memory.write_daily_snapshot(datetime.now().date().isoformat())

            rows = [
                r
                for r in _read_stream(root / "audit")
                if r["event"] == "retention_prune" and r.get("role") == "embeddings"
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["change"]["before"], "rows=3")
            self.assertEqual(rows[0]["reason"], "embedding_retention_days")
            self.assertEqual(rows[0]["actor"], "system:retention")

    def test_embedding_retention_default_never_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": True},
                memory_dir=root / "memory",
                audit=trail,
            )
            self.assertEqual(memory.embedding_retention_days, 0)
            stale = datetime.now(UTC) - timedelta(days=999)
            memory.add_message("group:42", "10001", "user", "很久以前", timestamp=stale)
            memory._flush_vector_buffer()
            memory.write_daily_snapshot(datetime.now().date().isoformat())

            _, total = memory.list_memory_records(conversation_id="group:42", limit=50)
            self.assertEqual(total, 1, "默认保留期 0 = 永不删除")
            self.assertEqual(
                [r for r in _read_stream(root / "audit") if r["event"] == "retention_prune"],
                [],
            )

    def test_media_memory_row_cap_prune_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False, "media_memory_max_rows": 200},
                memory_dir=root / "memory",
                audit=trail,
            )
            for index in range(205):
                memory.add_media_artifacts(
                    conversation_id="group:42",
                    message_id=f"m{index}",
                    user_id="10001",
                    source="message",
                    media_items=[{"type": "image", "url": f"https://example.com/{index}.png"}],
                )
            rows = [
                r
                for r in _read_stream(root / "audit")
                if r["event"] == "retention_prune" and r.get("role") == "media_memory"
            ]
            self.assertTrue(rows, "媒体记忆淘汰必须留痕，否则记忆凭空少了无从追溯")
            self.assertEqual(rows[0]["reason"], "media_memory_max_rows")

    def test_media_memory_max_rows_zero_disables_pruning(self) -> None:
        """改动前 max(200, ...) 让「永不删除」无法表达。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False, "media_memory_max_rows": 0},
                memory_dir=root / "memory",
                audit=trail,
            )
            self.assertEqual(memory.media_memory_max_rows, 0)
            for index in range(205):
                memory.add_media_artifacts(
                    conversation_id="group:42",
                    message_id=f"m{index}",
                    user_id="10001",
                    source="message",
                    media_items=[{"type": "image", "url": f"https://example.com/{index}.png"}],
                )
            rows = [
                r
                for r in _read_stream(root / "audit")
                if r["event"] == "retention_prune" and r.get("role") == "media_memory"
            ]
            self.assertEqual(rows, [], "上限 0 表示不淘汰，不该有任何删除")

    def test_media_memory_write_failure_is_logged_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False},
                memory_dir=Path(tmp) / "memory",
            )
            memory.db_path = Path(tmp) / "does_not_exist" / "nope.db"
            _reset_thread_conn()
            with self.assertLogs("yukiko.memory", level="WARNING") as captured:
                memory.add_media_artifacts(
                    conversation_id="group:42",
                    message_id="m1",
                    user_id="10001",
                    source="message",
                    media_items=[{"type": "image", "url": "https://example.com/a.png"}],
                )
            self.assertTrue(
                any("media_memory_write_failed" in line for line in captured.output),
                "原先是 except Exception: return，失败完全静默",
            )


if __name__ == "__main__":
    unittest.main()
