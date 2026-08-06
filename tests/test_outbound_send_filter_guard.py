"""直发工具的出站文字必须过敏感词过滤 —— 一个都不能漏。

## 背景（2026-08-06，业主提出封群风险）

`final_answer` 的文本会在 `core/engine.py` 的 `_try_agent_path` 后处理里过一遍
`SafetyEngine.filter_output`。但 `_SIDE_EFFECT_SEND_TOOLS` 那批工具是**直接调
NapCat API** 的，完全绕开那段后处理：

    _handle_send_group_message -> call_napcat_api("send_group_msg", message=...)

所以模型经工具发出去的文字此前是**零过滤**。时政 / 露骨内容是 QQ 封群的主因，
这条路径上漏一个工具，防护就等于没有。

## 为什么要靠 AST 守卫而不是人工核对

这正是 `tests/test_app_helpers_injection_guard_regression.py` 记录过的形态：
分散在多个 handler 里的必要调用，漏一个静态检查和单测都看不出来，
只有真实发送路径跑到那一行才出事 —— 而那时内容已经出群了。

本文件用 AST 扫每个直发 handler 的函数体，要求它**在把文字交给 API 之前**
调用过 `sanitize_outbound_text` / `sanitize_outbound_payload`。

## 不发自由文字的工具不在要求内

`send_emoji` / `send_sticker` / `learn_sticker` 的参数是本地表情库检索词和图片
标识，`upload_group_file` / `upload_private_file` 是文件路径和文件名 ——
它们不产出模型自己写的句子。把它们也一刀切要求过滤只会逼出无意义的调用，
所以这里显式列出**需要过滤的**是哪几个，并让 §反向测试 钉住这份名单的完整性。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core.agent import AgentLoop, AgentContext

_NAPCAT = Path("core/agent_tools_napcat.py")

# 这些 handler 会把「模型自己写的句子」发出去，必须过滤。
_TEXT_EMITTING_HANDLERS = {
    "_handle_send_group_message",
    "_handle_send_private_message",
    "_handle_send_group_ai_record",
    "_handle_send_group_forward_msg",
    "_handle_send_private_forward_msg",
}

_SANITIZERS = {"sanitize_outbound_text", "sanitize_outbound_payload"}


def _function_nodes(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


class EverySendHandlerFiltersOutboundTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.functions = _function_nodes(_NAPCAT)

    def test_all_expected_handlers_exist(self) -> None:
        """自检：handler 改名后本守卫必须红，而不是静默通过。"""

        missing = sorted(h for h in _TEXT_EMITTING_HANDLERS if h not in self.functions)
        self.assertEqual(
            missing,
            [],
            f"这些 handler 在 {_NAPCAT} 里找不到了 —— 改名或搬走了，本守卫需要跟着改: {missing}",
        )

    def test_every_text_emitting_handler_calls_a_sanitizer(self) -> None:
        unfiltered = sorted(
            name
            for name in _TEXT_EMITTING_HANDLERS
            if name in self.functions and not (_called_names(self.functions[name]) & _SANITIZERS)
        )
        self.assertEqual(
            unfiltered,
            [],
            "这些直发工具没过敏感词过滤，模型写的文字会零过滤出群（封群风险）: "
            f"{unfiltered}\n改法：把出站文字包一层 sanitize_outbound_text(...)，"
            "嵌套结构用 sanitize_outbound_payload(...)。",
        )

    def test_every_handler_that_sends_model_text_is_sanitized(self) -> None:
        """全集由**代码推导**，不靠手维护的清单。

        第一版守卫把全集锚在 `AgentLoop._SIDE_EFFECT_SEND_TOOLS` 上，
        结果漏掉三个 handler —— 2026-08-06 子 agent 审计抓到的：

            _handle_send_msg          (message)  -> send_msg
            _handle_send_forward_msg  (messages) -> send_forward_msg
            _handle_send_group_notice (content)  -> _send_group_notice   ← 群公告，置顶可见

        原因：那份清单是给「每回合只调一次」记账用的，**不是**「会发文字」的全集，
        `send_msg` / `send_forward_msg` 本来就不在里面。锚错了全集，守卫就是空转的绿灯。

        正确判据：函数体里既读了文字类 args，又出现了 NapCat 发送 API 名。
        """

        text_args = {"message", "text", "content", "messages"}
        send_apis = {
            "send_msg",
            "send_group_msg",
            "send_private_msg",
            "send_forward_msg",
            "send_group_forward_msg",
            "send_private_forward_msg",
            "_send_group_notice",
            "send_group_ai_record",
        }

        offenders: dict[str, dict[str, list[str]]] = {}
        for name, node in self.functions.items():
            if not name.startswith("_handle_"):
                continue
            body = ast.unparse(node)
            read_args = {
                str(c.args[0].value)
                for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "get"
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "args"
                and c.args
                and isinstance(c.args[0], ast.Constant)
            }
            hit_text = sorted(read_args & text_args)
            # 两种引号都要查：`ast.unparse` 把源码里的双引号统一改写成单引号，
            # 只查 f'"{a}"' 会永远匹配不到 —— 第一版就是这样变成假绿灯的，
            # HEAD（完全没有过滤）和修好之后都报 0 个违规。
            hit_api = sorted(a for a in send_apis if f'"{a}"' in body or f"'{a}'" in body)
            if not hit_text or not hit_api:
                continue
            if _called_names(node) & _SANITIZERS:
                continue
            offenders[name] = {"text_args": hit_text, "apis": hit_api}

        self.assertEqual(
            offenders,
            {},
            "这些 handler 把模型给的文字送进 NapCat 却没过滤（封群风险）:\n"
            f"{offenders}\n"
            "改法：文字包 sanitize_outbound_text(...)，嵌套结构包 "
            "sanitize_outbound_payload(...)。",
        )

    def test_the_ast_criteria_actually_match_something(self) -> None:
        """守卫自检：判据必须真能识别出 sink，否则它是空转的绿灯。

        第一版这条判据用 `f'"{api}"' in ast.unparse(node)`，而 `ast.unparse`
        把双引号统一改写成单引号 —— 判据永远匹配不到任何 handler，
        于是「HEAD 完全没有过滤」和「全部修好」都报 0 个违规。
        这条测试就是为了让那种失效变成红灯。
        """

        text_args = {"message", "text", "content", "messages"}
        send_apis = {
            "send_msg",
            "send_group_msg",
            "send_private_msg",
            "send_forward_msg",
            "send_group_forward_msg",
            "send_private_forward_msg",
            "_send_group_notice",
            "send_group_ai_record",
        }
        matched = []
        for name, node in self.functions.items():
            if not name.startswith("_handle_"):
                continue
            body = ast.unparse(node)
            read_args = {
                str(c.args[0].value)
                for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "get"
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "args"
                and c.args
                and isinstance(c.args[0], ast.Constant)
            }
            if not (read_args & text_args):
                continue
            if any(f'"{a}"' in body or f"'{a}'" in body for a in send_apis):
                matched.append(name)

        self.assertGreaterEqual(
            len(matched),
            len(_TEXT_EMITTING_HANDLERS),
            "AST 判据识别出的 sink 少于已知的直发 handler —— 判据失效了，"
            f"识别到 {sorted(matched)}，已知至少有 {sorted(_TEXT_EMITTING_HANDLERS)}",
        )

    def test_side_effect_list_members_are_still_classified(self) -> None:
        """保留对 `_SIDE_EFFECT_SEND_TOOLS` 的归类要求作为第二张网。

        它抓的是另一类漏：清单里新增了一个直发工具而没人想过要不要过滤。
        但它**不是**全集 —— 全集由上面那条 AST 测试负责。
        """

        exempt_no_free_text = {
            "send_emoji",        # 本地表情库检索词
            "send_sticker",      # 本地表情包标识
            "learn_sticker",     # 图片 URL / file 标识
            "upload_group_file",     # 文件路径 + 文件名
            "upload_private_file",   # 同上
        }
        needs_filter = {
            "send_group_message",
            "send_private_message",
            "send_group_ai_record",
            "send_group_forward_msg",
            "send_private_forward_msg",
            "send_group_notice",
        }
        unclassified = sorted(
            AgentLoop._SIDE_EFFECT_SEND_TOOLS - exempt_no_free_text - needs_filter
        )
        self.assertEqual(
            unclassified,
            [],
            f"这些直发工具还没归类：{unclassified}\n"
            "它如果会把模型写的句子发出去，就加 sanitize_outbound_text 并登记到 "
            "needs_filter；如果只发本地素材/文件，登记到 exempt_no_free_text。",
        )


class OutboundFilterIsActuallyInjectedTests(unittest.TestCase):
    """光有 sanitize 调用不够 —— 过滤函数必须真的被注入进 tool context。

    `sanitize_outbound_text` 在没注入时原样返回（WebUI 测试台等非 QQ 场景的
    正常情况）。所以「有调用」和「真在过滤」是两件事，这里钉后者。
    """

    def test_agent_context_carries_an_output_filter_field(self) -> None:
        self.assertTrue(
            hasattr(AgentContext("c", "u", "n", 0, "b", False, False, ""), "output_filter"),
            "AgentContext 没有 output_filter 字段 —— 过滤函数无法传到工具层",
        )

    def test_tool_context_exposes_the_output_filter(self) -> None:
        marker = object()
        ctx = AgentContext("c", "u", "n", 0, "b", False, False, "")
        ctx.output_filter = marker  # type: ignore[assignment]
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}  # type: ignore[attr-defined]
        built = AgentLoop._build_tool_context(loop, ctx, "user")
        self.assertIs(
            built.get("output_filter"),
            marker,
            "_build_tool_context 没把 output_filter 透传给工具 —— "
            "直发工具拿不到过滤器，sanitize 会静默变成原样返回",
        )

    def test_engine_injects_safety_filter_into_agent_context(self) -> None:
        """engine 侧必须把 SafetyEngine.filter_output 接上去。"""

        src = Path("core/engine.py").read_text(encoding="utf-8")
        self.assertIn(
            "output_filter=self.safety.filter_output",
            src,
            "core/engine.py 构造 AgentContext 时没注入 safety.filter_output —— "
            "工具直发路径全程零过滤",
        )


class SanitizerBehaviourTests(unittest.TestCase):
    def test_text_is_filtered_when_a_filter_is_present(self) -> None:
        from core.agent_tools_napcat import sanitize_outbound_text

        ctx = {"output_filter": lambda s: s.replace("坏词", "**")}
        self.assertEqual(sanitize_outbound_text("这里有坏词", ctx), "这里有**")

    def test_missing_filter_returns_text_unchanged(self) -> None:
        from core.agent_tools_napcat import sanitize_outbound_text

        self.assertEqual(sanitize_outbound_text("原文", {}), "原文")

    def test_filter_exception_does_not_leak_the_original_text(self) -> None:
        """过滤器炸了不能把原文放出去 —— 这里是封群风险点，宁可少说。"""

        from core.agent_tools_napcat import sanitize_outbound_text

        def boom(_: str) -> str:
            raise RuntimeError("filter exploded")

        result = sanitize_outbound_text("敏感原文", {"output_filter": boom})
        self.assertNotIn("敏感原文", result)

    def test_nested_forward_payload_text_is_filtered(self) -> None:
        from core.agent_tools_napcat import sanitize_outbound_payload

        ctx = {"output_filter": lambda s: s.replace("坏词", "**")}
        payload = [
            {
                "type": "node",
                "data": {
                    "name": "某人",
                    "content": [{"type": "text", "data": {"text": "带坏词的一句"}}],
                },
            }
        ]
        cleaned = sanitize_outbound_payload(payload, ctx)
        self.assertEqual(
            cleaned[0]["data"]["content"][0]["data"]["text"], "带**的一句"
        )

    def test_nested_sanitizer_leaves_urls_and_names_alone(self) -> None:
        """只碰 text 键 —— 别把图片地址和昵称也替换掉。"""

        from core.agent_tools_napcat import sanitize_outbound_payload

        ctx = {"output_filter": lambda s: "REPLACED"}
        payload = {"data": {"file": "http://a/b.jpg", "name": "坏词昵称"}}
        cleaned = sanitize_outbound_payload(payload, ctx)
        self.assertEqual(cleaned["data"]["file"], "http://a/b.jpg")
        self.assertEqual(cleaned["data"]["name"], "坏词昵称")


if __name__ == "__main__":
    unittest.main()
