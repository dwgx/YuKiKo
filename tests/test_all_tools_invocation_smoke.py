"""真调用每一个注册工具，只断言「不是我们自己代码崩的」。

## 为什么需要这个（2026-08-06）

`tests/test_platform_tool_smoke.py` 有 14 条测试，但**几乎全是元数据检查**
（description / category / handler / 模板是否存在），全文只有约 7 次真正的
`registry.call()`。167 个注册工具因此基本没有执行覆盖。

代价已经付过：本轮修掉 7 个 F821 运行时炸弹，其中
`core/tools_github.py` 漏 import httpx 让**所有** GitHub 工具第一次请求就 NameError，
而 1241 条测试全绿。`ruff --select F821` 基线上早就报了，没人看。

本文件补的是那个洞：把每个工具真的调一遍，看它会不会因为**我们自己的代码**炸。

## 判据：什么算失败

只有这几类异常算真缺陷 —— 它们说明代码本身坏了，与环境无关：

* `NameError` / `UnboundLocalError` —— 符号不存在（F821 那一族的运行时形态）
* `AttributeError` —— 拿模块/对象上不存在的东西
* `TypeError` —— 调用签名不对
* `IndentationError` / `SyntaxError` —— 不该发生但要兜住

**不算失败**的：任何 `ToolCallResult(ok=False)`。缺凭证、没有 NapCat、没有
model client、没配 API base，都是这台机器的环境限制，不是 bug。
所以本文件对「业务失败」完全宽容 —— 它只钉「崩」与「不崩」。

## 桩的边界

context 用 `core/agent.py` 的 `_build_tool_context` 作为键名真相源。
桩缺键导致的崩不算缺陷 —— 那是测试的错。所以这里显式覆盖那份键列表，
并有一条自检测试钉住「桩提供的键不少于 _build_tool_context 产出的键」，
免得 handler 新读一个键时本文件静默失去覆盖。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from core.agent import AgentContext, AgentLoop
from core.agent_tools_registry import AgentToolRegistry, register_builtin_tools
from core.agent_tools_utility import register_sticker_tools

# 这些异常说明是我们自己的代码坏了，与环境无关。
_CODE_DEFECT_EXCEPTIONS = (
    NameError,          # 含 UnboundLocalError（它是 NameError 子类）
    AttributeError,
    TypeError,
    SyntaxError,        # 含 IndentationError
)

# 真实感的参数值。用真 QQ 号形状是为了过 handler 自己的 id 校验，
# 从而让执行真的走进 handler 主体，而不是在入口就被挡回来。
_GROUP_ID = 974118886
_USER_ID = 136666451


def _build_registry() -> AgentToolRegistry:
    registry = AgentToolRegistry()
    register_builtin_tools(registry, None, None, None, {})
    try:
        register_sticker_tools(registry, None)
    except Exception:  # noqa: BLE001 - 缺 sticker_manager 时跳过，不影响主体覆盖
        pass
    return registry


def _plausible_value(name: str, spec: dict[str, Any]) -> Any:
    """按 schema 造一个「形状可信」的值。"""

    kind = spec.get("type")
    lowered = name.lower()

    if kind == "integer" or kind == "number":
        if "group" in lowered:
            return _GROUP_ID
        if "user" in lowered or "uin" in lowered or "qq" in lowered:
            return _USER_ID
        if "message_id" in lowered or "msg_id" in lowered:
            return 1654940768
        if "duration" in lowered or "second" in lowered or "time" in lowered:
            return 60
        if "limit" in lowered or "count" in lowered or "rows" in lowered:
            return 3
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        item = spec.get("items") or {}
        return [_plausible_value(name, item if isinstance(item, dict) else {})]
    if kind == "object":
        return {}

    # string 及未声明类型
    if "url" in lowered:
        return "https://www.bilibili.com/video/BV1GJ411x7h7"
    if "group" in lowered:
        return str(_GROUP_ID)
    if "user" in lowered or "uin" in lowered:
        return str(_USER_ID)
    if "file" in lowered or "path" in lowered:
        return "storage/cache/nonexistent-probe.bin"
    if "keyword" in lowered or "query" in lowered or "text" in lowered or "content" in lowered:
        return "测试关键词"
    if "name" in lowered:
        return "测试"
    return "测试"


def _args_for(spec: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(spec, dict):
        return {}
    props = spec.get("properties")
    if not isinstance(props, dict):
        return {}
    required = spec.get("required")
    keys = list(required) if isinstance(required, list) and required else list(props)
    out: dict[str, Any] = {}
    for key in keys:
        sub = props.get(key)
        out[key] = _plausible_value(str(key), sub if isinstance(sub, dict) else {})
    return out


class _RecordingApiCall:
    """记录调用并返回形状可信的 NapCat 响应，让 handler 能走进主体。

    绝不做真实副作用 —— 所有 set_/send_/delete_ 都只被记录。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((action, kwargs))
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "group_id": _GROUP_ID,
                "user_id": _USER_ID,
                "message_id": 1,
                "messages": [],
                "nickname": "测试",
                "card": "测试",
                "role": "member",
                "group_name": "测试群",
                "member_count": 10,
                "max_member_count": 200,
                "friends": [],
                "groups": [],
                "notices": [],
                "files": [],
                "url": "https://example.com/a.jpg",
            },
        }


def _stub_context(api_call: _RecordingApiCall) -> dict[str, Any]:
    """用 `_build_tool_context` 的键列表当真相源，缺一个就可能假失败。"""

    ctx = AgentContext(
        conversation_id=f"group:{_GROUP_ID}",
        user_id=str(_USER_ID),
        user_name="测试用户",
        group_id=_GROUP_ID,
        bot_id="2488687937",
        is_private=False,
        mentioned=True,
        message_text="测试消息",
    )
    ctx.api_call = api_call  # type: ignore[assignment]
    ctx.trace_id = "smoke-trace"

    loop = AgentLoop.__new__(AgentLoop)
    loop.config = {}  # type: ignore[attr-defined]
    built = AgentLoop._build_tool_context(loop, ctx, "super_admin")
    return dict(built)


def _invoke_all() -> tuple[dict[str, BaseException], int, int]:
    """返回 (工具名 -> 代码级异常, 调用总数, 业务失败数)。"""

    registry = _build_registry()
    api_call = _RecordingApiCall()
    context = _stub_context(api_call)

    crashes: dict[str, str] = {}
    invoked = 0
    business_failures = 0

    async def run() -> None:
        nonlocal invoked, business_failures
        for name in sorted(registry._schemas):
            schema = registry._schemas[name]
            args = _args_for(getattr(schema, "parameters", None))
            invoked += 1
            try:
                result = await asyncio.wait_for(
                    registry.call(name, args, dict(context)), timeout=20
                )
            except _CODE_DEFECT_EXCEPTIONS as exc:
                # 理论上到不了这里（registry.call 内部全捕获），但万一它改了行为，
                # 这条分支要在，不能把异常漏成通过。
                crashes[name] = f"{type(exc).__name__}: {exc}"
                continue
            except asyncio.TimeoutError:
                business_failures += 1
                continue
            except Exception as exc:  # noqa: BLE001
                business_failures += 1
                _ = exc
                continue

            if result is None:
                continue
            error_text = str(getattr(result, "error", "") or "")
            defect = _classify_error_text(error_text)
            if defect:
                crashes[name] = defect
            elif not getattr(result, "ok", True):
                business_failures += 1

    asyncio.run(run())
    return crashes, invoked, business_failures


def _classify_error_text(error_text: str) -> str:
    """从 `ToolCallResult.error` 里认出代码级异常。

    **这是本文件的关键判据，别改成靠 `except` 捕获。**
    `AgentToolRegistry.call`（core/agent_tools_registry.py:586）用
    `except Exception` 把 handler 的一切异常吞掉，转成
    `error=f"tool_exception: {type(exc).__name__}: {exc}"` 再返回。

    第一版这里写的是 `except _CODE_DEFECT_EXCEPTIONS`，于是注入一个真 NameError
    到 send_group_message 之后，harness **什么都没抓到** —— 对它专门要防的那类
    bug 完全无效的假绿灯。`test_harness_detects_an_injected_code_defect` 钉住这件事。
    """

    if not error_text.startswith("tool_exception:"):
        return ""
    for exc_name in (
        "NameError",
        "UnboundLocalError",
        "AttributeError",
        "TypeError",
        "SyntaxError",
        "IndentationError",
        "KeyError",
    ):
        if f"tool_exception: {exc_name}:" in error_text:
            # AttributeError on None 通常是本机缺依赖（没有 search_engine / model_client），
            # 不是代码缺陷。只有非 None 目标才算。
            if exc_name == "AttributeError" and "'NoneType' object has no attribute" in error_text:
                return ""
            return error_text[:200]
    return ""


class EveryToolCanBeInvokedWithoutCodeDefectTests(unittest.TestCase):
    """核心：真调用 167 个工具，不许有代码级异常。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.crashes, cls.invoked, cls.business_failures = _invoke_all()

    def test_a_meaningful_number_of_tools_were_invoked(self) -> None:
        """自检：注册链断了本文件会变成空转绿灯。"""

        self.assertGreaterEqual(
            self.invoked,
            120,
            f"只调用了 {self.invoked} 个工具 —— 注册链可能断了，"
            "本文件失去覆盖（应有 160+）",
        )

    def test_no_tool_raises_a_code_level_exception(self) -> None:
        rendered = "\n".join(
            f"  {name}: {type(exc).__name__}: {exc}"
            for name, exc in sorted(self.crashes.items())
        )
        self.assertEqual(
            self.crashes,
            {},
            "这些工具一调用就抛代码级异常（NameError/AttributeError/TypeError 一族）——"
            "handler 的 except 会把它包成像业务失败的话，线上看不出是 bug:\n"
            f"{rendered}\n"
            "注意：ok=False 不算失败，本测试只钉「崩」。",
        )

    def test_business_failures_are_tolerated(self) -> None:
        """说明性：本机没有凭证/NapCat/模型，大量 ok=False 是正常的。

        这条不断言数量，只保证前一条不是因为「全部都崩了所以没有业务失败」
        这种诡异状态而通过。
        """

        self.assertGreater(
            self.business_failures + len(self.crashes),
            0,
            "一个失败都没有反而可疑 —— 本机没有凭证，不该全部成功",
        )


class HarnessSelfCheckTests(unittest.TestCase):
    """证明这个 harness 真能抓到它要防的那类 bug。

    第一版靠 `except NameError` 捕获，而 `AgentToolRegistry.call` 内部
    `except Exception` 全吞了 —— 注入真 NameError 后 harness 一个都没抓到。
    没有这条自检，那个假绿灯会一直绿着。
    """

    def test_harness_detects_an_injected_code_defect(self) -> None:
        registry = _build_registry()
        api_call = _RecordingApiCall()
        context = _stub_context(api_call)

        async def broken_handler(args: dict[str, Any], context: dict[str, Any]) -> Any:
            _ = (args, context)
            return undefined_symbol_like_httpx.AsyncClient()  # noqa: F821

        target = "send_group_message"
        self.assertIn(target, registry._handlers, "注入目标不存在，本自检需要跟着改")
        registry._handlers[target] = broken_handler

        async def probe() -> str:
            result = await registry.call(
                target, {"group_id": _GROUP_ID, "message": "x"}, dict(context)
            )
            return str(getattr(result, "error", "") or "")

        error_text = asyncio.run(probe())
        self.assertTrue(
            _classify_error_text(error_text),
            "注入了真 NameError，判据却没认出来 —— harness 是空转的绿灯。"
            f"实际 error={error_text!r}",
        )

    def test_classifier_ignores_environment_failures(self) -> None:
        """反向：本机缺依赖不该被报成代码缺陷，否则这个文件永远红。"""

        for benign in (
            "",
            "invalid_args:missing group_id",
            "permission_denied:need_super_admin",
            "tool_exception: AttributeError: 'NoneType' object has no attribute 'search'",
            "search_download_resources_error:...",
        ):
            with self.subTest(benign=benign):
                self.assertEqual(_classify_error_text(benign), "")

    def test_classifier_flags_real_defect_shapes(self) -> None:
        for bad in (
            "tool_exception: NameError: name 'httpx' is not defined",
            "tool_exception: UnboundLocalError: local variable 'x' referenced",
            "tool_exception: TypeError: takes 2 positional arguments but 3 were given",
            "tool_exception: AttributeError: module 'base64' has no attribute 'b64dec'",
        ):
            with self.subTest(bad=bad):
                self.assertTrue(_classify_error_text(bad), bad)


class StubContextCoversWhatProductionSuppliesTests(unittest.TestCase):
    """桩的键必须覆盖 `_build_tool_context` 的产出。

    handler 新读一个 context 键时，如果桩没有它，上面的测试会把「桩不全」
    误报成「工具崩了」；反过来如果我手写键列表，漏了就静默失去覆盖。
    所以直接用 `_build_tool_context` 当真相源，并在这里钉住这件事。
    """

    def test_stub_uses_build_tool_context_as_source_of_truth(self) -> None:
        api_call = _RecordingApiCall()
        stub = _stub_context(api_call)
        for key in ("api_call", "output_filter", "topic_gate", "permission_level"):
            with self.subTest(key=key):
                self.assertIn(key, stub, f"桩缺 {key} —— 会把桩的问题误报成工具缺陷")

    def test_recording_api_call_performs_no_real_side_effect(self) -> None:
        """桩必须只记录不真发 —— 否则这个测试会真的去封人。"""

        api_call = _RecordingApiCall()
        asyncio.run(api_call("set_group_ban", group_id=1, user_id=2))
        self.assertEqual(api_call.calls, [("set_group_ban", {"group_id": 1, "user_id": 2})])


class ArgumentBuilderSanityTests(unittest.TestCase):
    """参数构造器本身要对，否则工具会在入口就被挡回来，覆盖变成假的。"""

    def test_group_id_gets_an_integer_group_shaped_value(self) -> None:
        self.assertEqual(
            _plausible_value("group_id", {"type": "integer"}), _GROUP_ID
        )

    def test_url_gets_a_real_looking_url(self) -> None:
        value = _plausible_value("url", {"type": "string"})
        self.assertTrue(str(value).startswith("http"))

    def test_required_fields_are_all_present(self) -> None:
        spec = {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer"},
                "message": {"type": "string"},
                "optional_extra": {"type": "string"},
            },
            "required": ["group_id", "message"],
        }
        args = _args_for(spec)
        self.assertIn("group_id", args)
        self.assertIn("message", args)

    def test_schema_without_properties_yields_empty_args(self) -> None:
        self.assertEqual(_args_for({"type": "object"}), {})
        self.assertEqual(_args_for(None), {})


if __name__ == "__main__":
    unittest.main()
