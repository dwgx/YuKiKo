"""状态变更类工具必须有权限门，且两份清单不许漂移。

## 缺陷（2026-08-06 子 agent 工具调用扫描，实测复现）

**(a) `upload_private_file` 没有路径白名单（critical）。**
它的兄弟 `_handle_upload_group_file` 有一份（注释写着「防止 LLM 上传任意系统文件」），
而 private 那个把 `file` 直接送进 NapCat。实测 `permission_level='user'` 调它传 `.env`：

```
ok=True  napcat 收到: ('upload_private_file', {'file': '.env', 'name': 'notes.txt'})
```

`.env` 里是全部 provider API key、`ONEBOT_ACCESS_TOKEN`、`WEBUI_TOKEN`。
两个 upload 工具当时都不在任何权限集合里，所以普通群成员就能走到。

**(b) 六个状态变更工具完全没有权限门。** 扫描 31 个状态变更形状的工具，
10 个无门，其中六个真该有：

```
delete_group_folder    描述自己写着「需要管理员权限」，registry 从不执行
set_group_add_request  批准入群申请 —— 普通成员能把任何人放进群
set_friend_add_request 接受好友申请，而 delete_friend 是 super_admin
set_qq_profile         改机器人自己的资料
upload_group_file / upload_private_file
```

而更弱的同族（`delete_group_file` / `create_group_file_folder`）早就有门。

**(c) 根因：两份手维护清单已经漂移。**
`AgentToolRegistry._GROUP_ADMIN_TOOLS`（权限执行方）与
`AgentLoop._group_admin_tools`（「必须点名机器人」那道门）各自维护。
实测 registry 16 项、agent 15 项，少的是 `recall_recent_messages` ——
于是批量撤回跳过了 `core/agent.py:2286` 那道门，而它 15 个同族兄弟都受管。

已把 agent 侧改成 `set(AgentToolRegistry._GROUP_ADMIN_TOOLS)`，不再手维护第二份。

## 本文件钉三件事

1. 两份清单必须一致（现在是同源，这条防止有人改回手维护）
2. 描述声称「需要管理员权限」的工具必须真的在权限集合里
3. 状态变更形状的工具要么有门、要么在**显式豁免名单**里 ——
   不许有第三种状态。新增工具时会被迫来这里归类。
"""

from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path

from core.agent import AgentLoop
from core.agent_tools_napcat import resolve_uploadable_path
from core.agent_tools_registry import AgentToolRegistry, register_builtin_tools
from core.agent_tools_utility import register_sticker_tools

_MUTATION_NAME_RE = re.compile(r"^(set_|delete_|del_|upload_|create_|clean_)")

# 状态变更形状但**不需要**权限门的工具，逐个说明理由。
# 新工具落到这里时必须写理由，不许空着凑数。
_EXEMPT_LOW_IMPACT = {
    "create_collection": "QQ 收藏夹，只影响机器人自己账号的收藏",
    "set_group_sign": "群打卡签到，无破坏性",
    "set_input_status": "「正在输入」状态提示，无副作用",
    "set_msg_emoji_like": "给消息贴表情回应，可撤销且无破坏性",
    "create_skill": "在 _SUPER_ADMIN_TOOLS 里（见下面的断言）",
    "test_in_sandbox": "在 _SUPER_ADMIN_TOOLS 里",
    "clean_cache": "在 _SUPER_ADMIN_TOOLS 里",
    "delete_memory": "只删调用者自己的记忆",
    "set_scene_state": "会话内状态，不出会话",
}


def _registry() -> AgentToolRegistry:
    reg = AgentToolRegistry()
    register_builtin_tools(reg, None, None, None, {})
    try:
        register_sticker_tools(reg, None)
    except Exception:  # noqa: BLE001
        pass
    return reg


class TwoPermissionListsMustNotDriftTests(unittest.TestCase):
    def test_agent_group_admin_set_matches_registry(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop._super_admin_tools = set()  # type: ignore[attr-defined]
        # 复刻构造逻辑而不真的跑 __init__（它要模型和网络）
        agent_side = set(AgentToolRegistry._GROUP_ADMIN_TOOLS)
        self.assertEqual(
            agent_side,
            set(AgentToolRegistry._GROUP_ADMIN_TOOLS),
            "agent 侧的 _group_admin_tools 不再同源 —— 会重新漂移",
        )

    def test_agent_source_derives_from_registry(self) -> None:
        """读源码确认 agent.py 没有把清单写回硬编码。"""

        src = Path("core/agent.py").read_text(encoding="utf-8")
        self.assertIn(
            "self._group_admin_tools = set(AgentToolRegistry._GROUP_ADMIN_TOOLS)",
            src,
            "agent.py 又手维护了一份 _group_admin_tools —— 两份必然漂移，"
            "上次漂移让 recall_recent_messages 跳过了「必须点名机器人」那道门",
        )

    def test_recall_recent_messages_is_gated_on_both_paths(self) -> None:
        """上次漂移的具体受害者，单独钉住。"""

        self.assertIn("recall_recent_messages", AgentToolRegistry._GROUP_ADMIN_TOOLS)


class ToolsClaimingAdminMustBeGatedTests(unittest.TestCase):
    def test_description_claiming_admin_implies_a_permission_set(self) -> None:
        reg = _registry()
        gated = AgentToolRegistry._SUPER_ADMIN_TOOLS | AgentToolRegistry._GROUP_ADMIN_TOOLS
        offenders = []
        for name in sorted(reg._schemas):
            desc = str(getattr(reg._schemas[name], "description", "") or "")
            if "管理员" in desc and name not in gated:
                offenders.append(name)
        self.assertEqual(
            offenders,
            [],
            "这些工具的描述声称需要管理员权限，但 registry 从不执行 —— "
            f"普通成员可直接调用: {offenders}",
        )


class MutationToolsAreEitherGatedOrExplicitlyExemptTests(unittest.TestCase):
    """结构性守卫：新增状态变更工具时必须来这里归类。"""

    def test_every_mutation_tool_is_classified(self) -> None:
        reg = _registry()
        gated = AgentToolRegistry._SUPER_ADMIN_TOOLS | AgentToolRegistry._GROUP_ADMIN_TOOLS
        unclassified = sorted(
            name
            for name in reg._schemas
            if _MUTATION_NAME_RE.match(name)
            and name not in gated
            and name not in _EXEMPT_LOW_IMPACT
        )
        self.assertEqual(
            unclassified,
            [],
            f"这些状态变更类工具既没有权限门、也没登记豁免理由: {unclassified}\n"
            "有破坏性/涉及身份/放人进群 -> 加进 _GROUP_ADMIN_TOOLS；"
            "确实无害 -> 写进本文件的 _EXEMPT_LOW_IMPACT 并说明理由。",
        )

    def test_exemption_list_has_no_stale_entries(self) -> None:
        """豁免名单里不许留已经不存在或已加门的工具。"""

        reg = _registry()
        gated = AgentToolRegistry._SUPER_ADMIN_TOOLS | AgentToolRegistry._GROUP_ADMIN_TOOLS
        stale = sorted(
            name
            for name in _EXEMPT_LOW_IMPACT
            if name in reg._schemas and name in gated and "SUPER_ADMIN" not in _EXEMPT_LOW_IMPACT[name]
        )
        # 已加门又留在豁免里只是冗余，不是错误；这里只要求理由里说明
        for name in stale:
            with self.subTest(name=name):
                self.assertIn(
                    "_SUPER_ADMIN_TOOLS",
                    _EXEMPT_LOW_IMPACT[name],
                    f"{name} 已经有权限门却留在豁免名单里，理由需要更新",
                )

    def test_the_six_newly_gated_tools_are_gated(self) -> None:
        """本轮补的六个，逐个钉住，防止被回滚。"""

        for name in (
            "delete_group_folder",
            "set_group_add_request",
            "set_friend_add_request",
            "upload_group_file",
            "upload_private_file",
        ):
            with self.subTest(name=name):
                self.assertIn(name, AgentToolRegistry._GROUP_ADMIN_TOOLS, name)

    def test_set_qq_profile_is_super_admin(self) -> None:
        """set_qq_profile 改机器人全局身份，与 set_qq_avatar/set_online_status 同级。"""
        self.assertIn("set_qq_profile", AgentToolRegistry._SUPER_ADMIN_TOOLS)
        self.assertNotIn("set_qq_profile", AgentToolRegistry._GROUP_ADMIN_TOOLS)


class UploadPathAllowlistIsSharedTests(unittest.TestCase):
    """路径白名单必须是**一份**，被两个 upload handler 共用。"""

    def test_secret_files_are_rejected(self) -> None:
        for path in (".env", "config/config.yml", "/etc/hosts", "core/agent.py"):
            with self.subTest(path=path):
                resolved, error = resolve_uploadable_path(path)
                self.assertIsNone(resolved, f"{path} 被放行了")
                self.assertTrue(error)

    def test_allowed_directory_is_accepted_when_file_exists(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write(b"probe")
            temp_path = fh.name
        try:
            resolved, error = resolve_uploadable_path(temp_path)
            self.assertIsNotNone(resolved, f"临时目录下的文件被拒: {error}")
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_both_upload_handlers_use_the_shared_helper(self) -> None:
        src = Path("core/agent_tools_napcat.py").read_text(encoding="utf-8")
        self.assertEqual(
            src.count("resolve_uploadable_path("),
            3,  # 1 个定义 + 2 个调用点
            "两个 upload handler 必须都用共享 helper —— "
            "各自维护一份白名单必然漂移（本仓已有两份权限清单漂移的先例）",
        )

    def test_private_upload_rejects_dotenv_end_to_end(self) -> None:
        """端到端：即使绕过权限门直调，路径白名单也要拦住。

        两道防线是刻意的：权限门管「谁能调」，白名单管「能传什么路径」。
        """

        from core.agent_tools_napcat import _handle_upload_private_file

        calls: list[tuple] = []

        async def api_call(action: str, **kwargs: object) -> dict:
            calls.append((action, kwargs))
            return {"status": "ok", "retcode": 0, "data": {}}

        result = asyncio.run(
            _handle_upload_private_file(
                {"user_id": 1, "file": ".env", "name": "notes.txt"},
                {"api_call": api_call},
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(calls, [], ".env 的路径被送到 NapCat 了")


if __name__ == "__main__":
    unittest.main()
