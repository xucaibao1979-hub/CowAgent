"""Unified artifact index for automation reports and future task outputs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from datetime import datetime


class ArtifactService:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.index_path = os.path.join(self.root, "artifacts.json")
        self.report_root = os.path.join(self.root, "reports")
        project_root = os.path.dirname(self.root)
        self.workspace_root = os.path.join(project_root, "work", "cow-workspace")
        self.deliverables_root = os.path.join(self.workspace_root, "deliverables")
        self.knowledge_sources_root = os.path.join(self.workspace_root, "knowledge", "sources")
        self.downloads_root = os.path.join(self.workspace_root, "downloads")
        self._lock = threading.RLock()

    def _is_safe_path(self, path: str) -> bool:
        path = os.path.abspath(str(path or ""))
        allowed_roots = [self.root, self.deliverables_root, self.knowledge_sources_root, self.downloads_root]
        return any(path == root or path.startswith(root + os.sep) for root in allowed_roots)

    def _read_index(self) -> list[dict]:
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data.get("artifacts", []) if isinstance(data, dict) else []
        except (OSError, ValueError, TypeError):
            return []

    def _write_index(self, items: list[dict]) -> None:
        temp = self.index_path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "artifacts": items}, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.index_path)

    @staticmethod
    def _id(path: str) -> str:
        return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _content_preview(content: str, limit: int = 240) -> str:
        try:
            from plugins.intelligence_center.output_formatting import compact_artifact_summary, normalize_artifact_display
            return compact_artifact_summary(normalize_artifact_display(content), limit)
        except Exception:
            return " ".join(str(content or "").replace("#", "").split())[:limit]

    def register(self, path: str, metadata: dict | None = None) -> dict:
        """Register a generated file in the artifact center immediately.

        The report scanner still works as a fallback, but explicit registration
        gives the UI stable task/run/status/notification metadata right after a
        task finishes.
        """
        file_path = os.path.abspath(str(path or ""))
        if not self._is_safe_path(file_path):
            raise ValueError("产物路径不安全")
        if not os.path.isfile(file_path):
            raise FileNotFoundError("产物文件不存在")
        metadata = dict(metadata or {})
        from common.runtime_identity import current_identity
        identity = current_identity()
        if identity.run_id and not metadata.get('run_id'):
            metadata['run_id'] = identity.run_id
        if identity.session_id and not metadata.get('session_id'):
            metadata['session_id'] = identity.session_id
        stat = os.stat(file_path)
        artifact_id = self._id(file_path)
        name = os.path.basename(file_path)
        title = str(metadata.get("title") or "").strip() or os.path.splitext(name)[0]
        preview = str(metadata.get("preview") or "").strip()
        if not preview and name.lower().endswith((".md", ".txt")):
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                    content = handle.read()
                preview = self._content_preview(content)
                first = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), "")
                title = str(metadata.get("title") or first or title).strip()
            except OSError:
                preview = ""
        tags = list(dict.fromkeys(
            str(value).strip()[:30]
            for value in (metadata.get("tags") or ["自动化", "报告"])
            if str(value).strip()
        ))[:8]
        item = {
            "id": artifact_id,
            "title": title,
            "type": str(metadata.get("type") or "report").strip()[:40] or "report",
            "format": os.path.splitext(name)[1].lstrip(".") or "file",
            "source": str(metadata.get("source") or "automation"),
            "source_id": str(metadata.get("source_id") or os.path.splitext(name)[0]),
            "source_url": str(metadata.get("source_url") or ""),
            "source_entry": str(metadata.get("source_entry") or ""),
            "source_label": str(metadata.get("source_label") or ""),
            "conversation_id": str(metadata.get("conversation_id") or metadata.get("session_id") or ""),
            "path": file_path,
            "size": stat.st_size,
            "preview": preview,
            "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            "project_ids": list(metadata.get("project_ids") or []),
            "tags": tags or ["自动化", "报告"],
            "task_id": metadata.get("task_id") or "",
            "task_name": metadata.get("task_name") or "",
            "run_id": metadata.get("run_id") or "",
            "run_status": metadata.get("run_status") or "",
            "trigger": metadata.get("trigger") or "",
            "kind_label": metadata.get("kind_label") or "",
            "result_summary": metadata.get("result_summary") or "",
            "notification_summary": metadata.get("notification_summary") or "",
            "started_at": metadata.get("started_at") or "",
            "finished_at": metadata.get("finished_at") or "",
        }
        with self._lock:
            items = [entry for entry in self._read_index() if entry.get("id") != artifact_id]
            items.insert(0, item)
            self._write_index(items[:2000])
        from common.task_trace import emit as emit_trace
        emit_trace(None, '保存产物', 'success', artifact_id=artifact_id)
        return item

    def _report_items(self) -> list[dict]:
        items = []
        if not os.path.isdir(self.report_root):
            return items
        for name in os.listdir(self.report_root):
            path = os.path.join(self.report_root, name)
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            title = os.path.splitext(name)[0]
            preview = ""
            if name.lower().endswith((".md", ".txt")):
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        content = handle.read()
                    first = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), "")
                    title, preview = first or title, self._content_preview(content)
                except OSError:
                    pass
            items.append({
                "id": self._id(path), "title": title, "type": "report", "format": os.path.splitext(name)[1].lstrip(".") or "file",
                "source": "automation", "source_id": name.split("-20", 1)[0], "path": path,
                "size": stat.st_size, "preview": preview, "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                "project_ids": [], "tags": ["自动化", "报告"],
            })
        return items

    def _deliverable_items(self) -> list[dict]:
        items = []
        roots = [
            (self.deliverables_root, "deliverable", ["交付物", "快速学习"]),
            (self.knowledge_sources_root, "knowledge_source", ["知识库", "来源资料"]),
            (self.downloads_root, "media_download", ["媒体文件", "下载任务"]),
        ]
        for root, source, tags in roots:
            if not os.path.isdir(root):
                continue
            for current_root, _dirs, files in os.walk(root):
                for name in files:
                    if not name.lower().endswith((".md", ".txt", ".pdf", ".docx", ".xlsx", ".csv", ".json")):
                        continue
                    path = os.path.join(current_root, name)
                    if not os.path.isfile(path):
                        continue
                    rel_path = os.path.relpath(path, self.workspace_root).replace("\\", "/") if path.startswith(self.workspace_root) else name
                    if source == "knowledge_source" and rel_path.startswith("knowledge/sources/books/"):
                        # Fast Reading saves a knowledge copy for retrieval, while
                        # the user-facing deliverable is the registered reading
                        # artifact in reports/. Showing both in the artifact center
                        # makes one reading look like duplicate outputs.
                        continue
                    stat = os.stat(path)
                    title = os.path.splitext(name)[0]
                    preview = ""
                    if name.lower().endswith((".md", ".txt")):
                        try:
                            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                                content = handle.read()
                            first = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), "")
                            title, preview = first or title, self._content_preview(content)
                        except OSError:
                            pass
                    inferred_tags = list(tags)
                    if "learning" in name.lower() or "学习" in title:
                        inferred_tags.append("快速学习")
                    items.append({
                        "id": self._id(path),
                        "title": title,
                        "type": "学习笔记" if "快速学习" in inferred_tags else "交付物",
                        "format": os.path.splitext(name)[1].lstrip(".") or "file",
                        "source": source,
                        "source_id": rel_path,
                        "source_entry": "common-function:quick-learning" if "快速学习" in inferred_tags else "",
                        "source_label": "快速学习 Agent" if "快速学习" in inferred_tags else "本地交付物",
                        "path": path,
                        "size": stat.st_size,
                        "preview": preview,
                        "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                        "project_ids": [],
                        "tags": list(dict.fromkeys(inferred_tags))[:8],
                        "kind_label": "快速学习 Agent" if "快速学习" in inferred_tags else "本地交付物",
                        "result_summary": preview,
                    })
        return items

    def _task_artifact_metadata(self) -> dict[str, dict]:
        """Read the latest task output settings without duplicating metadata into reports."""
        metadata = {}
        try:
            names = os.listdir(self.root)
        except OSError:
            return metadata
        for name in names:
            if not name.endswith("_tasks.json") or name == "unified_tasks.json":
                continue
            try:
                with open(os.path.join(self.root, name), "r", encoding="utf-8") as handle:
                    tasks = (json.load(handle) or {}).get("tasks", [])
            except (OSError, ValueError, TypeError):
                continue
            for task in tasks:
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    continue
                artifact_type = str(task.get("artifact_type") or "report").strip()[:40] or "report"
                tags = list(dict.fromkeys(
                    str(value).strip()[:30] for value in (task.get("artifact_tags") or ["自动化", "报告"]) if str(value).strip()
                ))[:8]
                metadata[task_id] = {"type": artifact_type, "tags": tags or ["自动化", "报告"]}
        try:
            scheduler_path = os.path.join(os.path.dirname(self.root), "scheduler", "tasks.json")
            with open(scheduler_path, "r", encoding="utf-8") as handle:
                tasks = (json.load(handle) or {}).get("tasks", [])
        except (OSError, ValueError, TypeError):
            tasks = []
        for task in tasks:
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                continue
            action = task.get("action", {}) if isinstance(task.get("action"), dict) else {}
            tags = ["定时任务", "自动化", "报告"]
            channel = str(action.get("channel_type") or "").strip()
            if channel:
                tags.append(channel.upper() if channel == "web" else channel)
            metadata[task_id] = {
                "type": str(task.get("artifact_type") or "report").strip()[:40] or "report",
                "tags": list(dict.fromkeys(tags))[:8],
                "task_id": task_id,
                "task_name": task.get("name"),
                "run_status": task.get("last_run_status"),
                "result_summary": task.get("last_result_summary") or task.get("last_error") or "",
                "notification_summary": f"发送到：{channel.upper() if channel == 'web' else channel or 'web'}",
                "trigger": "scheduled_agent",
                "kind_label": "定时任务",
                "started_at": task.get("last_run_at"),
                "finished_at": task.get("last_run_at"),
            }
        return metadata

    def _run_artifact_metadata(self) -> dict[str, dict]:
        """Map saved report files back to their automation execution sessions."""
        metadata = {}
        try:
            path = os.path.join(self.root, "automation_runs.json")
            with open(path, "r", encoding="utf-8") as handle:
                runs = (json.load(handle) or {}).get("runs", [])
        except (OSError, ValueError, TypeError):
            try:
                # Newer builds may persist runs in SQLite.  Keep this optional
                # so the artifact center still works if SQLite is unavailable.
                from intelligence_center.sqlite_state import SQLiteStateStore
                runs = SQLiteStateStore(self.root).read("automation_runs", {"runs": []}).get("runs", [])
            except Exception:
                runs = []
        for run in runs:
            report_path = os.path.abspath(str(run.get("report_path") or ""))
            if not report_path:
                continue
            metadata[report_path] = {
                "run_id": run.get("id"),
                "task_id": run.get("task_id"),
                "task_name": run.get("task_name"),
                "run_status": run.get("status"),
                "trigger": run.get("trigger"),
                "kind_label": run.get("kind_label"),
                "result_summary": run.get("result_summary") or run.get("error") or "",
                "notification_summary": self._notification_summary(run.get("notification_results") or []),
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
            }
        return metadata

    @staticmethod
    def _notification_summary(notifications: list[dict]) -> str:
        if not notifications:
            return "未配置通知，仅保存结果"
        total = len(notifications)
        sent = sum(1 for item in notifications if item.get("success"))
        channels = list(dict.fromkeys(str(item.get("channel") or item.get("target") or "通道") for item in notifications))
        fallback = any(
            item.get("web_fallback") or item.get("fallback")
            or str(item.get("target") or "").startswith("Web（自动补发")
            or "补发到 Web" in str(item.get("message") or "")
            for item in notifications
        )
        suffix = "，已补发 Web" if fallback else ""
        return f"通知 {sent}/{total}{suffix}：{'、'.join(channels[:4])}"

    @staticmethod
    def _apply_task_artifact_metadata(item: dict, metadata: dict[str, dict]) -> dict:
        haystack = " ".join((str(item.get("source_id") or ""), os.path.basename(str(item.get("path") or ""))))
        matches = [(task_id, config) for task_id, config in metadata.items() if task_id in haystack]
        if matches:
            _, config = max(matches, key=lambda pair: len(pair[0]))
            enrich = {key: value for key, value in config.items() if value not in (None, "")}
            item = {**item, **enrich, "type": config["type"], "tags": list(config["tags"])}
        return item

    @staticmethod
    def _apply_run_artifact_metadata(item: dict, metadata: dict[str, dict]) -> dict:
        path = os.path.abspath(str(item.get("path") or ""))
        config = metadata.get(path)
        if not config:
            return item
        tags = list(item.get("tags") or [])
        if config.get("run_status") == "failed" and "失败" not in tags:
            tags.insert(0, "失败")
        if config.get("trigger") == "scheduled" and "定时" not in tags:
            tags.append("定时")
        elif config.get("trigger") == "manual" and "手动" not in tags:
            tags.append("手动")
        return {**item, **config, "tags": list(dict.fromkeys(tags))[:8]}

    def _sync_report_index(self, report_items: list[dict]) -> list[dict]:
        """Persist report-scan fallback items into artifacts.json.

        Older task paths wrote Markdown reports before explicit artifact
        registration existed.  They were visible through the scanner, but not
        stable enough for source tracing, deletion counters and future metadata
        enrichment.  This backfills them the first time the artifact center is
        opened.
        """
        try:
            with self._lock:
                custom = self._read_index()
                existing = {item.get("id") for item in custom}
                missing = [item for item in report_items if item.get("id") and item.get("id") not in existing]
                if not missing:
                    return custom
                custom = missing + custom
                custom.sort(key=lambda item: item.get("created_at", ""), reverse=True)
                self._write_index(custom[:2000])
                return custom[:2000]
        except Exception:
            return self._read_index()

    @staticmethod
    def infer_category(item: dict) -> str:
        text = " ".join([
            str(item.get("source") or ""),
            str(item.get("source_label") or ""),
            str(item.get("kind_label") or ""),
            str(item.get("type") or ""),
            str(item.get("title") or ""),
            str(item.get("task_name") or ""),
            " ".join(str(x) for x in (item.get("tags") or [])),
        ]).lower()
        zh = " ".join([
            str(item.get("source_label") or ""),
            str(item.get("kind_label") or ""),
            str(item.get("type") or ""),
            str(item.get("title") or ""),
            str(item.get("task_name") or ""),
            " ".join(str(x) for x in (item.get("tags") or [])),
        ])
        rules = [
            ("快速读书", ("fast-reading", "快速读书", "读书笔记", "预读导读")),
            ("个人早间简报", ("早间简报", "个人早间", "morning briefing", "每日个人早间简报")),
            ("新闻热点", ("新闻热点", "新闻资讯", "国内热点", "国际热点", "热点", "network-search", "网络搜索")),
            ("快速学习", ("quick-learning", "快速学习", "学习笔记")),
            ("定时任务", ("automation", "自动化", "定时", "任务结果")),
            ("维护报告", ("maintenance", "维护", "验收", "readiness", "交付后维护")),
            ("自我进化", ("self", "harness", "自我优化", "自我进化", "失败诊断", "训练场")),
            ("知识库归档", ("knowledge", "知识库", "归档")),
            ("媒体文件", ("media", "音乐", "视频", "媒体文件")),
        ]
        for label, needles in rules:
            if any(str(n).lower() in text or str(n) in zh for n in needles):
                return label
        return "其他"

    def list(self, query: str = "", search_field: str = "all", date: str = "", category: str = "") -> list[dict]:
        report_items = self._report_items() + self._deliverable_items()
        merged = {item["id"]: item for item in report_items}
        for item in self._sync_report_index(report_items):
            existing = merged.get(item.get("id"), {})
            merged[item.get("id")] = {**existing, **item}
        metadata = self._task_artifact_metadata()
        run_metadata = self._run_artifact_metadata()
        items = [self._apply_run_artifact_metadata(self._apply_task_artifact_metadata(item, metadata), run_metadata) for item in merged.values()]
        items = [item for item in items if not (
            str(item.get("source") or "") == "knowledge_source"
            and str(item.get("source_id") or "").replace("\\", "/").startswith("knowledge/sources/books/")
        )]
        items = self._dedupe_fast_reading_items(items)
        items = [{**item, "artifact_category": self.infer_category(item)} for item in items]
        category = str(category or "").strip()
        if category and category not in {"全部", "all"}:
            items = [item for item in items if str(item.get("artifact_category") or "其他") == category]
        if date:
            items = [item for item in items if str(item.get("created_at") or "").startswith(date)]
        needle = str(query or "").strip().lower()
        field = search_field if search_field in {"all", "title", "content", "tags", "date"} else "all"
        if needle:
            filtered = []
            for item in items:
                values = []
                if field in {"all", "title"}: values.append(str(item.get("title") or ""))
                if field in {"all", "title"}: values.append(str(item.get("task_name") or ""))
                if field in {"all", "tags"}: values.append(" ".join(item.get("tags") or []))
                if field in {"all", "date"}: values.append(str(item.get("created_at") or ""))
                if field == "all":
                    values.extend([
                        str(item.get("kind_label") or ""),
                        str(item.get("run_status") or ""),
                        str(item.get("result_summary") or ""),
                        str(item.get("trigger") or ""),
                    ])
                if field in {"all", "content"}:
                    path = os.path.abspath(str(item.get("path") or ""))
                    if self._is_safe_path(path) and path.lower().endswith((".md", ".txt", ".json", ".csv")):
                        try:
                            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                                values.append(handle.read())
                        except OSError:
                            values.append(str(item.get("preview") or ""))
                    else:
                        values.append(str(item.get("preview") or ""))
                if any(needle in value.lower() for value in values):
                    filtered.append(item)
            items = filtered
        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)

    def categories(self, items: list[dict] | None = None) -> list[dict]:
        source = items if items is not None else self.list()
        counts: dict[str, int] = {}
        for item in source:
            label = str(item.get("artifact_category") or self.infer_category(item) or "其他")
            counts[label] = counts.get(label, 0) + 1
        preferred = ["全部", "快速读书", "个人早间简报", "新闻热点", "快速学习", "定时任务", "维护报告", "自我进化", "知识库归档", "媒体文件", "其他"]
        result = [{"name": "全部", "count": len(source)}]
        for name in preferred[1:]:
            if counts.get(name):
                result.append({"name": name, "count": counts[name]})
        for name, count in sorted(counts.items()):
            if name not in preferred:
                result.append({"name": name, "count": count})
        return result

    @staticmethod
    def _dedupe_fast_reading_items(items: list[dict]) -> list[dict]:
        """Keep only the latest visible Fast Reading artifact per book/source.

        The service keeps files for traceability, but the artifact center should
        feel like a deliverables view, not a debug log. Re-running the same book
        therefore updates/replaces the visible item instead of cluttering the UI.
        """
        latest_by_key: dict[tuple[str, str], dict] = {}
        output = []
        for item in items:
            if str(item.get("source") or "") != "fast-reading":
                output.append(item)
                continue
            key = (str(item.get("source_id") or item.get("title") or "").strip(), str(item.get("title") or "").strip())
            existing = latest_by_key.get(key)
            if not existing or str(item.get("created_at") or "") > str(existing.get("created_at") or ""):
                latest_by_key[key] = item
        output.extend(latest_by_key.values())
        return output

    def get(self, artifact_id: str, include_content: bool = False) -> dict | None:
        item = next((x for x in self.list() if x.get("id") == artifact_id), None)
        if item and include_content:
            path = os.path.abspath(item.get("path", ""))
            if not self._is_safe_path(path):
                raise ValueError("产物路径不安全")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    item["content"] = handle.read()
            except UnicodeDecodeError:
                item["content"] = "该文件需要下载后查看"
        return item

    def archive(self, artifact_id: str, project_id: str, project_service) -> dict:
        item = self.get(artifact_id)
        project = project_service.get(project_id)
        if not item or not project:
            raise KeyError("产物或项目不存在")
        with self._lock:
            custom = self._read_index()
            saved = next((x for x in custom if x.get("id") == artifact_id), dict(item))
            ids = list(saved.get("project_ids") or [])
            if project_id not in ids:
                ids.append(project_id)
            saved["project_ids"] = ids
            custom = [x for x in custom if x.get("id") != artifact_id] + [saved]
            self._write_index(custom)
        assets = [x for x in project.get("assets", []) if x.get("artifact_id") != artifact_id]
        assets.insert(0, {"artifact_id": artifact_id, "title": item["title"], "type": item["type"], "created_at": item["created_at"]})
        project_service.update(project_id, {"assets": assets})
        return saved

    def delete(self, artifact_ids: list[str]) -> dict:
        """Soft-delete artifacts by moving their files into a local trash folder."""
        ids = {str(value).strip() for value in artifact_ids if str(value).strip()}
        if not ids:
            raise ValueError("请选择需要删除的产物")
        current = {item.get("id"): item for item in self.list()}
        trash_root = os.path.join(self.root, ".trash", "artifacts")
        os.makedirs(trash_root, exist_ok=True)
        deleted, missing = [], []
        with self._lock:
            custom = self._read_index()
            for artifact_id in ids:
                item = current.get(artifact_id)
                if not item:
                    missing.append(artifact_id)
                    continue
                path = os.path.abspath(str(item.get("path") or ""))
                if not self._is_safe_path(path):
                    raise ValueError("产物路径不安全")
                if os.path.isfile(path):
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    target = os.path.join(trash_root, f"{stamp}-{os.path.basename(path)}")
                    shutil.move(path, target)
                custom = [entry for entry in custom if entry.get("id") != artifact_id]
                deleted.append(artifact_id)
            self._write_index(custom)
        return {"deleted": deleted, "missing": missing, "recoverable": True, "trash": trash_root}


_service = None


def get_artifact_service(root: str):
    global _service
    if _service is None or _service.root != os.path.abspath(root):
        _service = ArtifactService(root)
    return _service
