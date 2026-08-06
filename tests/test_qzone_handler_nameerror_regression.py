"""QZone 五个工具曾因一个未定义符号全部报废。

实测线上（storage/logs/yukiko.log，2026-08-05）：
    analyze_qzone 失败: tool_exception: NameError: name '_resolve_qzone_config' is not defined
    get_qzone_profile 失败: tool_exception: NameError: name '_resolve_qzone_config' is not defined

根因：`_resolve_qzone_config` 定义在 core/agent_tools_web.py，而 core/agent_tools_social.py
的 `_make_qzone_handler` 直接调用它，既没导入也没本地定义。
handler 是闭包，NameError 只在**真正被调用时**才抛 —— 所以注册、schema 校验、
启动自检全都看不出问题，只有群里有人用这个工具才炸。这类 bug 必须有测试兜。

这里断言的契约是「handler 能跑到配置检查那一步」，不是「QZone 功能可用」——
后者需要真 cookie 和网络。跑到配置检查就足以证明符号解析没问题。
"""

from __future__ import annotations

import asyncio
import unittest

from core.agent_tools_social import _make_qzone_handler

_QZONE_MODES = ("profile", "moods", "albums", "analyze", "photos")


class QZoneHandlerSymbolResolutionTests(unittest.TestCase):
    def _call(self, mode: str, config: dict, context: dict):
        handler = _make_qzone_handler(mode, config)
        return asyncio.run(handler({"qq_number": "10001"}, context))

    def test_handler_does_not_raise_nameerror_when_disabled(self) -> None:
        """未启用时应干净地返回「未启用」，而不是 NameError。"""

        for mode in _QZONE_MODES:
            with self.subTest(mode=mode):
                result = self._call(
                    mode,
                    {"video_analysis": {"qzone": {"enable": False}}},
                    {},
                )
                self.assertFalse(result.ok)
                self.assertIn("未启用", str(result.error))

    def test_handler_reaches_cookie_check_when_enabled(self) -> None:
        """启用但没 cookie 时应报缺 cookie —— 说明符号已解析、走到了业务判断。"""

        for mode in _QZONE_MODES:
            with self.subTest(mode=mode):
                result = self._call(
                    mode,
                    {"video_analysis": {"qzone": {"enable": True, "cookie": ""}}},
                    {},
                )
                self.assertFalse(result.ok)
                self.assertIn("cookie", str(result.error).lower())

    def test_runtime_config_from_context_wins(self) -> None:
        """_resolve_qzone_config 的语义是 context 里的运行时 config 优先，
        这是热重载能生效的前提。注册时给 enable=True，运行时给 False，应按运行时的来。"""

        result = self._call(
            "profile",
            {"video_analysis": {"qzone": {"enable": True, "cookie": "p_skey=x"}}},
            {"config": {"video_analysis": {"qzone": {"enable": False}}}},
        )
        self.assertFalse(result.ok)
        self.assertIn("未启用", str(result.error))

    def test_symbol_is_importable_from_its_defining_module(self) -> None:
        """把「它到底住在哪」钉住 —— 将来谁把它挪走，这条会红。"""

        from core.agent_tools_web import _resolve_qzone_config

        self.assertTrue(callable(_resolve_qzone_config))
        self.assertEqual(
            _resolve_qzone_config({"video_analysis": {"qzone": {"enable": True}}}, {}),
            {"enable": True},
        )

    def test_every_helper_the_handler_needs_is_resolvable(self) -> None:
        """不止 _resolve_qzone_config —— 同一个函数体里有六个跨模块符号。

        第一版修复只补了第一个，测试也只跑到 cookie 检查就返回了，
        所以另外五个仍然是 NameError 而测试全绿。这条按 handler 真正引用的名字逐个查，
        避免再出现「修了一个、剩下五个」。
        名单来自对 _make_qzone_handler 函数体的 AST 扫描，不是手抄。
        """

        import ast
        import inspect

        import core.agent_tools_social as social

        source = inspect.getsource(social._make_qzone_handler)
        tree = ast.parse(source.lstrip())
        referenced = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id.startswith("_")
        }
        # 函数体内 import 进来的名字 + 模块级可见的名字，都算已解析
        imported_locally = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        module_level = set(vars(social))
        # 局部变量不算跨模块依赖
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        unresolved = sorted(
            name
            for name in referenced
            if name not in imported_locally
            and name not in module_level
            and name not in assigned
        )
        self.assertEqual(
            unresolved,
            [],
            f"_make_qzone_handler 引用了这些既没导入也不在模块里的符号：{unresolved}",
        )

    def test_helpers_actually_come_from_the_web_module(self) -> None:
        """六个辅助函数都应能从 agent_tools_web 导入 —— 钉住它们的归属。"""

        from core.agent_tools_web import (
            _normalize_qzone_tool_error,
            _qzone_album_payload,
            _qzone_mood_payload,
            _qzone_profile_payload,
            _resolve_qzone_config,
            _safe_int,
        )

        for fn in (
            _normalize_qzone_tool_error,
            _qzone_album_payload,
            _qzone_mood_payload,
            _qzone_profile_payload,
            _resolve_qzone_config,
            _safe_int,
        ):
            self.assertTrue(callable(fn), fn)
        self.assertEqual(_safe_int("7", 1, min_value=1, max_value=10), 7)
        self.assertEqual(_safe_int("999", 1, min_value=1, max_value=10), 10)


if __name__ == "__main__":
    unittest.main()
