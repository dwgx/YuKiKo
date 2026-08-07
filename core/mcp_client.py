"""Phase 3c：MCP 连接器（带信任门）。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.3（4）。三块：

1. `parse_mcp_servers`：从配置解析 mcpServers（command/args/env 或 streamableHttp url）。
2. `MCPTrustStore`：信任门状态机（configured → disconnected → trusted → connected），
   connector_trust.json 持久化。未 trusted 不暴露工具。
3. `McpStdioClient`：最小 stdio JSON-RPC 2.0 客户端（initialize / tools/list / tools/call），
   用换行分隔 JSON 与子进程通信。零第三方依赖。

真正的 MCP server 桥接由上层（MCPConnectorManager）用这三块组合；本模块只做可测的原子能力。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

# ── 配置解析 ──

def parse_mcp_servers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 config 里取 mcpServers，规整成 {name: spec}。

    spec 两种形态：
    - stdio：{"command": str, "args": [str], "env": {str: str}}
    - streamableHttp：{"type": "streamableHttp", "url": str}
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        spec: dict[str, Any] = {}
        command = raw.get("command")
        if isinstance(command, str) and command.strip():
            spec["command"] = command.strip()
            spec["args"] = [str(a) for a in raw.get("args", []) if isinstance(raw.get("args"), list)]
            spec["env"] = {str(k): str(v) for k, v in raw.get("env", {}).items()} if isinstance(raw.get("env"), dict) else {}
        url = raw.get("url")
        if isinstance(url, str) and url.strip():
            spec["type"] = "streamableHttp"
            spec["url"] = url.strip()
        if spec:
            out[str(name)] = spec
    return out


# ── 信任门 ──

class MCPTrustStore:
    """连接器信任门：未 trusted 不暴露工具（WorkBuddy s17 状态机）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._trusted: set[str] = set()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("trusted"), list):
                    self._trusted = {str(t) for t in data["trusted"]}
            except (OSError, json.JSONDecodeError):
                self._trusted = set()

    def is_trusted(self, name: str) -> bool:
        return name in self._trusted

    def trust(self, name: str) -> None:
        self._trusted.add(name)
        self._save()

    def revoke(self, name: str) -> None:
        self._trusted.discard(name)
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"trusted": sorted(self._trusted)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


# ── stdio JSON-RPC 客户端 ──

class McpStdioClient:
    """最小 MCP stdio 客户端（换行分隔 JSON-RPC 2.0）。"""

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._request_timeout = 30.0

    async def connect(self) -> bool:
        # 最小 env 白名单：只传 server 配置的 env + PATH，不注入完整 os.environ
        # （否则第三方 MCP server 可读走 provider key / WEBUI_TOKEN）。
        env = dict(self.env)
        env.setdefault("PATH", os.environ.get("PATH", ""))
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin
        # MCP initialize 握手（协议 2024-11-05）。
        result = await self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "yukiko", "version": "1.0"}})
        return isinstance(result, dict)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            return result["tools"]
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {}

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        if self._writer is None:
            raise RuntimeError("mcp_not_connected")
        req_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, ensure_ascii=False)
        self._writer.write((payload + "\n").encode("utf-8"))
        await self._writer.drain()
        assert self._reader is not None
        # 逐行读直到匹配本请求的 id（并发安全），带总超时防子进程 hang。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._request_timeout
        msg: dict[str, Any] = {}
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError("mcp_request_timeout")
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("mcp_request_timeout") from exc
            if not line:
                raise RuntimeError("mcp_server_closed")
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"mcp_bad_json:{line[:120]!r}") from exc
            if msg.get("id") == req_id:
                break
        if "error" in msg:
            raise RuntimeError(f"mcp_error:{msg.get('error')}")
        return msg.get("result")

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass


class MCPConnectorManager:
    """把 trusted MCP server 的工具桥进 AgentToolRegistry。

    流程：sync() 对每个已信任的 stdio server 连接 → tools/list → 注册为
    `mcp__{connector}__{tool}` 工具（handler 转发 tools/call）。未 trusted 不暴露。
    streamableHttp 暂不接（无客户端实现），跳过。
    """

    def __init__(self, config: dict[str, Any], trust_store: MCPTrustStore, registry: Any) -> None:
        self.servers = parse_mcp_servers(config)
        self.trust_store = trust_store
        self.registry = registry
        self._clients: dict[str, McpStdioClient] = {}

    async def sync(self) -> list[str]:
        """连接所有 trusted stdio server 并注册其工具，返回注册的工具名列表。"""
        registered: list[str] = []
        for name, spec in self.servers.items():
            if not self.trust_store.is_trusted(name):
                continue
            if "command" not in spec:
                continue  # streamableHttp 暂不接
            try:
                client = McpStdioClient(spec["command"], spec.get("args", []), spec.get("env", {}))
                if not await client.connect():
                    continue
                tools = await client.list_tools()
            except Exception:
                continue
            self._clients[name] = client
            registered.extend(self._register_tools(name, client, tools))
        return registered

    def _register_tools(self, connector: str, client: McpStdioClient, tools: list[dict[str, Any]]) -> list[str]:
        from core.agent_tools_types import ToolCallResult, ToolSchema

        registered: list[str] = []
        for tool in tools:
            tool_name = str(tool.get("name", "")).strip()
            if not tool_name:
                continue
            input_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
            mcp_tool_name = f"mcp__{connector}__{tool_name}"

            async def _handler(args: dict[str, Any], context: Any) -> ToolCallResult:
                _ = context
                result = await client.call_tool(tool_name, args)
                text_parts: list[str] = []
                for item in result.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(str(item.get("text", "")))
                display = "\n".join(text_parts) or json.dumps(result, ensure_ascii=False)[:500]
                is_error = bool(result.get("isError"))
                return ToolCallResult(
                    ok=not is_error,
                    display=display,
                    error=f"mcp_error:{result}" if is_error else "",
                    data=result,
                )

            self.registry.register(
                ToolSchema(
                    name=mcp_tool_name,
                    description=f"通过 MCP 连接器 {connector} 调用外部工具 {tool_name}",
                    parameters={
                        "type": "object",
                        "properties": input_schema.get("properties", {}),
                        "required": input_schema.get("required", []),
                    },
                    category="mcp",
                ),
                _handler,
            )
            registered.append(mcp_tool_name)
        return registered

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
