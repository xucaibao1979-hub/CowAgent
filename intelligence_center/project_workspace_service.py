"""Persistent project workspaces for the web console."""
from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime


class ProjectWorkspaceService:
    SCHEMA_VERSION = 2

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, "project_workspaces.json")
        self.assets_root = os.path.join(root, "project_assets")
        self._lock = threading.RLock()
        os.makedirs(self.assets_root, exist_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {"schema_version": self.SCHEMA_VERSION, "projects": []}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data.get("projects"), list):
                data["projects"] = []
            return data
        except (OSError, ValueError, TypeError):
            return {"schema_version": self.SCHEMA_VERSION, "projects": []}

    def _write(self, data: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return deepcopy(sorted(self._read()["projects"], key=lambda x: (bool(x.get("pinned")), x.get("updated_at", "")), reverse=True))

    def get(self, project_id: str) -> dict | None:
        return next((item for item in self.list() if item.get("id") == project_id), None)

    def create(self, payload: dict) -> dict:
        now = self._now()
        item = {
            "id": uuid.uuid4().hex[:12], "name": str(payload.get("name") or "未命名项目").strip(),
            "description": str(payload.get("description") or "").strip(), "instruction": str(payload.get("instruction") or "").strip(),
            "status": "active", "pinned": bool(payload.get("pinned", False)), "model": str(payload.get("model") or ""), "approval_policy": payload.get("approval_policy") or "risk_based",
            "experts": list(payload.get("experts") or []), "skills": list(payload.get("skills") or []),
            "connectors": list(payload.get("connectors") or []), "automations": list(payload.get("automations") or []),
            "tasks": [], "assets": [], "members": [{"name": "Admin", "role": "owner"}],
            "created_at": now, "updated_at": now,
        }
        if not item["name"]:
            raise ValueError("项目名称不能为空")
        with self._lock:
            data = self._read(); data["projects"].append(item); self._write(data)
        os.makedirs(os.path.join(self.assets_root, item["id"]), exist_ok=True)
        return deepcopy(item)

    def update(self, project_id: str, payload: dict) -> dict:
        allowed = {"name", "description", "instruction", "status", "pinned", "model", "approval_policy", "experts", "skills", "connectors", "automations", "tasks", "assets", "members"}
        with self._lock:
            data = self._read()
            item = next((x for x in data["projects"] if x.get("id") == project_id), None)
            if item is None:
                raise KeyError("项目不存在")
            for key in allowed:
                if key in payload:
                    item[key] = deepcopy(payload[key])
            item["updated_at"] = self._now(); self._write(data)
            return deepcopy(item)

    def delete(self, project_id: str) -> bool:
        with self._lock:
            data = self._read(); before = len(data["projects"])
            data["projects"] = [x for x in data["projects"] if x.get("id") != project_id]
            if len(data["projects"]) == before:
                return False
            self._write(data); return True

    def add_task(self, project_id: str, payload: dict) -> dict:
        project = self.get(project_id)
        if project is None:
            raise KeyError("项目不存在")
        task = {"id": uuid.uuid4().hex[:10], "name": str(payload.get("name") or "新任务").strip(), "content": str(payload.get("content") or "").strip(), "status": "todo", "created_at": self._now(), "updated_at": self._now()}
        project["tasks"].insert(0, task)
        return self.update(project_id, {"tasks": project["tasks"]})

    def update_task(self, project_id: str, task_id: str, payload: dict) -> dict:
        project = self.get(project_id)
        if project is None:
            raise KeyError("项目不存在")
        task = next((item for item in project["tasks"] if item.get("id") == task_id), None)
        if task is None:
            raise KeyError("任务不存在")
        for key in ("name", "content", "status"):
            if key in payload:
                task[key] = str(payload[key]).strip()
        task["updated_at"] = self._now()
        return self.update(project_id, {"tasks": project["tasks"]})

    def delete_task(self, project_id: str, task_id: str) -> dict:
        project = self.get(project_id)
        if project is None:
            raise KeyError("项目不存在")
        tasks = [item for item in project["tasks"] if item.get("id") != task_id]
        if len(tasks) == len(project["tasks"]):
            raise KeyError("任务不存在")
        return self.update(project_id, {"tasks": tasks})


_service = None


def get_project_workspace_service(root: str):
    global _service
    if _service is None or _service.root != root:
        _service = ProjectWorkspaceService(root)
    return _service
