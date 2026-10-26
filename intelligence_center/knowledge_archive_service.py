from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
try:
    from intelligence_center.decision_engine import decision_engine
except Exception:
    decision_engine = None


_ARCHIVE_SCHEDULER_STARTED = False


class KnowledgeArchiveService:
    """Archive real conversations, artifacts and automation outputs into knowledge.

    The service is deliberately extractive and conservative: it reads local
    evidence, writes a compact weekly/monthly page into the shared knowledge
    base, and mirrors the same page into Artifact Center.  It does not mutate
    original conversations, tasks or artifacts.
    """

    def __init__(self, root, workspace=None):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace or root).resolve()
        self.state_path = self.root / "knowledge_archive_state.json"
        self.settings_path = self.root / "knowledge_archive_settings.json"
        self.knowledge_dir = self.workspace / "knowledge" / "archives"
        self.reports_dir = self.root / "reports"
        self._model_summary_disabled = False

    DEFAULT_SETTINGS = {
        "enabled": True,
        "run_time": "18:00",
        "catch_up_days": 7,
        "retry": {
            "enabled": True,
            "max_retries": 2,
            "interval_hours": 2,
            "notify_on_final_failure": True,
        },
        "retention": {
            "day_months": 3,
            "week_months": 6,
            "month_years": 3,
            "year_years": 0,
        },
        "quality": {
            "dedupe_enabled": True,
            "summarize_repeated": True,
            "model_summary_enabled": True,
            "min_group_count": 2,
        },
        "categories": ["问题类", "知识类", "学习类", "进化类", "任务产物类"],
    }

    @staticmethod
    def _now():
        return datetime.now().astimezone()

    @staticmethod
    def _read_json(path, default):
        try:
            path = Path(path)
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                return data if isinstance(data, type(default)) else default
        except Exception:
            pass
        return default

    @staticmethod
    def _write_json(path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def settings(self):
        settings = self._read_json(self.settings_path, {})
        merged = json.loads(json.dumps(self.DEFAULT_SETTINGS, ensure_ascii=False))
        if isinstance(settings, dict):
            for key, value in settings.items():
                if key == "retention" and isinstance(value, dict):
                    merged["retention"].update(value)
                elif key == "retry" and isinstance(value, dict):
                    merged["retry"].update(value)
                elif key == "quality" and isinstance(value, dict):
                    merged["quality"].update(value)
                elif key in merged:
                    merged[key] = value
        merged["enabled"] = bool(merged.get("enabled", True))
        if not re.match(r"^\d{1,2}:\d{2}$", str(merged.get("run_time") or "")):
            merged["run_time"] = "18:00"
        retention = merged.get("retention") or {}
        retention["day_months"] = max(1, min(36, int(retention.get("day_months") or 3)))
        retention["week_months"] = max(1, min(36, int(retention.get("week_months") or 6)))
        retention["month_years"] = max(1, min(10, int(retention.get("month_years") or 3)))
        retention["year_years"] = max(0, min(50, int(retention.get("year_years") or 0)))
        merged["retention"] = retention
        retry = merged.get("retry") or {}
        retry["enabled"] = bool(retry.get("enabled", True))
        retry["max_retries"] = max(0, min(10, int(retry.get("max_retries") if retry.get("max_retries") is not None else 2)))
        retry["interval_hours"] = max(1, min(24, int(retry.get("interval_hours") or 2)))
        retry["notify_on_final_failure"] = bool(retry.get("notify_on_final_failure", True))
        merged["retry"] = retry
        quality = merged.get("quality") or {}
        quality["dedupe_enabled"] = bool(quality.get("dedupe_enabled", True))
        quality["summarize_repeated"] = bool(quality.get("summarize_repeated", True))
        quality["model_summary_enabled"] = bool(quality.get("model_summary_enabled", True))
        quality["min_group_count"] = max(2, min(20, int(quality.get("min_group_count") or 2)))
        merged["quality"] = quality
        merged["catch_up_days"] = max(1, min(31, int(merged.get("catch_up_days") or 7)))
        return merged

    def save_settings(self, payload):
        current = self.settings()
        if isinstance(payload, dict):
            if "enabled" in payload:
                current["enabled"] = bool(payload.get("enabled"))
            if "run_time" in payload:
                value = str(payload.get("run_time") or "").strip()
                if re.match(r"^\d{1,2}:\d{2}$", value):
                    hour, minute = [int(x) for x in value.split(":", 1)]
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        current["run_time"] = f"{hour:02d}:{minute:02d}"
            retention = payload.get("retention") or {}
            if isinstance(retention, dict):
                for key in ("day_months", "week_months", "month_years", "year_years"):
                    if key in retention:
                        current["retention"][key] = int(retention[key])
            retry = payload.get("retry") or {}
            if isinstance(retry, dict):
                if "enabled" in retry:
                    current.setdefault("retry", {})["enabled"] = bool(retry.get("enabled"))
                if "max_retries" in retry:
                    current.setdefault("retry", {})["max_retries"] = int(retry.get("max_retries") or 0)
                if "interval_hours" in retry:
                    current.setdefault("retry", {})["interval_hours"] = int(retry.get("interval_hours") or 2)
                if "notify_on_final_failure" in retry:
                    current.setdefault("retry", {})["notify_on_final_failure"] = bool(retry.get("notify_on_final_failure"))
            quality = payload.get("quality") or {}
            if isinstance(quality, dict):
                if "dedupe_enabled" in quality:
                    current.setdefault("quality", {})["dedupe_enabled"] = bool(quality.get("dedupe_enabled"))
                if "summarize_repeated" in quality:
                    current.setdefault("quality", {})["summarize_repeated"] = bool(quality.get("summarize_repeated"))
                if "model_summary_enabled" in quality:
                    current.setdefault("quality", {})["model_summary_enabled"] = bool(quality.get("model_summary_enabled"))
                if "min_group_count" in quality:
                    current.setdefault("quality", {})["min_group_count"] = int(quality.get("min_group_count") or 2)
            if "catch_up_days" in payload:
                current["catch_up_days"] = int(payload.get("catch_up_days") or 7)
            categories = payload.get("categories")
            if isinstance(categories, list):
                clean = [self._compact(item, 16) for item in categories if str(item or "").strip()]
                if clean:
                    current["categories"] = clean[:12]
        retention = current.get("retention") or {}
        retention["day_months"] = max(1, min(36, int(retention.get("day_months") or 3)))
        retention["week_months"] = max(1, min(36, int(retention.get("week_months") or 6)))
        retention["month_years"] = max(1, min(10, int(retention.get("month_years") or 3)))
        retention["year_years"] = max(0, min(50, int(retention.get("year_years") or 0)))
        current["retention"] = retention
        retry = current.get("retry") or {}
        retry["enabled"] = bool(retry.get("enabled", True))
        retry["max_retries"] = max(0, min(10, int(retry.get("max_retries") if retry.get("max_retries") is not None else 2)))
        retry["interval_hours"] = max(1, min(24, int(retry.get("interval_hours") or 2)))
        retry["notify_on_final_failure"] = bool(retry.get("notify_on_final_failure", True))
        current["retry"] = retry
        quality = current.get("quality") or {}
        quality["dedupe_enabled"] = bool(quality.get("dedupe_enabled", True))
        quality["summarize_repeated"] = bool(quality.get("summarize_repeated", True))
        quality["model_summary_enabled"] = bool(quality.get("model_summary_enabled", True))
        quality["min_group_count"] = max(2, min(20, int(quality.get("min_group_count") or 2)))
        current["quality"] = quality
        current["catch_up_days"] = max(1, min(31, int(current.get("catch_up_days") or 7)))
        self._write_json(self.settings_path, current)
        return self.settings()

    @staticmethod
    def _compact(text, limit=160):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        text = re.sub(r"https?://\\S+", "链接", text)
        return text[:limit] + ("…" if len(text) > limit else "")

    def status(self):
        state = self._read_json(self.state_path, {})
        pending = state.get("pending_retries") or {}
        next_retry_at = ""
        try:
            next_retry_at = min(
                str(item.get("next_retry_at") or "")
                for item in pending.values()
                if str(item.get("next_retry_at") or "")
            )
        except Exception:
            next_retry_at = ""
        return {
            "enabled": self.settings().get("enabled", True),
            "knowledge_dir": str(self.knowledge_dir),
            "layout": "year/month/day_week_month + year/yearly",
            "last_daily": state.get("last_daily") or {},
            "last_weekly": state.get("last_weekly") or {},
            "last_monthly": state.get("last_monthly") or {},
            "last_yearly": state.get("last_yearly") or {},
            "last_run_at": state.get("last_run_at") or "",
            "last_due_date": state.get("last_due_date") or "",
            "pending_retries": state.get("pending_retries") or {},
            "pending_retry_count": len(pending),
            "next_retry_at": next_retry_at,
            "last_failures": state.get("last_failures") or [],
            "settings": self.settings(),
        }

    def run_auto(self, period="week", force=False, target_date=None):
        raw_period = str(period or "week").lower()
        if raw_period.startswith(("day", "daily")):
            period = "day"
        elif raw_period.startswith("year"):
            period = "year"
        elif raw_period.startswith("month"):
            period = "month"
        else:
            period = "week"
        start, end, key, title_date = self._period_bounds(period, target_date)
        display_title = self._archive_display_title(period, start, end)
        evidence = self._collect_evidence(start, end)
        signature = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        state = self._read_json(self.state_path, {})
        state_key = {"day": "last_daily", "month": "last_monthly", "year": "last_yearly"}.get(period, "last_weekly")
        previous = state.get(state_key) or {}
        if not force and previous.get("period_key") == key and previous.get("signature") == signature:
            state["last_run_at"] = self._now().isoformat(timespec="seconds")
            self._write_json(self.state_path, state)
            return {"status": "skipped", "message": "知识库归档已是最新，无需重复生成", **previous}

        page = self._render_page(period, display_title, start, end, evidence)
        page_path = self._archive_page_path(period, key, start)
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(page, encoding="utf-8")
        self._update_index(period, key, display_title, page_path)
        artifact = self._register_artifact(page_path, period, key, display_title, evidence)
        result = {
            "status": "success",
            "period": period,
            "period_key": key,
            "signature": signature,
            "knowledge_path": str(page_path),
            "artifact": artifact,
            "summary": {
                "conversations": len(evidence.get("conversations") or []),
                "artifacts": len(evidence.get("artifacts") or []),
                "automation": len(evidence.get("automation") or []),
                "failures": len(evidence.get("failures") or []),
            },
            "message": "知识库归档已生成，并同步保存到产物/交付物",
        }
        state[state_key] = result
        state["last_run_at"] = self._now().isoformat(timespec="seconds")
        self._write_json(self.state_path, state)
        self.cleanup_retention()
        return result

    @staticmethod
    def _cn_date(dt, include_day=True):
        if include_day:
            return f"{dt.year}年{dt.month}月{dt.day}日"
        return f"{dt.year}年{dt.month}月"

    def _archive_display_title(self, period, start, end):
        if period == "day":
            return f"日归档-{self._cn_date(start)}"
        if period == "week":
            last = end - timedelta(days=1)
            if start.year == last.year and start.month == last.month:
                return f"周归档-{start.year}年{start.month}月{start.day}-{last.day}日"
            return f"周归档-{self._cn_date(start)}-{self._cn_date(last)}"
        if period == "month":
            return f"月归档-{start.year}年{start.month}月份"
        if period == "year":
            return f"年归档-{start.year}年"
        return f"自动归档-{self._cn_date(start)}"

    def _period_bounds(self, period, reference=None):
        now = self._coerce_reference_datetime(reference) or self._now()
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            return start, end, start.strftime("%Y-%m-%d"), start.strftime("%Y年%m月%d日")
        if period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
            return start, end, start.strftime("%Y-%m"), start.strftime("%Y年%m月")
        if period == "year":
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = start.replace(year=start.year + 1)
            return start, end, start.strftime("%Y"), start.strftime("%Y年")
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        iso_year, iso_week, _ = now.isocalendar()
        return start, end, f"{iso_year}-W{iso_week:02d}", f"{iso_year}年第{iso_week:02d}周"

    def _coerce_reference_datetime(self, reference):
        if reference is None:
            return None
        if isinstance(reference, datetime):
            return reference.astimezone() if reference.tzinfo else reference.astimezone()
        text = str(reference or "").strip()
        if not text:
            return None
        try:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
                now = self._now()
                parsed = datetime.strptime(text, "%Y-%m-%d")
                return parsed.replace(tzinfo=now.tzinfo)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()
        except Exception:
            return None

    def _archive_page_path(self, period, key, start):
        """Return knowledge archive path using year/month hierarchy.

        Examples:
        - knowledge/archives/2026/09/day-2026-09-15.md
        - knowledge/archives/2026/09/week-2026-W38_2026-09-14.md
        - knowledge/archives/2026/09/month-2026-09.md
        - knowledge/archives/2026/year-2026.md
        """
        year_dir = start.strftime("%Y")
        month_dir = start.strftime("%m")
        _, end, _, _ = self._period_bounds(period, start)
        display_name = self._archive_display_title(period, start, end)
        safe_key = re.sub(r"[^0-9A-Za-z一-龥_.-]+", "-", display_name).strip("-") or str(key)
        if period == "year":
            file_name = f"{safe_key}.md"
            return self.knowledge_dir / year_dir / file_name
        if period == "week":
            file_name = f"{safe_key}.md"
        elif period == "month":
            file_name = f"{safe_key}.md"
        else:
            file_name = f"{safe_key}.md"
        return self.knowledge_dir / year_dir / month_dir / file_name

    def _collect_evidence(self, start, end):
        evidence = {
            "artifacts": self._recent_artifacts(start, end),
            "automation": self._recent_automation(start, end),
            "conversations": self._recent_conversations(start, end),
            "failures": self._recent_failures(start, end),
        }
        return self._compress_evidence(evidence)

    @staticmethod
    def _dedupe_text(text):
        text = re.sub(r"https?://\S+", "链接", str(text or ""))
        text = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", "<date>", text)
        text = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", "<time>", text)
        text = re.sub(r"\b\d{5,}\b", "<num>", text)
        text = re.sub(r"[0-9a-f]{8,}", "<id>", text, flags=re.I)
        text = re.sub(r"@[A-Za-z0-9._-]+", "@<id>", text)
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text[:260]

    @staticmethod
    def _strip_repeated_title(title, summary):
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        summary = re.sub(r"\s+", " ", str(summary or "")).strip()
        if title and summary.startswith(title):
            summary = summary[len(title):].lstrip("，,：:｜|-— ")
        duplicated = re.match(r"^(.{4,60}?)[，,：:｜|-]\s*\1", summary)
        if duplicated:
            summary = re.sub(r"^(.{4,60}?)[，,：:｜|-]\s*\1", r"\1", summary)
        return summary

    def _fingerprint_record(self, record, kind):
        title = record.get("title") or record.get("task") or ""
        summary = record.get("summary") or ""
        channel = record.get("channel") or ""
        status = record.get("status") or ""
        source = record.get("source") or ""
        if kind == "failures":
            base = f"{kind}|{channel}|{title}|{summary}"
        elif kind == "automation":
            base = f"{kind}|{title}|{status}|{summary}"
        else:
            base = f"{kind}|{source}|{title}|{summary}"
        return hashlib.sha256(self._dedupe_text(base).encode("utf-8", errors="ignore")).hexdigest()[:16]

    def _ai_style_group_summary(self, kind, title, summary, group):
        """Create a concise, useful archive summary from repeated local evidence.

        This is intentionally local and deterministic so archiving does not fail
        when the model or network is unavailable.  It behaves like a small
        summarizer: remove repeated wording, keep the meaning, add count/latest
        and a practical next action.
        """
        count = len(group)
        latest = max(str(item.get("created_at") or item.get("last_active") or "") for item in group)
        title = self._compact(title, 64)
        summary = self._strip_repeated_title(title, summary)
        summary = self._compact(summary, 120)
        if kind == "failures":
            channel = group[0].get("channel") or title.replace("通知失败：", "")
            local = (
                f"合并 {count} 条同类通知异常；通道：{channel}；最近一次：{latest or '未知'}。"
                f"核心原因：{summary or '通道发送失败'}。建议先检查该通道连接和默认接收人，避免把重复失败继续写入日归档。"
            )
            return self._model_refine_archive_summary(kind, title, local, group)
        if kind == "automation":
            statuses = sorted({str(item.get("status") or "未知") for item in group})
            local = (
                f"合并 {count} 条同类自动化记录；状态：{'、'.join(statuses[:4])}；最近一次：{latest or '未知'}。"
                f"主要内容：{summary or title}。建议只保留结论、失败原因和后续动作，不再逐条保存重复运行文本。"
            )
            return self._model_refine_archive_summary(kind, title, local, group)
        if kind == "artifacts":
            local = (
                f"合并 {count} 个相似产物；最近一次：{latest or '未知'}。"
                f"核心内容：{summary or title}。建议保留这条综合摘要，原始产物仍可在产物中心按需查看。"
            )
            return self._model_refine_archive_summary(kind, title, local, group)
        local = (
            f"合并 {count} 条相似会话/知识片段；最近一次：{latest or '未知'}。"
            f"核心内容：{summary or title}。建议归档为可检索结论，不重复保存原始对话。"
        )
        return self._model_refine_archive_summary(kind, title, local, group)

    def _model_refine_archive_summary(self, kind, title, local_summary, group):
        quality = self.settings().get("quality") or {}
        if self._model_summary_disabled or not quality.get("model_summary_enabled", True):
            return local_summary
        try:
            from config import conf
            cfg = conf()
            model = str(cfg.get("model") or "")
            if model.startswith("deepseek"):
                key, base = cfg.get("deepseek_api_key"), cfg.get("deepseek_api_base") or "https://api.deepseek.com/v1"
            else:
                key, base = cfg.get("open_ai_api_key"), cfg.get("open_ai_api_base") or "https://api.openai.com/v1"
            if not key or not model:
                return local_summary
            samples = []
            for item in (group or [])[:8]:
                samples.append(f"- {item.get('title') or item.get('task') or title}：{item.get('summary') or ''}")
            prompt = (
                "请把以下重复归档证据总结成一条知识库归档摘要。要求："
                "1）不要逐条复述；2）说明核心事实/问题、影响和下一步建议；"
                "3）80到160字；4）不要编造原文没有的信息。\n\n"
                f"类型：{kind}\n标题：{title}\n本地摘要：{local_summary}\n证据：\n" + "\n".join(samples)
            )
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是知识库归档助手，擅长把重复记录压缩成可检索、可行动的一句话总结。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                base.rstrip("/") + "/chat/completions",
                data=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            text = str(result["choices"][0]["message"]["content"]).strip()
            return self._compact(text, 220) if text else local_summary
        except Exception:
            self._model_summary_disabled = True
            return local_summary

    def _compress_records(self, records, kind):
        quality = self.settings().get("quality") or {}
        if not quality.get("dedupe_enabled", True):
            return list(records or []), {"raw": len(records or []), "kept": len(records or []), "merged": 0, "groups": 0}
        groups = {}
        order = []
        for record in records or []:
            key = self._fingerprint_record(record, kind)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(record)
        min_group_count = int(quality.get("min_group_count") or 2)
        compressed = []
        merged = 0
        merged_groups = 0
        for key in order:
            group = groups.get(key) or []
            first = dict(group[0])
            title = first.get("title") or first.get("task") or "归档项"
            summary = self._strip_repeated_title(title, first.get("summary") or "")
            if len(group) >= min_group_count and quality.get("summarize_repeated", True):
                first["summary"] = self._ai_style_group_summary(kind, title, summary, group)
                first["deduped_count"] = len(group)
                first["deduped_latest_at"] = max(str(item.get("created_at") or item.get("last_active") or "") for item in group)
                merged += len(group) - 1
                merged_groups += 1
            else:
                first["summary"] = self._compact(summary, 140)
            compressed.append(first)
        return compressed, {"raw": len(records or []), "kept": len(compressed), "merged": merged, "groups": merged_groups}

    def _compress_evidence(self, evidence):
        stats = {}
        output = {}
        for kind in ("artifacts", "automation", "conversations", "failures"):
            output[kind], stats[kind] = self._compress_records(evidence.get(kind) or [], kind)
        output["_compression"] = stats
        output["_raw_counts"] = {key: (stats.get(key) or {}).get("raw", 0) for key in stats}
        return output

    def _in_period(self, value, start, end):
        try:
            if isinstance(value, (int, float)):
                dt = datetime.fromtimestamp(value).astimezone()
            else:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.astimezone()
            return start <= dt.astimezone() < end
        except Exception:
            return False

    def _recent_artifacts(self, start, end, limit=30):
        items = self._read_json(self.root / "artifacts.json", {}).get("artifacts") or []
        result = []
        for item in items:
            if not self._in_period(item.get("created_at"), start, end):
                continue
            title = str(item.get("title") or "产物")
            source = str(item.get("source") or "")
            item_type = str(item.get("type") or "")
            if source == "knowledge_archive" or item_type == "knowledge_archive":
                continue
            if any(word in title for word in ("训练场评测报告", "自我优化诊断报告", "通知通道测试", "日归档-", "周归档-", "月归档-", "年归档-")):
                continue
            result.append({
                "title": title,
                "summary": self._compact(item.get("preview") or item.get("result_summary") or "", 120),
                "source": source,
                "artifact_id": item.get("id") or "",
                "created_at": item.get("created_at") or "",
            })
        return result[:limit]

    def _recent_automation(self, start, end, limit=30):
        runs = self._read_json(self.root / "automation_runs.json", {}).get("runs") or []
        if isinstance(runs, dict):
            runs = list(runs.values())
        result = []
        for item in runs or []:
            when = item.get("finished_at") or item.get("started_at") or item.get("created_at")
            if not self._in_period(when, start, end):
                continue
            result.append({
                "task": item.get("task_name") or item.get("name") or "自动化任务",
                "status": item.get("status") or "",
                "summary": self._compact(item.get("result_summary") or item.get("error") or item.get("failure_reason") or "", 120),
                "run_id": item.get("id") or item.get("run_id") or "",
                "created_at": when or "",
            })
        return result[:limit]

    def _recent_conversations(self, start, end, limit=20):
        result = []
        try:
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            sessions = store.list_sessions(page=1, page_size=30).get("sessions") or []
            for session in sessions:
                if not self._in_period(session.get("last_active"), start, end):
                    continue
                messages = store.load_messages(session.get("session_id"), max_turns=6)
                text = self._messages_text(messages)
                if not text:
                    continue
                result.append({
                    "title": session.get("title") or "会话",
                    "summary": self._compact(text, 160),
                    "session_id": session.get("session_id") or "",
                    "last_active": session.get("last_active") or "",
                })
                if len(result) >= limit:
                    break
        except Exception:
            pass
        return result

    def _recent_failures(self, start, end, limit=20):
        failures = []
        events = self._read_json(self.root / "notification_events.json", {}).get("events") or []
        for item in events[-200:]:
            if item.get("success") or not self._in_period(item.get("created_at"), start, end):
                continue
            count = int(item.get("noise_count") or 1)
            message = item.get("message") or item.get("verification_message") or item.get("error") or "发送失败"
            if count > 1:
                message = f"{message}（同类问题已合并 {count - 1} 次）"
            failures.append({
                "title": f"通知失败：{item.get('channel') or item.get('target') or 'unknown'}",
                "summary": self._compact(message, 160),
                "channel": item.get("channel") or item.get("target") or "",
                "noise_count": count,
                "created_at": item.get("created_at") or "",
            })
        return failures[:limit]

    @staticmethod
    def _messages_text(messages):
        chunks = []
        for msg in messages or []:
            role = msg.get("role") or ""
            content = msg.get("content")
            if isinstance(content, list):
                text = " ".join(str(x.get("text") or x.get("content") or "") for x in content if isinstance(x, dict))
            else:
                text = str(content or "")
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                chunks.append(f"{role}: {text[:220]}")
        return " ".join(chunks)

    def _render_page(self, period, title_date, start, end, evidence):
        categorized = self._categorize_evidence(evidence)
        compression = evidence.get("_compression") or {}
        raw_counts = evidence.get("_raw_counts") or {}
        merged_total = sum(int((compression.get(key) or {}).get("merged") or 0) for key in compression)
        lines = [
            f"# {title_date}",
            "",
            f"范围：{start.strftime('%Y-%m-%d')} 至 {(end - timedelta(seconds=1)).strftime('%Y-%m-%d')}",
            "",
            "## 本期摘要",
            f"- 会话：{len(evidence.get('conversations') or [])} 条（原始 {raw_counts.get('conversations', len(evidence.get('conversations') or []))} 条）",
            f"- 产物/交付物：{len(evidence.get('artifacts') or [])} 条（原始 {raw_counts.get('artifacts', len(evidence.get('artifacts') or []))} 条）",
            f"- 自动化运行：{len(evidence.get('automation') or [])} 条（原始 {raw_counts.get('automation', len(evidence.get('automation') or []))} 条）",
            f"- 失败/异常：{len(evidence.get('failures') or [])} 条（原始 {raw_counts.get('failures', len(evidence.get('failures') or []))} 条）",
            f"- 去重压缩：已合并 {merged_total} 条重复或低价值记录，归档只保留综合结论。",
            "",
            "## 分类归档",
            "",
        ]
        for title in self.settings().get("categories") or self.DEFAULT_SETTINGS["categories"]:
            items = categorized.get(title) or []
            lines.extend([f"### {title}", ""])
            if not items:
                lines.extend(["本期暂无新增。", ""])
                continue
            for idx, item in enumerate(items[:12], 1):
                suffix = f"（合并 {int(item.get('deduped_count') or 1)} 条）" if int(item.get("deduped_count") or 1) > 1 else ""
                lines.append(f"{idx}. {item.get('title') or '未命名'}{suffix}")
                lines.append(f"   {item.get('summary') or '暂无摘要'}")
            lines.append("")
        lines.extend([
            "## 后续行动建议",
            "- 问题类内容进入失败经验和自我进化队列，避免重复提醒同一旧问题。",
            "- 知识类和学习类内容优先补充来源、标签和可复用方法，便于后续检索。",
            "- 进化类内容用于更新 Agent 规则、评测用例和回放验证。",
        ])
        return "\n".join(lines).strip() + "\n"

    def _categorize_evidence(self, evidence):
        result = {name: [] for name in (self.settings().get("categories") or self.DEFAULT_SETTINGS["categories"])}

        def add(category, title, summary, source="", decision=None, extra=None):
            if category not in result:
                result[category] = []
            payload = {
                "title": self._compact(title or "未命名", 80),
                "summary": self._compact(summary or "暂无摘要", 220),
                "source": source,
                "importance": (decision or {}).get("importance"),
                "decision_reason": (decision or {}).get("reason") or "",
            }
            if isinstance(extra, dict):
                for key in ("deduped_count", "deduped_latest_at"):
                    if key in extra:
                        payload[key] = extra.get(key)
            result[category].append(payload)

        for item in evidence.get("failures") or []:
            decision = self._archive_decision(item, "failure")
            add(decision.get("category") or "问题类", item.get("title"), item.get("summary"), "failure", decision, item)

        for item in evidence.get("artifacts") or []:
            decision = self._archive_decision(item, "artifact")
            add(decision.get("category") or "知识类", item.get("title"), item.get("summary"), "artifact", decision, item)

        for item in evidence.get("automation") or []:
            title = item.get("task") or "自动化任务"
            status = item.get("status") or ""
            decision = self._archive_decision({"title": title, "summary": f"{status}｜{item.get('summary') or '暂无摘要'}", "status": status}, "automation")
            add(decision.get("category") or ("问题类" if status in {"failed", "error", "blocked"} else "任务产物类"), title, f"{status}｜{item.get('summary') or '暂无摘要'}", "automation", decision, item)

        for item in evidence.get("conversations") or []:
            decision = self._archive_decision(item, "conversation")
            add(decision.get("category") or "知识类", item.get("title"), item.get("summary"), "conversation", decision, item)

        return result

    @staticmethod
    def _archive_decision(item, source):
        try:
            if decision_engine:
                return decision_engine().archive_item(item or {}, source)
        except Exception:
            pass
        text = f"{(item or {}).get('title')} {(item or {}).get('summary')} {(item or {}).get('status')} {source}"
        if re.search(r"失败|错误|异常|报错|failed|error", text, re.I):
            return {"category": "问题类", "importance": 0.8, "reason": "规则分类"}
        if re.search(r"学习|读书|笔记", text, re.I):
            return {"category": "学习类", "importance": 0.7, "reason": "规则分类"}
        if re.search(r"优化|进化|训练|评测", text, re.I):
            return {"category": "进化类", "importance": 0.75, "reason": "规则分类"}
        if re.search(r"任务|简报|自动化|产物|报告", text, re.I):
            return {"category": "任务产物类", "importance": 0.65, "reason": "规则分类"}
        return {"category": "知识类", "importance": 0.55, "reason": "规则分类"}

    def _update_index(self, period, key, title_date, page_path):
        index = self.workspace / "knowledge" / "index.md"
        index.parent.mkdir(parents=True, exist_ok=True)
        rel = page_path.relative_to(self.workspace).as_posix()
        period_label = {"day": "日归档", "week": "周归档", "month": "月归档", "year": "年归档"}.get(period, "自动归档")
        line = f"- {title_date}：[{title_date}]({rel})"
        old = index.read_text(encoding="utf-8") if index.exists() else "# Knowledge Index\n"
        if line not in old:
            index.write_text(old.rstrip() + "\n" + line + "\n", encoding="utf-8")

    def _register_artifact(self, page_path, period, key, title_date, evidence):
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.reports_dir / f"knowledge-archive-{period}-{key}.md"
        report_path.write_text(page_path.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            from intelligence_center.artifact_service import ArtifactService
            return ArtifactService(str(self.root)).register(str(report_path), {
                "title": title_date,
                "type": "knowledge_archive",
                "source": "knowledge_archive",
                "source_id": f"{period}:{key}",
                "tags": ["知识库", "自动归档", "会话总结"],
                "result_summary": f"会话 {len(evidence.get('conversations') or [])}，产物 {len(evidence.get('artifacts') or [])}，自动化 {len(evidence.get('automation') or [])}",
            })
        except Exception:
            return {}

    def run_due(self, now=None, force=False):
        settings = self.settings()
        if not settings.get("enabled", True):
            return {"status": "disabled"}
        now = now or self._now()
        run_time = settings.get("run_time") or "18:00"
        hour, minute = [int(x) for x in run_time.split(":", 1)]
        state = self._read_json(self.state_path, {})
        retry_results = self._process_due_retries(state, now)
        results = {}
        if force:
            today_key = now.strftime("%Y-%m-%d")
            results["day"] = self._run_with_retry("day", today_key, force=True, trigger="manual")
            due_dates = [today_key]
        else:
            due_dates = self._daily_due_dates(state, now, settings)
            if (now.hour, now.minute) < (hour, minute) and not due_dates:
                return {"status": "waiting", "retry_results": retry_results}
            if not due_dates and not retry_results:
                return {"status": "skipped", "message": "知识库归档已是最新"}
            for date_key in due_dates:
                results[f"day:{date_key}"] = self._run_with_retry("day", date_key, force=False, trigger="auto")

        should_rollup = bool(force or due_dates)
        if should_rollup and now.weekday() == 6:
            results["week"] = self._run_with_retry("week", now.strftime("%Y-%m-%d"), force=force, trigger="auto")
        tomorrow = now + timedelta(days=1)
        if should_rollup and tomorrow.month != now.month:
            results["month"] = self._run_with_retry("month", now.strftime("%Y-%m-%d"), force=force, trigger="auto")
        if should_rollup and tomorrow.year != now.year:
            results["year"] = self._run_with_retry("year", now.strftime("%Y-%m-%d"), force=force, trigger="auto")
        state = self._read_json(self.state_path, {})
        successful_dates = [
            key.split(":", 1)[1]
            for key, value in results.items()
            if key.startswith("day:") and value.get("status") in {"success", "skipped"}
        ]
        if force and results.get("day", {}).get("status") in {"success", "skipped"}:
            successful_dates.append(due_dates[-1])
        if successful_dates:
            state["last_due_date"] = max(successful_dates)
        state["last_due_run_at"] = self._now().isoformat(timespec="seconds")
        self._write_json(self.state_path, state)
        cleaned = self.cleanup_retention(settings)
        any_failed = any((item or {}).get("status") == "failed" for item in results.values())
        return {"status": "partial_failed" if any_failed else "success", "results": results, "retry_results": retry_results, "cleaned": cleaned}

    def run_manual(self, period="day", target_date=None, force=True):
        key = target_date or self._now().strftime("%Y-%m-%d")
        result = self._run_with_retry(period, key, force=force, trigger="manual")
        if str(period or "").lower().startswith(("day", "daily")) and result.get("status") in {"success", "skipped"}:
            state = self._read_json(self.state_path, {})
            current = str(state.get("last_due_date") or "")
            if not current or key > current:
                state["last_due_date"] = key
                state["last_due_run_at"] = self._now().isoformat(timespec="seconds")
                self._write_json(self.state_path, state)
        return result

    def _daily_due_dates(self, state, now, settings):
        latest = now.date()
        run_time = settings.get("run_time") or "18:00"
        hour, minute = [int(x) for x in run_time.split(":", 1)]
        if (now.hour, now.minute) < (hour, minute):
            latest = latest - timedelta(days=1)
        last_key = state.get("last_due_date") or (state.get("last_daily") or {}).get("period_key") or ""
        try:
            start = datetime.strptime(last_key, "%Y-%m-%d").date() + timedelta(days=1)
        except Exception:
            start = latest
        catch_up_days = int(settings.get("catch_up_days") or 7)
        min_start = latest - timedelta(days=catch_up_days - 1)
        if start < min_start:
            start = min_start
        dates = []
        while start <= latest:
            key = start.strftime("%Y-%m-%d")
            if not self._has_archive_file("day", key, start):
                dates.append(key)
            else:
                state["last_due_date"] = key
            start += timedelta(days=1)
        if state.get("last_due_date"):
            self._write_json(self.state_path, state)
        return dates

    def _has_archive_file(self, period, key, date_value):
        path = self._archive_page_path(period, key, self._coerce_reference_datetime(str(date_value)) or self._now())
        return path.exists()

    def _run_with_retry(self, period, date_key, force=False, trigger="auto"):
        job_id = f"{period}:{date_key}"
        try:
            result = self.run_auto(period, force=force, target_date=date_key)
            state = self._read_json(self.state_path, {})
            pending = state.get("pending_retries") or {}
            pending.pop(job_id, None)
            state["pending_retries"] = pending
            self._write_json(self.state_path, state)
            return result
        except Exception as error:
            return self._record_failure(job_id, period, date_key, error, trigger)

    def _record_failure(self, job_id, period, date_key, error, trigger):
        settings = self.settings()
        retry = settings.get("retry") or {}
        now = self._now()
        state = self._read_json(self.state_path, {})
        pending = state.get("pending_retries") or {}
        previous = pending.get(job_id) or {}
        retries_done = int(previous.get("retries_done") or 0)
        max_retries = int(retry.get("max_retries") if retry.get("max_retries") is not None else 2)
        final_failed = not retry.get("enabled", True) or retries_done >= max_retries
        entry = {
            "job_id": job_id,
            "period": period,
            "date": date_key,
            "trigger": trigger,
            "error": str(error),
            "last_failed_at": now.isoformat(timespec="seconds"),
            "retries_done": retries_done,
            "max_retries": max_retries,
            "status": "failed_final" if final_failed else "retry_scheduled",
        }
        if final_failed:
            pending.pop(job_id, None)
            if retry.get("notify_on_final_failure", True):
                self._notify_archive_failure(entry)
        else:
            entry["next_retry_at"] = (now + timedelta(hours=int(retry.get("interval_hours") or 2))).isoformat(timespec="seconds")
            pending[job_id] = entry
        failures = state.get("last_failures") or []
        failures.append(entry)
        state["last_failures"] = failures[-30:]
        state["pending_retries"] = pending
        state["last_run_at"] = now.isoformat(timespec="seconds")
        self._write_json(self.state_path, state)
        return {"status": "failed", "message": str(error), "retry": entry}

    def _process_due_retries(self, state, now):
        pending = dict(state.get("pending_retries") or {})
        results = {}
        for job_id, item in list(pending.items()):
            try:
                next_retry = datetime.fromisoformat(str(item.get("next_retry_at") or "").replace("Z", "+00:00"))
                if next_retry.tzinfo is None:
                    next_retry = next_retry.astimezone()
                if now < next_retry.astimezone():
                    continue
            except Exception:
                continue
            item["retries_done"] = int(item.get("retries_done") or 0) + 1
            state = self._read_json(self.state_path, {})
            pending_state = state.get("pending_retries") or {}
            pending_state[job_id] = item
            state["pending_retries"] = pending_state
            self._write_json(self.state_path, state)
            results[job_id] = self._run_with_retry(item.get("period") or "day", item.get("date") or "", force=True, trigger="retry")
        return results

    def _notify_archive_failure(self, entry):
        period_label = {"day": "日归档", "week": "周归档", "month": "月归档", "year": "年归档"}.get(entry.get("period"), "归档")
        report = (
            f"知识库{period_label}失败提醒\n\n"
            f"- 日期：{entry.get('date')}\n"
            f"- 已按设置重试：{entry.get('retries_done', 0)} / {entry.get('max_retries', 0)} 次\n"
            f"- 失败原因：{entry.get('error') or '未知错误'}\n"
            f"- 建议：打开知识库设置，点击“手动一键归档”重新执行；如果仍失败，请查看系统日志。"
        )
        self._notify_archive_failure_inbox(entry, period_label, report)
        try:
            from plugins.intelligence_center.notifications import NotificationDispatcher
            task = {
                "name": "知识库自动归档",
                "notifications": NotificationDispatcher.default_targets(),
            }
            NotificationDispatcher().dispatch(task, report)
        except Exception:
            pass

    def _notify_archive_failure_inbox(self, entry, period_label, report):
        try:
            from intelligence_center.inbox_service import InboxStore
            source_id = f"knowledge-archive-failure:{entry.get('period')}:{entry.get('date')}"
            InboxStore(self.root).upsert(
                source_id=source_id,
                kind="system_notice",
                title=f"知识库{period_label}失败",
                summary=f"{entry.get('date')} 归档失败，已重试 {entry.get('retries_done', 0)}/{entry.get('max_retries', 0)} 次：{entry.get('error') or '未知错误'}",
                status="warning",
                target={"view": "knowledge", "action": "archive_failure"},
                payload={"archive_failure": entry, "report": report},
                unread=True,
            )
        except Exception:
            pass

    def cleanup_retention(self, settings=None):
        settings = settings or self.settings()
        retention = settings.get("retention") or {}
        now = self._now()
        rules = {
            "day": int(retention.get("day_months") or 3) * 31,
            "week": int(retention.get("week_months") or 6) * 31,
            "month": int(retention.get("month_years") or 3) * 366,
            "year": int(retention.get("year_years") or 0) * 366,
        }
        removed = {"day": 0, "week": 0, "month": 0, "year": 0}
        for root in (self.knowledge_dir, self.reports_dir):
            if not root.exists():
                continue
            for path in list(root.rglob("*.md")):
                name = path.name
                period = None
                if name.startswith("knowledge-archive-"):
                    match = re.match(r"knowledge-archive-(day|week|month|year)-", name)
                    period = match.group(1) if match else None
                else:
                    match = re.match(r"(day|week|month|year)-", name)
                    period = match.group(1) if match else None
                if not period or not rules.get(period):
                    continue
                try:
                    age_days = (now.timestamp() - path.stat().st_mtime) / 86400
                    if age_days > rules[period]:
                        path.unlink()
                        removed[period] += 1
                except OSError:
                    continue
        self._remove_empty_dirs(self.knowledge_dir)
        return removed

    def _remove_empty_dirs(self, root):
        try:
            for current, dirs, files in os.walk(root, topdown=False):
                path = Path(current)
                if path == root:
                    continue
                try:
                    if not any(path.iterdir()):
                        path.rmdir()
                except OSError:
                    pass
        except Exception:
            pass


def start_knowledge_archive_scheduler(root, workspace=None, logger=None):
    global _ARCHIVE_SCHEDULER_STARTED
    if _ARCHIVE_SCHEDULER_STARTED:
        return False
    _ARCHIVE_SCHEDULER_STARTED = True

    def loop():
        service = KnowledgeArchiveService(root, workspace)
        while True:
            try:
                service.run_due()
            except Exception as error:
                if logger:
                    logger.warning(f"[KnowledgeArchive] Auto archive skipped: {error}")
            time.sleep(60)

    thread = threading.Thread(target=loop, name="knowledge-archive-scheduler", daemon=True)
    thread.start()
    return True
