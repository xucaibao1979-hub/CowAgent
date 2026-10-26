"""Small SQLite-backed JSON document store used during the persistence migration."""

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class SQLiteStateStore:
    _lock = threading.RLock()

    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.path = self.root / "cowagent_state.db"

    def read(self, key, default, legacy_path=None):
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT payload FROM state_documents WHERE document_key = ?", (key,)).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except (ValueError, TypeError):
                    return default
            payload = default
            legacy = Path(legacy_path) if legacy_path else None
            if legacy and legacy.exists():
                try:
                    payload = json.loads(legacy.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    payload = default
            self._write(connection, key, payload)
            return payload

    def write(self, key, payload):
        with self._lock, closing(self._connect()) as connection:
            self._write(connection, key, payload)

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("CREATE TABLE IF NOT EXISTS state_documents (document_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
        return connection

    @staticmethod
    def _write(connection, key, payload):
        connection.execute(
            "INSERT INTO state_documents(document_key,payload,updated_at) VALUES(?,?,?) ON CONFLICT(document_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (key, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
