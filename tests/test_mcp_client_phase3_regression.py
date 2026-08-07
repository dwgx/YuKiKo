"""Phase 3c：MCP 连接器回归测试。

锁三件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.3（4））：
1. parse_mcp_servers 解析 stdio / streamableHttp 两种形态。
2. MCPTrustStore 信任门状态机 + JSON 持久化（未 trusted 不暴露）。
3. McpStdioClient 与假 MCP server 的 initialize / tools/list / tools/call 真实交互。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from core.mcp_client import McpStdioClient, MCPTrustStore, parse_mcp_servers

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
        result = {"tools": [{"name": "echo", "description": "echo tool", "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "pong"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}), flush=True)
'''


class ParseMcpServersTests(unittest.TestCase):
    def test_parses_stdio_server(self) -> None:
        config = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                    "env": {"FOO": "1"},
                }
            }
        }
        out = parse_mcp_servers(config)
        self.assertEqual(out["filesystem"]["command"], "npx")
        self.assertEqual(out["filesystem"]["args"], ["-y", "@modelcontextprotocol/server-filesystem"])
        self.assertEqual(out["filesystem"]["env"], {"FOO": "1"})

    def test_parses_streamable_http_server(self) -> None:
        config = {"mcpServers": {"remote": {"type": "streamableHttp", "url": "https://example.com/mcp"}}}
        out = parse_mcp_servers(config)
        self.assertEqual(out["remote"]["type"], "streamableHttp")
        self.assertEqual(out["remote"]["url"], "https://example.com/mcp")

    def test_empty_or_invalid_config(self) -> None:
        self.assertEqual(parse_mcp_servers({}), {})
        self.assertEqual(parse_mcp_servers({"mcpServers": {}}), {})


class MCPTrustStoreTests(unittest.TestCase):
    def test_trust_persists_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trust.json"
            store = MCPTrustStore(path)
            self.assertFalse(store.is_trusted("filesystem"))
            store.trust("filesystem")
            self.assertTrue(store.is_trusted("filesystem"))
            # 重载后仍在（持久化）
            reloaded = MCPTrustStore(path)
            self.assertTrue(reloaded.is_trusted("filesystem"))

    def test_revoke_removes_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trust.json"
            store = MCPTrustStore(path)
            store.trust("a")
            store.revoke("a")
            self.assertFalse(store.is_trusted("a"))

    def test_no_path_is_in_memory_only(self) -> None:
        store = MCPTrustStore()
        store.trust("a")
        self.assertTrue(store.is_trusted("a"))


class McpStdioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_list_tools_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_mcp.py"
            script.write_text(_FAKE_MCP_SERVER, encoding="utf-8")
            client = McpStdioClient(sys.executable, [str(script)])
            try:
                ok = await client.connect()
                self.assertTrue(ok)
                tools = await client.list_tools()
                self.assertEqual([t["name"] for t in tools], ["echo"])
                result = await client.call_tool("echo", {"msg": "hi"})
                self.assertIn("content", result)
                self.assertIn("pong", json.dumps(result))
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
