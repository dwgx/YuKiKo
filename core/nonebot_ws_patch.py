"""NoneBot FastAPI 驱动 ws 路由的 403 修复。

FastAPI 0.141 的 `APIWebSocketRoute` 在真实 uvicorn 0.50 下会对 ws 升级提前
close(403)（与 run_primary 的 `@app.websocket` 同机制，TestClient 测不出）。
NoneBot 的 fastapi driver 内部用 `app.add_api_websocket_route(...)` 挂 OneBot
反向 ws（`nonebot/drivers/fastapi.py` 的 `setup_websocket_server`），同样命中该 bug。

修复方式与 run_primary 一致：`register_adapter()` 之后把 `driver.server_app` 上
`/onebot/v11/*` 的 `APIWebSocketRoute` 换成等价的 Starlette `WebSocketRoute`。
endpoint 签名是 `async def (websocket)`，与 `WebSocketRoute` 的调用约定完全一致，
行为不变；HTTP 路由原样保留。
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("yukiko.nonebot_ws_patch")

_ONEBOT_WS_PREFIX = "/onebot/v11"


def patch_nonebot_ws_routes(driver: Any) -> int:
    """把 driver.server_app 上 OneBot 的 FastAPI ws 路由换成 Starlette WebSocketRoute。

    返回被替换的路由数；0 表示没有需要替换的（例如平台主路径未启用 NoneBot 时不调用）。
    """
    from starlette.routing import WebSocketRoute

    app = driver.server_app
    new_routes: list[Any] = []
    replaced = 0
    for route in list(app.router.routes):
        if (
            type(route).__name__ == "APIWebSocketRoute"
            and getattr(route, "path", "").startswith(_ONEBOT_WS_PREFIX)
        ):
            new_routes.append(WebSocketRoute(route.path, route.endpoint, name=route.name))
            replaced += 1
        else:
            new_routes.append(route)
    if replaced:
        app.router.routes[:] = new_routes
        _log.info("nonebot_ws_route_patch | replaced=%d", replaced)
    return replaced
