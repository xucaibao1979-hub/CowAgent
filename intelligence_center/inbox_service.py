"""Durable in-app inbox for approvals, automation results and system notices."""

import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


class InboxStore:
    _lock = threading.RLock()

    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.path = self.root / "inbox.json"
        from intelligence_center.sqlite_state import SQLiteStateStore
        self.state = SQLiteStateStore(self.root)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def upsert(self, *, source_id, kind, title, summary="", status="info", target=None, payload=None, unread=True):
        source_id = str(source_id or "").strip()
        if not source_id:
            source_id = uuid.uuid4().hex[:16]
        now = self._now()
        with self._lock:
            data = self._read()
            item = next((x for x in data["items"] if x.get("source_id") == source_id and x.get("kind") == kind), None)
            if item is None:
                item = {"id": uuid.uuid4().hex[:16], "source_id": source_id, "kind": kind, "created_at": now}
                data["items"].insert(0, item)
            item.update({"title": str(title or "消息"), "summary": str(summary or ""), "status": status,
                         "target": target or {}, "payload": payload or {}, "unread": bool(unread), "updated_at": now})
            self._write(data)
            return deepcopy(item)

    def list(self, *, unread_only=False, kind="", limit=20, offset=0, grouped=False):
        all_items = self._read()["items"]
        items = all_items
        if unread_only:
            items = [x for x in items if x.get("unread")]
        if kind:
            items = [x for x in items if x.get("kind") == kind]
        items = sorted(items, key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
        items = self._group_items(items) if grouped else self._dedupe_session_alerts(items)
        unread_items = [x for x in all_items if x.get("unread")]
        unread_count = len(self._group_items(unread_items) if grouped else self._dedupe_session_alerts(unread_items))
        total = len(items); limit = max(1, min(int(limit or 20), 100)); offset = max(0, int(offset or 0))
        return {"items": items[offset:offset + limit], "total": total,
                "unread": unread_count, "has_more": offset + limit < total}

    @staticmethod
    def _dedupe_session_alerts(items):
        output = []
        seen = set()
        for item in items or []:
            payload = item.get("payload") or {}
            task_id = str(payload.get("task_id") or item.get("source_id") or "")
            if "channel-session-" in task_id:
                key = task_id.split(":", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
            output.append(item)
        return output

    @classmethod
    def _group_items(cls, items):
        groups = {}
        order = []
        for item in items or []:
            key = cls._group_key(item)
            if key not in groups:
                groups[key] = deepcopy(item)
                groups[key]["group_ids"] = [item.get("id")]
                groups[key]["group_count"] = 1
                groups[key]["group_unread_count"] = 1 if item.get("unread") else 0
                order.append(key)
                continue
            group = groups[key]
            group["group_ids"].append(item.get("id"))
            group["group_count"] = int(group.get("group_count") or 1) + 1
            if item.get("unread"):
                group["group_unread_count"] = int(group.get("group_unread_count") or 0) + 1
            if cls._item_time(item) > cls._item_time(group):
                keep_ids = group.get("group_ids") or []
                keep_count = group.get("group_count") or 1
                keep_unread = group.get("group_unread_count") or 0
                groups[key] = deepcopy(item)
                groups[key]["group_ids"] = keep_ids
                groups[key]["group_count"] = keep_count
                groups[key]["group_unread_count"] = keep_unread
        return sorted(groups.values(), key=cls._item_time, reverse=True)

    @staticmethod
    def _item_time(item):
        return str((item or {}).get("updated_at") or (item or {}).get("created_at") or "")

    @staticmethod
    def _group_key(item):
        item = item or {}
        payload = item.get("payload") or {}
        target = item.get("target") or {}
        source_id = str(item.get("source_id") or "")
        kind = str(item.get("kind") or "")
        status = str(item.get("status") or "")
        day = str(item.get("updated_at") or item.get("created_at") or "")[:10]
        task_id = str(payload.get("task_id") or target.get("task_id") or "")
        channel = str(payload.get("channel") or target.get("channel") or "")
        session_key = task_id or source_id
        if "channel-session-" in session_key:
            return "channel-session:" + session_key.split(":", 1)[0]
        if source_id.startswith("notification-failure:"):
            failure_summary = payload.get("failure_summary") if isinstance(payload.get("failure_summary"), list) else []
            if failure_summary:
                first = failure_summary[0] or {}
                channel = str(first.get("channel") or channel or "unknown").strip()
                reason = str(first.get("reason") or item.get("title") or "通知失败").strip()
                return f"notification-failure:{channel}:{reason[:80]}:{day}"
            title = str(item.get("title") or "通知通道失败").strip()
            return f"notification-failure:{title[:60]}:{status}:{day}"
        if source_id.startswith("web-notify:"):
            parts = source_id.split(":")
            if len(parts) >= 2:
                return f"web-notify:{parts[1]}:{status}:{day}"
        if kind == "automation_result" and task_id:
            return f"automation-result:{task_id}:{channel}:{status}:{day}"
        if kind in {"system", "system_notice"}:
            title = str(item.get("title") or "").strip()
            if title:
                return f"{kind}:{title[:40]}:{status}:{day}"
        return f"item:{item.get('id') or source_id}"

    def mark(self, item_id="", *, unread=False, all_items=False, item_ids=None):
        target_ids = {str(x) for x in (item_ids or []) if x}
        with self._lock:
            data = self._read(); changed = 0
            for item in data["items"]:
                if all_items or item.get("id") == item_id or (target_ids and item.get("id") in target_ids):
                    item["unread"] = bool(unread); item["updated_at"] = self._now(); changed += 1
                    if not all_items and not target_ids:
                        break
            if changed:
                self._write(data)
            return changed

    def _read(self):
        data = self.state.read("inbox", {"version": 1, "items": []}, self.path)
        data.setdefault("items", [])
        return data

    def _write(self, data):
        self.state.write("inbox", data)
