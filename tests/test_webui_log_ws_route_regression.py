"""回归测试：WebUI /logs/stream 与 NoneBot OneBot ws 不再用 FastAPI APIWebSocketRoute。

FastAPI 0.141 的 `APIWebSocketRoute`（含 `@app.websocket` / `@router.websocket` /
`add_api_websocket_route`）在真实 uvicorn 0.50 下对 ws 升级会提前 close(403)，
TestClient 测不出，只影响真实连接。修复方式与 run_primary 一致：改用 Starlette
`WebSocketRoute`。这两个测试锁路由类型，防止回退到 FastAPI 的 ws 机制。
"""
from __future__ import annotations

import logging
import os
import unittest
from pathlib import Path

import core.webui as webui
from core.nonebot_ws_patch import patch_nonebot_ws_routes
from core.webui_log_routes import build_log_router
from core.webui_route_context import WebUIRouteContext
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect


async def _noop_auth(request):
    return None


async def _noop_ws_auth(ws):
    return True


def _stub_ctx() -> WebUIRouteContext:
    return WebUIRouteContext(
        get_engine=lambda: None,
        get_start_time=lambda: 0.0,
        get_token=lambda: "",
        check_auth=_noop_auth,
        check_ws_auth=_noop_ws_auth,
        set_auth_cookie=lambda *a: None,
        clear_auth_cookie=lambda *a: None,
        count_registered_napcat_tools=lambda: 0,
        collect_napcat_status=lambda bot_id="": {},
        resolve_log_file_path=lambda: Path("/tmp/yukiko-nonexistent.log"),
        resolve_auth_attempt_store_path=lambda: Path("/tmp"),
        read_log_tail=lambda *a: [],
        split_log_chunks=lambda s: [s],
        cookie_capabilities_payload=lambda: {},
        start_bilibili_qr_session=lambda: {},
        bilibili_qr_status=lambda s: {},
        cancel_bilibili_qr_session=lambda s: {},
        logger=logging.getLogger("test"),
    )


class WebuiLogWsRouteRegressionTests(unittest.TestCase):
    def test_log_stream_route_is_starlette_websocket_route(self) -> None:
        router = build_log_router(_stub_ctx())
        ws_routes = [r for r in router.routes if isinstance(r, WebSocketRoute)]
        api_ws_routes = [r for r in router.routes if type(r).__name__ == "APIWebSocketRoute"]
        self.assertTrue(
            any(r.path == "/logs/stream" for r in ws_routes),
            "/logs/stream 应为 Starlette WebSocketRoute",
        )
        self.assertFalse(
            any(r.path == "/logs/stream" for r in api_ws_routes),
            "/logs/stream 不应是 FastAPI APIWebSocketRoute（uvicorn 下 403）",
        )

    def test_log_stream_still_connects_end_to_end(self) -> None:
        self._orig_engine = webui._engine
        self._orig_token = os.environ.get("WEBUI_TOKEN")
        os.environ["WEBUI_TOKEN"] = "test-token"
        webui._engine = None
        try:
            app = FastAPI()
            app.include_router(webui.router)
            with TestClient(app) as client:
                auth_res = client.post("/api/webui/auth", json={"token": "test-token"})
                self.assertEqual(auth_res.status_code, 200)
                with client.websocket_connect("/api/webui/logs/stream") as ws:
                    ws.close()
        finally:
            webui._engine = self._orig_engine
            if self._orig_token is None:
                os.environ.pop("WEBUI_TOKEN", None)
            else:
                os.environ["WEBUI_TOKEN"] = self._orig_token


class NonebotWsPatchRegressionTests(unittest.TestCase):
    def _make_fake_driver(self) -> tuple[FastAPI, object]:
        app = FastAPI()

        @app.websocket("/onebot/v11/ws")
        async def fake_onebot_ws(ws):
            await ws.accept()

        @app.websocket("/ws/other")
        async def fake_other_ws(ws):
            await ws.accept()

        @app.get("/webui/status")
        async def fake_http():
            return {"ok": True}

        class _FakeDriver:
            server_app = app

        return app, _FakeDriver()

    def test_patch_swaps_onebot_ws_routes_to_starlette(self) -> None:
        app, driver = self._make_fake_driver()
        replaced = patch_nonebot_ws_routes(driver)
        self.assertEqual(replaced, 1)

        ws_routes = [r for r in app.router.routes if isinstance(r, WebSocketRoute)]
        api_ws_routes = [r for r in app.router.routes if type(r).__name__ == "APIWebSocketRoute"]
        self.assertTrue(
            any(r.path == "/onebot/v11/ws" for r in ws_routes),
            "OneBot ws 应被换成 Starlette WebSocketRoute",
        )
        self.assertFalse(
            any(r.path.startswith("/onebot/v11") for r in api_ws_routes),
            "OneBot ws 不应残留 APIWebSocketRoute",
        )
        # 非 OneBot 的 ws / HTTP 路由原样保留。
        self.assertTrue(
            any(r.path == "/ws/other" for r in api_ws_routes),
            "非 OneBot 的 ws 路由不应被改动",
        )
        self.assertTrue(
            any(type(r).__name__ == "APIRoute" and r.path == "/webui/status" for r in app.router.routes),
            "HTTP 路由不应被改动",
        )

    def test_patch_preserves_endpoint_callable(self) -> None:
        app, driver = self._make_fake_driver()
        before = next(
            r for r in app.router.routes if r.path == "/onebot/v11/ws"
        ).endpoint
        patch_nonebot_ws_routes(driver)
        after = next(
            r for r in app.router.routes
            if isinstance(r, WebSocketRoute) and r.path == "/onebot/v11/ws"
        ).endpoint
        self.assertIs(after, before, "替换后 endpoint 应保持不变")

    def test_patch_noop_when_no_onebot_ws(self) -> None:
        app = FastAPI()

        @app.websocket("/ws/other")
        async def fake_other_ws(ws):
            await ws.accept()

        class _FakeDriver:
            server_app = app

        replaced = patch_nonebot_ws_routes(_FakeDriver())
        self.assertEqual(replaced, 0)


if __name__ == "__main__":
    unittest.main()
