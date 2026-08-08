"""纯文本判断/解析函数的收敛模块。

这些函数原本以 staticmethod 形式散落在 `core/agent.py` / `core/engine.py`
的 God class 里，既不依赖 self、也不依赖实例状态，因此收敛到独立模块。
搬移时保持逻辑完全不变。
"""

from __future__ import annotations

import json
import re
from typing import Any

from utils.text import clip_text, normalize_text

_RE_WHITESPACE = re.compile(r"\s+")
_RE_WHITESPACE_2PLUS = re.compile(r"\s{2,}")
_RE_URL_STRIP = re.compile(r"https?://\S+", re.IGNORECASE)
_RE_BARE_WEB_HOST = re.compile(
    r"(?<![@A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|dev|io|ai|app|site|xyz|me|co|cn|jp|tv|gg|cc|info|wiki|top)"
    r"(?::\d{2,5})?(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?)",
    re.IGNORECASE,
)
_RE_PUNCTUATION_CJK = re.compile(
    r"[\s\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\u2000-\u206f\u2e00-\u2e7f!-/:-@\[-`{-~。，、；：？！…—·''""〈〉《》「」『』【】〔〕〖〗]+"
)
_RE_CODE_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def cue_is_negated(content: str, cue: str) -> bool:
    """确认词前面紧挨着否定词时，这不是确认，是拒绝。

    只做语法否定判定，不是语义词表。否定词必须紧邻确认词才算。
    """
    negators = ("不", "别", "勿", "非", "无法", "未", "没", "莫")
    # 否定词与确认词之间常隔一个情态字（「不要确认」「不能确认」「不用确认」）。
    # 这些字本身不表意，剥掉再判否定。注意不能把主语一起剥 ——
    # 「我要确认」剥成「我」，不是否定词，仍算确认。
    modals = "要想能会用准许可以得了着"
    start = 0
    while True:
        idx = content.find(cue, start)
        if idx < 0:
            return True  # 每一处出现都被否定了
        prefix = content[:idx].rstrip(modals)
        if not any(prefix.endswith(neg) for neg in negators):
            return False  # 存在一处未被否定的确认词 → 视为确认
        start = idx + 1


def to_declared_flag(value: Any) -> bool:
    """把模型声明的布尔参数读成 bool。

    模型可能给真 bool，也可能给 "true"/"false" 字符串；后者用 bool() 判断会把
    "false" 当真。这里只做类型解析，不读用户原文，因此不是意图猜测。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return normalize_text(str(value or "")).lower() in {"true", "1", "yes", "y", "on"}


def to_safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = normalize_text(str(value))
    if not text or not re.fullmatch(r"-?\d+", text):
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def infer_lookup_keyword(text: str) -> str:
    t = normalize_text(text)
    if not t:
        return ""
    t = re.sub(r"^(?i:/(?:lookup|wiki))\s*", "", t)
    t = re.sub(r"^(?i:keyword)\s*=\s*", "", t)
    t = _RE_PUNCTUATION_CJK.sub(" ", t)
    t = _RE_WHITESPACE.sub(" ", t).strip()
    return t[:80]


def infer_split_video_mode(text: str) -> str:
    t = normalize_text(text).lower()
    if not t:
        return ""
    plain = _RE_WHITESPACE.sub("", t)
    if "mode=audio" in plain:
        return "audio"
    if "mode=cover" in plain:
        return "cover"
    if "mode=frames" in plain or "mode=frame" in plain:
        return "frames"
    if "mode=clip" in plain or re.search(
        r"\b\d+(?:\.\d+)?\s*(?:s|sec|seconds?)\s*-\s*\d+(?:\.\d+)?\s*(?:s|sec|seconds?)\b",
        t,
    ):
        return "clip"
    return ""


def parse_time_token_to_seconds(token: str) -> float | None:
    raw = normalize_text(token).lower()
    if not raw:
        return None
    clock = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", raw)
    if clock:
        h_or_m = int(clock.group(1))
        m_or_s = int(clock.group(2))
        sec_part = clock.group(3)
        if sec_part is None:
            return float(max(0, h_or_m * 60 + m_or_s))
        return float(max(0, h_or_m * 3600 + m_or_s * 60 + int(sec_part)))
    second = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:秒|s)?", raw)
    if second:
        try:
            return max(0.0, float(second.group(1)))
        except ValueError:
            return None
    return None


def infer_frame_count_hint(text: str) -> int:
    t = normalize_text(text)
    if not t:
        return 0
    m = re.search(
        r"(?:max_frames|frame_count)\s*=\s*(\d{1,2})", t, flags=re.IGNORECASE
    )
    if not m:
        m = re.search(
            r"(\d{1,2})\s*(?:screenshots?|frames?)", t, flags=re.IGNORECASE
        )
    if not m:
        m = re.search(r"(\d{1,2})\s*(?:张|幀|帧)", t, flags=re.IGNORECASE)
    if not m:
        return 0
    try:
        value = int(m.group(1))
    except ValueError:
        return 0
    return max(1, min(12, value))


def is_context_continuation_phrase(text: str) -> bool:
    t = normalize_text(text).lower()
    if not t:
        return False
    plain = _RE_WHITESPACE.sub("", t)
    explicit_tokens = ("/next", "next=1", "continue=1", "context=continue")
    if any(token in plain for token in explicit_tokens):
        return True
    if len(t) <= 16 and re.fullmatch(r"[?？!！,，.。~\-\s]*", t):
        return True
    if len(t) <= 12 and any(
        cue in t
        for cue in (
            "继续找",
            "你找",
            "找啊",
            "查啊",
            "搜啊",
            "去找",
            "那你找",
        )
    ):
        return True
    return False


def strip_continuation_prefix(text: str) -> str:
    t = normalize_text(text)
    t = re.sub(r"^(?i:/(?:next|continue))\s*[?？:：,，]?\s*", "", t)
    t = normalize_text(t)
    return t


def looks_like_reference_to_previous_link(text: str) -> bool:
    t = normalize_text(text).lower()
    if not t:
        return False
    plain = _RE_WHITESPACE.sub("", t)
    explicit_tokens = (
        "/source",
        "source=previous",
        "source=last",
        "from=previous",
        "from=last",
        "use_previous_url=1",
        "use_last_url=1",
    )
    if any(token in plain for token in explicit_tokens):
        return True
    patterns = (
        r"(?:^|\s)/source(?:\s|$)",
        r"(?:^|\s)(?:source|from)\s*=\s*(?:previous|last)(?:\s|$)",
    )
    return any(re.search(pattern, t) for pattern in patterns)


def looks_like_image_question(text: str) -> bool:
    """Weak check: does the text ask about an image?

    中文关键词匹配已移除，只接受显式控制 token。
    图片流水线由 raw_segments / URL 结构信号驱动。
    """
    t = (text or "").lower()
    # Only accept explicit control tokens
    if any(tok in t for tok in ("/analyze", "mode=analyze", "ocr=true")):
        return True
    return False


def looks_like_english_refusal_text(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    refusal_markers = (
        "i can't",
        "i cannot",
        "i can’t",
        "i'm not able",
        "i’m not able",
        "unable to",
        "cannot help with that request",
        "can't help with that request",
        "text-based ai assistant",
        "as an ai",
        "adult content",
        "sexually explicit",
        "18+",
        "nsfw",
    )
    if not any(marker in content for marker in refusal_markers):
        return False
    cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
    alpha_count = sum(1 for ch in content if ch.isalpha())
    return alpha_count > 0 and cjk_count <= 2


def sanitize_profile_summary(summary: str) -> str:
    content = normalize_text(summary)
    if not content:
        return ""
    # 避免把可识别画像统计直接喂给模型，降低隐私泄露概率。
    content = re.sub(
        r"(?:QQ号|qq号|消息数|发言数|发了\d+条消息|凌晨\d+点(?:左右)?活跃|活跃时段|作息规律)[^。；;\n]*[。；;]?",
        "",
        content,
        flags=re.IGNORECASE,
    )
    content = _RE_WHITESPACE_2PLUS.sub(" ", content).strip()
    return content


def strip_urls_and_hosts(text: str) -> str:
    stripped = _RE_URL_STRIP.sub(" ", normalize_text(text))
    stripped = _RE_BARE_WEB_HOST.sub(" ", stripped)
    stripped = re.sub(r"\[CQ:[^\]]+\]", " ", stripped)
    stripped = re.sub(r"@\S+", " ", stripped)
    stripped = _RE_PUNCTUATION_CJK.sub(" ", stripped)
    stripped = _RE_WHITESPACE.sub(" ", stripped).strip()
    return stripped


def parse_json_object_from_text(text: str) -> dict[str, Any] | None:
    raw = normalize_text(text)
    if not raw:
        return None
    block = _RE_CODE_BLOCK.search(raw)
    if block:
        raw = block.group(1).strip()
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def find_json_end(text: str) -> int | None:
    """找到第一个完整 JSON 对象的结束位置 (括号匹配)。"""
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def normalize_tool_call(data: Any) -> dict[str, Any] | None:
    """将不同格式的 tool call 统一为 {"tool": ..., "args": ...}。

    支持的格式:
    - 标准: {"tool": "...", "args": {...}}
    - OpenAI: {"name": "...", "arguments": {...}}
    - 弱模型: {"function": "...", "parameters": {...}}
    - 弱模型: {"action": "...", "input": {...}}
    """
    if not isinstance(data, dict):
        return None
    if "tool" in data:
        return data
    # OpenAI function calling 格式: {"name": "tool", "arguments": {...}}
    if "name" in data:
        return {
            "tool": data["name"],
            "args": data.get("arguments", data.get("args", data.get("parameters", {}))),
        }
    # 弱模型常见: {"function": "tool", "parameters": {...}}
    if "function" in data and isinstance(data["function"], str):
        return {
            "tool": data["function"],
            "args": data.get("parameters", data.get("arguments", data.get("args", {}))),
        }
    # 弱模型常见: {"action": "tool", "input": {...}}
    if "action" in data and isinstance(data["action"], str):
        return {
            "tool": data["action"],
            "args": data.get("input", data.get("parameters", data.get("args", {}))),
        }
    return None


def has_control_token(text: str, *tokens: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    for token in tokens:
        token_norm = normalize_text(token).lower()
        if not token_norm:
            continue
        if re.search(
            rf"(?<![a-z0-9_]){re.escape(token_norm)}(?![a-z0-9_])", content
        ):
            return True
    return False


def normalize_short_ping_phrase(text: str) -> str:
    content = normalize_text(text).lower()
    if not content:
        return ""
    content = re.sub(r"\s+", "", content)
    content = re.sub(r"[。！？!?，,、~…]+$", "", content)
    return content


def looks_like_explicit_request(text: str) -> bool:
    content = normalize_text(text)
    if not content:
        return False
    if "?" in content or "？" in content:
        return True
    if re.match(r"^[!/][a-z0-9_.:-]+", content, flags=re.IGNORECASE):
        return True
    return False


def looks_like_media_request(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    if re.search(r"https?://[^\s]+", content):
        return True
    # BV/av 号识别
    if re.search(r"(?:bv|av)\w{6,}", content, flags=re.IGNORECASE):
        return True
    return False


def looks_like_low_info_group_chitchat(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return True
    compact = re.sub(r"\s+", "", content)
    if not compact:
        return True
    if re.fullmatch(r"[?？!！。./\\,，:：;；~～\-_=+*'\"`·…]{1,12}", compact):
        return True
    return len(compact) <= 2


def looks_like_download_task_intent(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    return bool(re.search(r"\.(exe|apk|ipa|msi|zip|rar|7z)\b", content))


def looks_like_music_request(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    if re.search(r"https?://[^\s]+", content):
        return True
    if re.search(r"\.(?:mp3|wav|flac|ogg|m4a|aac)\b", content, flags=re.IGNORECASE):
        return True
    return has_control_token(text, "/music", "/song", "mode=music")


def extract_music_keyword(text: str) -> str:
    content = normalize_text(text)
    if not content:
        return ""
    content = re.sub(r"^@\S+\s*", "", content)
    content = re.sub(r"(?i)(?<!\S)/(?:music|song|search)\b", " ", content)
    content = re.sub(
        r"(?i)\b(?:mode|type|platform|source|output|target|title|artist|id)=[^\s]+",
        " ",
        content,
    )
    content = re.sub(r"\s+", " ", content).strip(
        "`\"'[](){}<>.,;:!?\uFF0C\u3002\uFF1F\uFF01\uFF1A"
    )
    return content


def extract_github_repo_from_text(text: str) -> str:
    content = normalize_text(text)
    if not content:
        return ""
    match = re.search(
        r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
        content,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    owner = match.group(1)
    repo = re.sub(r"\.git$", "", match.group(2), flags=re.IGNORECASE)
    return f"{owner}/{repo}"


def normalize_reply_echo_text(text: str) -> str:
    content = normalize_text(text).lower()
    if not content:
        return ""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", content)


def extract_local_path_candidates(text: str) -> list[str]:
    content = normalize_text(text)
    if not content:
        return []
    patterns = (
        r"[A-Za-z]:\\[^\s\"'<>|?*]+",
        r"(?:\./|\.\./|/)[^\s\"'<>|?*]+",
        r"(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10}",
        r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+",
    )
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, content):
            candidate = normalize_text(str(raw)).strip().rstrip("，。！？!?,.;:)]}")
            if not candidate:
                continue
            lower = candidate.lower()
            if lower.startswith("http://") or lower.startswith("https://"):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            out.append(candidate)
    return out


def clamp_unit_float(value: Any, default: float = 0.5) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = float(default)
    return max(0.0, min(1.0, numeric))


def mask_numeric_id(value: str) -> str:
    raw = normalize_text(value)
    if not raw:
        return ""
    if len(raw) <= 4:
        return "*" * len(raw)
    keep_tail = 3 if len(raw) >= 7 else 2
    return f"{'*' * (len(raw) - keep_tail)}{raw[-keep_tail:]}"


def strip_known_kaomoji_tokens(text: str) -> str:
    content = str(text or "")
    if not content:
        return ""
    known = ("QWQ", "AWA", "OwO", "UwU", "QAQ", ">_<", "TAT", "XD")
    for token in known:
        if re.fullmatch(r"[A-Za-z0-9_]+", token):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"
        else:
            pattern = re.escape(token)
        content = re.sub(pattern, " ", content, flags=re.IGNORECASE)
    return normalize_text(content)


def enforce_identity_claim(text: str) -> str:
    content = normalize_text(text)
    if not content:
        return ""
    # 清理模型常见越权身份拒答话术，统一身份口径
    strips = (
        r"我注意到这个请求不在我的能力范围内[^。！？]*[。！？]?",
        r"我是\s*SKIAPI[^。！？]*[。！？]?",
        r"我专注于帮助开发者[^。！？]*[。！？]?",
        r"不能扮演[^。！？]*[。！？]?",
        r"这里的对话似乎是在模拟[^。！？]*[。！？]?",
    )
    for pat in strips:
        content = re.sub(pat, "", content, flags=re.IGNORECASE)
    content = normalize_text(content)
    lower = content.lower()
    vendor_hint = bool(
        re.search(
            r"\b(openai|chatgpt|anthropic|claude|gemini|kiro|deepseek|skiapi)\b", lower
        )
    )
    assistant_claim = bool(
        re.search(
            r"(?i)\b(i am|i'm)\b.{0,32}\b(ai|assistant|model|bot|ide)\b", content
        )
        or re.search(
            r"(我是|我叫).{0,32}(ai|助手|模型|机器人|ide)",
            content,
            flags=re.IGNORECASE,
        )
    )
    if vendor_hint and assistant_claim:
        return "我是 YuKiKo。"
    if "基于 skiapi 的助手" in lower:
        return content.replace("基于 SKIAPI 的助手", "YuKiKo").strip("（）() ")
    if not content:
        return "我是 YuKiKo。"
    return content


def parse_zh_number(token: str) -> int | None:
    value = normalize_text(token)
    if not value:
        return None
    mapping = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "十" not in value:
        return mapping.get(value)
    # 十一 / 二十 / 二十三
    parts = value.split("十")
    if len(parts) != 2:
        return None
    left = parts[0].strip()
    right = parts[1].strip()
    tens = 1 if left == "" else mapping.get(left, 0)
    ones = 0 if right == "" else mapping.get(right, 0)
    if tens <= 0:
        return None
    return tens * 10 + ones


def extract_choice_index(text: str) -> int | None:
    content = normalize_text(text)
    if not content:
        return None
    compact = re.sub(r"\s+", "", content)
    unit_pattern = r"(?:\u4e2a|\u500b|\u5f20|\u5f35|\u6761|\u689d|\u53f7|\u865f)"
    direct_match = re.fullmatch(rf"([1-9]\d?){unit_pattern}?", compact)
    if direct_match:
        try:
            return int(direct_match.group(1))
        except Exception:
            return None
    ordinal_match = re.fullmatch(rf"\u7b2c([1-9]\d?){unit_pattern}", compact)
    if ordinal_match:
        try:
            return int(ordinal_match.group(1))
        except Exception:
            return None
    zh_ordinal = re.fullmatch(
        rf"\u7b2c([\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{{1,3}}){unit_pattern}",
        compact,
    )
    if zh_ordinal:
        n = parse_zh_number(zh_ordinal.group(1))
        if n is not None and n > 0:
            return n
    return None


def contains_choice_numbered_list(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    has_1 = bool(re.search(r"(?:^|\n)\s*1\s*[\.、\)]", content))
    has_2 = bool(re.search(r"(?:^|\n)\s*2\s*[\.、\)]", content))
    return has_1 and has_2


def is_fragment_continuation(text: str) -> bool:
    content = normalize_text(text)
    if not content:
        return False
    if len(content) > 42:
        return False
    return bool(re.fullmatch(r"[?？!！~～…,.，]{1,6}", content))


def is_fragment_timeout_nudge(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    return bool(re.fullmatch(r"[?？!！~～…,.，]{1,8}", content))


def user_typed_text_for_trigger(raw_text: str) -> str:
    """从**未压平**的 message.text 里精确切出用户自己打的那段字。

    按行切：app.py 的结构是确定的 —— `f"{media_event}\\n{clean_text}"`，
    `media_event` 恒为单行，语音转写另起一行 `[语音内容] xxx`。
    换行就是精确边界。

    `[语音内容]` 行**算**用户内容：语音转写就是用户说的话。
    """
    raw = str(raw_text or "")
    if not raw:
        return ""
    kept: list[str] = []
    for idx, line in enumerate(raw.split("\n")):
        stripped = line.strip()
        if not stripped:
            continue
        if idx == 0 and re.match(
            r"^MULTIMODAL_EVENT(?:_AT)?\b", stripped, flags=re.IGNORECASE
        ):
            continue
        kept.append(stripped)
    return normalize_text(" ".join(kept))
