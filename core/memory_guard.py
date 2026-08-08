"""记忆写入前的注入 / 泄露 / 隐形字符扫描（对比 Hermes 的写入前防护）。

`scan_memory_content` 只做**保守**检测，避免误伤正常聊天：
- 注入特征（warning）：一小撮 prompt injection 惯用短语，命中只记录不拦截。
- 泄露特征（critical）：密钥 / 令牌形态（sk- 前缀、api_key 赋值、Bearer 令牌、
  32 位以上 hex / base64 高熵串）。命中拒绝写入。
- 隐形字符（warning）：零宽字符与异常控制字符（Cf/Cc 分类），清洗时剥离。

清洗复用 `utils.text.strip_invisible_format_chars`，不另造一套字符过滤。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from utils.text import strip_invisible_format_chars

# 零宽 / 方向控制 / 软连字符等容易被用来混淆内容的字符。
ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\u202a\u202b\u202c\u202d\u202e")

# 保守注入词表：只收录跨模型普遍使用的惯用短语，宁缺毋滥。
INJECTION_PHRASES = (
    "忽略之前",
    "ignore previous",
    "ignore all previous",
    "忘记你是",
    "忘了你是",
    "system prompt",
    "系统提示词",
)

# sk- 开头的 API 密钥（与 _redact_sensitive_content 同一形态）。
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
# api_key / apikey 赋值形态。
_API_KEY_ASSIGN = re.compile(r"\bapi[_-]?key\s*[=:]\s*[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE)
# Bearer 令牌（JWT 等，20 位以上）。
_BEARER_TOKEN = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE)
# 32 位以上连续 hex（md5/sha1/sha256 等摘要形态），要求至少含一个 a-f 字母，
# 排除纯数字长串（账号 / 流水号）。左右界用反查避免从长串中间截取。
_HEX_TOKEN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
# 32 位以上 base64 串（允许 1~2 个 = 填充），要求大小写与数字俱全，排除普通长单词。
_B64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])")


@dataclass(frozen=True, slots=True)
class MemoryIssue:
    """一条扫描发现。severity: critical（拒绝写入）/ warning（清洗后写入）。"""

    severity: str
    kind: str  # injection / leak / invisible
    detail: str


def _is_hex_key(token: str) -> bool:
    """32 位以上连续 hex，且至少含一个 a-f 字母（排除纯数字长串）。"""
    return any(ch.isalpha() for ch in token)


def _is_base64_token(token: str) -> bool:
    """32 位以上 base64 串，且大小写与数字俱全（排除普通长单词 / 纯数字）。"""
    body = token.rstrip("=")
    return any(ch.isupper() for ch in body) and any(ch.islower() for ch in body) and any(ch.isdigit() for ch in body)


def _is_invisible_char(ch: str) -> bool:
    if ch in ZERO_WIDTH_CHARS:
        return True
    category = unicodedata.category(ch)
    return category in {"Cf", "Cc"} and ch not in "\r\n\t"


def scan_memory_content(text: str) -> list[MemoryIssue]:
    """扫描记忆内容，返回全部发现（不含命中位置，保守起见只报类别）。

    空文本返回空列表；扫描不修改文本。
    """
    content = text or ""
    issues: list[MemoryIssue] = []

    lower = content.lower()
    for phrase in INJECTION_PHRASES:
        if phrase in lower:
            issues.append(MemoryIssue("warning", "injection", f"命中注入惯用短语: {phrase}"))
            break  # 一类报一条即可，避免同一句话刷屏

    if _SK_KEY.search(content):
        issues.append(MemoryIssue("critical", "leak", "疑似 API 密钥 (sk- 形态)"))
    if _API_KEY_ASSIGN.search(content):
        issues.append(MemoryIssue("critical", "leak", "疑似密钥赋值 (api_key=)"))
    if _BEARER_TOKEN.search(content):
        issues.append(MemoryIssue("critical", "leak", "疑似 Bearer 令牌"))
    hex_hit = _HEX_TOKEN.search(content)
    if hex_hit and _is_hex_key(hex_hit.group(0)):
        issues.append(MemoryIssue("critical", "leak", "疑似 32 位以上 hex 密钥"))
    b64_hit = _B64_TOKEN.search(content)
    if b64_hit and _is_base64_token(b64_hit.group(0)):
        issues.append(MemoryIssue("critical", "leak", "疑似 32 位以上 base64 令牌"))

    if any(_is_invisible_char(ch) for ch in content):
        issues.append(MemoryIssue("warning", "invisible", "含零宽 / 隐形控制字符"))

    return issues


def clean_memory_content(text: str) -> tuple[str, list[MemoryIssue]]:
    """写入前扫描并清洗：返回 (可写入文本, 全部问题)。

    有 critical 泄露时返回 ("", issues) 表示拒绝写入；
    仅 warning（注入 / 隐形字符）时剥离隐形字符后原样返回。
    """
    issues = scan_memory_content(text)
    if any(issue.severity == "critical" for issue in issues):
        return "", issues
    cleaned = strip_invisible_format_chars(text) if any(issue.kind == "invisible" for issue in issues) else text
    return cleaned, issues
