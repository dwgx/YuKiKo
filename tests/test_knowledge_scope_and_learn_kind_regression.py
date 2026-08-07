"""知识库作用域隔离 + learn_knowledge 声明式 kind 的回归。

覆盖六个线上缺陷：
1. search_knowledge 无权限过滤 → 跨用户 / 跨群泄漏（只加排序权重，照样全量返回）
2. 内容嗅探正则劫持 learn_knowledge → 存事实变成改称呼，条目静默丢弃
3. 称呼学习守卫按字面词表拒掉合法说法（带标点 / 带「大家」/ 带「666」）
4. FTS5 对中文零命中且 LIKE 兜底走不到（`rows is None` 而零命中是 `[]`）
5. `user:<id>` 混进语义变体队列 → 中文查询必然只命中「本人画像」
6. 内嵌中文 stop_words 词表（语义判断写死在代码里）
"""
from __future__ import annotations

import ast
import inspect
import re
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.knowledge import KnowledgeBase


def _make_kb(tmp: str) -> KnowledgeBase:
    return KnowledgeBase(db_path=str(Path(tmp) / "knowledge.db"))


class KnowledgeSearchByTagTests(unittest.TestCase):
    """修复 4 前半：作用域标签检索必须是显式接口，不靠 FTS 抛异常误打误撞。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kb = _make_kb(self._tmp.name)
        self.addCleanup(self.kb.close)

    def test_search_by_tag_exists_and_finds_scope_tagged_entry(self) -> None:
        from core.knowledge import KnowledgeBase as _KB

        self.assertTrue(hasattr(_KB, "search_by_tag"))
        self.kb.add(
            category="learned",
            title="喜欢的歌",
            content="10001 喜欢夜曲",
            source="chat",
            tags=["user:10001", "group:42"],
            upsert=False,
        )
        rows = self.kb.search_by_tag("user:10001", category="learned", limit=5)
        self.assertEqual([r.title for r in rows], ["喜欢的歌"])

    def test_search_by_tag_does_not_prefix_match_other_user_ids(self) -> None:
        """`user:1` 不得命中 `user:10001` —— 裸 LIKE '%user:1%' 会串号。"""
        self.kb.add(
            category="learned",
            title="别人的偏好",
            content="10001 喜欢乌龙茶",
            source="chat",
            tags=["user:10001"],
            upsert=False,
        )
        self.assertEqual(self.kb.search_by_tag("user:1", category="learned", limit=5), [])
        self.assertEqual(len(self.kb.search_by_tag("user:10001", category="learned", limit=5)), 1)

    def test_search_by_tag_rejects_sql_metacharacters_as_data(self) -> None:
        """参数化 SQL：引号 / 百分号只能当数据，不能改语义、不能报错。"""
        self.kb.add(
            category="learned",
            title="奇怪标签",
            content="内容",
            source="chat",
            tags=["user:x'y"],
            upsert=False,
        )
        self.assertEqual(len(self.kb.search_by_tag("user:x'y", category="learned", limit=5)), 1)
        self.assertEqual(self.kb.search_by_tag("' OR 1=1 --", limit=5), [])

    def test_search_by_tag_uses_parameterized_sql_only(self) -> None:
        """判据落在 AST 上：方法体内不得出现把 tag 拼进 SQL 字面量的 f-string/`%`/`+`。

        只检查真正传给 conn.execute 的那个字符串实参必须是常量。
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(KnowledgeBase.search_by_tag)))
        executes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ]
        self.assertTrue(executes, "search_by_tag 里没有 conn.execute 调用，测试锚点失效")
        for call in executes:
            sql_arg = call.args[0]
            resolved = sql_arg
            if isinstance(sql_arg, ast.Name):
                # sql = "..." 形式：找到该名字的赋值
                assigns = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == sql_arg.id for t in n.targets)
                ]
                self.assertTrue(assigns, f"找不到 {sql_arg.id} 的赋值")
                resolved = assigns[-1].value
            self.assertNotIsInstance(resolved, ast.JoinedStr)
            self.assertNotIsInstance(resolved, ast.BinOp)

    def test_search_by_tag_honours_expiry(self) -> None:
        rid = self.kb.add(
            category="trend",
            title="过期热搜",
            content="旧内容",
            source="weibo",
            tags=["user:10001"],
            ttl=1,
            upsert=False,
        )
        self.assertTrue(rid)
        conn = self.kb._get_conn()
        conn.execute("UPDATE knowledge SET expires_at=1 WHERE id=?", (rid,))
        conn.commit()
        self.assertEqual(self.kb.search_by_tag("user:10001", limit=5), [])


class ChineseSearchFallbackTests(unittest.TestCase):
    """修复 4 后半：FTS5 unicode61 对中文零命中时必须落到 LIKE 兜底。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kb = _make_kb(self._tmp.name)
        self.addCleanup(self.kb.close)
        self.kb.add(
            category="fact",
            title="申通快递",
            content="申通快递的客服电话在官网可以查到",
            source="chat",
            tags=["物流"],
            upsert=False,
        )
        self.kb.add(
            category="wiki",
            title="图灵机",
            content="图灵机是一种抽象计算模型，由艾伦·图灵提出",
            source="wikipedia",
            tags=["计算机"],
            upsert=False,
        )

    def test_fts_match_really_returns_zero_rows_for_a_chinese_substring(self) -> None:
        """先钉住前提：MATCH 对中文子串确实零命中且**不抛异常**。

        这是 `rows is None` 走不到兜底的根因。前提一变（比如换 tokenize），
        这个用例会红，提示上面的兜底注释要更新。
        """
        conn = self.kb._get_conn()
        for needle in ("申通", "图灵"):
            matched = conn.execute(
                "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?", (needle,)
            ).fetchone()[0]
            self.assertEqual(matched, 0, f"MATCH '{needle}' 竟然命中了，兜底注释的前提要更新")

    def test_chinese_substring_query_returns_the_entry(self) -> None:
        rows = self.kb.search("申通", limit=5)
        self.assertEqual([r.title for r in rows], ["申通快递"])

    def test_chinese_query_with_category_returns_the_entry(self) -> None:
        # 用「图灵」而不是「图灵机」：unicode61 把标题整段切成一个 token，
        # MATCH '图灵机' 恰好等于那个 token 所以命中，测不出兜底。
        rows = self.kb.search("图灵", category="wiki", limit=5)
        self.assertEqual([r.title for r in rows], ["图灵机"])

    def test_genuinely_absent_chinese_query_still_returns_empty(self) -> None:
        """兜底不能变成「什么都返回」。"""
        self.assertEqual(self.kb.search("哈尔滨暴雨", limit=5), [])

    def test_scope_retrieval_via_search_leaks_across_user_ids_but_search_by_tag_does_not(self) -> None:
        """为什么作用域检索必须换成显式接口，而不是继续用 search()。

        `user:<id>` 走 search() 时只能靠 tags 列的子串 LIKE 命中 —— 子串匹配串号：
        查 `user:1` 会把 `user:10001` 和 `user:1999` 的条目一起捞出来。
        search_by_tag 按标签字面量精确匹配，不串号。
        """
        for uid, drink in (("10001", "乌龙茶"), ("1999", "可乐"), ("1", "白开水")):
            self.kb.add(
                category="learned",
                title=f"偏好{uid}",
                content=f"{uid} 喜欢{drink}",
                source="chat",
                tags=[f"user:{uid}"],
                upsert=False,
            )

        leaked = self.kb.search("user:1", category="learned", limit=10)
        self.assertGreater(
            len(leaked), 1, "前提失效：search('user:1') 本应因子串 LIKE 串号捞到多个用户"
        )

        scoped = self.kb.search_by_tag("user:1", category="learned", limit=10)
        self.assertEqual([r.content for r in scoped], ["1 喜欢白开水"])


class SearchKnowledgeScopeIsolationTests(unittest.IsolatedAsyncioTestCase):
    """修复 1：作用域标签是硬边界。修复 5/6：语义变体不再夹带本人画像与内嵌词表。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kb = _make_kb(self._tmp.name)
        self.addCleanup(self.kb.close)
        # 当前用户 10001 与他人 20002 在同一个群，另一个群 99 里还有第三方条目。
        self.kb.add(
            category="learned",
            title="喜欢喝什么",
            content="10001 喜欢乌龙茶",
            source="chat",
            tags=["user:10001", "conversation:group:42", "group:42"],
            upsert=False,
        )
        self.kb.add(
            category="learned",
            title="喜欢喝什么（别人）",
            content="20002 喜欢可乐",
            source="chat",
            tags=["user:20002", "conversation:group:42", "group:42"],
            upsert=False,
        )
        self.kb.add(
            category="learned",
            title="健康情况",
            content="我有抑郁症在吃舍曲林",
            source="chat",
            tags=["user:999888777", "conversation:group:99", "group:99"],
            upsert=False,
        )
        self.kb.add(
            category="learned",
            title="称呼偏好",
            content="以后叫我小柯",
            source="chat",
            tags=["user:999888777", "conversation:group:99", "group:99"],
            upsert=False,
        )
        # 无作用域标签的公共知识：谁都能读。
        self.kb.add(
            category="fact",
            title="申通快递",
            content="申通快递的客服电话在官网可以查到",
            source="chat",
            tags=["物流"],
            upsert=False,
        )

    def _ctx(self, **over: object) -> dict[str, object]:
        ctx: dict[str, object] = {
            "knowledge_base": self.kb,
            "user_id": "10001",
            "conversation_id": "group:42",
            "group_id": 42,
            "permission_level": "user",
        }
        ctx.update(over)
        return ctx

    async def test_other_users_entries_are_dropped_not_merely_deranked(self) -> None:
        from core.agent_tools_knowledge import _handle_search_knowledge

        result = await _handle_search_knowledge({"query": "喜欢喝什么", "category": "learned"}, self._ctx())
        self.assertTrue(result.ok)
        contents = [row["content"] for row in result.data.get("results", [])]
        self.assertIn("10001 喜欢乌龙茶", contents)
        self.assertNotIn("20002 喜欢可乐", contents)
        self.assertNotIn("20002 喜欢可乐", result.display)
        self.assertEqual(result.data.get("count"), len(contents))

    async def test_cross_group_private_entries_never_reach_another_user(self) -> None:
        """线上实测的泄漏：query='user:999888777' 拿到他人的抑郁症与称呼记录。"""
        from core.agent_tools_knowledge import _handle_search_knowledge

        result = await _handle_search_knowledge({"query": "user:999888777"}, self._ctx())
        self.assertTrue(result.ok)
        blob = result.display + repr(result.data.get("results", []))
        self.assertNotIn("舍曲林", blob)
        self.assertNotIn("小柯", blob)
        self.assertGreaterEqual(int(result.data.get("dropped_out_of_scope", 0)), 1)

    async def test_super_admin_still_sees_cross_scope_entries(self) -> None:
        from core.agent_tools_knowledge import _handle_search_knowledge

        result = await _handle_search_knowledge(
            {"query": "喜欢喝什么", "category": "learned"},
            self._ctx(permission_level="super_admin"),
        )
        self.assertTrue(result.ok)
        contents = [row["content"] for row in result.data.get("results", [])]
        self.assertIn("20002 喜欢可乐", contents)

    async def test_unscoped_public_knowledge_stays_readable(self) -> None:
        """过滤不能变成「只剩自己的条目」——没有作用域标签的公共知识照常返回。"""
        from core.agent_tools_knowledge import _handle_search_knowledge

        result = await _handle_search_knowledge({"query": "申通"}, self._ctx())
        self.assertTrue(result.ok)
        contents = [row["content"] for row in result.data.get("results", [])]
        self.assertIn("申通快递的客服电话在官网可以查到", contents)

    async def test_unrelated_chinese_query_is_not_answered_with_the_users_profile(self) -> None:
        """修复 5：查「哈尔滨暴雨」不得把本人画像当成命中结果交给模型。"""
        from core.agent_tools_knowledge import _handle_search_knowledge

        self.kb.add(
            category="learned",
            title="音乐偏好",
            content="最喜欢宋岳庭-上帝为何要这样",
            source="chat",
            tags=["user:10001", "conversation:group:42", "group:42"],
            upsert=False,
        )
        result = await _handle_search_knowledge({"query": "哈尔滨暴雨"}, self._ctx())
        self.assertTrue(result.ok)
        self.assertEqual(int(result.data.get("query_matched", -1)), 0)
        self.assertTrue(result.data.get("profile_only"))
        for row in result.data.get("results", []):
            if "宋岳庭" in row["content"]:
                self.assertTrue(row["from_profile"], "画像条目必须标注来源，否则模型当成检索命中")

    async def test_query_variants_no_longer_contain_a_user_scope_probe(self) -> None:
        """修复 5：`user:<id>` 不再混进语义变体队列。"""
        from core.agent_tools_knowledge import _handle_search_knowledge

        result = await _handle_search_knowledge({"query": "喜欢喝什么"}, self._ctx())
        variants = [str(v) for v in result.data.get("query_variants", [])]
        self.assertTrue(variants)
        self.assertFalse([v for v in variants if v.startswith("user:")], variants)

    async def test_stop_word_list_is_gone_so_query_terms_survive_verbatim(self) -> None:
        """修复 6：内嵌 22 词中文 stop_words 表已删除。

        基线会把「音乐」「什么」这类词从变体里剔掉；现在只做标点归一化。
        """
        from core.agent_tools_knowledge import _handle_search_knowledge

        result = await _handle_search_knowledge({"query": "喜欢 什么 音乐"}, self._ctx())
        variants = [str(v) for v in result.data.get("query_variants", [])]
        self.assertEqual(variants, ["喜欢 什么 音乐"])

    def test_no_stop_word_literal_set_remains_in_the_source(self) -> None:
        """判据落在 AST 上：函数体内不得再有中文停用词字面量集合。"""
        import core.agent_tools_knowledge as mod

        tree = ast.parse(textwrap.dedent(inspect.getsource(mod._handle_search_knowledge)))
        literal_sets = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Set)
            and len(node.elts) >= 5
            and all(isinstance(el, ast.Constant) and isinstance(el.value, str) for el in node.elts)
        ]
        self.assertEqual(literal_sets, [], "_handle_search_knowledge 里又出现了字面量词表")


class _RecordingKB:
    """记录写入的假知识库。走 upsert_conflict_checked 那条真实路径。"""

    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.add_calls = 0

    def add(self, **kwargs: object) -> int:
        self.add_calls += 1
        return 1

    def upsert_conflict_checked(self, **kwargs: object) -> dict[str, object]:
        self.upserts.append(kwargs)
        return {"ok": True, "action": "inserted", "id": len(self.upserts)}

    @property
    def write_count(self) -> int:
        return len(self.upserts) + self.add_calls


class LearnKnowledgeDeclaredKindTests(unittest.IsolatedAsyncioTestCase):
    """修复 2：改名与入库的分流由模型声明的 kind 决定，不再嗅探内容。

    基线（内容嗅探）实测：content='我是程序员' 被 `looks_like_preferred_name_knowledge`
    判成改名请求 → 知识条目静默丢弃（写入 0 次）、用户被改口叫「程序员」，
    而模型收到 ok=True「已更新用户偏好称呼: 程序员」。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kb = _RecordingKB()

    def _memory(self):
        from core.memory import MemoryEngine

        memory = MemoryEngine(
            {"preferred_name_patterns": [r"(?:以后)?(?:叫我|喊我|称呼我)(?P<name>[^，。！？!?]{1,12})$"]},
            Path(self._tmp.name),
            global_config={"control": {"heuristic_rules_enable": True}},
        )
        self.addCleanup(memory.close)
        return memory

    def _ctx(self, memory, **over: object) -> dict[str, object]:
        ctx: dict[str, object] = {
            "knowledge_base": self.kb,
            "memory_engine": memory,
            "conversation_id": "private:u1",
            "user_id": "u1",
            "bot_id": "bot",
            "is_private": True,
            "mentioned": True,
            "explicit_bot_addressed": True,
            "config": {"bot": {"name": "YuKiKo", "nicknames": ["yukiko"]}},
        }
        ctx.update(over)
        return ctx

    async def test_a_fact_about_the_user_is_stored_and_never_renames_them(self) -> None:
        """线上原始症状：存「我是程序员」变成把用户改名叫「程序员」。"""
        from core.agent_tools_knowledge import _handle_learn_knowledge

        for content, hijacked_name in (
            ("我是程序员", "程序员"),
            ("我叫小柯", "小柯"),
            ("我是这个群的群主", "这个群的群主"),
            ("我是女生", "女生"),
        ):
            with self.subTest(content=content):
                self.kb = _RecordingKB()
                memory = self._memory()
                result = await _handle_learn_knowledge(
                    {"title": "用户画像", "content": content},
                    self._ctx(memory, message_text=content),
                )
                self.assertTrue(result.ok, result.error)
                self.assertEqual(self.kb.write_count, 1, "知识条目被静默丢弃了")
                self.assertEqual(self.kb.upserts[-1]["content"], content)
                self.assertNotEqual(
                    memory.get_preferred_name("u1", fallback_name="原名"),
                    hijacked_name,
                    "事实被劫持成了改名请求",
                )
                self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "原名")

    async def test_only_the_declared_preferred_name_kind_renames_the_user(self) -> None:
        from core.agent_tools_knowledge import _handle_learn_knowledge

        memory = self._memory()
        result = await _handle_learn_knowledge(
            {"title": "用户称呼偏好", "content": "阿背", "kind": "preferred_name"},
            self._ctx(memory, message_text="以后叫我阿背"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.kb.write_count, 0, "声明了改名却又写了一条知识")
        self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "阿背")

    async def test_declared_name_is_taken_from_content_not_from_a_regex_on_the_message(self) -> None:
        """称呼取模型写进 content 的声明值。

        基线对原文跑正则：这句原文（带标点 + 「谢谢」）抽不出名字，
        于是返回 ok=False 与「群聊称呼学习需要明确点名我…」——
        与真实原因（正则没匹配上）无关的文案。
        """
        from core.agent_tools_knowledge import _handle_learn_knowledge

        memory = self._memory()
        result = await _handle_learn_knowledge(
            {"title": "用户称呼偏好", "content": "阿背", "kind": "preferred_name"},
            self._ctx(memory, message_text="以后叫我阿背，谢谢啦"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "阿背")

    async def test_non_preferred_name_kinds_all_write_to_the_knowledge_base(self) -> None:
        """preference / music_preference / fact / correction 一律入库，不改名。"""
        from core.agent_tools_knowledge import _handle_learn_knowledge

        for kind in ("fact", "preference", "music_preference", "correction"):
            with self.subTest(kind=kind):
                self.kb = _RecordingKB()
                memory = self._memory()
                result = await _handle_learn_knowledge(
                    # content 刻意写成基线一定会误判成改名的句子。
                    {"title": "用户画像", "content": "以后叫我阿背", "kind": kind},
                    self._ctx(memory, message_text="以后叫我阿背"),
                )
                self.assertTrue(result.ok, result.error)
                self.assertEqual(self.kb.write_count, 1)
                self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "原名")

    async def test_correction_kind_implies_is_correction_on_the_write(self) -> None:
        """与 core/knowledge_updater.py 的 `is_correction or kind == 'correction'` 对齐。"""
        from core.agent_tools_knowledge import _handle_learn_knowledge

        memory = self._memory()
        await _handle_learn_knowledge(
            {"title": "群主生日", "content": "群主生日是 3 月 6 日", "kind": "correction"},
            self._ctx(memory, message_text="不对，群主生日是 3 月 6 日"),
        )
        self.assertTrue(self.kb.upserts, "没有写入")
        self.assertTrue(self.kb.upserts[-1]["mark_correction"])

    async def test_an_unknown_kind_degrades_to_a_plain_write_not_a_rename(self) -> None:
        from core.agent_tools_knowledge import _handle_learn_knowledge

        memory = self._memory()
        result = await _handle_learn_knowledge(
            {"title": "用户画像", "content": "以后叫我阿背", "kind": "nickname"},
            self._ctx(memory, message_text="以后叫我阿背"),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.kb.write_count, 1)
        self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "原名")

    async def test_every_legitimate_phrasing_renames_through_the_real_tool_path(self) -> None:
        """修复 3 的行为判据，走 learn_knowledge 而不是直接调守卫。

        这里刻意不传 `declared_name` 关键字 —— 那样基线只会抛
        `TypeError: unexpected keyword argument`，证明不了行为。改成模型声明
        kind='preferred_name' + content='阿背'，原文放进 message_text：
        基线对 message_text 跑正则，13 句里 8 句抽不出名字或被词表拦下，
        返回 ok=False +「群聊称呼学习需要明确点名我…」；修复后 13 句全部改名成功。
        """
        from core.agent_tools_knowledge import _handle_learn_knowledge

        phrasings = [
            "以后叫我阿背",
            "以后叫我阿背。",
            "以后叫我阿背！",
            "以后叫我阿背，谢谢",
            "记住，以后叫我阿背",
            "以后大家叫我阿背",
            "以后都叫我阿背",
            "以后叫我阿背 666",
            "哈哈以后叫我阿背",
            "叫我阿背吧",
            "我的名字是阿背",
            "称呼我阿背",
            "@YuKiKo 以后叫我阿背好不好",
        ]
        for text in phrasings:
            with self.subTest(text=text):
                self.kb = _RecordingKB()
                memory = self._memory()
                result = await _handle_learn_knowledge(
                    {"title": "用户称呼偏好", "content": "阿背", "kind": "preferred_name"},
                    self._ctx(
                        memory,
                        conversation_id="group:1",
                        is_private=False,
                        reply_to_user_id="bot",
                        message_text=text,
                    ),
                )
                self.assertTrue(result.ok, f"{text} -> {result.error} / {result.display}")
                self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "阿背")

    async def test_a_roleplay_name_is_the_models_call_in_group_and_private_alike(self) -> None:
        """基线：「老婆」在群聊被 _GROUP_ROLEPLAY_NAMES 拦下、私聊完全畅通。"""
        from core.agent_tools_knowledge import _handle_learn_knowledge

        for is_private in (False, True):
            with self.subTest(is_private=is_private):
                self.kb = _RecordingKB()
                memory = self._memory()
                result = await _handle_learn_knowledge(
                    {"title": "用户称呼偏好", "content": "老婆", "kind": "preferred_name"},
                    self._ctx(
                        memory,
                        conversation_id="group:1" if not is_private else "private:u1",
                        is_private=is_private,
                        reply_to_user_id="bot",
                        message_text="以后叫我老婆",
                    ),
                )
                self.assertTrue(result.ok, result.error)
                self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "老婆")

    async def test_structural_refusals_still_reach_the_tool_caller(self) -> None:
        """结构事实仍然拦得住：群聊里 @ 了别人 / 回复对象不是 bot / 没指向 bot。"""
        from core.agent_tools_knowledge import _handle_learn_knowledge

        cases = [
            ("at_other", {"at_other_user_ids": ["999"], "reply_to_user_id": ""}),
            ("reply_other", {"at_other_user_ids": [], "reply_to_user_id": "777"}),
            (
                "not_directed",
                {
                    "at_other_user_ids": [],
                    "reply_to_user_id": "",
                    "mentioned": False,
                    "explicit_bot_addressed": False,
                },
            ),
        ]
        for label, over in cases:
            with self.subTest(label=label):
                self.kb = _RecordingKB()
                memory = self._memory()
                result = await _handle_learn_knowledge(
                    {"title": "用户称呼偏好", "content": "阿背", "kind": "preferred_name"},
                    self._ctx(
                        memory,
                        conversation_id="group:1",
                        is_private=False,
                        message_text="以后叫我阿背",
                        **over,
                    ),
                )
                self.assertFalse(result.ok)
                self.assertEqual(memory.get_preferred_name("u1", fallback_name="原名"), "原名")

    def test_kind_enum_is_declared_in_the_schema(self) -> None:
        from core.agent_tools import AgentToolRegistry, register_builtin_tools

        registry = AgentToolRegistry()
        register_builtin_tools(registry, None, None, None, {})
        schema = registry.get_schema("learn_knowledge")
        self.assertIsNotNone(schema)
        kind = schema.parameters["properties"]["kind"]
        self.assertEqual(
            set(kind["enum"]),
            {"fact", "preference", "music_preference", "preferred_name", "correction"},
        )

    def test_kind_enum_matches_the_extractor_enum_verbatim(self) -> None:
        """两条入库路径对同一个词的理解必须一致。

        判据落在 AST 上：从 core/knowledge_updater.py 的抽取器提示字符串里取那段
        `kind":"a|b|c` 字面量，与 learn_knowledge schema 的 enum 逐项比对。
        """
        import core.agent_tools_knowledge as atk
        import core.knowledge_updater as ku

        extractor_kinds: set[str] = set()
        for node in ast.walk(ast.parse(Path(ku.__file__).read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            match = re.search(r'"kind"\s*:\s*"([a-z_|]+)"', node.value)
            if match:
                extractor_kinds = set(match.group(1).split("|"))
                break
        self.assertTrue(extractor_kinds, "没能从抽取器提示里解析出 kind 枚举")
        self.assertEqual(extractor_kinds, set(atk._LEARN_KNOWLEDGE_KINDS))

    def test_the_content_sniffing_dispatch_is_gone_from_the_handler(self) -> None:
        """判据落在 AST 调用节点上，不是源码子串（子串会匹配到本文件与注释）。"""
        import core.agent_tools_knowledge as atk

        tree = ast.parse(textwrap.dedent(inspect.getsource(atk._handle_learn_knowledge)))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("looks_like_preferred_name_knowledge", called)


class PreferredNameGuardStructureOnlyTests(unittest.TestCase):
    """修复 3：守卫只留结构约束，词表判断删除。

    基线实测：群聊已 @ bot 的 13 种合法说法只放过 5 种（带标点的、带「大家」的、
    带「666」的全被拒），而同一句「以后叫我老婆」群聊拦、私聊放 —— 既拦错也放错。
    """

    def _assess(self, text: str, **over: object):
        from utils.learning_guard import assess_preferred_name_learning

        kwargs: dict[str, object] = {
            "is_private": False,
            "mentioned": True,
            "explicit_bot_addressed": True,
            "bot_aliases": ["YuKiKo", "yukiko"],
            "at_other_user_ids": [],
            "reply_to_user_id": "bot",
            "bot_id": "bot",
        }
        kwargs.update(over)
        return assess_preferred_name_learning(text, **kwargs)  # type: ignore[arg-type]

    def test_every_legitimate_phrasing_passes_once_the_model_declares_the_name(self) -> None:
        phrasings = [
            "以后叫我阿背",
            "以后叫我阿背。",
            "以后叫我阿背！",
            "以后叫我阿背，谢谢",
            "记住，以后叫我阿背",
            "以后大家叫我阿背",
            "以后都叫我阿背",
            "以后叫我阿背 666",
            "哈哈以后叫我阿背",
            "叫我阿背吧",
            "我的名字是阿背",
            "称呼我阿背",
        ]
        for text in phrasings:
            with self.subTest(text=text):
                decision = self._assess(text, declared_name="阿背")
                self.assertTrue(decision.allow, decision.reason)
                self.assertEqual(decision.candidate, "阿背")

    def test_group_and_private_agree_now_that_the_roleplay_name_table_is_gone(self) -> None:
        """「以后叫我老婆」原来群聊被词表拦、私聊畅通。合不合适归模型，不归词表。"""
        self.assertTrue(self._assess("以后叫我老婆", declared_name="老婆").allow)
        self.assertTrue(
            self._assess("以后叫我老婆", is_private=True, reply_to_user_id="", declared_name="老婆").allow
        )

    def test_structural_constraints_are_kept(self) -> None:
        """这几条是结构事实（@了谁 / 回复对象 / 是否指向 bot），必须继续拦。"""
        self.assertFalse(
            self._assess("以后叫我阿背", at_other_user_ids=["999"], declared_name="阿背").allow
        )
        self.assertFalse(
            self._assess("以后叫我阿背", reply_to_user_id="777", declared_name="阿背").allow
        )
        self.assertFalse(
            self._assess(
                "以后叫我阿背",
                mentioned=False,
                explicit_bot_addressed=False,
                reply_to_user_id="",
                declared_name="阿背",
            ).allow
        )

    def test_a_declared_name_still_gets_structural_hygiene(self) -> None:
        for bad in ("", "   ", "阿" * 40, "‮⁦"):
            with self.subTest(bad=bad):
                self.assertFalse(self._assess("随便一句", declared_name=bad).allow)
        self.assertEqual(self._assess("随便一句", declared_name=" 「阿背」 ").candidate, "阿背")

    def test_the_deleted_word_tables_do_not_come_back(self) -> None:
        """判据落在模块属性与 AST 上，不是源码子串（注释里还提着这些名字）。"""
        import utils.learning_guard as guard

        for name in (
            "_PREFERRED_NAME_TITLE_CUES",
            "_NON_SERIOUS_TEXT_CUES",
            "_COLLECTIVE_NAME_CUES",
            "_GROUP_ROLEPLAY_NAMES",
            "looks_like_non_serious_name_context",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(guard, name), f"{name} 又回来了")

        tree = ast.parse(textwrap.dedent(inspect.getsource(guard.assess_preferred_name_learning)))
        self.assertEqual(
            [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Set, ast.Tuple, ast.List))
                and len(node.elts) >= 3
                and all(isinstance(el, ast.Constant) and isinstance(el.value, str) for el in node.elts)
            ],
            [],
            "assess_preferred_name_learning 里又出现了字面量词表",
        )

    def test_the_import_compat_shim_survives_but_never_dispatches(self) -> None:
        """八个 agent_tools_*.py 仍 import 这个符号；删掉会让 import 悬空。"""
        from utils.learning_guard import looks_like_preferred_name_knowledge

        self.assertFalse(looks_like_preferred_name_knowledge("用户称呼偏好", "以后叫我阿背", ["preferred_name"]))

    def test_the_legacy_regex_fallback_still_serves_callers_without_a_declaration(self) -> None:
        """core/memory.py 与 core/knowledge_updater.py 尚未传声明，兜底不能断。"""
        decision = self._assess("以后叫我阿背")
        self.assertTrue(decision.allow, decision.reason)
        self.assertEqual(decision.candidate, "阿背")


if __name__ == "__main__":
    unittest.main()
