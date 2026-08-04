from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.agent import AgentContext, AgentLoop
from core.agent_tools import ToolCallResult, _handle_analyze_local_video
from core.engine import YukikoEngine
from core.prompt_navigator import PromptNavigator, default_prompt_navigator_payload


class ToolCallLeakRegressionTests(unittest.TestCase):
    class _StubRegistry:
        def __init__(self, names: set[str]):
            self._names = set(names)

        def has_tool(self, name: str) -> bool:
            return name in self._names
            
        def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
            return [{"type": "function", "function": {"name": n, "description": "", "parameters": {"type": "object", "properties": {}}}} for n in tool_names]

    class _SequencedModelClient:
        def __init__(self, responses: list[str]):
            self._responses = list(responses)

        async def chat_text_with_retry(
            self, messages, max_tokens=0, retries=0, backoff=0.0
        ):
            _ = (messages, max_tokens, retries, backoff)
            if not self._responses:
                raise AssertionError("No more model responses prepared for test")
            return self._responses.pop(0)

        async def chat_completion_with_retry(self, messages, max_tokens=0, tools=None, retries=0, backoff=0.0):
            _ = (messages, max_tokens, tools, retries, backoff)
            if not self._responses:
                raise AssertionError("No more model responses prepared for test")
            resp = self._responses.pop(0)
            
            try:
                import json
                parsed = json.loads(resp)
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": parsed.get("tool", "unknown"),
                                    "arguments": json.dumps(parsed.get("args", {}))
                                }
                            }]
                        }
                    }]
                }
            except:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": resp
                        }
                    }]
                }

    class _RunnableRegistry(_StubRegistry):
        def __init__(self, names: set[str]):
            super().__init__(names)
            self.calls: list[tuple[str, dict[str, str]]] = []

        async def call(
            self, name: str, args: dict[str, str], context: dict[str, str]
        ) -> ToolCallResult:
            _ = context
            self.calls.append((name, dict(args)))
            return ToolCallResult(
                ok=True,
                data={"image_url": "https://example.com/generated.png"},
                display="图片已生成",
            )

    @staticmethod
    def _make_ctx(**overrides) -> AgentContext:
        base = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="",
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_agent_recovers_truncated_named_final_answer_payload(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.fallback_on_parse_error = True

        parsed = loop._parse_llm_output(
            '{"name":"final_answer","arguments":{"text":"「生于忧患死于安乐」这句话"}'
        )

        self.assertEqual(
            parsed,
            {
                "tool": "final_answer",
                "args": {"text": "「生于忧患死于安乐」这句话"},
            },
        )

    def test_engine_sanitizes_unclosed_fenced_tool_call_payload(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.sanitize_banned_phrases = ()
        engine._apply_privacy_output_guard = lambda text, action="": text
        engine._build_mention_only_reply = lambda text: text

        payloads = (
            '```json\n{"tool":"final_answer","args":{"text":"hello"',
            '```json\n{"name":"final_answer","arguments":{"text":"hello"',
            '```json\n{"tool":"learn_knowledge","args":{"title":"用户称呼偏好","content":"以后叫我"妈妈""}',
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    engine._sanitize_reply_output(payload, action="reply"), ""
                )

    def test_engine_no_longer_local_matches_provider_refusal_templates(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.sanitize_banned_phrases = ()
        engine._apply_privacy_output_guard = lambda text, action="": text
        engine._build_mention_only_reply = lambda text: text

        payloads = (
            "I'm Claude, an AI assistant made by Anthropic. I'm a text-based AI assistant and cannot generate images directly.",
            "抱歉，我无法查看图片内容。我是一个文本助手，只能处理文字信息。我目前无法直接生成图片，不具备图像生成功能。",
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    engine._sanitize_reply_output(payload, action="reply"),
                    payload,
                )

    def test_agent_marks_generic_fenced_tool_payload_as_leak(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        payload = (
            '```json { "tool": "learn_knowledge", "args": { "title": "用户称呼偏好", '
            '"content": "以后叫我"妈妈"" } } ```'
        )
        self.assertTrue(loop._looks_like_embedded_tool_payload_text(payload))

    def test_agent_detects_image_hint_from_multimodal_event_text(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        text = (
            "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: "
            "image:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"
        )
        self.assertTrue(loop._text_has_image_hint(text))

    def test_agent_treats_ntqq_download_url_as_image(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"
        self.assertTrue(loop._looks_like_image_url(url))

    # ── A6：七个 forced-tool helper 已删。下面这组测试保留原来的场景，
    # 把断言从「本地词表挑出哪个工具」翻转成「结构闸门要求过工具 + 分区暴露了正确的工具」。
    # 真正的契约「有媒体 + 一句短问句必须调工具，不能纯文本作答」一条没丢。

    _NAV_TOOLS = [
        "think",
        "final_answer",
        "navigate_section",
        "analyze_image",
        "resolve_image",
        "analyze_voice",
        "analyze_local_video",
        "analyze_video",
        "parse_video",
        "split_video",
        "learn_sticker",
        "fetch_webpage",
    ]

    def _nav_state(self, ctx):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        return nav, nav.initial_state(ctx, self._NAV_TOOLS)

    def test_agent_forces_image_tool_for_short_image_question(self) -> None:
        """原断言：词表把 image + 「這是什麽」强制成 analyze_image(url, question)。

        现断言：图片 segment 这个结构事实让分区落在 multimodal_media，
        analyze_image 在该分区可见，且结构闸门要求先过工具。选哪个工具由模型决定。
        """
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        ctx = self._make_ctx(
            message_text="MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: image:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123\n這是什麽",
            raw_segments=[
                {
                    "type": "image",
                    "data": {
                        "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123"
                    },
                }
            ],
        )
        nav, state = self._nav_state(ctx)
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("analyze_image", nav.scoped_tools(state))
        ctx.navigator_state = state
        self.assertTrue(loop._requires_tool_review_before_final(ctx))

    def test_agent_forces_local_video_tool_for_short_question(self) -> None:
        """原断言：词表把 video + 「这是什么」强制成 analyze_local_video。

        现断言分两种结构，因为它们客观上不是一回事：
        - 真正的本地视频文件 → multimodal_media，analyze_local_video 可见；
        - http 视频直链 → video_url，parse_video / analyze_video 可见。
        旧词表对这两种一律吐 analyze_local_video，对直链是错的。
        """
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}

        local_ctx = self._make_ctx(
            message_text="这是什么",
            raw_segments=[{"type": "video", "data": {"file": "/tmp/yukiko/demo.mp4"}}],
            media_summary=["video:/tmp/yukiko/demo.mp4"],
        )
        nav, state = self._nav_state(local_ctx)
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("analyze_local_video", nav.scoped_tools(state))
        local_ctx.navigator_state = state
        self.assertTrue(loop._requires_tool_review_before_final(local_ctx))

        url_ctx = self._make_ctx(
            message_text="这是什么",
            raw_segments=[
                {"type": "video", "data": {"url": "https://example.com/demo.mp4"}}
            ],
            media_summary=["video:https://example.com/demo.mp4"],
        )
        nav, state = self._nav_state(url_ctx)
        self.assertEqual(state.active_section, "video_url")
        self.assertIn("parse_video", nav.scoped_tools(state))
        self.assertIn("analyze_video", nav.scoped_tools(state))

    def test_agent_forces_split_video_tool_for_structured_video_request(self) -> None:
        """typed command contract，KEEP 类：`mode=audio` / `10s-20s` 是用户敲的显式 token。

        改的只是「谁选 split_video」——以前本地词表选，现在模型选；
        模型选定之后，这些 typed token 仍然要被解析进 args，这一半不能丢。
        """
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        ctx = self._make_ctx(
            message_text="mode=audio 10s-20s",
            raw_segments=[
                {
                    "type": "video",
                    "data": {"url": "https://example.com/demo.mp4"},
                }
            ],
            media_summary=["video:https://example.com/demo.mp4"],
        )
        nav, state = self._nav_state(ctx)
        self.assertIn("split_video", nav.scoped_tools(state))

        args = loop._normalize_tool_args("split_video", {}, ctx)
        self.assertEqual(args.get("mode"), "audio")
        self.assertEqual(args.get("start_seconds"), 10.0)
        self.assertEqual(args.get("end_seconds"), 20.0)
        self.assertEqual(args.get("url"), "https://example.com/demo.mp4")

    def test_agent_requires_tool_first_for_media_even_without_keyword_cues(self) -> None:
        """原来问 `_should_force_tool_first`（内部靠词表），现在问结构闸门。

        「嗯」里没有任何可匹配的词，闸门照样 True —— 这正是不需要词表的证据。
        """
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        ctx = self._make_ctx(
            message_text="嗯",
            raw_segments=[
                {
                    "type": "video",
                    "data": {"url": "https://example.com/demo.mp4"},
                }
            ],
            media_summary=["video:https://example.com/demo.mp4"],
        )
        self.assertTrue(loop._requires_tool_review_before_final(ctx))

    def test_agent_forces_voice_tool_for_short_question(self) -> None:
        """原断言：词表把 record + 「说了什么」强制成 analyze_voice。

        现断言：语音 segment → multimodal_media，analyze_voice 在该分区可见。
        """
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        ctx = self._make_ctx(
            message_text="说了什么",
            raw_segments=[
                {
                    "type": "record",
                    "data": {"url": "https://example.com/demo.mp3"},
                }
            ],
            media_summary=["record:https://example.com/demo.mp3"],
        )
        nav, state = self._nav_state(ctx)
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("analyze_voice", nav.scoped_tools(state))
        ctx.navigator_state = state
        self.assertTrue(loop._requires_tool_review_before_final(ctx))

    def test_agent_does_not_locally_force_image_generation(self) -> None:
        """原来两条测试（enhanced / basic）都只是断言 `_select_forced_media_tool` 返回 None。

        那个函数已经删掉，断言合并成：符号不存在，且创作类请求没有结构闸门，
        分区由模型自己挑。
        """
        self.assertFalse(hasattr(AgentLoop, "_select_forced_media_tool"))
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}
        for text in ("帮我生成一张猫娘图片，眼睛里有爱心", "画个猫娘"):
            ctx = self._make_ctx(message_text=text)
            with self.subTest(text=text):
                self.assertFalse(loop._requires_tool_review_before_final(ctx))

    def test_agent_direct_reply_without_forced_image_tool(self) -> None:
        """当 AI 模型直接返回文本拒绝时，不再通过关键词强制触发 generate_image。"""
        registry = self._RunnableRegistry({"generate_image"})
        loop = AgentLoop(
            model_client=self._SequencedModelClient(
                [
                    "I'm a text-based AI assistant and cannot generate images directly.",
                ]
            ),
            tool_registry=registry,
            config={},
        )
        loop.max_steps = 4
        loop.high_risk_control_enable = False
        loop.fallback_on_parse_error = False
        loop._build_system_prompt = lambda ctx: "system"  # type: ignore[assignment]
        loop._build_user_message = lambda ctx: ctx.message_text  # type: ignore[assignment]

        result = asyncio.run(
            loop.run(self._make_ctx(message_text="帮我生成一张猫娘图片，眼睛里有爱心"))
        )

        # AI 应自行决定是否调用工具；如果直接返回文本则以 direct_reply 结束
        self.assertIn(result.reason, {"agent_direct_reply", "agent_final_answer"})

    def test_agent_normalizes_english_refusal_to_chinese(self) -> None:
        refusal = (
            "I can't help with that request. "
            "I’m not able to generate sexually explicit content."
        )
        normalized = AgentLoop._normalize_final_answer_text(refusal)
        self.assertEqual(
            normalized,
            "这个请求我不能帮你处理（涉及不当或露骨内容）。你可以换个健康、合规的话题，我继续帮你。",
        )

    def test_analyze_local_video_handler_reuses_shared_video_analyzer(self) -> None:
        class _DummyExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def _method_video_analyze(
                self,
                method_name: str,
                method_args: dict[str, object],
                query: str,
                message_text: str,
                raw_segments: list[dict[str, object]] | None = None,
                conversation_id: str = "",
            ):
                self.calls.append(
                    {
                        "method_name": method_name,
                        "method_args": dict(method_args),
                        "query": query,
                        "message_text": message_text,
                        "raw_segments": list(raw_segments or []),
                        "conversation_id": conversation_id,
                    }
                )
                return SimpleNamespace(
                    ok=True,
                    payload={
                        "text": "这是本地视频分析结果",
                        "analysis_context": "标题: demo\n时长: 00:12",
                        "video_url": "file:///tmp/demo.mp4",
                    },
                    error="",
                )

        executor = _DummyExecutor()
        result = asyncio.run(
            _handle_analyze_local_video(
                {},
                {
                    "tool_executor": executor,
                    "message_text": "看看这段视频",
                    "conversation_id": "group:1",
                    "raw_segments": [],
                    "reply_media_segments": [
                        {
                            "type": "video",
                            "data": {"url": "file:///tmp/demo.mp4"},
                        }
                    ],
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertIn("这是本地视频分析结果", result.display)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["method_name"], "analyze_local_video")
        self.assertEqual(executor.calls[0]["conversation_id"], "group:1")
        self.assertEqual(
            executor.calls[0]["raw_segments"],
            [{"type": "video", "data": {"url": "file:///tmp/demo.mp4"}}],
        )


if __name__ == "__main__":
    unittest.main()
