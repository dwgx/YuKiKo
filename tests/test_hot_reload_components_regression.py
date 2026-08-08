"""E3b: 热重载接线回归测试。

钉住 commit 7be09aa 的行为：
- reload_config 重建 search/affinity/skill_registry/sticker（新对象），
  并重注册绑定旧组件的 agent 工具（read_skill 闭包重新指向新 skill_registry）；
- ModelClient / MemoryEngine 不重建（重量级保护）；
- _async_init_done 不被重置（避免下一条消息重跑 plugins.setup_all / MCP 初始化）；
- search 配置改动在 reload 后生效；
- refresh_runtime_policy_components 重建 affinity（连同 trigger/router）。

用 make_engine 构造 engine，再补 reload_config 所需的周边接线
（config_manager stub / audit / plugins stub / 临时路径）。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.audit import AuditTrail
from core.search import SearchEngine
from tests.conftest import make_engine


class RecordingToolRegistry:
    """记录 register 调用的最小 AgentToolRegistry 替身。

    真实注册表的 register 按名覆盖（_rebind_light_tool_registrations 依赖该语义），
    替身只需记录每次调用即可断言「重注册发生」。
    """

    def __init__(self) -> None:
        self.registered: list[tuple[object, object]] = []
        self.prompt_hints: list[object] = []

    def register(self, schema, handler) -> None:
        self.registered.append((schema, handler))

    def register_prompt_hint(self, hint) -> None:
        self.prompt_hints.append(hint)


def _closure_cells(func) -> set:
    return {cell.cell_contents for cell in (func.__closure__ or ())}


def _last_handler_for(registry, name):
    for schema, handler in reversed(registry.registered):
        if getattr(schema, "name", None) == name:
            return handler
    return None


def _make_reloadable_engine(tmp_path, config=None, with_tool_registry=False):
    """make_engine + reload_config 所需的补充接线（config_manager/audit/plugins/路径）。"""
    engine = make_engine(config=config)
    engine.project_root = Path(tmp_path)
    engine.config_dir = Path(tmp_path) / "config"
    engine.storage_dir = Path(tmp_path) / "storage"
    engine.audit = AuditTrail(Path(tmp_path) / "audit", enable=False)
    engine.image = None
    engine.plugins = SimpleNamespace(load=lambda global_config: None)
    if with_tool_registry:
        engine.agent_tool_registry = RecordingToolRegistry()
    # config_manager stub：reload() 成功，raw 指向 engine.config，
    # 原地改 dict 即可模拟「配置被外部修改后热重载」。
    engine.config_manager = SimpleNamespace(
        reload=lambda: (True, "ok"),
        raw=engine.config,
    )
    return engine


class HotReloadComponentsRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="yukiko-hot-reload-")

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def _tmp(self) -> Path:
        return Path(self._tmp_dir.name)

    def test_reload_rebuilds_search_affinity_skill_registry_sticker(self):
        engine = _make_reloadable_engine(self._tmp())
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        first = {
            "search": engine.search,
            "affinity": engine.affinity,
            "skill_registry": engine.skill_registry,
            "sticker": engine.sticker,
        }
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        for name, old_obj in first.items():
            self.assertIsNot(old_obj, getattr(engine, name), f"{name} 应被重建")
        self.assertIsInstance(engine.search, SearchEngine)

    def test_reload_keeps_model_client_and_memory(self):
        engine = _make_reloadable_engine(self._tmp())
        model_client, memory = engine.model_client, engine.memory
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        self.assertIs(model_client, engine.model_client, "ModelClient 不应重建")
        self.assertIs(memory, engine.memory, "MemoryEngine 不应重建")

    def test_reload_applies_changed_search_config(self):
        engine = _make_reloadable_engine(
            self._tmp(), config={"search": {"enable": True, "max_results": 12}}
        )
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        self.assertEqual(12, engine.search.max_results)
        # 模拟管理员改配置后热重载：search 应读新值（否则旧实例静默失效）
        engine.config["search"]["max_results"] = 99
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        self.assertEqual(99, engine.search.max_results)

    def test_reload_does_not_reset_async_init_done(self):
        engine = _make_reloadable_engine(self._tmp())
        self.assertTrue(engine._async_init_done)
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        self.assertTrue(engine._async_init_done, "_async_init_done 不应被重置")

    def test_refresh_runtime_policy_components_rebuilds_affinity(self):
        engine = _make_reloadable_engine(self._tmp())
        engine.reload_config()  # 先铺 personality/router（refresh 不重建 personality）
        affinity_before = engine.affinity
        trigger_before = engine.trigger
        router_before = engine.router
        model_client = engine.model_client
        engine.refresh_runtime_policy_components(reason="test")
        self.assertIsNot(affinity_before, engine.affinity, "affinity 应被重建")
        self.assertIsNot(trigger_before, engine.trigger)
        self.assertIsNot(router_before, engine.router)
        self.assertIs(model_client, engine.model_client, "ModelClient 不应重建")

    def test_reload_rebinds_read_skill_closure_to_new_skill_registry(self):
        engine = _make_reloadable_engine(self._tmp(), with_tool_registry=True)
        registry = engine.agent_tool_registry
        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        self.assertTrue(registry.registered, "重建后应重注册工具")
        self.assertTrue(registry.prompt_hints, "sticker 提示词 hint 应重注册")

        handler_1 = _last_handler_for(registry, "read_skill")
        self.assertIsNotNone(handler_1)
        skill_registry_1 = engine.skill_registry
        # 第一次 reload：read_skill 闭包绑定当前 skill_registry
        self.assertIn(skill_registry_1, _closure_cells(handler_1))

        ok, msg = engine.reload_config()
        self.assertTrue(ok, msg)
        handler_2 = _last_handler_for(registry, "read_skill")
        self.assertIsNotNone(handler_2)
        # 第二次 reload：闭包必须重绑到新 skill_registry（register 按名覆盖）
        self.assertIsNot(handler_1, handler_2, "read_skill 应被重新注册")
        self.assertIn(engine.skill_registry, _closure_cells(handler_2))
        self.assertNotIn(skill_registry_1, _closure_cells(handler_2))


if __name__ == "__main__":
    unittest.main()
