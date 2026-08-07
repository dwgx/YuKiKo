"""agent 循环检测（OpenClaw 风格）：检测 AgentLoop 工具调用时的重复空转。

两套机制：
- LoopGuard：实时检测。AgentLoop 在每次工具执行完成后 observe()，
  在发起下一次工具调用前用 veto_if_looping() 判断是否已陷入
  「同参数同结果」的连续循环，从而分级止损 token。
- PostCompactionGuard：压缩后守卫。在内存/上下文压缩之后 arm()，
  监视紧接着的工具调用，一旦窗口内连续出现 ≥3 次完全相同的三元组，
  判定为死循环并建议中止。

自包含模块，仅依赖标准库。不引入任何第三方依赖。
"""

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

DEFAULT_HISTORY_SIZE = 30
DEFAULT_WARNING = 10
DEFAULT_CRITICAL = 20
DEFAULT_CIRCUIT_BREAKER = 30
DEFAULT_POST_COMPACTION_WINDOW = 3

# 结果哈希中应被忽略的易变字段（任意嵌套深度）。
# 这些字段每次调用都会变（消息号、时间戳等），会掩盖「同语义结果」。
_VOLATILE_KEYS = frozenset({"message_id", "msg_id", "ts", "time", "timestamp", "id"})


@dataclass(frozen=True)
class ToolCallRecord:
    """一次工具调用的稳定指纹：名称 + 参数哈希 + 结果哈希。"""

    name: str
    args_hash: str
    result_hash: str


def stable_json(obj: Any) -> str:
    """将任意 JSON 可序列化对象转为稳定紧凑字符串：dict 键排序、非 ASCII 原样。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_call(name: str, args: Any) -> str:
    """对一次工具调用（名称 + 参数）做稳定哈希：同参数必然同哈希。

    格式：`{name}:{sha256(stable_json(args)).hexdigest()[:16]}`。
    """
    digest = hashlib.sha256(stable_json(args).encode("utf-8")).hexdigest()[:16]
    return f"{name}:{digest}"


def strip_volatile(obj: Any) -> Any:
    """递归剥掉结果中的易变字段，使同语义但带不同消息号/时间戳的结果哈希一致。

    dict 剥掉 _VOLATILE_KEYS 中的键并递归处理值；list 逐个递归；标量原样返回。
    """
    if isinstance(obj, dict):
        return {key: strip_volatile(value) for key, value in obj.items() if key not in _VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(value) for value in obj]
    return obj


def hash_result(result: Any) -> str:
    """对一次工具调用的结果做稳定哈希：先剥易变字段再整体哈希。"""
    return hashlib.sha256(stable_json(strip_volatile(result)).encode("utf-8")).hexdigest()


class LoopGuard:
    """实时 loop 检测：在历史窗口内统计「同参数同结果」的连续出现次数。"""

    def __init__(
        self,
        history_size: int = DEFAULT_HISTORY_SIZE,
        warning: int = DEFAULT_WARNING,
        critical: int = DEFAULT_CRITICAL,
        circuit_breaker: int = DEFAULT_CIRCUIT_BREAKER,
    ) -> None:
        self._warning = warning
        self._critical = critical
        self._circuit_breaker = circuit_breaker
        self._history: deque[ToolCallRecord] = deque(maxlen=history_size)

    def observe(self, record: ToolCallRecord) -> None:
        """记录一次已完成的工具调用。"""
        self._history.append(record)

    def no_progress_streak(self, name: str, args_hash: str) -> int:
        """从尾部倒序统计连续「完全相同的三元组」的数量。

        参考结果哈希取最近一条记录（tail）；tail 与给定 name/args_hash
        不一致则视为当前调用已改变，计数为 0。只要出现一次同参数不同结果，
        即认为产生了进展，计数立即断开。
        """
        if not self._history:
            return 0
        tail = self._history[-1]
        if tail.name != name or tail.args_hash != args_hash:
            return 0
        result_hash = tail.result_hash
        streak = 0
        for record in reversed(self._history):
            if (
                record.name == name
                and record.args_hash == args_hash
                and record.result_hash == result_hash
            ):
                streak += 1
            else:
                break
        return streak

    def veto_if_looping(self, name: str, args_hash: str) -> tuple[str, int]:
        """按连续 no-progress 次数返回等级：(level, streak)。

        streak >= circuit_breaker -> "circuit"
        streak >= critical        -> "critical"
        streak >= warning         -> "warn"
        其余                         -> "ok"
        """
        streak = self.no_progress_streak(name, args_hash)
        if streak >= self._circuit_breaker:
            return ("circuit", streak)
        if streak >= self._critical:
            return ("critical", streak)
        if streak >= self._warning:
            return ("warn", streak)
        return ("ok", streak)


class PostCompactionGuard:
    """压缩后守卫：在内存/上下文压缩后监视工具调用，检测重复空转。

    未 arm 时 observe() 恒返回 False；arm() 后窗口内连续出现 ≥3 次
    完全相同的三元组即返回 True 表示应中止当前 agent 循环。
    """

    def __init__(self, window: int = DEFAULT_POST_COMPACTION_WINDOW) -> None:
        self._window = window
        self._armed = False
        self._recent: deque[ToolCallRecord] = deque(maxlen=window)

    def arm(self) -> None:
        """开始武装：清空已有观察，从零开始计数。"""
        self._armed = True
        self._recent.clear()

    def observe(self, record: ToolCallRecord) -> bool:
        """记录一次工具调用；返回 True 表示已检测到循环、应中止。"""
        if not self._armed:
            return False
        self._recent.append(record)
        tail = self._recent[-1]
        streak = 0
        for existing in reversed(self._recent):
            if existing == tail:
                streak += 1
            else:
                break
        return streak >= 3

    def disarm(self) -> None:
        """解除武装并清空观察窗口。"""
        self._armed = False
        self._recent.clear()
