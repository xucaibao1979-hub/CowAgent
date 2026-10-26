"""Small, credential-safe audit trail for unified tool execution."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_SENSITIVE = ("password", "passwd", "secret", "token", "credential", "api_key", "authorization")


def classify_error(value: Any, permission_denied: bool = False) -> str:
    if permission_denied:
        return "permission_denied"
    text = str(value or "").casefold()
    if "timed out" in text or "timeout" in text or "超时" in text:
        return "timeout"
    if "not found" in text or "no module named" in text or "未找到" in text:
        return "dependency_missing"
    if "needs_auth" in text or "unauthorized" in text or "401" in text:
        return "authentication_required"
    if "connection" in text or "无法连接" in text or "10061" in text:
        return "connection_failed"
    return "execution_failed"


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if any(marker in str(key).casefold() for marker in _SENSITIVE)
            else _safe(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value[:50]]
    text = str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
    if isinstance(text, str) and len(text) > 2000:
        return text[:2000] + "…"
    return text


def sanitize_for_audit(value: Any) -> Any:
    """Public redaction helper for approval previews and other audit records."""
    return _safe(value)


def record_tool_run(tool: str, arguments: dict, success: bool, *, status: str = "",
                    result: Any = None, execution_time: float = 0,
                    error_code: str = "", intent: str = "", run_id: str = "") -> None:
    try:
        from common.state_dir import state_path
        path = Path(state_path("logs", "tool_audit.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tool": str(tool or ""),
            "status": status or ("success" if success else "error"),
            "success": bool(success),
            "error_code": error_code or ("" if success else classify_error(result)),
            "intent": str(intent or ""),
            "run_id": str(run_id or ""),
            "execution_time": round(float(execution_time or 0), 3),
            "arguments": _safe(arguments or {}),
            "result": _safe(result),
        }
        from common.runtime_identity import current_identity
        identity = current_identity()
        event.update({"session_id": identity.session_id or "", "agent_id": identity.agent_id or "",
                      "parent_run_id": identity.run_id or ""})
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LOCK:
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                backup = path.with_suffix(".1.jsonl")
                if backup.exists():
                    backup.unlink()
                path.replace(backup)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        # Audit must never break the user's task.
        return
