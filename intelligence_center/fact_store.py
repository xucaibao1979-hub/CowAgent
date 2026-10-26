"""Local fact store for CowAgent personal memory and reusable experience.

The store is intentionally local-first and auditable.  It does not train model
weights; it keeps small, source-linked facts that other Agent modules can
retrieve later.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1


class FactStore:
    _lock = threading.RLock()

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "cowagent_fact_store.db"

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    @staticmethod
    def fingerprint(content: str, source_url: str = "", fact_type: str = "") -> str:
        normalized = re.sub(r"\s+", " ", str(content or "")).strip().lower()
        seed = "\n".join([fact_type or "fact", source_url or "", normalized])
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def upsert(
        self,
        *,
        content: str,
        fact_type: str = "knowledge",
        source: str = "",
        source_url: str = "",
        title: str = "",
        tags: Iterable[str] | None = None,
        scope: str = "private",
        confidence: float = 0.7,
        status: str = "active",
        metadata: dict | None = None,
    ) -> dict:
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if not text:
            raise ValueError("事实内容不能为空")
        tags_list = [str(item).strip() for item in (tags or []) if str(item).strip()]
        digest = self.fingerprint(text, source_url, fact_type)
        now = self.now()
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT id, seen_count, created_at FROM facts WHERE fingerprint = ?", (digest,)).fetchone()
            if row:
                fact_id, seen_count, created_at = row
                connection.execute(
                    """UPDATE facts SET content=?, title=?, source=?, source_url=?, tags=?, scope=?,
                       confidence=?, status=?, metadata=?, updated_at=?, seen_count=?
                       WHERE id=?""",
                    (
                        text,
                        title,
                        source,
                        source_url,
                        json.dumps(tags_list, ensure_ascii=False),
                        scope,
                        float(confidence),
                        status,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        now,
                        int(seen_count or 0) + 1,
                        fact_id,
                    ),
                )
            else:
                fact_id = uuid.uuid4().hex[:16]
                created_at = now
                connection.execute(
                    """INSERT INTO facts(id,fingerprint,fact_type,title,content,source,source_url,tags,scope,
                       confidence,status,metadata,created_at,updated_at,seen_count)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fact_id,
                        digest,
                        fact_type,
                        title,
                        text,
                        source,
                        source_url,
                        json.dumps(tags_list, ensure_ascii=False),
                        scope,
                        float(confidence),
                        status,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        created_at,
                        now,
                        1,
                    ),
                )
            connection.execute(
                "INSERT INTO fact_events(id,fact_id,action,summary,created_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex[:16], fact_id, "upsert", f"{fact_type}：{title or text[:40]}", now),
            )
            self._rebuild_fts(connection)
            connection.commit()
        return self.get(fact_id) or {}

    def search(self, query: str = "", fact_type: str = "", limit: int = 20, include_disabled: bool = False) -> list[dict]:
        needle = re.sub(r"\s+", " ", str(query or "")).strip()
        limit = max(1, min(int(limit or 20), 100))
        with self._lock, closing(self._connect()) as connection, connection:
            params = []
            where = ["status != 'deleted'"] if include_disabled else ["status = 'active'"]
            if fact_type:
                where.append("fact_type = ?")
                params.append(fact_type)
            if needle:
                where.append("id IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?)")
                params.append(self._fts_query(needle))
            sql = "SELECT * FROM facts WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = connection.execute(sql, params).fetchall()
            if needle and not rows:
                rows = self._like_search(connection, needle, fact_type, limit, include_disabled=include_disabled)
            return [self._row_to_dict(row) for row in rows]

    def get(self, fact_id: str) -> dict | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def stats(self) -> dict:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT fact_type, COUNT(*) FROM facts WHERE status = 'active' GROUP BY fact_type").fetchall()
            total = sum(int(row[1] or 0) for row in rows)
            pending = connection.execute("SELECT COUNT(*) FROM facts WHERE status = 'pending'").fetchone()[0]
            disabled = connection.execute("SELECT COUNT(*) FROM facts WHERE status = 'disabled'").fetchone()[0]
            deleted = connection.execute("SELECT COUNT(*) FROM facts WHERE status = 'deleted'").fetchone()[0]
            return {
                "version": SCHEMA_VERSION,
                "total": total,
                "pending": int(pending or 0),
                "disabled": int(disabled or 0),
                "deleted": int(deleted or 0),
                "by_type": {row[0]: int(row[1] or 0) for row in rows},
            }

    def update_status(self, fact_id: str, status: str, reason: str = "") -> dict:
        fact_id = str(fact_id or "").strip()
        status = str(status or "").strip().lower()
        if status not in {"pending", "active", "disabled", "deleted"}:
            raise ValueError("不支持的事实状态")
        now = self.now()
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT id, title, content FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if not row:
                raise ValueError("事实不存在")
            connection.execute("UPDATE facts SET status=?, updated_at=? WHERE id=?", (status, now, fact_id))
            summary = reason or {"pending": "设为待确认", "active": "确认生效", "disabled": "禁用记忆", "deleted": "删除记忆"}[status]
            connection.execute(
                "INSERT INTO fact_events(id,fact_id,action,summary,created_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex[:16], fact_id, f"status:{status}", summary, now),
            )
            self._rebuild_fts(connection)
            connection.commit()
        return self.get(fact_id) or {"id": fact_id, "status": status}

    def events(self, fact_id: str, limit: int = 12) -> list[dict]:
        fact_id = str(fact_id or "").strip()
        limit = max(1, min(int(limit or 12), 100))
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM fact_events WHERE fact_id = ? ORDER BY created_at DESC LIMIT ?",
                (fact_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS facts(
                id TEXT PRIMARY KEY,
                fingerprint TEXT UNIQUE NOT NULL,
                fact_type TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                source TEXT,
                source_url TEXT,
                tags TEXT,
                scope TEXT NOT NULL DEFAULT 'private',
                confidence REAL NOT NULL DEFAULT 0.7,
                status TEXT NOT NULL DEFAULT 'active',
                metadata TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS fact_events(
                id TEXT PRIMARY KEY,
                fact_id TEXT NOT NULL,
                action TEXT NOT NULL,
                summary TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                title, content, tags, content='facts', content_rowid='rowid'
            )"""
        )
        return connection

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key, fallback in (("tags", []), ("metadata", {})):
            try:
                item[key] = json.loads(item.get(key) or json.dumps(fallback, ensure_ascii=False))
            except (TypeError, ValueError):
                item[key] = fallback
        return item

    @staticmethod
    def _fts_query(value: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]{2,}", value)
        return " OR ".join(tokens[:12]) or value.replace('"', "")

    @staticmethod
    def _like_search(connection, needle: str, fact_type: str, limit: int, include_disabled: bool = False):
        tokens = re.findall(r"[A-Za-z0-9_+-]+|[\u4e00-\u9fff]{2,}", needle)
        tokens = tokens[:8] or [needle]
        where = ["status != 'deleted'"] if include_disabled else ["status = 'active'"]
        params = []
        if fact_type:
            where.append("fact_type = ?")
            params.append(fact_type)
        keyword_clauses = []
        for token in tokens:
            keyword_clauses.append("(title LIKE ? OR content LIKE ? OR tags LIKE ?)")
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
        where.append("(" + " OR ".join(keyword_clauses) + ")")
        params.append(limit)
        return connection.execute(
            "SELECT * FROM facts WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()

    @staticmethod
    def _rebuild_fts(connection):
        connection.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")


def get_fact_store(root: str | Path) -> FactStore:
    return FactStore(root)
