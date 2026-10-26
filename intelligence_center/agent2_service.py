"""Unified, auditable control plane for CowAgent 2.0 adaptive capabilities.

This module does not train model weights. It coordinates the existing
self-evolution, memory, skills, scheduler, sub-agent and approval subsystems,
and stores only configuration, observations and audit events.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path


CAPABILITIES = [
    ("self_review", "Self-improvement Review", "P0", "复盘会话并只提出可审计、可回滚的改进"),
    ("user_profile", "自动 User Profile", "P0", "从明确且重复的偏好中更新用户模型"),
    ("memory_extract", "自动 Memory 提取", "P0", "提取长期偏好、事实和承诺，避免重复写入"),
    ("memory_tree", "Memory Tree / 个人认知库", "P0", "把画像、偏好、失败经验和来源沉淀为可编辑、可回滚的记忆树"),
    ("tool_result_compact", "TokenJuice 工具结果压缩", "P0", "工具原始结果先清洗压缩，再交给模型或发给用户，减少废话和 token"),
    ("split_brain_router", "Split Brain 快速/深度路由", "P0", "简单问题走快速工具，复杂任务再进入强模型和深度编排"),
    ("skill_generate", "自动 Skill 生成", "P1", "把稳定、可重复的工作流转成候选 Skill"),
    ("skill_evolution", "Skill Patch / Evolution", "P1", "小步修补技能并保留版本、差异和回滚"),
    ("error_learning", "Error Learning", "P0", "聚合重复失败，区分环境故障和可学习问题"),
    ("auto_fetch_context", "Auto-fetch 自动上下文同步", "P1", "定时同步邮箱、微信、RSS、文件和网站，避免每次从零查询"),
    ("workflow_canvas", "可视化工作流画布", "P1", "把触发器、数据源、处理规则、审批和通知展示成可编辑流程"),
    ("checkpoint_graph", "Checkpoint 任务恢复", "P1", "任务失败后从失败节点继续，避免整条链路重复执行"),
    ("cron_agent", "Cron 定时 Agent", "P0", "复用现有 Cron、间隔和单次调度能力"),
    ("heartbeat", "Heartbeat 主动 Agent", "P1", "主动检查审批、异常、承诺和待办，而非无目的唤醒"),
    ("planner_subagent", "Planner + Sub-Agent", "P1", "复杂任务拆解、并行执行和汇总验收"),
    ("multi_channel_router", "多通道统一入口", "P1", "Web、微信、企业微信、Webhook、邮箱和远程 Bridge 走同一套路由"),
    ("privacy_mode", "隐私模式 / 凭据隔离", "P0", "邮件、文件、Cookie、Token 和外部发送默认受权限与脱敏保护"),
    ("approval_audit", "Human Approval + Audit", "P0", "高风险动作人工确认并记录完整审计链"),
]

_heartbeat_started = False


def start_agent2_heartbeat(root, workspace, config, log_path):
    """Start the conservative local heartbeat loop once per process."""
    global _heartbeat_started
    if _heartbeat_started: return
    _heartbeat_started = True
    def loop():
        last_run = 0.0
        while True:
            time.sleep(60)
            try:
                service = Agent2Service(root, workspace, config)
                settings = service._read()["settings"]
                if not settings.get("heartbeat_enabled") or not settings.get("capabilities",{}).get("heartbeat",{}).get("enabled",True): continue
                interval = max(5, int(settings.get("heartbeat_minutes", 30))) * 60
                if time.time() - last_run < interval: continue
                service.scan_errors(log_path)
                service.heartbeat()
                from intelligence_center.harness_evolution_service import HarnessEvolutionService
                HarnessEvolutionService(root, workspace).review()
                last_run = time.time()
            except Exception:
                continue
    threading.Thread(target=loop, daemon=True, name="agent2-heartbeat").start()


class Agent2Service:
    def __init__(self, root, workspace, config):
        self.root = Path(root)
        self.workspace = Path(workspace)
        self.config = config
        self.path = self.root / "agent2_state.json"

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _default(self):
        approvals={"user_profile":"auto_low_risk","memory_extract":"auto_low_risk","error_learning":"auto_low_risk","heartbeat":"auto_low_risk","skill_generate":"always","skill_evolution":"always","approval_audit":"always"}
        capability_settings = {key: {"enabled": True, "approval": approvals.get(key,"risk_based")} for key,_,_,_ in CAPABILITIES}
        return {
            "version": 1,
            "settings": {
                "review_mode": "idle",
                "review_min_turns": int(self.config.get("self_evolution_min_turns", 6)),
                "review_idle_minutes": int(self.config.get("self_evolution_idle_minutes", 10)),
                "heartbeat_enabled": True,
                "heartbeat_minutes": 30,
                "memory_approval": "auto_low_risk",
                "skill_approval": "always",
                "error_threshold": 3,
                "capabilities": capability_settings,
                "approval_notifications": [],
                "recovery_policy": {
                    "enabled": True,
                    "allow_expand_time": True,
                    "allow_switch_sources": True,
                    "allow_web_fallback": True,
                    "max_auto_retries": 1,
                },
                "self_training": {
                    "enabled": True,
                    "auto_review": True,
                    "learn_success_paths": True,
                    "learn_failure_lessons": True,
                    "suggest_skill_candidates": True,
                    "export_training_data": False,
                    "min_success_count": 2,
                    "min_failure_count": 2,
                    "require_approval_for_skill": True,
                },
                "split_brain": {
                    "enabled": True,
                    "fast_first_channels": ["web", "weixin", "wechatcom_app", "wecom_bot"],
                    "fast_timeout_seconds": 5,
                    "deep_task_keywords": ["研究", "分析", "报告", "代码", "浏览器代办", "自动化", "方案"],
                    "simple_task_keywords": ["天气", "时间", "外汇", "汇率", "邮件", "文件", "你好"],
                },
                "output_compaction": {
                    "enabled": True,
                    "mode": "brief",
                    "max_items": 10,
                    "summary_min_chars": 30,
                    "summary_max_chars": 80,
                    "hide_raw_urls": True,
                    "strip_process_logs": True,
                    "preserve_sources": True,
                },
                "privacy_mode": {
                    "enabled": True,
                    "local_first": True,
                    "redact_credentials": True,
                    "approval_for_external_send": True,
                },
                "policy_version": 2,
            },
            "events": [], "error_patterns": [], "recovery_patterns": [],
            "mission_command_patterns": [], "weixin_failure_patterns": [],
            "weixin_success_patterns": [], "self_training_reviews": [],
            "training_exports": [], "skill_candidates": [], "skill_drafts": [], "skill_versions": [], "proposals": [],
        }

    def _read(self):
        if not self.path.exists():
            return self._default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            default = self._default()
            saved_settings=data.get("settings") or {}
            for key,value in data.items():
                if key!="settings":default[key]=value
            default["settings"].update({key:value for key,value in saved_settings.items() if key!="capabilities"})
            saved_caps=(data.get("settings") or {}).get("capabilities") or {}
            for key,value in saved_caps.items():
                if key in default["settings"]["capabilities"] and isinstance(value,dict):
                    default["settings"]["capabilities"][key].update(value)
            return default
        except (OSError, ValueError, TypeError):
            return self._default()

    def _write(self, data):
        from intelligence_center.harness_evolution_service import HarnessEvolutionService
        old = self._read()
        if self.path.exists() and old.get("settings") != data.get("settings"):
            HarnessEvolutionService(self.root, self.workspace).checkpoint_before_change(
                "Agent 2.0 策略", old.get("settings"), data.get("settings"))
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def _event(self, data, action, summary, details=None, risk=0, status="completed"):
        item = {"id": uuid.uuid4().hex[:14], "action": action, "summary": summary,
                "details": details or {}, "risk": risk, "status": status, "created_at": self._now()}
        data["events"].insert(0, item)
        data["events"] = data["events"][:1000]
        return item

    def _remember_route_fact(self, fact_type, title, content, tags=None, metadata=None, confidence=0.78, status="pending"):
        """Persist route learning into the shared fact store.

        The fact store is used as read-only routing evidence later. Sensitive
        values such as tokens, cookies and passwords are deliberately skipped.
        """
        text = f"{title or ''} {content or ''}"
        if not str(content or "").strip():
            return
        if re.search(r"(密码|验证码|token|secret|api[_-]?key|sk-[A-Za-z0-9]|cookie|authorization|bearer\s+[A-Za-z0-9._-]+)", text, re.I):
            return
        try:
            from intelligence_center.fact_store import get_fact_store
            store = get_fact_store(self.root)
            meta = dict(metadata or {})
            source_type = str(meta.get("source_type") or meta.get("source") or "").strip()
            if not source_type:
                source_type = "agent2_learning"
            meta.setdefault("source_type", source_type)
            meta.setdefault(
                "source_id",
                meta.get("command_id") or meta.get("run_id") or meta.get("task_id") or meta.get("session_id") or meta.get("version_id") or meta.get("source") or source_type,
            )
            meta.setdefault("artifact_id", "")
            meta.setdefault("run_id", meta.get("latest_run_id") or "")
            meta.setdefault("session_id", meta.get("conversation_id") or "")
            meta.setdefault("source_label", "Agent 2.0")
            store.upsert(
                content=str(content or "").strip()[:1000],
                fact_type=str(fact_type or "reuse_pattern"),
                title=str(title or "Agent 2.0 路由经验").strip()[:120],
                source="Agent 2.0",
                tags=list(tags or []),
                scope="local-routing",
                confidence=float(confidence or 0.78),
                status=str(status or "pending"),
                metadata=meta,
            )
        except Exception:
            return

    def _memory_learning_summary(self):
        """Compact review status for Memory Wiki / fact_store."""
        try:
            from intelligence_center.fact_store import get_fact_store
            store = get_fact_store(self.root)
            stats = store.stats()
            items = store.search("", "", 50, include_disabled=True)
            pending = [item for item in items if str(item.get("status") or "active") == "pending"]
            disabled = [item for item in items if str(item.get("status") or "") == "disabled"]
            return {
                "active": int(stats.get("total") or 0),
                "pending": int(stats.get("pending") or len(pending)),
                "disabled": int(stats.get("disabled") or len(disabled)),
                "items": pending[:5],
            }
        except Exception as error:
            return {"active": 0, "pending": 0, "disabled": 0, "items": [], "error": str(error)}

    def dashboard(self):
        data = self._read()
        profile_path = self.root / "personal_agent_profile.json"
        memory_dir = self.workspace / "memory"
        skills_dir = self.workspace / "skills"
        evolution_dir = memory_dir / "evolution"
        approvals_path = self.root / "approvals.json"
        approvals = []
        try:
            approvals = json.loads(approvals_path.read_text(encoding="utf-8"))
            if isinstance(approvals, dict): approvals = approvals.get("items", [])
        except (OSError, ValueError):
            pass
        evidence = {
            "self_review": bool(self.config.get("self_evolution_enabled", True)),
            "user_profile": profile_path.exists(), "memory_extract": memory_dir.exists(),
            "memory_tree": profile_path.exists() or bool(data.get("error_patterns")) or bool(data.get("weixin_success_patterns")),
            "tool_result_compact": bool(data["settings"].get("output_compaction", {}).get("enabled", True)),
            "split_brain_router": bool(data["settings"].get("split_brain", {}).get("enabled", True)),
            "skill_generate": skills_dir.exists(), "skill_evolution": evolution_dir.exists(),
            "error_learning": bool(data["error_patterns"]), "cron_agent": True,
            "auto_fetch_context": bool(self.config.get("conversation_persistence", True)),
            "workflow_canvas": True,
            "checkpoint_graph": bool(data["settings"].get("recovery_policy", {}).get("enabled", True)),
            "heartbeat": bool(data["settings"].get("heartbeat_enabled")),
            "planner_subagent": bool((self.config.get("subagent") or {}).get("enabled", True)),
            "multi_channel_router": True,
            "privacy_mode": bool(data["settings"].get("privacy_mode", {}).get("enabled", True)),
            "approval_audit": True,
        }
        evidence_reason = {
            "self_review": "自进化复盘开关已开启" if evidence["self_review"] else "请先开启自进化复盘",
            "user_profile": "本地用户画像已建立" if evidence["user_profile"] else "等待首次保存用户画像",
            "memory_extract": "Memory 目录可用" if evidence["memory_extract"] else "等待首次提取长期记忆",
            "memory_tree": "已接入 Memory Wiki / 画像 / 失败经验" if evidence["memory_tree"] else "等待画像、偏好或失败经验进入记忆树",
            "tool_result_compact": "工具结果压缩策略已启用" if evidence["tool_result_compact"] else "工具结果压缩已关闭",
            "split_brain_router": "快速/深度双脑路由已启用" if evidence["split_brain_router"] else "快速/深度双脑路由已关闭",
            "skill_generate": "Skill 工作区可用" if evidence["skill_generate"] else "等待初始化 Skill 工作区",
            "skill_evolution": "技能进化记录目录可用" if evidence["skill_evolution"] else "等待首次批准 Skill Patch",
            "error_learning": "已形成失败模式记录" if evidence["error_learning"] else "等待重复错误达到阈值",
            "auto_fetch_context": "会话/来源同步底座可用，后续逐步接邮箱、微信、RSS、文件" if evidence["auto_fetch_context"] else "请先开启会话与来源持久化",
            "workflow_canvas": "自动化任务已具备触发、来源、处理、通知等结构，可继续升级为画布",
            "checkpoint_graph": "失败恢复策略已启用，可继续升级为节点级恢复" if evidence["checkpoint_graph"] else "失败恢复策略未开启",
            "cron_agent": "统一调度服务已接入",
            "heartbeat": "Heartbeat 定时检查已开启" if evidence["heartbeat"] else "Heartbeat 尚未开启",
            "planner_subagent": "Sub-Agent 拆解能力已开启" if evidence["planner_subagent"] else "请先开启 Sub-Agent",
            "multi_channel_router": "Web、微信、企业微信、Webhook 与远程 Bridge 已纳入统一入口规划",
            "privacy_mode": "隐私模式已开启：优先本地、凭据脱敏、高风险审批" if evidence["privacy_mode"] else "隐私模式已关闭",
            "approval_audit": "审批存储与审计链可用",
        }
        action_capability={"error.scan":"error_learning","heartbeat.check":"heartbeat","proposal.created":"approval_audit","proposal.resolved":"approval_audit","agent2.config.updated":"approval_audit","agent2.policy.recommended":"approval_audit","agent2.health.checked":"approval_audit"}
        last_activity={}
        for event in data["events"]:
            capability=action_capability.get(event.get("action"))
            if capability and capability not in last_activity:last_activity[capability]=event.get("created_at","")
        cards=[]
        for key,name,priority,description in CAPABILITIES:
            policy=data["settings"]["capabilities"].get(key,{})
            cards.append({"key":key,"name":name,"priority":priority,"description":description,
                          "enabled":bool(policy.get("enabled",True)),"available":evidence[key],"evidence":evidence_reason[key],"last_activity":last_activity.get(key,""),"approval":policy.get("approval","risk_based"),
                          "state":"active" if evidence[key] and policy.get("enabled",True) else "disabled" if not policy.get("enabled",True) else "ready"})
        reliability = {}
        try:
            from agent.tools.reliability import ToolReliability
            reliability = ToolReliability().dashboard()
        except Exception:
            reliability = {"tools": {}, "recovery_paths": [], "summary": {}}
        memory_learning = self._memory_learning_summary()
        return {"capabilities":cards,"settings":data["settings"],"events":data["events"][:50],
                "error_patterns":data["error_patterns"][:30],
                "recovery_patterns":data.get("recovery_patterns", [])[:30],
                "mission_command_patterns":data.get("mission_command_patterns", [])[:30],
                "weixin_failure_patterns":data.get("weixin_failure_patterns", [])[:30],
                "weixin_success_patterns":data.get("weixin_success_patterns", [])[:30],
                "self_training": self.self_training_status(),
                "proposals":data["proposals"][:30],
                "tool_reliability": reliability,
                "memory_learning": memory_learning,
                "summary":{"active":sum(1 for x in cards if x["state"]=="active"),"enabled":sum(1 for x in cards if x["enabled"]),"total":len(cards),
                           "pending_approvals":sum(1 for x in approvals if x.get("status")=="pending"),
                           "learned_errors":sum(1 for x in data["error_patterns"] if x.get("status")!="ignored"),
                           "pending_memories":int(memory_learning.get("pending") or 0),
                           "recovery_paths":len(data.get("recovery_patterns", [])),
                           "mission_commands":len(data.get("mission_command_patterns", [])),
                           "weixin_failures":len(data.get("weixin_failure_patterns", [])),
                           "weixin_successes":len(data.get("weixin_success_patterns", [])),
                           "self_training_reviews":len(data.get("self_training_reviews", [])),
                           "skill_candidates":len(data.get("skill_candidates", [])),
                           "skill_drafts":len(data.get("skill_drafts", []))}}

    def health_check(self):
        data=self._read();dashboard=self.dashboard();cards=dashboard["capabilities"]
        details={"active":sum(1 for x in cards if x["state"]=="active"),"ready":sum(1 for x in cards if x["state"]=="ready"),"disabled":sum(1 for x in cards if x["state"]=="disabled")}
        self._event(data,"agent2.health.checked",f"Agent 2.0 能力验证完成：{details['active']} 项已生效，{details['ready']} 项待触发",details)
        self._write(data);return {"summary":details,"capabilities":self.dashboard()["capabilities"]}

    def configure(self, patch):
        data=self._read(); allowed={"review_mode","review_min_turns","review_idle_minutes","heartbeat_enabled","heartbeat_minutes","memory_approval","skill_approval","error_threshold","capabilities","approval_notifications","recovery_policy","self_training","split_brain","output_compaction","privacy_mode","policy_version"}
        for key,value in (patch or {}).items():
            if key=="capabilities" and isinstance(value,dict):
                for cap_key,policy in value.items():
                    if cap_key in data["settings"]["capabilities"] and isinstance(policy,dict):
                        if "enabled" in policy:data["settings"]["capabilities"][cap_key]["enabled"]=bool(policy["enabled"])
                        if policy.get("approval") in {"always","risk_based","auto_low_risk"}:data["settings"]["capabilities"][cap_key]["approval"]=policy["approval"]
            elif key=="recovery_policy" and isinstance(value,dict):
                current=dict(data["settings"].get("recovery_policy") or {})
                for policy_key in ("enabled","allow_expand_time","allow_switch_sources","allow_web_fallback"):
                    if policy_key in value: current[policy_key]=bool(value[policy_key])
                if "max_auto_retries" in value:
                    current["max_auto_retries"]=max(0,min(int(value.get("max_auto_retries") or 0),3))
                data["settings"]["recovery_policy"]=current
            elif key=="self_training" and isinstance(value,dict):
                current=dict(data["settings"].get("self_training") or self._default()["settings"]["self_training"])
                for policy_key in ("enabled","auto_review","learn_success_paths","learn_failure_lessons","suggest_skill_candidates","export_training_data","require_approval_for_skill"):
                    if policy_key in value: current[policy_key]=bool(value[policy_key])
                for count_key in ("min_success_count","min_failure_count"):
                    if count_key in value: current[count_key]=max(1,min(int(value.get(count_key) or 1),20))
                data["settings"]["self_training"]=current
            elif key=="split_brain" and isinstance(value,dict):
                current=dict(data["settings"].get("split_brain") or self._default()["settings"]["split_brain"])
                if "enabled" in value: current["enabled"]=bool(value["enabled"])
                if "fast_timeout_seconds" in value: current["fast_timeout_seconds"]=max(1,min(int(value.get("fast_timeout_seconds") or 5),30))
                for list_key in ("fast_first_channels","deep_task_keywords","simple_task_keywords"):
                    if list_key in value and isinstance(value[list_key],list):
                        current[list_key]=[str(x).strip() for x in value[list_key] if str(x).strip()][:50]
                data["settings"]["split_brain"]=current
            elif key=="output_compaction" and isinstance(value,dict):
                current=dict(data["settings"].get("output_compaction") or self._default()["settings"]["output_compaction"])
                for policy_key in ("enabled","hide_raw_urls","strip_process_logs","preserve_sources"):
                    if policy_key in value: current[policy_key]=bool(value[policy_key])
                if value.get("mode") in {"brief","standard","detailed"}: current["mode"]=value["mode"]
                for count_key,minimum,maximum in (("max_items",1,30),("summary_min_chars",10,120),("summary_max_chars",20,300)):
                    if count_key in value: current[count_key]=max(minimum,min(int(value.get(count_key) or current.get(count_key) or minimum),maximum))
                if current.get("summary_max_chars",80) < current.get("summary_min_chars",30):
                    current["summary_max_chars"]=current["summary_min_chars"]
                data["settings"]["output_compaction"]=current
            elif key=="privacy_mode" and isinstance(value,dict):
                current=dict(data["settings"].get("privacy_mode") or self._default()["settings"]["privacy_mode"])
                for policy_key in ("enabled","local_first","redact_credentials","approval_for_external_send"):
                    if policy_key in value: current[policy_key]=bool(value[policy_key])
                data["settings"]["privacy_mode"]=current
            elif key in allowed: data["settings"][key]=value
        self._event(data,"agent2.config.updated","Agent 2.0 策略已更新",{"keys":sorted(set(patch or {}) & allowed)})
        self._write(data); return data["settings"]

    def apply_recommended_policy(self):
        data=self._read();recommended=self._default()["settings"]
        data["settings"]["capabilities"]=recommended["capabilities"]
        data["settings"]["memory_approval"]="auto_low_risk";data["settings"]["skill_approval"]="always";data["settings"]["heartbeat_enabled"]=True
        data["settings"]["recovery_policy"]=dict(recommended.get("recovery_policy") or {})
        data["settings"]["self_training"]=dict(recommended.get("self_training") or {})
        data["settings"]["split_brain"]=dict(recommended.get("split_brain") or {})
        data["settings"]["output_compaction"]=dict(recommended.get("output_compaction") or {})
        data["settings"]["privacy_mode"]=dict(recommended.get("privacy_mode") or {})
        data["settings"]["policy_version"]=2
        self._event(data,"agent2.policy.recommended","已应用“低风险自动、高风险审批”推荐模式")
        self._write(data);return data["settings"]

    @staticmethod
    def _normalize_error(line):
        text=re.sub(r"\b[0-9a-f]{8,}\b","<id>",line,flags=re.I)
        text=re.sub(r"\d{4}-\d{2}-\d{2}[^\]]*","<time>",text)
        text=re.sub(r"\b\d+\b","<n>",text)
        return text[-280:].strip()

    @staticmethod
    def _normalize_automation_failure(kind, reason, message):
        text = f"[Automation:{kind}] {reason or ''} {message or ''}".strip()
        text = re.sub(r"https?://[^\s)）]+", "<url>", text)
        text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text, flags=re.I)
        text = re.sub(r"\d{4}-\d{2}-\d{2}[^\s]*", "<time>", text)
        text = re.sub(r"\b\d+\b", "<n>", text)
        return text[-280:].strip()

    @staticmethod
    def _classify_error(fingerprint):
        lower=fingerprint.lower()
        if "automation:" in lower:
            if any(token in lower for token in ("模型或接口认证失败","authentication","unauthorized","auth header","api key")):
                return "observed", "模型认证失败：下次执行前应优先检查模型配置、API Key、Base URL 和 Bearer 格式"
            if any(token in lower for token in ("筛选条件过严","没有匹配到","暂无合格","无内容")):
                return "observed", "空结果经验：同类搜索任务应优先放宽时间范围、减少必须包含词，并启用全网或更多来源兜底"
            if any(token in lower for token in ("通知通道发送失败","通道","发送失败","webhook","邮箱","微信")):
                return "observed", "通知失败经验：下次执行前应优先检查接收通道健康状态，失败时保底发送到 Web 并写入交付物"
            if any(token in lower for token in ("网络","连接失败","ssl","urlopen","timeout","超时")):
                return "observed", "来源访问经验：同类任务应减少单次来源数量，优先用已接入来源/RSS，失败后再降级搜索"
            if any(token in lower for token in ("浏览器执行环境不可用","browser","chrome","cdp","playwright")):
                return "observed", "浏览器环境经验：浏览器代办前应先检查运行环境；不可用时不要反复重试，应直接降级或提示修复"
        if any(token in lower for token in ("permission denied","access is denied","拒绝访问","winerror <n>")):
            return "ignored", "本地文件权限问题：不写入 Memory；应修正存储路径或目录权限"
        if "not found, but found in plugins.json" in lower:
            return "ignored", "插件登记与实际启用状态不一致：属于配置清理问题，不写入 Memory 或生成 Skill Patch"
        if "no module named" in lower:
            return "ignored", "运行环境缺少依赖或使用了错误的 Python 环境：属于部署问题，不作为 Agent 长期经验"
        if any(token in lower for token in ("timeout","timed out","connection refused","temporarily unavailable","network is unreachable")):
            return "ignored", "临时网络或服务可用性问题：仅保留日志，不作为长期经验"
        return "observed", "达到重复阈值，等待确认根因后再决定是否形成 Memory 或 Skill Patch"

    def scan_errors(self, log_path):
        data=self._read(); path=Path(log_path); lines=[]
        if path.exists(): lines=path.read_text(encoding="utf-8",errors="replace").splitlines()[-3000:]
        counter=Counter(self._normalize_error(x) for x in lines if "[ERROR]" in x or "[CRITICAL]" in x)
        known={x["fingerprint"]:x for x in data["error_patterns"]}
        threshold=max(1,int(data["settings"].get("error_threshold",3)))
        for item in data["error_patterns"]:
            item["status"],item["lesson"]=self._classify_error(item.get("fingerprint", ""))
        for fingerprint,count in counter.most_common(50):
            item=known.get(fingerprint)
            if item: item.update({"count":count,"last_seen":self._now()})
            elif count>=threshold:
                status,lesson=self._classify_error(fingerprint)
                item={"id":uuid.uuid4().hex[:12],"fingerprint":fingerprint,"count":count,"status":status,"first_seen":self._now(),"last_seen":self._now(),"lesson":lesson}
                data["error_patterns"].insert(0,item)
        event=self._event(data,"error.scan",f"错误学习扫描完成，识别 {len(counter)} 类错误",{"qualified":sum(1 for n in counter.values() if n>=threshold)})
        self._write(data); return {"event":event,"patterns":data["error_patterns"][:30]}

    def observe_automation_failure(self, payload):
        """Learn from automation empty/failed runs without changing user config."""
        data = self._read()
        settings = data.get("settings") or {}
        if not settings.get("capabilities", {}).get("error_learning", {}).get("enabled", True):
            return {"learned": False, "reason": "error_learning disabled"}
        kind = str(payload.get("kind") or "automation")
        task_id = str(payload.get("task_id") or "")
        task_name = str(payload.get("task_name") or "自动任务")
        reason = str(payload.get("reason") or "")
        message = str(payload.get("message") or "")
        suggestion = str(payload.get("suggestion") or "")
        fingerprint = self._normalize_automation_failure(kind, reason, message)
        known = {item.get("fingerprint"): item for item in data["error_patterns"]}
        item = known.get(fingerprint)
        now = self._now()
        threshold = max(1, int(settings.get("error_threshold", 3)))
        status, lesson = self._classify_error(fingerprint)
        if status == "observed" and suggestion:
            lesson = suggestion
        if item:
            item["count"] = int(item.get("count") or 0) + 1
            item["last_seen"] = now
            item["status"] = status
            item["lesson"] = lesson
        else:
            item = {"id": uuid.uuid4().hex[:12], "fingerprint": fingerprint, "count": 1,
                    "status": status, "first_seen": now, "last_seen": now, "lesson": lesson,
                    "source": "automation"}
            data["error_patterns"].insert(0, item)
        examples = item.setdefault("examples", [])
        examples.insert(0, {"task_id": task_id, "task_name": task_name, "kind": kind,
                            "reason": reason, "message": message[:300], "suggestion": suggestion,
                            "at": now})
        item["examples"] = examples[:10]
        affected = item.setdefault("affected_tasks", [])
        if task_id and task_id not in affected:
            affected.insert(0, task_id)
        item["affected_tasks"] = affected[:20]
        if item["count"] >= threshold and item.get("status") != "ignored":
            item["ready_for_reuse"] = True
            item["lesson"] = lesson or "同类任务再次执行前，应优先检查数据来源、网络、权限和通知通道"
        self._event(data, "error.automation.observed",
                    f"自动化失败经验已记录：{task_name}",
                    {"task_id": task_id, "kind": kind, "fingerprint": fingerprint, "count": item["count"]},
                    risk=0)
        self._write(data)
        return {"learned": True, "pattern": item}

    def automation_lessons(self, kind="", task_id="", limit=3):
        data = self._read()
        lessons = []
        for item in data.get("error_patterns", []):
            if item.get("status") == "ignored":
                continue
            examples = item.get("examples") or []
            if kind and not any(str(x.get("kind") or "") == str(kind) for x in examples):
                continue
            if task_id and task_id not in (item.get("affected_tasks") or []) and not any(str(x.get("task_id") or "") == str(task_id) for x in examples):
                continue
            lessons.append({"fingerprint": item.get("fingerprint", ""), "count": item.get("count", 0),
                            "lesson": item.get("lesson", ""), "last_seen": item.get("last_seen", "")})
            if len(lessons) >= int(limit or 3):
                break
        return lessons

    @staticmethod
    def _recovery_category(reason):
        text = str(reason or "").lower()
        if any(x in text for x in ("通知", "发送", "webhook", "邮箱", "微信")):
            return "notification"
        if any(x in text for x in ("暂无", "无合格", "没有匹配", "筛选", "来源", "空")):
            return "empty_search"
        if any(x in text for x in ("browser", "chrome", "cdp", "playwright", "浏览器")):
            return "browser"
        if any(x in text for x in ("网络", "连接", "ssl", "timeout", "超时")):
            return "network"
        return "general"

    @staticmethod
    def _normalize_recovery_action(action):
        text = re.sub(r"\s+", " ", str(action or "")).strip()
        text = re.sub(r"\d{4}-\d{2}-\d{2}[^\s]*", "<time>", text)
        text = re.sub(r"https?://[^\s)）]+", "<url>", text)
        return text[:160]

    def observe_recovery_attempt(self, payload):
        """Record whether a recovery action actually helped a retry."""
        data = self._read()
        data.setdefault("recovery_patterns", [])
        kind = str(payload.get("kind") or "automation")
        task_id = str(payload.get("task_id") or "")
        task_name = str(payload.get("task_name") or "自动任务")
        reason = str(payload.get("reason") or "")
        category = self._recovery_category(reason)
        outcome = str(payload.get("outcome") or "unknown")
        success = outcome == "success"
        actions = [self._normalize_recovery_action(x) for x in (payload.get("actions") or []) if str(x).strip()]
        now = self._now()
        index = {(x.get("kind"), x.get("category"), x.get("action")): x for x in data["recovery_patterns"]}
        updated = []
        for action in actions:
            key = (kind, category, action)
            item = index.get(key)
            if not item:
                item = {"id": uuid.uuid4().hex[:12], "kind": kind, "category": category,
                        "action": action, "attempts": 0, "successes": 0, "empty": 0,
                        "failures": 0, "success_rate": 0.0, "first_seen": now,
                        "last_seen": now, "examples": []}
                data["recovery_patterns"].insert(0, item)
                index[key] = item
            item["attempts"] = int(item.get("attempts") or 0) + 1
            if success:
                item["successes"] = int(item.get("successes") or 0) + 1
            elif outcome == "empty":
                item["empty"] = int(item.get("empty") or 0) + 1
            else:
                item["failures"] = int(item.get("failures") or 0) + 1
            item["success_rate"] = round(item["successes"] / max(1, item["attempts"]), 4)
            item["last_seen"] = now
            examples = item.setdefault("examples", [])
            examples.insert(0, {"task_id": task_id, "task_name": task_name, "reason": reason[:200],
                                "outcome": outcome, "run_id": payload.get("run_id", ""), "at": now})
            item["examples"] = examples[:8]
            item["recommended"] = item["attempts"] >= 2 and item["success_rate"] >= 0.5
            updated.append(item)
        data["recovery_patterns"] = sorted(
            data["recovery_patterns"][:200],
            key=lambda x: (float(x.get("success_rate") or 0), int(x.get("successes") or 0), str(x.get("last_seen") or "")),
            reverse=True,
        )
        self._event(data, "recovery.attempt.observed",
                    f"恢复动作已记录：{task_name} · {outcome}",
                    {"task_id": task_id, "kind": kind, "category": category,
                     "actions": actions, "outcome": outcome},
                    risk=0)
        self._write(data)
        return {"recorded": bool(updated), "patterns": updated}

    def recommended_recovery_actions(self, kind="", reason="", limit=5):
        data = self._read()
        category = self._recovery_category(reason)
        items = []
        for item in data.get("recovery_patterns", []):
            if kind and item.get("kind") != str(kind):
                continue
            if category != "general" and item.get("category") != category:
                continue
            if int(item.get("successes") or 0) <= 0:
                continue
            items.append(item)
        items.sort(key=lambda x: (float(x.get("success_rate") or 0), int(x.get("successes") or 0), str(x.get("last_seen") or "")), reverse=True)
        return [{"action": x.get("action", ""), "success_rate": x.get("success_rate", 0),
                 "successes": x.get("successes", 0), "attempts": x.get("attempts", 0),
                 "category": x.get("category", "")} for x in items[:int(limit or 5)]]

    @staticmethod
    def _normalize_mission_command(command):
        text = re.sub(r"\s+", " ", str(command or "")).strip()
        text = re.sub(r"https?://[^\s)）]+", "<url>", text)
        text = re.sub(r"\d{4}-\d{2}-\d{2}[^\s]*", "<time>", text)
        text = re.sub(r"\b\d+\b", "<n>", text)
        return text[:160] or "未命名指令"

    def observe_mission_command(self, payload):
        """Learn how the user uses Mission Control command box.

        This is preference evidence only. It must not grant permissions or skip
        approvals; it only improves ranking, shortcut suggestions and routing.
        """
        data = self._read()
        data.setdefault("mission_command_patterns", [])
        command = str(payload.get("command") or "")
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        outcome = str(payload.get("outcome") or "planned")
        intent = str(payload.get("intent") or decision.get("intent") or "unknown")
        target = str(payload.get("target") or decision.get("target") or "")
        source = str(payload.get("source") or decision.get("source") or "")
        channel = str(payload.get("channel") or decision.get("source_channel") or source or "")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        key = (intent, target, self._normalize_mission_command(command))
        now = self._now()
        index = {(item.get("intent"), item.get("target"), item.get("normalized_command")): item for item in data["mission_command_patterns"]}
        item = index.get(key)
        if not item:
            item = {
                "id": uuid.uuid4().hex[:12],
                "intent": intent,
                "target": target,
                "normalized_command": key[2],
                "display_command": command[:180],
                "count": 0,
                "successes": 0,
                "failures": 0,
                "planned": 0,
                "last_outcome": "",
                "source_stats": {},
                "channel_stats": {},
                "first_seen": now,
                "last_seen": now,
                "examples": [],
            }
            data["mission_command_patterns"].insert(0, item)
        item["count"] = int(item.get("count") or 0) + 1
        if outcome in {"success", "completed", "ok"}:
            item["successes"] = int(item.get("successes") or 0) + 1
        elif outcome in {"failed", "error"}:
            item["failures"] = int(item.get("failures") or 0) + 1
        else:
            item["planned"] = int(item.get("planned") or 0) + 1
        item["last_outcome"] = outcome
        item["last_seen"] = now
        item["display_command"] = command[:180] or item.get("display_command") or ""
        source_stats = item.setdefault("source_stats", {})
        if source:
            source_stats[source] = int(source_stats.get(source) or 0) + 1
        channel_stats = item.setdefault("channel_stats", {})
        if channel:
            channel_stats[channel] = int(channel_stats.get(channel) or 0) + 1
        examples = item.setdefault("examples", [])
        examples.insert(0, {
            "command": command[:220],
            "intent": intent,
            "target": target,
            "outcome": outcome,
            "message": str(payload.get("message") or decision.get("message") or "")[:220],
            "source": source,
            "channel": channel,
            "metadata": metadata,
            "at": now,
        })
        item["examples"] = examples[:10]
        total = max(1, int(item.get("successes") or 0) + int(item.get("failures") or 0))
        item["success_rate"] = round(int(item.get("successes") or 0) / total, 4)
        item["recommended"] = int(item.get("count") or 0) >= 2 and int(item.get("failures") or 0) == 0
        data["mission_command_patterns"] = sorted(
            data["mission_command_patterns"][:200],
            key=lambda x: (int(x.get("successes") or 0) * 5 + int(x.get("count") or 0) - int(x.get("failures") or 0) * 4, str(x.get("last_seen") or "")),
            reverse=True,
        )
        self._event(data, "mission.command.observed",
                    f"任务指挥中心学习：{intent} · {outcome}",
                    {"intent": intent, "target": target, "command": command[:160], "outcome": outcome},
                    risk=0)
        if outcome in {"success", "completed", "ok"}:
            self._remember_route_fact(
                "success_path",
                f"任务指挥中心成功路径：{intent}",
                f"用户常用指令“{command[:120] or key[2]}”在 {source or channel or '总控台'} 入口执行成功；同类任务可优先复用该路由。",
                tags=["总控台", "成功路径", intent, target],
                metadata={
                    "source_type": "mission_control",
                    "source_id": key[2],
                    "artifact_id": "",
                    "run_id": metadata.get("run_id", "") if isinstance(metadata, dict) else "",
                    "session_id": metadata.get("session_id", "") if isinstance(metadata, dict) else "",
                    "intent": intent,
                    "target": target,
                    "source": source,
                    "channel": channel,
                    "outcome": outcome,
                },
            )
        elif outcome in {"failed", "error"}:
            self._remember_route_fact(
                "failure_lesson",
                f"任务指挥中心失败经验：{intent}",
                f"用户指令“{command[:120] or key[2]}”执行失败；下次同类任务应先检查可用工具、权限和通知通道，再按推荐恢复路径重试。",
                tags=["总控台", "失败经验", intent, target],
                metadata={
                    "source_type": "mission_control",
                    "source_id": key[2],
                    "artifact_id": "",
                    "run_id": metadata.get("run_id", "") if isinstance(metadata, dict) else "",
                    "session_id": metadata.get("session_id", "") if isinstance(metadata, dict) else "",
                    "intent": intent,
                    "target": target,
                    "source": source,
                    "channel": channel,
                    "outcome": outcome,
                },
                confidence=0.72,
            )
        self._write(data)
        return {"recorded": True, "pattern": item}

    def mission_command_learning(self, limit=6):
        data = self._read()
        rows = []
        for item in data.get("mission_command_patterns", []):
            command = item.get("display_command") or item.get("normalized_command") or ""
            if not command:
                continue
            rows.append({
                "command": command,
                "intent": item.get("intent") or "",
                "target": item.get("target") or "",
                "count": int(item.get("count") or 0),
                "successes": int(item.get("successes") or 0),
                "failures": int(item.get("failures") or 0),
                "success_rate": item.get("success_rate") or 0,
                "last_outcome": item.get("last_outcome") or "",
                "last_seen": item.get("last_seen") or "",
                "source_stats": item.get("source_stats") or {},
                "channel_stats": item.get("channel_stats") or {},
                "top_source": self._top_counter_label(item.get("source_stats") or {}),
                "top_channel": self._top_counter_label(item.get("channel_stats") or {}),
                "recommended": bool(item.get("recommended")),
            })
        rows.sort(key=lambda x: (x["recommended"], x["successes"], x["count"], x["last_seen"]), reverse=True)
        return rows[:int(limit or 6)]

    @staticmethod
    def _top_counter_label(counter):
        if not isinstance(counter, dict) or not counter:
            return ""
        key, _value = sorted(counter.items(), key=lambda x: int(x[1] or 0), reverse=True)[0]
        return str(key or "")

    @staticmethod
    def _normalize_weixin_failure(command, category, message):
        text = f"[Weixin:{category}] {command or ''} {message or ''}".strip()
        text = re.sub(r"https?://[^\s)）]+", "<url>", text)
        text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text, flags=re.I)
        text = re.sub(r"\d{4}-\d{2}-\d{2}[^\s]*", "<time>", text)
        text = re.sub(r"\b\d+\b", "<n>", text)
        return text[-240:].strip()

    @staticmethod
    def _weixin_failure_category(message, category=""):
        text = f"{category} {message}".lower()
        if any(x in text for x in ("401", "authentication", "unauthorized", "auth header", "api key", "bearer")):
            return "model_auth", "模型认证失败：微信任务应优先检查模型配置，失败时不要反复调用模型"
        if any(x in text for x in ("邮箱", "imap", "smtp", "授权码", "mail")):
            return "email_config", "邮箱配置缺失：微信邮件请求应先检查邮箱配置，缺失时给出最短配置入口"
        if any(x in text for x in ("暂无", "无合格", "没有匹配", "无内容", "empty")):
            return "empty_result", "空结果：下次同类微信搜索应先放宽时间范围、减少必含词或切换已接入来源"
        if any(x in text for x in ("not found", "找不到", "没有在已授权目录", "文件")):
            return "file_not_found", "文件未找到：下次应先刷新索引、扩大授权目录内搜索，再提示用户补充文件名"
        if any(x in text for x in ("permission", "拒绝访问", "无权限", "winerror 5")):
            return "permission", "权限不足：下次先检查本地授权目录/通道授权，涉及写入仍需审批"
        if any(x in text for x in ("timeout", "超时", "connection", "ssl", "urlopen", "10054", "10061")):
            return "network", "网络失败：同类微信查询应减少来源、优先本地候选或已接入 RSS"
        if any(x in text for x in ("browser", "chrome", "cdp", "playwright", "agent-browser")):
            return "browser_env", "浏览器环境不可用：微信网页代办前应先检查浏览器运行环境，不可用则降级"
        if any(x in text for x in ("空回复", "没有生成可发送", "empty_reply")):
            return "empty_reply", "空回复：下次同类请求应优先走确定性工具或给出更短的兜底结果"
        if any(x in text for x in ("send_failure", "发送失败", "文本消息发送失败", "降级文本消息发送失败")):
            return "send_failure", "微信发送失败：先检查微信通道在线、发送令牌和接收会话；失败时自动补发到 Web 并保留产物"
        if category:
            return str(category), "微信任务失败经验：下次先尝试确定性工具，失败后再说明原因"
        return "general", "微信任务失败经验：下次先尝试可用路径，失败后再说明原因"

    def observe_weixin_failure(self, payload):
        """Record WeChat task failures for future route preference.

        This is learning evidence only. It must not grant permissions, bypass
        approval, or store credentials.
        """
        data = self._read()
        data.setdefault("weixin_failure_patterns", [])
        command = str(payload.get("command") or "").strip()
        raw_category = str(payload.get("category") or "")
        message = str(payload.get("message") or payload.get("reason") or "").strip()
        route = str(payload.get("route") or "").strip()
        category, lesson = self._weixin_failure_category(message, raw_category)
        fingerprint = self._normalize_weixin_failure(command, category, message)
        now = self._now()
        index = {item.get("fingerprint"): item for item in data["weixin_failure_patterns"]}
        item = index.get(fingerprint)
        if not item:
            item = {
                "id": uuid.uuid4().hex[:12],
                "fingerprint": fingerprint,
                "category": category,
                "count": 0,
                "first_seen": now,
                "last_seen": now,
                "lesson": lesson,
                "routes": {},
                "examples": [],
            }
            data["weixin_failure_patterns"].insert(0, item)
        item["count"] = int(item.get("count") or 0) + 1
        item["last_seen"] = now
        item["lesson"] = lesson
        item["category"] = category
        routes = item.setdefault("routes", {})
        if route:
            routes[route] = int(routes.get(route) or 0) + 1
        examples = item.setdefault("examples", [])
        examples.insert(0, {
            "command": command[:180],
            "message": message[:240],
            "route": route,
            "at": now,
        })
        item["examples"] = examples[:10]
        item["recommended_action"] = self._weixin_recommended_action(category)
        data["weixin_failure_patterns"] = sorted(
            data["weixin_failure_patterns"][:200],
            key=lambda x: (int(x.get("count") or 0), str(x.get("last_seen") or "")),
            reverse=True,
        )
        self._event(data, "weixin.failure.observed",
                    f"微信失败经验已记录：{category}",
                    {"category": category, "route": route, "count": item["count"]},
                    risk=0)
        self._remember_route_fact(
            "failure_lesson",
            f"微信失败经验：{category}",
            f"{lesson}；推荐动作：{item.get('recommended_action') or self._weixin_recommended_action(category)}",
            tags=["微信", "失败经验", category, route],
            metadata={
                "source_type": "weixin_failure",
                "source_id": f"{category}:{route}",
                "artifact_id": "",
                "run_id": "",
                "session_id": "",
                "channel": "weixin",
                "category": category,
                "route": route,
                "count": item["count"],
            },
            confidence=0.76,
        )
        self._write(data)
        return {"recorded": True, "pattern": item}

    @staticmethod
    def _weixin_recommended_action(category):
        return {
            "model_auth": "先检查模型配置；认证失败时停止模型重试，改用已配置本地工具或提示修复入口",
            "email_config": "先尝试邮箱读取；缺配置时只提示设置 → 邮箱配置",
            "empty_result": "自动放宽到 7 天、减少必含词、切换已接入来源后重试一次",
            "file_not_found": "刷新本地文件索引，优先桌面/下载/文档授权目录内搜索",
            "permission": "提示授权目录或请求审批，不执行写入/删除",
            "network": "减少来源数量，优先 RSS/本地候选，失败后再全网搜索",
            "browser_env": "浏览器任务先做环境检查，不可用时降级静态解析或普通搜索",
            "empty_reply": "同类请求优先走确定性快通道，模型空回复时给出明确兜底",
            "send_failure": "先确认微信在线和发送令牌；发送失败时自动补发到 Web，并把结果写入产物/交付物",
        }.get(category, "先尝试确定性工具；失败后给出最短原因和可重试方式")

    def weixin_failure_learning(self, limit=8):
        data = self._read()
        rows = []
        for item in data.get("weixin_failure_patterns", []):
            examples = item.get("examples") or []
            latest = examples[0] if examples else {}
            category = item.get("category") or ""
            recommended_action = item.get("recommended_action") or self._weixin_recommended_action(category)
            if category == "send_failure" and recommended_action == "先尝试确定性工具；失败后给出最短原因和可重试方式":
                recommended_action = self._weixin_recommended_action(category)
            lesson = item.get("lesson") or ""
            if category == "send_failure" and "先尝试确定性工具" in lesson:
                lesson = "微信发送失败：先检查微信通道在线、发送令牌和接收会话；失败时自动补发到 Web 并保留产物"
            rows.append({
                "id": item.get("id") or "",
                "category": category,
                "count": int(item.get("count") or 0),
                "lesson": lesson,
                "recommended_action": recommended_action,
                "last_seen": item.get("last_seen") or "",
                "routes": item.get("routes") or {},
                "latest_command": latest.get("command") or "",
                "latest_message": latest.get("message") or "",
                "latest_route": latest.get("route") or "",
                "examples": examples[:5],
            })
        rows.sort(key=lambda x: (x["count"], x["last_seen"]), reverse=True)
        return rows[:int(limit or 8)]

    @staticmethod
    def _normalize_weixin_success(command, task_type, action):
        text = f"[WeixinSuccess:{task_type}:{action}] {command or ''}".strip()
        text = re.sub(r"https?://[^\s)）]+", "<url>", text)
        text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text, flags=re.I)
        text = re.sub(r"\d{4}-\d{2}-\d{2}[^\s]*", "<time>", text)
        text = re.sub(r"\b\d+\b", "<n>", text)
        return text[-240:].strip()

    @staticmethod
    def _weixin_success_label(task_type, action):
        labels = {
            "email:configured_task": "已复用已配置邮箱任务成功读取邮件",
            "email:one_off_read": "无固定任务时直接读取邮箱成功",
            "news:configured_task": "已复用已配置资讯/财税任务成功生成结果",
            "news:direct_preview": "已通过已接入来源快速查询成功",
            "news:expanded_7d": "24小时无结果后，放宽到7天成功拿到内容",
            "file:index_search": "已通过本地文件索引快速找到文件",
            "file:refresh_then_search": "首次未命中后刷新索引并成功找到文件",
            "send:weixin_text": "微信文本消息发送成功",
        }
        return labels.get(f"{task_type}:{action}", "微信任务成功路径已记录，可供下次优先复用")

    def observe_weixin_success(self, payload):
        """Record successful WeChat execution paths for future route preference."""
        data = self._read()
        data.setdefault("weixin_success_patterns", [])
        command = str(payload.get("command") or "").strip()
        task_type = str(payload.get("task_type") or payload.get("category") or "general").strip() or "general"
        action = str(payload.get("action") or payload.get("route") or "success").strip() or "success"
        route = str(payload.get("route") or "").strip()
        evidence = str(payload.get("evidence") or "").strip()
        fingerprint = self._normalize_weixin_success(command, task_type, action)
        now = self._now()
        index = {item.get("fingerprint"): item for item in data["weixin_success_patterns"]}
        item = index.get(fingerprint)
        if not item:
            item = {
                "id": uuid.uuid4().hex[:12],
                "fingerprint": fingerprint,
                "task_type": task_type,
                "action": action,
                "count": 0,
                "first_seen": now,
                "last_seen": now,
                "routes": {},
                "examples": [],
            }
            data["weixin_success_patterns"].insert(0, item)
        item["count"] = int(item.get("count") or 0) + 1
        item["last_seen"] = now
        item["label"] = self._weixin_success_label(task_type, action)
        routes = item.setdefault("routes", {})
        if route:
            routes[route] = int(routes.get(route) or 0) + 1
        examples = item.setdefault("examples", [])
        examples.insert(0, {
            "command": command[:180],
            "evidence": evidence[:240],
            "route": route,
            "at": now,
        })
        item["examples"] = examples[:10]
        data["weixin_success_patterns"] = sorted(
            data["weixin_success_patterns"][:200],
            key=lambda x: (int(x.get("count") or 0), str(x.get("last_seen") or "")),
            reverse=True,
        )
        self._event(data, "weixin.success.observed",
                    f"微信成功路径已记录：{task_type}/{action}",
                    {"task_type": task_type, "action": action, "route": route, "count": item["count"]},
                    risk=0)
        self._remember_route_fact(
            "success_path",
            f"微信成功路径：{task_type}/{action}",
            f"{item.get('label') or self._weixin_success_label(task_type, action)}；同类微信请求可优先复用该执行路径。",
            tags=["微信", "成功路径", task_type, action, route],
            metadata={
                "source_type": "weixin_success",
                "source_id": f"{task_type}:{action}:{route}",
                "artifact_id": "",
                "run_id": "",
                "session_id": "",
                "channel": "weixin",
                "task_type": task_type,
                "action": action,
                "route": route,
                "count": item["count"],
            },
            confidence=0.84,
        )
        self._write(data)
        return {"recorded": True, "pattern": item}

    def weixin_success_learning(self, limit=8):
        data = self._read()
        rows = []
        for item in data.get("weixin_success_patterns", []):
            rows.append({
                "task_type": item.get("task_type") or "",
                "action": item.get("action") or "",
                "count": int(item.get("count") or 0),
                "label": item.get("label") or "",
                "last_seen": item.get("last_seen") or "",
                "routes": item.get("routes") or {},
            })
        rows.sort(key=lambda x: (x["count"], x["last_seen"]), reverse=True)
        return rows[:int(limit or 8)]

    def self_training_status(self):
        data = self._read()
        settings = data.get("settings", {}).get("self_training") or self._default()["settings"]["self_training"]
        reviews = data.get("self_training_reviews", [])
        exports = data.get("training_exports", [])
        candidates = data.get("skill_candidates", [])
        drafts = data.get("skill_drafts", [])
        rollback_summary = self.skill_rollback_summary(data)
        reusable_success = [x for x in data.get("weixin_success_patterns", []) if int(x.get("count") or 0) >= int(settings.get("min_success_count") or 2)]
        reusable_failures = [x for x in data.get("weixin_failure_patterns", []) if int(x.get("count") or 0) >= int(settings.get("min_failure_count") or 2)]
        reusable_recovery = [x for x in data.get("recovery_patterns", []) if int(x.get("successes") or 0) > 0]
        skill_usage = []
        try:
            from agent.tools.reliability import ToolReliability
            for name, item in ToolReliability().scores().items():
                if str(name).startswith("skill:"):
                    skill_usage.append({
                        "name": str(name).replace("skill:", "", 1),
                        "success_rate": item.get("success_rate", 0),
                        "score": item.get("score", 0),
                        "successes": item.get("successes", 0),
                        "total": item.get("total", 0),
                        "recent_failures": item.get("recent_failures", 0),
                    })
            skill_usage.sort(key=lambda x: (float(x.get("score") or 0), int(x.get("total") or 0)), reverse=True)
        except Exception:
            skill_usage = []
        skill_patch_candidates = [
            x for x in data.get("proposals", [])
            if x.get("kind") == "skill_patch" and x.get("status") in {"pending", "approved"}
        ]
        return {
            "settings": settings,
            "summary": {
                "enabled": bool(settings.get("enabled", True)),
                "reviews": len(reviews),
                "skill_candidates": len([x for x in candidates if x.get("status") != "deleted"]),
                "skill_drafts": len([x for x in drafts if x.get("status") != "deleted"]),
                "training_exports": len(exports),
                "success_samples": len(reusable_success) + len(reusable_recovery),
                "failure_samples": len(reusable_failures),
                "skill_rollbacks": sum(int(x.get("rollback_count") or 0) for x in rollback_summary),
            },
            "latest_review": reviews[0] if reviews else None,
            "reviews": reviews[:10],
            "skill_candidates": [x for x in candidates if x.get("status") != "deleted"][:10],
            "skill_drafts": [x for x in drafts if x.get("status") != "deleted"][:10],
            "skill_usage": skill_usage[:10],
            "skill_patch_candidates": skill_patch_candidates[:10],
            "skill_versions": self.skill_version_history()[:10],
            "skill_rollback_summary": rollback_summary[:10],
            "training_exports": exports[:10],
        }

    def run_self_training_review(self):
        """Convert local execution traces into auditable learning candidates.

        This is not weight training. It learns routing/memory/skill candidates
        from verified local outcomes and stores only non-secret summaries.
        """
        data = self._read()
        settings = data.get("settings", {}).get("self_training") or self._default()["settings"]["self_training"]
        data.setdefault("self_training_reviews", [])
        data.setdefault("skill_candidates", [])
        if not settings.get("enabled", True):
            raise ValueError("自我训练当前未启用")
        min_success = int(settings.get("min_success_count") or 2)
        min_failure = int(settings.get("min_failure_count") or 2)
        learned = []
        candidates = []
        for item in data.get("weixin_success_patterns", []):
            if int(item.get("count") or 0) < min_success:
                continue
            title = f"微信成功路径：{item.get('task_type') or 'general'}/{item.get('action') or 'success'}"
            content = f"{item.get('label') or '微信任务成功路径'}；同类请求可优先复用，仍需遵守权限与审批。"
            learned.append({"kind": "success_path", "title": title, "content": content})
            self._remember_route_fact("success_path", title, content, tags=["自我训练", "微信", "成功路径", str(item.get("task_type") or "")], metadata={"source": "self_training", "source_type": "self_training", "source_id": str(item.get("task_type") or "weixin_success"), "channel": "weixin"})
            if settings.get("suggest_skill_candidates", True):
                candidates.append(self._skill_candidate_from_pattern("weixin", item, title, content))
        for item in data.get("recovery_patterns", []):
            if int(item.get("successes") or 0) <= 0:
                continue
            title = f"失败恢复成功路径：{item.get('category') or 'general'}"
            content = f"恢复动作“{item.get('action') or '按推荐参数重试'}”历史成功 {item.get('successes',0)}/{item.get('attempts',0)} 次；同类失败可优先尝试。"
            learned.append({"kind": "recovery_path", "title": title, "content": content})
            self._remember_route_fact("success_path", title, content, tags=["自我训练", "失败恢复", str(item.get("category") or "")], metadata={"source": "self_training", "source_type": "self_training", "source_id": str(item.get("category") or "recovery_path")})
        for item in data.get("weixin_failure_patterns", []):
            if int(item.get("count") or 0) < min_failure:
                continue
            title = f"微信失败经验：{item.get('category') or 'general'}"
            content = f"{item.get('lesson') or '微信任务失败经验'}；推荐动作：{item.get('recommended_action') or '先尝试确定性工具，失败后再说明原因'}。"
            learned.append({"kind": "failure_lesson", "title": title, "content": content})
            self._remember_route_fact("failure_lesson", title, content, tags=["自我训练", "微信", "失败经验", str(item.get("category") or "")], metadata={"source": "self_training", "source_type": "self_training", "source_id": str(item.get("category") or "weixin_failure"), "channel": "weixin"}, confidence=0.74)
        if settings.get("suggest_skill_candidates", True):
            self._merge_skill_candidates(data, candidates)
            patch_candidates = self._generate_skill_patch_proposals(data)
            for item in patch_candidates:
                learned.append({"kind": "skill_patch", "title": item.get("title", ""), "content": item.get("reason", "")})
        review = {
            "id": uuid.uuid4().hex[:12],
            "created_at": self._now(),
            "summary": f"自我训练复盘完成：沉淀 {len(learned)} 条经验，生成 {len(candidates)} 个 Skill 候选",
            "learned": learned[:30],
            "candidate_count": len(candidates),
            "status": "completed",
        }
        data["self_training_reviews"].insert(0, review)
        data["self_training_reviews"] = data["self_training_reviews"][:100]
        self._event(data, "self_training.review", review["summary"], {"review_id": review["id"], "learned": len(learned), "skill_candidates": len(candidates)})
        self._write(data)
        return {"review": review, "self_training": self.self_training_status()}

    def _generate_skill_patch_proposals(self, data):
        """Create pending patch proposals for repeatedly failing self-training Skills."""
        try:
            from agent.tools.reliability import ToolReliability
            scores = ToolReliability().scores()
        except Exception:
            return []
        existing_keys = {
            str(item.get("source_key") or "")
            for item in data.get("proposals", [])
            if item.get("kind") == "skill_patch" and item.get("status") in {"pending", "approved"}
        }
        rollback_stats = {x.get("skill_name"): x for x in self.skill_rollback_summary(data)}
        created = []
        for tool_name, score in scores.items():
            if not str(tool_name).startswith("skill:"):
                continue
            name = str(tool_name).replace("skill:", "", 1)
            total = int(score.get("total") or 0)
            recent_failures = int(score.get("recent_failures") or 0)
            success_rate = float(score.get("success_rate") or 0)
            if total < 2 or (recent_failures < 2 and success_rate >= 0.45):
                continue
            rollback_info = rollback_stats.get(name) or {}
            if int(rollback_info.get("rollback_count") or 0) > 0 and recent_failures < 3:
                self._event(
                    data,
                    "self_training.skill_patch.deferred",
                    f"暂缓生成 Skill 修补提案：{name}",
                    {"skill": name, "reason": "该 Skill 最近有回滚记录，等待更多失败证据后再建议修补", "recent_failures": recent_failures},
                    risk=1,
                    status="completed",
                )
                continue
            source_key = f"skill_patch:{name}"
            if source_key in existing_keys:
                continue
            skill_path = self.workspace / "skills" / name / "SKILL.md"
            current_summary = ""
            try:
                if skill_path.exists():
                    current_summary = skill_path.read_text(encoding="utf-8", errors="replace")[:1200]
            except OSError:
                current_summary = ""
            proposal = {
                "id": uuid.uuid4().hex[:14],
                "kind": "skill_patch",
                "title": f"修补 Skill：{name}",
                "reason": (
                    f"该 Skill 最近使用效果偏低：总调用 {total} 次，"
                    f"成功率 {round(success_rate * 100)}%，最近连续失败 {recent_failures} 次。"
                ),
                "change": "\n".join([
                    f"目标 Skill：{name}",
                    f"当前路径：{skill_path}",
                    "建议修补：",
                    "1. 明确触发边界，避免不匹配任务误用。",
                    "2. 增加失败兜底：缺配置、空结果、权限不足时先换可用路径，再给最短原因。",
                    "3. 精简输出，优先给用户结果，不输出冗长排查过程。",
                    "4. 对涉及登录、发送、写入、删除的动作保留审批点。",
                    "",
                    "当前内容片段：",
                    current_summary or "未读取到当前 SKILL.md 内容。",
                ]),
                "status": "pending",
                "source_key": source_key,
                "skill_name": name,
                "created_at": self._now(),
            }
            data.setdefault("proposals", []).insert(0, proposal)
            self._event(
                data,
                "self_training.skill_patch.proposed",
                f"已根据失败率生成 Skill 修补提案：{name}",
                {"proposal_id": proposal["id"], "skill": name, "total": total, "success_rate": success_rate, "recent_failures": recent_failures},
                risk=2,
                status="pending",
            )
            existing_keys.add(source_key)
            created.append(proposal)
        return created

    @staticmethod
    def _skill_candidate_from_pattern(source, item, title, content):
        task_type = str(item.get("task_type") or item.get("category") or "general")
        action = str(item.get("action") or item.get("recommended_action") or "execute")
        return {
            "id": f"{source}-{task_type}-{action}".lower().replace(" ", "-")[:80],
            "source": source,
            "title": title.replace("：", " - ")[:80],
            "description": content[:180],
            "task_type": task_type,
            "action": action,
            "count": int(item.get("count") or item.get("successes") or 0),
            "status": "candidate",
            "created_at": Agent2Service._now(),
        }

    @staticmethod
    def _merge_skill_candidates(data, candidates):
        existing = {x.get("id"): x for x in data.setdefault("skill_candidates", [])}
        for item in candidates:
            current = existing.get(item.get("id"))
            if current:
                current.update({k: v for k, v in item.items() if k not in ("created_at",)})
                current["updated_at"] = Agent2Service._now()
            else:
                data["skill_candidates"].insert(0, item)
        data["skill_candidates"] = data["skill_candidates"][:100]

    def resolve_skill_candidate(self, candidate_id, decision):
        """Mark a self-training Skill candidate as adopted or ignored.

        Adopting a candidate deliberately creates an approval proposal instead
        of installing or patching a Skill directly. This keeps self-evolution
        auditable and reversible while still moving useful repeated patterns
        into the normal Agent 2.0 approval flow.
        """
        data = self._read()
        data.setdefault("skill_candidates", [])
        candidate = next((x for x in data["skill_candidates"] if x.get("id") == candidate_id), None)
        if not candidate:
            raise ValueError("Skill 候选不存在")
        normalized = "approved" if decision == "approved" else "ignored"
        now = self._now()
        candidate["status"] = normalized
        candidate["resolved_at"] = now
        result = {"candidate": candidate}
        if normalized == "approved":
            proposal = {
                "id": uuid.uuid4().hex[:14],
                "kind": "skill_create",
                "title": f"创建 Skill：{candidate.get('title') or '自我训练候选'}",
                "reason": candidate.get("description") or "来自自我训练复盘的高频成功/失败模式。",
                "change": "\n".join([
                    f"来源：{candidate.get('source') or 'self_training'}",
                    f"任务类型：{candidate.get('task_type') or 'general'}",
                    f"建议动作：{candidate.get('action') or 'execute'}",
                    "建议把该模式整理成可复用 Skill；正式创建前需人工审批。",
                ]),
                "status": "pending",
                "created_at": now,
            }
            data.setdefault("proposals", []).insert(0, proposal)
            candidate["proposal_id"] = proposal["id"]
            result["proposal"] = proposal
            self._event(data, "self_training.skill_candidate.approved", "Skill 候选已采纳，已生成待审批提案", {"candidate_id": candidate_id, "proposal_id": proposal["id"]}, risk=2, status="pending")
        else:
            self._event(data, "self_training.skill_candidate.ignored", "Skill 候选已忽略", {"candidate_id": candidate_id})
        self._write(data)
        return result

    @staticmethod
    def _slugify_skill_name(value):
        text = str(value or "self-training-skill").strip().lower()
        text = re.sub(r"创建\s*skill[:：]?", "", text, flags=re.I)
        text = re.sub(r"修补\s*skill[:：]?", "", text, flags=re.I)
        ascii_part = "-".join(re.findall(r"[a-z0-9]+", text))
        if ascii_part:
            return ascii_part[:60]
        digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:8]
        return f"self-training-{digest}"

    @staticmethod
    def _skill_draft_markdown(proposal, source_candidate=None):
        title = str(proposal.get("title") or "自我训练 Skill 草稿").replace("创建 Skill：", "").strip()
        reason = str(proposal.get("reason") or "").strip()
        change = str(proposal.get("change") or "").strip()
        candidate = source_candidate or {}
        task_type = candidate.get("task_type") or "general"
        action = candidate.get("action") or "execute"
        source = candidate.get("source") or "Agent 2.0 自我训练"
        return "\n".join([
            "---",
            f"name: {Agent2Service._slugify_skill_name(title)}",
            f"description: {title}。来自本机自我训练沉淀的可复用执行模式。",
            "origin: self_training",
            "default_enabled: true",
            "---",
            "",
            f"# {title}",
            "",
            "## 使用场景",
            "",
            f"- 任务类型：{task_type}",
            f"- 推荐动作：{action}",
            f"- 来源：{source}",
            "",
            "## 触发方式",
            "",
            "- 当用户提出相似需求时，先判断是否已有成功路径或失败恢复经验。",
            "- 低风险读取、整理、摘要类任务可优先复用；写入、发送、登录、删除等动作仍需按权限策略审批。",
            "",
            "## 执行准则",
            "",
            "1. 先直接尝试可用路径，失败后再说明阻塞原因和修复建议。",
            "2. 不记录密码、Cookie、Token、验证码、API Key 等敏感信息。",
            "3. 输出给用户时只保留结果、关键证据和下一步可执行动作，避免冗长排查过程。",
            "4. 同类任务执行成功后写入成功路径；失败时写入可归类失败经验。",
            "",
            "## 草稿来源",
            "",
            reason or "来自 Agent 2.0 自我训练复盘。",
            "",
            "## 原始变化说明",
            "",
            change or "暂无。",
            "",
        ])

    def _skill_patch_draft_markdown(self, proposal):
        """Build a patch draft by preserving the original SKILL.md content."""
        skill_name = str(proposal.get("skill_name") or "").strip() or self._slugify_skill_name(proposal.get("title"))
        original_path = self.workspace / "skills" / skill_name / "SKILL.md"
        original = ""
        try:
            if original_path.exists():
                original = original_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            original = ""
        reason = str(proposal.get("reason") or "").strip()
        change = str(proposal.get("change") or "").strip()
        now = self._now()
        patch_block = "\n".join([
            "",
            "",
            f"## Agent 2.0 修补建议（{now}）",
            "",
            "### 失败信号",
            "",
            reason or "该 Skill 在近期任务中多次未能稳定完成目标。",
            "",
            "### 触发边界",
            "",
            "- 仅当用户需求与本 Skill 的使用场景明确匹配时才启用。",
            "- 如果只是出现通用词，如“查询、整理、发送”，但没有命中本 Skill 的关键对象，不要强行使用。",
            "- 当任务涉及登录、发送、写入、删除、付款、发布等动作时，必须保留审批或人工接管点。",
            "",
            "### 失败兜底策略",
            "",
            "1. 先检查必要配置、授权目录、通道状态和运行环境是否可用。",
            "2. 如果空结果，先放宽安全范围或切换已验证来源，再重试一次。",
            "3. 如果工具不可用，降级到可完成的最小结果，并告诉用户缺什么配置。",
            "4. 如果模型认证失败，不要反复调用模型，直接提示修复模型配置。",
            "5. 输出给用户时只保留结果、关键原因和一个明确下一步，避免长篇排查。",
            "",
            "### 原始修补依据",
            "",
            change or "暂无详细依据。",
            "",
        ])
        if original:
            return original.rstrip() + patch_block
        return "\n".join([
            "---",
            f"name: {skill_name}",
            f"description: {skill_name}。来自 Agent 2.0 失败经验生成的修补 Skill。",
            "origin: self_training",
            "default_enabled: true",
            "---",
            "",
            f"# {skill_name}",
            "",
            "这是 Agent 2.0 根据失败经验生成的修补草稿。原 SKILL.md 未读取到，请先人工补全主说明后再安装。",
            patch_block,
        ])

    def create_skill_draft_from_proposal(self, proposal_id):
        """Create a local, non-active SKILL.md draft from an approved proposal."""
        data = self._read()
        proposal = next((x for x in data.get("proposals", []) if x.get("id") == proposal_id), None)
        if not proposal:
            raise ValueError("改进提案不存在")
        if proposal.get("kind") not in {"skill_create", "skill_patch"}:
            raise ValueError("该提案不是 Skill 类型")
        if proposal.get("status") != "approved":
            raise ValueError("请先批准该 Skill 提案")
        draft_id = proposal.get("draft_id") or uuid.uuid4().hex[:12]
        candidate = next((x for x in data.get("skill_candidates", []) if proposal_id in str(x.get("proposal_id") or "") or str(x.get("title") or "") in str(proposal.get("title") or "")), None)
        is_patch = proposal.get("kind") == "skill_patch"
        name = str(proposal.get("skill_name") or "").strip() if is_patch else ""
        if not name:
            name = self._slugify_skill_name(proposal.get("title"))
        draft_dir = self.root / "skill_drafts" / f"{name}-{draft_id}"
        draft_dir.mkdir(parents=True, exist_ok=True)
        skill_path = draft_dir / "SKILL.md"
        content = self._skill_patch_draft_markdown(proposal) if is_patch else self._skill_draft_markdown(proposal, candidate)
        skill_path.write_text(content, encoding="utf-8")
        draft = {
            "id": draft_id,
            "proposal_id": proposal_id,
            "name": name,
            "title": proposal.get("title") or name,
            "description": proposal.get("reason") or "",
            "path": str(skill_path),
            "kind": proposal.get("kind"),
            "target_skill": proposal.get("skill_name") or name,
            "status": "draft",
            "created_at": self._now(),
        }
        data.setdefault("skill_drafts", [])
        old = next((x for x in data["skill_drafts"] if x.get("proposal_id") == proposal_id), None)
        if old:
            old.update(draft)
        else:
            data["skill_drafts"].insert(0, draft)
        proposal["draft_id"] = draft_id
        proposal["draft_path"] = str(skill_path)
        self._event(data, "self_training.skill_draft.created", "已从审批提案生成 Skill 草稿", {"proposal_id": proposal_id, "draft_id": draft_id, "path": str(skill_path)}, risk=1)
        self._write(data)
        return draft

    def _resolve_skill_draft_path(self, raw_path):
        """Return a safe draft SKILL.md path inside the Agent 2.0 draft area."""
        draft_root = (self.root / "skill_drafts").resolve()
        target = Path(str(raw_path or "")).resolve()
        if draft_root != target and draft_root not in target.parents:
            raise ValueError("Skill 草稿路径不在允许范围内")
        if target.name != "SKILL.md":
            raise ValueError("Skill 草稿文件必须是 SKILL.md")
        return target

    def _resolve_skill_backup_path(self, raw_path):
        backup_root = (self.root / "skill_drafts" / "_backups").resolve()
        target = Path(str(raw_path or "")).resolve()
        if backup_root != target and backup_root not in target.parents:
            raise ValueError("Skill 备份路径不在允许范围内")
        if not target.exists() or not target.is_file():
            raise ValueError("Skill 备份文件不存在")
        return target

    def _resolve_installed_skill_path(self, raw_path):
        skills_root = (self.workspace / "skills").resolve()
        target = Path(str(raw_path or "")).resolve()
        if skills_root != target and skills_root not in target.parents:
            raise ValueError("正式 Skill 路径不在允许范围内")
        if target.name != "SKILL.md":
            raise ValueError("正式 Skill 文件必须是 SKILL.md")
        return target

    def get_skill_draft(self, draft_id):
        """Read a Skill draft for UI preview/editing."""
        data = self._read()
        draft = next((x for x in data.get("skill_drafts", []) if x.get("id") == draft_id and x.get("status") != "deleted"), None)
        if not draft:
            raise ValueError("Skill 草稿不存在")
        skill_path = self._resolve_skill_draft_path(draft.get("path"))
        content = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
        return {**draft, "content": content}

    def update_skill_draft(self, draft_id, payload):
        """Update a local Skill draft without installing it."""
        data = self._read()
        draft = next((x for x in data.get("skill_drafts", []) if x.get("id") == draft_id and x.get("status") != "deleted"), None)
        if not draft:
            raise ValueError("Skill 草稿不存在")
        skill_path = self._resolve_skill_draft_path(draft.get("path"))
        content = str((payload or {}).get("content") or "").strip()
        if not content:
            raise ValueError("Skill 草稿内容不能为空")
        if len(content) > 50000:
            raise ValueError("Skill 草稿内容过长，请精简后再保存")
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(content, encoding="utf-8")
        if (payload or {}).get("title") is not None:
            draft["title"] = str(payload.get("title") or draft.get("title") or draft.get("name") or "Skill 草稿")[:120]
        if (payload or {}).get("description") is not None:
            draft["description"] = str(payload.get("description") or "")[:500]
        draft["updated_at"] = self._now()
        if draft.get("status") == "installed":
            draft["status"] = "draft"
            draft.pop("installed_at", None)
        self._event(data, "self_training.skill_draft.updated", "Skill 草稿已保存修改", {"draft_id": draft_id, "path": str(skill_path)}, risk=1)
        self._write(data)
        return self.get_skill_draft(draft_id)

    def install_skill_draft(self, draft_id):
        """Install a reviewed draft as a project-local formal Skill.

        The install target is intentionally limited to the current workspace's
        skills directory. Existing unrelated skills are never overwritten.
        """
        data = self._read()
        draft = next((x for x in data.get("skill_drafts", []) if x.get("id") == draft_id and x.get("status") != "deleted"), None)
        if not draft:
            raise ValueError("Skill 草稿不存在")
        skill_path = self._resolve_skill_draft_path(draft.get("path"))
        if not skill_path.exists():
            raise ValueError("Skill 草稿文件不存在")
        is_patch = draft.get("kind") == "skill_patch"
        name = self._slugify_skill_name(draft.get("target_skill") or draft.get("name") or draft.get("title") or draft_id)
        skills_root = (self.workspace / "skills").resolve()
        target_dir = (skills_root / name).resolve()
        if skills_root != target_dir and skills_root not in target_dir.parents:
            raise ValueError("正式 Skill 安装路径不在允许范围内")
        target_file = target_dir / "SKILL.md"
        previous_install = Path(str(draft.get("installed_path") or "")).resolve() if draft.get("installed_path") else None
        if target_file.exists() and previous_install != target_file.resolve() and not is_patch:
            raise ValueError(f"正式 Skill 已存在：{name}。请先修改草稿名称后再安装。")
        content = skill_path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError("Skill 草稿内容为空，无法安装")
        target_dir.mkdir(parents=True, exist_ok=True)
        backup_path = ""
        if target_file.exists():
            backup_dir = self.root / "skill_drafts" / "_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = str(backup_dir / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.SKILL.md.bak")
            Path(backup_path).write_text(target_file.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        target_file.write_text(content, encoding="utf-8")
        draft["name"] = name
        draft["status"] = "installed"
        draft["installed_at"] = self._now()
        draft["installed_path"] = str(target_file)
        if backup_path:
            draft["backup_path"] = backup_path
        data.setdefault("skill_versions", [])
        for item in data["skill_versions"]:
            if item.get("skill_name") == name and item.get("status") == "active":
                item["status"] = "superseded"
        version = {
            "id": uuid.uuid4().hex[:12],
            "skill_name": name,
            "draft_id": draft_id,
            "proposal_id": draft.get("proposal_id") or "",
            "kind": draft.get("kind") or "skill_create",
            "installed_path": str(target_file),
            "backup_path": backup_path,
            "summary": draft.get("description") or draft.get("title") or name,
            "status": "active",
            "created_at": self._now(),
            "can_rollback": bool(backup_path),
        }
        data["skill_versions"].insert(0, version)
        data["skill_versions"] = data["skill_versions"][:200]
        self._event(data, "self_training.skill_draft.installed", "Skill 草稿已安装为正式 Skill", {"draft_id": draft_id, "name": name, "path": str(target_file), "backup_path": backup_path}, risk=2)
        self._write(data)
        return self.get_skill_draft(draft_id)

    def skill_version_history(self, skill_name=""):
        data = self._read()
        name = str(skill_name or "").strip()
        versions = data.get("skill_versions", [])
        if name:
            versions = [x for x in versions if x.get("skill_name") == name]
        result = []
        for item in versions:
            can_rollback = False
            if item.get("backup_path"):
                try:
                    can_rollback = self._resolve_skill_backup_path(item.get("backup_path")).exists()
                except Exception:
                    can_rollback = False
            result.append({**item, "can_rollback": can_rollback})
        result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return result

    def skill_rollback_summary(self, data=None):
        payload = data or self._read()
        rows = {}
        for item in payload.get("skill_versions", []):
            if item.get("kind") != "rollback":
                continue
            name = str(item.get("skill_name") or "").strip()
            if not name:
                continue
            row = rows.setdefault(name, {"skill_name": name, "rollback_count": 0, "last_rollback_at": "", "last_reason": ""})
            row["rollback_count"] += 1
            if str(item.get("created_at") or "") >= str(row.get("last_rollback_at") or ""):
                row["last_rollback_at"] = item.get("created_at") or ""
                row["last_reason"] = item.get("summary") or "用户已回滚该 Skill 修补"
        result = list(rows.values())
        result.sort(key=lambda x: (int(x.get("rollback_count") or 0), str(x.get("last_rollback_at") or "")), reverse=True)
        return result

    def rollback_skill_version(self, version_id):
        data = self._read()
        versions = data.setdefault("skill_versions", [])
        version = next((x for x in versions if x.get("id") == version_id), None)
        if not version:
            raise ValueError("Skill 版本记录不存在")
        backup_path = self._resolve_skill_backup_path(version.get("backup_path"))
        installed_path = self._resolve_installed_skill_path(version.get("installed_path"))
        if not installed_path.exists():
            raise ValueError("正式 Skill 文件不存在，无法回滚")
        skill_name = version.get("skill_name") or installed_path.parent.name
        rollback_backup_dir = self.root / "skill_drafts" / "_backups"
        rollback_backup_dir.mkdir(parents=True, exist_ok=True)
        current_backup = rollback_backup_dir / f"{skill_name}-rollback-current-{datetime.now().strftime('%Y%m%d-%H%M%S')}.SKILL.md.bak"
        current_backup.write_text(installed_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        installed_path.write_text(backup_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        for item in versions:
            if item.get("skill_name") == skill_name and item.get("status") == "active":
                item["status"] = "superseded"
        rollback_version = {
            "id": uuid.uuid4().hex[:12],
            "skill_name": skill_name,
            "draft_id": version.get("draft_id") or "",
            "proposal_id": version.get("proposal_id") or "",
            "kind": "rollback",
            "installed_path": str(installed_path),
            "backup_path": str(current_backup),
            "restored_from": version_id,
            "summary": f"已回滚到 {version.get('created_at') or '上一版'} 前的 Skill 内容",
            "status": "active",
            "created_at": self._now(),
            "can_rollback": True,
        }
        versions.insert(0, rollback_version)
        data["skill_versions"] = versions[:200]
        self._remember_route_fact(
            "failure_lesson",
            f"Skill 回滚经验：{skill_name}",
            f"该 Skill 曾被回滚。后续路由和修补建议需要降低激进修补优先级，除非出现更多失败证据。",
            tags=["自我训练", "Skill回滚", skill_name],
            metadata={"source": "skill_version_rollback", "source_type": "skill_version_rollback", "source_id": version_id, "version_id": version_id},
            confidence=0.84,
        )
        self._event(data, "self_training.skill_version.rollback", f"Skill 已回滚：{skill_name}", {"version_id": version_id, "path": str(installed_path)}, risk=2)
        self._write(data)
        return rollback_version

    def export_training_dataset(self):
        """Export local learning samples as JSONL for future fine-tuning.

        The export is local only and excludes credentials / cookies / tokens.
        """
        data = self._read()
        rows = []
        for item in data.get("weixin_success_patterns", []):
            command = (item.get("examples") or [{}])[0].get("command") or item.get("task_type") or "微信任务"
            rows.append({"input": command, "output": item.get("label") or "复用成功路径", "type": "success_path", "source": "weixin"})
        for item in data.get("weixin_failure_patterns", []):
            command = (item.get("examples") or [{}])[0].get("command") or item.get("category") or "微信失败"
            rows.append({"input": command, "output": item.get("recommended_action") or item.get("lesson") or "失败后给出修复建议", "type": "failure_lesson", "source": "weixin"})
        for item in data.get("recovery_patterns", []):
            rows.append({"input": item.get("category") or "失败恢复", "output": item.get("action") or "按推荐参数重试", "type": "recovery_path", "source": "automation"})
        safe_rows = []
        for row in rows:
            text = json.dumps(row, ensure_ascii=False)
            if re.search(r"(密码|验证码|token|secret|api[_-]?key|sk-[A-Za-z0-9]|cookie|authorization|bearer\s+[A-Za-z0-9._-]+)", text, re.I):
                continue
            safe_rows.append(row)
        export_dir = self.root / "training_exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        path = export_dir / f"cowagent-training-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in safe_rows), encoding="utf-8")
        data.setdefault("training_exports", []).insert(0, {"path": str(path), "count": len(safe_rows), "created_at": self._now(), "format": "jsonl"})
        data["training_exports"] = data["training_exports"][:50]
        self._event(data, "self_training.export", f"训练样本已导出：{len(safe_rows)} 条", {"path": str(path), "count": len(safe_rows)})
        self._write(data)
        return {"path": str(path), "count": len(safe_rows)}

    def heartbeat(self):
        data=self._read(); checks=[]
        checks.append({"name":"重复错误","count":len(data["error_patterns"]),"severity":"warning" if data["error_patterns"] else "ok"})
        checks.append({"name":"待审改进提案","count":sum(1 for x in data["proposals"] if x.get("status")=="pending"),"severity":"warning"})
        checks.append({"name":"自进化开关","count":1 if self.config.get("self_evolution_enabled",True) else 0,"severity":"ok" if self.config.get("self_evolution_enabled",True) else "warning"})
        event=self._event(data,"heartbeat.check","Heartbeat 主动检查完成",{"checks":checks})
        self._write(data); return {"event":event,"checks":checks}

    def create_proposal(self, payload):
        data=self._read(); kind=str(payload.get("kind") or "memory"); allowed={"memory","profile","skill_create","skill_patch"}
        if kind not in allowed: raise ValueError("不支持的改进类型")
        item={"id":uuid.uuid4().hex[:14],"kind":kind,"title":str(payload.get("title") or "改进提案"),"reason":str(payload.get("reason") or ""),"change":str(payload.get("change") or ""),"status":"pending","created_at":self._now()}
        data["proposals"].insert(0,item);self._event(data,"proposal.created",f"创建 {kind} 改进提案",{"proposal_id":item["id"]},risk=2,status="pending");self._write(data);return item

    def resolve_proposal(self, proposal_id, decision):
        data=self._read();item=next((x for x in data["proposals"] if x["id"]==proposal_id),None)
        if not item: raise ValueError("改进提案不存在")
        item["status"]="approved" if decision=="approved" else "rejected";item["resolved_at"]=self._now()
        self._event(data,"proposal.resolved",f"改进提案已{('批准' if decision=='approved' else '拒绝')}",{"proposal_id":proposal_id,"decision":decision})
        self._write(data);return item
