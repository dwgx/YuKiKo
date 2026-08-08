"""重构 B5：平台双轨入口收敛回归。

覆盖：
- `_platform_primary_enabled()` 对 primary true / false / 缺失（含 parse 错误）的判定；
- `_select_entry_path()` 统一入口选择；
- `main()` 按入口分发到平台主路径（run_primary，不碰 NoneBot）或 NoneBot legacy 路径
  （nonebot.init + register_adapter + ws 补丁 + create_engine + register_handlers）。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

import main


class _FakePath:
    """最小 Path 替身：resolve/parent/__truediv__ 都返回自身，exists/read_text 可配。"""

    def __init__(self, _name: str = "") -> None:
        self._exists = True
        self._text = ""

    def resolve(self) -> _FakePath:
        return self

    @property
    def parent(self) -> _FakePath:
        return self

    def __truediv__(self, _other: object) -> _FakePath:
        return self

    def exists(self) -> bool:
        return self._exists

    def read_text(self, **_: object) -> str:
        return self._text


class PlatformPrimaryEnabledTests(unittest.TestCase):
    """`_platform_primary_enabled()` 对 platform.onebot11.primary 的判定。"""

    def _run_with_cfg(self, text: str | None) -> bool:
        fake = _FakePath()
        if text is None:
            fake._exists = False
        else:
            fake._text = text
        with mock.patch("main.Path", return_value=fake):
            return main._platform_primary_enabled()

    def test_should_enable_platform_primary_when_flag_is_true(self) -> None:
        self.assertTrue(self._run_with_cfg("platform:\n  onebot11:\n    primary: true\n"))

    def test_should_disable_when_flag_is_false(self) -> None:
        self.assertFalse(self._run_with_cfg("platform:\n  onebot11:\n    primary: false\n"))

    def test_should_disable_when_config_file_missing(self) -> None:
        self.assertFalse(self._run_with_cfg(None))

    def test_should_disable_when_platform_section_missing(self) -> None:
        self.assertFalse(self._run_with_cfg("bot:\n  name: yukiko\n"))

    def test_should_disable_when_onebot11_section_missing(self) -> None:
        self.assertFalse(self._run_with_cfg("platform:\n  other: 1\n"))

    def test_should_disable_on_yaml_parse_error(self) -> None:
        self.assertFalse(self._run_with_cfg("platform: [broken"))

    def test_should_disable_on_read_error(self) -> None:
        fake = _FakePath()
        fake.read_text = lambda **_: (_ for _ in ()).throw(OSError("boom"))
        with mock.patch("main.Path", return_value=fake):
            self.assertFalse(main._platform_primary_enabled())


class SelectEntryPathTests(unittest.TestCase):
    """`_select_entry_path()` 统一入口选择。"""

    def test_should_select_primary_when_platform_enabled(self) -> None:
        with mock.patch("main._platform_primary_enabled", return_value=True):
            self.assertEqual(main._select_entry_path(), main.ENTRY_PRIMARY)

    def test_should_select_legacy_nonebot_when_platform_disabled(self) -> None:
        with mock.patch("main._platform_primary_enabled", return_value=False):
            self.assertEqual(main._select_entry_path(), main.LEGACY_NONEBOT_PATH)


class MainEntryDispatchTests(unittest.TestCase):
    """`main()` 按入口分发：平台主路径不碰 NoneBot；legacy 路径走 NoneBot 全套。"""

    def test_should_run_primary_and_exit_zero_when_primary_selected(self) -> None:
        with (
            mock.patch("main._select_entry_path", return_value=main.ENTRY_PRIMARY),
            mock.patch("main._log_onebot_reverse_ws_hint"),
            mock.patch("main.needs_setup", return_value=False),
            mock.patch("main.sys.argv", ["main.py"]),
            mock.patch("core.platform.run_primary.run_primary") as run_primary,
        ):
            with self.assertRaises(SystemExit) as cm:
                main.main()
        self.assertEqual(cm.exception.code, 0)
        run_primary.assert_called_once_with()

    def test_should_not_enter_nonebot_boot_when_primary_selected(self) -> None:
        with (
            mock.patch("main._select_entry_path", return_value=main.ENTRY_PRIMARY),
            mock.patch("main._log_onebot_reverse_ws_hint"),
            mock.patch("main.needs_setup", return_value=False),
            mock.patch("main.sys.argv", ["main.py"]),
            mock.patch("main.nonebot.init") as nonebot_init,
            mock.patch("core.platform.run_primary.run_primary", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                main.main()
        nonebot_init.assert_not_called()

    def test_should_boot_nonebot_legacy_path_when_legacy_selected(self) -> None:
        with (
            mock.patch("main._select_entry_path", return_value=main.LEGACY_NONEBOT_PATH),
            mock.patch("main._log_onebot_reverse_ws_hint"),
            mock.patch("main.needs_setup", return_value=False),
            mock.patch("main.sys.argv", ["main.py"]),
            mock.patch("main.nonebot.init") as nonebot_init,
            mock.patch("main.nonebot.get_driver") as get_driver,
            mock.patch("main.OneBotV11Adapter") as adapter_cls,
            mock.patch("core.nonebot_ws_patch.patch_nonebot_ws_routes") as ws_patch,
            mock.patch("main.create_engine") as create_engine,
            mock.patch("main.register_handlers") as register_handlers,
            mock.patch("core.webui.init_webui") as init_webui,
            mock.patch("main.nonebot.get_asgi") as get_asgi,
            mock.patch("main.nonebot.run") as nonebot_run,
        ):
            main.main()
        nonebot_init.assert_called_once_with()
        get_driver.return_value.register_adapter.assert_called_once_with(adapter_cls)
        ws_patch.assert_called_once_with(get_driver.return_value)
        create_engine.assert_called_once_with()
        register_handlers.assert_called_once_with(create_engine.return_value)
        init_webui.assert_called_once_with(create_engine.return_value)
        get_asgi.return_value.include_router.assert_called_once_with(init_webui.return_value)
        nonebot_run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
