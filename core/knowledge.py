"""独立知识库 — 与记忆库分离的持久化知识存储。

记忆库 (memory.py): 对话历史、用户画像、短期上下文
知识库 (knowledge.py): 事实知识、热梗、百科、学习到的概念

特性:
- SQLite + FTS5 全文检索
- 分类与 TTL
- category+title 维度去重/upsert
- 冲突更新版本表 knowledge_versions
- 自净化：读时衰减重排（access_count/last_used_at）、写时质量门（重复/矛盾/陈旧）
  自净化的取舍原则：**降权与取代允许，销毁历史不允许**。
  重复不删旧条目（记 alias）、矛盾不覆盖高置信旧值（挂 disputed）、
  陈旧只标记不淘汰。任何真删（只剩 cleanup_expired）必须先存版本快照 + 写审计流。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.text import normalize_text

from core.audit import STREAM_KNOWLEDGE

_db_local = threading.local()
_log = logging.getLogger("yukiko.knowledge")


# ── 分类常量 ──
CATEGORY_FACT = "fact"        # 事实知识 (永久)
CATEGORY_MEME = "meme"        # 热梗/流行语 (30天TTL)
CATEGORY_WIKI = "wiki"        # 百科知识 (永久)
CATEGORY_TREND = "trend"      # 热搜快照 (1天TTL)
CATEGORY_LEARNED = "learned"  # 从对话中学到的 (永久)

_DEFAULT_TTL = {
    CATEGORY_FACT: 0,       # 永久
    CATEGORY_MEME: 2592000, # 30天
    CATEGORY_WIKI: 0,       # 永久
    CATEGORY_TREND: 86400,  # 1天
    CATEGORY_LEARNED: 0,    # 永久
}

# schema 版本，走 PRAGMA user_version。加列只能 ALTER TABLE ADD COLUMN（追加到末尾），
# 因此 _row_to_entry 一律按列名取值而不是位置索引，见该函数注释。
_SCHEMA_VERSION = 1

# 没带置信度写进来的条目（add() 的裸调用：lookup_wiki / batch_add_trends / 热搜）
# 给一个中性基线。取 0.5 是因为它低于 knowledge_updater 的 min_confidence 默认 0.62：
# 经过抽取器质量门的条目理应排在裸写入之前。
# 没有这个基线，衰减公式对绝大多数条目恒等于 0（confidence*decay*reinforcement），
# 也就是「接线了但不起作用」。
_DEFAULT_CONFIDENCE = 0.5

# 衰减 + 强化：直接复用 core/memory.py:568-570 的 knowledge_search 模型
# （本仓唯一已实现的真·衰减+强化公式，那张表恒空所以从未生效）。
# 这里不发明第二套公式，只是把同一套接到真实在用的读路径上。
_DECAY_PER_DAY = 0.005      # 约 200 天衰减到下限
_DECAY_FLOOR = 0.1
_REINFORCE_PER_HIT = 0.1
_REINFORCE_CEIL = 2.0

# 矛盾判据：新值置信度低于旧值的这个比例时不覆盖，挂 disputed 等模型/人工裁决。
_CONTRADICTION_MARGIN = 0.9

# 陈旧判据：从未被召回过、且写入已超过这个天数 → 标记 stale（只标记，不淘汰）。
_STALE_AFTER_DAYS = 90.0


@dataclass(slots=True)
class KnowledgeEntry:
    """知识条目。"""
    id: int = 0
    category: str = ""
    title: str = ""
    content: str = ""
    source: str = ""       # zhihu / baike / wikipedia / weibo / chat / manual
    tags: list[str] = field(default_factory=list)
    created_at: float = 0
    expires_at: float = 0  # 0 = 永不过期
    extra: dict[str, Any] = field(default_factory=dict)
    access_count: int = 0   # 被召回次数，喂强化项
    last_used_at: float = 0 # 最近一次被召回的时间戳，0 = 从未


class KnowledgeBase:
    """独立知识库，SQLite + FTS5 全文搜索。"""

    def __init__(self, db_path: str = "storage/knowledge/knowledge.db", audit: Any = None):
        self._db_path = Path(db_path)
        if not self._db_path.is_absolute():
            self._db_path = (Path(__file__).resolve().parents[1] / self._db_path).resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        # 审计流由 engine 注入（与 AdminEngine 同一范式，core/admin.py:180 有说明）：
        # 同进程两个 AuditTrail 实例各持一把锁，写同一文件不互斥。
        # 注入 None 时所有埋点静默跳过 —— 审计缺失不能变成功能缺失。
        self._audit = audit
        self._fts_enabled = True
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """按 (线程, db_path) 取连接。

        原实现只按线程缓存（`_db_local.conn`），而 `_db_local` 是**模块级**的：
        同一线程里构造第二个 KnowledgeBase 会直接复用第一个的连接，
        于是所有读写都静默落到**第一个库**上。engine 只建一个实例所以线上没暴露，
        但迁移/备份脚本与测试里一开第二个库就会静默写错库。
        """
        conns = getattr(_db_local, "conns", None)
        if conns is None:
            conns = {}
            _db_local.conns = conns
        key = str(self._db_path)
        conn = conns.get(key)
        if conn is None:
            conn = sqlite3.connect(key, timeout=30.0)
            # 按列名取值是加列的前提：ALTER TABLE 追加列后 SELECT * 的元组会变长，
            # 位置索引取列会静默丢字段（vision-knowledge-base.md R1）。
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conns[key] = conn
        return conn

    def record_audit(self, event: str, **fields: Any) -> None:
        """写一条 knowledge 审计记录。audit 未注入时静默跳过；AuditTrail.write 自身不抛异常。"""
        if self._audit is None:
            return
        writer = getattr(self._audit, "write", None)
        if not callable(writer):
            return
        writer(STREAM_KNOWLEDGE, event, **fields)

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0,
                extra TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
            CREATE INDEX IF NOT EXISTS idx_knowledge_title ON knowledge(title);
            CREATE INDEX IF NOT EXISTS idx_knowledge_expires ON knowledge(expires_at);
            CREATE TABLE IF NOT EXISTS knowledge_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                extra TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                replaced_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_versions_kid ON knowledge_versions(knowledge_id);
        """)
        # FTS5 全文搜索索引
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    title, content, tags,
                    content='knowledge',
                    content_rowid='id',
                    tokenize='unicode61'
                )
            """)
        except sqlite3.OperationalError:
            self._fts_enabled = False
            _log.warning("FTS5 not available, falling back to LIKE search")
        self._migrate_schema(conn)
        conn.commit()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """按 PRAGMA user_version 阶梯加列。

        CREATE TABLE IF NOT EXISTS 对已存在的老库是静默无操作，所以新列只能在这里补。
        ALTER TABLE ADD COLUMN 总是追加到末尾，配合 _row_to_entry 的按列名取值即可安全。
        """
        row = conn.execute("PRAGMA user_version").fetchone()
        current = int((row[0] if row else 0) or 0)
        if current >= _SCHEMA_VERSION:
            return

        existing = {str(r["name"]) for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()}
        # v1：自净化所需的召回计数与最近召回时间。没有这两列就没有陈旧判据。
        for column, ddl in (
            ("access_count", "ALTER TABLE knowledge ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"),
            ("last_used_at", "ALTER TABLE knowledge ADD COLUMN last_used_at REAL NOT NULL DEFAULT 0"),
        ):
            if column in existing:
                continue
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                # 并发或重复迁移；下一行 PRAGMA 仍会把版本推进，重跑是幂等的。
                _log.warning("knowledge_migrate_add_column_failed | column=%s", column, exc_info=True)
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        _log.info("knowledge_schema_migrated | from=%d | to=%d", current, _SCHEMA_VERSION)

    def add(
        self,
        category: str,
        title: str,
        content: str = "",
        source: str = "",
        tags: list[str] | None = None,
        ttl: int | None = None,
        extra: dict[str, Any] | None = None,
        upsert: bool = True,
    ) -> int:
        """添加知识条目。upsert=True 时同 category+title 会更新。"""
        conn = self._get_conn()
        now = time.time()
        if ttl is None:
            ttl = _DEFAULT_TTL.get(category, 0)
        expires_at = (now + ttl) if ttl > 0 else 0
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        # 更新分支只合并调用方真的给了的键：若在这里注入默认置信度，
        # 一次不带 extra 的 add() 就会把老条目的 0.9 降成 0.5（_merge_extra 是新覆盖旧）。
        update_extra_json = json.dumps(extra or {}, ensure_ascii=False)
        # 插入分支必须带置信度，否则 _entry_rank 的衰减项恒等于 0：
        # confidence*decay*reinforcement 里 confidence 缺失就当 0，整个公式失效。
        # upsert_conflict_checked 传进来的显式 confidence 不被覆盖（setdefault）。
        insert_extra = dict(extra or {})
        insert_extra.setdefault("confidence", _DEFAULT_CONFIDENCE)
        extra_json = json.dumps(insert_extra, ensure_ascii=False)

        if upsert:
            # 检查是否已存在
            row = conn.execute(
                "SELECT id, content, source, tags, extra, created_at FROM knowledge WHERE category=? AND title=?",
                (category, title),
            ).fetchone()
            if row:
                previous_content = normalize_text(str(row[1] or ""))
                next_content = normalize_text(str(content or ""))
                if previous_content != next_content:
                    self._record_version_snapshot(
                        conn=conn,
                        knowledge_id=int(row[0]),
                        category=category,
                        title=title,
                        content=str(row[1] or ""),
                        source=str(row[2] or ""),
                        tags=str(row[3] or "[]"),
                        extra=str(row[4] or "{}"),
                        created_at=float(row[5] or now),
                        replaced_at=now,
                    )
                conn.execute(
                    "UPDATE knowledge SET content=?, source=?, tags=?, expires_at=?, extra=? WHERE id=?",
                    (
                        content,
                        source,
                        tags_json,
                        expires_at,
                        json.dumps(
                            self._merge_extra(
                                self._safe_json_dict(str(row[4] or "{}")),
                                self._safe_json_dict(update_extra_json),
                            ),
                            ensure_ascii=False,
                        ),
                        row[0],
                    ),
                )
                self._update_fts(conn, row[0], title, content, tags_json)
                conn.commit()
                return row[0]

        cursor = conn.execute(
            "INSERT INTO knowledge (category, title, content, source, tags, created_at, expires_at, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (category, title, content, source, tags_json, now, expires_at, extra_json),
        )
        row_id = cursor.lastrowid or 0
        self._insert_fts(conn, row_id, title, content, tags_json)
        conn.commit()
        return row_id

    def upsert_conflict_checked(
        self,
        category: str,
        title: str,
        content: str,
        source: str = "",
        tags: list[str] | None = None,
        extra: dict[str, Any] | None = None,
        confidence: float = 0.7,
        update_mode: str = "auto",
        mark_correction: bool = False,
    ) -> dict[str, Any]:
        """Upsert with conflict detection + versioning metadata."""
        conn = self._get_conn()
        now = time.time()
        title_n = normalize_text(title)
        content_n = normalize_text(content)
        if not title_n or not content_n:
            return {"ok": False, "action": "skipped", "reason": "empty"}

        row = conn.execute(
            "SELECT id, content, source, tags, extra, created_at FROM knowledge WHERE category=? AND title=?",
            (category, title_n),
        ).fetchone()

        payload_extra = dict(extra or {})
        payload_extra["confidence"] = max(0.0, min(1.0, float(confidence)))
        payload_extra["update_mode"] = normalize_text(update_mode) or "auto"
        payload_extra["is_correction"] = bool(mark_correction)
        payload_extra["updated_at"] = now

        tags_json = json.dumps(tags or [], ensure_ascii=False)

        if row is None:
            # 质量门 1（去重）：同分类下已有一条内容完全相同、只是标题不同的条目。
            # 不新增第二行、也不删旧行 —— 把新标题记成 alias，返回既有 id。
            dup = self._find_content_duplicate(conn, category=category, content=content_n)
            if dup is not None:
                dup_id = int(dup["id"])
                self._register_alias(conn, entry_id=dup_id, alias=title_n, extra_raw=str(dup["extra"] or "{}"))
                conn.commit()
                self.record_audit(
                    "knowledge_duplicate_merged",
                    knowledge_id=dup_id,
                    category=category,
                    kept_title=str(dup["title"] or ""),
                    alias_title=title_n,
                    source=normalize_text(source),
                    update_mode=payload_extra["update_mode"],
                )
                return {"ok": True, "action": "duplicate", "id": dup_id, "updated": False}
            new_id = self.add(
                category=category,
                title=title_n,
                content=content_n,
                source=source,
                tags=tags or [],
                extra=payload_extra,
                upsert=False,
            )
            return {"ok": True, "action": "inserted", "id": new_id, "updated": False}

        kid = int(row[0])
        old_content = normalize_text(str(row[1] or ""))
        old_extra_raw = str(row[4] or "{}")
        try:
            old_extra = json.loads(old_extra_raw)
        except Exception:
            _log.warning("knowledge_parse_extra_error | kid=%s", row[0], exc_info=True)
            old_extra = {}

        # 质量门 2（矛盾检测）：同 category+title 但内容不同 = 两个互斥的值。
        # 原行为是盲目后写胜出（判据只有 old_content != content_n），
        # confidence 只被写进 extra、从不参与写入决策。
        # 现在：显式更正照旧覆盖；否则新值置信度明显低于旧值时**不覆盖**，
        # 把落选值挂进 extra["disputed"] 等模型或人工裁决。旧内容与历史一律不动。
        old_confidence = float(old_extra.get("confidence", _DEFAULT_CONFIDENCE) or 0.0)
        new_confidence = float(payload_extra["confidence"])
        if (
            old_content != content_n
            and not mark_correction
            and new_confidence < old_confidence * _CONTRADICTION_MARGIN
        ):
            disputed_extra = dict(old_extra)
            disputed = list(disputed_extra.get("disputed") or [])
            disputed.append(
                {
                    "content": content_n,
                    "confidence": new_confidence,
                    "source": normalize_text(source),
                    "update_mode": payload_extra["update_mode"],
                    "at": now,
                }
            )
            # 只留最近若干条，避免同一条目被反复写坏时 extra 无限膨胀。
            disputed_extra["disputed"] = disputed[-5:]
            conn.execute(
                "UPDATE knowledge SET extra=? WHERE id=?",
                (json.dumps(disputed_extra, ensure_ascii=False), kid),
            )
            conn.commit()
            self.record_audit(
                "knowledge_contradiction_held",
                knowledge_id=kid,
                category=category,
                title=title_n,
                kept_confidence=old_confidence,
                rejected_confidence=new_confidence,
                rejected_content=content_n,
                source=normalize_text(source),
            )
            return {"ok": True, "action": "disputed", "id": kid, "updated": False}

        merged_extra = self._merge_extra(old_extra, payload_extra)
        action = "noop"
        if old_content != content_n:
            self._record_version_snapshot(
                conn=conn,
                knowledge_id=kid,
                category=category,
                title=title_n,
                content=str(row[1] or ""),
                source=str(row[2] or ""),
                tags=str(row[3] or "[]"),
                extra=old_extra_raw,
                created_at=float(row[5] or now),
                replaced_at=now,
            )
            conn.execute(
                "UPDATE knowledge SET content=?, source=?, tags=?, extra=?, expires_at=? WHERE id=?",
                (
                    content_n,
                    source or str(row[2] or ""),
                    tags_json if tags is not None else str(row[3] or "[]"),
                    json.dumps(merged_extra, ensure_ascii=False),
                    0,
                    kid,
                ),
            )
            self._update_fts(conn, kid, title_n, content_n, tags_json if tags is not None else str(row[3] or "[]"))
            action = "updated"
            # 取代不是销毁：旧内容已进 knowledge_versions，这里留一条可按字段查的痕迹。
            self.record_audit(
                "knowledge_superseded",
                knowledge_id=kid,
                category=category,
                title=title_n,
                old_confidence=old_confidence,
                new_confidence=new_confidence,
                is_correction=bool(mark_correction),
                source=normalize_text(source),
                update_mode=payload_extra["update_mode"],
            )
        else:
            # 内容未变化也更新元信息（置信度/来源等）。
            conn.execute(
                "UPDATE knowledge SET source=?, tags=?, extra=? WHERE id=?",
                (
                    source or str(row[2] or ""),
                    tags_json if tags is not None else str(row[3] or "[]"),
                    json.dumps(merged_extra, ensure_ascii=False),
                    kid,
                ),
            )
            action = "noop"
        conn.commit()
        return {"ok": True, "action": action, "id": kid, "updated": action == "updated"}

    def search(self, query: str, category: str = "", limit: int = 10) -> list[KnowledgeEntry]:
        """搜索知识库 (FTS5 优先，LIKE 兜底)。"""
        conn = self._get_conn()
        now = time.time()
        rows: list[Any] | None = None

        # 尝试 FTS5
        try:
            if category:
                sql = (
                    "SELECT k.* FROM knowledge k "
                    "JOIN knowledge_fts f ON k.id = f.rowid "
                    "WHERE knowledge_fts MATCH ? AND k.category=? "
                    "AND (k.expires_at=0 OR k.expires_at>?) "
                    "ORDER BY rank LIMIT ?"
                )
                rows = conn.execute(sql, (query, category, now, limit)).fetchall()
            else:
                sql = (
                    "SELECT k.* FROM knowledge k "
                    "JOIN knowledge_fts f ON k.id = f.rowid "
                    "WHERE knowledge_fts MATCH ? "
                    "AND (k.expires_at=0 OR k.expires_at>?) "
                    "ORDER BY rank LIMIT ?"
                )
                rows = conn.execute(sql, (query, now, limit)).fetchall()
        except sqlite3.OperationalError:
            # MATCH 语法/索引不可用 → LIKE 兜底。注意 touch 与重排必须留在 try 外，
            # 否则它们抛的 OperationalError 会被误判成「FTS 不可用」而重跑一次全表 LIKE。
            rows = None

        if not rows:
            # LIKE 兜底。条件是 `not rows` 而不是 `rows is None`：
            # 建表用 tokenize='unicode61'，它不切 CJK，整段连续汉字算 1 个 token，
            # 于是中文查询在 FTS 上**零命中而不报错** —— rows 是 []，不是 None。
            # 原来的 `rows is None` 因此走不到兜底，直接返回空。
            # 实测 19 条真实中文查询 12 条零命中（MATCH '申通'=0，LIKE '%申通%'=2）。
            # tokenize 换 trigram 是更大的改动，这里只补兜底条件。
            like_q = f"%{query}%"
            if category:
                sql = (
                    "SELECT * FROM knowledge WHERE category=? "
                    "AND (title LIKE ? OR content LIKE ? OR tags LIKE ?) "
                    "AND (expires_at=0 OR expires_at>?) "
                    "ORDER BY created_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, (category, like_q, like_q, like_q, now, limit)).fetchall()
            else:
                sql = (
                    "SELECT * FROM knowledge "
                    "WHERE (title LIKE ? OR content LIKE ? OR tags LIKE ?) "
                    "AND (expires_at=0 OR expires_at>?) "
                    "ORDER BY created_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, (like_q, like_q, like_q, now, limit)).fetchall()

        entries = [self._row_to_entry(r) for r in rows]
        self.touch([e.id for e in entries], now=now)
        return self._rerank_entries(entries, limit=limit)

    def search_by_tag(self, tag: str, category: str = "", limit: int = 10) -> list[KnowledgeEntry]:
        """按标签精确检索（`user:<id>` / `conversation:<id>` / `group:<id>` 这类作用域标签）。

        为什么必须是独立接口而不是复用 search()：标签含冒号，FTS5 会把 `user:123`
        解析成「列过滤器 user」并抛 OperationalError，于是它**只能**靠 search() 的
        LIKE 兜底分支命中 tags 列 —— 一条误打误撞的路径。search() 的兜底条件一变
        （见同文件 search() 里 `not rows` 的注释），这条路就断了，表现为
        「搜索修好了、反而忘了用户偏好」。所以作用域检索在这里显式落地，
        不再依赖 FTS 抛异常。

        tags 列是 JSON 数组文本，用参数化 LIKE 匹配 `"<tag>"` 带引号的完整字面量，
        避免 `user:1` 命中 `user:10001`。SQL 一律参数化，不做字符串拼接。
        """
        needle = normalize_text(tag)
        if not needle:
            return []
        conn = self._get_conn()
        now = time.time()
        # json.dumps 保证与写入侧 (add(): json.dumps(tags)) 的转义规则一致。
        like_tag = f"%{json.dumps(needle, ensure_ascii=False)}%"
        if category:
            sql = (
                "SELECT * FROM knowledge WHERE category=? AND tags LIKE ? "
                "AND (expires_at=0 OR expires_at>?) "
                "ORDER BY created_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (category, like_tag, now, limit)).fetchall()
        else:
            sql = (
                "SELECT * FROM knowledge WHERE tags LIKE ? "
                "AND (expires_at=0 OR expires_at>?) "
                "ORDER BY created_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (like_tag, now, limit)).fetchall()
        entries = [self._row_to_entry(r) for r in rows]
        # 精确到标签，再按标签字面量核一遍：LIKE 只是索引用的粗筛。
        entries = [e for e in entries if any(normalize_text(str(t)) == needle for t in (e.tags or []))]
        self.touch([e.id for e in entries], now=now)
        return self._rerank_entries(entries, limit=limit)

    def get_by_category(self, category: str, limit: int = 20) -> list[KnowledgeEntry]:
        """按分类获取最新条目。"""
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM knowledge WHERE category=? AND (expires_at=0 OR expires_at>?) "
            "ORDER BY created_at DESC LIMIT ?",
            (category, now, limit),
        ).fetchall()
        entries = [self._row_to_entry(r) for r in rows]
        self.touch([e.id for e in entries], now=now)
        return self._rerank_entries(entries, limit=limit)

    def touch(self, entry_ids: list[int], now: float | None = None) -> int:
        """召回一次就记一次：access_count+1、last_used_at=now。

        没有这一步就永远没有陈旧判据（vision-knowledge-base.md G3.4）。
        强化项 reinforcement 也靠它喂。失败只告警，不能让读路径因为写计数而失败。
        """
        ids = [int(i) for i in entry_ids if int(i or 0) > 0]
        if not ids:
            return 0
        stamp = float(now if now is not None else time.time())
        placeholders = ",".join("?" for _ in ids)
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                f"UPDATE knowledge SET access_count=access_count+1, last_used_at=? WHERE id IN ({placeholders})",
                (stamp, *ids),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            _log.warning("knowledge_touch_failed | ids=%s", ids, exc_info=True)
            return 0

    def list_stale(self, threshold_days: float = _STALE_AFTER_DAYS, limit: int = 50) -> list[KnowledgeEntry]:
        """列出「写入很久、从未被召回」的条目 —— 只报告，不淘汰。

        自净化的陈旧判据落在这里：交给模型或人工决定取代/复核，
        而不是让代码自动删。永久知识库的前提是删除必须是显式动作。
        """
        conn = self._get_conn()
        cutoff = time.time() - max(0.0, float(threshold_days)) * 86400.0
        rows = conn.execute(
            "SELECT * FROM knowledge WHERE last_used_at=0 AND created_at<? "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, max(1, int(limit))),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def count(self, category: str = "") -> int:
        """统计条目数。"""
        conn = self._get_conn()
        if category:
            row = conn.execute("SELECT COUNT(*) FROM knowledge WHERE category=?", (category,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()
        return row[0] if row else 0

    def cleanup_expired(self) -> int:
        """清理过期条目 —— 全库唯一的 DELETE 路径。

        原实现是裸 DELETE：不存版本快照、不删对应的 FTS 行、不写审计。
        那是「打开 TTL 回收的第一天就静默丢数据」的源头。
        现在每一行在删之前先进 knowledge_versions（历史可回看）、
        同步删 FTS 行（避免孤儿 rowid），并逐条写 knowledge 审计流。
        默认配置下本方法仍然不可达（唯一调用点在 core/engine.py 的热搜循环里，
        而 trend_fetch_enable 默认 False），行为不变，只是不再是静默的。
        """
        conn = self._get_conn()
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM knowledge WHERE expires_at>0 AND expires_at<?", (now,)
        ).fetchall()
        if not rows:
            return 0

        deleted = 0
        for row in rows:
            entry = self._row_to_entry(row)
            self._record_version_snapshot(
                conn=conn,
                knowledge_id=entry.id,
                category=entry.category,
                title=entry.title,
                content=entry.content,
                source=entry.source,
                tags=json.dumps(entry.tags, ensure_ascii=False),
                extra=json.dumps(entry.extra, ensure_ascii=False),
                created_at=entry.created_at,
                replaced_at=now,
            )
            conn.execute("DELETE FROM knowledge WHERE id=?", (entry.id,))
            self._delete_fts(conn, entry.id)
            deleted += 1
            self.record_audit(
                "knowledge_expired_purged",
                knowledge_id=entry.id,
                category=entry.category,
                title=entry.title,
                expires_at=entry.expires_at,
                access_count=entry.access_count,
                last_used_at=entry.last_used_at,
                snapshot_kept=True,
            )
        conn.commit()
        _log.info("knowledge_cleanup | deleted=%d | snapshots=%d", deleted, deleted)
        return deleted

    def stats(self) -> dict[str, int]:
        """各分类统计。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM knowledge GROUP BY category"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def batch_add_trends(self, trends: list[dict[str, Any]], source: str = "") -> int:
        """批量添加热搜条目。"""
        added = 0
        for item in trends:
            title = normalize_text(str(item.get("title", "")))
            if not title:
                continue
            self.add(
                category=CATEGORY_TREND,
                title=title,
                content=normalize_text(str(item.get("snippet", ""))),
                source=source or normalize_text(str(item.get("source", "trend"))),
                tags=[source] if source else [],
                extra={"heat": str(item.get("heat", "")), "url": str(item.get("url", ""))},
            )
            added += 1
        return added

    def close(self) -> None:
        """关掉当前线程上属于**本实例数据库**的连接。

        原实现关的是 `_db_local.conn`（当前线程唯一那条），
        在多实例场景下会把别的库的连接一起关掉。
        其他线程的连接仍然泄漏 —— thread-local 没有集中登记，本次不动这一点。
        """
        conns = getattr(_db_local, "conns", None)
        if not conns:
            return
        conn = conns.pop(str(self._db_path), None)
        if conn is not None:
            conn.close()

    # ── 内部方法 ──

    @staticmethod
    def _row_to_entry(row: Any) -> KnowledgeEntry:
        """按**列名**取值。

        原实现是 row[0]..row[8] 位置索引。ALTER TABLE ADD COLUMN 会让 SELECT * /
        SELECT k.* 的元组变长，位置索引读不到新列且不报错（静默丢字段）。
        连接已设 row_factory=sqlite3.Row，所以这里能按名取；
        仍兼容裸元组（万一有人换掉 row_factory）。
        """
        if hasattr(row, "keys"):
            def _col(name: str, position: int, default: Any = None) -> Any:
                try:
                    return row[name]
                except (IndexError, KeyError):
                    return default
        else:
            def _col(name: str, position: int, default: Any = None) -> Any:
                try:
                    return row[position]
                except (IndexError, KeyError):
                    return default

        row_id = _col("id", 0, 0)
        try:
            raw_tags = _col("tags", 5)
            tags_val = json.loads(raw_tags) if raw_tags else []
        except Exception:
            _log.warning("knowledge_parse_tags_error | row_id=%s", row_id, exc_info=True)
            tags_val = []
        try:
            raw_extra = _col("extra", 8)
            extra_val = json.loads(raw_extra) if raw_extra else {}
        except Exception:
            _log.warning("knowledge_parse_extra_json_error | row_id=%s", row_id, exc_info=True)
            extra_val = {}
        return KnowledgeEntry(
            id=int(row_id or 0),
            category=_col("category", 1) or "",
            title=_col("title", 2) or "",
            content=_col("content", 3) or "",
            source=_col("source", 4) or "",
            tags=tags_val if isinstance(tags_val, list) else [],
            created_at=float(_col("created_at", 6, 0) or 0),
            expires_at=float(_col("expires_at", 7, 0) or 0),
            extra=extra_val if isinstance(extra_val, dict) else {},
            access_count=int(_col("access_count", 9, 0) or 0),
            last_used_at=float(_col("last_used_at", 10, 0) or 0),
        )

    def _insert_fts(self, conn: sqlite3.Connection, row_id: int, title: str, content: str, tags: str) -> None:
        if not self._fts_enabled:
            return
        try:
            conn.execute(
                "INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (row_id, title, content, tags),
            )
        except sqlite3.OperationalError:
            # 主表写成功而索引没写上时该条目会从此搜不到（MATCH 不报错，只是查不到），
            # 原实现在这里静默 pass，没有任何代码能发现。至少要留告警。
            _log.warning("knowledge_fts_insert_failed | row_id=%s", row_id, exc_info=True)

    def _update_fts(self, conn: sqlite3.Connection, row_id: int, title: str, content: str, tags: str) -> None:
        if not self._fts_enabled:
            return
        try:
            conn.execute("DELETE FROM knowledge_fts WHERE rowid=?", (row_id,))
            conn.execute(
                "INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (row_id, title, content, tags),
            )
        except sqlite3.OperationalError:
            _log.warning("knowledge_fts_update_failed | row_id=%s", row_id, exc_info=True)

    def _delete_fts(self, conn: sqlite3.Connection, row_id: int) -> None:
        """删主表行时同步删索引行，避免 external-content 模式下的孤儿 rowid。"""
        if not self._fts_enabled:
            return
        try:
            conn.execute("DELETE FROM knowledge_fts WHERE rowid=?", (row_id,))
        except sqlite3.OperationalError:
            _log.warning("knowledge_fts_delete_failed | row_id=%s", row_id, exc_info=True)

    @staticmethod
    def _find_content_duplicate(conn: sqlite3.Connection, category: str, content: str) -> Any:
        """同分类下内容完全相同、标题不同的既有条目。

        为什么不做语义近重复：中文 tokenize 不切词（实测 '喜欢的歌' → ['喜欢的歌']），
        字面 Jaccard 会把「喜欢的歌」和「喜欢的歌手」判成 0.8 相似而错并两个不同主体。
        结构等价（normalize 后内容全等）是可判定的，语义近似交给模型。

        必须排除已过期的行：读路径按 expires_at 过滤，若把新条目 alias 到一个
        读不出来的过期行上，这条新知识就凭空消失了。
        """
        if not content:
            return None
        return conn.execute(
            "SELECT * FROM knowledge WHERE category=? AND content=? "
            "AND (expires_at=0 OR expires_at>?) ORDER BY id ASC LIMIT 1",
            (category, content, time.time()),
        ).fetchone()

    def _register_alias(self, conn: sqlite3.Connection, entry_id: int, alias: str, extra_raw: str) -> None:
        """把重复写入的标题记成保留条目的别名。不新增行、不删旧行。"""
        merged = self._safe_json_dict(extra_raw)
        aliases = [str(a) for a in (merged.get("aliases") or []) if str(a)]
        if alias and alias not in aliases:
            aliases.append(alias)
        merged["aliases"] = aliases[:20]
        conn.execute(
            "UPDATE knowledge SET extra=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), entry_id),
        )

    @staticmethod
    def _merge_extra(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = dict(old or {})
        for k, v in (new or {}).items():
            merged[k] = v
        # 追踪纠错次数，便于检索优先级排序。
        if bool(new.get("is_correction")):
            merged["correction_count"] = int(merged.get("correction_count", 0) or 0) + 1
        return merged

    def _record_version_snapshot(
        self,
        conn: sqlite3.Connection,
        knowledge_id: int,
        category: str,
        title: str,
        content: str,
        source: str,
        tags: str,
        extra: str,
        created_at: float,
        replaced_at: float,
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) FROM knowledge_versions WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        next_ver = int((row[0] if row else 0) or 0) + 1
        conn.execute(
            "INSERT INTO knowledge_versions (knowledge_id, version_no, category, title, content, source, tags, extra, created_at, replaced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                knowledge_id,
                next_ver,
                category,
                title,
                content,
                source,
                tags,
                extra,
                created_at,
                replaced_at,
            ),
        )

    @staticmethod
    def _effective_score(entry: KnowledgeEntry, now: float) -> float:
        """置信度 × 时间衰减 × 召回强化。

        公式直接复用 core/memory.py:568-570 的 knowledge_search 模型
        —— 那是本仓唯一已实现的衰减+强化公式，但它所在的 knowledge_store 表恒空，
        所以从未参与过任何真实决策。这里不发明第二套，只把同一套接到真在跑的读路径。
        向量相似度那一项不带过来：本表没有 embedding 列。
        """
        extra = entry.extra if isinstance(entry.extra, dict) else {}
        confidence = float(extra.get("confidence", _DEFAULT_CONFIDENCE) or 0.0)
        basis = float(extra.get("updated_at", entry.created_at) or entry.created_at or now)
        days_old = max(0.0, (now - basis) / 86400.0)
        decay = max(_DECAY_FLOOR, 1.0 - days_old * _DECAY_PER_DAY)
        reinforcement = min(_REINFORCE_CEIL, 1.0 + max(0, entry.access_count) * _REINFORCE_PER_HIT)
        return confidence * decay * reinforcement

    @classmethod
    def _entry_rank(cls, entry: KnowledgeEntry, now: float) -> tuple[int, float, float]:
        """(纠错偏置, 有效分, 时间戳)。

        第二项原来是裸 confidence，与陈旧程度、召回热度都无关 ——
        写好的三套衰减公式因此对检索没有任何影响（vision-knowledge-base.md G3）。
        现在换成 _effective_score，衰减与强化真正参与排序。
        元组逐项比较的语义不变：纠错优先 → 有效分 → 时间兜底。
        """
        extra = entry.extra if isinstance(entry.extra, dict) else {}
        correction_bias = 0
        if normalize_text(str(extra.get("source_type", ""))).lower() == "user_correction":
            correction_bias += 2
        if bool(extra.get("is_correction")):
            correction_bias += 1
        correction_bias += int(extra.get("correction_count", 0) or 0)
        freshness = float(extra.get("updated_at", entry.created_at) or entry.created_at)
        return correction_bias, cls._effective_score(entry, now), freshness

    def _rerank_entries(self, entries: list[KnowledgeEntry], limit: int) -> list[KnowledgeEntry]:
        if not entries:
            return []
        # now 取一次，保证同一批条目用同一个时间基准算衰减。
        now = time.time()
        ranked = sorted(entries, key=lambda e: self._entry_rank(e, now), reverse=True)
        return ranked[: max(1, int(limit))]

    @staticmethod
    def _safe_json_dict(text: str) -> dict[str, Any]:
        try:
            raw = json.loads(text or "{}")
            if isinstance(raw, dict):
                return raw
        except Exception:
            _log.warning("knowledge_safe_json_dict_error", exc_info=True)
        return {}
