from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


class AgentTrainingLabService:
    """Lightweight Agent evaluation lab.

    This is an engineering harness, not model-weight training.  It turns real
    user workflows into reusable test cases, scores the agent from local traces
    and artifacts, captures failure samples, and feeds the Harness evolution
    panel with repeatable regression checks.
    """

    RUBRIC = [
        {"id": "intent", "name": "是否理解用户意图", "weight": 15, "rule": "不能只罗列原始数据，必须按用户目标整理。"},
        {"id": "do_first", "name": "是否先执行", "weight": 10, "rule": "优先尝试可用路径，失败后再说明配置或权限问题。"},
        {"id": "filter_noise", "name": "是否过滤无效内容", "weight": 10, "rule": "过滤系统邮件、发票、Bug/ETL 通知、低质量搜索结果。"},
        {"id": "artifact", "name": "是否有产物", "weight": 15, "rule": "重要输出必须进入产物/交付物中心并可追溯来源。"},
        {"id": "notification", "name": "是否有通知", "weight": 10, "rule": "需要送达 Web/微信/企微/邮箱之一，失败要补发 Web。"},
        {"id": "source_trace", "name": "是否有来源", "weight": 10, "rule": "结果能关联到任务、运行会话、原文链接或 Trace。"},
        {"id": "readable_failure", "name": "失败是否可读", "weight": 15, "rule": "失败不能只写操作失败，要有原因、证据和重试建议。"},
        {"id": "memory_reuse", "name": "是否复用记忆", "weight": 15, "rule": "应使用用户偏好、常用来源、邮件过滤偏好和历史成功路径。"},
    ]

    DEFAULT_CASES = [
        {
            "id": "weixin-check-important-mail",
            "name": "微信查重要邮件",
            "agent": "微信助理",
            "scenario": "微信消息",
            "input": "帮我看今天有没有重要邮件",
            "expected": "先尝试读取邮箱/Thunderbird，再过滤系统邮件，按优先级汇报重要邮件；失败时说明邮箱或模型原因。",
            "required_tools": ["weixin", "email", "artifact", "trace"],
            "forbidden": ["只提示配置", "只列原始邮件", "空回复"],
            "success_criteria": ["有执行记录", "过滤系统邮件", "有可读摘要", "进入会话记录"],
        },
        {
            "id": "mail-digest-ignore-system",
            "name": "邮件整理过滤系统通知",
            "agent": "邮件 Agent",
            "scenario": "邮件整理",
            "input": "帮我整理 8.31-9.8 邮件，系统邮件忽略",
            "expected": "忽略 Bug、发票、ETL 等系统邮件，只保留合同、续费、付款、审批、客户、故障类重点，并按优先级汇报。",
            "required_tools": ["email", "artifact", "memory"],
            "forbidden": ["直接列 10 封邮件", "保留 ETL/Bug/发票为重点", "没有优先级"],
            "success_criteria": ["只保留有效邮件", "有优先级", "有摘要", "写入产物"],
        },
        {
            "id": "quick-learning-url",
            "name": "快速学习链接总结",
            "agent": "快速学习 Agent",
            "scenario": "网页/公众号学习",
            "input": "粘贴公众号或网页链接，帮我快速学习核心内容",
            "expected": "提炼文章讲什么、值得学习什么、可复用什么，输出短学习笔记并保存产物。",
            "required_tools": ["scrapling", "quick_learning", "artifact"],
            "forbidden": ["大段搬运原文", "只抽关键词", "没有复用建议"],
            "success_criteria": ["正文提取", "核心结论", "可复用动作", "保存产物"],
        },
        {
            "id": "automation-weather-pudong",
            "name": "创建天气自动化",
            "agent": "自动化任务",
            "scenario": "任务创建",
            "input": "每天早上8:00发送上海浦东天气情况",
            "expected": "识别为天气任务，不应生成新闻搜索 Prompt；保存后能按周期执行并发送到指定通道。",
            "required_tools": ["weather", "scheduler", "notification"],
            "forbidden": ["生成个人早间简报", "查询新闻热点", "不支持的任务内容类型"],
            "success_criteria": ["任务类型正确", "时间正确", "地点正确", "通知目标正确"],
        },
        {
            "id": "news-hotspot-nonempty",
            "name": "新闻热点必须有内容",
            "agent": "网络搜索 Agent",
            "scenario": "资讯采集",
            "input": "查询新闻热点，要求必须有内容",
            "expected": "搜索/读取/筛选/去重后输出 5-10 条；空结果要自动扩展时间或切换来源，不能重复生成暂无内容产物。",
            "required_tools": ["search", "rss", "artifact", "recovery"],
            "forbidden": ["一天重复触发多次", "大量暂无内容产物", "标题和摘要重复"],
            "success_criteria": ["有内容", "去重", "30-50字描述", "空结果有兜底"],
        },
        {
            "id": "wecom-roundtrip",
            "name": "企业微信消息回传",
            "agent": "企业微信机器人",
            "scenario": "企业微信",
            "input": "企业微信发送一条任务并回传结果",
            "expected": "企业微信真实消息和模拟发送进入同一会话记录，执行结果可回传，失败时补发 Web 并沉淀失败学习。",
            "required_tools": ["wecom_bot", "trace", "notification", "conversation_archive"],
            "forbidden": ["真实/模拟不同步", "发送测试无响应", "只显示连接成功但收不到"],
            "success_criteria": ["会话同步", "可回传", "失败可读", "写入学习"],
        },
        {
            "id": "failed-task-retry",
            "name": "失败任务按推荐参数重试",
            "agent": "失败恢复",
            "scenario": "失败恢复",
            "input": "失败任务一键重试",
            "expected": "按失败原因选择推荐参数，例如搜索空结果扩展到7天、通知失败补发Web，并记录恢复效果。",
            "required_tools": ["recovery", "automation_runs", "artifact", "notification"],
            "forbidden": ["重复原参数失败", "只展示失败不处理", "无 run_id"],
            "success_criteria": ["有 run_id", "有恢复动作", "记录效果", "最终状态清晰"],
        },
    ]

    def __init__(self, root, workspace=None):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace or root).resolve()
        self.state_path = self.root / "agent_training_lab.json"
        self.reports_dir = self.root / "reports"

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
        data.setdefault("cases", [])
        data.setdefault("runs", [])
        data.setdefault("failure_samples", [])
        data.setdefault("events", [])
        data.setdefault("ingested_sources", [])
        data.setdefault("last_ingest", {})
        data.setdefault("created_at", self._now())
        if not data["cases"]:
            data["cases"] = [dict(item, enabled=True, created_at=self._now(), updated_at=self._now()) for item in self.DEFAULT_CASES]
        self._prune_state_noise(data)
        return data

    @staticmethod
    def _is_training_noise_text(text):
        hay = str(text or "").lower()
        noise_words = ("agent 训练场评测报告", "训练场评测报告", "自我优化诊断报告", "通知通道测试", "web 通知测试", "channel-test", "web-sim", "模拟发送")
        return any(word.lower() in hay for word in noise_words)

    def _prune_state_noise(self, data):
        removed_case_ids = set()
        clean_cases = []
        for item in data.get("cases") or []:
            hay = " ".join(str(item.get(k) or "") for k in ("name", "input", "expected", "source_type", "source_id"))
            if item.get("auto_generated") and self._is_training_noise_text(hay):
                removed_case_ids.add(item.get("id"))
                continue
            clean_cases.append(item)
        if removed_case_ids:
            data["cases"] = clean_cases
            data["runs"] = [x for x in (data.get("runs") or []) if x.get("case_id") not in removed_case_ids]
            data["failure_samples"] = [x for x in (data.get("failure_samples") or []) if x.get("case_id") not in removed_case_ids and not self._is_training_noise_text(" ".join(str(x.get(k) or "") for k in ("agent", "scenario", "user_input", "bad_output")))]

    def _save_state(self, data):
        data["updated_at"] = self._now()
        data["runs"] = (data.get("runs") or [])[:500]
        data["failure_samples"] = (data.get("failure_samples") or [])[:500]
        data["events"] = (data.get("events") or [])[:300]
        data["ingested_sources"] = (data.get("ingested_sources") or [])[-1500:]
        self._write_json(self.state_path, data)

    def dashboard(self):
        state = self._state()
        cases = state.get("cases") or []
        runs = state.get("runs") or []
        ranking = self.capability_ranking(runs)
        last_suite = next((r for r in runs if r.get("kind") == "suite"), None)
        summary = {
            "cases": len(cases),
            "enabled_cases": sum(1 for item in cases if item.get("enabled", True)),
            "runs": len([r for r in runs if r.get("kind") == "case"]),
            "failure_samples": len(state.get("failure_samples") or []),
            "avg_score": self._avg([r.get("score") for r in runs if r.get("kind") == "case"]),
            "last_suite_score": last_suite.get("score") if last_suite else None,
            "ingested_sources": len(state.get("ingested_sources") or []),
            "last_ingest_new_cases": (state.get("last_ingest") or {}).get("new_cases", 0),
            "last_ingest_failure_samples": (state.get("last_ingest") or {}).get("failure_samples", 0),
            "last_ingest_at": (state.get("last_ingest") or {}).get("created_at", ""),
        }
        return {
            "summary": summary,
            "cases": cases,
            "recent_runs": [r for r in runs if r.get("kind") == "case"][:20],
            "failure_samples": (state.get("failure_samples") or [])[:20],
            "ranking": ranking,
            "rubric": self.RUBRIC,
            "events": (state.get("events") or [])[:20],
            "last_ingest": state.get("last_ingest") or {},
        }

    @staticmethod
    def _avg(values):
        nums = [float(v) for v in values if isinstance(v, (int, float))]
        return round(sum(nums) / len(nums), 1) if nums else 0

    def save_case(self, payload):
        state = self._state()
        case_id = str(payload.get("id") or f"case_{uuid.uuid4().hex[:10]}")
        existing = next((x for x in state["cases"] if x.get("id") == case_id), None)
        item = dict(existing or {})
        for key in ("name", "agent", "scenario", "input", "expected"):
            value = str(payload.get(key) or item.get(key) or "").strip()
            if key in {"name", "input", "expected"} and not value:
                raise ValueError("请填写测试名称、输入和期望行为")
            item[key] = value
        for key in ("required_tools", "forbidden", "success_criteria"):
            value = payload.get(key, item.get(key) or [])
            if isinstance(value, str):
                value = [x.strip() for x in re.split(r"[\n,，;；]+", value) if x.strip()]
            item[key] = list(value or [])
        item["id"] = case_id
        item["enabled"] = bool(payload.get("enabled", item.get("enabled", True)))
        item["updated_at"] = self._now()
        item.setdefault("created_at", self._now())
        state["cases"] = [x for x in state["cases"] if x.get("id") != case_id]
        state["cases"].insert(0, item)
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "case.save", "summary": f"保存评测用例：{item['name']}"})
        self._save_state(state)
        return item

    def reset_defaults(self):
        state = self._state()
        existing = {x.get("id"): x for x in state.get("cases") or []}
        cases = []
        for item in self.DEFAULT_CASES:
            old = existing.get(item["id"]) or {}
            cases.append({**item, "enabled": old.get("enabled", True), "created_at": old.get("created_at") or self._now(), "updated_at": self._now()})
        state["cases"] = cases
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "case.reset", "summary": "已恢复默认真实任务测试集"})
        self._save_state(state)
        return cases

    def run_case(self, case_id, mode="simulation"):
        state = self._state()
        case = next((x for x in state.get("cases") or [] if x.get("id") == case_id), None)
        if not case:
            raise KeyError("测试用例不存在")
        run = self._evaluate_case(case, mode=mode)
        state["runs"].insert(0, run)
        if run.get("status") != "passed":
            state["failure_samples"].insert(0, self._failure_sample(case, run))
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "case.run", "summary": f"执行评测：{case.get('name')} · {run.get('score')}分"})
        self._save_state(state)
        return run

    def run_suite(self, mode="simulation"):
        state = self._state()
        cases = [x for x in state.get("cases") or [] if x.get("enabled", True)]
        case_runs = [self._evaluate_case(case, mode=mode) for case in cases]
        for run, case in zip(case_runs, cases):
            if run.get("status") != "passed":
                state["failure_samples"].insert(0, self._failure_sample(case, run))
        suite = {
            "id": f"suite_{uuid.uuid4().hex[:10]}",
            "kind": "suite",
            "mode": mode,
            "created_at": self._now(),
            "case_count": len(case_runs),
            "passed": sum(1 for item in case_runs if item.get("status") == "passed"),
            "score": self._avg([item.get("score") for item in case_runs]),
            "case_run_ids": [item.get("id") for item in case_runs],
        }
        state["runs"] = [suite] + case_runs + (state.get("runs") or [])
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "suite.run", "summary": f"完成训练场回放：{suite['passed']}/{suite['case_count']} 通过，平均 {suite['score']} 分"})
        self._save_state(state)
        report = self._write_suite_report(suite, case_runs)
        suite["report_path"] = report.get("path")
        suite["artifact"] = report.get("artifact")
        return {"suite": suite, "runs": case_runs, "report": report}

    def auto_cycle(self, mode="simulation", max_items=60):
        ingest = self.auto_ingest(max_items=max_items)
        replay = self.run_suite(mode=mode)
        return {"ingest": ingest, "replay": replay, "message": f"已吸收 {ingest.get('new_cases', 0)} 个用例、{ingest.get('failure_samples', 0)} 个失败样本，并完成回放"}

    def auto_ingest(self, max_items=80, max_new_cases=25, max_failure_samples=25):
        """Safely turn recent local evidence into regression cases.

        This only reads local history and writes training metadata.  It never
        sends messages, changes task configs, or replays real-world side effects.
        """
        state = self._state()
        seen = {item.get("id") for item in state.get("ingested_sources") or [] if item.get("id")}
        new_cases = []
        new_samples = []

        def remember(source_id, kind):
            seen.add(source_id)
            state["ingested_sources"].append({"id": source_id, "kind": kind, "at": self._now()})

        for source in self._iter_ingest_sources(max_items=max_items):
            source_id = source.get("source_id")
            if not source_id or source_id in seen:
                continue
            if source.get("failed"):
                if len(new_samples) >= max_failure_samples:
                    continue
                sample = self._sample_from_source(source)
                if sample:
                    new_samples.append(sample)
                    state["failure_samples"].insert(0, sample)
            else:
                if len(new_cases) >= max_new_cases:
                    continue
                case = self._case_from_source(source)
                if case and not self._case_exists(state, case):
                    new_cases.append(case)
                    state["cases"].insert(0, case)
            remember(source_id, source.get("source_type") or "unknown")

        summary = {
            "created_at": self._now(),
            "scanned": len(seen),
            "new_cases": len(new_cases),
            "failure_samples": len(new_samples),
            "sources": {
                "artifact": sum(1 for item in new_cases + new_samples if item.get("source_type") == "artifact"),
                "automation_run": sum(1 for item in new_cases + new_samples if item.get("source_type") == "automation_run"),
                "notification_failure": sum(1 for item in new_cases + new_samples if item.get("source_type") == "notification_failure"),
                "scheduler_task": sum(1 for item in new_cases + new_samples if item.get("source_type") == "scheduler_task"),
                "conversation": sum(1 for item in new_cases + new_samples if item.get("source_type") == "conversation"),
            },
            "message": f"自动吸收完成：新增 {len(new_cases)} 个训练用例、{len(new_samples)} 个失败样本",
        }
        state["last_ingest"] = summary
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "training.auto_ingest", "summary": summary["message"]})
        self._save_state(state)
        return summary

    def _iter_ingest_sources(self, max_items=80):
        items = []
        items.extend(self._artifact_sources(max_items=max_items))
        items.extend(self._automation_run_sources(max_items=max_items))
        items.extend(self._notification_failure_sources(max_items=max_items))
        items.extend(self._agent_reach_sources())
        items.extend(self._scheduler_task_sources(max_items=max_items))
        items.extend(self._conversation_sources(max_items=max_items))
        return sorted(items, key=lambda x: str(x.get("created_at") or ""), reverse=True)[:max_items]

    def _artifact_sources(self, max_items=80):
        artifacts = self._read_json(self.root / "artifacts.json", {}).get("artifacts") or []
        if isinstance(artifacts, dict):
            artifacts = list(artifacts.values())
        if not isinstance(artifacts, list):
            artifacts = []
        result = []
        for item in artifacts[:max_items]:
            title = str(item.get("title") or item.get("name") or "产物")
            preview = str(item.get("preview") or item.get("result_summary") or "")
            tags = " ".join(map(str, item.get("tags") or []))
            text = " ".join([title, preview, tags, str(item.get("source") or ""), str(item.get("task_name") or "")])
            if self._is_system_generated_artifact(item, text):
                continue
            result.append({
                "source_type": "artifact",
                "source_id": "artifact:" + str(item.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, text).hex),
                "artifact_id": item.get("id"),
                "created_at": item.get("created_at") or item.get("updated_at") or "",
                "title": title,
                "text": text,
                "agent": self._agent_from_text(text),
                "scenario": "产物质量回归",
                "failed": self._looks_failed(text),
                "failure_reason": "产物为空、失败或包含暂无内容" if self._looks_failed(text) else "",
            })
        return result

    @staticmethod
    def _is_system_generated_artifact(item, text=""):
        source = str(item.get("source") or item.get("source_type") or "").lower()
        title = str(item.get("title") or "")
        hay = (str(text or "") + " " + source + " " + title).lower()
        noise_words = ("agent_training_lab", "agent 训练场评测报告", "训练场评测", "harness_review", "自我优化诊断报告", "channel-test", "通知通道测试", "web-sim", "模拟发送")
        return any(word.lower() in hay for word in noise_words)

    def _automation_run_sources(self, max_items=80):
        runs = self._read_json(self.root / "automation_runs.json", {}).get("runs") or []
        if isinstance(runs, dict):
            runs = list(runs.values())
        if not isinstance(runs, list):
            runs = []
        result = []
        for item in runs[:max_items]:
            title = str(item.get("task_name") or item.get("name") or "自动化运行")
            text = " ".join(str(item.get(k) or "") for k in ("task_name", "kind_label", "status", "result_summary", "error", "empty_reason", "failure_reason"))
            status = str(item.get("status") or "").lower()
            failed = status in {"failed", "error", "empty"} or self._looks_failed(text)
            result.append({
                "source_type": "automation_run",
                "source_id": "automation_run:" + str(item.get("id") or item.get("run_id") or uuid.uuid5(uuid.NAMESPACE_URL, text).hex),
                "run_id": item.get("id") or item.get("run_id"),
                "created_at": item.get("started_at") or item.get("finished_at") or item.get("created_at") or "",
                "title": title,
                "text": text,
                "agent": self._agent_from_text(text),
                "scenario": "自动化运行回归",
                "failed": failed,
                "failure_reason": item.get("failure_reason") or item.get("empty_reason") or item.get("error") or ("自动化运行未产出有效内容" if failed else ""),
            })
        return result

    def _notification_failure_sources(self, max_items=80):
        events = self._read_json(self.root / "notification_events.json", {}).get("events") or []
        if isinstance(events, dict):
            events = list(events.values())
        if not isinstance(events, list):
            events = []
        result = []
        for item in events[-max_items:]:
            if item.get("success"):
                continue
            channel = str(item.get("channel") or item.get("target") or "unknown").strip() or "unknown"
            reason = str(item.get("message") or item.get("error") or "发送失败").strip() or "发送失败"
            task = str(item.get("task_name") or item.get("name") or "").strip()
            text = f"{channel} 通知发送失败：{reason}" + (f"｜{task}" if task else "")
            result.append({
                "source_type": "notification_failure",
                "source_id": "notification_failure:" + str(item.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, text + str(item.get("created_at") or "")).hex),
                "created_at": item.get("created_at") or "",
                "title": "通知失败：" + channel,
                "text": text,
                "agent": "通知通道",
                "scenario": "通知失败学习",
                "failed": True,
                "failure_reason": reason,
                "run_id": item.get("run_id") or "",
            })
        return result

    def _agent_reach_sources(self):
        item = self._read_json(self.root / "agent_reach_health.json", {})
        if not item:
            return []
        installed = bool(item.get("installed"))
        doctor_ok = bool(item.get("doctor_ok"))
        # Agent-Reach is an enhanced source provider. When it is unavailable the
        # product already has normal search/RSS fallback, so do not turn this
        # into a failure sample or noisy training item.
        if installed and doctor_ok:
            return []
        return []

    def _scheduler_task_sources(self, max_items=80):
        candidates = [
            self.workspace / "work" / "cow-workspace" / "scheduler" / "tasks.json",
            self.root.parent / "work" / "cow-workspace" / "scheduler" / "tasks.json",
            self.root / "scheduler" / "tasks.json",
        ]
        tasks = []
        for path in candidates:
            data = self._read_json(path, {})
            tasks = data.get("tasks") if isinstance(data, dict) else data
            if isinstance(tasks, dict):
                tasks = list(tasks.values())
            if tasks and not isinstance(tasks, list):
                tasks = []
            if tasks:
                break
        result = []
        for item in (tasks or [])[:max_items]:
            title = str(item.get("name") or item.get("title") or "定时任务")
            text = " ".join(str(item.get(k) or "") for k in ("name", "title", "content", "prompt", "status", "last_result", "last_error", "last_failure_reason"))
            failed = self._looks_failed(text) or str(item.get("last_status") or "").lower() in {"failed", "empty", "error"}
            result.append({
                "source_type": "scheduler_task",
                "source_id": "scheduler_task:" + str(item.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, text).hex),
                "task_id": item.get("id"),
                "created_at": item.get("updated_at") or item.get("created_at") or "",
                "title": title,
                "text": text,
                "agent": "自动化任务",
                "scenario": "定时任务回归",
                "failed": failed,
                "failure_reason": item.get("last_failure_reason") or item.get("last_error") or ("定时任务最近失败或暂无内容" if failed else ""),
            })
        return result

    def _conversation_sources(self, max_items=80):
        db_paths = [self.root / "cowagent_state.db", self.workspace / "cowagent_state.db"]
        result = []
        for db_path in db_paths:
            if not db_path.exists():
                continue
            try:
                con = sqlite3.connect(str(db_path))
                con.row_factory = sqlite3.Row
                tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'").fetchall()]
                for table in tables:
                    columns = [r[1] for r in con.execute(f"pragma table_info({table})").fetchall()]
                    content_col = next((c for c in columns if c.lower() in {"content", "message", "text", "body", "reply", "response"}), None)
                    if not content_col:
                        continue
                    time_col = next((c for c in columns if c.lower() in {"created_at", "updated_at", "time", "timestamp", "ts"}), None)
                    role_col = next((c for c in columns if c.lower() in {"role", "sender", "source", "direction", "type"}), None)
                    order = f" order by {time_col} desc" if time_col else " order by rowid desc"
                    select_cols = ["rowid", content_col] + ([time_col] if time_col else []) + ([role_col] if role_col else [])
                    rows = con.execute(f"select {', '.join(select_cols)} from {table}{order} limit ?", (max_items,)).fetchall()
                    for row in rows:
                        text = str(row[content_col] or "").strip()
                        if len(text) < 8 or not self._is_training_worthy(text):
                            continue
                        role = str(row[role_col] or "") if role_col else ""
                        created_at = str(row[time_col] or "") if time_col else ""
                        result.append({
                            "source_type": "conversation",
                            "source_id": f"conversation:{db_path.name}:{table}:{row['rowid']}",
                            "created_at": created_at,
                            "title": self._short_title(text, "会话样本"),
                            "text": text,
                            "agent": self._agent_from_text(text),
                            "scenario": "会话回归",
                            "failed": self._looks_failed(text),
                            "failure_reason": "会话中出现失败、空结果或不可读回复" if self._looks_failed(text) else "",
                            "role": role,
                        })
                        if len(result) >= max_items:
                            return result
            except Exception:
                continue
            finally:
                try:
                    con.close()
                except Exception:
                    pass
        return result

    @staticmethod
    def _case_exists(state, case):
        source_id = case.get("source_id")
        return any(x.get("source_id") == source_id or x.get("id") == case.get("id") for x in state.get("cases") or [])

    @staticmethod
    def _looks_failed(text):
        text = str(text or "")
        return any(word in text for word in ("失败", "暂无内容", "无内容", "操作失败", "Failed", "Error", "空回复", "未配置", "不支持", "无法生成"))

    @staticmethod
    def _is_training_worthy(text):
        text = str(text or "")
        keywords = ("邮件", "微信", "企业微信", "企微", "自动化", "定时", "产物", "交付物", "新闻", "热点", "学习", "网页", "公众号", "搜索", "天气", "失败", "暂无内容")
        return any(k in text for k in keywords)

    @staticmethod
    def _short_title(text, fallback="训练样本"):
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        cleaned = re.sub(r"[#*`>\\[\\]()]+", "", cleaned)
        return (cleaned[:28] + "…") if len(cleaned) > 28 else (cleaned or fallback)

    def _agent_from_text(self, text):
        text = str(text or "")
        if "企业微信" in text or "企微" in text or "wecom" in text:
            return "企业微信机器人"
        if "微信" in text or "weixin" in text:
            return "微信助理"
        if "邮件" in text or "邮箱" in text or "Thunderbird" in text:
            return "邮件 Agent"
        if "公众号" in text or "网页" in text or "学习" in text:
            return "快速学习 Agent"
        if "新闻" in text or "热点" in text or "资讯" in text:
            return "网络搜索 Agent"
        if "定时" in text or "自动化" in text or "Cron" in text:
            return "自动化任务"
        return "通用 Agent"

    def _case_from_source(self, source):
        source_id = source.get("source_id")
        text = str(source.get("text") or source.get("title") or "")
        name = f"回归：{self._short_title(source.get('title') or text)}"
        agent = source.get("agent") or self._agent_from_text(text)
        expected = self._expected_from_agent(agent, text)
        return {
            "id": "auto_case_" + uuid.uuid5(uuid.NAMESPACE_URL, source_id).hex[:10],
            "name": name,
            "agent": agent,
            "scenario": source.get("scenario") or "自动吸收样本",
            "input": self._short_title(text, source.get("title") or name),
            "expected": expected,
            "required_tools": self._required_tools_from_agent(agent),
            "forbidden": ["空回复", "只提示配置", "暂无内容产物", "标题和摘要重复", "只列原始数据"],
            "success_criteria": ["理解意图", "有结果摘要", "写入产物或会话", "有来源可追溯"],
            "enabled": True,
            "auto_generated": True,
            "source_type": source.get("source_type"),
            "source_id": source_id,
            "artifact_id": source.get("artifact_id"),
            "run_id": source.get("run_id"),
            "session_id": source.get("session_id"),
            "created_at": self._now(),
            "updated_at": self._now(),
        }

    def _sample_from_source(self, source):
        text = str(source.get("text") or source.get("title") or "")
        agent = source.get("agent") or self._agent_from_text(text)
        return {
            "id": f"sample_{uuid.uuid4().hex[:10]}",
            "created_at": self._now(),
            "agent": agent,
            "scenario": source.get("scenario") or "自动吸收失败",
            "user_input": self._short_title(text, source.get("title") or "真实任务"),
            "bad_output": self._short_title(source.get("failure_reason") or text, "失败或空结果"),
            "expected_output": self._expected_from_agent(agent, text),
            "lesson": "该问题来自真实会话/产物/定时任务记录，后续应作为回归样本防止复发。",
            "patch_hint": "优先补充确定性工具、失败兜底、产物留痕和可读原因；真实发送或改配置前进入审批。",
            "status": "pending",
            "source_type": source.get("source_type"),
            "source_id": source.get("source_id"),
            "artifact_id": source.get("artifact_id"),
            "run_id": source.get("run_id"),
            "session_id": source.get("session_id"),
        }

    @staticmethod
    def _required_tools_from_agent(agent):
        agent = str(agent or "")
        if "微信" in agent:
            return ["weixin", "trace", "artifact"]
        if "企业微信" in agent:
            return ["wecom_bot", "trace", "notification"]
        if "邮件" in agent:
            return ["email", "artifact", "memory"]
        if "学习" in agent:
            return ["scrapling", "quick_learning", "artifact"]
        if "搜索" in agent or "新闻" in agent:
            return ["search", "artifact", "recovery"]
        if "自动化" in agent:
            return ["scheduler", "artifact", "notification"]
        return ["trace", "artifact"]

    @staticmethod
    def _expected_from_agent(agent, text):
        agent = str(agent or "")
        if "邮件" in agent:
            return "按用户目标整理邮件，过滤 Bug、发票、ETL 等系统通知，只保留合同、续费、付款、审批、客户、故障类重点，并写入产物。"
        if "学习" in agent:
            return "说明文章讲什么、值得学什么、哪些方法可复用，用短笔记降低学习时间，并保存产物。"
        if "搜索" in agent or "新闻" in agent:
            return "输出简短标题、链接和 30-50 字描述；去重、过滤低质量内容，空结果自动换来源或放宽范围。"
        if "微信" in agent or "企业微信" in agent:
            return "先执行可用路径，再返回结果；失败时给出最短原因、可重试方式，并进入同一会话记录和失败学习。"
        if "自动化" in agent:
            return "任务执行后必须有 run_id、执行状态、产物留痕、通知状态；失败需有可读原因和推荐恢复动作。"
        return "理解用户意图后先尝试执行，输出可读结果，并保留来源、Trace 和产物记录。"

    def _evaluate_case(self, case, mode="simulation"):
        evidence = self._collect_evidence(case)
        rubric_scores = []
        for rubric in self.RUBRIC:
            score, reason = self._score_rubric(rubric["id"], case, evidence)
            rubric_scores.append({**rubric, "score": score, "reason": reason})
        total_weight = sum(item["weight"] for item in self.RUBRIC) or 1
        score = round(sum(item["score"] * item["weight"] for item in rubric_scores) / total_weight, 1)
        status = "passed" if score >= 75 else ("warning" if score >= 55 else "failed")
        failed = [item for item in rubric_scores if item["score"] < 60]
        return {
            "id": f"run_{uuid.uuid4().hex[:10]}",
            "kind": "case",
            "case_id": case.get("id"),
            "case_name": case.get("name"),
            "agent": case.get("agent"),
            "scenario": case.get("scenario"),
            "mode": mode,
            "created_at": self._now(),
            "status": status,
            "score": score,
            "rubric_scores": rubric_scores,
            "failed_dimensions": [item["name"] for item in failed],
            "diagnosis": self._diagnosis(case, evidence, failed),
            "evidence": evidence,
            "recommended_patch": self._recommended_patch(case, failed),
        }

    def _collect_evidence(self, case):
        text = " ".join([case.get("name", ""), case.get("agent", ""), case.get("scenario", ""), case.get("input", "")]).lower()
        artifacts = self._read_json(self.root / "artifacts.json", {}).get("artifacts") or []
        runs = self._read_json(self.root / "automation_runs.json", {}).get("runs") or []
        harness = self._read_json(self.root / "harness_evolution.json", {})
        agent2 = self._read_json(self.root / "agent2_state.json", {})
        recent = self._recent_artifacts(artifacts, case)
        matched_runs = self._matched_runs(runs, case)
        error_patterns = agent2.get("error_patterns") or []
        diagnostics = harness.get("last_review", {}).get("diagnostics") or []
        return {
            "recent_artifacts": recent[:5],
            "matched_runs": matched_runs[:5],
            "has_artifact": bool(recent) or any(r.get("artifact") for r in matched_runs),
            "has_notification": any(r.get("notification_results") for r in matched_runs) or any(a.get("notification_summary") for a in recent),
            "has_trace": bool(matched_runs) or bool(diagnostics),
            "has_failure_learning": bool(error_patterns) or bool(diagnostics),
            "has_memory": (self.root / "memory_wiki.json").exists() or (self.root / "fact_store.json").exists() or bool(agent2.get("memory_candidates")),
            "recent_empty": any("暂无内容" in str(a.get("preview") or a.get("result_summary") or "") for a in recent) or any(r.get("status") == "empty" for r in matched_runs),
            "recent_failed": any(r.get("status") == "failed" for r in matched_runs),
            "weixin_related": "微信" in text or "weixin" in text or "wecom" in text or "企业微信" in text,
            "mail_related": "邮件" in text or "email" in text or "邮箱" in text,
            "news_related": "新闻" in text or "热点" in text or "资讯" in text,
            "quick_learning_related": "学习" in text or "网页" in text or "公众号" in text,
        }

    def _recent_artifacts(self, artifacts, case):
        words = self._case_words(case)
        scored = []
        for item in artifacts:
            hay = " ".join(str(item.get(k) or "") for k in ("title", "preview", "task_name", "kind_label", "source", "tags"))
            score = sum(1 for word in words if word and word in hay)
            if score:
                scored.append((score, item))
        return [item for _, item in sorted(scored, key=lambda x: (x[0], x[1].get("created_at", "")), reverse=True)]

    def _matched_runs(self, runs, case):
        words = self._case_words(case)
        result = []
        for item in runs:
            hay = " ".join(str(item.get(k) or "") for k in ("task_name", "kind_label", "result_summary", "error", "empty_reason", "failure_reason"))
            if any(word and word in hay for word in words):
                result.append(item)
        return sorted(result, key=lambda x: str(x.get("started_at") or x.get("finished_at") or ""), reverse=True)

    @staticmethod
    def _case_words(case):
        raw = " ".join(str(case.get(k) or "") for k in ("name", "agent", "scenario", "input", "expected"))
        words = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,}", raw)
        stop = {"帮我", "需要", "任务", "Agent", "agent", "是否", "内容", "结果", "发送", "执行"}
        return [w for w in dict.fromkeys(words) if w not in stop][:20]

    def _score_rubric(self, rubric_id, case, evidence):
        required = set(case.get("required_tools") or [])
        if rubric_id == "artifact":
            return (100, "已有相关产物或运行产物记录") if evidence["has_artifact"] else (35, "未找到相关产物记录")
        if rubric_id == "notification":
            return (90, "发现相关通知或发送摘要") if evidence["has_notification"] else (50, "尚未找到通知送达证据；模拟评测不直接发送")
        if rubric_id == "source_trace":
            return (90, "已有运行记录、Trace 或诊断证据") if evidence["has_trace"] else (45, "缺少可追溯 run_id / Trace")
        if rubric_id == "readable_failure":
            if evidence["recent_failed"] or evidence["recent_empty"]:
                return (75, "存在失败/空结果样本，已纳入失败学习判断") if evidence["has_failure_learning"] else (40, "有失败迹象但缺少可读诊断")
            return (85, "未发现近期同类失败")
        if rubric_id == "memory_reuse":
            return (80, "检测到记忆/画像/失败经验基础") if evidence["has_memory"] else (45, "尚未发现可复用记忆证据")
        if rubric_id == "filter_noise":
            if evidence["mail_related"]:
                return (70, "邮件过滤偏好已作为评测要求，需真实执行验证") if evidence["has_memory"] else (40, "邮件过滤偏好未见记忆证据")
            if evidence["news_related"]:
                return (45, "近期同类任务出现空结果或低质量风险") if evidence["recent_empty"] else (80, "未发现明显低质量结果")
            return (70, "该场景暂无明显噪声样本")
        if rubric_id == "do_first":
            return (80, "以本地证据先评测，真实执行可后续单独开启") if mode_safe_required(required) else (70, "当前为安全模拟评测")
        if rubric_id == "intent":
            if evidence["recent_empty"]:
                return (50, "同类任务曾出现空结果，说明意图/来源仍需增强")
            return (80, "测试用例已定义明确预期，可用于回归检查")
        return 60, "默认评分"

    def _diagnosis(self, case, evidence, failed):
        if not failed:
            return "该用例在当前本地证据下通过，可作为后续回归样本。"
        names = "、".join(item["name"] for item in failed[:3])
        if evidence["recent_empty"]:
            return f"重点问题：{names}。近期存在空结果，建议启用来源切换、放宽时间范围和结果质量检查。"
        if not evidence["has_artifact"]:
            return f"重点问题：{names}。缺少产物留痕，建议把该 Agent 输出统一写入产物/交付物。"
        return f"重点问题：{names}。建议补充 Trace、通知和失败学习证据。"

    def _recommended_patch(self, case, failed):
        ids = {item["id"] for item in failed}
        if "artifact" in ids:
            return "将该任务输出接入 ArtifactService，并写入 source_type/source_id/run_id/session_id。"
        if "readable_failure" in ids:
            return "把失败归类为可读原因，提供按推荐参数重试入口。"
        if "filter_noise" in ids:
            return "补充场景专用过滤规则，并加入失败样本回放。"
        return "保留为回归测试，后续改动后自动回放。"

    def _failure_sample(self, case, run):
        return {
            "id": f"sample_{uuid.uuid4().hex[:10]}",
            "created_at": self._now(),
            "case_id": case.get("id"),
            "agent": case.get("agent"),
            "scenario": case.get("scenario"),
            "user_input": case.get("input"),
            "bad_output": "根据本地证据评分未达标；详见失败维度：" + "、".join(run.get("failed_dimensions") or []),
            "expected_output": case.get("expected"),
            "lesson": run.get("diagnosis"),
            "patch_hint": run.get("recommended_patch"),
            "status": "pending",
            "run_id": run.get("id"),
        }

    def capability_ranking(self, runs=None):
        state = self._state()
        runs = runs if runs is not None else state.get("runs") or []
        grouped = defaultdict(list)
        for run in runs:
            if run.get("kind") != "case":
                continue
            grouped[run.get("agent") or "未知 Agent"].append(run)
        result = []
        for agent, items in grouped.items():
            total = len(items)
            passed = sum(1 for item in items if item.get("status") == "passed")
            warnings = sum(1 for item in items if item.get("status") == "warning")
            failed = sum(1 for item in items if item.get("status") == "failed")
            avg = self._avg([item.get("score") for item in items])
            result.append({
                "agent": agent,
                "total": total,
                "passed": passed,
                "warnings": warnings,
                "failed": failed,
                "score": avg,
                "success_rate": round(passed / total * 100, 1) if total else 0,
                "latest": items[0].get("created_at") if items else "",
                "most_common_failure": self._most_common_failure(items),
            })
        return sorted(result, key=lambda x: (x["score"], x["success_rate"], x["total"]), reverse=True)

    @staticmethod
    def _most_common_failure(items):
        counter = Counter()
        for item in items:
            counter.update(item.get("failed_dimensions") or [])
        return counter.most_common(1)[0][0] if counter else "暂无"

    def capture_failure_from_text(self, payload):
        state = self._state()
        item = {
            "id": f"sample_{uuid.uuid4().hex[:10]}",
            "created_at": self._now(),
            "agent": str(payload.get("agent") or "未分类 Agent"),
            "scenario": str(payload.get("scenario") or "真实反馈"),
            "user_input": str(payload.get("user_input") or ""),
            "bad_output": str(payload.get("bad_output") or ""),
            "expected_output": str(payload.get("expected_output") or ""),
            "lesson": str(payload.get("lesson") or "真实反馈已沉淀为训练样本，后续回放时检查是否复发。"),
            "patch_hint": str(payload.get("patch_hint") or "补充 Prompt/规则/工具路由 Patch，并加入回归测试。"),
            "status": "pending",
        }
        state["failure_samples"].insert(0, item)
        state["events"].insert(0, {"id": uuid.uuid4().hex[:10], "created_at": self._now(), "action": "sample.capture", "summary": f"新增失败样本：{item['agent']} / {item['scenario']}"})
        self._save_state(state)
        return item

    def _write_suite_report(self, suite, runs):
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"agent-training-lab-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        lines = [
            f"# Agent 训练场评测报告｜{datetime.now().strftime('%Y-%m-%d')}",
            "",
            f"本次回放 {suite['case_count']} 个用例，通过 {suite['passed']} 个，平均分 {suite['score']}。",
            "",
            "## 用例结果",
        ]
        for index, run in enumerate(runs, 1):
            lines.extend([
                f"{index}. {run.get('case_name')}｜{run.get('agent')}｜{run.get('score')}分｜{run.get('status')}",
                f"   {run.get('diagnosis')}",
                "",
            ])
        lines.extend(["## 能力排行榜"])
        for item in self.capability_ranking(runs):
            lines.append(f"- {item['agent']}：{item['score']}分，成功率 {item['success_rate']}%，常见问题：{item['most_common_failure']}")
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        artifact = {}
        try:
            from intelligence_center.artifact_service import ArtifactService
            artifact = ArtifactService(str(self.root)).register(str(path), {
                "title": f"Agent 训练场评测报告｜{datetime.now().strftime('%Y-%m-%d')}",
                "type": "report",
                "source": "agent_training_lab",
                "tags": ["Agent训练场", "评测", "自我进化"],
                "result_summary": f"回放 {suite['case_count']} 个用例，通过 {suite['passed']} 个，平均分 {suite['score']}",
            })
        except Exception:
            pass
        return {"path": str(path), "artifact": artifact}


def mode_safe_required(required):
    high_risk = {"weixin", "wecom_bot", "notification", "scheduler"}
    return bool(set(required or []) & high_risk)
