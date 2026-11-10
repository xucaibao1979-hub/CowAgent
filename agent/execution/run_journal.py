"""Crash-safe scheduler checkpoints with conservative recovery decisions."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path


class RunJournal:
    _lock = threading.RLock()

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", str(run_id or "")):
            raise ValueError("无效的执行 ID")
        return self.root / f"{run_id}.json"

    def read(self, run_id: str) -> dict:
        try:
            data = json.loads(self._path(run_id).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, run_id: str, data: dict) -> None:
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def begin(self, run_id: str, task_id: str, action_type: str, *, mode: str = "scheduled", guard_path: str = "") -> dict:
        with self._lock:
            data = {"run_id": run_id, "task_id": str(task_id), "action_type": str(action_type),
                    "mode": mode, "guard_path": str(guard_path), "status": "running",
                    "expected": "任务执行后核对通知接口状态", "actual": "尚未完成",
                    "steps": [], "updated_at": datetime.now().astimezone().isoformat()}
            self._save(run_id, data)
            return data

    def step(self, run_id: str, stage: str, status: str, *, expected: str = "", actual: str = "") -> dict:
        with self._lock:
            data = self.read(run_id)
            if not data or data.get("status") != "running":
                return data
            data["steps"].append({"stage": stage, "status": status, "expected": expected,
                                  "actual": actual, "at": datetime.now().astimezone().isoformat()})
            data["actual"] = actual or data["actual"]
            data["updated_at"] = datetime.now().astimezone().isoformat()
            self._save(run_id, data)
            return data

    def finish(self, run_id: str, succeeded: bool, detail: str = "") -> dict:
        with self._lock:
            data = self.read(run_id)
            if not data:
                return {}
            data["status"] = "callback_completed" if succeeded else "failed"
            data["actual"] = detail or ("执行回调成功；外部发送状态另行核对" if succeeded else "执行回调失败")
            data["updated_at"] = datetime.now().astimezone().isoformat()
            self._save(run_id, data)
            return data

    def interrupted(self) -> list[dict]:
        if not self.root.exists():
            return []
        results = []
        for path in list(self.root.glob("*.json"))[-1000:]:
            data = self.read(path.stem)
            if data.get("status") == "running":
                results.append(data)
        return results

    def resolve_interrupted(self, run_id: str, status: str, detail: str) -> dict:
        if status not in {"recovery_scheduled", "interrupted_unverified", "interrupted_needs_review"}:
            raise ValueError("无效的恢复状态")
        with self._lock:
            data = self.read(run_id)
            if not data or data.get("status") != "running":
                return data
            data.update(status=status, actual=detail, updated_at=datetime.now().astimezone().isoformat())
            self._save(run_id, data)
            return data

    @staticmethod
    def recovery_plan(data: dict, trace_events: list[dict] | None = None) -> str:
        if data.get("status") != "running":
            return "none"
        stages = {str(step.get("stage") or "") for step in data.get("steps") or []}
        trace_events = trace_events or []
        sent = "send_started" in stages or any(
            item.get("stage") == "微信接口发送" and item.get("status") in {"running", "accepted"}
            for item in trace_events
        )
        if sent:
            return "needs_verification"
        if data.get("mode") == "scheduled" and data.get("action_type") == "send_message":
            return "safe_retry"
        return "needs_review"
