"""Fast, persistent filename index used by chat channels.

The index intentionally stores metadata only (never file contents).  It keeps
WeChat "find a file" requests out of the multi-turn LLM/tool loop while still
respecting an explicit allow-list of search roots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from common.log import logger
from config import conf, get_data_root


_SKIP_DIRS = {
    ".git", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "AppData", "$Recycle.Bin", "System Volume Information", "Windows",
    "Program Files", "Program Files (x86)", ".cache", ".npm", ".pnpm-store",
}
_TYPE_EXTENSIONS = {
    "word": {".doc", ".docx"}, "文档": {".doc", ".docx", ".odt", ".rtf", ".txt", ".md"},
    "excel": {".xls", ".xlsx", ".xlsm", ".csv"}, "表格": {".xls", ".xlsx", ".xlsm", ".csv"},
    "ppt": {".ppt", ".pptx"}, "演示": {".ppt", ".pptx"},
    "pdf": {".pdf"}, "图片": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
    "视频": {".mp4", ".mov", ".mkv", ".avi", ".wmv"},
    "压缩包": {".zip", ".7z", ".rar", ".tar", ".gz"},
}


@dataclass
class FileSearchIntent:
    query: str
    extensions: set[str]
    recent_days: int = 0
    limit: int = 8


def parse_file_search_intent(message: str) -> Optional[FileSearchIntent]:
    text = str(message or "").strip()
    # A user commonly follows a broad request with only the exact filename,
    # e.g. ``网络安全应急预案框架.docx``.  Treat that as a deterministic file
    # search instead of sending it back through the general chat model.
    standalone_filename = re.fullmatch(
        r"\s*([^\\/:*?\"<>|\r\n]{1,180}\.[A-Za-z0-9]{1,10})\s*", text
    )
    find_intent = re.search(r"(?:找|查找|搜索|查一下|帮我找|帮我查).{0,30}(?:文件|文档|表格|报告|周报|合同|资料|图片|照片)", text, re.I)
    send_intent = (re.search(r"(?:文件|文档|表格|报告|周报|合同|资料|图片|照片).{0,10}(?:发|传|发送)给我", text, re.I)
                   and re.search(r"(?:电脑|本地|桌面|下载|文档目录|名字?叫|名称)", text, re.I))
    if not find_intent and not send_intent and not standalone_filename:
        return None
    if standalone_filename:
        filename = standalone_filename.group(1).strip()
        suffix = Path(filename).suffix.lower()
        return FileSearchIntent(query=filename, extensions={suffix} if suffix else set())
    quoted = re.search(r"[“\"']([^”\"']{1,100})[”\"']", text)
    direct_send = re.search(
        r"(?:请|麻烦)?(?:帮我)?(?:把|将)?(?:桌面上?|电脑上?|电脑里|本地|下载目录|文档目录)(?:的)?\s*"
        r"([^，。；;]{1,100}?)(?:的)?(?:文件|文档|资料|图片|照片)\s*(?:发|传|发送)给我",
        text,
        re.I,
    )
    named = re.search(r"(?:名字?叫|名称(?:是|为)?|叫)\s*([^，。；;]{1,100}?)(?=(?:的)?(?:文件|文档|图片|照片)|(?:发|传|发送)给我|$)", text, re.I)
    located = re.search(
        r"(?:桌面上?|电脑上?|电脑里|本地|下载目录|文档目录)(?:的)?\s*"
        r"([^，。；;]{1,100}?)(?:的)?(?:文件|文档|资料|图片|照片)\s*(?:发|传|发送)给我",
        text, re.I,
    )
    work = quoted.group(1) if quoted else (
        direct_send.group(1) if direct_send else (
            named.group(1) if named else (located.group(1) if located else text)
        )
    )
    work = re.sub(r"^(?:请|麻烦)?(?:帮我)?(?:把|将)?\s*", "", work)
    work = re.sub(r"^(?:请|麻烦)?(?:帮我)?(?:在电脑里|在本地|从电脑里)?(?:找|查找|搜索|查一下)\s*", "", work)
    work = re.sub(r"^(?:文件|文档|资料)\s*[：:]?\s*", "", work)
    work = re.sub(r"(?:电脑里|本地|电脑上|桌面上?|下载目录|文档目录|中的|里面|文件名|名称|名字)", " ", work)
    work = re.sub(r"(?:最近|过去)\s*\d+\s*天", " ", work)
    work = re.sub(r"(?:按)?(?:最新|修改时间|日期)(?:排序)?", " ", work)
    work = re.sub(r"(?:发|传|发送)给我.*$", "", work)
    work = re.sub(r"(?:的)?(?:文件|文档|资料|图片|照片)$", "", work)
    work = re.sub(r"(?:^|\s)(?:文件|文档|资料)(?:\s|$)", " ", work)
    extensions = set()
    for label, values in _TYPE_EXTENSIONS.items():
        if label.lower() in text.lower():
            extensions.update(values)
            work = re.sub(re.escape(label), " ", work, flags=re.I)
    suffix = re.search(r"\.([A-Za-z0-9]{1,8})(?:\s|$)", text)
    if suffix:
        extensions.add("." + suffix.group(1).lower())
    days = re.search(r"(?:最近|过去)\s*(\d+)\s*天", text)
    query = re.sub(r"[：:，,。；;\s]+", " ", work).strip()
    query = re.sub(r"^(?:的|里|里的|中|中的)\s*", "", query).strip()
    query = re.sub(r"^(?:一个|一下)\s*", "", query).strip()
    if not query and not extensions:
        return FileSearchIntent("", extensions)
    return FileSearchIntent(query=query, extensions=extensions,
                            recent_days=int(days.group(1)) if days else 0)


class LocalFileIndex:
    def __init__(self):
        self.path = Path(get_data_root()) / "local_file_index.sqlite3"
        self._lock = threading.RLock()
        self._refreshing = False
        self._last_refresh = 0.0
        self._ensure_schema()

    def roots(self) -> List[Path]:
        configured = conf().get("file_search_roots") or []
        if isinstance(configured, str):
            configured = [part.strip() for part in configured.split(";") if part.strip()]
        if not configured:
            home = Path.home()
            configured = [self._desktop_path(), home / "Documents", home / "Downloads",
                          Path(str(conf().get("agent_workspace") or "")).expanduser()]
        result = []
        for raw in configured:
            path = Path(raw).expanduser()
            try:
                path = path.resolve()
            except OSError:
                continue
            if path.exists() and path not in result:
                result.append(path)
        return result

    @staticmethod
    def _desktop_path() -> Path:
        """Return the real Windows Desktop, including OneDrive redirection."""
        if os.name == "nt":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                )
                value, _ = winreg.QueryValueEx(key, "Desktop")
                winreg.CloseKey(key)
                expanded = os.path.expandvars(str(value))
                if expanded:
                    return Path(expanded).expanduser()
            except OSError:
                pass
        return Path.home() / "Desktop"

    def start_background_refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, name="local-file-index", daemon=True).start()

    def _refresh_worker(self):
        try:
            self.refresh()
        except Exception as exc:
            logger.warning(f"[FileIndex] refresh failed: {exc}")
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self) -> int:
        rows = []
        # Index explicitly selected folders before broad drive roots.  Without
        # this ordering, a 250k-entry system drive can consume the entire cap
        # and make a small, specifically authorised folder invisible.
        roots = sorted(self.roots(), key=lambda item: len(str(item)), reverse=True)
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
                for name in filenames:
                    path = Path(dirpath) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    rows.append((str(path), name, path.suffix.lower(), stat.st_mtime, stat.st_size, str(root)))
                    if len(rows) >= 250_000:
                        break
                if len(rows) >= 250_000:
                    break
        with self._connect() as db:
            db.execute("DELETE FROM files")
            db.executemany("INSERT OR REPLACE INTO files(path,name,ext,mtime,size,root) VALUES(?,?,?,?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_refresh',?)", (str(time.time()),))
        self._last_refresh = time.time()
        logger.info(f"[FileIndex] indexed {len(rows)} files from {len(roots)} roots")
        return len(rows)

    def search(self, intent: FileSearchIntent) -> List[dict]:
        if self.count() == 0:
            self.refresh()
        elif time.time() - self.last_refresh() > 900:
            self.start_background_refresh()
        tokens = [token for token in re.split(r"\s+", intent.query.lower()) if token]
        sql = "SELECT path,name,ext,mtime,size,root FROM files WHERE 1=1"
        params: List[object] = []
        for token in tokens:
            sql += " AND lower(name) LIKE ?"
            params.append(f"%{token}%")
        if intent.extensions:
            sql += " AND ext IN (" + ",".join("?" for _ in intent.extensions) + ")"
            params.extend(sorted(intent.extensions))
        if intent.recent_days:
            sql += " AND mtime>=?"
            params.append(time.time() - intent.recent_days * 86400)
        sql += " ORDER BY mtime DESC LIMIT ?"
        params.append(max(1, min(intent.limit, 20)))
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        results = [dict(row) for row in rows]
        # A broad authorised disk can contain more entries than the persistent
        # index cap.  For an explicit filename, a miss must fall back to a live
        # exact-name walk instead of incorrectly claiming that the file does
        # not exist.  This reads metadata only and remains inside allow-listed
        # roots.
        if not results and self._looks_like_explicit_filename(intent.query):
            results = self._search_exact_filename(intent)
        return results

    @staticmethod
    def _looks_like_explicit_filename(query: str) -> bool:
        name = os.path.basename(str(query or "").strip())
        return bool(name and Path(name).suffix and len(name) <= 220)

    def _search_exact_filename(self, intent: FileSearchIntent) -> List[dict]:
        wanted = os.path.basename(intent.query).casefold()
        matches: List[dict] = []
        seen_paths = set()
        # Search narrower roots first and avoid walking them again underneath a
        # subsequently configured drive root.
        roots = sorted(self.roots(), key=lambda item: len(str(item)), reverse=True)
        for root in roots:
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [
                        name for name in dirnames
                        if name not in _SKIP_DIRS and not name.startswith(".")
                    ]
                    for name in filenames:
                        if name.casefold() != wanted:
                            continue
                        path = Path(dirpath) / name
                        canonical = os.path.normcase(os.path.realpath(path))
                        if canonical in seen_paths:
                            continue
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        if intent.recent_days and stat.st_mtime < time.time() - intent.recent_days * 86400:
                            continue
                        seen_paths.add(canonical)
                        matches.append({
                            "path": str(path), "name": name,
                            "ext": path.suffix.lower(), "mtime": stat.st_mtime,
                            "size": stat.st_size, "root": str(root),
                        })
                        if len(matches) >= max(1, min(intent.limit, 20)):
                            return sorted(matches, key=lambda item: item["mtime"], reverse=True)
            except (OSError, PermissionError) as exc:
                logger.debug(f"[FileIndex] exact search skipped root={root}: {exc}")
        return sorted(matches, key=lambda item: item["mtime"], reverse=True)

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def last_refresh(self) -> float:
        if self._last_refresh:
            return self._last_refresh
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='last_refresh'").fetchone()
        return float(row[0]) if row else 0.0

    def is_refreshing(self) -> bool:
        with self._lock:
            return self._refreshing

    def _ensure_schema(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,name TEXT,ext TEXT,mtime REAL,size INTEGER,root TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db


_instance = None
_instance_lock = threading.Lock()


def get_local_file_index() -> LocalFileIndex:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = LocalFileIndex()
        return _instance


def format_file_search_reply(intent: FileSearchIntent, hits: Iterable[dict]) -> str:
    hits = list(hits)
    if not hits:
        subject = intent.query or "指定类型"
        return f"没有在已授权目录中找到“{subject}”相关文件。你可以补充文件名、类型或大致所在目录。"
    lines = [f"🔎 找到 {len(hits)} 个相关文件（按最新修改排序）："]
    for index, item in enumerate(hits, 1):
        stamp = datetime.fromtimestamp(float(item["mtime"])).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{index}. {item['name']}\n   {stamp}｜{item['path']}")
    lines.append("\n回复“发送第1个”可继续发送文件；移动、删除或覆盖前仍会请求审批。")
    return "\n".join(lines)
