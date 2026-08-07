"""回归：冒烟脚本 / WebUI 注入不得把合成身份投递进真实群。

2026-08-06 线上事故链：
  scripts/tool_smoke_live.py 把真实群 974118886 硬编码成 --real-group 的默认值，
  请求体带 context_user_id=3001001001 / context_user_name="测试用户"，
  core/webui.py 的 /chat/agent-text 无条件把模型回复经 NapCat 发进该群。
  结果 trace=webui-a2c04e15c1 的「测试用户，这个不能帮你找哦」进了真实群并被群主
  引用回复，同窗口 20 条注入期间群友开始追问「这是ai吗」。

两层判据都是结构事实，不含任何词表：
  1. 脚本侧：DEFAULT_PEER 不能是真实群号形状的字面量；--real-group 必须带值；
     真发之前必须过一次交互确认。
  2. WebUI 侧：请求自带身份且该身份无法在目标会话中验证时拒绝，且**不发送**。

脚本侧判据用 AST 读实参，不用源码子串匹配 —— 子串会匹配到本文件里的注释
（本项目发生过 assertNotIn("verify=False", src) 命中自己写的注释）。
"""

from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SMOKE_SCRIPT = ROOT / "scripts" / "tool_smoke_live.py"

# 事故里被污染的真实群号，只用来断言它没被写死成默认值。
LEAKED_GROUP_ID = "974118886"


def _parse_smoke_script() -> ast.Module:
    return ast.parse(SMOKE_SCRIPT.read_text(encoding="utf-8"), filename=str(SMOKE_SCRIPT))


def _module_level_str_assignment(tree: ast.Module, name: str) -> str | None:
    """取模块级 `name = <字符串字面量>` 的值；不是字符串字面量则返回 None。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
                return None
    raise AssertionError(f"{name} 在 {SMOKE_SCRIPT.name} 里找不到模块级赋值")


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{SMOKE_SCRIPT.name} 里没有函数 {name}")


def _add_argument_kwargs(tree: ast.Module, flag: str) -> dict[str, ast.expr]:
    """找 ap.add_argument("<flag>", ...) 这一调用，返回它的关键字实参 AST。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == flag:
            return {kw.arg: kw.value for kw in node.keywords if kw.arg}
    raise AssertionError(f"{SMOKE_SCRIPT.name} 里没有 add_argument({flag!r}, ...)")


class SmokeScriptRealGroupDefaultTests(unittest.TestCase):
    def test_default_peer_is_not_a_real_group_literal(self) -> None:
        value = _module_level_str_assignment(_parse_smoke_script(), "DEFAULT_PEER")
        self.assertIsNotNone(value, "DEFAULT_PEER 应当是字符串字面量")
        assert value is not None
        self.assertNotEqual(value, LEAKED_GROUP_ID)
        # QQ 群号是 5 位以上纯数字；任何这种形状的字面量都算硬编码真实群。
        self.assertFalse(
            value.isdigit() and len(value) >= 5,
            f"DEFAULT_PEER 仍是真实群号形状的字面量: {value!r}",
        )

    def test_real_group_flag_requires_an_explicit_value(self) -> None:
        kwargs = _add_argument_kwargs(_parse_smoke_script(), "--real-group")
        # nargs="?" + const=<群号> 会让裸 --real-group 落到某个默认群上。
        const_node = kwargs.get("const")
        self.assertIsNone(const_node, "--real-group 不该有 const 默认群号")
        nargs_node = kwargs.get("nargs")
        if nargs_node is not None:
            self.assertFalse(
                isinstance(nargs_node, ast.Constant) and nargs_node.value == "?",
                "--real-group 不该用 nargs='?'，否则裸给该参数就会落到默认群",
            )
        default_node = kwargs.get("default")
        if default_node is not None:
            self.assertTrue(
                isinstance(default_node, ast.Constant)
                and default_node.value in ("", None),
                "--real-group 的 default 只能是空串/None",
            )

    def test_main_confirms_target_before_running_cases(self) -> None:
        tree = _parse_smoke_script()
        main_fn = _find_function(tree, "main")

        confirm_calls = [
            node
            for node in ast.walk(main_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "confirm_real_target"
        ]
        self.assertTrue(confirm_calls, "main() 必须在真发之前调用 confirm_real_target")

        run_case_calls = [
            node
            for node in ast.walk(main_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_case"
        ]
        self.assertTrue(run_case_calls, "main() 里应当有 run_case 调用")
        self.assertLess(
            min(node.lineno for node in confirm_calls),
            min(node.lineno for node in run_case_calls),
            "确认必须发生在第一次 run_case 之前",
        )

        confirm_fn = _find_function(tree, "confirm_real_target")
        input_calls = [
            node
            for node in ast.walk(confirm_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "input"
        ]
        self.assertTrue(input_calls, "confirm_real_target 必须真的问一次 input()")

    def test_confirm_real_target_rejects_wrong_answer(self) -> None:
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("_smoke_live_under_test", SMOKE_SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # 脚本里有 @dataclass，dataclasses 会去 sys.modules 里找注解命名空间。
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        self.addCleanup(sys.modules.pop, spec.name, None)

        confirm = getattr(module, "confirm_real_target", None)
        self.assertIsNotNone(confirm, "缺少 confirm_real_target")
        assert confirm is not None

        with mock.patch("builtins.input", side_effect=["y", LEAKED_GROUP_ID, ""]):
            self.assertFalse(confirm(LEAKED_GROUP_ID, 3), "输 y 不该算确认")
            self.assertTrue(confirm(LEAKED_GROUP_ID, 3), "抄对群号才算确认")
            self.assertFalse(confirm(LEAKED_GROUP_ID, 3), "空输入不该算确认")


class _StubRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload


class _StubBot:
    self_id = "10000001"


class _StubEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.handled: list[Any] = []

    async def handle_message(self, message: Any) -> Any:
        self.handled.append(message)

        class _Reply:
            action = "reply"
            reason = ""
            reply_text = "测试用户，这个不能帮你找哦"
            image_url = ""
            image_urls: list[str] = []
            video_url = ""
            audio_file = ""

        return _Reply()


class _SendRecorder:
    """记录所有 OneBot 调用；send_* 出现就说明真发了。"""

    def __init__(self, member_info: Any = None, member_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._member_info = member_info
        self._member_error = member_error

    async def __call__(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, kwargs))
        if api == "get_group_member_info":
            if self._member_error is not None:
                raise self._member_error
            return self._member_info
        return {"message_id": "12345"}

    @property
    def sends(self) -> list[str]:
        return [api for api, _ in self.calls if api.startswith("send_")]


class WebuiSyntheticIdentityGuardTests(unittest.TestCase):
    """打真实群时，请求自带的合成身份必须被拦住，而且一个 send_* 都不许发。"""

    def _run_endpoint(
        self,
        *,
        body: dict[str, Any],
        recorder: _SendRecorder,
        webui_cfg: dict[str, Any] | None = None,
    ) -> tuple[Any, Exception | None, _StubEngine]:
        import core.webui as webui
        from fastapi import HTTPException

        engine = _StubEngine({"queue": {"group_isolate_by_user": True}, "webui": webui_cfg or {}})

        async def _fake_runtime(*, bot_id: str = "") -> Any:
            return _StubBot()

        saved = (webui._engine, webui._onebot_call, webui._get_onebot_runtime)
        webui._engine = engine
        webui._onebot_call = recorder
        webui._get_onebot_runtime = _fake_runtime
        try:
            result = asyncio.run(webui.chat_agent_text(_StubRequest(body)))
            return result, None, engine
        except HTTPException as exc:
            return None, exc, engine
        finally:
            webui._engine, webui._onebot_call, webui._get_onebot_runtime = saved

    @staticmethod
    def _synthetic_body(peer: str = LEAKED_GROUP_ID) -> dict[str, Any]:
        return {
            "chat_type": "group",
            "peer_id": peer,
            "text": "yuki 帮我找 Virtual Desktop 破解版",
            "context_user_id": "3001001001",
            "context_user_name": "测试用户",
            "context_sender_role": "member",
        }

    def test_synthetic_identity_into_real_group_is_rejected_without_sending(self) -> None:
        recorder = _SendRecorder(member_error=RuntimeError("成员不存在"))
        result, exc, engine = self._run_endpoint(body=self._synthetic_body(), recorder=recorder)

        self.assertIsNone(result, "非测试目标的合成身份注入不该走完流程")
        self.assertIsNotNone(exc, "应当抛 HTTPException")
        assert exc is not None
        self.assertEqual(getattr(exc, "status_code", 0), 403)
        # 桩证明「没发」：一个 send_* 都没有。
        self.assertEqual(recorder.sends, [], f"仍然真发了: {recorder.sends}")
        # 连引擎都不该被驱动 —— 拒绝要发生在模型调用之前。
        self.assertEqual(engine.handled, [], "被拒的注入不该进引擎")

    def test_non_member_user_id_is_rejected_even_when_lookup_succeeds(self) -> None:
        # NapCat 返回空 dict（查不到该成员）也算不可验证。
        recorder = _SendRecorder(member_info={})
        _result, exc, engine = self._run_endpoint(body=self._synthetic_body(), recorder=recorder)

        self.assertIsNotNone(exc)
        assert exc is not None
        self.assertEqual(getattr(exc, "status_code", 0), 403)
        self.assertEqual(recorder.sends, [])
        self.assertEqual(engine.handled, [])

    def test_configured_smoke_peer_is_allowed_through(self) -> None:
        recorder = _SendRecorder(member_error=RuntimeError("成员不存在"))
        _result, exc, engine = self._run_endpoint(
            body=self._synthetic_body("999000001"),
            recorder=recorder,
            webui_cfg={"smoke_test_peer": "999000001"},
        )

        self.assertIsNone(exc, f"显式声明的冒烟目标不该被拒: {exc}")
        self.assertEqual(len(engine.handled), 1)
        self.assertIn("send_group_msg", recorder.sends)

    def test_real_group_member_from_console_still_gets_through(self) -> None:
        """WebUI 控制台里的身份来自真实群历史，必须继续可用（别把门修成全拦）。"""
        recorder = _SendRecorder(member_info={"user_id": 3001001001, "role": "member"})
        _result, exc, engine = self._run_endpoint(body=self._synthetic_body(), recorder=recorder)

        self.assertIsNone(exc, f"真实群成员被误拦: {exc}")
        self.assertEqual(len(engine.handled), 1)
        self.assertIn("send_group_msg", recorder.sends)

    def test_request_without_claimed_identity_is_untouched(self) -> None:
        recorder = _SendRecorder()
        body = self._synthetic_body()
        body["context_user_id"] = ""
        body["context_user_name"] = ""
        _result, exc, engine = self._run_endpoint(body=body, recorder=recorder)

        self.assertIsNone(exc, f"不带身份的请求不该被拦: {exc}")
        self.assertEqual(len(engine.handled), 1)
        self.assertNotIn("get_group_member_info", [api for api, _ in recorder.calls])


if __name__ == "__main__":
    unittest.main()
