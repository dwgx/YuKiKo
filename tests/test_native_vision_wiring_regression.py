"""agent 侧原生看图接线：图片块必须是 data URI，且失败不能拖垮整个回合。

原来 core/agent.py 自己从 raw_segments 取 `data.url` 塞 image_url 块，而那些是
QQ CDN 链接 —— 实测对外不可达，三种失效形态都见过：
  HTTP 400 {"retcode":-5503022,"retmsg":"appid is not supported"}
  HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"}
  HTTP 400 {"retcode":-5503011,"retmsg":"invalid rkey"}
所以那段代码只会让模型收到一堆死链。现已改为调
core/tools_vision.py 的 build_native_vision_blocks()（只用实测成功的取法）。

本文件锁 agent 这一侧的接线契约，不重复测 tools_vision 那边的转换逻辑
（那边有 tests/test_native_vision_blocks_regression.py）。

另外钉住一件容易被顺手改坏的事：`_build_user_message` 必须保持**同步**。
它有四个同步调用方 —— tests/test_agent_smoke.py:188 和
tests/test_tool_call_leak_regression.py:357 会把它替换成同步 lambda，
tests/test_dialog_and_sticker_regression.py:84 与
scripts/agent_deep_selfcheck.py:165 直接同步调用。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

from core.agent import AgentContext, AgentLoop

_TEXT = "这张图是什么"


class _VisionCapableClient:
    def supports_vision_input(self) -> bool:
        return True


class _TextOnlyClient:
    def supports_vision_input(self) -> bool:
        return False


class _Executor:
    """替身 ToolExecutor：只实现 agent 侧真正会调的两个方法。"""

    def __init__(self, blocks, reason="", *, boom=False):
        self._blocks = blocks
        self._reason = reason
        self._boom = boom
        self.calls = 0

    async def build_native_vision_blocks(
        self, raw_segments=None, reply_media_segments=None, api_call=None, max_images=0
    ):
        self.calls += 1
        if self._boom:
            raise RuntimeError("转换炸了")
        return list(self._blocks), self._reason

    @staticmethod
    def estimate_native_vision_tokens(blocks) -> int:
        return 123 * len(blocks)


def _data_uri_block(payload: str = "AAAA"):
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}}


def _compose(executor, client, *, text: str = _TEXT):
    loop = AgentLoop.__new__(AgentLoop)
    loop.config = {}
    loop._build_user_message = lambda ctx: text
    ctx = AgentContext(
        conversation_id="group:1:user:2",
        user_id="2",
        user_name="tester",
        group_id=1,
        bot_id="bot",
        is_private=False,
        mentioned=True,
        message_text=text,
        trace_id="wiring",
    )
    ctx.tool_executor = executor
    ctx.api_call = None
    return asyncio.run(loop._compose_user_content(ctx, client))


class NativeVisionWiringTests(unittest.TestCase):
    def test_blocks_are_appended_after_the_text_part(self) -> None:
        block = _data_uri_block()
        content = _compose(_Executor([block]), _VisionCapableClient())
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": _TEXT})
        self.assertEqual(content[1], block)

    def test_no_blocks_degrades_to_plain_text(self) -> None:
        content = _compose(_Executor([], "all_conversions_failed"), _VisionCapableClient())
        self.assertEqual(content, _TEXT)

    def test_converter_exception_degrades_instead_of_failing_the_turn(self) -> None:
        """原生看图是增强项 —— 它炸了不能让整个回合挂掉。"""

        content = _compose(_Executor([], boom=True), _VisionCapableClient())
        self.assertEqual(content, _TEXT)

    def test_text_only_model_skips_the_converter_entirely(self) -> None:
        """模型不支持图片输入时不该白烧 get_image 的往返。"""

        executor = _Executor([_data_uri_block()])
        content = _compose(executor, _TextOnlyClient())
        self.assertEqual(content, _TEXT)
        self.assertEqual(executor.calls, 0, "不支持 vision 还去调转换 = 白烧网络往返")

    def test_missing_executor_is_tolerated(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        loop._build_user_message = lambda ctx: _TEXT
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="t",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text=_TEXT,
            trace_id="wiring",
        )
        ctx.tool_executor = None
        content = asyncio.run(loop._compose_user_content(ctx, _VisionCapableClient()))
        self.assertEqual(content, _TEXT)

    def test_capability_probe_failure_degrades(self) -> None:
        class _Broken:
            def supports_vision_input(self):
                raise RuntimeError("探测炸了")

        executor = _Executor([_data_uri_block()])
        self.assertEqual(_compose(executor, _Broken()), _TEXT)
        self.assertEqual(executor.calls, 0)

    def test_already_structured_content_is_passed_through(self) -> None:
        """_build_user_message 若已返回 list，不能再包一层。"""

        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        prebuilt = [{"type": "text", "text": "已经是结构化的"}]
        loop._build_user_message = lambda ctx: prebuilt
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="t",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text=_TEXT,
            trace_id="wiring",
        )
        ctx.tool_executor = _Executor([_data_uri_block()])
        ctx.api_call = None
        self.assertEqual(
            asyncio.run(loop._compose_user_content(ctx, _VisionCapableClient())), prebuilt
        )


class BuildUserMessageStaysSyncTests(unittest.TestCase):
    def test_build_user_message_is_not_a_coroutine_function(self) -> None:
        self.assertFalse(
            inspect.iscoroutinefunction(AgentLoop._build_user_message),
            "四个同步调用方会直接坏掉，见本文件模块 docstring",
        )

    def test_compose_user_content_is_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(AgentLoop._compose_user_content))

    def test_agent_no_longer_reads_the_ghost_vision_enabled_key(self) -> None:
        """agent.vision_enabled 的读取点已删除，开关是
        search.vision.native_blocks_enable。两个开关串联是更糟的状态。"""

        import pathlib

        src = pathlib.Path("core/agent.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'agent_cfg.get("vision_enabled"', src, "幽灵键读取点不该回来"
        )


if __name__ == "__main__":
    unittest.main()
