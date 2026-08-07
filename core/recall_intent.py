"""Phase 4b：检索双车道的车道 2 触发 —— 回顾意图检测。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1（3）：车道 1（零模型）命中弱时，
只有「显式回溯意图」才升级到车道 2（recall 子 agent / 强化检索）。本模块负责
识别这个意图，纯结构正则，不调用模型。

只识别「在回忆/回溯」的措辞，不做语义判断（不判断它到底在回忆什么）。
"""

from __future__ import annotations

import re

from utils.text import normalize_text

# 中文回顾意图：提到过去说过/记着/之前。
_RECALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:我记得|我记着|你?[是还]?说过|你?之前说|你?以前说|上次说|刚才说|"
        r"之前不是|以前不是|一开始|最开始|回想|回忆)"
    ),
    # 独立时间回溯词：以前 / 之前 / 上次 / 刚才（低成本触发，只强化检索不改行为）。
    re.compile(r"(?:以前|之前|上次|刚才)"),
    re.compile(
        r"\b(?:remember|recall|you said|you mentioned|earlier|previously|before)\b",
        re.IGNORECASE,
    ),
)


def looks_like_recall_intent(text: str) -> bool:
    """判断消息是否含「显式回溯意图」。

    命中表示用户明确在回忆/追问过去的内容，此时值得升级到车道 2 检索。
    纯结构正则，不做语义判断。
    """
    content = normalize_text(text)
    if not content:
        return False
    return any(pattern.search(content) for pattern in _RECALL_PATTERNS)
