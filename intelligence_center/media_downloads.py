"""Media source assistant and lightweight download task center.

This module deliberately avoids bypassing platform restrictions. It can:
- recommend legitimate music/video sources from a name or URL;
- keep a local task list with status, retry and failure reasons;
- register user-provided direct links or local-file conversion requests;
- search configured authorized local libraries / public-domain collections;
- check ffmpeg availability for MP3/MP4 conversion workflows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
import urllib.request


class MediaDownloadService:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        project_root = self.root.parent
        self.workspace = project_root / "work" / "cow-workspace"
        self.download_root = self.workspace / "downloads"
        self.task_path = self.download_root / "media_tasks.json"
        self.failure_path = self.root / "media_failure_learning.json"
        self.config_path = self.root / "media_tool_config.json"
        self.download_root.mkdir(parents=True, exist_ok=True)

    def health(self) -> dict:
        cfg = self._read_config()
        ffmpeg = self._ffmpeg_path()
        ffprobe = shutil.which("ffprobe")
        return {
            "ffmpeg": bool(ffmpeg),
            "ffmpeg_path": ffmpeg or "",
            "ffprobe": bool(ffprobe),
            "configured_ffmpeg_path": cfg.get("ffmpeg_path") or "",
            "authorized_source_dirs": self._authorized_source_dirs(),
            "download_root": str(self.download_root),
            "policy": "仅处理合法来源、用户自有文件、公共版权库或用户明确有权下载的直链；不绕过平台、登录、DRM 或版权限制。",
        }

    def configure_ffmpeg(self, explicit_path: str = "") -> dict:
        candidates = []
        if explicit_path:
            candidates.append(Path(explicit_path))
        found = shutil.which("ffmpeg")
        if found:
            candidates.append(Path(found))
        candidates.extend([
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
            self.workspace / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
            self.root.parent / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
        ])
        for candidate in candidates:
            if candidate and candidate.exists() and candidate.is_file():
                cfg = self._read_config()
                cfg["ffmpeg_path"] = str(candidate)
                cfg["configured_at"] = self._now()
                self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                retried = self._retry_ffmpeg_blocked_tasks()
                return {"configured": True, "ffmpeg_path": str(candidate), "message": "ffmpeg 已配置", "retried": retried}
        return {
            "configured": False,
            "ffmpeg_path": "",
            "message": "未找到 ffmpeg。请安装 ffmpeg，或把 ffmpeg.exe 放到 work/cow-workspace/tools/ffmpeg/bin/ 后重新检测。",
            "retried": 0,
        }

    def suggest_sources(self, payload: dict) -> dict:
        query = str(payload.get("query") or "").strip()
        media_type = str(payload.get("media_type") or "auto").strip() or "auto"
        source_hint = str(payload.get("source_hint") or "").strip()
        if not query:
            raise ValueError("请输入音乐/视频名称或链接")
        parsed = urlparse(query)
        is_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        detected = self._detect_type(query, media_type)
        direct = self._looks_direct_media_url(query) if is_url else False
        sources = self._source_candidates(query, detected, source_hint, is_url, direct)
        warnings = [
            "请确认你对目标内容拥有下载、保存或格式转换的权利。",
            "平台视频、会员音乐、DRM 内容、登录后内容不会默认下载，只提供来源和操作建议。",
        ]
        if direct:
            warnings.insert(0, "检测到可能是直链媒体；可创建下载任务，但仍需确认来源合法。")
        return {
            "status": "success",
            "query": query,
            "media_type": detected,
            "is_url": is_url,
            "direct_media": direct,
            "sources": sources,
            "warnings": warnings,
            "can_create_task": bool(direct),
            "summary": self._summary(query, detected, is_url, direct),
        }

    def create_task(self, payload: dict) -> dict:
        query = str(payload.get("query") or payload.get("url") or "").strip()
        media_type = str(payload.get("media_type") or "auto").strip() or "auto"
        target_format = str(payload.get("target_format") or "").strip().lower()
        quality = str(payload.get("quality") or "").strip()
        legal_confirmed = bool(payload.get("legal_confirmed"))
        action = str(payload.get("action") or "source").strip()
        save_dir = str(payload.get("save_dir") or "").strip()
        if not query:
            raise ValueError("请输入名称、链接或本地文件路径")
        if action == "auto":
            action = "download" if self._looks_direct_media_url(query) else "search_download"
            legal_confirmed = True
        if action in {"download", "convert"} and not legal_confirmed:
            raise ValueError("请先确认该内容为合法来源或你有权处理")
        if action == "download" and not self._looks_direct_media_url(query):
            action = "search_download"
        if action == "convert":
            source_path = Path(query)
            if not source_path.exists():
                raise ValueError("本地源文件不存在，请填写具体音频/视频文件路径。")
            if not source_path.is_file():
                raise ValueError("当前路径不是具体媒体文件，请选择 .mp3、.wav、.mp4、.mov 等具体文件。")
        tasks = self._read_tasks()
        task_key = self._task_key(action, query, target_format)
        for existing in tasks:
            if self._task_key(existing.get("action"), existing.get("query"), existing.get("target_format")) == task_key:
                existing_status = str(existing.get("status") or "").lower()
                previous_preview = (
                    action == "search_download"
                    and existing_status == "success"
                    and (
                        "公开预览" in str(existing.get("source_title") or "")
                        or "30 秒" in str(existing.get("next_step") or "")
                        or "30秒" in str(existing.get("next_step") or "")
                    )
                )
                if (existing_status in {"blocked", "failed"} or previous_preview) and action in {"download", "search_download", "convert"}:
                    if save_dir:
                        existing["save_dir"] = save_dir
                    existing["failure_reason"] = ""
                    existing["next_step"] = ""
                    existing["source_url"] = ""
                    existing["source_title"] = ""
                    self._run_task_once(existing, legal_confirmed=True)
                    existing["updated_at"] = self._now()
                    existing["deduped"] = True
                    self._write_tasks(tasks)
                    return existing
                if existing_status in {"planned", "queued", "ready", "blocked", "failed", "success"}:
                    existing["updated_at"] = self._now()
                    if save_dir:
                        existing["save_dir"] = save_dir
                    existing["deduped"] = True
                    self._write_tasks(tasks)
                    return existing
        now = self._now()
        task = {
            "id": "media-" + uuid.uuid4().hex[:10],
            "trace_id": uuid.uuid4().hex,
            "query": query,
            "media_type": self._detect_type(query, media_type),
            "action": action,
            "target_format": target_format,
            "quality": quality,
            "status": "queued" if action in {"download", "convert", "search_download"} else "planned",
            "created_at": now,
            "updated_at": now,
            "attempts": 0,
            "save_dir": save_dir or str(self.download_root),
            "result_path": "",
            "artifact_id": "",
            "failure_reason": "",
            "next_step": "",
        }
        self._run_task_once(task, legal_confirmed=legal_confirmed)
        tasks.insert(0, task)
        self._write_tasks(tasks[:300])
        return task

    def list_tasks(self, query: str = "", status: str = "") -> list[dict]:
        tasks = self._read_tasks()
        needle = str(query or "").strip().lower()
        status = str(status or "").strip().lower()
        if needle:
            tasks = [t for t in tasks if needle in json.dumps(t, ensure_ascii=False).lower()]
        if status:
            tasks = [t for t in tasks if str(t.get("status") or "").lower() == status]
        return tasks

    def retry(self, task_id: str) -> dict:
        tasks = self._read_tasks()
        for task in tasks:
            if task.get("id") == task_id:
                self._run_task_once(task, legal_confirmed=True)
                task["updated_at"] = self._now()
                self._write_tasks(tasks)
                return task
        raise KeyError("下载任务不存在")

    def delete(self, task_id: str) -> dict:
        tasks = self._read_tasks()
        kept = [task for task in tasks if task.get("id") != task_id]
        self._write_tasks(kept)
        return {"deleted": len(tasks) - len(kept)}

    def _run_task_once(self, task: dict, *, legal_confirmed: bool) -> None:
        self._emit_trace(task, "媒体任务执行", "running")
        task["attempts"] = int(task.get("attempts") or 0) + 1
        action = str(task.get("action") or "")
        query = str(task.get("query") or "")
        if action == "source":
            task["status"] = "planned"
            task["next_step"] = "已生成来源建议；如有合法直链或自有文件，可再创建下载/格式处理任务。"
            self._emit_trace(task, "媒体来源建议", "completed")
            self._notify(task, True, "媒体来源建议已保存")
            return
        if action == "download":
            if not self._looks_direct_media_url(query):
                task["status"] = "blocked"
                task["failure_reason"] = "当前不是可直接下载的媒体直链，暂不绕过平台限制。"
                task["next_step"] = "请使用官方平台下载、提供有权下载的直链，或上传自有文件做格式处理。"
                self._learn_failure(task, "not_direct_media_url")
                self._emit_trace(task, "媒体直链校验", "failed")
                self._notify(task, False, task["failure_reason"])
                return
            try:
                target = self._download_direct_media(query, task.get("save_dir"))
                target = self._maybe_convert_download_format(task, target)
                task["status"] = "success"
                task["result_path"] = str(target)
                task["next_step"] = "合法直链下载完成，已进入产物/交付物。"
                artifact = self._try_register_artifact(task, target, "媒体下载")
                task["artifact_id"] = artifact.get("id", "")
                self._emit_trace(task, "媒体直链下载", "completed", artifact_id=task["artifact_id"])
                self._notify(task, True, "媒体直链下载完成")
            except Exception as error:
                task["status"] = "failed"
                task["failure_reason"] = str(error)[:300]
                task["next_step"] = "请检查直链是否可公开访问、是否过期，或换用官方下载方式。"
                self._learn_failure(task, "download_failed")
                self._emit_trace(task, "媒体直链下载", "failed")
                self._notify(task, False, task["failure_reason"])
            return
        if action == "search_download":
            try:
                normalized_query = self._normalized_media_query(query)
                if len(normalized_query) <= 1:
                    task["status"] = "failed"
                    task["failure_reason"] = "关键词太短，无法可靠判断要下载哪首歌或哪个视频。"
                    task["next_step"] = "请改成“歌手 + 歌名”或“视频标题 + 作者”，例如输入更完整的名称后再一键下载。"
                    self._learn_failure(task, "public_source_query_too_short")
                    self._emit_trace(task, "公开媒体源搜索", "failed")
                    self._notify(task, False, task["failure_reason"])
                    return
                candidate = self._find_public_media_candidate(query, str(task.get("media_type") or "auto"))
                if not candidate:
                    task["status"] = "failed"
                    task["failure_reason"] = "未找到合法完整版音频源。"
                    task["next_step"] = "已尝试授权本地音乐库和公共版权库；不会再把 30 秒试听当成完整版。你可以添加本地音乐库目录，或粘贴你已有权限的完整 .mp3/.m4a 直链后再试。"
                    self._learn_failure(task, "public_source_not_found")
                    self._emit_trace(task, "公开媒体源搜索", "failed")
                    self._notify(task, False, task["failure_reason"])
                    return
                if candidate.get("local_path"):
                    target = self._copy_local_media(candidate["local_path"], task.get("save_dir"))
                else:
                    target = self._download_direct_media(candidate["url"], task.get("save_dir"))
                target = self._maybe_convert_download_format(task, target)
                task["status"] = "success"
                task["result_path"] = str(target)
                task["source_url"] = candidate.get("page_url") or candidate.get("url") or ""
                task["source_title"] = candidate.get("title") or ""
                source_label = candidate.get("source") or "合法媒体源"
                task["next_step"] = f"已从{source_label}下载/复制完成：{candidate.get('title') or '媒体文件'}。"
                artifact = self._try_register_artifact(task, target, "媒体下载")
                task["artifact_id"] = artifact.get("id", "")
                self._emit_trace(task, "公开媒体源下载", "completed", artifact_id=task["artifact_id"])
                self._notify(task, True, "公开媒体源下载完成")
            except Exception as error:
                task["status"] = "failed"
                task["failure_reason"] = str(error)[:300]
                task["next_step"] = "可换更准确的名称，添加授权本地音乐库目录，或粘贴你已有权限的 .mp3/.mp4 直链后再试。"
                self._learn_failure(task, "public_source_download_failed")
                self._emit_trace(task, "公开媒体源下载", "failed")
                self._notify(task, False, task["failure_reason"])
            return
        if action == "convert":
            ffmpeg = self._ffmpeg_path()
            if not ffmpeg:
                task["status"] = "blocked"
                task["failure_reason"] = "本机未检测到 ffmpeg，暂无法执行 MP3/MP4 转换。"
                task["next_step"] = "安装 ffmpeg 后可重试；当前已保留任务。"
                self._learn_failure(task, "ffmpeg_missing")
                self._emit_trace(task, "媒体格式处理", "failed")
                self._notify(task, False, task["failure_reason"])
                return
            source_path = Path(query)
            if not source_path.exists():
                task["status"] = "blocked"
                task["failure_reason"] = "本地源文件不存在。"
                task["next_step"] = "请确认文件路径，或从页面上传/选择本地文件。"
                self._learn_failure(task, "source_file_missing")
                self._emit_trace(task, "媒体格式处理", "failed")
                self._notify(task, False, task["failure_reason"])
                return
            if not source_path.is_file():
                task["status"] = "blocked"
                task["failure_reason"] = "当前路径不是具体媒体文件。"
                task["next_step"] = "请选择具体音频/视频文件，例如 .mp3、.wav、.mp4、.mov。"
                self._learn_failure(task, "source_path_not_file")
                self._emit_trace(task, "媒体格式处理", "failed")
                self._notify(task, False, task["failure_reason"])
                return
            target_format = str(task.get("target_format") or "mp3").lower()
            if target_format not in {"mp3", "mp4", "m4a", "wav"}:
                target_format = "mp3"
            target_dir = self._resolve_save_dir(task.get("save_dir"))
            target = target_dir / f"{source_path.stem}-{int(time.time())}.{target_format}"
            command = [ffmpeg, "-y", "-i", str(source_path), str(target)]
            try:
                subprocess.run(command, capture_output=True, text=True, timeout=180, check=True)
                task["status"] = "success"
                task["result_path"] = str(target)
                task["next_step"] = "格式处理完成，已进入产物/交付物。"
                artifact = self._register_artifact(task, target, "媒体格式处理")
                task["artifact_id"] = artifact.get("id", "")
                self._emit_trace(task, "媒体格式处理", "completed", artifact_id=task["artifact_id"])
                self._notify(task, True, "媒体格式处理完成")
            except Exception as error:
                task["status"] = "failed"
                task["failure_reason"] = str(error)[:300]
                task["next_step"] = "可检查源文件格式或换用较短文件后重试。"
                self._learn_failure(task, "convert_failed")
                self._emit_trace(task, "媒体格式处理", "failed")
                self._notify(task, False, task["failure_reason"])

    def _download_direct_media(self, url: str, save_dir: str | None = None) -> Path:
        from intelligence_center.safe_network import safe_urlopen
        parsed = urlparse(url)
        ext = Path(parsed.path).suffix.lower()
        if ext not in {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".mp4", ".mov", ".mkv", ".webm", ".avi"}:
            raise ValueError("链接不是支持的媒体文件扩展名")
        name = self._safe_name(Path(parsed.path).stem) or "media"
        target_dir = self._resolve_save_dir(save_dir)
        target = target_dir / f"{name}-{int(time.time())}{ext}"
        request = urllib.request.Request(url, headers={"User-Agent": "CowAgent/1.0"})
        max_bytes = 200 * 1024 * 1024
        downloaded = 0
        with safe_urlopen(request, timeout=30, allow_private=False) as response, target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise ValueError("媒体文件超过 200MB，已停止下载")
                handle.write(chunk)
        if target.stat().st_size <= 0:
            raise ValueError("下载结果为空")
        return target

    def _copy_local_media(self, source: str, save_dir: str | None = None) -> Path:
        source_path = Path(source)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError("授权本地媒体文件不存在")
        target_dir = self._resolve_save_dir(save_dir)
        safe_stem = self._safe_name(source_path.stem) or "media"
        target = target_dir / f"{safe_stem}-{int(time.time())}{source_path.suffix.lower()}"
        if source_path.resolve() == target.resolve():
            target = target_dir / f"{safe_stem}-copy-{int(time.time())}{source_path.suffix.lower()}"
        shutil.copy2(str(source_path), str(target))
        if target.stat().st_size <= 0:
            raise ValueError("复制结果为空")
        return target

    def _maybe_convert_download_format(self, task: dict, path: Path) -> Path:
        requested = Path(str(task.get("query") or "")).suffix.lower().lstrip(".")
        if requested not in {"mp3", "mp4", "m4a", "wav"}:
            return path
        if path.suffix.lower().lstrip(".") == requested:
            return path
        ffmpeg = self._ffmpeg_path()
        if not ffmpeg:
            task["format_warning"] = f"已下载 {path.suffix.lstrip('.')}，但未检测到 ffmpeg，暂无法自动转为 {requested}。"
            return path
        target = path.with_suffix(f".{requested}")
        if target.exists():
            target = path.with_name(f"{path.stem}-{int(time.time())}.{requested}")
        command = [ffmpeg, "-y", "-i", str(path), str(target)]
        subprocess.run(command, capture_output=True, text=True, timeout=180, check=True)
        try:
            if target.exists() and target.stat().st_size > 0:
                path.unlink(missing_ok=True)
        except Exception:
            pass
        task["format_warning"] = ""
        return target

    def _find_public_media_candidate(self, query: str, media_type: str = "auto") -> dict | None:
        clean_query = self._normalized_media_query(query)
        if not clean_query:
            return None
        if len(clean_query) <= 1:
            return None
        detected = self._detect_type(clean_query, media_type)
        local_candidate = self._find_authorized_local_candidate(clean_query, detected)
        if local_candidate:
            return local_candidate
        mediatype = "movies" if detected == "video" else "audio"
        search_terms_list = [
            f'title:("{clean_query}") AND mediatype:({mediatype})',
            f'({clean_query}) AND mediatype:({mediatype})',
            f'({clean_query} music) AND mediatype:(audio)' if detected == "music" else f'({clean_query} video) AND mediatype:(movies)',
        ]
        docs: list[dict] = []
        seen_ids: set[str] = set()
        for search_terms in search_terms_list:
            params = urlencode({
                "q": search_terms,
                "fl[]": ["identifier", "title", "mediatype"],
                "rows": "12",
                "page": "1",
                "output": "json",
            }, doseq=True)
            payload = self._fetch_json_with_retry(f"https://archive.org/advancedsearch.php?{params}", timeout=20)
            for doc in ((payload.get("response") or {}).get("docs") or []) if isinstance(payload, dict) else []:
                identifier = str(doc.get("identifier") or "").strip()
                if identifier and identifier not in seen_ids:
                    seen_ids.add(identifier)
                    docs.append(doc)
            if docs:
                break
        preferred = (
            [".mp3", ".m4a", ".ogg", ".flac", ".wav"] if detected == "music"
            else [".mp4", ".webm", ".mov"]
        )
        for doc in docs[:12]:
            identifier = str(doc.get("identifier") or "").strip()
            if not identifier:
                continue
            metadata_url = f"https://archive.org/metadata/{quote(identifier)}"
            try:
                metadata = self._fetch_json_with_retry(metadata_url, timeout=20)
            except Exception:
                continue
            for item in metadata.get("files") or []:
                name = str(item.get("name") or "")
                title = str(doc.get("title") or identifier)
                if not self._is_relevant_public_media(clean_query, title, name):
                    continue
                lower = name.lower()
                if not any(lower.endswith(ext) for ext in preferred):
                    continue
                if any(skip in lower for skip in ("sample", "thumbnail", "spectrogram")):
                    continue
                try:
                    size = int(item.get("size") or 0)
                except Exception:
                    size = 0
                if size and size > 200 * 1024 * 1024:
                    continue
                file_url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='')}"
                return {
                    "url": file_url,
                    "page_url": f"https://archive.org/details/{quote(identifier, safe='')}",
                    "title": title,
                    "source": "Internet Archive",
                }
        return None

    def _find_authorized_local_candidate(self, query: str, media_type: str = "auto") -> dict | None:
        exts = (
            {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"} if self._detect_type(query, media_type) == "music"
            else {".mp4", ".mov", ".mkv", ".webm", ".avi"}
        )
        terms = self._query_terms(query)
        if not terms:
            return None
        best: tuple[int, Path] | None = None
        for folder in self._authorized_source_dirs():
            root = Path(folder)
            if not root.exists() or not root.is_dir():
                continue
            try:
                for path in root.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in exts:
                        continue
                    score = self._local_match_score(terms, path)
                    if score <= 0:
                        continue
                    if best is None or score > best[0] or (score == best[0] and path.stat().st_size > best[1].stat().st_size):
                        best = (score, path)
            except Exception:
                continue
        if not best:
            return None
        path = best[1]
        return {
            "url": path.resolve().as_uri(),
            "page_url": str(path),
            "title": path.stem,
            "source": "授权本地音乐库",
            "local_path": str(path),
        }

    def _authorized_source_dirs(self) -> list[str]:
        cfg = self._read_config()
        configured = cfg.get("authorized_source_dirs")
        raw_dirs = configured if isinstance(configured, list) else []
        user_profile = Path(os.environ.get("USERPROFILE") or "")
        defaults = [
            user_profile / "Music",
            user_profile / "Downloads",
            user_profile / "Desktop",
            Path(r"D:\KuGou"),
            Path(r"D:\KuGou\agent"),
            self.download_root,
        ]
        result: list[str] = []
        for item in [*raw_dirs, *defaults]:
            try:
                value = str(item).strip()
                if value and value not in result:
                    result.append(value)
            except Exception:
                continue
        return result

    def _query_terms(self, query: str) -> list[str]:
        normalized = self._normalized_media_query(query).lower()
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized)
        stop = {"mp3", "m4a", "wav", "flac", "aac", "ogg", "music", "audio", "video"}
        return [t for t in normalized.split() if t and t not in stop]

    @staticmethod
    def _local_match_score(terms: list[str], path: Path) -> int:
        haystack = f"{path.stem} {path.name} {path.parent}".lower()
        score = 0
        for term in terms:
            if term in haystack:
                score += max(1, len(term))
            else:
                return 0
        return score + min(10, int(path.stat().st_size / (1024 * 1024)))

    def _find_itunes_preview_candidate(self, query: str) -> dict | None:
        countries = ["CN", "US", "HK", "TW", "JP"]
        for country in countries:
            params = urlencode({
                "term": query,
                "entity": "song",
                "limit": "12",
                "country": country,
            })
            try:
                payload = self._fetch_json_with_retry(f"https://itunes.apple.com/search?{params}", timeout=15)
            except Exception:
                continue
            for item in payload.get("results") or []:
                preview_url = str(item.get("previewUrl") or "").strip()
                if not preview_url or not self._looks_direct_media_url(preview_url):
                    continue
                artist = str(item.get("artistName") or "")
                track = str(item.get("trackName") or "")
                title = " - ".join(x for x in [artist, track] if x).strip() or query
                if not self._is_relevant_public_media(query, title, preview_url):
                    continue
                return {
                    "url": preview_url,
                    "page_url": str(item.get("trackViewUrl") or ""),
                    "title": f"{title}（iTunes 公开预览）",
                    "source": f"iTunes Preview {country}",
                    "is_preview": True,
                }
        return None

    @staticmethod
    def _normalized_media_query(query: str) -> str:
        return re.sub(
            r"\.(mp3|wav|m4a|flac|aac|ogg|mp4|mov|mkv|webm|avi)$",
            "",
            str(query or "").strip(),
            flags=re.I,
        ).strip()

    @staticmethod
    def _is_relevant_public_media(query: str, title: str, filename: str) -> bool:
        q = re.sub(r"\s+", " ", str(query or "").strip().lower())
        haystack = f"{title} {filename}".lower()
        if not q:
            return False
        if q in haystack:
            return True
        tokens = [token for token in re.split(r"[\s\-_/.,，。]+", q) if len(token) >= 2]
        if not tokens:
            return False
        return all(token in haystack for token in tokens[:4])

    def _fetch_json_with_retry(self, url: str, timeout: int = 20, attempts: int = 3) -> dict:
        from intelligence_center.safe_network import safe_urlopen

        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "CowAgent/1.0 (+local personal assistant)",
                        "Accept": "application/json,text/plain,*/*",
                        "Connection": "close",
                    },
                )
                with safe_urlopen(request, timeout=timeout, allow_private=False) as response:
                    return json.loads(response.read().decode("utf-8", "ignore"))
            except Exception as error:
                last_error = error
                if attempt < attempts - 1:
                    time.sleep(0.8 * (attempt + 1))
        raise last_error or RuntimeError("公开源请求失败")

    def _resolve_save_dir(self, save_dir: str | None) -> Path:
        raw = str(save_dir or "").strip()
        target_dir = Path(raw) if raw else self.download_root
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def _register_artifact(self, task: dict, path: Path, artifact_type: str) -> dict:
        from intelligence_center.artifact_service import get_artifact_service
        return get_artifact_service(str(self.root)).register(str(path), {
            "title": f"{artifact_type}：{Path(path).name}",
            "type": "媒体文件",
            "source": "media-assistant",
            "source_id": str(task.get("id") or ""),
            "source_entry": "common-function:media-assistant",
            "source_label": "音乐/视频来源助手",
            "tags": ["媒体文件", str(task.get("media_type") or "media"), str(task.get("action") or "")],
            "kind_label": "音乐/视频来源助手",
            "result_summary": str(task.get("next_step") or ""),
            "finished_at": self._now(),
        })

    def _try_register_artifact(self, task: dict, path: Path, artifact_type: str) -> dict:
        try:
            return self._register_artifact(task, path, artifact_type)
        except Exception as error:
            task["artifact_warning"] = f"已保存到本地，但未进入产物中心：{str(error)[:120]}"
            return {}

    def _emit_trace(self, task: dict, stage: str, status: str, **facts) -> None:
        try:
            from common.task_trace import emit
            context = {"trace_id": task.get("trace_id"), "channel": "web", "session_id": "media_assistant"}
            emit(context, stage, status, **facts)
        except Exception:
            pass

    def _notify(self, task: dict, success: bool, message: str) -> None:
        try:
            from plugins.intelligence_center.notifications import NotificationDispatcher
            NotificationDispatcher._persist_events(
                {
                    "id": task.get("id"),
                    "name": f"媒体任务：{task.get('query') or task.get('action')}",
                    "run_id": task.get("trace_id") or "",
                    "report_path": task.get("result_path") or "",
                },
                [{
                    "target": "web",
                    "channel": "web",
                    "receiver": "web_user",
                    "success": bool(success),
                    "message": message or "",
                }],
                f"{task.get('query') or '媒体任务'}：{message or task.get('status') or ''}",
            )
        except Exception:
            pass

    def _learn_failure(self, task: dict, category: str) -> None:
        try:
            data = json.loads(self.failure_path.read_text(encoding="utf-8")) if self.failure_path.exists() else {}
        except Exception:
            data = {}
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        items.insert(0, {
            "id": uuid.uuid4().hex[:10],
            "at": self._now(),
            "category": category,
            "task_id": task.get("id"),
            "action": task.get("action"),
            "media_type": task.get("media_type"),
            "status": task.get("status"),
            "message": task.get("failure_reason"),
            "next_step": task.get("next_step"),
        })
        self.failure_path.write_text(json.dumps({"items": items[:200]}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _source_candidates(self, query: str, media_type: str, source_hint: str, is_url: bool, direct: bool) -> list[dict]:
        if direct:
            return [{
                "name": "用户提供的媒体直链",
                "type": "direct",
                "description": "看起来是可直接访问的媒体文件链接，适合创建受控下载任务。",
                "action": "确认合法来源后创建下载任务",
                "url": query,
                "button_label": "打开链接",
            }]
        if is_url:
            return [{
                "name": "原始网页",
                "type": "webpage",
                "description": "这是网页链接，不一定是媒体直链。建议先打开来源确认下载权限。",
                "action": "只记录来源，不自动下载",
                "url": query,
                "button_label": "打开网页",
            }]
        music_sources = [
            ("Apple Music / Spotify / QQ音乐 / 网易云音乐", "优先使用官方音乐平台收藏、购买或离线下载。", f"https://www.google.com/search?q={quote(query + ' Apple Music Spotify QQ音乐 网易云音乐')}"),
            ("YouTube Music / Bilibili 音乐区", "适合查找官方 MV、现场或授权发布内容。", f"https://www.google.com/search?q={quote(query + ' YouTube Music Bilibili 官方')}"),
        ]
        video_sources = [
            ("YouTube / Bilibili / Vimeo", "优先查看官方频道、授权转载或公开视频。", f"https://www.google.com/search?q={quote(query + ' YouTube Bilibili Vimeo 官方')}"),
            ("官网 / 课程平台 / 云盘分享", "适合处理你自有课程、内部视频或已授权素材。", f"https://www.google.com/search?q={quote(query + ' 官网 下载 授权')}"),
        ]
        raw = music_sources if media_type == "music" else video_sources if media_type == "video" else music_sources + video_sources
        if source_hint:
            raw.insert(0, (source_hint, "用户指定来源；需确认其下载规则和授权范围。", f"https://www.google.com/search?q={quote(query + ' ' + source_hint)}"))
        return [{
            "name": name,
            "type": "source",
            "description": desc,
            "action": "点击“打开搜索”去对应来源确认是否可下载。",
            "url": url,
            "button_label": "打开搜索",
        } for name, desc, url in raw]

    @staticmethod
    def _detect_type(query: str, media_type: str) -> str:
        if media_type in {"music", "video"}:
            return media_type
        lower = query.lower()
        if any(lower.endswith(ext) for ext in (".mp3", ".wav", ".m4a", ".flac", ".aac")):
            return "music"
        if any(lower.endswith(ext) for ext in (".mp4", ".mov", ".mkv", ".webm", ".avi")):
            return "video"
        return "video" if any(x in lower for x in ("video", "youtube", "bilibili", "mp4", "视频")) else "music"

    @staticmethod
    def _looks_direct_media_url(value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return False
        return bool(re.search(r"\.(mp3|wav|m4a|flac|aac|mp4|mov|mkv|webm|avi)(\?|$)", parsed.path.lower()))

    @staticmethod
    def _task_key(action: str | None, query: str | None, target_format: str | None = "") -> str:
        return "|".join(
            [
                str(action or "").strip().lower(),
                str(query or "").strip().lower(),
                str(target_format or "").strip().lower(),
            ]
        )

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(value or "")).strip("-")[:80]

    @staticmethod
    def _summary(query: str, media_type: str, is_url: bool, direct: bool) -> str:
        if direct:
            return f"已识别为{media_type}直链，可在确认合法来源后登记下载任务。"
        if is_url:
            return "已识别为网页来源，当前只提供来源分析和处理建议，不自动绕过平台下载。"
        if re.search(r"\.(mp3|wav|m4a|flac|aac|mp4|mov|mkv|webm|avi)$", query.lower()):
            return "当前输入像文件名，不是可访问的下载链接；请粘贴完整 http/https 媒体直链。"
        return f"已为“{query}”生成{media_type}来源建议，可选择官方平台或提供合法直链。"

    def _read_tasks(self) -> list[dict]:
        try:
            data = json.loads(self.task_path.read_text(encoding="utf-8"))
            return data.get("tasks", []) if isinstance(data, dict) else []
        except Exception:
            return []

    def _write_tasks(self, tasks: list[dict]) -> None:
        self.download_root.mkdir(parents=True, exist_ok=True)
        tmp = self.task_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.task_path)

    def _retry_ffmpeg_blocked_tasks(self) -> int:
        tasks = self._read_tasks()
        count = 0
        for task in tasks:
            if (
                str(task.get("action") or "") == "convert"
                and str(task.get("status") or "") == "blocked"
                and "ffmpeg" in str(task.get("failure_reason") or "").lower()
            ):
                self._run_task_once(task, legal_confirmed=True)
                task["updated_at"] = self._now()
                count += 1
        if count:
            self._write_tasks(tasks)
        return count

    def _read_config(self) -> dict:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _ffmpeg_path(self) -> str:
        cfg = self._read_config()
        configured = str(cfg.get("ffmpeg_path") or "").strip()
        if configured and Path(configured).exists():
            return configured
        return shutil.which("ffmpeg") or ""

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


_service: MediaDownloadService | None = None


def get_media_download_service(root: str) -> MediaDownloadService:
    global _service
    root_abs = str(Path(root).resolve())
    if _service is None or str(_service.root) != root_abs:
        _service = MediaDownloadService(root_abs)
    return _service
