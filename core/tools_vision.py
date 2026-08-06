"""ToolExecutor 视觉/图片分析 mixin — 图片识别、OCR、GIF 处理等。

从 core/tools.py 拆分。"""
from __future__ import annotations

import base64
import io
import mimetypes
import re

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from utils.text import clip_text, normalize_text
from core.tools_types import ToolResult
from core.tools_types import _unwrap_redirect_url, _normalize_multimodal_query, _is_known_image_signature
import logging as _logging
from core.tools_types import _tool_trace_tag
from core.system_prompts import SystemPromptRelay
from core import prompt_loader as _pl

try:  # pragma: no cover
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    Image = None
    ImageDraw = None

_tool_log = _logging.getLogger("yukiko.tools")

# OneBot image 段 data.summary 里客户端自己填的动图标记（例如 "[动画表情]"）。
# 这是段元数据，不是用户输入的自由文本 —— 用户意图走 analyze_image 的 is_animated 参数。
_ANIMATED_SEGMENT_MARKERS = (
    "动画表情",
    "动图",
    "动态图",
    "动态贴纸",
    "gif",
)

# 下载失败原因中「把 URL 直接甩给外部 vision API 也一定失败」的那几个。
# 依据：QQ CDN（gchat.qpic.cn / multimedia.nt.qq.com.cn）实测有**两种**失效形态，
# 都是 HTTP 400，换任何客户端、任何 IP、任何 header 都是同一个响应：
#   {"retcode":-5503007,"retmsg":"download url has expired"}   —— rkey 过期
#   {"retcode":-5503022,"retmsg":"appid is not supported"}     —— 2026-08-05 curl 实测
# 两者都归进 http_400（前者另有更精确的 url_expired），所以回退直传纯属把失败往后挪。
# 403 / 429 / 5xx / 超时不在此列：那些可能只是本机被挡，外部 API 仍有机会。
#
# 顺带记一条实测事实，避免下一个窗口重复排查：这条死路并不是「图片识别失败」的主因。
# 61 次 vision_analyze_start 里图片解析成功 59 次 —— 直连 CDN 失败后会回退
# NapCat 的 get_image（日志 source=onebot_get_image，45 次全部成功）。
# analyze_image 真正的 20 次失败里 14 次是 tool_timeout_seconds_media（45s）超时，
# 只有 4 次是真的拿不到图。要提识别成功率，先看那个超时，不是看这里。
_FATAL_DOWNLOAD_REASONS = frozenset(
    {
        "ssrf_blocked",
        "http_400",
        "http_404",
        "http_410",
        "url_expired",
    }
)

# 非 200 响应体里截取多少字符进日志。QQ CDN 的错误 JSON 只有 70 字节，够用；
# 上限存在是因为响应体可能是整页 HTML。
_DOWNLOAD_ERROR_BODY_CLIP = 200

# ---------------------------------------------------------------------------
# 「模型原生看图」用的 image_url 块 —— 代码侧兜底默认值。
#
# 这三个值同名键要落到 config.search.vision 下（见 build_native_vision_blocks
# 的 docstring）；这里的常量只是配置缺失时的兜底，两边必须同值。
#
# 张数上限 4：一次对话里同时要看的图很少超过 4 张，且直接决定请求体大小。
# 单图上限 512 KiB：不是拍的，是从 storage/logs/yukiko.log 里 92 条
# `vision_image_ref | bytes=N` 实测分布算出来的 ——
#   min 9174 / median 42543 / mean 123925 / p90 396875 / max 986302 字节
#   256 KiB 覆盖 76/92 (83%)、512 KiB 覆盖 90/92 (98%)、1 MiB 覆盖 92/92
# 512 KiB 是「几乎不漏图」和「请求体不爆」的折中：4 张 × 512 KiB 的 base64
# 约 2.8 MB 请求体，还在多数中转站的 body 上限内；1 MiB 会到 5.6 MB。
#
# token 量级（算法：base64 长度 = ceil(bytes/3)*4，BPE 对 base64 这种
# 无空格随机串大致 4 字符 1 token，所以 token ≈ base64_len/4 ≈ bytes/3）：
#   median 42 KB 图 → b64 56724 字符 → ~14181 token
#   p90   387 KB 图 → b64 529168 字符 → ~132292 token
#   512 KiB 封顶图  → b64 682 KB     → ~174763 token
# 这是**上界**，只在中转站把 data URI 当纯文本 token 化时才成立。正常 vision
# API（OpenAI/Anthropic）会解码图片按图块计费，单图约 1.1k~1.6k token。
# 差三个数量级，所以这个上界必须当成「中转站行为未知时的风险敞口」来看，
# 而不是账单预测 —— 这也正是张数和单图字节都要有硬上限的原因。
_NATIVE_VISION_BLOCKS_ENABLE_DEFAULT = True
_NATIVE_VISION_MAX_IMAGES_DEFAULT = 4
_NATIVE_VISION_MAX_IMAGE_BYTES_DEFAULT = 512 * 1024

# 张数与单图字节的夹取区间，防止配置里写出 0 张或 200 MB 这种值。
_NATIVE_VISION_MAX_IMAGES_CEILING = 8
_NATIVE_VISION_MIN_IMAGE_BYTES_FLOOR = 64 * 1024
_NATIVE_VISION_MAX_IMAGE_BYTES_CEILING = 4 * 1024 * 1024


class ToolVisionMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    async def _method_media_analyze_image(
        self,
        method_name: str,
        method_args: dict[str, Any],
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]],
        conversation_id: str = "",
        api_call: Callable[..., Awaitable[Any]] | None = None,
    ) -> ToolResult:
        if not self._vision_enable:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={"text": "当前没开识图能力"},
                error="vision_disabled",
            )
        if (
            self._vision_require_independent_config
            and not self._has_independent_vision_config()
        ):
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "识图模型配置不完整（需要单独的 provider/base_url/model/api_key）"
                },
                error="vision_config_incomplete",
            )

        def _to_flag(value: Any, default: bool = False) -> bool:
            if isinstance(value, bool):
                return value
            text = normalize_text(str(value)).lower()
            if not text:
                return default
            if text in {"1", "true", "yes", "on", "y"}:
                return True
            if text in {"0", "false", "no", "off", "n"}:
                return False
            return default

        explicit_url = normalize_text(str(method_args.get("url", "")))
        allow_recent_fallback = _to_flag(
            method_args.get("allow_recent_fallback", False), False
        )
        recent_only_when_unique = _to_flag(
            method_args.get("recent_only_when_unique", False), False
        )
        analyze_all = _to_flag(method_args.get("analyze_all", False), False)
        # 动图与联网补查都由模型显式给参数（见 Prompt Navigator multimodal_media 分区说明），
        # 本地不再从用户原话里猜。
        explicit_animated = _to_flag(method_args.get("is_animated", False), False)
        web_lookup_on_uncertain = _to_flag(
            method_args.get("web_lookup_on_uncertain", False), False
        )
        max_images_raw = normalize_text(str(method_args.get("max_images", "")))
        try:
            max_images = int(max_images_raw) if max_images_raw else 0
        except ValueError:
            max_images = 0
        if max_images <= 0:
            max_images = 8 if analyze_all else 6
        max_images = max(1, min(24, max_images))
        target_source = (
            normalize_text(str(method_args.get("target_source", ""))) or "unspecified"
        )
        target_message_id = normalize_text(
            str(method_args.get("target_message_id", ""))
        )

        def _is_likely_incomplete_media_url(value: str) -> bool:
            candidate = normalize_text(value)
            if not candidate or not re.match(r"^https?://", candidate, flags=re.IGNORECASE):
                return False
            try:
                parsed = urlparse(candidate)
            except Exception:
                return False
            host = normalize_text(parsed.netloc).lower()
            if "multimedia.nt.qq.com.cn" in host:
                lower_candidate = candidate.lower()
                # QQ CDN 图片直链常需要 rkey；缺失时通常是被截断后的无效链接。
                if "fileid=" in lower_candidate and "rkey=" not in lower_candidate:
                    return True
            return False

        candidates: list[str] = []
        candidate_meta: list[dict[str, str]] = []
        if explicit_url:
            resolved_explicit = _unwrap_redirect_url(explicit_url)
            if _is_likely_incomplete_media_url(resolved_explicit):
                _tool_log.info(
                    "vision_explicit_url_incomplete%s | source=%s",
                    _tool_trace_tag(),
                    clip_text(resolved_explicit, 160),
                )
            else:
                candidates.append(resolved_explicit)
                candidate_meta.append(
                    {
                        "source": "explicit_url",
                        "message_id": target_message_id or "-",
                        "url": resolved_explicit,
                    }
                )
        if explicit_url and conversation_id and allow_recent_fallback:
            recent = self._get_recent_media(
                conversation_id=conversation_id, media_type="image"
            )
            if recent and recent_only_when_unique and len(recent) > 1:
                _tool_log.info(
                    "vision_recent_fallback_skip%s | reason=ambiguous_recent_cache | count=%d",
                    _tool_trace_tag(),
                    len(recent),
                )
            elif recent:
                recent_candidates = recent[:1] if recent_only_when_unique else recent
                for item in recent_candidates:
                    normalized_recent = normalize_text(item)
                    if not normalized_recent:
                        continue
                    candidates.append(normalized_recent)
                    candidate_meta.append(
                        {
                            "source": "recent_cache_fallback",
                            "message_id": "-",
                            "url": normalized_recent,
                        }
                    )
        message_candidates = self._extract_message_media_urls(
            raw_segments, media_type="image"
        )
        if message_candidates:
            candidates.extend(message_candidates)
            for item in message_candidates:
                candidate_meta.append(
                    {
                        "source": target_source or "current_or_reply",
                        "message_id": target_message_id or "-",
                        "url": item,
                    }
                )
        if not candidates:
            merged_text = normalize_text(f"{query}\n{message_text}")
            text_candidates = self._extract_urls(merged_text)
            candidates.extend(text_candidates)
            for item in text_candidates:
                candidate_meta.append(
                    {
                        "source": "message_text_url",
                        "message_id": "-",
                        "url": item,
                    }
                )
        if not candidates and conversation_id and allow_recent_fallback:
            recent = self._get_recent_media(
                conversation_id=conversation_id, media_type="image"
            )
            if recent and recent_only_when_unique and len(recent) > 1:
                return ToolResult(
                    ok=False,
                    tool_name=method_name,
                    payload={
                        "text": "我这边最近有不止一张图片，请直接回复你要分析的那张图片再问我。"
                    },
                    error="image_recent_ambiguous",
                )
            if recent:
                candidates.extend(recent[:1] if recent_only_when_unique else recent)
                for item in recent[:1] if recent_only_when_unique else recent:
                    candidate_meta.append(
                        {
                            "source": "recent_cache",
                            "message_id": "-",
                            "url": item,
                        }
                    )

        uniq: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            value = normalize_text(raw)
            if not value or value in seen:
                continue
            seen.add(value)
            uniq.append(value)
        if len(uniq) > max_images:
            uniq = uniq[:max_images]
        if not uniq:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": '没拿到可识别图片 你可以直接发图或给我图片 URL 也可以先发图再说"分析这张图"'
                },
                error="image_not_found",
            )
        url_file_map = self._extract_image_url_file_map(raw_segments)
        if conversation_id:
            recent_url_file_map = self._get_recent_image_file_map(conversation_id)
            if recent_url_file_map:
                for key, token in recent_url_file_map.items():
                    url_file_map.setdefault(key, token)

        if (
            self._vision_route_text_model_to_local
            and not self._can_use_remote_vision_model()
        ):
            _tool_log.info(
                "vision_route_local%s | method=%s | reason=model_text_only_or_unsupported",
                _tool_trace_tag(),
                method_name,
            )
            local_result = await self._analyze_image_local_fallback(
                method_name=method_name,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                api_call=api_call,
            )
            if local_result is not None:
                return local_result
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "当前模型不支持图片理解，已尝试本地识别但没拿到稳定结果。你可以切换支持图片的模型，或让我只做 OCR 文字提取。"
                },
                error="vision_local_unavailable",
            )

        animated_hint = explicit_animated or self._has_animated_image_hint(
            raw_segments=raw_segments
        )
        prompt = self._build_vision_prompt(
            query=query,
            message_text=message_text,
            animated_hint=animated_hint,
        )
        _tool_log.info(
            "vision_analyze_start%s | method=%s | candidates=%d | explicit=%s | target_source=%s | target_mid=%s | analyze_all=%s | max_images=%d",
            _tool_trace_tag(),
            method_name,
            len(uniq),
            bool(explicit_url),
            target_source,
            target_message_id or "-",
            analyze_all,
            max_images,
        )
        if candidate_meta:
            preview = " | ".join(
                f"{idx + 1}:{item.get('source','-')}:{item.get('message_id','-')}:{clip_text(item.get('url',''), 90)}"
                for idx, item in enumerate(candidate_meta[:6])
            )
            _tool_log.info(
                "vision_analyze_candidates%s | method=%s | %s",
                _tool_trace_tag(),
                method_name,
                preview,
            )
        low_confidence_seen = False
        successful_items: list[dict[str, Any]] = []
        collected_evidence: list[dict[str, str]] = []
        for url in uniq:
            if self._is_blocked_image_url(url):
                continue
            if re.match(
                r"^https?://", url, flags=re.IGNORECASE
            ) and not self._is_safe_public_http_url(url):
                continue
            file_token = url_file_map.get(normalize_text(url))
            image_ref = await self._prepare_vision_image_ref(url)
            if not image_ref and file_token and api_call is not None:
                onebot_data_uri = await self._data_uri_from_onebot_image_file(
                    image_file=file_token,
                    api_call=api_call,
                )
                if onebot_data_uri:
                    image_ref = onebot_data_uri
                    _tool_log.info(
                        "vision_image_ref%s | source=onebot_get_image | converted=data_uri",
                        _tool_trace_tag(),
                    )
            if (
                image_ref
                and re.match(r"^https?://", image_ref, flags=re.IGNORECASE)
                and api_call is not None
            ):
                remote_file_token = file_token or url_file_map.get(
                    normalize_text(image_ref)
                )
                if remote_file_token:
                    onebot_data_uri = await self._data_uri_from_onebot_image_file(
                        image_file=remote_file_token,
                        api_call=api_call,
                    )
                    if onebot_data_uri:
                        image_ref = onebot_data_uri
                        _tool_log.info(
                            "vision_image_ref%s | source=onebot_get_image | converted=data_uri",
                            _tool_trace_tag(),
                        )
            if not image_ref:
                _tool_log.warning(
                    "vision_image_ref_empty%s | url=%s",
                    _tool_trace_tag(),
                    clip_text(url, 120),
                )
                continue
            _tool_log.info(
                "vision_describe_start%s | image_ref=%s",
                _tool_trace_tag(),
                clip_text(image_ref, 120),
            )
            raw_answer = await self._vision_describe(image_ref=image_ref, prompt=prompt)
            _tool_log.info(
                "vision_describe_done%s | raw_answer=%s",
                _tool_trace_tag(),
                clip_text(raw_answer or "-", 200),
            )
            answer = await self._normalize_vision_answer_with_retry(
                image_ref=image_ref,
                answer=raw_answer,
                prompt=prompt,
                query=query,
                message_text=message_text,
                animated_hint=animated_hint,
            )
            if not answer:
                raw_fallback = normalize_text(str(raw_answer or ""))
                if raw_fallback:
                    answer = clip_text(raw_fallback, 1200)
                    _tool_log.info("vision_answer_fallback_to_raw%s", _tool_trace_tag())
            if not answer:
                _tool_log.warning(
                    "vision_answer_empty_after_normalize%s", _tool_trace_tag()
                )
                continue
            if self._looks_like_weak_vision_answer(answer):
                _tool_log.warning(
                    "vision_answer_weak%s | answer=%s",
                    _tool_trace_tag(),
                    clip_text(answer, 100),
                )
                low_confidence_seen = True
                continue
            source = _unwrap_redirect_url(url)
            evidence = [
                {
                    "title": "图像识别",
                    "point": clip_text(answer, 180),
                    "source": source,
                }
            ]
            _tool_log.info(
                "vision_analyze_ok%s | method=%s | source=%s",
                _tool_trace_tag(),
                method_name,
                clip_text(source, 120),
            )
            if not analyze_all:
                return ToolResult(
                    ok=True,
                    tool_name=method_name,
                    payload={
                        "text": answer,
                        "analysis": answer,
                        "source": source,
                        "evidence": evidence,
                    },
                    evidence=evidence,
                )
            successful_items.append(
                {
                    "index": len(successful_items) + 1,
                    "analysis": answer,
                    "source": source,
                }
            )
            collected_evidence.extend(evidence)

        if analyze_all and successful_items:
            lines = [f"已识别 {len(successful_items)} 张图（候选 {len(uniq)} 张）："]
            for idx, item in enumerate(successful_items, start=1):
                one_line = clip_text(item.get("analysis", ""), 220)
                one_source = clip_text(item.get("source", ""), 80)
                if one_source:
                    lines.append(f"{idx}. {one_line}（来源: {one_source}）")
                else:
                    lines.append(f"{idx}. {one_line}")
            merged = "\n".join(lines)
            sources = [
                normalize_text(item.get("source", ""))
                for item in successful_items
                if normalize_text(item.get("source", ""))
            ]
            first_source = sources[0] if sources else ""
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "text": merged,
                    "analysis": merged,
                    "source": first_source,
                    "sources": sources,
                    "analyses": successful_items,
                    "count": len(successful_items),
                    "requested": len(uniq),
                    "evidence": collected_evidence,
                },
                evidence=collected_evidence,
            )

        if low_confidence_seen:
            web_fallback = await self._vision_uncertain_web_fallback(
                query=query,
                message_text=message_text,
                web_lookup_requested=web_lookup_on_uncertain,
            )
            if web_fallback is not None:
                return web_fallback
            single_low_confidence_text = (
                "这张动画表情/动图我已经按多帧尝试识别了，但结果还不够稳定。你可以发更清晰的静态截图，"
                "或者直接问我它大概想表达什么。"
                if animated_hint
                else "这张图识别没成功，能再发一次吗？或者告诉我这张图里你主要想看哪一块，我重点看。"
            )
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": (
                        "这些图我已经尝试识别了，但内容太模糊或信息不足，结果不稳定。你可以发更清晰截图，"
                        "或告诉我要重点看哪一块。"
                        if analyze_all
                        else single_low_confidence_text
                    )
                },
                error="vision_low_confidence",
            )

        web_fallback = await self._vision_uncertain_web_fallback(
            query=query,
            message_text=message_text,
            web_lookup_requested=web_lookup_on_uncertain,
        )
        if web_fallback is not None:
            return web_fallback
        if api_call is not None:
            local_fallback = await self._analyze_image_local_fallback(
                method_name=method_name,
                query=query,
                message_text=message_text,
                raw_segments=raw_segments,
                api_call=api_call,
            )
            if local_fallback.ok:
                return local_fallback
        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={
                    "text": (
                        "这些图这次没识别出来，请发更清晰的图片，或告诉我要重点看哪一块。"
                        if analyze_all
                        else (
                            "这张动画表情/动图这次还是没稳定识别出来。你可以发一张关键帧截图，"
                            "或者直接问我它像是在表达什么情绪/态度。"
                            if animated_hint
                            else "这张图识别没成功，能再发一次吗？或者告诉我这张图里你主要想看哪一块，我重点看。"
                        )
                    )
                },
                error="vision_analyze_failed",
            )

    async def _vision_uncertain_web_fallback(
        self, query: str, message_text: str, *, web_lookup_requested: bool
    ) -> ToolResult | None:
        # 是否联网补查由模型在 analyze_image 里显式传 web_lookup_on_uncertain 决定，
        # 不再由本地词表从用户原话猜「出处/来源/是什么梗」这类意图。
        if not web_lookup_requested:
            _tool_log.info(
                "vision_web_fallback_skip%s | reason=web_lookup_not_requested",
                _tool_trace_tag(),
            )
            return None
        merged = _normalize_multimodal_query(f"{query}\n{message_text}")
        if not merged:
            return None

        refined = re.sub(
            r"(image|picture|screenshot|analyze|analysis|identify|ocr)",
            " ",
            merged,
            flags=re.IGNORECASE,
        )
        refined = normalize_text(re.sub(r"\s+", " ", refined))
        search_query = refined if len(refined) >= 2 else merged
        if len(search_query) < 2:
            return None

        query_type = "text"
        try:
            results = await self._search_text_with_variants(
                query=search_query, query_type=query_type
            )
        except Exception:
            return None
        results = self._filter_and_rank_results(
            search_query, results, query_type=query_type
        )
        if not results:
            return None

        evidence = self._build_evidence_from_results(results)
        summary = self._format_search_text(
            search_query, results, evidence=evidence, query_type=query_type
        )
        text_out = f"图像识别不确定，已联网补查：\n{summary}"
        payload = {
            "query": search_query,
            "query_type": query_type,
            "text": text_out,
            "results": [
                {"title": item.title, "snippet": item.snippet, "url": item.url}
                for item in results
            ],
            "evidence": evidence,
            "vision_uncertain_fallback": True,
        }
        return ToolResult(
            ok=True, tool_name="vision_web_fallback", payload=payload, evidence=evidence
        )

    def _can_use_remote_vision_model(self) -> bool:
        mode = normalize_text(self._vision_model_supports_image).lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        if mode in {"0", "false", "no", "off"}:
            return False

        model_client = getattr(self.image_engine, "model_client", None)
        if model_client is None:
            return False
        client = getattr(model_client, "client", None)
        model_name = self._vision_model or normalize_text(
            str(getattr(client, "model", "") or getattr(model_client, "model", ""))
        )
        checker = getattr(model_client, "supports_vision_input", None)
        if callable(checker):
            try:
                return bool(checker(model=model_name))
            except Exception:
                return False
        return False

    async def _analyze_image_local_fallback(
        self,
        method_name: str,
        query: str,
        message_text: str,
        raw_segments: list[dict[str, Any]],
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> ToolResult:
        if api_call is None:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "当前模型不支持图片理解，且本地 OCR 通道不可用（缺少 OneBot API 上下文）。"
                },
                error="local_ocr_api_unavailable",
            )

        file_tokens = self._extract_image_file_tokens(raw_segments)
        if not file_tokens:
            return ToolResult(
                ok=False,
                tool_name=method_name,
                payload={
                    "text": "当前模型不支持图片理解，本地 OCR 需要直接发送图片（含 file 标识）后再试。"
                },
                error="local_ocr_image_token_missing",
            )

        for token in file_tokens[:3]:
            try:
                result = await api_call("ocr_image", image=token)
            except Exception:
                continue
            text = self._extract_ocr_text(result)
            if not text:
                continue
            source = token
            evidence = [
                {
                    "title": "本地 OCR",
                    "point": clip_text(text, 180),
                    "source": source,
                }
            ]
            # 走没走本地 OCR 已经由 payload 的 analysis_route 结构化传出，
            # 措辞不再按用户原话里的「总结/概括/要点/分析」切换。
            out = (
                "我先走了本地 OCR（当前模型不支持图片理解）。识别到的文字如下：\n"
                f"{text}"
            )
            return ToolResult(
                ok=True,
                tool_name=method_name,
                payload={
                    "text": clip_text(out, 900),
                    "analysis": text,
                    "source": source,
                    "analysis_route": "local_ocr",
                    "evidence": evidence,
                },
                evidence=evidence,
            )

        return ToolResult(
            ok=False,
            tool_name=method_name,
            payload={
                "text": "当前模型不支持图片理解，已尝试本地 OCR，但这张图没有提取到可用文字。"
            },
            error="local_ocr_empty",
        )

    @staticmethod
    def _extract_image_file_tokens(raw_segments: list[dict[str, Any]]) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for seg in raw_segments or []:
            if not isinstance(seg, dict):
                continue
            if normalize_text(str(seg.get("type", ""))).lower() != "image":
                continue
            data = seg.get("data", {}) or {}
            if not isinstance(data, dict):
                continue
            for key in ("file", "id", "file_id"):
                value = normalize_text(str(data.get(key, "")))
                if not value:
                    continue
                if value in seen:
                    continue
                seen.add(value)
                tokens.append(value)
        return tokens

    @staticmethod
    def _extract_ocr_text(result: Any) -> str:
        payload = result
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            payload = result.get("data")
        if not isinstance(payload, dict):
            return ""
        texts = payload.get("texts", [])
        if not isinstance(texts, list):
            return ""
        rows: list[str] = []
        for item in texts:
            if isinstance(item, dict):
                txt = normalize_text(str(item.get("text", "")))
            else:
                txt = normalize_text(str(item))
            if txt:
                rows.append(txt)
        return normalize_text("\n".join(rows))

    @staticmethod
    def _has_animated_image_hint(
        raw_segments: list[dict[str, Any]] | None = None,
    ) -> bool:
        """只从 OneBot image 段的结构元数据判断是否动图。

        用户原话里的「动图 / 表情包 / gif」不再参与判断 —— 那要模型显式传
        analyze_image 的 is_animated 参数。这里认的是段自带的 sub_type、
        客户端填的 summary 标记、以及文件名/URL 扩展名，属结构事实。
        """
        for seg in raw_segments or []:
            if not isinstance(seg, dict):
                continue
            if normalize_text(str(seg.get("type", ""))).lower() != "image":
                continue
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            summary = normalize_text(str(data.get("summary", ""))).lower()
            file_name = normalize_text(str(data.get("file", ""))).lower()
            url = normalize_text(str(data.get("url", ""))).lower()
            sub_type = normalize_text(str(data.get("sub_type", ""))).lower()
            if sub_type == "1":
                return True
            if any(marker in summary for marker in _ANIMATED_SEGMENT_MARKERS):
                return True
            if file_name.endswith(".gif"):
                return True
            if ".gif" in url:
                return True
        return False

    def _build_vision_prompt(
        self, query: str, message_text: str, *, animated_hint: bool = False
    ) -> str:
        merged = _normalize_multimodal_query(f"{query}\n{message_text}")
        if not merged:
            merged = "请描述这张图的主要内容，并提取可见文字。"
        # 原来这里有一张「软件/任务栏/图标/窗口」词表，命中就追加桌面截图专用指令。
        # 已删：merged 本身（用户问题原话）逐字进了 vision_main_prompt 的 user_query，
        # 视觉模型看得到「开着哪些软件」，不需要本地再按关键词替它加戏。
        extra_parts: list[str] = []
        if animated_hint:
            extra_parts.append(
                "\n如果这是动画表情、GIF 或多帧拼图："
                "请综合所有帧，先判断主体是谁、在做什么、情绪/语气是什么、可能想表达什么梗或态度；"
                "即使不能百分百确定，也要给出最可能的解释，不要只说“看不清”或“可能是动图”。"
            )
        extra = "".join(extra_parts)
        base = SystemPromptRelay.vision_main_prompt(user_query=merged, extra=extra)
        return self._prompt_policy.compose_prompt(
            channel="vision",
            base_prompt=base,
            tool_name="media.analyze_image",
        )

    def _build_vision_retry_prompt(
        self, query: str, message_text: str, *, animated_hint: bool = False
    ) -> str:
        merged = _normalize_multimodal_query(f"{query}\n{message_text}")
        if not merged:
            merged = "请识别这张图。"
        if animated_hint:
            merged = (
                f"{merged}\n补充要求：如果这是动画表情/GIF/多帧图，请综合各帧动作与情绪，"
                "优先回答“这张图想表达什么”。"
            )
        base = SystemPromptRelay.vision_retry_prompt(user_query=merged)
        return self._prompt_policy.compose_prompt(
            channel="vision",
            base_prompt=base,
            tool_name="media.analyze_image",
        )

    @staticmethod
    def _extract_image_url_file_map(
        raw_segments: list[dict[str, Any]]
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            if seg_type != "image":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            url = normalize_text(str(data.get("url", "")))
            file_token = normalize_text(str(data.get("file", "")))
            if url and file_token:
                mapping[url] = file_token
                resolved = _unwrap_redirect_url(url)
                if resolved:
                    mapping[resolved] = file_token
            if file_token:
                mapping[file_token] = file_token
        return mapping

    @staticmethod
    def _extract_api_data(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                return data
            return payload
        data_obj = getattr(payload, "data", None)
        if isinstance(data_obj, dict):
            return data_obj
        return {}

    def _to_data_uri_from_image_bytes(
        self,
        data: bytes,
        mime: str = "image/png",
        *,
        source: str = "unknown",
        allow_gif_keyframes: bool = True,
    ) -> str:
        if not data:
            _tool_log.warning(
                "vision_image_encode_reject%s | source=%s | reason=empty_bytes",
                _tool_trace_tag(),
                source,
            )
            return ""
        if (
            len(data) < self._vision_min_image_bytes
            or len(data) > self._vision_max_image_bytes
        ):
            # 注意：调用方的 small_image_warning 写着「继续处理」，但这里仍会拒 ——
            # 该矛盾此前完全静默，只有把它记下来才看得见。
            _tool_log.warning(
                "vision_image_encode_reject%s | source=%s | reason=size_out_of_range | bytes=%d | min=%d | max=%d",
                _tool_trace_tag(),
                source,
                len(data),
                self._vision_min_image_bytes,
                self._vision_max_image_bytes,
            )
            return ""
        head = data[:16]
        if not _is_known_image_signature(head):
            # CDN 用 HTTP 200 返回 JSON/HTML 错误页时就落在这里。
            _tool_log.warning(
                "vision_image_encode_reject%s | source=%s | reason=not_image_signature | bytes=%d | mime=%s | head=%s",
                _tool_trace_tag(),
                source,
                len(data),
                clip_text(mime, 40) or "-",
                head.hex(),
            )
            return ""
        mime_norm = normalize_text(mime).lower()
        if not mime_norm.startswith("image/"):
            mime_norm = "image/png"
        if allow_gif_keyframes and self._is_gif_payload(data=data, mime=mime_norm):
            keyframe_data_uri = self._gif_keyframes_to_data_uri(data)
            if keyframe_data_uri:
                _tool_log.info(
                    "vision_image_ref%s | source=%s | gif=keyframes_collage",
                    _tool_trace_tag(),
                    source,
                )
                return keyframe_data_uri
        b64 = base64.b64encode(data).decode("ascii")
        data_uri = f"data:{mime_norm};base64,{b64}"
        _tool_log.info(
            "vision_image_ref%s | source=%s | mime=%s | bytes=%d",
            _tool_trace_tag(),
            source,
            mime_norm,
            len(data),
        )
        return data_uri

    @staticmethod
    def _is_gif_payload(data: bytes, mime: str) -> bool:
        mime_l = normalize_text(mime).lower()
        if "gif" in mime_l:
            return True
        return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")

    @staticmethod
    def _pick_gif_keyframe_indexes(frame_count: int) -> list[int]:
        if frame_count <= 1:
            return [0]
        target_frames = min(6, frame_count)
        last = frame_count - 1
        picks = [
            int(round(last * idx / max(1, target_frames - 1)))
            for idx in range(target_frames)
        ]
        out: list[int] = []
        seen: set[int] = set()
        for idx in picks:
            if idx < 0 or idx >= frame_count or idx in seen:
                continue
            seen.add(idx)
            out.append(idx)
        return out or [0]

    def _gif_keyframes_to_data_uri(self, gif_bytes: bytes) -> str:
        if Image is None:
            _tool_log.info(
                "vision_gif_keyframes_skip%s | reason=pillow_unavailable",
                _tool_trace_tag(),
            )
            return ""
        try:
            with Image.open(io.BytesIO(gif_bytes)) as gif:
                if not bool(getattr(gif, "is_animated", False)):
                    return ""
                frame_count = int(getattr(gif, "n_frames", 1) or 1)
                frame_indexes = self._pick_gif_keyframe_indexes(frame_count)
                frames = []
                for idx in frame_indexes:
                    gif.seek(idx)
                    frames.append(gif.convert("RGB").copy())
        except Exception as exc:
            _tool_log.warning(
                "vision_gif_keyframes_fail%s | err=%s",
                _tool_trace_tag(),
                clip_text(str(exc), 160),
            )
            return ""

        if not frames:
            return ""

        target_h = max(180, min(640, max(frame.height for frame in frames)))
        resized = []
        for frame in frames:
            src_h = max(1, int(frame.height))
            width = max(
                1, int(round(float(frame.width) * float(target_h) / float(src_h)))
            )
            resized.append(frame.resize((width, target_h)))

        gap = 8
        cols = min(3, len(resized)) if len(resized) > 2 else (2 if len(resized) > 1 else 1)
        rows = max(1, (len(resized) + cols - 1) // cols)
        cell_w = max(frame.width for frame in resized)
        cell_h = max(frame.height for frame in resized)
        canvas = Image.new(
            "RGB",
            (
                cell_w * cols + gap * max(0, cols - 1),
                cell_h * rows + gap * max(0, rows - 1),
            ),
            (12, 12, 12),
        )
        draw = ImageDraw.Draw(canvas) if ImageDraw is not None else None
        for idx, frame in enumerate(resized):
            row = idx // cols
            col = idx % cols
            x = col * (cell_w + gap)
            y = row * (cell_h + gap)
            paste_x = x + max(0, (cell_w - frame.width) // 2)
            paste_y = y + max(0, (cell_h - frame.height) // 2)
            canvas.paste(frame, (paste_x, paste_y))
            if draw is not None:
                draw.rectangle((x + 4, y + 4, x + 34, y + 22), fill=(0, 0, 0))
                draw.text((x + 9, y + 7), f"F{idx + 1}", fill=(255, 255, 255))

        buf = io.BytesIO()
        try:
            canvas.save(buf, format="JPEG", quality=86, optimize=True)
        except Exception as exc:
            _tool_log.warning(
                "vision_gif_keyframes_encode_fail%s | err=%s",
                _tool_trace_tag(),
                clip_text(str(exc), 160),
            )
            return ""
        merged = buf.getvalue()
        if not merged:
            return ""
        if len(merged) > self._vision_max_image_bytes:
            return ""
        return self._to_data_uri_from_image_bytes(
            merged,
            mime="image/jpeg",
            source="gif_keyframes",
            allow_gif_keyframes=False,
        )

    async def _data_uri_from_onebot_image_file(
        self,
        image_file: str,
        api_call: Callable[..., Awaitable[Any]] | None,
    ) -> str:
        file_token = normalize_text(image_file)
        if not file_token or api_call is None:
            return ""
        for kwargs in (
            {"file": file_token},
            {"file_id": file_token},
            {"id": file_token},
        ):
            try:
                result = await api_call("get_image", **kwargs)
            except Exception:
                continue

            payload = self._extract_api_data(result)
            for key in ("file", "file_path", "path", "local_path", "filename"):
                raw_path = normalize_text(str(payload.get(key, "")))
                if not raw_path:
                    continue
                if raw_path.startswith("file://"):
                    parsed = urlparse(raw_path)
                    file_part = unquote(parsed.path or "")
                    if re.match(r"^/[A-Za-z]:/", file_part):
                        file_part = file_part[1:]
                    raw_path = file_part
                local_path = Path(raw_path)
                if not local_path.is_absolute():
                    local_path = (self._project_root / local_path).resolve()
                if not local_path.exists() or not local_path.is_file():
                    continue
                try:
                    data = local_path.read_bytes()
                except Exception:
                    continue
                # 同 _prepare_vision_image_ref：这条路径也没有包含性检查，
                # 必须靠内容判定挡住非图片文件。见那里的完整说明。
                if not _is_known_image_signature(data[:16]):
                    _tool_log.warning(
                        "vision_image_ref%s | source=onebot_local_file | "
                        "rejected_not_an_image | path=%s",
                        _tool_trace_tag(),
                        clip_text(str(local_path), 120),
                    )
                    continue
                mime = mimetypes.guess_type(str(local_path))[0] or "image/png"
                data_uri = self._to_data_uri_from_image_bytes(
                    data,
                    mime=mime,
                    source="onebot_local_file",
                )
                if data_uri:
                    return data_uri

            for key in ("url", "download_url", "src"):
                remote_url = normalize_text(str(payload.get(key, "")))
                if not remote_url:
                    continue
                data_uri = await self._download_image_as_data_uri(remote_url)
                if data_uri:
                    return data_uri
        return ""

    async def _prepare_vision_image_ref(self, raw: str) -> str:
        value = normalize_text(raw)
        if not value:
            return ""
        if value.startswith("data:image"):
            mime, b64 = self._decode_data_image_ref(value)
            if mime and b64:
                try:
                    raw_bytes = base64.b64decode(b64, validate=False)
                except Exception:
                    raw_bytes = b""
                if raw_bytes:
                    prepared = self._to_data_uri_from_image_bytes(
                        raw_bytes,
                        mime=mime,
                        source="data_uri",
                    )
                    if prepared:
                        return prepared
            _tool_log.info(
                "vision_image_ref%s | source=data_uri | passthrough=true",
                _tool_trace_tag(),
            )
            return value
        if value.startswith("base64://"):
            b64 = value[len("base64://") :].strip()
            if not b64:
                return ""
            try:
                raw_bytes = base64.b64decode(b64, validate=False)
            except Exception:
                raw_bytes = b""
            if raw_bytes:
                prepared = self._to_data_uri_from_image_bytes(
                    raw_bytes,
                    mime="image/png",
                    source="base64_scheme",
                )
                if prepared:
                    return prepared
            _tool_log.info(
                "vision_image_ref%s | source=base64_scheme | passthrough=true",
                _tool_trace_tag(),
            )
            return f"data:image/png;base64,{b64}"
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            if not self._is_safe_public_http_url(value):
                return ""
            # QQ CDN 等内网图片外部 API 无法访问，统一下载转 base64
            downloaded, fail_reason = await self._download_image_as_data_uri_detailed(
                value
            )
            if downloaded:
                _tool_log.info(
                    "vision_image_ref%s | source=http_url | converted=data_uri",
                    _tool_trace_tag(),
                )
                return downloaded
            provider_hint = self._resolve_vision_provider_hint()
            if provider_hint in {"anthropic", "gemini", "skiapi"}:
                _tool_log.warning(
                    "vision_image_ref_empty%s | source=http_url | reason=download_failed_for_provider_%s | cause=%s",
                    _tool_trace_tag(),
                    provider_hint,
                    fail_reason or "unknown",
                )
                return ""
            if fail_reason in _FATAL_DOWNLOAD_REASONS:
                # 链接本身已经死了（实测 QQ CDN 失效 rkey → 400 url_expired）。
                # 回退直传只是让外部 API 去撞同一面墙，还会把「拿不到图」伪装成
                # 「模型看不懂图」。返回空，让调用方走 OneBot get_image 那条能走通的路。
                _tool_log.warning(
                    "vision_image_ref_empty%s | source=http_url | reason=%s | direct_url_fallback=skipped",
                    _tool_trace_tag(),
                    fail_reason,
                )
                return ""
            # 下载失败则回退直传 URL（公网图片 API 可能能访问）
            _tool_log.info(
                "vision_image_ref%s | source=http_url | converted=direct_url | cause=%s",
                _tool_trace_tag(),
                fail_reason or "unknown",
            )
            return value
        if value.startswith("file://"):
            parsed = urlparse(value)
            file_part = unquote(parsed.path or "")
            if re.match(r"^/[A-Za-z]:/", file_part):
                file_part = file_part[1:]
            value = file_part

        # 防止 data URI 被当作文件路径处理（会导致 "File name too long" 错误）
        if value.startswith("data:"):
            _tool_log.warning(
                "vision_image_ref%s | source=data_uri | unhandled_format | skipping_file_path_check",
                _tool_trace_tag(),
            )
            return ""

        path = Path(value)
        if not path.is_absolute():
            path = (self._project_root / path).resolve()
        if not path.exists() or not path.is_file():
            return ""
        try:
            data = path.read_bytes()
        except Exception:
            return ""
        if not data:
            return ""
        # 必须真的是图片，不能只是「扩展名像图片」。
        #
        # 这里的 path 来自模型给的 `url` 参数（analyze_image 的 schema 里它是
        # 无格式约束的自由字符串），而上面对路径**没有任何包含性检查**：
        # 相对路径 `../../../etc/hosts` 会解析到项目外，绝对路径 `/etc/hosts`
        # 更是原样使用。实测两者都能落到这里，然后被 base64 发给第三方 vision API
        # —— 等于把任意进程可读文件外传。2026-08-06 两个独立 workflow 都报了这里。
        #
        # 不用目录白名单是因为合法路径本来就在项目外：NapCat 的本地文件在
        # QQ 容器里（实测 /Users/<u>/Library/Containers/com.tencent.qq/Data/tmp/...），
        # 按目录拦会打断 analyze_image（线上第二热的工具，288 次调用）。
        # 改判「内容是不是图片」既堵掉文本类机密（passwd / .env / id_rsa / config.yml
        # 都没有图片 magic bytes），又不挑目录。
        if not _is_known_image_signature(data[:16]):
            _tool_log.warning(
                "vision_image_ref%s | source=local_file | rejected_not_an_image | path=%s",
                _tool_trace_tag(),
                clip_text(str(path), 120),
            )
            return ""
        if len(data) < self._vision_min_image_bytes:
            _tool_log.warning(
                "vision_image_ref%s | source=local_file | small_image_warning | bytes=%d | will_try_anyway",
                _tool_trace_tag(),
                len(data),
            )
            # 不要拒绝小图片，继续处理
        if len(data) > self._vision_max_image_bytes:
            return ""
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return self._to_data_uri_from_image_bytes(
            data,
            mime=mime,
            source="local_file",
        )

    @staticmethod
    def _native_vision_segment_candidates(
        seg: dict[str, Any]
    ) -> tuple[str, list[tuple[str, str]]]:
        """把一个 OneBot image 段拆成 `(去重键, [(取法, 值), ...])`。

        取法顺序就是成功率顺序，实测（storage/logs/yukiko.log）：
        `source=onebot_local_file` 61/61 成功、`source=onebot_get_image` 45/45 成功、
        `source=http_url` 直连 QQ CDN 50/50 **全败**。所以 URL 排最后 ——
        它不是主路，只是给「用户贴的外站公网图链」留的口子。
        """

        if not isinstance(seg, dict):
            return "", []
        if normalize_text(str(seg.get("type", ""))).lower() != "image":
            return "", []
        data = seg.get("data")
        if not isinstance(data, dict):
            return "", []

        memory_data_uri = normalize_text(str(data.get("memory_data_uri", "")))
        file_field = normalize_text(
            str(data.get("file", "") or data.get("file_id", ""))
        )
        path_field = normalize_text(str(data.get("path", "")))
        url_field = normalize_text(str(data.get("url", "")))

        def _looks_like_path(value: str) -> bool:
            return bool(
                value.startswith(("file://", "/"))
                or re.match(r"^[A-Za-z]:[\\/]", value)
            )

        candidates: list[tuple[str, str]] = []
        if memory_data_uri.startswith("data:image"):
            candidates.append(("memory_data_uri", memory_data_uri))
        for value in (path_field, file_field):
            if value and _looks_like_path(value):
                candidates.append(("local_file", value))
        if file_field and not _looks_like_path(file_field):
            candidates.append(("onebot_get_image", file_field))
        if url_field:
            candidates.append(("http_url", url_field))

        dedup_key = file_field or url_field or path_field or memory_data_uri[:96]
        return dedup_key, candidates

    async def _native_vision_data_uri_for_segment(
        self,
        seg: dict[str, Any],
        api_call: Any,
        max_bytes: int,
    ) -> tuple[str, str]:
        """把一个 image 段转成 data URI，返回 `(data_uri, reason)`。

        失败时 data_uri 为空、reason 说明卡在哪一步；成功时 reason 是命中的取法
        （`memory_data_uri` / `local_file` / `onebot_get_image` / `http_url`）。
        """

        _, candidates = self._native_vision_segment_candidates(seg)
        if not candidates:
            return "", "no_usable_field"

        attempts: list[str] = []
        for kind, value in candidates:
            if kind == "onebot_get_image":
                if api_call is None:
                    attempts.append(f"{kind}:no_api_call")
                    continue
                data_uri = await self._data_uri_from_onebot_image_file(
                    image_file=value,
                    api_call=api_call,
                )
            else:
                data_uri = await self._prepare_vision_image_ref(value)

            if not data_uri:
                attempts.append(f"{kind}:empty")
                continue
            if not data_uri.startswith("data:image"):
                # `_prepare_vision_image_ref` 在非致命下载失败时会回退直传原 URL。
                # 原生看图这条路**不能**要那个回退：塞进 image_url 块的 QQ CDN 链接
                # 外部 API 一定打不开（实测 HTTP 400 appid is not supported /
                # download url has expired），只会让模型收到死链后瞎猜。
                attempts.append(f"{kind}:not_data_uri")
                _tool_log.warning(
                    "native_vision_block_reject%s | source=%s | reason=direct_url_not_accepted | ref=%s",
                    _tool_trace_tag(),
                    kind,
                    clip_text(data_uri, 100),
                )
                continue
            payload_bytes = self._data_uri_payload_bytes(data_uri)
            if payload_bytes > max_bytes:
                # 超限的图不塞进上下文，但模型仍可以显式调 analyze_image ——
                # 那条路自己有 6 MB 上限，不受这里的 512 KiB 约束。
                attempts.append(f"{kind}:too_large")
                _tool_log.warning(
                    "native_vision_block_reject%s | source=%s | reason=too_large_for_native | bytes=%d | limit=%d",
                    _tool_trace_tag(),
                    kind,
                    payload_bytes,
                    max_bytes,
                )
                continue
            return data_uri, kind

        return "", "|".join(attempts) or "no_usable_field"

    async def build_native_vision_blocks(
        self,
        raw_segments: list[dict[str, Any]] | None = None,
        reply_media_segments: list[dict[str, Any]] | None = None,
        api_call: Any = None,
        max_images: int = 0,
    ) -> tuple[list[dict[str, Any]], str]:
        """把消息里的图片段转成外部 vision API 真能吃的 image_url 块列表。

        调用方（agent 侧）约定：

        ```python
        blocks, reason = await ctx.tool_executor.build_native_vision_blocks(
            raw_segments=ctx.raw_segments,
            reply_media_segments=ctx.reply_media_segments,
            api_call=ctx.api_call,          # async fn(action, **kwargs)，用于 get_image
        )
        if blocks:
            content = [{"type": "text", "text": text_content}, *blocks]
        else:
            content = text_content          # reason 已在本方法内打过日志
        ```

        - 返回 `(blocks, reason)`。`blocks` 里每项形如
          `{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`,
          **url 一定是 data URI，绝不会是 http 链接**。
        - 成功时 `reason` 为 `""`；`blocks` 为空时 `reason` 是原因码：
          `vision_disabled` / `native_blocks_disabled` / `no_image_segments` /
          `all_conversions_failed`。空列表时调用方退回纯文本即可，不要自己拼 URL。
        - `api_call` 可为 None（私聊/无 NapCat 场景），此时 `onebot_get_image`
          这一取法跳过，其余取法照走。

        配置键（`config.search.vision` 下，代码侧有同值兜底）：
        `native_blocks_enable` / `native_max_images` / `native_max_image_bytes`。
        """

        if not self._vision_enable:
            _tool_log.info(
                "native_vision_blocks_skip%s | reason=vision_disabled", _tool_trace_tag()
            )
            return [], "vision_disabled"
        enabled, cfg_max_images, max_bytes = self._native_vision_limits()
        if not enabled:
            _tool_log.info(
                "native_vision_blocks_skip%s | reason=native_blocks_disabled",
                _tool_trace_tag(),
            )
            return [], "native_blocks_disabled"

        limit = cfg_max_images
        if max_images > 0:
            limit = max(1, min(cfg_max_images, max_images))

        segments: list[dict[str, Any]] = []
        for group in (raw_segments or [], reply_media_segments or []):
            for seg in group:
                if not isinstance(seg, dict):
                    continue
                if normalize_text(str(seg.get("type", ""))).lower() == "image":
                    segments.append(seg)
        if not segments:
            return [], "no_image_segments"

        blocks: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        seen_data_uris: set[str] = set()
        skipped = 0
        for seg in segments:
            if len(blocks) >= limit:
                # 达到上限就停，剩下的不下载 —— 省的是网络往返，不只是 token。
                _tool_log.info(
                    "native_vision_blocks_truncated%s | kept=%d | segments=%d | max_images=%d",
                    _tool_trace_tag(),
                    len(blocks),
                    len(segments),
                    limit,
                )
                break
            dedup_key, _ = self._native_vision_segment_candidates(seg)
            if dedup_key and dedup_key in seen_keys:
                continue
            if dedup_key:
                seen_keys.add(dedup_key)

            data_uri, reason = await self._native_vision_data_uri_for_segment(
                seg=seg,
                api_call=api_call,
                max_bytes=max_bytes,
            )
            if not data_uri:
                skipped += 1
                _tool_log.warning(
                    "native_vision_block_failed%s | key=%s | attempts=%s",
                    _tool_trace_tag(),
                    clip_text(dedup_key, 80) or "-",
                    reason,
                )
                continue
            if data_uri in seen_data_uris:
                # 同一张图在原消息和被引用消息里各出现一次是常态，
                # dedup_key 不同但内容相同，这里再兜一层。
                continue
            seen_data_uris.add(data_uri)
            blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
            _tool_log.info(
                "native_vision_block_ok%s | source=%s | bytes=%d",
                _tool_trace_tag(),
                reason,
                self._data_uri_payload_bytes(data_uri),
            )

        if not blocks:
            _tool_log.warning(
                "native_vision_blocks_empty%s | segments=%d | skipped=%d | reason=all_conversions_failed",
                _tool_trace_tag(),
                len(segments),
                skipped,
            )
            return [], "all_conversions_failed"

        _tool_log.info(
            "native_vision_blocks_ready%s | blocks=%d | segments=%d | skipped=%d | est_tokens=%d | max_images=%d | max_image_bytes=%d",
            _tool_trace_tag(),
            len(blocks),
            len(segments),
            skipped,
            self.estimate_native_vision_tokens(blocks),
            limit,
            max_bytes,
        )
        return blocks, ""

    def _native_vision_limits(self) -> tuple[bool, int, int]:
        """读 `config.search.vision` 下的原生看图三个键，返回 `(启用, 张数, 单图字节)`。

        不在 `ToolExecutor.__init__` 里读是有意的：那份 `__init__` 不在本车道，
        而这里每次调用读一遍 `self._raw_config` 的成本可以忽略，还顺带让
        `/yukibot` 热加载后的新值立即生效（`reload_config` 会重建 tools）。
        """

        raw_cfg = getattr(self, "_raw_config", None)
        if not isinstance(raw_cfg, dict):
            raw_cfg = {}
        search_cfg = raw_cfg.get("search")
        if not isinstance(search_cfg, dict):
            search_cfg = {}
        vision_cfg = search_cfg.get("vision", raw_cfg.get("vision", {}))
        if not isinstance(vision_cfg, dict):
            vision_cfg = {}

        enabled = bool(
            vision_cfg.get(
                "native_blocks_enable", _NATIVE_VISION_BLOCKS_ENABLE_DEFAULT
            )
        )
        try:
            max_images = int(
                vision_cfg.get("native_max_images", _NATIVE_VISION_MAX_IMAGES_DEFAULT)
            )
        except (TypeError, ValueError):
            max_images = _NATIVE_VISION_MAX_IMAGES_DEFAULT
        max_images = max(1, min(_NATIVE_VISION_MAX_IMAGES_CEILING, max_images))
        try:
            max_bytes = int(
                vision_cfg.get(
                    "native_max_image_bytes", _NATIVE_VISION_MAX_IMAGE_BYTES_DEFAULT
                )
            )
        except (TypeError, ValueError):
            max_bytes = _NATIVE_VISION_MAX_IMAGE_BYTES_DEFAULT
        max_bytes = max(
            _NATIVE_VISION_MIN_IMAGE_BYTES_FLOOR,
            min(_NATIVE_VISION_MAX_IMAGE_BYTES_CEILING, max_bytes),
        )
        return enabled, max_images, max_bytes

    @staticmethod
    def _data_uri_payload_bytes(data_uri: str) -> int:
        """从 data URI 反推解码后的字节数，不真的解码。

        base64 每 4 字符还原 3 字节，末尾 `=` 各占位 1 字节，所以
        `(len - padding) * 3 // 4` 就是精确值。用它是为了避免为了量个大小
        再 b64decode 一份几百 KB 的 bytes。
        """

        _, b64 = ToolVisionMixin._decode_data_image_ref(data_uri)
        if not b64:
            return 0
        padding = len(b64) - len(b64.rstrip("="))
        return max(0, (len(b64) * 3) // 4 - padding)

    @staticmethod
    def estimate_native_vision_tokens(blocks: list[dict[str, Any]]) -> int:
        """估算一组 image_url 块**按纯文本 token 化**时的 token 数（上界）。

        算法：token ≈ base64 字符数 / 4。依据是 BPE 对 base64 这种无空格
        随机串大致 4 字符合成 1 token。这是**风险上界**，不是账单预测 ——
        正常 vision API 会解码图片按图块计费（单图约 1.1k~1.6k token），
        只有中转站把 data URI 当普通文本转发时才会真的按这个量级计。

        实测量级（storage/logs/yukiko.log 92 张真实 QQ 图）：
        median 42 KB → ~14k token；p90 387 KB → ~132k token；
        512 KiB 封顶 → ~175k token。所以张数上限不是洁癖，是必需品。
        """

        total = 0
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            image_url = block.get("image_url")
            if not isinstance(image_url, dict):
                continue
            _, b64 = ToolVisionMixin._decode_data_image_ref(
                normalize_text(str(image_url.get("url", "")))
            )
            total += len(b64) // 4
        return total

    def _resolve_vision_provider_hint(self) -> str:
        provider_hint = normalize_text(self._vision_provider).lower()
        if provider_hint:
            return provider_hint
        model_client = getattr(self.image_engine, "model_client", None)
        if model_client is None:
            return ""
        return normalize_text(str(getattr(model_client, "provider", ""))).lower()

    @staticmethod
    def _classify_download_error_body(status_code: int, body: bytes) -> str:
        """把非 200 响应归类成一个稳定的 reason 串。

        QQ CDN 的失效链接是 HTTP 400 + retcode -5503007，单独给一个
        `url_expired` 而不是笼统的 `http_400` —— 这是目前唯一已实测的失效形态，
        日志里必须一眼能认出来，否则又要重新做一遍抓包才知道图为什么没识别。
        """

        snippet = ""
        try:
            snippet = body[:_DOWNLOAD_ERROR_BODY_CLIP].decode("utf-8", "replace").lower()
        except Exception:  # pragma: no cover - decode 已经带 errors="replace"
            snippet = ""
        if "download url has expired" in snippet or "-5503007" in snippet:
            return "url_expired"
        return f"http_{int(status_code)}"

    async def _download_image_as_data_uri_detailed(self, url: str) -> tuple[str, str]:
        """下载远程图片并转为 data URI，返回 `(data_uri, reason)`。

        失败时 data_uri 为空、reason 说明原因；成功时 reason 为空。
        每条失败路径都必须留一条 WARNING —— 之前四条 `return ""` 全部静默，
        线上只能看到最外层的 `vision_image_ref_empty`，根本无法区分是被 SSRF
        护栏拦了、CDN 返回了 4xx、响应体为空，还是图片超限。
        """

        clipped = clip_text(url, 100)
        if not self._is_safe_public_http_url(url):
            _tool_log.warning(
                "vision_image_download_fail%s | reason=ssrf_blocked | url=%s",
                _tool_trace_tag(),
                clipped,
            )
            return "", "ssrf_blocked"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                resp = await client.get(url)
            content_type = str(resp.headers.get("content-type", "")).lower()
            data = resp.content
            if resp.status_code != 200:
                reason = self._classify_download_error_body(resp.status_code, data)
                _tool_log.warning(
                    "vision_image_download_fail%s | reason=%s | status=%d | ctype=%s | body=%s | url=%s",
                    _tool_trace_tag(),
                    reason,
                    resp.status_code,
                    content_type or "-",
                    clip_text(
                        data[:_DOWNLOAD_ERROR_BODY_CLIP].decode("utf-8", "replace"),
                        _DOWNLOAD_ERROR_BODY_CLIP,
                    )
                    or "-",
                    clipped,
                )
                return "", reason
            if not data:
                _tool_log.warning(
                    "vision_image_download_fail%s | reason=empty_body | status=200 | ctype=%s | url=%s",
                    _tool_trace_tag(),
                    content_type or "-",
                    clipped,
                )
                return "", "empty_body"
            if len(data) < self._vision_min_image_bytes:
                _tool_log.warning(
                    "vision_image_ref%s | source=http_url | small_image_warning | bytes=%d | will_try_anyway",
                    _tool_trace_tag(),
                    len(data),
                )
                # 不要拒绝小图片，继续处理
            if len(data) > self._vision_max_image_bytes:
                _tool_log.warning(
                    "vision_image_download_fail%s | reason=too_large | bytes=%d | limit=%d | ctype=%s | url=%s",
                    _tool_trace_tag(),
                    len(data),
                    self._vision_max_image_bytes,
                    content_type or "-",
                    clipped,
                )
                return "", "too_large"
            if "image/" in content_type:
                mime = content_type.split(";")[0].strip()
            else:
                mime = "image/png"
            data_uri = self._to_data_uri_from_image_bytes(
                data,
                mime=mime,
                source="http_url_download",
            )
            if not data_uri:
                _tool_log.warning(
                    "vision_image_download_fail%s | reason=encode_rejected | bytes=%d | ctype=%s | url=%s",
                    _tool_trace_tag(),
                    len(data),
                    content_type or "-",
                    clipped,
                )
                return "", "encode_rejected"
            return data_uri, ""
        except Exception as exc:
            _tool_log.warning(
                "vision_image_download_fail%s | reason=exception | exc=%s | err=%s | url=%s",
                _tool_trace_tag(),
                type(exc).__name__,
                clip_text(str(exc), 160) or "-",
                clipped,
            )
            return "", f"exception_{type(exc).__name__}"

    async def _download_image_as_data_uri(self, url: str) -> str:
        """下载远程图片并转为 data URI（base64），用于 vision API。"""
        data_uri, _reason = await self._download_image_as_data_uri_detailed(url)
        return data_uri

    async def _vision_describe(self, image_ref: str, prompt: str) -> str:
        model_client = getattr(self.image_engine, "model_client", None)
        client = (
            getattr(model_client, "client", None) if model_client is not None else None
        )

        if (
            self._vision_route_text_model_to_local
            and not self._can_use_remote_vision_model()
        ):
            return ""

        if (
            self._vision_require_independent_config
            and not self._has_independent_vision_config()
        ):
            return ""

        provider = (
            self._vision_provider
            or normalize_text(str(getattr(model_client, "provider", ""))).lower()
        )
        api_key = self._vision_api_key or normalize_text(
            str(getattr(client, "api_key", ""))
        )
        base_url = (
            self._vision_base_url
            or normalize_text(str(getattr(client, "base_url", "")))
        ).rstrip("/")
        model_name = self._vision_model or normalize_text(
            str(getattr(client, "model", ""))
        )
        if not api_key or not base_url or not model_name:
            return ""
        model_candidates = self._candidate_vision_models(model_name, client)

        timeout_seconds = float(
            getattr(client, "timeout_seconds", self._vision_timeout_seconds)
            if client is not None
            else self._vision_timeout_seconds
        )
        temperature = (
            float(getattr(client, "temperature", self._vision_temperature))
            if client is not None
            else self._vision_temperature
        )
        max_tokens = (
            int(getattr(client, "max_tokens", self._vision_max_tokens))
            if client is not None
            else self._vision_max_tokens
        )
        prefer_v1 = (
            bool(getattr(client, "prefer_v1", self._vision_prefer_v1))
            if client is not None
            else self._vision_prefer_v1
        )
        image_ref_kind = (
            "data_uri"
            if image_ref.startswith("data:image")
            else ("http_url" if image_ref.startswith("http") else "other")
        )
        _tool_log.info(
            "vision_request%s | provider=%s | model=%s | image_ref=%s | timeout=%.1fs",
            _tool_trace_tag(),
            provider or "-",
            ",".join(model_candidates) or model_name or "-",
            image_ref_kind,
            timeout_seconds,
        )

        if provider == "anthropic":
            text = await self._vision_describe_via_anthropic(
                image_ref=image_ref,
                prompt=prompt,
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                prefer_v1=prefer_v1,
                anthropic_version=normalize_text(
                    str(getattr(client, "anthropic_version", "2023-06-01"))
                ),
            )
            if text:
                return text

        if provider == "gemini":
            text = await self._vision_describe_via_gemini(
                image_ref=image_ref,
                prompt=prompt,
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                api_version=normalize_text(
                    str(getattr(client, "api_version", "v1beta"))
                )
                or "v1beta",
            )
            if text:
                return text

        if provider not in {
            "openai",
            "newapi",
            "deepseek",
            "skiapi",
            "openrouter",
            "xai",
            "qwen",
            "moonshot",
            "mistral",
            "zhipu",
            "siliconflow",
        }:
            # 非 OpenAI 兼容 provider 兜底到文本模式（可能无法真正看图）
            if model_client is None or not bool(
                getattr(model_client, "enabled", False)
            ):
                return ""
            try:
                return normalize_text(
                    await model_client.chat_text(
                        [
                            {
                                "role": "system",
                                "content": SystemPromptRelay.vision_system_prompt_basic(),
                            },
                            {
                                "role": "user",
                                "content": f"{prompt}\n图片链接：{image_ref}",
                            },
                        ]
                    )
                )
            except Exception:
                return ""

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        candidates = self._candidate_openai_bases(
            base_url=base_url, prefer_v1=prefer_v1
        )

        for candidate_model in model_candidates:
            payload = {
                "model": candidate_model,
                "messages": [
                    {
                        "role": "system",
                        "content": SystemPromptRelay.vision_system_prompt_detailed(),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_ref, "detail": "auto"},
                            },
                        ],
                    },
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            for base in candidates:
                url = f"{base}/chat/completions"
                try:
                    async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                        resp = await client_http.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=%s",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                        str(exc)[:240],
                    )
                    continue

                choices = data.get("choices") if isinstance(data, dict) else None
                if not isinstance(choices, list) or not choices:
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=empty_choices",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                    )
                    continue
                message = (
                    choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                )
                content = message.get("content", "")
                if isinstance(content, str):
                    text = normalize_text(content)
                    if text:
                        return text
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=empty_content",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                    )
                    continue
                if isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if isinstance(item, dict):
                            parts.append(normalize_text(str(item.get("text", ""))))
                    text = normalize_text("".join(parts))
                    if text:
                        return text
                    _tool_log.warning(
                        "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | err=empty_content_parts",
                        _tool_trace_tag(),
                        provider or "-",
                        candidate_model or "-",
                        url,
                    )

        # 某些 OpenAI 兼容网关（如部分 skiapi/newapi）在 claude 模型下会返回空 content，
        # 这里自动补一次 Anthropic /messages 兼容路径，避免图片识别整体失效。
        if image_ref.startswith("data:image") and "claude" in model_name.lower():
            anthro_text = await self._vision_describe_via_anthropic(
                image_ref=image_ref,
                prompt=prompt,
                api_key=api_key,
                base_url=base_url,
                model_name=model_name,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                prefer_v1=prefer_v1,
                anthropic_version=normalize_text(
                    str(getattr(client, "anthropic_version", "2023-06-01"))
                ),
            )
            if anthro_text:
                _tool_log.info(
                    "vision_request_fallback%s | route=anthropic_messages_proxy",
                    _tool_trace_tag(),
                )
                return anthro_text
        fallback_text = await self._vision_describe_via_model_fallbacks(
            image_ref=image_ref,
            prompt=prompt,
            model_client=model_client,
            tried_provider=provider,
            tried_model=model_name,
        )
        if fallback_text:
            return fallback_text
        return ""

    async def _vision_describe_via_model_fallbacks(
        self,
        image_ref: str,
        prompt: str,
        model_client: Any,
        tried_provider: str = "",
        tried_model: str = "",
    ) -> str:
        if model_client is None:
            return ""

        fallback_clients = getattr(model_client, "_fallback_clients", {}) or {}
        fallback_providers = list(getattr(model_client, "_fallback_providers", []) or [])
        active_provider = normalize_text(
            str(getattr(model_client, "_active_provider", ""))
        ).lower()
        primary_provider = normalize_text(
            str(getattr(model_client, "_primary_provider", getattr(model_client, "provider", "")))
        ).lower()

        ordered: list[tuple[str, Any, str]] = []
        if active_provider and active_provider != primary_provider:
            active_getter = getattr(model_client, "_get_active_client", None)
            try:
                active_client = active_getter() if callable(active_getter) else None
            except Exception:
                active_client = None
            if active_client is not None:
                ordered.append((active_provider, active_client, "active"))
        for provider_name in fallback_providers:
            provider = normalize_text(str(provider_name)).lower()
            client_obj = fallback_clients.get(provider)
            if client_obj is not None:
                ordered.append((provider, client_obj, "fallback"))

        seen: set[tuple[str, str, str]] = set()
        openai_compatible = {
            "openai",
            "newapi",
            "deepseek",
            "skiapi",
            "openrouter",
            "xai",
            "qwen",
            "moonshot",
            "mistral",
            "zhipu",
            "siliconflow",
        }
        image_ref_kind = (
            "data_uri"
            if image_ref.startswith("data:image")
            else ("http_url" if image_ref.startswith("http") else "other")
        )

        for provider, client_obj, source in ordered:
            provider_text = normalize_text(provider).lower()
            api_key = normalize_text(str(getattr(client_obj, "api_key", "")))
            base_url = normalize_text(str(getattr(client_obj, "base_url", ""))).rstrip("/")
            model_name = self._vision_model or normalize_text(str(getattr(client_obj, "model", "")))
            if not provider_text or not api_key or not base_url or not model_name:
                continue
            marker = (provider_text, base_url, model_name)
            if marker in seen:
                continue
            seen.add(marker)
            if (
                provider_text == normalize_text(tried_provider).lower()
                and model_name == normalize_text(tried_model)
            ):
                continue

            timeout_seconds = float(
                getattr(client_obj, "timeout_seconds", self._vision_timeout_seconds)
            )
            temperature = float(
                getattr(client_obj, "temperature", self._vision_temperature)
            )
            max_tokens = int(getattr(client_obj, "max_tokens", self._vision_max_tokens))
            prefer_v1 = bool(getattr(client_obj, "prefer_v1", self._vision_prefer_v1))
            model_candidates = self._candidate_vision_models(model_name, client_obj)

            _tool_log.info(
                "vision_request_fallback%s | provider=%s | model=%s | source=%s | image_ref=%s | timeout=%.1fs",
                _tool_trace_tag(),
                provider_text or "-",
                ",".join(model_candidates) or model_name or "-",
                source,
                image_ref_kind,
                timeout_seconds,
            )

            if provider_text == "anthropic":
                text = await self._vision_describe_via_anthropic(
                    image_ref=image_ref,
                    prompt=prompt,
                    api_key=api_key,
                    base_url=base_url,
                    model_name=model_name,
                    timeout_seconds=timeout_seconds,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    prefer_v1=prefer_v1,
                    anthropic_version=normalize_text(
                        str(getattr(client_obj, "anthropic_version", "2023-06-01"))
                    ),
                )
                if text:
                    _tool_log.info(
                        "vision_provider_failover_ok%s | provider=%s | source=%s",
                        _tool_trace_tag(),
                        provider_text,
                        source,
                    )
                    return text
                continue

            if provider_text == "gemini":
                text = await self._vision_describe_via_gemini(
                    image_ref=image_ref,
                    prompt=prompt,
                    api_key=api_key,
                    base_url=base_url,
                    model_name=model_name,
                    timeout_seconds=timeout_seconds,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    api_version=normalize_text(str(getattr(client_obj, "api_version", "v1beta"))) or "v1beta",
                )
                if text:
                    _tool_log.info(
                        "vision_provider_failover_ok%s | provider=%s | source=%s",
                        _tool_trace_tag(),
                        provider_text,
                        source,
                    )
                    return text
                continue

            if provider_text not in openai_compatible:
                continue

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            bases = self._candidate_openai_bases(
                base_url=base_url, prefer_v1=prefer_v1
            )
            for candidate_model in model_candidates:
                payload = {
                    "model": candidate_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": SystemPromptRelay.vision_system_prompt_detailed(),
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_ref, "detail": "auto"},
                                },
                            ],
                        },
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                for base in bases:
                    url = f"{base}/chat/completions"
                    try:
                        async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                            resp = await client_http.post(url, headers=headers, json=payload)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as exc:
                        _tool_log.warning(
                            "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | source=%s | err=%s",
                            _tool_trace_tag(),
                            provider_text or "-",
                            candidate_model or "-",
                            url,
                            source,
                            str(exc)[:240],
                        )
                        continue

                    choices = data.get("choices") if isinstance(data, dict) else None
                    if not isinstance(choices, list) or not choices:
                        _tool_log.warning(
                            "vision_provider_failed_exact%s | provider=%s | model=%s | url=%s | source=%s | err=empty_choices",
                            _tool_trace_tag(),
                            provider_text or "-",
                            candidate_model or "-",
                            url,
                            source,
                        )
                        continue
                    message = (
                        choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                    )
                    content = message.get("content", "")
                    if isinstance(content, str):
                        text = normalize_text(content)
                        if text:
                            _tool_log.info(
                                "vision_provider_failover_ok%s | provider=%s | source=%s",
                                _tool_trace_tag(),
                                provider_text,
                                source,
                            )
                            return text
                        continue
                    if isinstance(content, list):
                        parts = [
                            normalize_text(str(item.get("text", "")))
                            for item in content
                            if isinstance(item, dict)
                        ]
                        text = normalize_text("".join(parts))
                        if text:
                            _tool_log.info(
                                "vision_provider_failover_ok%s | provider=%s | source=%s",
                                _tool_trace_tag(),
                                provider_text,
                                source,
                            )
                            return text

        return ""

    async def _vision_describe_via_anthropic(
        self,
        image_ref: str,
        prompt: str,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float,
        temperature: float,
        max_tokens: int,
        prefer_v1: bool,
        anthropic_version: str,
    ) -> str:
        mime, b64 = self._decode_data_image_ref(image_ref)
        if not mime or not b64:
            return ""

        payload = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": SystemPromptRelay.vision_system_prompt_detailed(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": b64,
                            },
                        },
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": anthropic_version or "2023-06-01",
            "Content-Type": "application/json",
        }
        candidates = self._candidate_openai_bases(
            base_url=base_url, prefer_v1=prefer_v1
        )
        for base in candidates:
            url = f"{base}/messages"
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                    resp = await client_http.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for item in content:
                if (
                    isinstance(item, dict)
                    and normalize_text(str(item.get("type", ""))) == "text"
                ):
                    parts.append(normalize_text(str(item.get("text", ""))))
            text = normalize_text("".join(parts))
            if text:
                return text
        return ""

    async def _vision_describe_via_gemini(
        self,
        image_ref: str,
        prompt: str,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float,
        temperature: float,
        max_tokens: int,
        api_version: str,
    ) -> str:
        mime, b64 = self._decode_data_image_ref(image_ref)
        if not mime or not b64:
            return ""

        base = normalize_text(base_url).rstrip("/")
        for suffix in ("/v1beta", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if not base:
            return ""

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": b64}},
                    ],
                }
            ],
            "system_instruction": {
                "parts": [{"text": SystemPromptRelay.vision_system_prompt_detailed()}]
            },
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        ver = normalize_text(api_version).strip("/") or "v1beta"
        url = f"{base}/{ver}/models/{model_name}:generateContent?key={api_key}"
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client_http:
                resp = await client_http.post(
                    url, headers={"Content-Type": "application/json"}, json=payload
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return ""

        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            return ""
        content = (
            candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        )
        parts = content.get("parts", []) if isinstance(content, dict) else []
        if not isinstance(parts, list):
            return ""
        out: list[str] = []
        for item in parts:
            if isinstance(item, dict):
                out.append(normalize_text(str(item.get("text", ""))))
        return normalize_text("".join(out))

    @staticmethod
    def _decode_data_image_ref(image_ref: str) -> tuple[str, str]:
        raw = normalize_text(image_ref)
        if not raw.startswith("data:image") or ";base64," not in raw:
            return "", ""
        head, b64 = raw.split(";base64,", 1)
        mime = normalize_text(head.replace("data:", ""))
        data = normalize_text(b64)
        if not mime or not data:
            return "", ""
        return mime, data

    def _has_independent_vision_config(self) -> bool:
        return bool(
            self._vision_provider
            and self._vision_base_url
            and self._vision_model
            and self._vision_api_key
        )

    def _candidate_vision_models(self, primary: str, client: Any | None) -> list[str]:
        values: list[Any] = [primary]
        values.extend(getattr(self, "_vision_fallback_models", []) or [])
        if client is not None and not getattr(self, "_vision_model", ""):
            client_cfg = getattr(client, "config", {}) or {}
            if isinstance(client_cfg, dict):
                values.extend(
                    self._normalize_vision_model_list(
                        client_cfg.get("vision_fallback_models", [])
                    )
                )
                values.extend(
                    self._normalize_vision_model_list(client_cfg.get("fallback_models", []))
                )
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = normalize_text(str(item or ""))
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result or [primary]

    @staticmethod
    def _normalize_vision_model_list(raw: Any) -> list[str]:
        if isinstance(raw, str):
            values = re.split(r"[,;\n]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = list(raw)
        else:
            values = []
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = normalize_text(str(item or ""))
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    async def _normalize_vision_answer(self, answer: str, prompt: str) -> str:
        content = normalize_text(answer)
        if not content:
            return ""
        if content.strip().lower() in {"-", "--", "n/a", "na", "null", "none"}:
            return ""
        content = re.sub(r"\s+", " ", content).strip()

        # 尝试翻译非中文内容
        if (
            self._looks_like_non_chinese_text(content)
            and self._vision_retry_translate_enable
        ):
            translated = await self._translate_to_chinese(
                content=content, prompt=prompt
            )
            if translated:
                content = translated
        # 移除过于严格的二次检查，即使内容包含英文也应返回，而不是丢弃
        return normalize_text(content)

    async def _normalize_vision_answer_with_retry(
        self,
        image_ref: str,
        answer: str,
        prompt: str,
        query: str,
        message_text: str,
        animated_hint: bool = False,
    ) -> str:
        normalized = await self._normalize_vision_answer(answer, prompt=prompt)
        need_retry = not normalized or self._looks_like_weak_vision_answer(normalized)
        if not self._vision_second_pass_enable or not need_retry:
            return normalized

        retry_prompt = self._build_vision_retry_prompt(
            query=query,
            message_text=message_text,
            animated_hint=animated_hint,
        )
        retry_raw = await self._vision_describe(
            image_ref=image_ref, prompt=retry_prompt
        )
        retry_norm = await self._normalize_vision_answer(retry_raw, prompt=retry_prompt)
        if retry_norm and not self._looks_like_weak_vision_answer(retry_norm):
            return retry_norm
        if retry_norm and not normalized:
            return retry_norm
        return normalized or retry_norm

    async def _translate_to_chinese(self, content: str, prompt: str) -> str:
        model_client = getattr(self.image_engine, "model_client", None)
        if model_client is None or not bool(getattr(model_client, "enabled", False)):
            return ""
        try:
            translated = await model_client.chat_text(
                [
                    {
                        "role": "system",
                        "content": SystemPromptRelay.translate_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": (
                            "把下面识图结果翻译为自然中文并保持原意：\n"
                            f"用户问题：{normalize_text(prompt)}\n"
                            f"原始结果：{normalize_text(content)}"
                        ),
                    },
                ]
            )
        except Exception:
            return ""
        translated_text = normalize_text(translated)
        if not translated_text:
            return ""
        if self._looks_like_non_chinese_text(translated_text):
            return ""
        return translated_text

    @staticmethod
    def _looks_like_non_chinese_text(text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return False
        cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
        alpha_count = sum(1 for ch in content if ch.isalpha())
        if alpha_count < 8:
            return False
        return cjk_count / max(alpha_count, 1) < 0.25

    @staticmethod
    def _looks_like_weak_vision_answer(text: str) -> bool:
        content = normalize_text(text)
        if not content:
            return True
        plain = re.sub(r"\s+", "", content).lower()
        explicit_markers = (
            "???",
            "n/a",
            "unknown",
            "无法识别???",
            "识别失败???",
        )
        if any(marker in plain for marker in explicit_markers):
            return True
        return False

    @staticmethod
    def _candidate_openai_bases(base_url: str, prefer_v1: bool) -> list[str]:
        base = normalize_text(base_url).rstrip("/")
        if not base:
            return []
        with_v1 = base if base.endswith("/v1") else f"{base}/v1"
        without_v1 = base[:-3] if base.endswith("/v1") else base
        ordered = [with_v1, without_v1] if prefer_v1 else [without_v1, with_v1]
        out: list[str] = []
        for item in ordered:
            value = normalize_text(item).rstrip("/")
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def _looks_like_image_analysis_request(text: str) -> bool:
        """只认显式类型化命令 `/analyze <图片直链>`。

        识图意图交给模型读 Prompt Navigator 的 multimodal_media 分区说明后直接调
        analyze_image，不再由本地词表猜。原来这里还比对 prompts.yml 的
        image_question_cues 词表，但该 key 在 config/ 里根本不存在
        （get_list 实测返回 []），那条分支本就恒假，已删。
        """
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        if re.search(
            r"(?:^|\s)/(?:analyze|analyse|summary|summarize)(?:\s|$)", content
        ) and re.search(
            r"https?://\S+\.(png|jpg|jpeg|webp|bmp|gif)(?:\?\S*)?$", content
        ):
            return True
        return False
