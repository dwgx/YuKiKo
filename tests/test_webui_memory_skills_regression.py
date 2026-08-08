from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import core.webui as webui
from core.skill_loader import SkillMeta
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _seed_memory_db(db_path: Path) -> None:
    """建一张与 MemoryEngine 同构的 embeddings 表并写入分层样例数据。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT,
            created_at TEXT NOT NULL,
            origin_class TEXT NOT NULL DEFAULT 'untrusted'
        );
        """
    )
    rows = [
        ("group:10001:user", "u-1", "user", "大家好", "user", "2026-08-08T00:00:00+00:00"),
        ("group:10001:user", "u-2", "user", "你好呀", "agent", "2026-08-08T00:00:01+00:00"),
        ("group:10002:user", "u-3", "system", "系统记录", "untrusted", "2026-08-08T00:00:02+00:00"),
        ("private:10003", "u-1", "user", "私聊消息", "user", "2026-08-08T00:00:03+00:00"),
    ]
    for conv, uid, role, content, origin, ts in rows:
        conn.execute(
            "INSERT INTO embeddings (conversation_id, user_id, role, content, embedding, created_at, origin_class)"
            " VALUES (?, ?, ?, ?, '[]', ?, ?);",
            (conv, uid, role, content, ts, origin),
        )
    conn.commit()
    conn.close()


class _FakeMemory:
    """假记忆引擎：db_path 指向真实 SQLite，list_memory_records 返回固定样例。"""

    enable_vector_memory = True

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def list_memory_records(self, *, conversation_id="", user_id="", role="", keyword="", limit=50, offset=0):
        items = [
            {
                "id": 1,
                "conversation_id": "group:10001:user",
                "conversation_label": "群聊 10001（按用户隔离）",
                "user_id": "u-1",
                "display_name": "用户-u-1",
                "role": "user",
                "content": "大家好",
                "created_at": "2026-08-08T00:00:00+00:00",
            },
            {
                "id": 4,
                "conversation_id": "private:10003",
                "conversation_label": "私聊",
                "user_id": "u-1",
                "display_name": "用户-u-1",
                "role": "user",
                "content": "私聊消息",
                "created_at": "2026-08-08T00:00:03+00:00",
            },
        ]
        return items, len(items)

    def get_display_name(self, user_id: str) -> str:
        return f"用户-{user_id}"


class _DuckSkill:
    """非 SkillMeta 的鸭子对象，验证序列化容错。"""

    name = "duck-skill"
    description = "Duck skill"
    description_zh = ""
    homepage = None
    user_invocable = True
    disable_model_invocation = False
    always = False
    requires = {"bins": ["duck"]}
    install = []


class _FakeSkillRegistry:
    def __init__(self, skills) -> None:
        self._skills = skills

    def load(self):
        return self._skills


class _FakeEngine:
    def __init__(self, memory=None, skill_registry=None) -> None:
        self.memory = memory
        self.skill_registry = skill_registry


class WebuiMemorySkillsRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_engine = webui._engine
        self._orig_token = os.environ.get("WEBUI_TOKEN")
        os.environ["WEBUI_TOKEN"] = "test-token"
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "memory.db"
        _seed_memory_db(db_path)
        skills = [
            SkillMeta(
                name="web-search",
                description="Search the web for answers",
                description_zh="联网搜索",
                user_invocable=False,
            ),
            _DuckSkill(),
        ]
        self.engine = _FakeEngine(memory=_FakeMemory(db_path), skill_registry=_FakeSkillRegistry(skills))
        webui._engine = self.engine

    def tearDown(self) -> None:
        webui._engine = self._orig_engine
        if self._orig_token is None:
            os.environ.pop("WEBUI_TOKEN", None)
        else:
            os.environ["WEBUI_TOKEN"] = self._orig_token

    def _make_client(self) -> TestClient:
        app = FastAPI()
        app.include_router(webui.router)
        return TestClient(app)

    def _make_authed_client(self) -> TestClient:
        client = self._make_client()
        auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
        self.assertEqual(auth_res.status_code, 200)
        return client

    def test_memory_summary_returns_layered_counts(self) -> None:
        with self._make_authed_client() as client:
            response = client.get("/api/webui/memory/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["layers"], {"user": 2, "agent": 1, "untrusted": 1})

    def test_memory_records_filters_by_origin_class(self) -> None:
        with self._make_authed_client() as client:
            response = client.get("/api/webui/memory/records", params={"origin_class": "user"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 2)
        self.assertTrue(all(item["origin_class"] == "user" for item in payload["items"]))
        self.assertEqual(payload["items"][0]["content"], "私聊消息")  # ORDER BY id DESC
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["page_size"], 50)

    def test_memory_records_without_origin_class_keeps_existing_logic(self) -> None:
        with self._make_authed_client() as client:
            response = client.get("/api/webui/memory/records")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(len(payload["items"]), 2)

    def test_memory_summary_empty_db_falls_back_to_zeros(self) -> None:
        webui._engine = _FakeEngine(memory=_FakeMemory(Path(self._tmp.name) / "missing.db"))
        with self._make_authed_client() as client:
            response = client.get("/api/webui/memory/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total": 0, "layers": {}})

    def test_memory_records_filtered_empty_db_returns_empty_items(self) -> None:
        webui._engine = _FakeEngine(memory=_FakeMemory(Path(self._tmp.name) / "missing.db"))
        with self._make_authed_client() as client:
            response = client.get("/api/webui/memory/records", params={"origin_class": "agent"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["total"], 0)

    def test_memory_disabled_returns_empty_summary(self) -> None:
        disabled = _FakeMemory(Path(self._tmp.name) / "memory.db")
        disabled.enable_vector_memory = False
        webui._engine = _FakeEngine(memory=disabled)
        with self._make_authed_client() as client:
            response = client.get("/api/webui/memory/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total": 0, "layers": {}})

    def test_skills_returns_registry_items(self) -> None:
        with self._make_authed_client() as client:
            response = client.get("/api/webui/skills")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 2)
        by_name = {item["name"]: item for item in payload["items"]}
        self.assertIn("web-search", by_name)
        self.assertEqual(by_name["web-search"]["description"], "Search the web for answers")
        self.assertEqual(by_name["web-search"]["description_zh"], "联网搜索")
        self.assertFalse(by_name["web-search"]["user_invocable"])
        self.assertIn("duck-skill", by_name)
        self.assertEqual(by_name["duck-skill"]["requires"], {"bins": ["duck"]})

    def test_skills_empty_registry_returns_empty_items(self) -> None:
        webui._engine = _FakeEngine(memory=self.engine.memory, skill_registry=_FakeSkillRegistry([]))
        with self._make_authed_client() as client:
            response = client.get("/api/webui/skills")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "total": 0})

    def test_skills_without_registry_returns_empty_items(self) -> None:
        webui._engine = _FakeEngine(memory=self.engine.memory)
        with self._make_authed_client() as client:
            response = client.get("/api/webui/skills")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "total": 0})

    def test_endpoints_require_auth(self) -> None:
        webui._engine = None
        with self._make_client() as client:
            for path in ("/api/webui/memory/summary", "/api/webui/memory/records", "/api/webui/skills"):
                response = client.get(path)
                self.assertEqual(response.status_code, 401, path)

    def test_endpoints_return_503_when_engine_uninitialized_but_authed(self) -> None:
        webui._engine = None
        with self._make_client() as client:
            auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
            self.assertEqual(auth_res.status_code, 200)
            for path in ("/api/webui/memory/summary", "/api/webui/memory/records", "/api/webui/skills"):
                response = client.get(path)
                self.assertEqual(response.status_code, 503, path)


if __name__ == "__main__":
    unittest.main()
