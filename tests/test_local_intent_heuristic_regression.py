from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core.tools_video as tools_video
from core.agent import AgentLoop
from core.config_templates import _built_in_config_defaults
from core.engine import EngineMessage, YukikoEngine
from core.router import RouterDecision, RouterEngine
from core.tools import ToolExecutor
from core.trigger import TriggerEngine


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class _Bilibili412YoutubeDL:
    calls: list[str] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.calls.append(str(options.get("format", "")))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def extract_info(self, source_url: str, download: bool = True):
        _ = source_url, download
        raise RuntimeError("HTTP Error 412: Precondition Failed")


class _AcFunVideoOnlyYoutubeDL:
    calls: list[str] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.calls.append(str(options.get("format", "")))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def extract_info(self, source_url: str, download: bool = True):
        _ = source_url, download
        outtmpl = str(self.options.get("outtmpl", ""))
        path = Path(outtmpl.replace("%(id)s", "ac10315127").replace("%(ext)s", "mp4"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        return {"requested_downloads": [{"filepath": str(path)}]}


class LocalIntentHeuristicRegressionTests(unittest.TestCase):
    def test_passive_group_image_followup_does_not_enter_agent(self) -> None:
        """原断言：群友互发图 → 本地闸门直接 return，模型看不到这轮。

        现断言：闸门已删。同一场景照样进 Agent，由模型读 general_chat 分区说明
        （「判断该沉默时调用 final_answer 并把 text 留空」）自己选择沉默。
        「被动多模态信封」这个**结构事实**保留，作为喂给模型的判断依据。
        """

        engine = YukikoEngine.__new__(YukikoEngine)
        engine._recent_directed_hints = {}
        engine._looks_like_bot_call = lambda text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            text="MULTIMODAL_EVENT user sent multimodal message: image:[image]",
            mentioned=False,
            is_private=False,
        )

        self.assertFalse(hasattr(engine, "_should_ignore_passive_multimodal_turn"))
        # 结构事实仍然可用（它现在只作为 evidence，不再作为否决权）
        self.assertTrue(YukikoEngine._is_passive_multimodal_text(message.text))

    def test_bilibili_cookie_reaches_hybrid_resolver(self) -> None:
        executor = ToolExecutor(
            None,
            None,
            lambda *args, **kwargs: None,
            {"video_analysis": {"bilibili": {"sessdata": "sess", "bili_jct": "csrf"}}},
        )

        self.assertEqual(executor._bilibili_sessdata, "sess")
        self.assertEqual(getattr(executor._hybrid_resolver.bilix_resolver, "sess_data", ""), "sess")

    def test_acfun_silent_video_is_allowed_for_delivery(self) -> None:
        executor = _DummyExecutor()

        self.assertTrue(executor._video_require_audio_for_send)
        self.assertTrue(executor._allow_silent_video_for_url("https://www.acfun.cn/v/ac10315127"))
        self.assertFalse(executor._allow_silent_video_for_url("https://www.bilibili.com/video/BV1xx411c7mD/"))

    def test_bilibili_412_aborts_after_first_format(self) -> None:
        executor = _DummyExecutor()
        url = "https://www.bilibili.com/video/BV1xx411c7mD/"
        _Bilibili412YoutubeDL.calls = []

        with patch.object(tools_video, "YoutubeDL", _Bilibili412YoutubeDL):
            result = executor._download_platform_video_sync(url)

        self.assertIsNone(result)
        self.assertEqual(len(_Bilibili412YoutubeDL.calls), 1)
        self.assertEqual(
            executor._last_video_download_error.get(url),
            "bilibili_412_throttled",
        )

    def test_acfun_video_only_returns_first_valid_file(self) -> None:
        executor = _DummyExecutor()
        url = "https://www.acfun.cn/v/ac10315127"
        _AcFunVideoOnlyYoutubeDL.calls = []

        with patch.object(tools_video, "YoutubeDL", _AcFunVideoOnlyYoutubeDL):
            result = executor._download_platform_video_sync(url)

        self.assertIsNotNone(result)
        self.assertEqual(len(_AcFunVideoOnlyYoutubeDL.calls), 1)
        self.assertTrue(str(result).endswith(".mp4"))
        executor._safe_unlink(result)

    def test_video_unsupported_message_lists_all_supported_platforms(self) -> None:
        executor = _DummyExecutor()

        result = asyncio.run(
            executor._method_browser_resolve_video(
                "parse_video",
                {"url": "https://example.com/watch?v=1"},
                "",
            )
        )

        text = str((result.payload or {}).get("text", ""))
        self.assertFalse(result.ok)
        self.assertIn("腾讯视频", text)
        self.assertIn("爱奇艺", text)
        self.assertIn("YouTube", text)
        self.assertIn("优酷", text)

    def test_iqiyi_phantomjs_missing_has_specific_diagnostic(self) -> None:
        executor = _DummyExecutor()
        url = "https://www.iqiyi.com/v_19rr7p0r18.html"
        proc = SimpleNamespace(returncode=1, stderr="PhantomJS not found", stdout="")

        with patch.object(tools_video.subprocess, "run", return_value=proc):
            result = executor._download_iqiyi_video_subprocess_sync(
                url,
                "iqiyi-test",
                str(executor._video_cache_dir / "iqiyi-test_%(id)s.%(ext)s"),
            )

        self.assertIsNone(result)
        self.assertEqual(executor._last_video_download_error.get(url), "iqiyi_phantomjs_missing")
        self.assertIn(
            "PhantomJS",
            executor._build_video_resolve_failed_text("iqiyi_phantomjs_missing"),
        )

    def test_directed_image_still_enters_agent(self) -> None:
        """契约不变：被 @ 且带图的消息必须进 Agent。

        以前靠 `_should_ignore_passive_multimodal_turn` 返回 False 放行，
        现在没有闸门可拦，因此**无条件**进 Agent —— 契约由「闸门放行」升级为「不存在闸门」。
        这里同时锁住信封解析这个结构能力：@ 版信封要能被识别出来，
        用户正文要能从信封里抽出来喂给模型。
        """

        engine = YukikoEngine.__new__(YukikoEngine)
        engine._recent_directed_hints = {}
        engine._looks_like_bot_call = lambda text: False

        message = EngineMessage(
            conversation_id="group:1",
            user_id="2",
            text="MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: image:[image]",
            mentioned=True,
            is_private=False,
        )

        self.assertFalse(hasattr(engine, "_should_ignore_passive_multimodal_turn"))
        self.assertTrue(YukikoEngine._is_passive_multimodal_text(message.text))


    def test_engine_followup_keywords_do_not_trigger_local_guesses(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False
        engine._get_bot_aliases = lambda: {"yukiko"}

        self.assertFalse(
            YukikoEngine._looks_like_summary_followup("\u603b\u7ed3\u4e00\u4e0b")
        )
        self.assertFalse(
            YukikoEngine._looks_like_resend_followup("\u518d\u53d1\u4e00\u904d")
        )
        self.assertFalse(
            YukikoEngine._looks_like_source_trace_followup(
                "\u4f60\u7528\u4e86\u4ec0\u4e48\u94fe\u63a5"
            )
        )
        self.assertFalse(
            YukikoEngine._looks_like_sticker_request("\u53d1\u4e2a\u8868\u60c5\u5305")
        )
        self.assertFalse(
            YukikoEngine._looks_like_video_text_only_intent("\u53ea\u8981\u603b\u7ed3")
        )
        self.assertFalse(
            YukikoEngine._looks_like_music_request("\u70b9\u6b4c \u70ed\u6c34\u6fa1")
        )
        # engine \u4fa7 `_looks_like_qq_avatar_intent` / `_looks_like_local_file_request`
        # \u5df2\u5220\u9664\uff08\u5220\u6389 `_should_prefer_router_for_plain_text` \u4e0e self_check \u540e\u5b83\u4eec\u751f\u4ea7\u5f15\u7528\u4e3a 0\uff09\u3002
        # \u540c\u4e00\u6761\u300c\u4e2d\u6587\u8bf4\u6cd5\u4e0d\u5f97\u547d\u4e2d\u300d\u5951\u7ea6\u7531\u6d3b\u7740\u7684\u90a3\u4efd\u6301\u6709\uff0c\u89c1\u4e0b\u65b9 typed-command \u6d4b\u8bd5\u3002
        self.assertFalse(hasattr(engine, "_looks_like_qq_avatar_intent"))
        self.assertFalse(hasattr(YukikoEngine, "_looks_like_local_file_request"))
        self.assertFalse(
            ToolExecutor._looks_like_qq_avatar_request(
                "\u67e5\u4e00\u4e0b\u6211\u7684\u5934\u50cf"
            )
        )
        self.assertFalse(engine._looks_like_bot_call("\u4f60\u770b\u770b\u8fd9\u4e2a"))

    def test_engine_only_accepts_explicit_control_tokens_or_structure(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._is_passive_multimodal_text = lambda text: False
        engine._get_bot_aliases = lambda: {"yukiko"}

        self.assertTrue(YukikoEngine._looks_like_summary_followup("/summary"))
        self.assertTrue(YukikoEngine._looks_like_resend_followup("/resend"))
        self.assertTrue(YukikoEngine._looks_like_source_trace_followup("/sources"))
        self.assertTrue(YukikoEngine._looks_like_sticker_request("/sticker"))
        self.assertTrue(YukikoEngine._looks_like_video_text_only_intent("output=text"))
        self.assertTrue(
            YukikoEngine._looks_like_music_request("/music \u70ed\u6c34\u6fa1")
        )
        # `/avatar target=self` 这条 typed-command 契约仍然在，只是活的那份在 core/tools.py
        # （engine 里的重复定义已删）。契约内容逐字未变。
        self.assertTrue(ToolExecutor._looks_like_qq_avatar_request("/avatar target=self"))
        self.assertTrue(ToolExecutor._looks_like_qq_avatar_request("/avatar target=me"))
        # 本地文件能力不再有 engine 侧词表入口，改由模型进 group_files 分区拿工具。
        from core.prompt_navigator import (
            PromptNavigator,
            default_prompt_navigator_payload,
        )

        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        self.assertIn(
            "upload_group_file", nav.config.sections["group_files"].tools
        )
        self.assertTrue(engine._looks_like_bot_call("yukiko?"))

    def test_recent_user_image_followup_can_wake_from_not_directed(self) -> None:
        class _RecentMediaTools:
            def __init__(self) -> None:
                self._recent_media_by_conversation = {
                    "group:901:user:347": {
                        "image": ["https://example.com/a.png"],
                    },
                    "group:901:user:999": {
                        "image": ["https://example.com/other.png"],
                    },
                }

        engine = YukikoEngine.__new__(YukikoEngine)
        engine.tools = _RecentMediaTools()
        message = EngineMessage(
            conversation_id="group:901",
            group_id=901,
            user_id="347",
            text="\u76f4\u63a5cyber\u6389",
            mentioned=False,
            is_private=False,
        )

        # 32 \u8bcd\u7684\u7f16\u8f91/\u5206\u6790\u8bcd\u8868\u5df2\u5220\u9664\uff1a\u5524\u9192\u5224\u636e\u53ea\u5269\u7ed3\u6784\u4e8b\u5b9e\u3002
        self.assertFalse(
            hasattr(engine, "_looks_like_recent_media_followup_instruction")
        )
        self.assertTrue(engine._looks_like_recent_media_followup(message, message.text))
        self.assertEqual(
            engine._build_recent_media_summary_for_followup(message),
            ["image:https://example.com/a.png"],
        )

        # \u539f\u65ad\u8a00\uff1a\u300c\u968f\u4fbf\u804a\u804a\u300d\u4e0d\u542b\u8bcd\u8868\u8bcd \u2192 \u4e0d\u5524\u9192\u3002
        # \u73b0\u65ad\u8a00\uff1a\u540c\u4e00\u4e2a\u4eba\u3001\u540c\u4e00\u4e2a\u4f1a\u8bdd\u91cc\u521a\u51fa\u73b0\u8fc7\u56fe\u7247\uff0c\u8fd9\u662f\u5ba2\u89c2\u4e8b\u5b9e\uff0c\u7167\u6837\u9001\u8fdb\u6a21\u578b\u89c6\u91ce\uff1b
        # \u8fd9\u53e5\u8bdd\u5230\u5e95\u662f\u4e0d\u662f\u5728\u6307\u90a3\u5f20\u56fe\uff0c\u7531\u6a21\u578b\u8bfb multimodal_media \u5206\u533a\u81ea\u5df1\u5224\u65ad\uff0c
        # \u5224\u65ad\u65e0\u5173\u65f6\u7528\u7a7a\u6587\u672c final_answer \u6536\u573a\u3002\u672c\u5730\u4e0d\u518d\u66ff\u6a21\u578b\u4e0b\u7ed3\u8bba\u3002
        quiet = EngineMessage(
            conversation_id="group:901",
            group_id=901,
            user_id="347",
            text="\u968f\u4fbf\u804a\u804a",
            mentioned=False,
            is_private=False,
        )
        self.assertTrue(engine._looks_like_recent_media_followup(quiet, quiet.text))

        cross_user = EngineMessage(
            conversation_id="group:901",
            group_id=901,
            user_id="555",
            text="\u76f4\u63a5cyber\u6389",
            mentioned=False,
            is_private=False,
        )
        self.assertFalse(
            engine._looks_like_recent_media_followup(cross_user, cross_user.text)
        )

    def test_agent_context_and_inference_helpers_require_structure(self) -> None:
        self.assertFalse(
            AgentLoop._looks_like_reference_to_previous_link("\u90a3\u4e2a\u94fe\u63a5")
        )
        self.assertTrue(AgentLoop._looks_like_reference_to_previous_link("/source"))
        self.assertFalse(AgentLoop._is_context_continuation_phrase("\u7ee7\u7eed"))
        self.assertFalse(
            AgentLoop._is_context_continuation_phrase("\u6240\u4ee5\u5462")
        )
        self.assertTrue(AgentLoop._is_context_continuation_phrase("/next"))
        self.assertEqual(
            AgentLoop._strip_continuation_prefix("\u7ee7\u7eed \u5e2e\u6211\u770b"),
            "\u7ee7\u7eed \u5e2e\u6211\u770b",
        )
        self.assertEqual(AgentLoop._strip_continuation_prefix("/next foo"), "foo")
        self.assertEqual(
            AgentLoop._strip_continuation_prefix(
                "\u6240\u4ee5\u5462 \u5e2e\u6211\u770b"
            ),
            "\u6240\u4ee5\u5462 \u5e2e\u6211\u770b",
        )

        # A7\uff1a\u8fd9\u4e24\u4e2a\u7b26\u53f7\u5df2\u6574\u4f53\u5220\u9664\u3002\u539f\u65ad\u8a00\u9a8c\u7684\u662f\u300c\u81ea\u7136\u4e2d\u6587\u4e0d\u547d\u4e2d\u3001\u53ea\u6709\u663e\u5f0f\u4ee4\u724c\u547d\u4e2d\u300d\uff0c
        # \u5951\u7ea6\u73b0\u5728\u7531 schema \u627f\u62c5 \u2014\u2014 \u4e0b\u8f7d\u683c\u5f0f\u6539\u4e3a\u6a21\u578b\u81ea\u5df1\u586b prefer_ext / file_type\uff0c
        # \u7559\u7a7a\u5373\u5168\u7c7b\u578b\u641c\u7d22\uff0c\u5de5\u5177\u4e0d\u518d\u4ece\u4e2d\u6587\u91cc\u5265\u683c\u5f0f\u3002
        self.assertFalse(hasattr(AgentLoop, "_infer_resource_file_type"))
        self.assertFalse(hasattr(AgentLoop, "_looks_like_download_file_request"))
        self.assertFalse(hasattr(AgentLoop, "_looks_like_file_send_request"))

        self.assertEqual(
            AgentLoop._infer_split_video_mode("\u63d0\u53d6\u97f3\u9891"), ""
        )
        self.assertEqual(AgentLoop._infer_split_video_mode("mode=audio"), "audio")
        self.assertEqual(AgentLoop._infer_split_video_mode("12s-20s"), "clip")

        self.assertEqual(AgentLoop._infer_frame_count_hint("\u4e5d\u5bab\u683c"), 0)
        self.assertEqual(AgentLoop._infer_frame_count_hint("max_frames=9"), 9)
        self.assertEqual(AgentLoop._infer_frame_count_hint("9 screenshots"), 9)

        self.assertEqual(
            AgentLoop._infer_video_time_hints("\u4ece 10 \u5230 20 \u79d2"),
            {"point": 10.0},
        )
        self.assertEqual(
            AgentLoop._infer_video_time_hints("10s-20s"), {"start": 10.0, "end": 20.0}
        )

        # A7：_fallback_tool_on_failure 已删。它原来在工具失败后用关键词挑替代工具
        # （smart_download 失败 -> web_search / search_download_resources），
        # 那是本地替模型改主意。现在工具失败作为 observation 回给模型，由模型自己决定
        # 换哪个工具或换个说法 —— 它看得到分区里所有可用工具，判断依据比这段代码多。
        self.assertFalse(hasattr(AgentLoop, "_fallback_tool_on_failure"))

    def test_trigger_and_router_drop_local_keyword_defaults(self) -> None:
        trigger = TriggerEngine({}, {"name": "YuKiKo", "nicknames": []})
        self.assertEqual(trigger.ai_listen_keywords, [])
        self.assertFalse(hasattr(trigger, "explicit_request_cues"))
        self.assertEqual(
            trigger._structural_request_signal("\u5e2e\u6211\u67e5\u4e00\u4e0b"), 0.0
        )
        self.assertGreater(trigger._structural_request_signal("/lookup test"), 0.0)
        # \u95ee\u53f7\u4e0e\u53e5\u957f\u8fd9\u4e24\u4e2a\u8bed\u4e49\u52a0\u5206\u4f4d\u5df2\u5220\u9664\uff1a\u7eaf\u81ea\u7531\u6587\u672c\u5fc5\u987b\u6052\u4e3a 0\u3002
        self.assertEqual(
            trigger._structural_request_signal(
                "\u8fd9\u4e2a\u89c6\u9891\u5230\u5e95\u8bb2\u4e86\u4ec0\u4e48"
                "\u5185\u5bb9\u554a\u6709\u70b9\u770b\u4e0d\u61c2\uff1f"
            ),
            0.0,
        )
        self.assertFalse(RouterEngine._contains_explicit_adult_intent("\u6da9\u56fe"))
        self.assertTrue(RouterEngine._contains_explicit_adult_intent("/nsfw"))

    def test_tools_do_not_route_on_natural_language_cues(self) -> None:
        executor = _DummyExecutor()

        self.assertFalse(
            executor._looks_like_music_request("\u70b9\u6b4c \u70ed\u6c34\u6fa1")
        )
        self.assertFalse(
            executor._looks_like_video_request("\u53d1\u4e2a\u6296\u97f3\u89c6\u9891")
        )
        self.assertFalse(
            executor._looks_like_image_analysis_request(
                "\u770b\u770b\u8fd9\u5f20\u56fe"
            )
        )
        self.assertFalse(
            executor._looks_like_video_analysis_request(
                "\u603b\u7ed3\u4e00\u4e0b\u8fd9\u4e2a\u89c6\u9891"
            )
        )
        self.assertFalse(
            executor._looks_like_qq_avatar_request(
                "\u67e5\u4e00\u4e0b\u6211\u7684\u5934\u50cf"
            )
        )
        self.assertFalse(
            executor._looks_like_analysis_text_only_request("\u53ea\u8981\u603b\u7ed3")
        )
        self.assertFalse(
            executor._looks_like_weak_vision_answer(
                "\u7ed3\u679c\u4e0d\u591f\u7a33\u5b9a"
            )
        )

    def test_tools_accept_only_explicit_tokens_or_media_locators(self) -> None:
        executor = _DummyExecutor()

        self.assertTrue(executor._looks_like_music_request("/music \u70ed\u6c34\u6fa1"))
        self.assertTrue(
            executor._looks_like_video_request("https://example.com/demo.mp4")
        )
        self.assertTrue(
            executor._looks_like_video_analysis_request(
                "/analyze https://example.com/demo.mp4"
            )
        )
        self.assertTrue(
            executor._looks_like_image_analysis_request(
                "/analyze https://example.com/demo.png"
            )
        )
        self.assertTrue(executor._looks_like_qq_avatar_request("/avatar target=self"))
        self.assertTrue(executor._looks_like_analysis_text_only_request("output=text"))
        self.assertTrue(executor._looks_like_weak_vision_answer("???"))
        self.assertEqual(
            executor._build_targeted_video_queries("\u6296\u97f3 \u732b\u732b"),
            [
                "\u6296\u97f3 \u732b\u732b site:bilibili.com/video",
                "\u6296\u97f3 \u732b\u732b site:douyin.com/video",
                "\u6296\u97f3 \u732b\u732b site:kuaishou.com/short-video",
                "\u6296\u97f3 \u732b\u732b site:acfun.cn/v/ac",
                "\u6296\u97f3 \u732b\u732b site:youtube.com/watch",
                "\u6296\u97f3 \u732b\u732b site:v.qq.com/x",
                "\u6296\u97f3 \u732b\u732b site:iqiyi.com/v_",
                "\u6296\u97f3 \u732b\u732b site:iqiyi.com/a_",
                "\u6296\u97f3 \u732b\u732b site:iq.com/play",
            ],
        )
        self.assertEqual(
            executor._build_targeted_video_queries("platform=douyin cat"),
            ["platform=douyin cat site:douyin.com/video"],
        )
        for url in (
            "https://www.bilibili.com/video/BV1xx411c7mD/",
            "https://b23.tv/abc123",
            "https://www.acfun.cn/v/ac12345678",
            "https://v.douyin.com/hskaBb36Hfg/",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://v.qq.com/x/page/m3534f3t3hb.html",
            "https://www.iqiyi.com/v_19rr7p0r18.html",
            "https://www.iq.com/play/demo-12345",
        ):
            self.assertTrue(executor._is_supported_platform_video_url(url), url)
            self.assertTrue(executor._is_platform_video_detail_url(url), url)
        self.assertEqual(executor._pick_gif_keyframe_indexes(1), [0])
        self.assertEqual(executor._pick_gif_keyframe_indexes(3), [0, 1, 2])
        self.assertEqual(executor._pick_gif_keyframe_indexes(8), [0, 1, 3, 4, 6, 7])
        animated_prompt = executor._build_vision_prompt(
            query="这个表情什么意思",
            message_text="[动画表情]",
            animated_hint=True,
        )
        self.assertIn("动画表情", animated_prompt)
        self.assertIn("多帧拼图", animated_prompt)
        self.assertFalse(executor._looks_like_weak_vision_answer("看不清"))

    def test_vision_tools_take_explicit_args_instead_of_keyword_guessing(self) -> None:
        """A9：core/tools_vision.py 的自由文本猜测已改成显式工具参数。

        原来这三个函数从用户原话猜「要联网查出处」「要批量看所有图」「要走最近图兜底」，
        现在这些决定由模型读 multimodal_media 分区说明后显式填 analyze_image 的参数。
        """
        executor = _DummyExecutor()

        for gone in (
            "_looks_like_vision_web_lookup_request",
            "_looks_like_analyze_all_images_request",
            "_analyze_image_from_message",
        ):
            self.assertFalse(hasattr(executor, gone), gone)

        # 联网补查的门只认显式参数。原来「查一下这张图的出处」命中词表会自动联网。
        for text in ("查一下这张图的出处", "这是谁啊", "帮我查一下这是哪部动漫"):
            self.assertIsNone(
                asyncio.run(
                    executor._vision_uncertain_web_fallback(
                        query=text,
                        message_text="",
                        web_lookup_requested=False,
                    )
                ),
                text,
            )

        # 动图判断只吃 OneBot image 段的结构元数据，不吃用户原话。
        self.assertTrue(
            executor._has_animated_image_hint(
                raw_segments=[
                    {"type": "image", "data": {"sub_type": "1", "file": "a.png"}}
                ]
            )
        )
        self.assertTrue(
            executor._has_animated_image_hint(
                raw_segments=[{"type": "image", "data": {"file": "b.gif"}}]
            )
        )
        self.assertTrue(
            executor._has_animated_image_hint(
                raw_segments=[
                    {"type": "image", "data": {"summary": "[动画表情]", "file": "c.png"}}
                ]
            )
        )
        self.assertFalse(
            executor._has_animated_image_hint(
                raw_segments=[{"type": "image", "data": {"file": "d.png"}}]
            )
        )
        self.assertFalse(
            executor._has_animated_image_hint(
                raw_segments=[{"type": "text", "data": {"text": "这个动图什么意思"}}]
            )
        )

        # 视觉 prompt 不再按「软件/任务栏」词表追加桌面截图指令；
        # 用户原话本来就逐字进 prompt，模型自己看得到。
        prompt = executor._build_vision_prompt(
            query="这截图里开着哪些软件", message_text="", animated_hint=False
        )
        self.assertNotIn("任务栏截图", prompt)
        self.assertIn("开着哪些软件", prompt)

    def test_vision_keyword_contracts_now_live_in_navigator_section(self) -> None:
        """被删掉的词表所承载的契约，改由 multimodal_media 分区措辞 + 显式参数承担。"""
        from core.prompt_navigator import (
            PromptNavigator,
            default_prompt_navigator_payload,
        )

        class _NavCtx:
            message_text = ""
            original_message_text = ""
            reply_to_text = ""
            media_summary: list[str] = []
            reply_media_summary: list[str] = []
            raw_segments: list[dict] = []
            reply_media_segments: list[dict] = []
            at_other_user_ids: list[str] = []
            recent_media_artifact: dict = {}

        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = _NavCtx()
        # 结构信号（消息里真有 image 段）才是起始分区的依据，不是「看看这张图」这句话。
        ctx.raw_segments = [{"type": "image", "data": {"url": "file://demo.png"}}]
        state = nav.initial_state(
            ctx, ["think", "final_answer", "navigate_section", "analyze_image"]
        )
        self.assertEqual(state.active_section, "multimodal_media")
        self.assertIn("analyze_image", nav.scoped_tools(state))

        block = nav.render_system_block(state, nav.scoped_tools(state))
        for arg in (
            "analyze_all",
            "max_images",
            "is_animated",
            "web_lookup_on_uncertain",
            "target_message_id",
        ):
            self.assertIn(arg, block, arg)

    def test_tools_no_longer_local_match_vision_refusal_templates(self) -> None:
        executor = _DummyExecutor()
        executor._vision_retry_translate_enable = False

        payload = "I'm an AI assistant and cannot analyze this image."

        self.assertEqual(
            asyncio.run(executor._normalize_vision_answer(payload, prompt="")),
            payload,
        )

    def test_vision_describe_can_use_model_fallback_client(self) -> None:
        executor = _DummyExecutor()
        calls: list[str] = []

        async def fake_anthropic(**kwargs):
            calls.append(str(kwargs.get("model_name", "")))
            return "这张图里是一只猫"

        executor._vision_describe_via_anthropic = fake_anthropic  # type: ignore[method-assign]
        model_client = SimpleNamespace(
            provider="newapi",
            _primary_provider="newapi",
            _active_provider="newapi",
            _fallback_providers=["anthropic"],
            _fallback_clients={
                "anthropic": SimpleNamespace(
                    enabled=True,
                    api_key="sk-test",
                    base_url="https://anthropic.example",
                    model="claude-vision",
                    timeout_seconds=8,
                    temperature=0.2,
                    max_tokens=512,
                    prefer_v1=True,
                    anthropic_version="2023-06-01",
                    config={},
                )
            },
        )

        result = asyncio.run(
            executor._vision_describe_via_model_fallbacks(
                image_ref="data:image/png;base64,aaa",
                prompt="看图",
                model_client=model_client,
                tried_provider="newapi",
                tried_model="gpt-5.4",
            )
        )

        self.assertEqual(result, "这张图里是一只猫")
        self.assertEqual(calls, ["claude-vision"])

    def test_memory_followups_require_structure_not_local_link_words(self) -> None:
        self.assertFalse(
            YukikoEngine._looks_like_ambiguous_link_memory_query(
                "\u8fd8\u8bb0\u5f97\u90a3\u4e2a\u94fe\u63a5\u5417"
            )
        )
        self.assertTrue(YukikoEngine._looks_like_ambiguous_link_memory_query("/link"))
        self.assertFalse(
            YukikoEngine._looks_like_ambiguous_link_memory_query("/link `migu`")
        )
        self.assertEqual(
            YukikoEngine._extract_topic_terms_for_memory("\u8fd9\u4e2a \u90a3\u4e2a"),
            [],
        )
        self.assertEqual(
            YukikoEngine._extract_topic_terms_for_memory(
                "`migu` \u90a3\u4e2a", max_terms=2
            ),
            ["migu"],
        )

    def test_memory_guard_only_checks_explicit_structured_references(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        fallback = "\u6211\u521a\u624d\u90a3\u53e5\u5386\u53f2\u5f15\u7528\u4e0d\u51c6\u786e\uff0c\u5ffd\u7565\u5b83\u3002\u4f60\u73b0\u5728\u76f4\u63a5\u544a\u8bc9\u6211\u9700\u6c42\uff0c\u6211\u6309\u4f60\u8fd9\u6761\u6765\u3002"

        guarded = engine._guard_unverified_memory_claims(
            reply_text="\u4f60\u4e4b\u524d\u63d0\u5230\u8fc7\u300aOcean\u300b",
            user_text="",
            current_user_recent=["[\u5f53\u524d\u7528\u6237\u8fd1\u671f] Daylight"],
            related_memories=[],
        )
        self.assertEqual(guarded, fallback)

        untouched = engine._guard_unverified_memory_claims(
            reply_text="\u6211\u53ef\u80fd\u8bb0\u5f97\u4f60\u4e4b\u524d\u8bf4\u8fc7\u8fd9\u4e2a",
            user_text="",
            current_user_recent=["[\u5f53\u524d\u7528\u6237\u8fd1\u671f] Daylight"],
            related_memories=[],
        )
        self.assertEqual(
            untouched,
            "\u6211\u53ef\u80fd\u8bb0\u5f97\u4f60\u4e4b\u524d\u8bf4\u8fc7\u8fd9\u4e2a",
        )

    def test_self_check_veto_layer_is_gone_and_silence_moves_to_the_menu(self) -> None:
        """原断言：多人活跃群 + 未 @ + 无 listen_probe → `_self_check_decision` 返回
        `self_check:undirected_requires_listen_probe`，即本地 13 条规则一票否决模型判定。

        现断言：整个否决层不存在了。同一场景照样交模型，模型读 general_chat 分区说明
        自己选择「空文本 final_answer」表示沉默。契约（该沉默时要沉默）不变，
        变的是由谁做判断 —— 从代码事后否决改成模型自己决定。

        `_normalize_decision_with_tool_policy`（替模型补 query）与
        `_should_block_undirected_agent_plain_reply`（事后丢弃模型纯文本回复）同批删除。
        """

        engine = YukikoEngine.__new__(YukikoEngine)

        for gone in (
            "_self_check_decision",
            "_normalize_decision_with_tool_policy",
            "_should_block_undirected_agent_plain_reply",
            "_is_cross_user_context_collision",
        ):
            self.assertFalse(hasattr(engine, gone), gone)

        # 那 5 个只写不读的 self_check.* 阈值字段也一并不再解析。
        for dead_attr in (
            "self_check_enable",
            "self_check_block_at_other",
            "self_check_listen_probe_min_confidence",
            "self_check_non_direct_reply_min_confidence",
            "self_check_cross_user_guard_seconds",
        ):
            self.assertFalse(hasattr(engine, dead_attr), dead_attr)

    def test_undirected_group_chitchat_silence_is_reachable_in_menu(self) -> None:
        """接管能力必须真的在菜单里：非指向群闲聊落 general_chat，且该区教了怎么沉默。

        这是上一个测试删掉的 S-4 / S-5 / S-7 / S-10 / S-11 的语义承接点。
        """

        from core.prompt_navigator import (
            PromptNavigator,
            default_prompt_navigator_payload,
        )

        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        ctx = SimpleNamespace(
            message_text="这波怎么说",
            raw_segments=[],
            reply_media_segments=[],
            recent_media_artifact=None,
            mentioned=False,
            is_private=False,
            at_other_user_ids=[],
            reply_to_user_id="",
        )
        state = nav.initial_state(ctx, ["think", "final_answer", "navigate_section"])

        self.assertEqual(state.active_section, "general_chat")
        instructions = nav.config.sections["general_chat"].instructions
        self.assertIn("final_answer", instructions)
        self.assertIn("空", instructions)

    def test_choice_followups_accept_only_structural_number_forms(self) -> None:
        self.assertEqual(YukikoEngine._extract_choice_index("1"), 1)
        self.assertEqual(YukikoEngine._extract_choice_index("\u7b2c1\u4e2a"), 1)
        self.assertEqual(YukikoEngine._extract_choice_index("\u7b2c\u4e00\u4e2a"), 1)
        self.assertIsNone(YukikoEngine._extract_choice_index("\u90091"))
        self.assertIsNone(
            YukikoEngine._extract_choice_index("\u53d1\u7ed9\u6211\u7b2c\u4e00\u4e2a")
        )

    def test_engine_defaults_to_agent_for_directed_plain_text_chat(self) -> None:
        """原本三个用例围绕 `_should_prefer_router_for_plain_text` 断言「什么时候绕开 Agent
        去走旧 Router 闲聊路径」。该函数已删除，因此契约收敛为一条：
        **指向性纯文本闲聊一律走 Agent + Prompt Navigator，不存在绕过通道。**

        删除理由有两条，第二条是删除过程中实测发现的真缺陷：
        1. 它是 Navigator 之外的第二条旁路，靠一张结构否决清单替模型决定走哪条管线；
        2. 函数体里调 `self._extract_first_url`，而该方法只定义在 `core/agent.py` 的
           AgentLoop 上，`YukikoEngine` 根本没有 —— 一旦运维把文档化的配置键
           `agent.prefer_router_for_directed_plain_text` 打开，主流程直接抛 AttributeError。
           原来的三个用例之所以是绿的，是因为它们逐个 monkeypatch 了
           `engine._extract_first_url = lambda text: ""`，把真实缺陷盖住了。
        """

        engine = YukikoEngine.__new__(YukikoEngine)

        self.assertFalse(hasattr(engine, "_should_prefer_router_for_plain_text"))
        # engine 上确实没有这个方法 —— 这正是旧实现打开配置就崩的原因。
        self.assertFalse(hasattr(engine, "_extract_first_url"))

    def test_engine_detects_structural_echo_of_recent_bot_reply(self) -> None:
        reply = (
            "This appears to be a QQ bot event log showing a recursive or self-referential loop. "
            "The user is sending messages that describe the bot's previous responses, creating a pattern."
        )
        incoming = f"武庸，{reply}"
        self.assertTrue(
            YukikoEngine._looks_like_recent_bot_reply_echo(incoming, [reply])
        )

    def test_engine_does_not_flag_fresh_chat_as_recent_bot_reply_echo(self) -> None:
        incoming = "你刚才那句话什么意思，直接讲白一点。"
        recent_bot_replies = [
            "这是上一轮回复，主要是在解释一个群聊里的消息循环问题。",
        ]
        self.assertFalse(
            YukikoEngine._looks_like_recent_bot_reply_echo(incoming, recent_bot_replies)
        )

    def test_tools_require_explicit_avatar_and_download_controls(self) -> None:
        executor = _DummyExecutor()

        self.assertEqual(
            executor._extract_avatar_name_candidates("/avatar alice"), ["alice"]
        )
        self.assertEqual(executor._extract_avatar_name_candidates("alice avatar"), [])
        self.assertFalse(executor._looks_like_github_request("github foo"))
        self.assertTrue(
            executor._looks_like_github_request("https://github.com/foo/bar")
        )
        self.assertFalse(executor._looks_like_repo_readme_request("docs please"))
        self.assertTrue(executor._looks_like_repo_readme_request("/readme foo/bar"))
        self.assertFalse(executor._looks_like_download_request_text("download demo"))
        self.assertTrue(executor._looks_like_download_request_text("/download demo"))

    def test_data_uri_media_value_never_treated_as_local_path(self) -> None:
        executor = _DummyExecutor()
        huge_data_uri = "data:image/png;base64," + ("a" * 12000)
        normalized = executor._normalize_message_media_value(huge_data_uri)
        self.assertTrue(normalized.startswith("data:image/png;base64,"))

    def test_built_in_defaults_disable_short_ping_heuristics_and_enable_risk_confirm(
        self,
    ) -> None:
        defaults = _built_in_config_defaults()
        bot_cfg = defaults.get("bot", {})
        agent_cfg = defaults.get("agent", {})
        hr_cfg = agent_cfg.get("high_risk_control", {})

        self.assertEqual(bot_cfg.get("short_ping_phrases"), [])
        self.assertTrue(bot_cfg.get("short_ping_require_directed", False))
        self.assertFalse(agent_cfg.get("prefer_router_for_directed_plain_text", True))
        self.assertTrue(hr_cfg.get("default_require_confirmation", False))
        self.assertTrue(bool(hr_cfg.get("tool_name_patterns")))
        self.assertTrue(bool(hr_cfg.get("description_patterns")))


if __name__ == "__main__":
    unittest.main()
