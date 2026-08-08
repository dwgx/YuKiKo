"""O1: 工具运行时卸载 + MCP 重连回归测试。

钉住本任务实现的行为：
- `AgentToolRegistry.unregister(name)` 从 _schemas/_handlers 删除工具：
  从可见集（list_tool_names / list_tools_for_permission / get_schema）消失、
  handler 不再可调（call 返回 unknown_tool）、再 register 同名可恢复；
- `reload_config` 在 MCP 启用时重跑 `MCPConnectorManager.sync()`（后台任务），
  成功后替换 _mcp_manager 并关闭旧连接，失败保留旧连接；未启用 MCP 不动作；
- WebUI 端点：`POST /api/webui/tools/{name}/unregister`（显式卸载）、
  `POST /api/webui/mcp/sync`（重连）—— 均需鉴权，引擎未初始化返回 503。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import core.webui as webui
from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_types import ToolCallResult, ToolSchema
from core.audit import AuditTrail
from core.mcp_client import MCPTrustStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import make_engine


def _make_handler(ok: bool = True, display: str = "ok"):
    async def handler(args, context) -> ToolCallResult:
        _ = (args, context)
        return ToolCallResult(ok=ok, data={}, display=display)

    return handler


def _make_reloadable_engine(tmp_path: Path, config=None):
    """make_engine + reload_config 所需的补充接线（同 test_hot_reload_components_regression）。"""
    engine = make_engine(config=config)
    engine.project_root = Path(tmp_path)
    engine.config_dir = Path(tmp_path) / "config"
    engine.storage_dir = Path(tmp_path) / "storage"
    engine.audit = AuditTrail(Path(tmp_path) / "audit", enable=False)
    engine.image = None
    engine.plugins = SimpleNamespace(load=lambda global_config: None)
    engine.agent_tool_registry = AgentToolRegistry()
    engine.config_manager = SimpleNamespace(
        reload=lambda: (True, "ok"),
        raw=engine.config,
    )
    return engine


class RegistryUnregisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = AgentToolRegistry()

    def _register_demo(self) -> None:
        self.registry.register(
            ToolSchema(
                name="demo_tool",
                description="演示工具",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
            _make_handler(display="done"),
        )

    def test_unregister_removes_tool_from_all_visible_sets(self) -> None:
        self._register_demo()
        self.assertIn("demo_tool", self.registry.list_tool_names())
        self.assertIn("demo_tool", self.registry.list_tools_for_permission("user"))
        self.assertIsNotNone(self.registry.get_schema("demo_tool"))
        self.assertTrue(self.registry.has_tool("demo_tool"))
        self.assertTrue(
            any(
                t.get("function", {}).get("name") == "demo_tool"
                for t in self.registry.get_schemas_for_native_tools(["demo_tool"])
            )
        )

        removed = self.registry.unregister("demo_tool")
        self.assertTrue(removed, "存在且删除应返回 True")
        self.assertNotIn("demo_tool", self.registry.list_tool_names())
        self.assertNotIn("demo_tool", self.registry.list_tools_for_permission("user"))
        self.assertIsNone(self.registry.get_schema("demo_tool"))
        self.assertFalse(self.registry.has_tool("demo_tool"))
        self.assertEqual(self.registry.get_schemas_for_native_tools(["demo_tool"]), [])
        self.assertEqual(self.registry.tool_count, 0)

    def test_call_after_unregister_returns_unknown_tool(self) -> None:
        self._register_demo()
        result = asyncio.run(
            self.registry.call("demo_tool", {}, {"permission_level": "user"})
        )
        self.assertTrue(result.ok)
        self.registry.unregister("demo_tool")
        result = asyncio.run(
            self.registry.call("demo_tool", {}, {"permission_level": "user"})
        )
        self.assertFalse(result.ok)
        self.assertIn("unknown_tool", result.error)

    def test_unregister_unknown_tool_returns_false(self) -> None:
        self.assertFalse(self.registry.unregister("no_such_tool"))
        self.assertFalse(self.registry.unregister(""))
        self.assertFalse(self.registry.unregister(None))

    def test_reregister_restores_tool(self) -> None:
        self._register_demo()
        self.assertTrue(self.registry.unregister("demo_tool"))
        self._register_demo()
        self.assertIn("demo_tool", self.registry.list_tool_names())
        self.assertTrue(self.registry.has_tool("demo_tool"))
        result = asyncio.run(
            self.registry.call("demo_tool", {}, {"permission_level": "user"})
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.display, "done")

    def test_unregister_keeps_other_tools_intact(self) -> None:
        self._register_demo()
        self.registry.register(
            ToolSchema(name="other_tool", description="其他工具"),
            _make_handler(display="other"),
        )
        self.assertTrue(self.registry.unregister("demo_tool"))
        self.assertIn("other_tool", self.registry.list_tool_names())
        result = asyncio.run(
            self.registry.call("other_tool", {}, {"permission_level": "user"})
        )
        self.assertTrue(result.ok)


class EngineMcpResyncReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="yukiko-mcp-resync-")

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def _make_engine(self, mcp_enabled: bool = True) -> SimpleNamespace:
        config = None
        if mcp_enabled:
            config = {
                "mcp": {
                    "enabled": True,
                    "servers": {"demo": {"command": "echo", "args": ["hi"]}},
                }
            }
        return _make_reloadable_engine(Path(self._tmp_dir.name), config=config)

    def _manager_mock(self, sync_result=None, sync_side_effect=None):
        manager = mock.MagicMock()
        manager.sync = mock.AsyncMock(return_value=sync_result, side_effect=sync_side_effect)
        manager.close = mock.AsyncMock()
        return manager

    async def test_reload_config_resyncs_mcp_when_enabled(self) -> None:
        engine = self._make_engine(mcp_enabled=True)
        with mock.patch("core.engine.MCPConnectorManager") as mcp_cls:
            manager = self._manager_mock(sync_result=["mcp__demo__echo"])
            mcp_cls.return_value = manager
            ok, msg = engine.reload_config()
            self.assertTrue(ok, msg)
            await asyncio.sleep(0)  # 让后台 resync 任务跑完
            manager.sync.assert_awaited_once()
            mcp_cls.assert_called_once()
            call_args = mcp_cls.call_args.args
            self.assertEqual(
                call_args[0], {"mcpServers": {"demo": {"command": "echo", "args": ["hi"]}}}
            )
            self.assertIsInstance(call_args[1], MCPTrustStore)
            self.assertIs(call_args[2], engine.agent_tool_registry)
            self.assertIs(engine._mcp_manager, manager)
            manager.close.assert_not_awaited()

    async def test_reload_config_swaps_manager_and_closes_old(self) -> None:
        engine = self._make_engine(mcp_enabled=True)
        first = self._manager_mock(sync_result=["mcp__demo__echo"])
        second = self._manager_mock(sync_result=["mcp__demo__echo"])
        with mock.patch("core.engine.MCPConnectorManager") as mcp_cls:
            mcp_cls.side_effect = [first, second]
            ok, _ = engine.reload_config()
            self.assertTrue(ok)
            await asyncio.sleep(0)
            self.assertIs(engine._mcp_manager, first)

            ok, _ = engine.reload_config()
            self.assertTrue(ok)
            await asyncio.sleep(0)
            self.assertIs(engine._mcp_manager, second)
            first.close.assert_awaited_once()
            second.close.assert_not_awaited()

    async def test_reload_config_keeps_old_manager_on_sync_failure(self) -> None:
        engine = self._make_engine(mcp_enabled=True)
        first = self._manager_mock(sync_result=["mcp__demo__echo"])
        failing = self._manager_mock(sync_side_effect=RuntimeError("boom"))
        with mock.patch("core.engine.MCPConnectorManager") as mcp_cls:
            mcp_cls.side_effect = [first, failing]
            ok, _ = engine.reload_config()
            self.assertTrue(ok)
            await asyncio.sleep(0)
            self.assertIs(engine._mcp_manager, first)

            ok, _ = engine.reload_config()
            self.assertTrue(ok, "sync 失败不应让 reload 失败")
            await asyncio.sleep(0)
            self.assertIs(engine._mcp_manager, first, "失败应保留旧连接")
            first.close.assert_not_awaited()

    async def test_reload_config_skips_mcp_when_disabled(self) -> None:
        engine = self._make_engine(mcp_enabled=False)
        with mock.patch("core.engine.MCPConnectorManager") as mcp_cls:
            ok, msg = engine.reload_config()
            self.assertTrue(ok, msg)
            await asyncio.sleep(0)
            mcp_cls.assert_not_called()


class WebuiToolRuntimeEndpointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_engine = webui._engine
        self._orig_token = os.environ.get("WEBUI_TOKEN")
        os.environ["WEBUI_TOKEN"] = "test-token"
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

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

    def _make_engine_with_registry(self) -> SimpleNamespace:
        registry = AgentToolRegistry()
        registry.register(
            ToolSchema(name="demo_tool", description="演示工具"),
            _make_handler(display="done"),
        )
        return SimpleNamespace(agent_tool_registry=registry)

    def test_unregister_endpoint_removes_tool(self) -> None:
        engine = self._make_engine_with_registry()
        webui._engine = engine
        with self._make_authed_client() as client:
            response = client.post("/api/webui/tools/demo_tool/unregister")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        registry = engine.agent_tool_registry
        self.assertFalse(registry.has_tool("demo_tool"))
        self.assertNotIn("demo_tool", registry.list_tool_names())

    def test_unregister_unknown_tool_returns_404(self) -> None:
        webui._engine = self._make_engine_with_registry()
        with self._make_authed_client() as client:
            response = client.post("/api/webui/tools/no_such_tool/unregister")
        self.assertEqual(response.status_code, 404)

    def test_unregister_requires_auth(self) -> None:
        webui._engine = self._make_engine_with_registry()
        with self._make_client() as client:
            response = client.post("/api/webui/tools/demo_tool/unregister")
        self.assertEqual(response.status_code, 401)

    def test_unregister_engine_uninitialized_returns_503(self) -> None:
        webui._engine = None
        with self._make_authed_client() as client:
            response = client.post("/api/webui/tools/demo_tool/unregister")
        self.assertEqual(response.status_code, 503)

    def test_unregister_missing_registry_returns_503(self) -> None:
        webui._engine = SimpleNamespace(agent_tool_registry=None)
        with self._make_authed_client() as client:
            response = client.post("/api/webui/tools/demo_tool/unregister")
        self.assertEqual(response.status_code, 503)

    def test_mcp_sync_endpoint_triggers_resync(self) -> None:
        calls: list[bool] = []

        def fake_resync():
            calls.append(True)
            return None

        webui._engine = SimpleNamespace(_resync_mcp_connectors=fake_resync)
        with self._make_authed_client() as client:
            response = client.post("/api/webui/mcp/sync")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(calls, [True])

    def test_mcp_sync_endpoint_awaits_resync_task(self) -> None:
        awaited = asyncio.Event()

        def fake_resync():
            return asyncio.create_task(_mark_done(awaited))

        webui._engine = SimpleNamespace(_resync_mcp_connectors=fake_resync)
        with self._make_authed_client() as client:
            response = client.post("/api/webui/mcp/sync")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(awaited.is_set(), "端点应等待 resync 任务完成")

    def test_mcp_sync_requires_auth(self) -> None:
        webui._engine = SimpleNamespace(_resync_mcp_connectors=lambda: None)
        with self._make_client() as client:
            response = client.post("/api/webui/mcp/sync")
        self.assertEqual(response.status_code, 401)

    def test_mcp_sync_engine_uninitialized_returns_503(self) -> None:
        webui._engine = None
        with self._make_authed_client() as client:
            response = client.post("/api/webui/mcp/sync")
        self.assertEqual(response.status_code, 503)

    def test_mcp_sync_without_resync_support_returns_503(self) -> None:
        webui._engine = SimpleNamespace()
        with self._make_authed_client() as client:
            response = client.post("/api/webui/mcp/sync")
        self.assertEqual(response.status_code, 503)


async def _mark_done(event: asyncio.Event) -> None:
    event.set()


if __name__ == "__main__":
    unittest.main()
