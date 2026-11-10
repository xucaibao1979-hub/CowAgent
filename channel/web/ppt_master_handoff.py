"""Resume a web Agent after ppt-master's two user-confirmation receipts.

The confirmation page is a separate local server.  An Agent run may finish
while that page is waiting for its next file, so a Web request cannot rely on
the model remembering to keep a shell wait open.  This watcher never confirms
anything for the user; it only resumes the same session after a real receipt.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from common.log import logger


_SERVER_COMMAND = re.compile(
    r"confirm_ui[\\/]server\.py[\"']?\s+[\"'](?P<project>[^\"']+)[\"']",
    re.IGNORECASE,
)
_STATE_FILE = ".cowagent_handoff.json"


def project_from_daemon_command(command: str, workspace_root: str) -> Optional[Path]:
    """Accept only a local ppt-master project beneath this Agent's workspace."""
    command = str(command or "")
    if "--daemon" not in command:
        return None
    match = _SERVER_COMMAND.search(command)
    if not match:
        return None
    project = Path(match.group("project")).resolve()
    projects_root = (Path(workspace_root).resolve() / "projects")
    try:
        project.relative_to(projects_root)
    except ValueError:
        return None
    return project if project.is_dir() else None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class PptMasterHandoff:
    def __init__(self, workspace_root: str, dispatch: Callable[[str, str, str], None],
                 *, on_failure: Optional[Callable[[str, str, str], None]] = None,
                 poll_seconds: float = 3, wait_seconds: float = 24 * 3600):
        self.projects_root = Path(workspace_root).resolve() / "projects"
        self.dispatch = dispatch
        self.on_failure = on_failure
        self.poll_seconds = poll_seconds
        self.wait_seconds = wait_seconds
        self._session_projects: dict[str, Path] = {}
        self._workers: dict[Path, threading.Thread] = {}
        self._lock = threading.RLock()

    def register(self, project: Path, session_id: str, agent_id: str = "") -> None:
        project = Path(project).resolve()
        try:
            project.relative_to(self.projects_root)
        except ValueError:
            return
        if not project.is_dir() or not session_id:
            return
        path = project / "confirm_ui" / _STATE_FILE
        with self._lock:
            state = _read_json(path)
            state.update({"project": str(project), "session_id": session_id,
                          "agent_id": agent_id or "", "status": "waiting"})
            state.setdefault("stage1_attempts", 0)
            state.setdefault("final_attempts", 0)
            _write_json(path, state)
            self._session_projects[session_id] = project

    def start_for_session(self, session_id: str) -> bool:
        with self._lock:
            project = self._session_projects.get(session_id)
        return self.start(project) if project else False

    def start(self, project: Optional[Path]) -> bool:
        if not project:
            return False
        project = Path(project).resolve()
        path = project / "confirm_ui" / _STATE_FILE
        if not _read_json(path):
            return False
        with self._lock:
            existing = self._workers.get(project)
            if existing and existing.is_alive():
                return False
            worker = threading.Thread(target=self._watch, args=(project,),
                                      name="ppt-master-handoff", daemon=True)
            self._workers[project] = worker
            worker.start()
        return True

    def resume_pending(self) -> None:
        if not self.projects_root.is_dir():
            return
        for path in self.projects_root.glob(f"*/confirm_ui/{_STATE_FILE}"):
            state = _read_json(path)
            if state.get("status") not in {"complete", "failed"}:
                project = path.parent.parent.resolve()
                session_id = str(state.get("session_id") or "")
                if session_id:
                    with self._lock:
                        self._session_projects[session_id] = project
                    self.start(project)

    def _watch(self, project: Path) -> None:
        confirm = project / "confirm_ui"
        path = confirm / _STATE_FILE
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            state = _read_json(path)
            session_id = str(state.get("session_id") or "")
            agent_id = str(state.get("agent_id") or "")
            result = _read_json(confirm / "result.json")
            selection = _read_json(confirm / "template_selection.json")
            stage2 = _read_json(confirm / "recommendations.stage2.json")
            exported = list((project / "exports").glob("*.pptx"))
            if exported:
                state["status"] = "complete"
                _write_json(path, state)
                return
            if result.get("stage") == "final" and result.get("status") == "confirmed":
                progress_files = list((project / "svg_output").glob("*.svg"))
                progress_files += list((project / "validation").glob("*.json"))
                progress_signature = ",".join(
                    f"{item.name}:{item.stat().st_mtime_ns}:{item.stat().st_size}"
                    for item in sorted(progress_files)
                )
                if state.get("final_progress_signature") == progress_signature:
                    state["final_stalled_attempts"] = int(state.get("final_stalled_attempts") or 0) + 1
                else:
                    state["final_stalled_attempts"] = 0
                if (int(state.get("final_attempts") or 0) >= 5
                        or int(state.get("final_stalled_attempts") or 0) >= 2):
                    state["status"] = "failed"
                    state["reason"] = "final_export_missing"
                    _write_json(path, state)
                    logger.warning("[PptMasterHandoff] final continuation failed: %s", project)
                    self._notify_failure(session_id, agent_id, "最终确认后仍未导出 PPTX")
                    return
                state["final_attempts"] = int(state.get("final_attempts") or 0) + 1
                state["final_progress_signature"] = progress_signature
                state["status"] = "resuming_final"
                _write_json(path, state)
                prompt = (f"[自动续接] 用户已在 ppt-master 确认页最终确认方案。"
                          f"从现有项目 {project} 继续制作、质检并导出 PPTX。"
                          "先核对确认回执和已有产物，避免重复生成；完成后直接交付文件。")
                self._dispatch(session_id, agent_id, prompt)
            elif (result.get("stage") == "stage1"
                  and result.get("status") == "stage1-confirmed"
                  and selection.get("status") == "confirmed"
                  and stage2.get("stage") != "stage2"):
                if int(state.get("stage1_attempts") or 0) >= 2:
                    state["status"] = "failed"
                    state["reason"] = "stage2_recommendations_missing"
                    _write_json(path, state)
                    logger.warning("[PptMasterHandoff] stage2 recommendation failed: %s", project)
                    self._notify_failure(session_id, agent_id, "第一阶段确认后仍未生成最终方案")
                    return
                state["stage1_attempts"] = int(state.get("stage1_attempts") or 0) + 1
                state["status"] = "resuming_stage1"
                _write_json(path, state)
                prompt = (f"[自动续接] 用户已在 ppt-master 确认页提交第一阶段。"
                          f"从现有项目 {project} 和真实确认回执继续，生成第二阶段方案文件，"
                          "让同一确认页显示最终方案并等待用户确认。不要重做第一阶段或替用户提交确认。")
                self._dispatch(session_id, agent_id, prompt)
            time.sleep(self.poll_seconds)
        state = _read_json(path)
        state["status"] = "awaiting_user"
        _write_json(path, state)

    def _dispatch(self, session_id: str, agent_id: str, prompt: str) -> None:
        try:
            self.dispatch(session_id, agent_id, prompt)
        except Exception as error:
            logger.warning("[PptMasterHandoff] continuation failed: %s", error)

    def _notify_failure(self, session_id: str, agent_id: str, reason: str) -> None:
        if not self.on_failure:
            return
        try:
            self.on_failure(session_id, agent_id, reason)
        except Exception as error:
            logger.warning("[PptMasterHandoff] failure notification failed: %s", error)
