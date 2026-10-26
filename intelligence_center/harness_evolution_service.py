import json
import os
import shutil
import uuid
import re
import hashlib
import threading
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class HarnessEvolutionService:
    """Observability-driven evolution layer for CowAgent's harness.

    This does not train model weights. It inspects the agent harness around the
    model: prompts/config, tools, skills, MCP, sub-agents, memory and tasks.
    """

    _lock = threading.RLock()
    POLICY_PATCHES = {
        "notification-fallback": {"allow_web_fallback": True},
        "search-recovery-policy": {"allow_expand_time": True},
    }
    SNAPSHOT_FILES = [
        "agent_roster.json",
        "custom_templates.json",
        "connector_policies.json",
        "personal_agent_profile.json",
        "project_knowledge.json",
        "agent2_settings.json",
    ]

    def __init__(self, root, workspace=None):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace or root).resolve()
        self.state_path = self.root / "harness_evolution.json"
        self.snapshot_dir = self.root / "harness_snapshots"

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _read_json(path, default):
        try:
            path = Path(path)
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
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
        os.replace(tmp, path)

    def _state(self):
        data = self._read_json(self.state_path, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("snapshots", [])
        data.setdefault("events", [])
        data.setdefault("created_at", self._now())
        return data

    def _save_state(self, data):
        data["updated_at"] = self._now()
        data["snapshots"] = (data.get("snapshots") or [])[:80]
        data["events"] = (data.get("events") or [])[:300]
        self._write_json(self.state_path, data)

    def dashboard(self):
        from intelligence_center.task_rule_versions import TaskRuleVersions
        from intelligence_center.agent_training_lab_service import AgentTrainingLabService
        from intelligence_center.knowledge_archive_service import KnowledgeArchiveService
        components = self.components()
        traces = self.traces(20)
        diagnostics = self.diagnostics(20)
        suggestions = self.optimization_suggestions(diagnostics, components)
        state = self._state()
        training_lab = AgentTrainingLabService(self.root, self.workspace).dashboard()
        knowledge_archive = KnowledgeArchiveService(self.root, self.workspace).status()
        return {
            "summary": {
                "components": len(components),
                "enabled_components": sum(1 for item in components if item.get("enabled")),
                "traces": len(traces),
                "diagnostics": len(diagnostics),
                "suggestions": len(suggestions),
                "snapshots": len(state.get("snapshots") or []),
                "training_cases": training_lab.get("summary", {}).get("cases", 0),
                "training_avg_score": training_lab.get("summary", {}).get("avg_score", 0),
                "knowledge_archived": 1 if (knowledge_archive.get("last_weekly") or {}).get("knowledge_path") else 0,
            },
            "components": components,
            "traces": traces,
            "diagnostics": diagnostics,
            "suggestions": suggestions,
            "snapshots": state.get("snapshots") or [],
            "events": state.get("events") or [],
            "task_versions": TaskRuleVersions(self.root).list(),
            "proposals": [x for x in self._read_json(self.root / "agent2_state.json", {}).get("proposals", []) if str(x.get("source_key", "")).startswith("harness:")][:20],
            "training_lab": training_lab,
            "knowledge_archive": knowledge_archive,
        }

    def components(self):
        runs = self._automation_runs()
        agents = self._read_json(self.root / "agent_roster.json", {}).get("agents") or []
        agent2 = self._read_json(self.root / "agent2_state.json", {})
        components = []

        def add(component_id, name, kind, enabled=True, source="", usage=0, success=0, total=0, last_failure="", version_count=0):
            success_rate = round(success / total * 100, 1) if total else None
            components.append({
                "id": component_id,
                "name": name,
                "type": kind,
                "enabled": enabled,
                "source": source,
                "usage_count": int(usage or 0),
                "success_rate": success_rate,
                "last_failure": last_failure,
                "version_count": int(version_count or 0),
                "rollback_available": version_count > 0,
            })

        add("system-prompt", "系统提示词 / 会话策略", "系统提示词", None, "config + agent runtime", version_count=self._snapshot_count("system-prompt"))
        add("mcp-cli", "MCP / CLI 连接", "MCP/CLI", None, "MCP/CLI 管理")
        add("skills", "Skill 能力", "Skill", None, "技能与管理", version_count=self._skill_version_count(agent2))
        add("memory", "长期记忆 / Memory Wiki", "Memory", agent2.get("settings", {}).get("capabilities", {}).get("memory_extract", {}).get("enabled"), "fact_store", version_count=self._snapshot_count("memory"))
        add("automation", "自动化任务 Harness", "自动化", True, "unified_tasks + runs", len(runs), self._run_successes(runs), len(runs), self._last_failure(runs), self._snapshot_count("automation"))
        add("sub-agents", "Agent 名册 / Sub-Agent", "Sub-Agent", any(x.get("status")=="enabled" for x in agents), "agent_roster", version_count=self._snapshot_count("sub-agents"))
        events = self._tool_events()
        for name in sorted({str(x.get("tool")) for x in events if x.get("tool")}):
            samples = [x for x in events if x.get("tool")==name]
            failures = [x for x in events if x.get("tool")==name and not x.get("success")]
            add("tool:"+name, name, "Skill" if name.startswith("skill:") else "工具", None,
                "tool_audit.jsonl", len(samples), sum(x.get("success") is True for x in samples), len(samples),
                str(failures[-1].get("error_code") or "失败") if failures else "")
        for agent in agents[:20]:
            agent_runs = [run for run in runs if self._agent_hit(run, agent)]
            add(
                f"agent:{agent.get('id')}",
                agent.get("name") or agent.get("id") or "Agent",
                "业务 Agent",
                agent.get("status") != "disabled",
                agent.get("entry") or "agent_roster",
                len(agent_runs),
                self._run_successes(agent_runs),
                len(agent_runs),
                self._last_failure(agent_runs),
                self._snapshot_count(f"agent:{agent.get('id')}"),
            )
        skill_root = self.workspace / "skills"
        if skill_root.is_dir():
            for path in sorted(skill_root.glob("*/SKILL.md"))[:200]:
                if path.resolve().is_relative_to(skill_root.resolve()):
                    name = path.parent.name
                    if not any(x["id"] == "tool:skill:"+name for x in components):
                        add("skill:"+name, name, "Skill", None, "已安装；启用状态以技能管理为准")
        return components

    def traces(self, limit=20):
        events = self._tool_events()
        runs = sorted(self._automation_runs(), key=lambda x: str(x.get("started_at") or x.get("finished_at") or x.get("created_at") or ""), reverse=True)
        traces = []
        for item in runs[: max(1, int(limit or 20))]:
            status = item.get("status") or ""
            failure = item.get("failure_reason") or item.get("empty_reason") or item.get("error") or ""
            matched = [x for x in events if item.get("id") and item['id'] in {x.get('run_id'), x.get('parent_run_id')}]
            tools = list(dict.fromkeys(str(x["tool"]) for x in matched if x.get("tool")))
            notifications = item.get("notification_results") or []
            weixin = [x for x in notifications if x.get("channel") in {"weixin", "wechat"}]
            artifact = item.get("artifact") if isinstance(item.get("artifact"), dict) else {}
            traces.append({
                "id": item.get("id") or "",
                "source": "automation",
                "created_at": item.get("started_at") or item.get("finished_at") or item.get("created_at") or "",
                "user_request": item.get("task_name") or item.get("name") or "自动化任务",
                "agent_decision": item.get("result_summary") or item.get("summary") or ("空结果" if status == "empty" else status),
                "tools": tools,
                "sources": self._extract_sources(item),
                "failed_step": failure or ("通知发送" if item.get("notification_warning") else ""),
                "fallback": "已自动兜底" if item.get("recovery_actions") or item.get("recovery_source_run_id") else "未触发",
                "artifact_written": True if artifact.get("id") else None,
                "notification_written": all(x.get("success") is True for x in notifications) if notifications else None,
                "weixin_written": all(x.get("success") is True for x in weixin) if weixin else None,
                "status": status,
                "session_id": item.get('session_id') or '',
                "stages": [{"name": self._redact(s.get('name')), "status": s.get('status'),
                            "at": s.get('at'), "message": self._redact(s.get('message'))}
                           for s in item.get('stages', []) if isinstance(s, dict)] + self._audit_stages(matched),
                "sources_label": "来源配置（不代表已实际读取）",
            })
        known = {x.get('id') for x in runs if x.get('id')}
        from common.task_trace import read_events
        linked = {}
        for event in read_events(self.root):
            linked.setdefault(event['id'], []).append(event)
        for run_id, items in linked.items():
            if run_id in known:
                continue
            last = items[-1]
            origin = next((x for x in items if x.get('source')), last)
            matched = [x for x in events if run_id in {x.get('run_id'), x.get('parent_run_id')}]
            deliveries = [x for x in items if x.get('notification_written') is not None]
            delivery = deliveries[-1]['notification_written'] if deliveries else None
            artifacts = [x['artifact_id'] for x in items if x.get('artifact_id')]
            failed = [x for x in items if x.get('status') in {'failed', 'empty'}]
            traces.append({'id': run_id, 'source': origin.get('source'), 'created_at': last.get('at'),
                           'session_id': origin.get('session_id'), 'source_id': origin.get('source_id'),
                           'user_request': '会话任务（原文请查看来源会话）',
                           'agent_decision': '以下为实际执行事件；生成回复不等于已送达或目标已完成',
                           'status': 'failed' if failed else 'observed',
                           'tools': list(dict.fromkeys(x.get('tool') for x in matched if x.get('tool'))),
                           'sources': [], 'sources_label': '实际来源（未记录）',
                           'failed_step': '、'.join(dict.fromkeys(x['stage'] for x in failed)),
                           'fallback': '暂无明确记录', 'artifact_written': True if artifacts else None,
                           'artifact_ids': artifacts, 'notification_written': delivery,
                           'device_received': True if any(x.get('device_received') is True for x in items) else None,
                           'weixin_written': delivery if origin.get('source') in {'weixin', 'wechat'} else None,
                           'stages': [{'name': x['stage'], 'status': x['status'], 'at': x['at'], 'message': ''} for x in items] + self._audit_stages(matched)})
        known.update(linked)
        groups = {}
        for event in events:
            if event.get('run_id') in known or event.get('parent_run_id') in known:
                continue
            key = event.get('run_id') or ('unlinked:'+self._digest(event)[:16])
            groups.setdefault(key, []).append(event)
        for run_id, items in groups.items():
            last = items[-1]
            traces.append({'id': run_id, 'source': 'tool_audit', 'created_at': last.get('timestamp', ''),
                           'user_request': '工具执行：'+str(last.get('intent') or last.get('tool') or '未分类'),
                           'agent_decision': '工具记录；未关联最终回复，不能据此判断整项任务成功',
                           'tools': list(dict.fromkeys(x.get('tool') for x in items if x.get('tool'))),
                           'sources': [], 'sources_label': '实际来源（未记录）',
                           'failed_step': '、'.join(str(x.get('tool')) for x in items if x.get('success') is False),
                           'fallback': '暂无明确记录', 'artifact_written': None, 'notification_written': None,
                           'weixin_written': None, 'status': 'observed', 'session_id': last.get('session_id', ''),
                           'stages': self._audit_stages(items)})
        return sorted(traces, key=lambda x: self._trace_time(x.get('created_at')), reverse=True)[:max(1, int(limit or 20))]

    @staticmethod
    def _trace_time(value):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return dt.timestamp()
        except (ValueError, TypeError, OSError):
            return 0

    def _audit_stages(self, events):
        return [{'name': self._redact(x.get('tool')), 'status': x.get('status'), 'at': x.get('timestamp'),
                 'message': self._redact(x.get('error_code') or ('工具调用成功' if x.get('success') is True else '工具调用未成功')),
                 'duration_seconds': x.get('execution_time')} for x in events]

    def diagnostics(self, limit=20):
        rows = []
        for run in self._automation_runs():
            status = run.get("status")
            reason = run.get("failure_reason") or run.get("empty_reason") or run.get("error") or ""
            if status not in {"failed", "empty"} and not reason:
                continue
            rows.append(self._diagnose_run(run, reason))
        notification_diag = self._notification_diagnostic()
        if notification_diag:
            rows.append(notification_diag)
        agent_reach_diag = self._agent_reach_diagnostic()
        if agent_reach_diag:
            rows.append(agent_reach_diag)
        agent2 = self._read_json(self.root / "agent2_state.json", {})
        for item in agent2.get("error_patterns") or []:
            rows.append({
                "id": item.get("id") or uuid.uuid4().hex[:12],
                "phenomenon": self._redact(item.get("fingerprint") or "重复错误"),
                "root_cause": self._redact(item.get("lesson") or "重复错误达到阈值，需结合原始日志确认根因。"),
                "evidence": f"出现 {item.get('count') or 0} 次；最近 {item.get('last_seen') or ''}",
                "fix": "查看日志和执行 Trace，确认后沉淀为 Memory 或 Skill Patch。",
                "auto_fixable": False,
                "next_path": "进入 Agent 2.0 / Memory Wiki 复核",
                "source_id": item.get("id") or "",
                "created_at": item.get("last_seen") or "",
                "status": item.get("status") or "pending",
            })
        rows = sorted(rows, key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return rows[: max(1, int(limit or 20))]

    def _agent_reach_diagnostic(self):
        state = self._read_json(self.root / "agent_reach_health.json", {})
        if bool(state.get("installed")) and bool(state.get("doctor_ok")):
            return None
        try:
            import shutil as _shutil
            installed = bool(_shutil.which("agent-reach"))
        except Exception:
            installed = False
        doctor_ok = bool(state.get("doctor_ok"))
        if installed and doctor_ok:
            return None
        if installed:
            root = "Agent-Reach 已安装但最近 doctor 未通过或尚未完成健康检查，复杂来源会退回普通搜索/RSS。"
            fix = "运行 Agent-Reach doctor；根据缺失依赖修复浏览器、GitHub、视频或社媒来源；修复前保留 RSS/普通搜索兜底。"
            evidence = state.get("message") or state.get("doctor_output") or "未发现最近成功的 Agent-Reach doctor 记录。"
        else:
            root = "Agent-Reach 未安装或命令不在 PATH，复杂来源不可用。"
            fix = "在 Agent-Reach 面板执行安装/更新；若网络或权限失败，保留普通搜索/RSS 兜底并把失败写入训练样本。"
            evidence = "本机未检测到 agent-reach 命令。"
        return {
            "id": "agent-reach-not-ready",
            "phenomenon": "Agent-Reach 未就绪",
            "root_cause": root,
            "evidence": self._redact(evidence)[:500],
            "fix": fix,
            "auto_fixable": False,
            "next_path": "互联网来源：Agent-Reach doctor / 安装修复 + RSS/普通搜索兜底",
            "source_id": "agent-reach:not-ready",
            "created_at": state.get("checked_at") or self._now(),
            "status": "pending",
        }

    def _notification_diagnostic(self):
        data = self._read_json(self.root / "notification_events.json", {})
        events = data.get("events") if isinstance(data, dict) else []
        if not isinstance(events, list):
            return None
        failures = [x for x in events[-300:] if not x.get("success") and self._within_days(x.get("created_at"), 7)]
        if not failures:
            return None
        groups = {}
        latest = ""
        for item in failures:
            channel = str(item.get("channel") or item.get("target") or "unknown").strip() or "unknown"
            reason = self._redact(item.get("message") or item.get("error") or "发送失败")
            task = str(item.get("task_name") or item.get("name") or "").strip()
            key = f"{channel}|{reason}"
            entry = groups.setdefault(key, {"channel": channel, "reason": reason, "task": task, "count": 0, "latest_at": ""})
            entry["count"] += 1
            created_at = str(item.get("created_at") or "")
            latest = max(latest, created_at)
            if created_at >= entry["latest_at"]:
                entry["latest_at"] = created_at
                entry["task"] = task
        signature_payload = sorted(
            [{"channel": x["channel"], "reason": x["reason"], "count": x["count"], "latest_at": x["latest_at"], "latest_task": x.get("task") or ""} for x in groups.values()],
            key=lambda x: (x["channel"], x["reason"], x["latest_task"]),
        )
        signature = hashlib.sha256(json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        if self._notification_issue_seen(signature):
            return None
        summary = "；".join(f"{x['channel']} · {x['reason']}×{x['count']}" + (f"｜{x['task']}" if x.get("task") else "") for x in sorted(groups.values(), key=lambda v: v["count"], reverse=True)[:5])
        return {
            "id": "notification-" + signature,
            "phenomenon": f"通知通道失败 · 近 7 天 {len(failures)} 次",
            "root_cause": "通知通道或接收端不稳定，结果可能已生成但没有可靠送达。",
            "evidence": summary,
            "fix": "先把结果补发到 Web，再检查微信、企业微信、Webhook 配置；确认处理后同一批失败不再重复提醒。",
            "auto_fixable": True,
            "next_path": "通知兜底：Web 补发 + 通道健康检查 + 失败学习",
            "source_id": "notification:" + signature,
            "created_at": latest,
            "status": "pending",
        }

    @staticmethod
    def _within_days(value, days):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return (datetime.now().astimezone() - dt.astimezone()).total_seconds() <= days * 86400
        except Exception:
            return False

    def _notification_issue_seen(self, signature):
        try:
            from intelligence_center.inbox_service import InboxStore
            store = InboxStore(self.root)
            source_id = "notification-failure:" + str(signature or "")
            items = store.list(kind="system_notice", limit=100, offset=0).get("items") or []
            item = next((x for x in items if x.get("source_id") == source_id), None)
            return bool(item and not item.get("unread"))
        except Exception:
            return False

    @staticmethod
    def _diagnostic_signature(diagnostics):
        payload = []
        for item in diagnostics[:30]:
            payload.append({
                "id": str(item.get("source_id") or item.get("id") or ""),
                "phenomenon": str(item.get("phenomenon") or "")[:300],
                "evidence": str(item.get("evidence") or "")[:160],
                "status": str(item.get("status") or ""),
            })
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _seconds_since(value):
        try:
            return max(0, (datetime.now().astimezone() - datetime.fromisoformat(str(value).replace("Z", "+00:00"))).total_seconds())
        except (ValueError, TypeError, OSError):
            return 999999

    @staticmethod
    def _diagnostic_title(item):
        text = f"{item.get('phenomenon') or ''} {item.get('root_cause') or ''}".lower()
        if "agentllmmodel" in text or "remote end closed" in text or "connection aborted" in text:
            return "模型调用连接中断"
        if "wecom" in text or "企业微信" in text or "websocket" in text:
            return "企业微信长连接异常"
        if "weixin" in text or "微信" in text or "通知" in text or "发送失败" in text:
            return "微信/通知发送异常"
        if "socket" in text or "端口" in text or "只允许使用一次" in text:
            return "本地服务端口占用"
        if "ssl" in text or "certificate" in text or "证书" in text:
            return "网络证书异常"
        if "not found" in text or "未找到" in text:
            return "工具或文件未找到"
        return "待确认重复异常"

    def _prune_duplicate_harness_artifacts(self, keep_source_id=""):
        path = self.root / "artifacts.json"
        data = self._read_json(path, {})
        items = data.get("artifacts") if isinstance(data, dict) else []
        if not isinstance(items, list) or not items:
            return {"removed": 0}
        seen = set()
        seen_day = set()
        kept = []
        removed = 0
        def normalize(value):
            text = str(value or "")
            text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*", "<time>", text)
            text = re.sub(r"\b[0-9a-f]{10,16}\b", "<id>", text, flags=re.I)
            return text[:220]

        for item in items:
            is_harness = (
                item.get("source_type") == "harness_review"
                or item.get("source") == "automation"
                and str(item.get("title") or "").startswith("自我优化诊断报告")
                and "失败诊断" in (item.get("tags") or [])
            )
            if not is_harness:
                kept.append(item)
                continue
            key = "|".join([
                str(item.get("title") or ""),
                str(item.get("size") or ""),
                normalize(item.get("preview") or ""),
            ])
            day_key = str(item.get("title") or "")
            if item.get("source_id") == keep_source_id:
                seen.add(key)
                seen_day.add(day_key)
                kept.append(item)
            elif day_key in seen_day or key in seen:
                removed += 1
            else:
                seen.add(key)
                seen_day.add(day_key)
                kept.append(item)
        if removed:
            data["artifacts"] = kept
            self._write_json(path, data)
        return {"removed": removed}

    def optimization_suggestions(self, diagnostics=None, components=None):
        diagnostics = diagnostics if diagnostics is not None else self.diagnostics(20)
        components = components if components is not None else self.components()
        suggestions = []
        if any("空" in ((x.get("phenomenon") or "") + (x.get("root_cause") or "")) for x in diagnostics):
            suggestions.append(self._suggestion("search-recovery-policy", "搜索空结果兜底策略", "Prompt / 路由 Patch", "搜索、新闻、财税、网站监控遇到空结果时，自动放宽时间范围、切换来源、降低必含词。", "空结果诊断中出现高频样本。", "预计减少“暂无内容”交付，风险是偶尔引入低相关内容。"))
        if any("通知" in ((x.get("phenomenon") or "") + (x.get("root_cause") or "")) for x in diagnostics):
            suggestions.append(self._suggestion("notification-fallback", "通知失败自动补发 Web", "工具路由 Patch", "微信/Webhook/邮箱失败时，先把结果补发到 Web 并写入产物，再提示修复通道。", "通知链路失败会导致用户误以为任务没执行。", "预计提升可见性，风险较低。"))
        if any("浏览器" in ((x.get("phenomenon") or "") + (x.get("root_cause") or "")) for x in diagnostics):
            suggestions.append(self._suggestion("browser-static-fallback", "浏览器失败降级静态解析", "工具路由 Patch", "浏览器环境不可用时，先尝试静态网页/RSS/API 路径，不再反复要求用户排查。", "历史浏览器环境经常受权限或运行时影响。", "预计减少阻塞回复，风险是无法完成必须登录的任务。"))
        weak = [x for x in components if x.get("usage_count", 0) >= 3 and x.get("success_rate") is not None and x["success_rate"] < 40]
        for item in weak[:3]:
            suggestions.append(self._suggestion(f"component-watch-{item.get('id')}", f"降低不稳定组件优先级：{item.get('name')}", "组件策略 Patch", f"该组件近期成功率 {item.get('success_rate')}%，建议进入观察状态，路由时优先选择替代路径。", item.get("last_failure") or "成功率偏低。", "预计降低失败率，但可能减少该组件使用机会。"))
        if not suggestions:
            suggestions.append(self._suggestion("weekly-harness-review", "每周自动 Harness 体检", "维护任务", "每周汇总组件成功率、失败 Trace、Memory 新增和 Skill 变更，生成优化建议。", "当前暂无明显高频故障，但需要持续观测。", "帮助提前发现退化，风险低。"))
        return suggestions[:8]

    def create_snapshot(self, label="手动快照"):
        with self._lock:
            return self._create_snapshot(label)

    def _create_snapshot(self, label):
        state = self._state()
        snapshot_id = uuid.uuid4().hex[:12]
        folder = self.snapshot_dir / snapshot_id
        folder.mkdir(parents=True, exist_ok=True)
        copied = []
        for name in self.SNAPSHOT_FILES:
            src = self.root / name
            if name == "agent2_settings.json":
                settings = self._read_json(self.root / "agent2_state.json", {}).get("settings")
                if settings is not None:
                    self._write_json(folder / name, settings)
                    copied.append(name)
                continue
            if src.exists():
                dst = folder / name
                shutil.copy2(src, dst)
                copied.append(name)
        manifest = {
            "id": snapshot_id,
            "label": str(label or "手动快照")[:80],
            "created_at": self._now(),
            "files": copied,
            "components": [{"id": key} for key, filename in {
                "sub-agents": "agent_roster.json", "memory": "personal_agent_profile.json",
                "system-prompt": "agent2_settings.json", "mcp-cli": "connector_policies.json",
            }.items() if filename in copied],
            "hashes": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in copied},
        }
        self._write_json(folder / "manifest.json", manifest)
        state["snapshots"].insert(0, manifest)
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "snapshot.create", "summary": f"已创建 Harness 快照：{manifest['label']}", "snapshot_id": snapshot_id})
        self._save_state(state)
        return manifest

    def rollback_snapshot(self, snapshot_id):
        with self._lock:
            return self._rollback_snapshot(snapshot_id)

    def _rollback_snapshot(self, snapshot_id):
        snapshot_id = str(snapshot_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{12}", snapshot_id):
            raise ValueError("快照 ID 无效")
        folder = self.snapshot_dir / snapshot_id
        if folder.resolve().parent != self.snapshot_dir.resolve():
            raise ValueError("快照目录无效")
        manifest = self._read_json(folder / "manifest.json", {})
        files = manifest.get("files") or []
        if not folder.exists() or not files:
            raise ValueError("快照不存在或不可回滚")
        payloads = {}
        for name in files:
            if name not in self.SNAPSHOT_FILES or (self.root/name).is_symlink():
                raise ValueError("快照包含不允许回滚的配置")
            src = folder / name
            if src.resolve().parent != folder.resolve() or not src.is_file():
                raise ValueError("快照文件缺失或路径无效")
            raw = src.read_bytes()
            if hashlib.sha256(raw).hexdigest() != (manifest.get("hashes") or {}).get(name):
                raise ValueError("快照完整性校验失败，未修改当前配置")
            payloads[name] = json.loads(raw)
        backup = self.create_snapshot(f"回滚前自动备份 {snapshot_id}")
        restored = []
        originals = {}
        try:
            for name in files:
                target = self.root / ("agent2_state.json" if name=="agent2_settings.json" else name)
                originals[target] = target.read_bytes() if target.exists() else None
                if name == "agent2_settings.json":
                    current = self._read_json(self.root / "agent2_state.json", {})
                    current["settings"] = payloads[name]
                    self._write_json(self.root / "agent2_state.json", current)
                else:
                    self._write_json(self.root / name, payloads[name])
                restored.append(name)
        except Exception:
            for target, raw in originals.items():
                if raw is None:
                    target.unlink(missing_ok=True)
                else:
                    self._write_json(target, json.loads(raw))
            raise
        state = self._state()
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "snapshot.rollback", "summary": f"已回滚 Harness 快照：{manifest.get('label') or snapshot_id}", "snapshot_id": snapshot_id, "backup_id": backup.get("id"), "restored": restored})
        self._save_state(state)
        return {"snapshot": manifest, "backup": backup, "restored": restored}

    def checkpoint_before_change(self, name, before, after):
        if before != after:
            return self.create_snapshot("修改前自动版本："+name)
        return None

    def review(self, force=False):
        """Daily local review; creating advice never changes execution policy."""
        with self._lock:
            state = self._state()
            today = self._now()[:10]
            if not force and str(state.get("last_review_at", "")).startswith(today):
                return {"message": "今日已完成扫描", "review": state.get("last_review", {})}
            diagnostics = self.diagnostics(100)
            suggestions = self.optimization_suggestions(diagnostics)
            training_ingest = {}
            try:
                from intelligence_center.agent_training_lab_service import AgentTrainingLabService
                training_ingest = AgentTrainingLabService(self.root, self.workspace).auto_ingest(max_items=50)
            except Exception as training_error:
                training_ingest = {"error": str(training_error)}
            knowledge_archive = {}
            try:
                from intelligence_center.knowledge_archive_service import KnowledgeArchiveService
                knowledge_archive = KnowledgeArchiveService(self.root, self.workspace).run_auto("week")
            except Exception as knowledge_error:
                knowledge_archive = {"error": str(knowledge_error)}
            signature = self._diagnostic_signature(diagnostics)
            last_signature = state.get("last_review_signature") or ""
            if not last_signature and isinstance(state.get("last_review"), dict):
                last_signature = self._diagnostic_signature(state.get("last_review", {}).get("diagnostics") or [])
                if last_signature:
                    state["last_review_signature"] = last_signature
            if signature and signature == last_signature and self._seconds_since(state.get("last_review_at")) < 1800:
                cleanup = self._prune_duplicate_harness_artifacts(state.get("last_review", {}).get("id") or "")
                return {
                    "review": state.get("last_review", {}),
                    "deduplicated": True,
                    "message": "这批失败诊断和上次相同，已复用最近报告，未重复生成产物"
                               + (f"；已隐藏重复报告 {cleanup.get('removed')} 份" if cleanup.get("removed") else ""),
                }
            review = {"id": uuid.uuid4().hex[:12], "created_at": self._now(),
                      "diagnostics": diagnostics, "suggestions": suggestions,
                      "signature": signature, "training_ingest": training_ingest, "knowledge_archive": knowledge_archive}
            report = self.root / "harness_reports" / (review["id"] + ".md")
            report.parent.mkdir(parents=True, exist_ok=True)
            lines = ["# 自我优化诊断报告", "", "生成时间："+review["created_at"],
                     "", f"本次发现 {len(diagnostics)} 类候选问题。以下判断基于运行证据，尚未自动修改配置。",
                     "", f"训练场自动吸收：新增 {training_ingest.get('new_cases', 0)} 个用例、{training_ingest.get('failure_samples', 0)} 个失败样本。",
                     "", f"知识库自动归档：{knowledge_archive.get('message') or knowledge_archive.get('status') or '未执行'}",
                     "", "重点问题：" if diagnostics else "暂无达到阈值的问题。"]
            for item in diagnostics[:5]:
                lines.append(f"- {self._diagnostic_title(item)}：{item.get('evidence') or '暂无证据'}")
            lines.append("")
            for item in diagnostics:
                lines.extend(["## "+self._diagnostic_title(item), "", "原始现象："+item["phenomenon"],
                              "", "证据："+item["evidence"],
                              "", "可能原因："+item["root_cause"], "", "建议："+item["fix"], ""])
            report.write_text("\n".join(lines), encoding="utf-8")
            from intelligence_center.artifact_service import ArtifactService
            artifact = ArtifactService(str(self.root)).register(str(report), {
                "title": "自我优化诊断报告｜"+today, "source_type": "harness_review",
                "source_id": review["id"], "tags": ["失败诊断", "自我优化"]})
            review["artifact_id"] = artifact.get("id")
            state["last_review_at"] = review["created_at"]
            state["last_review"] = review
            state["last_review_signature"] = signature
            self._save_state(state)
            cleanup = self._prune_duplicate_harness_artifacts(review["id"])
            if cleanup.get("removed"):
                review["duplicate_artifacts_hidden"] = cleanup.get("removed")
            return {"review": review, "message": "扫描完成，诊断报告已保存到产物/交付物"}

    def propose(self, suggestion_id):
        suggestions = self.optimization_suggestions()
        suggestion = next((x for x in suggestions if x["id"] == suggestion_id), None)
        if not suggestion:
            raise ValueError("建议已变化，请刷新后重试")
        from intelligence_center.agent2_service import Agent2Service
        service = Agent2Service(self.root, self.workspace, {})
        data = service._read()
        if suggestion_id in self.POLICY_PATCHES:
            from plugins.intelligence_center.platform_features import ApprovalStore
            approvals = ApprovalStore(self.root)
            existing = next((x for x in approvals.list() if x.get("action")=="harness_policy_patch" and x.get("preview", {}).get("suggestion_id")==suggestion_id), None)
            if existing:
                return {"approval": existing, "message": "已存在相同审批，请到审批中心查看"}
            policy = data["settings"].get("recovery_policy", {})
            patch = self.POLICY_PATCHES[suggestion_id]
            if all(policy.get(k) == v for k, v in patch.items()):
                return {"message": "该恢复策略已启用，无需重复修改；请根据具体失败证据继续诊断"}
            preview = {"summary": suggestion["title"], "suggestion_id": suggestion_id,
                       "evidence": suggestion["failure_evidence"], "before": policy,
                       "after": {**policy, **patch}, "baseline": self._digest(policy),
                       "scope": "全局失败恢复策略；只更新展示的字段，不会立即重跑或发送任务"}
            approval = approvals.create("harness:"+suggestion_id, "harness_policy_patch", preview, 2)
            return {"approval": approval, "message": "已提交审批中心；批准后自动备份、应用并校验策略"}
        source_key = "harness:"+suggestion_id
        existing = next((x for x in data.get("proposals", []) if x.get("source_key")==source_key and x.get("status")=="pending"), None)
        if existing:
            return {"proposal": existing, "message": "已有待审草稿，请到 Agent 2.0 改进提案查看"}
        proposal = {"id": uuid.uuid4().hex[:14], "source_key": source_key,
                    "kind": "skill_patch", "title": suggestion["title"],
                    "reason": suggestion["failure_evidence"], "change": suggestion["targeted_fix"],
                    "status": "pending", "created_at": self._now()}
        data.setdefault("proposals", []).insert(0, proposal)
        service._event(data, "harness.proposal.created", proposal["title"], {"proposal_id": proposal["id"]}, risk=2, status="pending")
        service._write(data)
        return {"proposal": proposal, "message": "已生成待审改进草稿，尚未修改执行策略"}

    @staticmethod
    def _digest(payload):
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def apply_approved_patch(self, preview, approval):
        if not approval.get("approval_granted_at") or approval.get("action") != "harness_policy_patch":
            raise ValueError("改进尚未获得审批")
        patch = self.POLICY_PATCHES.get(preview.get("suggestion_id"))
        if patch is None:
            raise ValueError("此改进没有可执行的策略修改")
        with self._lock:
            state = self._state()
            applied = state.setdefault("applied_patches", {})
            if approval.get("id") in applied:
                return applied[approval["id"]]
            from intelligence_center.agent2_service import Agent2Service
            service = Agent2Service(self.root, self.workspace, {})
            policy = service._read()["settings"].get("recovery_policy", {})
            if self._digest(policy) != preview.get("baseline"):
                raise ValueError("策略在审批期间已变化，请刷新建议后重新审批")
            backup = self.create_snapshot("批准改进前备份")
            service.configure({"recovery_policy": patch})
            actual = service._read()["settings"]["recovery_policy"]
            if any(actual.get(k) != v for k, v in patch.items()):
                self.rollback_snapshot(backup["id"])
                raise ValueError("策略校验失败，已回滚")
            result = {"success": True, "verified": True, "backup_id": backup["id"],
                      "message": "策略已应用并通过配置校验，实际效果等待后续任务验证"}
            state = self._state()
            state.setdefault("applied_patches", {})[approval["id"]] = result
            state["events"].insert(0, {"action": "patch.applied", "created_at": self._now(),
                                      "summary": preview.get("summary"), "approval_id": approval["id"]})
            self._save_state(state)
            return result

    def _automation_runs(self):
        db = self.root / "cowagent_state.db"
        data = None
        if db.exists():
            with closing(sqlite3.connect(db.as_uri()+"?mode=ro", uri=True, timeout=5)) as connection:
                row = connection.execute("SELECT payload FROM state_documents WHERE document_key=?", ("automation_runs",)).fetchone()
                if row:
                    data = json.loads(row[0])
        if data is None:
            data = self._read_json(self.root / "automation_runs.json", {"runs": []})
        runs = data.get("runs") if isinstance(data, dict) and isinstance(data.get("runs"), list) else []
        return [item for item in runs if not self._is_harness_noise(item)]

    @staticmethod
    def _is_harness_noise(item):
        text = " ".join(str(item.get(k) or "") for k in ("id", "task_id", "task_name", "name", "kind", "kind_label", "source", "source_id", "result_summary", "trigger")).lower()
        title = str((item.get("artifact") or {}).get("title") if isinstance(item.get("artifact"), dict) else "")
        noise_words = ("测试", "模拟", "web-sim", "channel-test", "agent 训练场", "训练场评测", "自我优化诊断报告", "harness", "agent_training_lab")
        return any(word.lower() in text or word.lower() in title.lower() for word in noise_words)

    def _tool_events(self):
        path = self.workspace / "logs" / "tool_audit.jsonl"
        if not path.exists():
            return []
        from collections import deque
        events = []
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in deque(handle, maxlen=3000):
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        events.append(item)
                except ValueError:
                    continue
        return events

    def _fact_stats(self):
        try:
            from intelligence_center.fact_store import get_fact_store
            stats = get_fact_store(self.root).stats()
            return stats if isinstance(stats, dict) else {}
        except Exception:
            return {}

    def _run_successes(self, runs):
        return sum(1 for item in runs if item.get("status") == "success")

    def _last_failure(self, runs, keywords=None):
        keywords = tuple(str(x).lower() for x in (keywords or []))
        for item in sorted(runs, key=lambda x: str(x.get("finished_at") or x.get("started_at") or ""), reverse=True):
            text = json.dumps(item, ensure_ascii=False).lower()
            if keywords and not any(k in text for k in keywords):
                continue
            if item.get("status") in {"failed", "empty"} or item.get("error") or item.get("failure_reason"):
                return item.get("failure_reason") or item.get("empty_reason") or item.get("error") or item.get("result_summary") or "执行失败"
        return ""

    def _count_by_text(self, runs, keywords):
        keywords = tuple(str(x).lower() for x in keywords)
        return sum(1 for item in runs if any(k in json.dumps(item, ensure_ascii=False).lower() for k in keywords))

    def _skill_version_count(self, agent2):
        return len(agent2.get("skill_versions") or []) if isinstance(agent2, dict) else 0

    def _snapshot_count(self, component_id):
        if component_id.startswith("agent:"):
            component_id = "sub-agents"
        return sum(1 for item in (self._state().get("snapshots") or []) if any(str(c.get("id")) == component_id for c in item.get("components") or []))

    def _agent_hit(self, run, agent):
        text = json.dumps(run, ensure_ascii=False).lower()
        needles = [agent.get("id"), agent.get("name"), agent.get("entry")]
        return any(str(x or "").lower() in text for x in needles if x)

    def _extract_sources(self, item):
        snapshot = item.get("task_snapshot") if isinstance(item.get("task_snapshot"), dict) else {}
        values = []
        for key in ("sources", "source_urls", "urls", "repositories", "feeds"):
            value = snapshot.get(key) or item.get(key)
            if isinstance(value, list):
                values.extend(str(x) for x in value[:5])
            elif isinstance(value, str) and value.strip():
                values.extend([x.strip() for x in value.replace("\n", ",").split(",") if x.strip()][:5])
        return values[:8]

    def _diagnose_run(self, run, reason):
        task = run.get("task_name") or "自动化任务"
        status = run.get("status") or "failed"
        reason_text = self._redact(reason or run.get("result_summary") or "")
        if status == "empty" or "暂无" in reason_text or "无内容" in reason_text:
            root = "搜索/采集条件过窄、来源不可用或时间范围太短，导致候选为空。"
            fix = "自动放宽到 7 天、切换备用来源、降低必含词，并保留 Web 产物。"
            auto = True
            path = "搜索兜底：7天 + 备用来源 + Web 补发"
        elif "通知" in reason_text or "send" in reason_text.lower():
            root = "通知通道失败或接收端不可用，结果可能已生成但没有送达。"
            fix = "先补发到 Web，再检查微信/Webhook/邮箱配置。"
            auto = True
            path = "通知兜底：Web 补发"
        elif "browser" in reason_text.lower() or "浏览器" in reason_text:
            root = "浏览器运行环境不可用或页面需要人工登录/验证。"
            fix = "先降级到静态解析、RSS 或普通搜索；必须登录时进入审批/接管。"
            auto = True
            path = "浏览器降级：静态解析/搜索"
        else:
            root = "任务执行失败，需要结合执行 Trace 和日志进一步确认。"
            fix = "查看执行 Trace，确认工具、来源、权限和模型配置。"
            auto = False
            path = "人工复核后沉淀 Memory 或 Skill Patch"
        return {
            "id": run.get("id") or uuid.uuid4().hex[:12],
            "phenomenon": f"{task} · {status}",
            "root_cause": root,
            "evidence": reason_text or run.get("result_summary") or "运行记录显示失败/空结果。",
            "fix": fix,
            "auto_fixable": auto,
            "next_path": path,
            "source_id": run.get("id") or "",
            "created_at": run.get("finished_at") or run.get("started_at") or "",
            "status": status,
        }

    @staticmethod
    def _redact(value):
        text = str(value or "")
        text = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [已隐藏]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[已隐藏]", text)
        return re.sub(r"(?i)((?:token|api_key|password|secret)=)[^&\s]+", r"\1[已隐藏]", text)

    @staticmethod
    def _suggestion(sid, title, kind, fix, evidence, impact):
        return {
            "id": sid,
            "title": title,
            "kind": kind,
            "failure_evidence": evidence,
            "root_cause": evidence,
            "targeted_fix": fix,
            "predicted_impact": impact,
            "risk": "low" if "通知" in title or "每周" in title else "medium",
        }
