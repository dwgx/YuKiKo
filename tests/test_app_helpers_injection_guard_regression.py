"""app_helpers.py 引用但不自己定义的符号，必须全部在 bind_runtime_dependencies 里注册。

背景：app.py 与 app_helpers.py 是一个逻辑单元。为规避循环导入，app.py 先调
`_app_helpers.bind_runtime_dependencies(...)`（只是 `globals().update(deps)`），
再 `from app_helpers import *`。所以 app_helpers 里那些 `_is_xxx(...)` 是**运行时**
才存在的名字 —— pyproject.toml 因此给这个文件放行了 F821。

代价是：漏注册一个符号，静态检查、ruff、单测全都看不出来，
只有**真实发送路径**跑到那一行才炸 `NameError`。

实测踩过：给 app.py 加了 `_is_unretryable_send_error` 并在 app_helpers 里调用，
但忘了加进注入列表 —— 全仓测试只有一条 media fallback 测试红，
报的是 `NameError: name '_is_unretryable_send_error' is not defined`（app_helpers.py:770）。
如果那条测试恰好没覆盖这个分支，这个 bug 会一路上线。

本文件用 AST 扫出「app_helpers 引用、但既不是它自己定义、也不是它 import 进来」的名字，
逐个要求出现在 app.py 的 bind_runtime_dependencies 调用里。
"""

from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path

_APP = Path("app.py")
_HELPERS = Path("app_helpers.py")


def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _defined_and_imported(tree: ast.Module) -> set[str]:
    """模块自己定义或 import 进来的顶层名字。"""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.comprehension)):
            target = getattr(node, "target", None)
            if isinstance(target, ast.Name):
                names.add(target.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _injected_names() -> set[str]:
    """从 app.py 里 bind_runtime_dependencies(...) 的关键字参数取注册名单。"""

    tree = _module_ast(_APP)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if name != "bind_runtime_dependencies":
            continue
        return {kw.arg for kw in node.keywords if kw.arg}
    return set()


def _referenced_underscore_names(tree: ast.Module) -> set[str]:
    """被读取（Load）的下划线开头名字 —— 那些是 app.py 私有符号的形状。"""

    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id.startswith("_")
        and not node.id.startswith("__")
    }


class InjectionListIsCompleteTests(unittest.TestCase):
    def test_bind_call_is_findable(self) -> None:
        injected = _injected_names()
        self.assertTrue(
            injected,
            "在 app.py 里找不到 bind_runtime_dependencies(...) 的关键字参数 —— "
            "调用形状变了，本守卫需要跟着改",
        )

    def test_every_runtime_resolved_name_is_registered(self) -> None:
        helpers_tree = _module_ast(_HELPERS)
        local = _defined_and_imported(helpers_tree)
        referenced = _referenced_underscore_names(helpers_tree)
        injected = _injected_names()
        builtin_names = set(dir(builtins))

        missing = sorted(
            n
            for n in referenced
            if n not in local and n not in injected and n not in builtin_names
        )
        self.assertEqual(
            missing,
            [],
            "app_helpers.py 引用了这些既不属于自己、也没被注入的名字 —— "
            "运行时会 NameError，而且只在真实路径上炸："
            f"{missing}\n"
            "改法：在 app.py 的 bind_runtime_dependencies(...) 里加上同名参数。",
        )

    def test_send_error_classifiers_are_all_registered(self) -> None:
        """这一族最容易漏：判定函数住在 app.py，调用点在 app_helpers.py。"""

        app_tree = _module_ast(_APP)
        classifiers = {
            node.name
            for node in ast.walk(app_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("_is_")
            and node.name.endswith("_send_error")
        }
        self.assertTrue(classifiers, "app.py 里没找到 _is_*_send_error 形状的判定函数")

        helpers_src = _HELPERS.read_text(encoding="utf-8")
        injected = _injected_names()
        used_in_helpers = {name for name in classifiers if f"{name}(" in helpers_src}
        missing = sorted(name for name in used_in_helpers if name not in injected)
        self.assertEqual(
            missing,
            [],
            f"这些判定函数在 app_helpers 里被调用但没注册: {missing}",
        )

    def test_injected_names_actually_exist_in_app(self) -> None:
        """反向：注册了但 app.py 里已经没有这个符号 —— 那是删函数时漏清的残留。"""

        app_tree = _module_ast(_APP)
        app_names = _defined_and_imported(app_tree)
        injected = _injected_names()
        stale = sorted(n for n in injected if n not in app_names)
        self.assertEqual(stale, [], f"注入列表里有 app.py 已不存在的符号: {stale}")


if __name__ == "__main__":
    unittest.main()
