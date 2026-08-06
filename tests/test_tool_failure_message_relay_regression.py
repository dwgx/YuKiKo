"""router 工具失败时的人话必须传给模型 —— 只给错误码会让它多试三个工具。

## 实测（2026-08-06，trace 118886-12-a3424cb9，水位线后最慢的回合）

```
step=1 music_search      ok=True   找到 5 首歌曲
step=2 music_play_by_id  ok=False  display=music_play_by_id 失败: download_failed
step=3 music_play        （模型换工具再试）
step=4 bilibili_audio_extract（再换）
step=5 final_answer
agent_done | steps=6 | 73 秒
```

router 侧的 `ToolResult.payload["text"]` 里本来有人话，例如
「没找到与歌手「宋岳庭」匹配的可播版本，请换个关键词或指定歌曲ID」。
但 `_handle_music_play_by_id` 只返回 `error=result.error`，把 payload 整个丢了。
于是 `core/agent.py:2361` 用错误码合成了 `music_play_by_id 失败: download_failed`
—— 那读起来像**临时故障**，模型很合理地去试下一个工具。

## 为什么这条值钱

实测回合耗时几乎线性于步数（每步一次 LLM ≈10 秒）：

```
1-2 步 ≈20s    3-4 步 ≈30s    5-7 步 44-73s
503 有无对耗时无影响（p50 29.5s vs 31.0s）—— failover 吸收得很快
```

所以延迟的唯一有效抓手是**减步数**，而「失败原因说不清 → 模型换个工具再试」
是步数膨胀的主因。同族的 `_handle_music_search` 一直正确传 display，
说明这是漏写而非设计选择。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_ADMIN = Path("core/agent_tools_admin.py")

# 这些 handler 都通过 tool_executor.execute() 调 router 工具，
# router 侧的失败 payload 里有人话，必须转成 display。
_ROUTER_BACKED_HANDLERS = {
    "_handle_music_search",
    "_handle_music_play_by_id",
    "_handle_bilibili_audio_extract",
}


def _handler_nodes() -> dict[str, ast.AST]:
    tree = ast.parse(_ADMIN.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _failure_returns_without_display(node: ast.AST) -> list[str]:
    """找出 ok=False 但没有 display 关键字的 ToolCallResult 构造。"""

    bad = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "ToolCallResult":
            continue
        kwargs = {kw.arg for kw in child.keywords if kw.arg}
        is_failure = any(
            kw.arg == "ok"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in child.keywords
        )
        # 只针对「转发 router 结果」的返回：它们带 error= 且引用了 result
        references_result = "result" in ast.unparse(child)
        if is_failure and "error" in kwargs and references_result and "display" not in kwargs:
            bad.append(ast.unparse(child)[:160])
    return bad


class RouterFailureMessageIsRelayedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handlers = _handler_nodes()

    def test_all_expected_handlers_exist(self) -> None:
        """自检：handler 改名后本守卫要红。"""

        missing = sorted(h for h in _ROUTER_BACKED_HANDLERS if h not in self.handlers)
        self.assertEqual(missing, [], f"这些 handler 找不到了: {missing}")

    def test_no_handler_drops_the_failure_message(self) -> None:
        offenders: dict[str, list[str]] = {}
        for name in sorted(_ROUTER_BACKED_HANDLERS):
            node = self.handlers.get(name)
            if node is None:
                continue
            bad = _failure_returns_without_display(node)
            if bad:
                offenders[name] = bad
        self.assertEqual(
            offenders,
            {},
            "这些 handler 失败时只回错误码、丢掉了 router 的人话 —— "
            "模型看到的会是 core/agent.py:2361 合成的「X 失败: <错误码>」，"
            "读起来像临时故障，于是它会多试几个工具（实测因此出现 6 步 / 73 秒的回合）:\n"
            f"{offenders}\n"
            "改法：照 _handle_music_search 的写法，"
            'display=str(payload.get("text", "")).',
        )

    def test_music_search_remains_the_reference_implementation(self) -> None:
        """music_search 是这个模式的参照，它不能退化。"""

        node = self.handlers["_handle_music_search"]
        src = ast.unparse(node)
        self.assertIn(
            'display=str(payload.get(',
            src.replace("'", '"').replace(" ", "").replace("display=str(payload.get(", "display=str(payload.get("),
            "参照实现变了 —— 本守卫的判据需要跟着改",
        )


class FailureDisplaySynthesisIsStillTheFallbackTests(unittest.TestCase):
    """agent.py 的错误码合成要保留 —— 它是最后一道保险，不是要删的东西。

    没有 display 也没有人话时，模型至少要知道失败了。这条测试防止
    有人为了「不显示错误码」把合成逻辑一起删掉，导致失败变成静默。
    """

    def test_agent_still_synthesises_a_display_when_none_provided(self) -> None:
        src = Path("core/agent.py").read_text(encoding="utf-8")
        self.assertIn(
            'result.display = f"{tool_name} 失败: {result.error}"',
            src,
            "错误码兜底合成被删了 —— 没有 display 的失败会变成静默",
        )


if __name__ == "__main__":
    unittest.main()
