"""「这条消息别重发」和「停掉全 bot 发送」是两个决定，不能混成一个。

实测（storage/logs/yukiko.log，2026-08-05 ~ 08-06 共 27.1 小时）：

NapCat 报这个：
    ActionFailed(retcode=1200, message='EventChecker Failed: NTEvent
    serviceAndMethod:NodeIKernelMsgService/sendMsg
    ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate')

它出现 31 次，分布在多种发送通道：video(12) / upload_group_file(6) /
纯文本(8) / 无通道记录(8) —— 不是某一类媒体的问题。
同期**成功发送 215 次**，所以发送通道并没有坏。

原实现把它判成 `_is_hard_send_channel_error` → `_suspend_bot_send(seconds=120)`，
也就是一次回调歧义停掉**所有群**的发送两分钟。
原注释给的理由是「重试会导致重复刷屏」—— 那只支持「别重发」，
不支持「停掉整个 bot」。

实测代价：11 个撞上该报文的回合**零交付**（7 个 delivered=False，
4 个连 send_final 都没有）。用户提了要求、机器人算完了、什么都没收到。

现在拆成两个判定：
  _is_unretryable_send_error  -> 结果未知，别重发，但不动通道
  _is_hard_send_channel_error -> 通道真不可用（只有掉线/登录失效），停全 bot
"""

from __future__ import annotations

import unittest

from app import (
    _is_hard_send_channel_error,
    _is_payload_send_error,
    _is_transient_send_error,
)


def _unretryable(exc: Exception) -> bool:
    """延迟导入新符号。

    模块级 import 它会让未修的基线上整个文件收集失败（ImportError），
    连下面那条**不依赖新符号的纯行为断言**都跑不到，红证据就废了 ——
    「基线红是 ImportError 而不是行为断言失败」证明不了任何行为。
    """

    from app import _is_unretryable_send_error

    return _is_unretryable_send_error(exc)


class _SendError(Exception):
    pass


# 从日志里取的真实报文
_EVENTCHECKER = (
    "ActionFailed(status='failed', retcode=1200, data=None, "
    "message='EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
    "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate ', wording='')"
)
_SENDMSG_TIMEOUT = (
    "ActionFailed(message='Timeout:NTEvent "
    "serviceAndMethod:NodeIKernelMsgService/sendMsg')"
)
_KICKED = "ActionFailed(message='KickedOffline: 登录已失效，请重新登录')"


class CallbackAmbiguityDoesNotSuspendTheBotTests(unittest.TestCase):
    def test_eventchecker_failure_is_unretryable_but_not_a_channel_failure(self) -> None:
        exc = _SendError(_EVENTCHECKER)
        self.assertTrue(
            _unretryable(exc),
            "结果未知就不能重发 —— 消息可能已经发出去了，重发会重复刷屏",
        )
        self.assertFalse(
            _is_hard_send_channel_error(exc),
            "一次回调歧义不能停掉所有群的发送两分钟；同期通道成功发了 215 次",
        )

    def test_sendmsg_timeout_is_unretryable_but_not_a_channel_failure(self) -> None:
        exc = _SendError(_SENDMSG_TIMEOUT)
        self.assertTrue(_unretryable(exc))
        self.assertFalse(_is_hard_send_channel_error(exc))

    def test_callback_ambiguity_is_not_treated_as_transient(self) -> None:
        """它也不该走重试路径 —— 那才是重复刷屏的来源。"""

        for text in (_EVENTCHECKER, _SENDMSG_TIMEOUT):
            with self.subTest(text[:40]):
                self.assertFalse(_is_transient_send_error(_SendError(text)))


class RealChannelFailureStillSuspendsTests(unittest.TestCase):
    def test_kicked_offline_suspends_the_bot(self) -> None:
        """账号掉线时发什么都不会到，停全 bot 是对的。"""

        exc = _SendError(_KICKED)
        self.assertTrue(_is_hard_send_channel_error(exc))

    def test_kicked_offline_is_not_merely_unretryable(self) -> None:
        """两个判定不能都命中 —— 否则调用点的分支顺序会决定行为，很脆。"""

        exc = _SendError(_KICKED)
        self.assertFalse(_unretryable(exc))

    def test_english_kicked_offline_marker_also_works(self) -> None:
        self.assertTrue(_is_hard_send_channel_error(_SendError("KickedOffline")))


class UnrelatedErrorsAreUnaffectedTests(unittest.TestCase):
    def test_network_jitter_still_goes_to_the_retry_path(self) -> None:
        exc = _SendError("网络连接异常 connection reset by peer")
        self.assertTrue(_is_transient_send_error(exc))
        self.assertFalse(_is_hard_send_channel_error(exc))
        self.assertFalse(_unretryable(exc))

    def test_mute_and_rate_limit_are_neither_transient_nor_channel_failures(self) -> None:
        for text in ("forbidden: 禁言中", "发送频率过快", '{"result": 299}'):
            with self.subTest(text):
                exc = _SendError(text)
                self.assertFalse(_is_transient_send_error(exc))
                self.assertFalse(_is_hard_send_channel_error(exc))
                self.assertFalse(_unretryable(exc))

    def test_empty_error_matches_nothing(self) -> None:
        exc = _SendError("")
        self.assertFalse(_unretryable(exc))
        self.assertFalse(_is_hard_send_channel_error(exc))
        self.assertFalse(_is_transient_send_error(exc))
        self.assertFalse(_is_payload_send_error(exc))


class TheTwoDecisionsAreDisjointTests(unittest.TestCase):
    def test_no_error_is_both_unretryable_and_hard(self) -> None:
        """把两者判定分开的意义就在于它们不重叠。重叠了就等于没拆。"""

        samples = (
            _EVENTCHECKER,
            _SENDMSG_TIMEOUT,
            _KICKED,
            "KickedOffline",
            "登录已失效",
            "网络连接异常",
            "forbidden",
            "",
            "随便一个别的错误",
        )
        for text in samples:
            with self.subTest(text[:40]):
                exc = _SendError(text)
                both = _unretryable(exc) and _is_hard_send_channel_error(exc)
                self.assertFalse(both, f"两个判定同时命中: {text[:60]}")


if __name__ == "__main__":
    unittest.main()
