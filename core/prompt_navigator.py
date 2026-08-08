"""Prompt Navigator configuration and section switching helpers."""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.prompt_navigator")

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_BARE_WEB_HOST_RE = re.compile(
    r"(?<![@A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|dev|io|ai|app|site|xyz|me|co|cn|jp|tv|gg|cc|info|wiki|top)"
    # TLD 后必须是非字母数字边界：少了它，`photo.jpg` 会被截成裸域名 `photo.jp`（jp 在表里），
    # 于是"帮我发个 photo.jpg"被当成含 URL、起始分区推去 web_research。
    # 同类碰撞还有 .aiff/.ai、.appx/.app、.cnf/.cn、.tvg/.tv。
    r"(?![A-Za-z0-9-])"
    r"(?::\d{2,5})?(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?)",
    re.IGNORECASE,
)
_DOWNLOAD_EXT_RE = re.compile(r"\.(?:apk|exe|msi|zip|7z|rar|ipa|dmg)(?:[?#]|$)", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|bmp|heic|heif|avif)(?=$|[?#&!@_/])", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm|mov|m4v|mkv)(?:[?#]|$)", re.IGNORECASE)
_VIDEO_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "acfun.cn",
    "ixigua.com",
    "iqiyi.com",
    "qiyi.com",
    "iq.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "v.qq.com",
    "m.v.qq.com",
)

NAVIGATE_SECTION_TOOL = "navigate_section"
CONTROL_TOOLS = ("think", "final_answer", NAVIGATE_SECTION_TOOL, "read_skill")


@dataclass(slots=True)
class PromptSection:
    id: str
    name: str = ""
    when_to_use: str = ""
    tools: list[str] = field(default_factory=list)
    instructions: str = ""
    fallback_sections: list[str] = field(default_factory=list)
    failure_policy: str = ""


@dataclass(slots=True)
class PromptNavigatorConfig:
    # 这里没有 mode 字段：历史上的 mode="local_prefilter_llm_review" 全仓没有任何读点，
    # 只是每回合被打进 system prompt。且"本地预筛 + LLM 复核"这个说法与当前架构相反——
    # 本地只提供 _preselect 的结构事实提示，意图判断完全由模型读菜单完成。
    # 配置里残留的 mode 键会被忽略（load_prompt_navigator_config 逐字段取值，不报未知键）。
    enable: bool = True
    strict_tool_routing: bool = True
    default_section: str = "general_chat"
    max_switches: int = 3
    root_prompt: str = ""
    sections: dict[str, PromptSection] = field(default_factory=dict)


@dataclass(slots=True)
class NavigatorState:
    active_section: str
    candidate_sections: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    visible_tools: list[str] = field(default_factory=list)
    switch_count: int = 0
    visited_sections: list[str] = field(default_factory=list)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = normalize_text(value).lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize_text(str(item)) for item in value if normalize_text(str(item))]
    if isinstance(value, tuple):
        return [normalize_text(str(item)) for item in value if normalize_text(str(item))]
    if isinstance(value, str):
        parts: list[str] = []
        for line in value.replace(",", "\n").splitlines():
            text = normalize_text(line)
            if text:
                parts.append(text)
        return parts
    return []


_NAV_DATA_FILE = Path(__file__).resolve().parent / "prompt_navigator_data.yml"

# 读取成功才缓存；读失败回退到最小内联默认，保证任何情况下初始化都不崩。
_nav_payload_cache: dict[str, Any] | None = None

_MINIMAL_PAYLOAD: dict[str, Any] = {
    "enable": True,
    "strict_tool_routing": True,
    "default_section": "general_chat",
    "max_switches": 6,
    "root_prompt": "",
    "sections": {
        "general_chat": {
            "name": "通用对话",
            "when_to_use": "默认分区",
            "tools": ["think", "final_answer"],
            "instructions": "默认通用分区。",
            "fallback_sections": [],
            "failure_policy": "",
        },
    },
}


def default_prompt_navigator_payload() -> dict[str, Any]:
    """Default editable Prompt Navigator graph.

    数据本体在 core/prompt_navigator_data.yml（20 个分区），本函数只是加载入口：
    首次调用读 YAML 并缓存，读失败回退到最小内联默认。数据文件随包发布，属只读默认值，
    修改后需重启进程生效（与 prompts.yml 的可编辑热重载路径不同）。

    以下 12 个已注册工具**故意**不进任何 section.tools，请勿回补
    （理由见 .migration/tool-coverage.md 第 5 节）：
      凭证类：get_cookies / get_credentials / get_csrf_token / nc_get_rkey
      任意代码执行类：cli_invoke / create_skill / test_in_sandbox
      演示假数据：example_lookup
      引擎层结构判断，不是对话能力：can_send_image / can_send_record / get_robot_uin_range
      模型无法产出合法参数：get_mini_app_ark
    think / final_answer / navigate_section 是 CONTROL_TOOLS，由 scoped_tools() 无条件
    注入每个分区，各 section.tools 里也显式列了一份（冗余但无害）；目录里的工具数
    已排除它们，见 render_system_block()。
    """
    global _nav_payload_cache
    if _nav_payload_cache is None:
        _nav_payload_cache = _load_navigator_payload()
    return copy.deepcopy(_nav_payload_cache)


def _load_navigator_payload() -> dict[str, Any]:
    """从 core/prompt_navigator_data.yml 读取默认载荷，失败回退最小内联默认。"""
    if not _NAV_DATA_FILE.exists():
        _log.warning("prompt_navigator_data_missing | path=%s | fallback=minimal", _NAV_DATA_FILE)
        return copy.deepcopy(_MINIMAL_PAYLOAD)
    try:
        raw = yaml.safe_load(_NAV_DATA_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _log.warning("prompt_navigator_data_load_error | err=%s | fallback=minimal", exc)
        return copy.deepcopy(_MINIMAL_PAYLOAD)
    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), dict):
        _log.warning("prompt_navigator_data_invalid | path=%s | fallback=minimal", _NAV_DATA_FILE)
        return copy.deepcopy(_MINIMAL_PAYLOAD)
    return raw


def load_prompt_navigator_config(raw: Any) -> PromptNavigatorConfig:
    if not isinstance(raw, dict):
        raw = default_prompt_navigator_payload()
    defaults = default_prompt_navigator_payload()
    merged = dict(defaults)
    merged.update(raw)
    if not isinstance(merged.get("sections"), dict):
        merged["sections"] = defaults["sections"]

    sections: dict[str, PromptSection] = {}
    for section_id, value in (merged.get("sections") or {}).items():
        sid = normalize_text(str(section_id))
        if not sid or not isinstance(value, dict):
            continue
        # 自由文本字段只做 strip：normalize_text 会把 \s+ 压成单空格，
        # 多行 prompt 正文（instructions / when_to_use 等）必须保留换行。
        sections[sid] = PromptSection(
            id=sid,
            name=normalize_text(str(value.get("name", ""))),
            when_to_use=str(value.get("when_to_use", "") or "").strip(),
            tools=_as_list(value.get("tools")),
            instructions=str(value.get("instructions", "") or "").strip(),
            fallback_sections=_as_list(value.get("fallback_sections")),
            failure_policy=str(value.get("failure_policy", "") or "").strip(),
        )

    default_section = normalize_text(str(merged.get("default_section", "general_chat"))) or "general_chat"
    if default_section not in sections and sections:
        default_section = next(iter(sections.keys()))

    try:
        max_switches = int(merged.get("max_switches", 3))
    except (TypeError, ValueError):
        max_switches = 3
    max_switches = max(0, min(12, max_switches))

    return PromptNavigatorConfig(
        enable=_as_bool(merged.get("enable", True), default=True),
        strict_tool_routing=_as_bool(
            merged.get("strict_tool_routing", True),
            default=True,
        ),
        default_section=default_section,
        max_switches=max_switches,
        root_prompt=str(merged.get("root_prompt", "") or "").strip(),
        sections=sections,
    )


def validate_prompt_navigator_payload(
    raw: Any,
    known_tools: set[str] | list[str] | tuple[str, ...] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for WebUI validation."""
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return ["prompt_navigator 必须是对象"], []
    errors: list[str] = []
    warnings: list[str] = []
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, dict) or not sections_raw:
        errors.append("prompt_navigator.sections 必须是非空对象")
        return errors, warnings
    section_ids = {normalize_text(str(key)) for key in sections_raw.keys() if normalize_text(str(key))}
    default_section = normalize_text(str(raw.get("default_section", "")))
    if default_section and default_section not in section_ids:
        errors.append(f"默认分区不存在: {default_section}")
    known_tool_set = {normalize_text(str(name)) for name in (known_tools or []) if normalize_text(str(name))}

    for section_id, value in sections_raw.items():
        sid = normalize_text(str(section_id))
        if not sid:
            errors.append("存在空分区 ID")
            continue
        if not isinstance(value, dict):
            errors.append(f"分区 {sid} 必须是对象")
            continue
        for fallback in _as_list(value.get("fallback_sections")):
            if fallback not in section_ids:
                errors.append(f"分区 {sid} 的 fallback 不存在: {fallback}")
        if known_tool_set:
            for tool_name in _as_list(value.get("tools")):
                if tool_name not in known_tool_set:
                    warnings.append(f"分区 {sid} 引用了未知工具: {tool_name}")
    return errors, warnings


class PromptNavigator:
    def __init__(self, config: PromptNavigatorConfig) -> None:
        self.config = config

    @classmethod
    def from_payload(cls, raw: Any) -> PromptNavigator:
        return cls(load_prompt_navigator_config(raw))

    @property
    def enabled(self) -> bool:
        return bool(self.config.enable and self.config.sections)

    def initial_state(self, ctx: Any, visible_tools: list[str]) -> NavigatorState:
        active, candidates, evidence = self._preselect(ctx)
        return NavigatorState(
            active_section=active,
            candidate_sections=candidates,
            evidence=evidence,
            visible_tools=list(visible_tools),
            switch_count=0,
            visited_sections=[active],
        )

    def scoped_tools(self, state: NavigatorState) -> list[str]:
        visible = {normalize_text(str(name)) for name in state.visible_tools if normalize_text(str(name))}
        section = self.config.sections.get(state.active_section)
        requested = list(CONTROL_TOOLS)
        if section:
            requested.extend(section.tools)
        return _dedupe([name for name in requested if name in visible])

    def switch_section(self, state: NavigatorState, section_id: str) -> tuple[bool, str]:
        target = normalize_text(section_id)
        if not target:
            return False, "missing_section_id"
        if target not in self.config.sections:
            return False, f"unknown_section:{target}"
        if state.switch_count >= self.config.max_switches:
            return False, f"max_switches_reached:{self.config.max_switches}"
        if target == state.active_section:
            return True, "same_section"
        state.active_section = target
        state.switch_count += 1
        if target not in state.visited_sections:
            state.visited_sections.append(target)
        if target not in state.candidate_sections:
            state.candidate_sections.append(target)
        return True, "switched"

    def render_system_block(self, state: NavigatorState, scoped_tools: list[str]) -> str:
        lines: list[str] = ["## Prompt Navigator"]
        if self.config.root_prompt:
            lines.append(self.config.root_prompt)
        if state.evidence:
            lines.append("本地结构信号: " + "；".join(state.evidence[:6]))
        if state.candidate_sections:
            lines.append("候选分区: " + ", ".join(state.candidate_sections))
        lines.append("协议: 当前分区不够用或缺少工具时，先调用 navigate_section(section_id, reason) 切分区；切换后再调用新分区工具。")
        lines.append("")
        lines.append("分区目录:")
        for sid, item in self.config.sections.items():
            label = item.name or sid
            # 目录只放能力摘要（when_to_use 首行）+ 工具数量：
            # 完整 when_to_use、全量工具名和原生 schema 由 render_active_section_block()
            # 在进入该分区后单独给出，铺进目录属于每回合都付全款的冗余。
            summary = (item.when_to_use or "").strip().split("\n")[0].strip() or "按分区说明判断"
            # 工具数排除 CONTROL_TOOLS：think / final_answer / navigate_section 在每个分区都
            # 无条件可见，算进去只会让 general_chat 这种「零能力」分区显示成「3 工具」，
            # 反而暗示模型那里有活可干。
            tool_count = sum(1 for name in item.tools if name not in CONTROL_TOOLS)
            # fallback 不进目录：render_active_section_block() 已给出当前分区的建议 fallback，
            # 模型不需要背下另外 19 个分区的退路。
            lines.append(f"- {sid} ({label}, {tool_count} 工具): {summary}")
        lines.append("")
        lines.append(self.render_active_section_block(state, scoped_tools))
        return "\n".join(lines).strip()

    def render_active_section_block(self, state: NavigatorState, scoped_tools: list[str]) -> str:
        section = self.config.sections.get(state.active_section)
        if not section:
            return f"当前分区: {state.active_section}\n当前分区未找到，请切到 fallback_debug。"
        lines = [
            f"当前分区: {section.id} ({section.name or section.id})",
            f"使用条件: {section.when_to_use or '-'}",
            f"分区指令: {section.instructions or '-'}",
            f"失败策略: {section.failure_policy or '-'}",
            "当前分区可见工具: " + (", ".join(scoped_tools) if scoped_tools else "-"),
            "建议 fallback: " + (", ".join(section.fallback_sections) if section.fallback_sections else "-"),
        ]
        return "\n".join(lines)

    def render_switch_result(
        self,
        state: NavigatorState,
        scoped_tools: list[str],
        tool_docs: str = "",
    ) -> str:
        block = self.render_active_section_block(state, scoped_tools)
        if normalize_text(tool_docs):
            block += "\n\n新分区工具 schema:\n" + clip_text(tool_docs, 2400)
        return block

    def _preselect(self, ctx: Any) -> tuple[str, list[str], list[str]]:
        """只读结构事实排出**起始**分区，模型可以随时 navigate_section 否决。

        两档强度：
        - `add()` 强信号：信号本身就限定了能力族（视频直链、图片段、下载后缀…），
          可以当起始分区。
        - `add_weak()` 弱信号：只说明"消息里有这个结构"，完全不限定用户想干什么。
          只进候选列表，排在 default_section 之后，永远不会成为起始分区。
        """
        candidates: list[str] = []
        weak_candidates: list[str] = []
        evidence: list[str] = []

        def add(section_id: str, why: str) -> None:
            if section_id not in self.config.sections:
                return
            if section_id not in candidates:
                candidates.append(section_id)
            if why and why not in evidence:
                evidence.append(why)

        def add_weak(section_id: str, why: str) -> None:
            if section_id not in self.config.sections:
                return
            if section_id not in candidates and section_id not in weak_candidates:
                weak_candidates.append(section_id)
            if why and why not in evidence:
                evidence.append(why)

        current_urls = self._collect_urls(ctx, include_recent_artifact=False)
        urls = self._collect_urls(ctx)
        segment_kinds = self._collect_segment_kinds(ctx)
        has_current_video_url = any(self._looks_like_video_url(url) for url in current_urls)
        has_current_image_url = any(self._looks_like_image_url(url) for url in current_urls)
        if has_current_video_url:
            add("video_url", "video_url")
        if has_current_image_url:
            add("multimodal_media", "image_url")
        recent_artifact = getattr(ctx, "recent_media_artifact", None)
        if isinstance(recent_artifact, dict) and not (has_current_video_url or has_current_image_url):
            artifact_type = normalize_text(str(recent_artifact.get("type", ""))).lower()
            artifact_video = normalize_text(
                str(
                    recent_artifact.get("video_url", "")
                    or recent_artifact.get("video_file", "")
                    or recent_artifact.get("path", "")
                )
            )
            artifact_images = recent_artifact.get("image_urls", [])
            if artifact_type == "video" or artifact_video:
                add("video_url", "recent_media_artifact")
            elif artifact_type in {"image", "images"} or artifact_images:
                add("multimodal_media", "recent_media_artifact")
        if {"image", "voice", "audio", "video"} & segment_kinds:
            add("multimodal_media", "message_or_reply_media")
        if any(_DOWNLOAD_EXT_RE.search(url) for url in urls):
            add("download_resources", "download_file_extension")
        if urls:
            add("web_research", "url")
        # @了人是弱信号：群里 @ 某人绝大多数是普通对话（"@小明 你觉得呢"），
        # 与"想对这个人做群管理操作"没有关系。让它成为起始分区，等于每条 @ 消息
        # 一开局就把 set_group_kick / set_group_ban / set_group_whole_ban 这类
        # 不可逆写操作摆到模型面前，而用户根本没提管理。
        # 真要管理时模型自己 navigate_section 过去，成本是一次跳转。
        if getattr(ctx, "at_other_user_ids", None):
            add_weak("qq_admin_social", "mention_target")

        default = self.config.default_section
        if default in self.config.sections and default not in candidates:
            candidates.append(default)
        for section_id in weak_candidates:
            if section_id not in candidates:
                candidates.append(section_id)
        if "fallback_debug" in self.config.sections and "fallback_debug" not in candidates:
            candidates.append("fallback_debug")
        active = candidates[0] if candidates else default
        return active, candidates, evidence

    @staticmethod
    def _collect_segment_kinds(ctx: Any) -> set[str]:
        kinds: set[str] = set()
        for attr in ("media_summary", "reply_media_summary"):
            for item in getattr(ctx, attr, None) or []:
                text = normalize_text(str(item)).lower()
                if text.startswith("image:"):
                    kinds.add("image")
                elif text.startswith(("record:", "audio:", "voice:")):
                    kinds.add("voice")
                elif text.startswith("video:"):
                    kinds.add("video")
        for attr in ("raw_segments", "reply_media_segments"):
            for segment in getattr(ctx, attr, None) or []:
                if not isinstance(segment, dict):
                    continue
                seg_type = normalize_text(str(segment.get("type", ""))).lower()
                data = segment.get("data", {})
                if seg_type in {"image", "pic", "picture"}:
                    kinds.add("image")
                elif seg_type in {"record", "voice", "audio", "ptt"}:
                    kinds.add("voice")
                elif seg_type in {"video", "shortvideo"}:
                    kinds.add("video")
                if isinstance(data, dict):
                    # 必须由 seg_type 把关：视频段的 data.image 是封面缩略图，
                    # 少了括号会让它被当成图片，把该走视频的回合推去 multimodal_media。
                    if seg_type in {"image", "pic", "picture"} and (
                        data.get("image") or data.get("url")
                    ):
                        kinds.add("image")
                    if data.get("file") and seg_type in {"record", "voice"}:
                        kinds.add("voice")
        return kinds

    @staticmethod
    def _collect_urls(ctx: Any, include_recent_artifact: bool = True) -> list[str]:
        parts: list[str] = []
        for attr in ("message_text", "original_message_text", "reply_to_text"):
            text = normalize_text(str(getattr(ctx, attr, "") or ""))
            if text:
                parts.append(text)
        for attr in ("media_summary", "reply_media_summary"):
            for item in getattr(ctx, attr, None) or []:
                text = normalize_text(str(item))
                if text:
                    parts.append(text)
        recent_artifact = getattr(ctx, "recent_media_artifact", None)
        if include_recent_artifact and isinstance(recent_artifact, dict):
            for key in ("video_url", "video_file", "image_url", "url", "source_url", "path"):
                text = normalize_text(str(recent_artifact.get(key, "")))
                if text:
                    parts.append(text)
            raw_image_urls = recent_artifact.get("image_urls", [])
            if isinstance(raw_image_urls, list):
                for item in raw_image_urls:
                    text = normalize_text(str(item))
                    if text:
                        parts.append(text)
        for attr in ("raw_segments", "reply_media_segments"):
            for segment in getattr(ctx, attr, None) or []:
                if not isinstance(segment, dict):
                    continue
                data = segment.get("data", {})
                if isinstance(data, dict):
                    for key in ("url", "file", "path"):
                        text = normalize_text(str(data.get(key, "")))
                        if text:
                            parts.append(text)
        urls: list[str] = []
        for part in parts:
            for match in _URL_RE.findall(part):
                url = match.rstrip(").,，。!?！？】》」』")
                if url not in urls:
                    urls.append(url)
            for match in _BARE_WEB_HOST_RE.findall(part):
                url = "https://" + match.rstrip(").,，。!?！？】》」』")
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _looks_like_video_url(url: str) -> bool:
        text = normalize_text(url).lower()
        if not text:
            return False
        if _VIDEO_EXT_RE.search(text):
            return True
        try:
            host = (urlparse(text).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return False
        return any(host == domain or host.endswith("." + domain) for domain in _VIDEO_DOMAINS)

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        text = normalize_text(url).lower()
        if not text:
            return False
        if text.startswith("data:image/"):
            return True
        if "multimedia.nt.qq.com.cn/download" in text:
            return True
        return bool(_IMAGE_EXT_RE.search(text))

def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = normalize_text(str(item))
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
