"""
Background scheduler service for executing scheduled tasks
"""

import os
import time
import threading
import json
import hashlib
import uuid
import subprocess
from datetime import datetime, timedelta
from typing import Callable, Optional
from croniter import croniter
from common.log import logger


def _parse_naive_local(iso_str: str) -> datetime:
    """Parse an ISO datetime and coerce it to tz-naive local time.

    The scheduler uses ``datetime.now()`` (tz-naive) for all comparisons,
    so any persisted timestamp must be normalized to the same flavor —
    otherwise comparing naive vs aware raises TypeError.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _pid_alive(pid: int) -> bool:
    try:
        pid = int(pid or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=3)
        return str(pid) in (result.stdout or "")
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def _listening_pids(port: int) -> set:
    pids = set()
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
        for line in (result.stdout or "").splitlines():
            if f":{port}" not in line or "LISTENING" not in line:
                continue
            parts = line.split()
            if parts:
                try:
                    pids.add(int(parts[-1]))
                except Exception:
                    pass
    except Exception:
        pass
    return pids


def _append_execution_audit(
    task: dict,
    stage: str,
    channel: str = "",
    receiver: str = "",
    status: str = "info",
    message: str = "",
    details: dict = None,
) -> None:
    """Best-effort execution breadcrumb without coupling scheduler startup to plugins."""
    try:
        try:
            from intelligence_center.execution_audit import append_execution_event
        except Exception:
            from plugins.intelligence_center.execution_audit import append_execution_event
        append_execution_event(task or {}, stage, channel, receiver, status, message, details or {})
    except Exception as error:
        logger.warning(f"[Scheduler] Execution audit skipped: {error}")


def _channel_preflight(task: dict, channel: str, receiver: str = "") -> dict:
    try:
        try:
            from intelligence_center.execution_audit import channel_preflight, request_channel_recovery
        except Exception:
            from plugins.intelligence_center.execution_audit import channel_preflight, request_channel_recovery
        health = channel_preflight(task or {}, channel, receiver) or {}
        if not health.get("ok", True) and channel in {"weixin", "wecom_bot"}:
            request_channel_recovery(channel, health.get("message") or "manual run preflight")
        return health
    except Exception as error:
        message = f"通道预检暂不可用，继续执行：{error}"
        _append_execution_audit(task or {}, "preflight", channel, receiver, "warning", message)
        return {"ok": True, "message": message}


class SchedulerService:
    """
    Background service that executes scheduled tasks
    """
    
    def __init__(self, task_store, execute_callback: Callable):
        """
        Initialize scheduler service
        
        Args:
            task_store: TaskStore instance
            execute_callback: Function to call when executing a task
        """
        self.task_store = task_store
        self.execute_callback = execute_callback
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._active_task_ids = set()
        self._active_task_locks = {}
        self._retry_after = {}
        self._retry_counts = {}
        self._last_instance_guard_warning = 0
    
    def start(self):
        """Start the scheduler service"""
        with self._lock:
            if self.running:
                logger.warning("[Scheduler] Service already running")
                return
            
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
    
    def stop(self):
        """Stop the scheduler service"""
        with self._lock:
            if not self.running:
                return
            
            self.running = False
            if self.thread:
                self.thread.join(timeout=5)
            logger.info("[Scheduler] Service stopped")
    
    def _run_loop(self):
        """Main scheduler loop"""
        logger.info("[Scheduler] Scheduler loop started")
        
        while self.running:
            try:
                self._check_and_execute_tasks()
            except Exception as e:
                logger.error(f"[Scheduler] Error in scheduler loop: {e}")

            time.sleep(30)
    
    def _check_and_execute_tasks(self):
        """Check for due tasks and execute them"""
        now = datetime.now()
        self._cleanup_stale_scheduler_locks()
        tasks = self.task_store.list_tasks(enabled_only=True)
        duplicate_keys_seen = set()
        
        for task in tasks:
            try:
                duplicate_key = self._duplicate_task_key(task)
                if duplicate_key in duplicate_keys_seen:
                    logger.warning(
                        f"[Scheduler] Duplicate enabled task skipped: {task.get('id')} - {task.get('name')}"
                    )
                    updates = {
                        "last_run_at": now.isoformat(),
                        "last_run_status": "skipped_duplicate",
                        "last_result_summary": "检测到同名同内容重复任务，本次已跳过；请在任务列表保留一条即可。",
                        "last_error": "",
                    }
                    self.task_store.update_task(
                        task['id'],
                        self._append_run_history(task, updates, "scheduled", "", now, now),
                    )
                    continue
                duplicate_keys_seen.add(duplicate_key)
                if self._is_task_due(task, now):
                    task["_run_id"] = f"scheduled-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
                    retry_after = self._retry_after.get(task['id'])
                    if retry_after and now < retry_after:
                        continue
                    logger.info(f"[Scheduler] Executing task: {task['id']} - {task['name']}")
                    self._cleanup_stale_scheduler_locks(task)
                    if not self._claim_task(task['id']):
                        _append_execution_audit(
                            task,
                            "lock_claim",
                            "",
                            "",
                            "warning",
                            "任务仍有有效执行锁，本次调度跳过，避免重复执行",
                        )
                        logger.info(
                            f"[Scheduler] Task {task['id']} is already running; skipping this tick"
                        )
                        continue
                    if not self._claim_due_window(task, now):
                        _append_execution_audit(
                            task,
                            "due_window",
                            "",
                            "",
                            "warning",
                            "当前调度时间窗口已执行过，本次跳过重复触发",
                        )
                        self._release_task(task['id'])
                        logger.info(
                            f"[Scheduler] Task {task['id']} already executed in this due window; skipping duplicate tick"
                        )
                        continue
                    valid_instance, guard_reason = self._runtime_instance_guard()
                    if not valid_instance:
                        self._skip_task_for_instance_guard(task, guard_reason)
                        next_run = self._calculate_next_run(task, now)
                        if next_run:
                            self.task_store.update_task(task['id'], {"next_run_at": next_run.isoformat()})
                        self._release_task(task['id'])
                        continue
                    try:
                        ok = self._execute_task(task)
                        if not ok:
                            finished_at = datetime.now()
                            action = task.get("action") if isinstance(task.get("action"), dict) else {}
                            if action.get("type") == "send_message":
                                # A time-sensitive reminder is one occurrence, not a
                                # request to re-send the same text every 30-300 seconds.
                                # The dispatcher has already stored its Web fallback.
                                # Keep the primary failure visible and move to the next
                                # scheduled window so the next reminder can run on time.
                                self._retry_after.pop(task['id'], None)
                                self._retry_counts.pop(task['id'], None)
                                updates = self._execution_status_update(task, finished_at, "failed")
                                next_run = self._calculate_next_run(task, now)
                                if next_run:
                                    updates["next_run_at"] = next_run.isoformat()
                                else:
                                    updates["enabled"] = False
                                    updates["next_run_at"] = None
                                self.task_store.update_task(
                                    task['id'],
                                    self._append_run_history(task, updates, "scheduled", "", now, finished_at),
                                )
                                logger.warning(
                                    "[Scheduler] Task %s reminder delivery failed once; recorded failure and advanced schedule",
                                    task['id'],
                                )
                                continue
                            self._release_due_window(task)
                            updates = self._execution_status_update(task, finished_at, "retrying")
                            self.task_store.update_task(
                                task['id'],
                                self._append_run_history(task, updates, "scheduled", "", now, finished_at),
                            )
                            count = self._retry_counts.get(task['id'], 0) + 1
                            self._retry_counts[task['id']] = count
                            delay = min(300, 30 * (2 ** min(count - 1, 4)))
                            self._retry_after[task['id']] = now + timedelta(seconds=delay)
                            logger.warning(
                                f"[Scheduler] Task {task['id']} delivery failed; retrying in {delay}s"
                            )
                            continue

                        self._retry_after.pop(task['id'], None)
                        self._retry_counts.pop(task['id'], None)

                        next_run = self._calculate_next_run(task, now)
                        if next_run:
                            finished_at = datetime.now()
                            updates = self._execution_status_update(task, finished_at, "success")
                            updates["next_run_at"] = next_run.isoformat()
                            self.task_store.update_task(
                                task['id'],
                                self._append_run_history(task, updates, "scheduled", "", now, finished_at),
                            )
                        else:
                            self.task_store.delete_task(task['id'])
                            logger.info(f"[Scheduler] One-time task completed and removed: {task['id']}")
                    finally:
                        self._release_task(task['id'])
            except Exception as e:
                logger.error(f"[Scheduler] Error processing task {task.get('id')}: {e}")

    def run_task_now(self, task_id: str) -> str:
        """Queue one immediate execution without changing the task schedule.

        Disabled and one-time tasks may be run manually for testing. The
        stored ``next_run_at`` remains unchanged, so a manual run never
        consumes or delays the next scheduled occurrence.

        Raises:
            ValueError: if the task does not exist.
            RuntimeError: if the same task is already executing.
        """
        task = self.task_store.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")
        run_id = f"manual-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        started_at = datetime.now()
        task["_run_id"] = run_id
        self._cleanup_stale_scheduler_locks(task, run_id)
        self._manual_run_preflight(task, run_id)
        if not self._claim_task(task_id):
            message = "该任务已有未完成的执行记录，可能是正在执行或锁文件未过期；已记录到执行轨迹。"
            _append_execution_audit(task, "lock_claim", "", "", "failed", message)
            updates = {
                "last_manual_run_at": started_at.isoformat(),
                "last_manual_run_id": run_id,
                "last_manual_run_status": "failed",
                "last_manual_run_summary": message,
                "last_manual_run_started_at": started_at.isoformat(),
                "last_manual_run_finished_at": started_at.isoformat(),
                "last_run_status": "failed",
                "last_result_summary": message,
                "last_error": message,
                "last_error_at": started_at.isoformat(),
            }
            self.task_store.update_task(
                task_id,
                self._append_run_history(task, updates, "manual", run_id, started_at, started_at),
            )
            raise RuntimeError(message)
        _append_execution_audit(task, "lock_claim", "", "", "success", "已取得任务执行锁，可以开始手动执行")
        self.task_store.update_task(task_id, {
            "last_manual_run_id": run_id,
            "last_manual_run_status": "running",
            "last_manual_run_summary": "手动执行已开始",
            "last_manual_run_started_at": started_at.isoformat(),
            "last_manual_run_finished_at": "",
        })

        def _run():
            try:
                now = datetime.now()
                task["_run_id"] = run_id
                logger.info(f"[Scheduler] Manually executing task: {task_id} - {task.get('name', '')}")
                valid_instance, guard_reason = self._runtime_instance_guard()
                if not valid_instance:
                    _append_execution_audit(task, "instance_guard", "", "", "failed", guard_reason)
                    finished_at = datetime.now()
                    self._skip_task_for_instance_guard(task, guard_reason)
                    self.task_store.update_task(task_id, {
                        "last_manual_run_at": finished_at.isoformat(),
                        "last_manual_run_id": run_id,
                        "last_manual_run_status": "skipped_instance",
                        "last_manual_run_summary": guard_reason,
                        "last_manual_run_finished_at": finished_at.isoformat(),
                    })
                    return
                _append_execution_audit(task, "instance_guard", "", "", "success", "当前后端实例有效，继续执行任务")
                ok = self._execute_task(task)
                finished_at = datetime.now()
                if ok:
                    updates = self._execution_status_update(task, finished_at, "success")
                    updates["last_manual_run_at"] = finished_at.isoformat()
                    updates["last_manual_run_id"] = run_id
                    updates["last_manual_run_status"] = updates.get("last_run_status", "success")
                    updates["last_manual_run_summary"] = updates.get("last_result_summary", "执行完成")
                    updates["last_manual_run_finished_at"] = finished_at.isoformat()
                    self.task_store.update_task(
                        task_id,
                        self._append_run_history(task, updates, "manual", run_id, started_at, finished_at),
                    )
                    logger.info(f"[Scheduler] Manual execution completed: {task_id}")
                else:
                    updates = self._execution_status_update(task, finished_at, "failed")
                    updates["last_manual_run_at"] = finished_at.isoformat()
                    updates["last_manual_run_id"] = run_id
                    updates["last_manual_run_status"] = updates.get("last_run_status", "failed")
                    updates["last_manual_run_summary"] = updates.get("last_result_summary", "执行失败")
                    updates["last_manual_run_finished_at"] = finished_at.isoformat()
                    self.task_store.update_task(
                        task_id,
                        self._append_run_history(task, updates, "manual", run_id, started_at, finished_at),
                    )
                    logger.warning(f"[Scheduler] Manual execution failed: {task_id}")
            except Exception as error:
                finished_at = datetime.now()
                summary = f"手动执行异常：{error}"
                logger.error(f"[Scheduler] Manual execution crashed: {task_id}: {error}")
                updates = {
                    "last_run_at": finished_at.isoformat(),
                    "last_run_status": "failed",
                    "last_result_summary": summary,
                    "last_error": summary,
                    "last_error_at": finished_at.isoformat(),
                    "last_manual_run_at": finished_at.isoformat(),
                    "last_manual_run_id": run_id,
                    "last_manual_run_status": "failed",
                    "last_manual_run_summary": summary,
                    "last_manual_run_finished_at": finished_at.isoformat(),
                }
                self.task_store.update_task(
                    task_id,
                    self._append_run_history(task, updates, "manual", run_id, started_at, finished_at),
                )
            finally:
                task.pop("_run_id", None)
                self._release_task(task_id)

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"scheduler-manual-{task_id}",
        ).start()
        return run_id

    def _scheduler_base_dir(self) -> str:
        base_dir = os.path.dirname(getattr(self.task_store, "store_path", "") or "")
        return base_dir or os.getcwd()

    def _stale_scheduler_lock_files(self, max_age_seconds: int = 1800) -> list:
        base_dir = os.path.abspath(self._scheduler_base_dir())
        roots = [
            os.path.join(base_dir, "locks"),
            os.path.join(base_dir, "execution_locks"),
            os.path.join(base_dir, "run_windows"),
        ]
        now = time.time()
        files = []
        for root in roots:
            root_abs = os.path.abspath(root)
            if not (root_abs == base_dir or root_abs.startswith(base_dir + os.sep)):
                continue
            if not os.path.isdir(root_abs):
                continue
            for name in os.listdir(root_abs):
                path = os.path.abspath(os.path.join(root_abs, name))
                if not path.startswith(root_abs + os.sep) or not os.path.isfile(path):
                    continue
                if not (name.endswith(".lock") or name.endswith(".json")):
                    continue
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    continue
                if age >= max_age_seconds:
                    files.append({"path": path, "age_seconds": int(age), "root": os.path.basename(root_abs)})
        return files

    def _cleanup_stale_scheduler_locks(self, task: dict = None, run_id: str = "", max_age_seconds: int = 1800) -> list:
        stale = self._stale_scheduler_lock_files(max_age_seconds=max_age_seconds)
        if not stale:
            if task:
                _append_execution_audit(
                    {**task, "_run_id": run_id or task.get("_run_id", "")},
                    "lock_cleanup",
                    "",
                    "",
                    "success",
                    "未发现需要清理的过期调度锁",
                )
            return []
        removed = []
        for item in stale:
            path = item.get("path") or ""
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                continue
        audit_task = {**(task or {"id": "scheduler-lock-cleanup", "name": "调度锁清理"}), "_run_id": run_id or (task or {}).get("_run_id", "")}
        _append_execution_audit(
            audit_task,
            "lock_cleanup",
            "",
            "",
            "success" if removed else "warning",
            f"已清理 {len(removed)} 个过期调度锁" if removed else f"发现 {len(stale)} 个过期调度锁但清理失败",
            {"removed": removed[:20], "stale_count": len(stale), "max_age_seconds": max_age_seconds},
        )
        return removed

    def _manual_run_preflight(self, task: dict, run_id: str = "") -> None:
        audit_task = {**(task or {}), "_run_id": run_id or (task or {}).get("_run_id", "")}
        _append_execution_audit(audit_task, "manual_preflight", "", "", "success", "手动执行前预检已开始")
        valid_instance, guard_reason = self._runtime_instance_guard()
        _append_execution_audit(
            audit_task,
            "instance_guard",
            "",
            "",
            "success" if valid_instance else "failed",
            guard_reason,
        )
        action = task.get("action") if isinstance(task.get("action"), dict) else {}
        channel = str(action.get("channel_type") or "").strip()
        receiver = str(action.get("notify_session_id") or action.get("receiver") or "").strip()
        if channel:
            health = _channel_preflight(audit_task, channel, receiver)
            if health.get("ok", True):
                _append_execution_audit(audit_task, "manual_preflight", channel, receiver, "success", "通知通道预检通过")
            else:
                _append_execution_audit(
                    audit_task,
                    "manual_preflight",
                    channel,
                    receiver,
                    "warning",
                    f"通知通道预检有风险：{health.get('message') or '通道暂不可用'}",
                    {"next_action": health.get("next_action") or ""},
                )

    def _claim_task(self, task_id: str) -> bool:
        """Prevent scheduled and manual runs of the same task from overlapping.

        The in-memory set protects one backend process. The lock file protects
        the common desktop case where multiple CowAgent backends point at the
        same scheduler file and all see the same due task.
        """
        with self._execution_lock:
            if task_id in self._active_task_ids:
                return False
            lock_path = self._execution_lock_path(task_id)
            token = f"{os.getpid()}:{time.time()}"
            try:
                os.makedirs(os.path.dirname(lock_path), exist_ok=True)
                if os.path.exists(lock_path):
                    try:
                        age = time.time() - os.path.getmtime(lock_path)
                    except OSError:
                        age = 0
                    if age < 1800:
                        return False
                    try:
                        os.remove(lock_path)
                    except OSError:
                        return False
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(token)
                self._active_task_locks[task_id] = (lock_path, token)
            except FileExistsError:
                return False
            except Exception as exc:
                logger.warning(f"[Scheduler] Failed to create task lock for {task_id}: {exc}")
                return False
            self._active_task_ids.add(task_id)
            return True

    def _release_task(self, task_id: str) -> None:
        with self._execution_lock:
            self._active_task_ids.discard(task_id)
            lock_info = self._active_task_locks.pop(task_id, None)
            if lock_info:
                lock_path, token = lock_info
                try:
                    current = ""
                    if os.path.exists(lock_path):
                        with open(lock_path, "r", encoding="utf-8") as f:
                            current = f.read()
                    if current == token:
                        os.remove(lock_path)
                except Exception as exc:
                    logger.warning(f"[Scheduler] Failed to release task lock for {task_id}: {exc}")

    def _execution_lock_path(self, task_id: str) -> str:
        safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(task_id or "task"))
        base_dir = os.path.dirname(getattr(self.task_store, "store_path", "") or "")
        return os.path.join(base_dir or os.getcwd(), "locks", f"{safe_id}.lock")

    def _instance_lock_path(self) -> str:
        workspace = os.path.dirname(os.path.dirname(getattr(self.task_store, "store_path", "") or ""))
        return os.path.join(workspace or os.getcwd(), ".cow_runtime", "backend_instance.lock")

    def _runtime_instance_guard(self) -> tuple[bool, str]:
        """Return whether this process is still the active backend instance.

        This is checked immediately before a task is executed. It prevents old
        backend processes from continuing to run scheduler jobs after a restart
        or after losing the Web/WeCom ownership.
        """
        current_pid = os.getpid()
        lock_path = self._instance_lock_path()
        if not os.path.exists(lock_path):
            return False, "未找到后端单实例锁，跳过本次自动化执行以避免旧实例重复触发"
        try:
            with open(lock_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as error:
            return False, f"后端单实例锁读取失败：{error}"
        lock_pid = int(data.get("pid") or 0)
        if lock_pid != current_pid:
            state = "仍存活" if _pid_alive(lock_pid) else "已失效"
            return False, f"当前进程不是有效后端实例，锁归属 PID {lock_pid}（{state}），当前 PID {current_pid}"
        port = int(data.get("web_port") or 9899)
        pids = _listening_pids(port)
        if pids and current_pid not in pids:
            return False, f"Web 端口 {port} 已被其它进程接管：{', '.join(map(str, sorted(pids)))}"
        return True, "当前实例有效"

    def _skip_task_for_instance_guard(self, task: dict, reason: str) -> None:
        now = datetime.now()
        if time.time() - self._last_instance_guard_warning > 30:
            logger.warning(f"[Scheduler] Instance guard skipped task execution: {reason}")
            self._last_instance_guard_warning = time.time()
        try:
            updates = {
                "last_run_at": now.isoformat(),
                "last_run_status": "skipped_instance",
                "last_result_summary": reason,
                "last_error": "",
                "last_error_at": "",
            }
            self.task_store.update_task(
                task["id"],
                self._append_run_history(task, updates, "scheduled", "", now, now),
            )
        except Exception:
            pass

    def _claim_due_window(self, task: dict, now: datetime) -> bool:
        """Prevent duplicate executions for the same logical schedule window.

        The per-task lock prevents concurrent overlap. This guard prevents
        repeated sequential executions when duplicate backend instances, quick
        restarts, or stale UI actions see the same due time.
        """
        task_id = str(task.get("id") or "task")
        next_run = str(task.get("next_run_at") or "")
        try:
            due = _parse_naive_local(next_run) if next_run else now
        except Exception:
            due = now
        window = due.strftime("%Y%m%d%H%M")
        action = task.get("action") if isinstance(task.get("action"), dict) else {}
        fingerprint_payload = {
            "id": task_id,
            "name": task.get("name") or "",
            "schedule": task.get("schedule") or {},
            "action": {
                "type": action.get("type"),
                "channel_type": action.get("channel_type"),
                "receiver": action.get("receiver"),
                "task_description": action.get("task_description"),
                "content": action.get("content"),
            },
            "window": window,
        }
        digest = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        base_dir = os.path.dirname(getattr(self.task_store, "store_path", "") or "")
        guard_dir = os.path.join(base_dir or os.getcwd(), "run_windows")
        guard_path = os.path.join(guard_dir, f"{digest}.json")
        try:
            os.makedirs(guard_dir, exist_ok=True)
            if os.path.exists(guard_path):
                age = time.time() - os.path.getmtime(guard_path)
                if age < 3600:
                    return False
            with open(guard_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "task_id": task_id,
                    "task_name": task.get("name") or "",
                    "next_run_at": next_run,
                    "window": window,
                    "claimed_at": now.isoformat(),
                    "pid": os.getpid(),
                }, handle, ensure_ascii=False, indent=2)
            task["_due_window_guard_path"] = guard_path
            self._cleanup_due_window_guards(guard_dir)
            return True
        except Exception as exc:
            logger.warning(f"[Scheduler] Failed to claim due window for {task_id}: {exc}")
            return True

    @staticmethod
    def _release_due_window(task: dict) -> None:
        """Release the current schedule-window guard for retryable failures.

        A transient delivery failure (for example WeChat session tokens not
        restored yet after restart) should not consume the logical schedule
        window.  Otherwise every retry tick is treated as a duplicate and the
        task never gets a chance to recover within the 10-minute catch-up
        window.
        """
        guard_path = str(task.get("_due_window_guard_path") or "")
        if not guard_path:
            return
        try:
            if os.path.exists(guard_path):
                os.remove(guard_path)
        except OSError as exc:
            logger.warning(f"[Scheduler] Failed to release due window for retry: {exc}")
        finally:
            task.pop("_due_window_guard_path", None)

    @staticmethod
    def _cleanup_due_window_guards(guard_dir: str) -> None:
        try:
            cutoff = time.time() - 86400
            for name in os.listdir(guard_dir):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(guard_dir, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    continue
        except Exception:
            pass

    @staticmethod
    def _duplicate_task_key(task: dict) -> str:
        action = task.get("action") if isinstance(task.get("action"), dict) else {}
        schedule = task.get("schedule") if isinstance(task.get("schedule"), dict) else {}
        payload = {
            "name": str(task.get("name") or "").strip(),
            "schedule": schedule,
            "action_type": action.get("type"),
            "channel_type": action.get("channel_type"),
            "receiver": action.get("receiver"),
            "task_description": " ".join(str(action.get("task_description") or "").split()),
            "content": " ".join(str(action.get("content") or "").split()),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _execution_status_update(task: dict, now: datetime, fallback_status: str) -> dict:
        status = task.get("_last_run_status") or fallback_status
        if status not in {"success", "failed", "empty", "skipped_instance", "retrying"}:
            status = fallback_status
        updates = {
            "last_run_at": now.isoformat(),
            "last_run_status": status,
            "last_result_summary": task.get("_last_result_summary") or (
                "执行失败" if status == "failed" else (
                    "通道暂未就绪，等待自动重试" if status == "retrying" else (
                        "暂无内容" if status == "empty" else (
                            "旧实例已跳过" if status == "skipped_instance" else "执行完成"
                        )
                    )
                )
            ),
        }
        if task.get("_last_artifact_path"):
            updates["last_artifact_path"] = task.get("_last_artifact_path")
        if task.get("_last_delivery_summary"):
            updates["last_delivery_summary"] = task.get("_last_delivery_summary")
        action = task.get("action")
        if isinstance(action, dict):
            clean_action = {key: value for key, value in action.items() if not str(key).startswith("_")}
            updates["action"] = clean_action
        if status == "failed":
            updates["last_error"] = updates["last_result_summary"]
            updates["last_error_at"] = now.isoformat()
        elif status == "retrying":
            updates["last_error"] = ""
        else:
            updates["last_error"] = ""
            updates["last_error_at"] = ""
        return updates

    @staticmethod
    def _append_run_history(
        task: dict,
        updates: dict,
        trigger: str = "scheduled",
        run_id: str = "",
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
    ) -> dict:
        """Append one visible scheduler run history item to the task record."""
        finished_at = finished_at or datetime.now()
        started_at = started_at or finished_at
        status = updates.get("last_run_status") or updates.get("last_manual_run_status") or "unknown"
        summary = updates.get("last_result_summary") or updates.get("last_manual_run_summary") or updates.get("last_error") or ""
        history = list(task.get("run_history") or [])
        item_id = run_id or task.get("_run_id") or f"{trigger}-{finished_at.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        item = {
            "id": item_id,
            "run_id": item_id,
            "task_id": str(task.get("id") or ""),
            "task_name": str(task.get("name") or "未命名任务"),
            "trigger": trigger or "scheduled",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "status": status,
            "summary": summary,
            "artifact_path": updates.get("last_artifact_path") or task.get("_last_artifact_path") or task.get("last_artifact_path") or "",
            "delivery_summary": updates.get("last_delivery_summary") or task.get("_last_delivery_summary") or "",
            "delivery_audit": list(task.get("_delivery_audit") or [])[-8:],
        }
        updates["run_history"] = [item] + [old for old in history if old.get("id") != item["id"]][:199]
        return updates
    
    def _is_task_due(self, task: dict, now: datetime) -> bool:
        """
        Check if a task is due to run
        
        Args:
            task: Task dictionary
            now: Current datetime
            
        Returns:
            True if task should run now
        """
        next_run_str = task.get("next_run_at")
        if not next_run_str:
            # Calculate initial next_run_at
            next_run = self._calculate_next_run(task, now)
            if next_run:
                self.task_store.update_task(task['id'], {
                    "next_run_at": next_run.isoformat()
                })
                return False
            return False
        
        try:
            next_run = _parse_naive_local(next_run_str)

            if next_run < now:
                time_diff = (now - next_run).total_seconds()
                schedule = task.get("schedule", {})
                schedule_type = schedule.get("type")

                # Catch-up window: fire if we're within 10 minutes of the
                # scheduled tick. Beyond that we'd rather skip than push a
                # stale daily report to the user.
                if time_diff <= 600:
                    return True

                logger.warning(
                    f"[Scheduler] Task {task['id']} is overdue by {int(time_diff)}s, "
                    f"skipping and scheduling next run"
                )

                if schedule_type == "once":
                    self.task_store.delete_task(task['id'])
                    logger.info(f"[Scheduler] One-time task {task['id']} expired, removed")
                    return False

                next_next_run = self._calculate_next_run(task, now)
                if next_next_run:
                    self.task_store.update_task(task['id'], {
                        "next_run_at": next_next_run.isoformat()
                    })
                    logger.info(f"[Scheduler] Rescheduled task {task['id']} to {next_next_run}")
                return False

            return now >= next_run
        except Exception as e:
            logger.error(
                f"[Scheduler] Failed to evaluate due-state for task "
                f"{task.get('id')} (next_run_at={next_run_str!r}): {e}"
            )
            return False
    
    def _calculate_next_run(self, task: dict, from_time: datetime) -> Optional[datetime]:
        """
        Calculate next run time for a task
        
        Args:
            task: Task dictionary
            from_time: Calculate from this time
            
        Returns:
            Next run datetime or None for one-time tasks
        """
        schedule = task.get("schedule", {})
        schedule_type = schedule.get("type")
        
        if schedule_type == "cron":
            # Cron expression
            expression = schedule.get("expression")
            if not expression:
                return None
            
            try:
                cron = croniter(expression, from_time)
                return cron.get_next(datetime)
            except Exception as e:
                logger.error(f"[Scheduler] Invalid cron expression '{expression}': {e}")
                return None
        
        elif schedule_type == "interval":
            # Interval in seconds
            seconds = schedule.get("seconds", 0)
            if seconds <= 0:
                return None
            return from_time + timedelta(seconds=seconds)
        
        elif schedule_type == "once":
            # One-time task at specific time
            run_at_str = schedule.get("run_at")
            if not run_at_str:
                return None
            
            try:
                run_at = _parse_naive_local(run_at_str)
                if run_at > from_time:
                    return run_at
            except Exception as e:
                logger.error(
                    f"[Scheduler] Failed to parse once-task run_at "
                    f"{run_at_str!r}: {e}"
                )
            return None
        
        return None
    
    def _execute_task(self, task: dict) -> bool:
        """
        Execute a task.

        Returns True if delivery succeeded (caller should advance state),
        False if it failed (caller should keep next_run_at so the next
        loop iteration retries). Callback may return None for legacy
        behaviour, treated as success.
        """
        try:
            result = self.execute_callback(task)
            return False if result is False else True
        except Exception as e:
            logger.error(f"[Scheduler] Error executing task {task['id']}: {e}")
            self.task_store.update_task(task['id'], {
                "last_run_at": datetime.now().isoformat(),
                "last_run_status": "failed",
                "last_result_summary": str(e),
                "last_error": str(e),
                "last_error_at": datetime.now().isoformat()
            })
            return False
