"""Persistent user pairing and authorization for interactive channels."""

import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone


def _iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ChannelAccessService:
    PAIRING_TTL = 15 * 60
    MAX_EVENTS = 500
    MAX_PENDING_PER_CHANNEL = 100

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    data.setdefault("entries", [])
                    data.setdefault("events", [])
                    return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "entries": [], "events": []}

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    @staticmethod
    def _entry(data, entry_id=None, channel=None, user_id=None):
        for item in data["entries"]:
            if entry_id and item.get("id") == entry_id:
                return item
            if channel and user_id and item.get("channel") == channel and item.get("user_id") == user_id:
                return item
        return None

    def _event(self, data, event_type, entry, detail=""):
        data["events"].insert(0, {
            "id": uuid.uuid4().hex,
            "type": event_type,
            "entry_id": entry.get("id", ""),
            "channel": entry.get("channel", ""),
            "user_id": entry.get("user_id", ""),
            "display_name": entry.get("display_name", ""),
            "detail": detail,
            "created_at": _iso_now(),
        })
        del data["events"][self.MAX_EVENTS:]

    def _new_code(self, data):
        active = {str(x.get("pairing_code", "")) for x in data["entries"]}
        while True:
            code = f"{secrets.randbelow(900000) + 100000:06d}"
            if code not in active:
                return code

    def authorize_or_request(self, channel, user_id, display_name=""):
        """Return authorization decision, creating a short-lived pairing request when needed."""
        if not user_id:
            return {"allowed": False, "status": "invalid", "notify": False}
        now = time.time()
        with self._lock:
            data = self._load()
            entry = self._entry(data, channel=channel, user_id=user_id)
            if entry and display_name and not entry.get("display_name"):
                entry["display_name"] = display_name
            if entry and entry.get("status") == "approved":
                entry["last_seen_at"] = _iso_now()
                entry["message_count"] = int(entry.get("message_count", 0)) + 1
                self._event(data, "message_allowed", entry, "收到授权用户消息")
                self._save(data)
                return {"allowed": True, "status": "approved", "entry": dict(entry)}
            if entry and entry.get("status") in ("disabled", "revoked", "rejected"):
                entry["last_seen_at"] = _iso_now()
                self._event(data, "message_blocked", entry, f"账号状态：{entry['status']}")
                self._save(data)
                return {"allowed": False, "status": entry["status"], "notify": True, "entry": dict(entry)}

            notify = False
            if not entry:
                pending_count = sum(1 for item in data["entries"] if
                                    item.get("channel") == channel and item.get("status") == "pending")
                if pending_count >= self.MAX_PENDING_PER_CHANNEL:
                    return {"allowed": False, "status": "throttled", "notify": False}
                entry = {
                    "id": uuid.uuid4().hex,
                    "channel": channel,
                    "user_id": user_id,
                    "display_name": display_name or user_id,
                    "role": "user",
                    "status": "pending",
                    "permission_level": "standard",
                    "project_id": "",
                    "note": "",
                    "created_at": _iso_now(),
                    "message_count": 0,
                }
                data["entries"].append(entry)
                notify = True
            expires_at = float(entry.get("pairing_expires_ts", 0) or 0)
            if not entry.get("pairing_code") or expires_at <= now:
                entry["pairing_code"] = self._new_code(data)
                entry["pairing_expires_ts"] = now + self.PAIRING_TTL
                entry["pairing_expires_at"] = datetime.fromtimestamp(
                    entry["pairing_expires_ts"], timezone.utc
                ).astimezone().isoformat(timespec="seconds")
                notify = True
                self._event(data, "pairing_requested", entry, "生成新的微信配对码")
            last_notice = float(entry.get("last_notified_ts", 0) or 0)
            if notify or now - last_notice >= 60:
                entry["last_notified_ts"] = now
                notify = True
            entry["last_seen_at"] = _iso_now()
            self._save(data)
            return {"allowed": False, "status": "pending", "notify": notify, "entry": dict(entry)}

    def auto_approve_owner(self, channel, user_id, display_name="扫码管理员"):
        if not user_id:
            return None
        with self._lock:
            data = self._load()
            # A QR login represents the current controllable WeChat identity.
            # When the user scans again, older QR-owner rows often point to an
            # expired ilink identity and cannot be used for scheduled pushes.
            # Keep the audit trail, but remove old owners from the approved
            # notification target pool so the UI shows a single current admin.
            for old in data["entries"]:
                if (old.get("channel") == channel and old.get("role") == "owner"
                        and old.get("status") == "approved" and old.get("user_id") != user_id):
                    old["status"] = "revoked"
                    old["updated_at"] = _iso_now()
                    old["revoked_reason"] = "新的扫码登录已接管，旧扫码管理员自动过期"
                    self._event(data, "owner_expired", old, "新的扫码登录已覆盖旧管理员记录")
            entry = self._entry(data, channel=channel, user_id=user_id)
            if not entry:
                entry = {
                    "id": uuid.uuid4().hex, "channel": channel, "user_id": user_id,
                    "created_at": _iso_now(), "message_count": 0,
                    "permission_level": "standard", "project_id": "", "note": "",
                }
                data["entries"].append(entry)
            entry.update({
                "display_name": display_name or entry.get("display_name") or user_id,
                "role": "owner", "status": "approved", "approved_at": _iso_now(),
                "approved_by": "扫码登录", "pairing_code": "",
            })
            self._event(data, "owner_approved", entry, "扫码登录账号已设为管理员")
            self._save(data)
            return dict(entry)

    def list(self, channel="", status="", query=""):
        with self._lock:
            data = self._load()
        query = (query or "").lower().strip()
        entries = [dict(x) for x in data["entries"] if
                   (not channel or x.get("channel") == channel) and
                   (not status or x.get("status") == status) and
                   (not query or query in " ".join([
                       str(x.get("display_name", "")), str(x.get("user_id", "")),
                       str(x.get("pairing_code", "")), str(x.get("note", "")),
                   ]).lower())]
        priority = {"pending": 0, "approved": 1, "disabled": 2, "rejected": 3, "revoked": 4}
        entries.sort(key=lambda x: (priority.get(x.get("status"), 9), x.get("created_at", "")), reverse=False)
        summary = {key: 0 for key in ("pending", "approved", "disabled", "rejected", "revoked")}
        for item in data["entries"]:
            if not channel or item.get("channel") == channel:
                summary[item.get("status", "pending")] = summary.get(item.get("status", "pending"), 0) + 1
        return {"entries": entries, "events": data["events"][:30], "summary": summary}

    def action(self, action, entry_id, actor="控制台管理员"):
        allowed = {"approve", "reject", "disable", "enable", "revoke"}
        if action not in allowed:
            raise ValueError("不支持的授权操作")
        with self._lock:
            data = self._load()
            entry = self._entry(data, entry_id=entry_id)
            if not entry:
                raise KeyError("未找到该用户")
            if entry.get("role") == "owner" and action in ("reject", "disable", "revoke"):
                raise ValueError("扫码管理员不能在用户列表中被停用或撤销，请使用“解绑微信”")
            target = {"approve": "approved", "reject": "rejected", "disable": "disabled",
                      "enable": "approved", "revoke": "revoked"}[action]
            entry["status"] = target
            entry["updated_at"] = _iso_now()
            if target == "approved":
                entry["approved_at"] = _iso_now()
                entry["approved_by"] = actor
                entry["pairing_code"] = ""
            self._event(data, action, entry, f"由{actor}执行")
            self._save(data)
            return dict(entry)


_service = None
_service_lock = threading.Lock()


def get_channel_access_service():
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                from config import get_data_root
                _service = ChannelAccessService(os.path.join(get_data_root(), "channel_access.json"))
    return _service
