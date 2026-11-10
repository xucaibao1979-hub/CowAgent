"""Per-run task state assembled from the execution trace.

The trace stays free of prompts. Task goals live in the user's local data
directory, next to the existing conversation history, and never in the repo.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path


class TaskStateStore:
    _lock = threading.RLock()

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", str(run_id or "")):
            raise ValueError("无效的任务 ID")
        return self.root / f"{run_id}.json"

    def read(self, run_id: str) -> dict:
        try:
            value = json.loads(self._path(run_id).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, run_id: str, value: dict) -> None:
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def begin(self, run_id: str, *, goal: str, source: str, session_id: str,
              source_id: str = "", parent_run_id: str = "") -> dict:
        with self._lock:
            now = datetime.now().astimezone().isoformat()
            recipients = [item for item in re.findall(
                r"(?:给|向)\s*([\u4e00-\u9fffA-Za-z0-9_.-]{2,30})\s*(?:发送|发|传|通知)",
                str(goal or "")) if item not in {"自己", "我的", "我们"}]
            state = {"run_id": run_id, "goal": str(goal or "")[:1000],
                     "source": source, "source_id": source_id,
                     "session_id": session_id, "parent_run_id": parent_run_id,
                     "status": "running", "stage": "received", "delivery": "not_started",
                     "authorized_recipients": list(dict.fromkeys(recipients))[:5],
                     "steps": [], "created_at": now, "updated_at": now}
            self._save(run_id, state)
            return state

    def record(self, event: dict) -> dict:
        run_id = str(event.get("id") or "")
        with self._lock:
            state = self.read(run_id)
            if not state:
                now = str(event.get("at") or datetime.now().astimezone().isoformat())
                state = {"run_id": run_id, "goal": "", "source": event.get("source") or "",
                         "source_id": event.get("source_id") or "",
                         "session_id": event.get("session_id") or "", "parent_run_id": "",
                         "status": "running", "stage": "received", "delivery": "not_started",
                         "steps": [], "created_at": now}
            stage = str(event.get("stage") or "")
            status = str(event.get("status") or "")
            step = {key: event[key] for key in ("stage", "status", "at", "tool", "duration_ms",
                    "error_code", "artifact_id", "device_received") if key in event}
            if stage == "微信接口发送":
                step["expected"] = "微信发送接口明确接受请求"
                step["actual"] = {"accepted": "接口已接受", "unconfirmed": "接口未返回明确状态",
                                  "failed": "接口拒绝或发送失败"}.get(status, "等待接口结果")
                step["evidence_ref"] = str(event.get("artifact_id") or "")
                step["next_action"] = ("等待接收端核验" if status == "accepted" else
                                       "先核对接收端，再决定是否补发" if status == "unconfirmed" else
                                       "按错误类型诊断并恢复" if status == "failed" else "等待接口结果")
            elif stage == "设备确认收到":
                step.update(expected="接收端独立确认", actual="已确认收到" if status == "received" else "尚未确认",
                            next_action="结束任务" if status == "received" else "继续核验")
            elif stage == "工具执行":
                step.update(expected="工具完成当前步骤", actual="执行成功" if status == "completed" else "执行失败",
                            next_action="继续下一步" if status == "completed" else "按错误类型恢复")
            state["steps"].append(step)
            state["stage"] = stage
            state["updated_at"] = event.get("at") or datetime.now().astimezone().isoformat()
            if stage == "微信接口发送":
                state["delivery"] = {"accepted": "interface_accepted",
                                     "unconfirmed": "awaiting_verification",
                                     "failed": "send_failed"}.get(status, state["delivery"])
            elif stage == "设备确认收到" and status == "received" and event.get("device_received") is True:
                state["delivery"] = "received"
            if status in {"failed", "error"}:
                state["status"] = "awaiting_verification" if state["delivery"] == "awaiting_verification" else "failed"
            elif state["delivery"] == "received":
                state["status"] = "completed"
            elif state["delivery"] == "awaiting_verification":
                state["status"] = "awaiting_verification"
            elif status in {"completed", "accepted"} and stage != "工具执行":
                state["status"] = "interface_accepted" if state["delivery"] == "interface_accepted" else "completed"
            self._save(run_id, state)
            return state

    def link_parent(self, run_id: str, parent_run_id: str) -> dict:
        if not parent_run_id or run_id == parent_run_id:
            return self.read(run_id)
        with self._lock:
            state = self.read(run_id)
            if not state or not self.read(parent_run_id):
                return state
            state["parent_run_id"] = parent_run_id
            state["updated_at"] = datetime.now().astimezone().isoformat()
            self._save(run_id, state)
            return state

    def set_plan(self, run_id: str, steps: list[dict]) -> dict:
        """Persist a bounded plan before a long task begins external work."""
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError("计划需包含 1–20 个步骤")
        plan = []
        for index, item in enumerate(steps, 1):
            title = str((item or {}).get("title") or "").strip()
            if not title or len(title) > 200:
                raise ValueError("计划步骤缺少简短名称")
            plan.append({"id": str(index), "title": title,
                         "expected": str(item.get("expected") or "")[:500],
                         "status": "pending", "evidence": ""})
        with self._lock:
            state = self.read(run_id)
            if not state:
                raise ValueError("任务不存在")
            if state.get("plan") and any(item["status"] != "pending" for item in state["plan"]):
                raise ValueError("已有执行证据，不能覆盖计划")
            state["plan"] = plan
            state["next_step"] = "1"
            state["updated_at"] = datetime.now().astimezone().isoformat()
            self._save(run_id, state)
            return state

    def complete_step(self, run_id: str, step_id: str, evidence: str) -> dict:
        if not str(evidence or "").strip():
            raise ValueError("完成步骤需要证据引用")
        with self._lock:
            state = self.read(run_id)
            if not state:
                raise ValueError("任务不存在")
            plan = state.get("plan") or []
            step = next((item for item in plan if item["id"] == str(step_id)), None)
            if not step:
                raise ValueError("步骤不存在")
            if step["status"] == "completed":
                if step["evidence"] != str(evidence):
                    raise ValueError("步骤已完成，证据不能被覆盖")
                return state
            if state.get("next_step") != str(step_id):
                raise ValueError("请先完成前置步骤")
            step.update(status="completed", evidence=str(evidence)[:500])
            state["next_step"] = next((item["id"] for item in plan if item["status"] == "pending"), "")
            state["updated_at"] = datetime.now().astimezone().isoformat()
            self._save(run_id, state)
            return state

    @staticmethod
    def recovery_plan(state: dict) -> str:
        """A possible send is never replayed without independent verification."""
        if state.get("delivery") in {"awaiting_verification", "interface_accepted", "received"}:
            return "verify_before_retry"
        if any(step.get("stage") == "微信接口发送" for step in state.get("steps") or []):
            return "verify_before_retry"
        return "retry_read_only" if state.get("source") in {"web", "terminal"} else "review"

    def recover_interrupted(self) -> list[dict]:
        """Classify runs left in progress by a previous process.

        This never replays an external action; the next run can show the exact
        reason a send is waiting for verification.
        """
        if not self.root.exists():
            return []
        updated = []
        with self._lock:
            paths = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)[-1000:]
            for path in paths:
                state = self.read(path.stem)
                if state.get("status") != "running":
                    continue
                plan = self.recovery_plan(state)
                state["status"] = "awaiting_verification" if plan == "verify_before_retry" else "interrupted"
                state["recovery_plan"] = plan
                state["updated_at"] = datetime.now().astimezone().isoformat()
                self._save(path.stem, state)
                updated.append(state)
        return updated
