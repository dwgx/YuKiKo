from __future__ import annotations

import asyncio
import json
import time
import unittest

from core.agent import AgentContext, AgentLoop
from core.agent_tools import ToolCallResult
from core.prompt_navigator import (
    PromptNavigator,
    default_prompt_navigator_payload,
    validate_prompt_navigator_payload,
)


class _Ctx:
    message_text = ""
    original_message_text = ""
    reply_to_text = ""
    media_summary: list[str] = []
    reply_media_summary: list[str] = []
    raw_segments: list[dict] = []
    reply_media_segments: list[dict] = []
    at_other_user_ids: list[str] = []
    recent_media_artifact: dict = {}


class PromptNavigatorConfigTests(unittest.TestCase):
    def test_video_url_preselects_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "https://www.acfun.cn/v/ac12345 帮我解析"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
        self.assertEqual(state.active_section, "video_url")
        self.assertIn("video_url", state.candidate_sections)
        self.assertIn("video_url", state.evidence)

    def test_bare_domain_preselects_web_research(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "网络是时光机 看skiapi.dev"
        state = nav.initial_state(
            ctx,
            [
                "think",
                "final_answer",
                "navigate_section",
                "fetch_webpage",
                "wayback_lookup",
                "wayback_extract",
            ],
        )
        self.assertEqual(state.active_section, "web_research")
        self.assertIn("url", state.evidence)
        scoped = nav.scoped_tools(state)
        self.assertIn("wayback_lookup", scoped)
        self.assertIn("wayback_extract", scoped)

    def test_research_request_preselects_web_research_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "我要看异环的新手教程"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "web_search"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("external_research_request", state.evidence)
        block = nav.render_system_block(state, nav.scoped_tools(state))
        self.assertIn("web_research", block)
        self.assertIn("media_search", block)

    def test_media_search_request_preselects_media_search_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "给我找一个异环新手教程视频，直接发最合适的"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "search_media"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("media_search_request", state.evidence)
        self.assertIn("media_search", nav.render_system_block(state, nav.scoped_tools(state)))

    def test_short_image_request_preselects_media_search(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "来张猫图"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "search_media"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("media_search_request", state.evidence)

    def test_music_request_preselects_music_section_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "点歌 Never Gonna Give You Up - Rick Astley，直接发语音"
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "music_play"])
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("music_request", state.evidence)

    def test_download_request_preselects_download_section_without_url(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "帮我找一下 OBS Windows 安装包 exe 下载"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "search_download_resources"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("download_request", state.evidence)

    def test_creative_generation_request_preselects_creative_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "帮我画一张赛博猫娘头像"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "generate_image_enhanced"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("creative_generation_request", state.evidence)

    def test_memory_request_preselects_memory_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "你记得我是谁吗"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "recall_about_user"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("memory_request", state.evidence)

    def test_sticker_request_preselects_sticker_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "发一个点赞 QQ 表情"
        state = nav.initial_state(
            ctx, ["think", "final_answer", "navigate_section", "send_face"]
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("sticker_request", state.evidence)
        self.assertNotIn("send_face", nav.scoped_tools(state))
        self.assertIn("sticker_emoji", nav.render_system_block(state, nav.scoped_tools(state)))

    def test_bot_strategy_request_preselects_admin_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "@YuKiKo 闭嘴一下"
        state = nav.initial_state(
            ctx, ["think", "final_answer", "navigate_section", "admin_command"]
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("bot_strategy_request", state.evidence)
        self.assertNotIn("admin_command", nav.scoped_tools(state))
        self.assertIn("qq_admin_social", nav.render_system_block(state, nav.scoped_tools(state)))

    def test_common_video_platform_urls_with_suffix_text_preselect_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        samples = [
            "https://www.bilibili.com/video/BV123解析",
            "https://v.douyin.com/abc123/看看",
            "https://www.acfun.cn/v/ac12345总结",
            "https://v.qq.com/x/cover/demo.html解析一下",
        ]
        for sample in samples:
            ctx = _Ctx()
            ctx.message_text = sample
            state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
            self.assertEqual(state.active_section, "video_url", sample)

    def test_media_segment_preselects_multimodal_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.raw_segments = [{"type": "image", "data": {"url": "file://demo.png"}}]
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "analyze_image"])
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("analyze_image", nav.scoped_tools(state))

    def test_decorated_image_url_preselects_multimodal_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "发这张图 https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "resolve_image", "parse_video"],
        )
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("image_url", state.evidence)
        self.assertIn("resolve_image", nav.scoped_tools(state))

    def test_current_image_url_overrides_recent_video_artifact(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "发这张图 https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
        ctx.recent_media_artifact = {
            "type": "video",
            "video_url": "/tmp/yukiko/demo.mp4",
            "source_url": "https://v.douyin.com/demo/",
        }
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "resolve_image", "parse_video"],
        )
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("image_url", state.evidence)
        self.assertNotIn("recent_media_artifact", state.evidence)

    def test_recent_video_artifact_preselects_video_section(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.recent_media_artifact = {
            "type": "video",
            "video_url": "/tmp/yukiko/demo.mp4",
            "source_url": "https://v.douyin.com/demo/",
        }
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "parse_video"])
        self.assertEqual(state.active_section, "video_url")
        self.assertIn("recent_media_artifact", state.evidence)

    def test_section_tools_are_filtered_by_permission_visible_tools(self):
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section"])
        ok, status = nav.switch_section(state, "qq_admin_social")
        self.assertTrue(ok, status)
        scoped = nav.scoped_tools(state)
        self.assertIn("final_answer", scoped)
        self.assertIn("navigate_section", scoped)
        self.assertNotIn("set_group_ban", scoped)

    def test_max_switches_stops_section_loop(self):
        payload = default_prompt_navigator_payload()
        payload["max_switches"] = 1
        nav = PromptNavigator.from_payload(payload)
        ctx = _Ctx()
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section"])
        self.assertTrue(nav.switch_section(state, "web_research")[0])
        ok, status = nav.switch_section(state, "video_url")
        self.assertFalse(ok)
        self.assertIn("max_switches", status)

    def test_validation_reports_missing_fallback_and_unknown_tool_warning(self):
        payload = default_prompt_navigator_payload()
        payload["sections"]["general_chat"]["fallback_sections"] = ["missing_section"]
        payload["sections"]["general_chat"]["tools"] = ["think", "unknown_tool"]
        errors, warnings = validate_prompt_navigator_payload(payload, known_tools={"think"})
        self.assertTrue(any("fallback" in item for item in errors))
        self.assertTrue(any("unknown_tool" in item for item in warnings))

    # ── C2: mode 是死配置，已删除 ──────────────────────────────────────────

    def test_dead_mode_knob_is_gone_from_config_and_prompt(self):
        """`mode` 全仓无读点，只是每回合被打进 prompt，已删（MIGRATION_TODO C2）。

        原值 `local_prefilter_llm_review` 还反向暗示"本地已预筛、模型只需复核"，
        与"意图完全由模型读菜单判断"冲突。
        """
        payload = default_prompt_navigator_payload()
        self.assertNotIn("mode", payload)

        nav = PromptNavigator.from_payload(payload)
        self.assertFalse(hasattr(nav.config, "mode"))

        state = nav.initial_state(_Ctx(), ["think", "final_answer", "navigate_section"])
        block = nav.render_system_block(state, nav.scoped_tools(state))
        self.assertNotIn("模式:", block)
        self.assertNotIn("local_prefilter_llm_review", block)

    def test_stale_mode_key_in_yaml_is_ignored_not_fatal(self):
        """线上 prompts.yml / master.template.yml 里还留着 `mode:`，加载必须照常。"""
        payload = default_prompt_navigator_payload()
        payload["mode"] = "local_prefilter_llm_review"
        nav = PromptNavigator.from_payload(payload)
        self.assertTrue(nav.enabled)
        self.assertEqual(nav.config.default_section, "general_chat")
        errors, _ = validate_prompt_navigator_payload(payload)
        self.assertEqual(errors, [])

    # ── 结构信号强弱分档 ───────────────────────────────────────────────────

    def test_mention_alone_is_candidate_not_active_section(self):
        """@ 了人只是弱信号：不得让不可逆群管理写操作在开局就可见。

        原行为把任何 @ 别人的消息都直接落到 qq_admin_social，
        于是"@小明 你觉得呢"这种纯闲聊一开局就能看到 set_group_kick /
        set_group_ban。契约保留（qq_admin_social 仍要进候选、mention_target
        仍要作为结构事实告诉模型），但起始分区回到 general_chat，
        真要管理时由模型自己 navigate_section 过去。
        """
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "@小明 你觉得呢"
        ctx.at_other_user_ids = ["12345"]
        state = nav.initial_state(
            ctx,
            ["think", "final_answer", "navigate_section", "set_group_ban", "admin_command"],
        )
        self.assertEqual(state.active_section, "general_chat")
        self.assertIn("qq_admin_social", state.candidate_sections)
        self.assertIn("mention_target", state.evidence)
        # 开局看不到群管理写操作
        self.assertNotIn("set_group_ban", nav.scoped_tools(state))
        # 但模型可以自己走过去
        self.assertTrue(nav.switch_section(state, "qq_admin_social")[0])
        self.assertIn("set_group_ban", nav.scoped_tools(state))

    def test_mention_does_not_outrank_structural_media_signal(self):
        """@ 人 + 带图 时，起始分区必须是图片那一区，不是群管理。"""
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _Ctx()
        ctx.message_text = "@小明 看这个"
        ctx.at_other_user_ids = ["12345"]
        ctx.raw_segments = [{"type": "image", "data": {"url": "http://x/y.png"}}]
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section", "analyze_image"])
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("mention_target", state.evidence)

    # ── URL 解析：TLD 边界 ────────────────────────────────────────────────

    def test_image_filename_is_not_parsed_as_bare_domain(self):
        """`photo.jpg` 曾被截成裸域名 `photo.jp`（jp 在 TLD 表里）。

        后果是"帮我发个 photo.jpg"被判为消息含 URL，起始分区推去 web_research。
        """
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        for text in ("帮我发个 photo.jpg", "封面用 cover.aiff", "装 pkg.appx", "改 server.cnf"):
            with self.subTest(text=text):
                ctx = _Ctx()
                ctx.message_text = text
                state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section"])
                self.assertNotIn("url", state.evidence)
                self.assertEqual(state.active_section, "general_chat")

    def test_real_bare_domains_still_detected(self):
        """边界收紧不能把真域名一起收掉。"""
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        for text, expected in (
            ("去 bilibili.com 看", "video_url"),
            ("腾讯视频 v.qq.com 打不开", "video_url"),
            ("你看看 skiapi.dev 这个站", "web_research"),
            ("试试 example.com:8080/path", "web_research"),
        ):
            with self.subTest(text=text):
                ctx = _Ctx()
                ctx.message_text = text
                state = nav.initial_state(
                    ctx, ["think", "final_answer", "navigate_section", "fetch_webpage"]
                )
                self.assertIn("url", state.evidence)
                self.assertEqual(state.active_section, expected)

    # ── 菜单一致性 ────────────────────────────────────────────────────────

    def test_directory_tool_count_excludes_control_tools(self):
        """目录里的工具数是"能力数"。general_chat 有 0 个能力，不能显示成 3 个。"""
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        state = nav.initial_state(_Ctx(), ["think", "final_answer", "navigate_section"])
        block = nav.render_system_block(state, nav.scoped_tools(state))
        self.assertIn("general_chat (普通对话与判断起点, 0 工具)", block)

    def test_every_fallback_section_id_exists(self):
        payload = default_prompt_navigator_payload()
        section_ids = set(payload["sections"].keys())
        self.assertEqual(len(section_ids), 20)
        for sid, section in payload["sections"].items():
            for fallback in section.get("fallback_sections", []):
                with self.subTest(section=sid, fallback=fallback):
                    self.assertIn(fallback, section_ids)
                    self.assertNotEqual(fallback, sid)

    def test_deliberately_unexposed_tools_stay_out_of_menu(self):
        """12 个工具是有意不进菜单的，不是漏写。防止下一轮被当成缺口补回来。"""
        payload = default_prompt_navigator_payload()
        menu_tools: set[str] = set()
        for section in payload["sections"].values():
            menu_tools |= set(section.get("tools", []))
        for tool in (
            "get_cookies",
            "get_credentials",
            "get_csrf_token",
            "nc_get_rkey",
            "cli_invoke",
            "create_skill",
            "test_in_sandbox",
            "example_lookup",
            "can_send_image",
            "can_send_record",
            "get_robot_uin_range",
            "get_mini_app_ark",
        ):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, menu_tools)


class _SequencedModelClient:
    enabled = True

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        if not self.responses:
            raise AssertionError("No more responses")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _TimeoutModelClient:
    enabled = True

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        raise asyncio.TimeoutError()


class _TimeoutThenSectionModelClient:
    enabled = True

    def __init__(self, section_id: str):
        self.section_id = section_id
        self.calls = 0

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        self.calls += 1
        if self.calls == 1:
            raise asyncio.TimeoutError()
        if self.calls > 2:
            raise asyncio.TimeoutError()
        return json.dumps(
            {"section_id": self.section_id, "reason": "tiny navigator retry"},
            ensure_ascii=False,
        )


class _RecordingNavigatorModelClient:
    enabled = True

    def __init__(self):
        self.models: list[str | None] = []

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(
        self,
        messages,
        max_tokens=0,
        retries=0,
        backoff=0.0,
        model=None,
    ):
        _ = (messages, max_tokens, retries, backoff)
        self.models.append(model)
        return json.dumps(
            {
                "section_id": "media_search",
                "reason": "用户要看视频",
                "tool": "search_media",
                "args": {"query": "异环宣传片", "media_type": "video"},
            },
            ensure_ascii=False,
        )


class _ErrorModelClient:
    enabled = True

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        raise RuntimeError("HTTP 401: 无效的令牌")


class _SlowFirstThenFinalModelClient:
    enabled = True

    def __init__(self):
        self.calls = 0

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(2)
        return json.dumps(
            {"tool": "final_answer", "args": {"text": "解析好了。"}},
            ensure_ascii=False,
        )


class _SlowFirstThenToolThenFinalModelClient:
    """第 1 次调用很慢（用来触发 obvious-tool 超时上限），第 2 次是小 prompt 重试。

    第 2 次返回模型选的工具，第 3 次收尾。用来验证「提前超时」这个预算优化仍然生效，
    而工具名不再由本地 if-链决定。
    """

    enabled = True

    def __init__(self, tool_name: str, tool_args: dict):
        self.calls = 0
        self.tool_name = tool_name
        self.tool_args = dict(tool_args)

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        _ = (messages, max_tokens, retries, backoff)
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(2)
        if self.calls == 2:
            return json.dumps(
                {"tool": self.tool_name, "args": self.tool_args},
                ensure_ascii=False,
            )
        return json.dumps(
            {"tool": "final_answer", "args": {"text": "解析好了。"}},
            ensure_ascii=False,
        )


class _Registry:
    tool_count = 4

    def __init__(self):
        self.names = [
            "web_search",
            "fetch_webpage",
            "parse_video",
            "search_media",
            "search_download_resources",
            "smart_download",
            "wayback_lookup",
            "wayback_extract",
            "wayback_timeline",
            "music_play",
            "send_face",
            "send_emoji",
            "send_sticker",
            "analyze_image",
            "resolve_image",
            "generate_image_enhanced",
            "remember_user_fact",
            "recall_about_user",
            "think",
            "final_answer",
            "navigate_section",
        ]
        self.calls: list[tuple[str, dict]] = []

    def has_tool(self, name: str) -> bool:
        return name in self.names

    def get_schema(self, name: str):
        _ = name
        return None

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        _ = permission_level
        return list(self.names)

    def select_tools_for_intent(self, message_text: str, permission_level: str) -> list[str]:
        _ = (message_text, permission_level)
        raise AssertionError("strict navigator should not use legacy intent selector")

    def get_schemas_for_prompt_filtered(self, tool_names: list[str]) -> str:
        return "\n".join(f"### {name}" for name in tool_names if name in self.names)

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
        _ = tool_names
        return []

    def get_prompt_hints_text(self, section: str, tool_names: list[str] | None = None) -> str:
        _ = (section, tool_names)
        return ""

    def get_dynamic_context(self, payload: dict, tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        if name == "parse_video":
            return ToolCallResult(
                ok=True,
                display="parse_video ok",
                data={
                    "video_url": "/tmp/yukiko/demo.mp4",
                    "source_url": args.get("url", ""),
                    "text": "demo",
                },
            )
        if name == "fetch_webpage":
            return ToolCallResult(ok=True, display="fetch ok", data={"url": args.get("url", "")})
        if name == "web_search":
            return ToolCallResult(ok=True, display="search ok", data={"query": args.get("query", "")})
        if name.startswith("wayback_"):
            return ToolCallResult(
                ok=True,
                display="wayback ok",
                data={"url": args.get("url", ""), "text": "wayback ok"},
            )
        if name == "search_media":
            media_type = args.get("media_type", "")
            data = {"query": args.get("query", ""), "media_type": media_type, "text": "media ok"}
            if media_type == "video":
                data["video_url"] = "/tmp/yukiko/search.mp4"
            else:
                data["image_url"] = "https://example.test/image.jpg"
            return ToolCallResult(ok=True, display="media ok", data=data)
        if name == "search_download_resources":
            return ToolCallResult(
                ok=True,
                display="download candidates ok",
                data={
                    "query": args.get("query", ""),
                    "file_type": args.get("file_type", ""),
                    "items": [{"title": "demo", "url": "https://example.test/demo.exe"}],
                },
            )
        if name == "smart_download":
            return ToolCallResult(
                ok=True,
                display="download ok",
                data={"path": "/tmp/yukiko/demo.exe", "url": args.get("url", "")},
            )
        if name == "music_play":
            return ToolCallResult(
                ok=True,
                display="music ok",
                data={"audio_file": "/tmp/yukiko/song.mp3", "text": "music ok"},
            )
        if name in {"send_face", "send_emoji", "send_sticker"}:
            return ToolCallResult(
                ok=True,
                display=f"{name} ok",
                data={"sent": True, "query": args.get("query", "")},
            )
        if name == "analyze_image":
            return ToolCallResult(
                ok=True,
                display="image analysis ok",
                data={"analysis": "image analysis ok"},
            )
        if name == "resolve_image":
            url = args.get("url", "")
            return ToolCallResult(
                ok=True,
                display="image resolved",
                data={"image_url": url, "image_urls": [url]},
            )
        if name == "generate_image_enhanced":
            return ToolCallResult(
                ok=True,
                display="image generated",
                data={"image_url": "https://example.test/generated.png"},
            )
        if name == "remember_user_fact":
            return ToolCallResult(
                ok=True,
                display="remember ok",
                data={"fact": args.get("fact", "")},
            )
        if name == "recall_about_user":
            return ToolCallResult(
                ok=True,
                display="recall ok",
                data={"items": 1},
            )
        return ToolCallResult(ok=True, display=f"{name} ok", data={"name": name})


class _FailParseRegistry(_Registry):
    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        if name == "parse_video":
            self.calls.append((name, dict(args)))
            return ToolCallResult(
                ok=False,
                display="B站限流了（412），稍等一会儿再试就好。",
                error="bilibili_412_throttled",
            )
        return await super().call(name, args, context)


class _WaybackTimelineFailRegistry(_Registry):
    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        if name == "wayback_timeline":
            self.calls.append((name, dict(args)))
            return ToolCallResult(
                ok=False,
                display="timeline failed:",
                error="timeline_failed",
            )
        return await super().call(name, args, context)


class AgentPromptNavigatorTests(unittest.TestCase):
    def test_agent_can_switch_section_then_call_new_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "navigate_section",
                            "args": {"section_id": "web_research", "reason": "need search"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "web_search", "args": {"query": "YuKiKo"}},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "查好了。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="查一下 YuKiKo",
            trace_id="navigator-test",
        )
        result = asyncio.run(loop.run(ctx))
        self.assertEqual(result.reply_text, "查好了。")
        self.assertEqual([name for name, _ in registry.calls], ["web_search"])
        self.assertIsNotNone(ctx.navigator_state)
        self.assertEqual(ctx.navigator_state.active_section, "web_research")

    def test_strict_routing_blocks_toolless_final_for_video_url(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "我看不了。"}},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "parse_video", "args": {"url": "https://v.douyin.com/demo/"}},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "解析好了。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-strict-test",
        )
        result = asyncio.run(loop.run(ctx))
        self.assertEqual(result.reply_text, "解析好了。")
        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertTrue(any(step.get("tool") == "policy_guard" for step in result.steps))

    # ── A7-4：LLM 首轮超时后不再由本地 if-链挑工具，改由 _navigator_timeout_tool_retry
    # 发起第二次真实 LLM 调用（带该分区的 when_to_use / instructions / 真实 tool schema，
    # 返回后用 tool_name not in domain_tools 硬校验）。
    # 下面这组测试场景与断言全部保留，只把「工具是谁选的」从本地换成模型：
    # fake client 第 1 次调用超时，第 2 次（小 prompt 重试）返回模型选的工具。

    def test_video_url_llm_timeout_falls_back_to_parse_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "parse_video",
                            "args": {"url": "https://v.douyin.com/demo/"},
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")
        self.assertEqual(result.tool_calls_made, 1)

    def test_failed_tool_display_survives_llm_timeout_fallback(self):
        registry = _FailParseRegistry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "parse_video",
                            "args": {
                                "url": "https://www.bilibili.com/video/BV1xx411c7mD/"
                            },
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="解析 https://www.bilibili.com/video/BV1xx411c7mD/",
            trace_id="navigator-failed-tool-display-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertIn("B站限流了（412）", result.reply_text)
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_direct_image_url_llm_timeout_falls_back_to_resolve_image(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "resolve_image",
                            "args": {
                                "url": "https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
                            },
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        image_url = "https://imgs.699pic.com/images/601/562/786.jpg!detail.v1"
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text=f"发这张图 {image_url}",
            trace_id="navigator-direct-image-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["resolve_image"])
        self.assertEqual(registry.calls[0][1]["url"], image_url)
        self.assertEqual(result.image_url, image_url)
        self.assertEqual(result.image_urls, [image_url])
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_obvious_navigator_tool_caps_initial_llm_wait(self):
        """分区有明确结构证据时，首轮 LLM 不等满预算，提前进小 prompt 重试。

        原实现的触发条件是「本地 if-链已经挑好了工具」，现在是
        `_has_navigator_section_evidence(ctx)`（纯结构：分区不是 general_chat、
        有 evidence、分区里有真实工具）。省预算的效果不变，选工具的人换成了模型。
        """
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SlowFirstThenToolThenFinalModelClient(
                "parse_video", {"url": "https://v.douyin.com/demo/"}
            ),
            tool_registry=registry,
            config={
                "agent": {
                    "enable": True,
                    "max_steps": 5,
                    "fallback_on_parse_error": True,
                    "navigator_obvious_tool_timeout_seconds": 0.05,
                },
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-obvious-tool-cap-test",
        )

        started = time.perf_counter()
        result = asyncio.run(loop.run(ctx))
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.reply_text, "解析好了。")

    def test_video_url_llm_error_does_not_invent_local_tool(self):
        """原名 test_video_url_llm_error_falls_back_to_parse_tool。

        原断言：LLM 抛 401 之后，本地凭 URL 直接执行 parse_video，
        `tool_calls_made == 1`。那是在**模型完全没有做出任何决定**的情况下
        （请求根本没成功）替它执行一个带副作用的工具 —— 正是 owner 要删的本地否决权，
        所以这条测试是在忠实记录一个 bug。

        场景保持不变（视频直链 + LLM 鉴权失败），断言改为记录修复后的行为：
        一个工具都不调，把鉴权错误如实告诉用户。
        超时路径不同：超时时会发起第二次真实 LLM 调用（小 prompt 重试），
        那是模型自己选的工具，所以超时仍然能落到工具，见上面几条测试。
        """
        registry = _Registry()
        loop = AgentLoop(
            model_client=_ErrorModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-llm-error-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], [])
        self.assertEqual(result.tool_calls_made, 0)
        self.assertEqual(result.reason, "agent_llm_error")
        self.assertIn("鉴权失败", result.reply_text)

    def test_timeout_after_navigator_policy_block_still_falls_back_to_tool(self):
        """policy_guard 挡掉硬答之后再超时，仍然要落到工具。

        `_has_only_navigator_retry_steps` 把「只有 policy_guard 拦截记录」的 steps
        视为「还没真正调过工具」，所以小 prompt 重试照样允许触发；
        工具名由重试里的模型给出。
        """
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "final_answer",
                            "args": {"text": "没有工具结果，先硬答。"},
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "parse_video",
                            "args": {"url": "https://v.douyin.com/demo/"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "final_answer",
                            "args": {"text": "解析好了，我直接把视频发出来。"},
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="解析 https://v.douyin.com/demo/",
            trace_id="navigator-policy-block-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["parse_video"])
        self.assertEqual(result.video_url, "/tmp/yukiko/demo.mp4")
        self.assertTrue(any(step.get("error") == "navigator_tool_required_before_final_answer" for step in result.steps))
        self.assertEqual(result.reason, "agent_final_answer")

    def test_web_research_llm_timeout_falls_back_to_search_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="我要看异环的新手教程",
            trace_id="navigator-web-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_web_research_url_with_extra_instruction_does_not_force_fetch_on_timeout(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="网络时光机 看 skiapi.dev",
            trace_id="navigator-web-wayback-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_web_research_timeout_uses_tiny_tool_retry_for_wayback(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "wayback_lookup",
                            "args": {"url": "https://dwgx.top"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "查到归档了。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="网络时光机 看 dwgx.top以前有什么",
            trace_id="navigator-web-wayback-tool-retry-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["wayback_lookup"])
        self.assertEqual(registry.calls[0][1]["url"], "https://dwgx.top")
        self.assertEqual(result.reply_text, "查到归档了。")

    def test_wayback_timeline_failure_falls_back_to_lookup(self):
        """A7-3：工具失败后的替代工具由模型选，不再由 _fallback_tool_on_failure 的 if-链选。

        场景与断言都保留：wayback_timeline 失败后仍然要落到 wayback_lookup、
        limit 仍然被收敛到 20、用户仍然拿到 "wayback ok"。
        区别在于第二次调用现在是模型看到失败 observation 后自己发起的，
        会如实出现在 steps 里；旧实现是在同一个 step 里静默执行第二个工具，
        模型既看不到那次调用，也无法否决它。
        """
        registry = _WaybackTimelineFailRegistry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "wayback_timeline",
                            "args": {"url": "gov.cn", "limit": 500},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "wayback_lookup",
                            "args": {"url": "gov.cn", "limit": 20},
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="网络时光机 看 gov.cn 以前有什么",
            trace_id="navigator-wayback-timeline-fallback-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["wayback_timeline", "wayback_lookup"])
        self.assertEqual(registry.calls[1][1]["url"], "gov.cn")
        self.assertEqual(registry.calls[1][1]["limit"], 20)
        self.assertIn("wayback ok", result.reply_text)

    def test_general_chat_timeout_uses_tiny_navigator_retry_before_giving_up(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutThenSectionModelClient("media_search"),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="给我发一个异环的视频",
            trace_id="navigator-tiny-retry-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")
        self.assertEqual(registry.calls, [])

    def test_general_timeout_can_switch_section_then_tiny_tool_retry(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "section_id": "media_search",
                            "reason": "用户要找并发送视频",
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "异环宣传片", "media_type": "video"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "找到视频。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="给我发一个异环的视频",
            trace_id="navigator-section-then-tool-retry-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(
            registry.calls[0][1],
            {"query": "异环宣传片", "media_type": "video"},
        )
        self.assertEqual(result.video_url, "/tmp/yukiko/search.mp4")
        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))

    def test_tiny_navigator_retry_uses_configured_model(self):
        registry = _Registry()
        client = _RecordingNavigatorModelClient()
        loop = AgentLoop(
            model_client=client,
            tool_registry=registry,
            config={
                "agent": {
                    "enable": True,
                    "max_steps": 5,
                    "fallback_on_parse_error": True,
                    "navigator_retry_model": "gpt-5.4-mini",
                },
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="给我发一个异环的视频",
            trace_id="navigator-retry-model-test",
        )
        visible_tools = [
            "think",
            "final_answer",
            "navigate_section",
            "search_media",
            "web_search",
            "fetch_webpage",
            "parse_video",
            "search_download_resources",
        ]
        ctx.navigator_state = PromptNavigator.from_payload(
            default_prompt_navigator_payload()
        ).initial_state(ctx, visible_tools)

        retry = asyncio.run(
            loop._navigator_timeout_section_retry(
                ctx=ctx,
                step_idx=0,
                tool_calls_made=0,
                steps=[],
                remaining=60.0,
            )
        )

        self.assertEqual(client.models, ["gpt-5.4-mini"])
        self.assertIsNotNone(retry)
        self.assertEqual(retry[0], "media_search")
        self.assertEqual(retry[2], "search_media")

    def test_fallback_does_not_leak_navigator_section_prompt(self):
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=_Registry(),
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="给我发一个异环的视频",
            trace_id="navigator-fallback-leak-test",
        )

        result = asyncio.run(
            loop._build_fallback_result(
                ctx,
                [
                    {
                        "tool": "navigate_section",
                        "ok": True,
                        "display": "当前分区: media_search\n当前分区可见工具: search_media",
                    }
                ],
                tool_calls_made=0,
                t0=time.monotonic(),
                reason="llm_timeout",
            )
        )

        self.assertNotIn("当前分区", result.reply_text)
        self.assertNotIn("search_media", result.reply_text)

    def test_last_success_display_ignores_navigator_observation(self):
        display = AgentLoop._last_success_display(
            [
                {"tool": "web_search", "ok": True, "display": "搜索结果摘要"},
                {
                    "tool": "navigate_section",
                    "ok": True,
                    "display": "当前分区: media_search\n当前分区可见工具: search_media",
                },
            ]
        )

        self.assertEqual(display, "搜索结果摘要")

    def test_general_timeout_section_retry_can_seed_next_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {
                            "section_id": "media_search",
                            "reason": "用户要找并发送视频",
                            "tool": "search_media",
                            "args": {
                                "query": "你会选择什么男孩",
                                "media_type": "video",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="发一个 你会选择什么男孩的视频",
            trace_id="navigator-section-seeded-tool-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(
            registry.calls[0][1],
            {"query": "你会选择什么男孩", "media_type": "video"},
        )
        self.assertEqual(result.video_url, "/tmp/yukiko/search.mp4")
        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))

    def test_preflight_can_switch_plain_text_before_full_prompt_timeout(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "section_id": "media_search",
                            "reason": "用户要找并发送视频",
                            "tool": "search_media",
                            "args": {
                                "query": "你会选择什么男孩",
                                "media_type": "video",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    asyncio.TimeoutError(),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {
                    "enable": True,
                    "max_steps": 5,
                    "fallback_on_parse_error": True,
                    "navigator_preflight_plain_text": True,
                },
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="发一个 你会选择什么男孩的视频",
            trace_id="navigator-preflight-plain-text-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(result.video_url, "/tmp/yukiko/search.mp4")
        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))

    def test_media_search_llm_timeout_falls_back_to_search_media_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="给我找一个异环新手教程视频，直接发最合适的",
            trace_id="navigator-media-search-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_search_media_uses_media_tool_timeout_budget(self):
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=_Registry(),
            config={
                "agent": {
                    "enable": True,
                    "tool_timeout_seconds": 30,
                    "tool_timeout_seconds_media": 60,
                },
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )

        self.assertGreaterEqual(loop._resolve_tool_timeout_seconds("search_media", False), 120.0)

    def test_media_search_fallback_prefers_current_image_request_over_reply_video_text(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "navigate_section",
                            "args": {
                                "section_id": "media_search",
                                "reason": "用户要发送图片，当前分区没有媒体检索工具",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "猫图", "media_type": "image"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "给你猫图。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="那张猫图发一下，不要发视频",
            reply_to_text="解析好了，我直接把视频发出来。",
            trace_id="navigator-media-image-reply-video-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(registry.calls[0][1]["media_type"], "image")
        self.assertEqual(registry.calls[0][1]["query"], "猫图")
        self.assertEqual(result.image_url, "https://example.test/image.jpg")
        self.assertEqual(result.video_url, "")

    def test_media_search_free_text_uses_navigator_switch_before_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "navigate_section",
                            "args": {
                                "section_id": "media_search",
                                "reason": "用户想看主题视频，需要进入媒体检索分区",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "异环宣传片", "media_type": "video"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"tool": "final_answer", "args": {"text": "找到视频。"}},
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="我想看异环宣传片，找最合适的直接发",
            trace_id="navigator-media-search-switch-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(registry.calls[0][1], {"query": "异环宣传片", "media_type": "video"})
        self.assertEqual(result.video_url, "/tmp/yukiko/search.mp4")
        self.assertTrue(any(step.get("tool") == "navigate_section" for step in result.steps))

    def test_media_search_tool_image_survives_text_only_final_answer(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_SequencedModelClient(
                [
                    json.dumps(
                        {
                            "tool": "search_media",
                            "args": {"query": "猫咪", "media_type": "image"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "tool": "final_answer",
                            "args": {"text": "先给你一张猫咪图。"},
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="找一张猫咪图片发出来",
            trace_id="navigator-media-image-final-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["search_media"])
        self.assertEqual(result.image_url, "https://example.test/image.jpg")
        self.assertEqual(result.image_urls, ["https://example.test/image.jpg"])

    def test_download_llm_timeout_falls_back_to_download_search_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="帮我找一下 OBS Windows 安装包 exe 下载",
            trace_id="navigator-download-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_creative_generation_llm_timeout_falls_back_to_image_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="帮我画一张赛博猫娘头像",
            trace_id="navigator-image-gen-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_memory_llm_timeout_falls_back_to_recall_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="你记得我是谁吗",
            trace_id="navigator-memory-recall-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_music_llm_timeout_falls_back_to_music_play_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="点歌 Never Gonna Give You Up - Rick Astley，直接发语音",
            trace_id="navigator-music-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_sticker_llm_timeout_falls_back_to_send_face_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="发一个点赞 QQ 表情",
            trace_id="navigator-sticker-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual(registry.calls, [])
        self.assertEqual(result.reason, "agent_llm_timeout")

    def test_multimodal_llm_timeout_falls_back_to_analyze_image_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            # 模型只给工具名和空 args；question 由 _normalize_tool_args 从 ctx 结构补进去，
            # 这条「结构补参」是 KEEP 类，断言照旧。
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {"tool": "analyze_image", "args": {}}, ensure_ascii=False
                    ),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="分析一下引用的这张图",
            reply_media_summary=["image:https://example.test/cat.png"],
            trace_id="navigator-image-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["analyze_image"])
        self.assertIn("引用的这张图", registry.calls[0][1]["question"])
        self.assertEqual(result.reply_text, "image analysis ok")
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")

    def test_web_url_llm_timeout_falls_back_to_fetch_tool(self):
        registry = _Registry()
        loop = AgentLoop(
            # 裸域名 skiapi.dev 补全成 https://skiapi.dev 是结构补参，仍由 agent 做。
            model_client=_SequencedModelClient(
                [
                    asyncio.TimeoutError(),
                    json.dumps(
                        {"tool": "fetch_webpage", "args": {}}, ensure_ascii=False
                    ),
                    asyncio.TimeoutError(),
                ]
            ),
            tool_registry=registry,
            config={
                "agent": {"enable": True, "max_steps": 5, "fallback_on_parse_error": True},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        loop.high_risk_control_enable = False
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="skiapi.dev",
            trace_id="navigator-fetch-timeout-test",
        )

        result = asyncio.run(loop.run(ctx))

        self.assertEqual([name for name, _ in registry.calls], ["fetch_webpage"])
        self.assertEqual(registry.calls[0][1]["url"], "https://skiapi.dev")
        self.assertEqual(result.reason, "agent_fallback_llm_timeout")


if __name__ == "__main__":
    unittest.main()
