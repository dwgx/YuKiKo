"""Phase 3c 延伸：MCP 工具桥进 AgentToolRegistry 回归测试。

锁三件事：
1. sync() 把 trusted server 的工具注册为 `mcp__connector__tool`。
2. registry.call 转发到 MCP server，返回结果。
3. 未 trusted / streamableHttp server 不注册。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from core.agent_tools_registry import AgentToolRegistry
from core.mcp_client import MCPConnectorManager, MCPTrustStore

_FAKE_MCP_SERVER = '''\
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echo tool", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "pong:" + str(msg["params"].get("arguments", {}).get("msg", ""))}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}), flush=True)
'''


class MCPConnectorManagerTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, tmp: str, trusted: bool = True) -> tuple[MCPConnectorManager, MCPTrustStore, AgentToolRegistry]:
        script = Path(tmp) / "fake_mcp.py"
        script.write_text(_FAKE_MCP_SERVER, encoding="utf-8")
        config = {"mcpServers": {"fake": {"command": sys.executable, "args": [str(script)]}}}
        trust = MCPTrustStore(Path(tmp) / "trust.json")
        if trusted:
            trust.trust("fake")
        registry = AgentToolRegistry()
        manager = MCPConnectorManager(config, trust, registry)
        return manager, trust, registry

    async def test_sync_registers_mcp_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _, registry = self._setup(tmp)
            registered = await manager.sync()
            self.assertIn("mcp__fake__echo", registered)
            self.assertTrue(registry.has_tool("mcp__fake__echo"))
            try:
                await manager.close()
            except Exception:
                pass

    async def test_registry_call_forwards_to_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _, registry = self._setup(tmp)
            await manager.sync()
            result = await registry.call("mcp__fake__echo", {"msg": "hi"}, {})
            self.assertTrue(result.ok, result.error)
            self.assertIn("pong:hi", result.display)
            try:
                await manager.close()
            except Exception:
                pass

    async def test_untrusted_server_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _, registry = self._setup(tmp, trusted=False)
            registered = await manager.sync()
            self.assertEqual(registered, [])
            self.assertFalse(registry.has_tool("mcp__fake__echo"))


if __name__ == "__main__":
    unittest.main()
