"""Direct WeChat task execution for common safe requests.

The WeChat channel should feel action-first: try local configured capabilities
before falling back to the general model or giving setup instructions.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from common.log import logger
from config import conf, get_data_root


EMAIL_TERMS = ("邮件", "邮箱", "新邮件", "未读邮件", "今天邮件")
NEWS_TERMS = ("新闻", "热点", "资讯", "财税", "税务", "财政", "政策", "早报", "简报")


def maybe_handle_weixin_task_first(command: str) -> Optional[str]:
    """Return a direct reply when a safe common WeChat request can be executed.

    Returning ``None`` keeps the original full Agent path.
    """
    text = str(command or "").strip()
    if not text:
        return None
    strategy = _learning_strategy(text)
    split = _split_email_news_request(text)
    if split:
        email_text, news_text = split
        email_result = _handle_email(email_text, strategy)
        news_result = _handle_news_or_policy(news_text, strategy)
        return f"【邮件】\n{email_result}\n\n【新闻】\n{news_result}"
    if _looks_like_email(text):
        return _handle_email(text, strategy)
    if _looks_like_news_or_policy(text):
        return _handle_news_or_policy(text, strategy)
    return None


def _split_email_news_request(text: str) -> Optional[Tuple[str, str]]:
    """Keep independent mail and news clauses from swallowing one another."""
    if "邮件" not in text or not any(word in text for word in ("新闻", "资讯", "热点")):
        return None
    match = re.search(r"^(.*?邮件)(?:[，,、;；]|以及|并且|并|和|与)(.+)$", text)
    if not match or not _looks_like_email(match.group(1)):
        return None
    news_clause = match.group(2).strip()
    if not any(word in news_clause for word in ("新闻", "资讯", "热点")):
        return None
    return match.group(1).strip(), "查" + news_clause


def _looks_like_email(text: str) -> bool:
    return any(term in text for term in EMAIL_TERMS) and any(
        term in text for term in ("看", "查", "读取", "有没有", "整理", "总结", "新")
    )


def _looks_like_news_or_policy(text: str) -> bool:
    if any(term in text for term in ("配置", "怎么设置", "如何设置", "新建任务")):
        return False
    return any(term in text for term in NEWS_TERMS) and any(
        term in text for term in ("查", "查询", "看", "生成", "整理", "最新", "今天", "今日")
    )


def _data_dir() -> str:
    return str(Path(get_data_root()) / "intelligence_center")


def _handle_email(text: str, strategy: Optional[Dict] = None) -> str:
    """Read configured mailbox first; explain only after a real attempt fails."""
    strategy = strategy or {}
    try:
        from agent.fast_queries import _try_email_query

        # Keep WeChat aligned with the normal Agent/Web path: local mailbox
        # files are preferred, then online IMAP if configured.  The previous
        # WeChat-only path jumped straight to EmailSecretaryService and could
        # report "no matching mail" even when the unified local-mail reader
        # had valid results for the same query.
        direct_report = _try_email_query(text)
        if direct_report:
            _save_direct_report("微信邮件快速查看", direct_report, "email", ["微信", "邮件"])
            if not _looks_empty_report(direct_report):
                _record_success(text, "email", "unified_fast_query", "email_task_first", direct_report[:300], strategy)
            return _trim_weixin(direct_report)

        from plugins.intelligence_center.email_secretary import EmailSecretaryService
        from agent.fast_queries import _email_request, _filter_email_messages_by_date, _format_email_messages

        settings = dict(conf().get("email_secretary") or {})
        service = EmailSecretaryService(_data_dir(), settings)
        if (strategy or {}).get("email_precheck") and not _mail_settings_ready(settings):
            _record_failure(text, "email_config", RuntimeError("邮箱配置缺失"), "email_precheck", strategy)
            return "我已先检查邮箱读取能力：当前邮箱配置不完整。请到 Web 端「设置 → 邮箱配置」补充 IMAP/账号/授权码后再发“帮我看今天邮件”。"
        parsed = _email_request(text) or {}
        tasks = service.list_tasks()
        task = _pick_task(tasks, text, default_kind="email")
        # If the user gave a specific date range or analysis rule, run a direct
        # one-off read so the current instruction wins over the saved task.
        has_inline_rules = bool(parsed.get("date_from") or parsed.get("date_to") or parsed.get("strict_ignore_system") or parsed.get("briefing"))
        if task and not has_inline_rules:
            result = service.run(task["id"], trigger="weixin")
            report = _compact_report(result, empty_hint="我已尝试读取邮箱，当前没有匹配邮件。")
            if not _looks_empty_report(report):
                _record_success(text, "email", "configured_task", "email_task_first", report, strategy)
            return report

        # No persistent task yet: still try a one-off safe read if mailbox is configured.
        one_off = {
            "id": "weixin-once",
            "name": "微信邮件快速查看",
            "source_type": "imap" if _mail_settings_ready(settings) else "local",
            "folder": parsed.get("folder") or "INBOX",
            "unread_only": bool(parsed.get("unread_only")),
            "max_messages": int(parsed.get("max_messages") or (10 if ("今天" in text or "今日" in text) else 50)),
            "date_from": parsed.get("date_from"),
            "date_to": parsed.get("date_to"),
            "rules": {},
        }
        service._validate_settings()
        messages = service._receive(one_off)
        messages = _filter_email_messages_by_date(messages, parsed) if parsed else messages
        report = _format_email_messages("在线邮箱", messages, {**parsed, "briefing": True, "strict_ignore_system": True})
        _save_direct_report("微信邮件快速查看", report, "email", ["微信", "邮件"])
        if messages:
            _record_success(text, "email", "one_off_read", "email_task_first", f"读取 {len(messages)} 封", strategy)
            return _trim_weixin(report)
        _record_failure(text, "empty_result", RuntimeError("邮件查询无匹配内容"), "email_task_first", strategy)
        return "我已尝试读取邮箱，当前没有找到今天/未读的匹配邮件。"
    except Exception as error:
        logger.warning(f"[WeixinTaskFirst] email failed: {error}")
        _record_failure(text, "email", error, "email_task_first", strategy)
        return (
            "我先尝试读取邮箱，但没有完成。\n"
            f"原因：{_friendly_error(error)}\n"
            "请到 Web 端「设置 → 邮箱配置」检查 IMAP 服务器、账号和授权码；配置好后微信再发“帮我看今天邮件”即可直接执行。"
        )


def _handle_news_or_policy(text: str, strategy: Optional[Dict] = None) -> str:
    """Run matching automation first; otherwise do a bounded one-off source read."""
    strategy = strategy or {}
    try:
        requested_count = re.search(r"([1-9]\d?|[一二三四五六七八九十])\s*(?:条|则|篇)", text)
        numerals = {value: index for index, value in enumerate("一二三四五六七八九十", 1)}
        count_text = requested_count.group(1) if requested_count else ""
        max_items = max(1, min(int(count_text) if count_text.isdigit() else numerals.get(count_text, 8), 8))
        service, task = _find_matching_news_task(text)
        if service and task and not requested_count and "附原文" not in text:
            result = service.run(task["id"], trigger="weixin")
            report = _compact_report(result, empty_hint="我已执行匹配任务，但当前条件下暂无合格结果。")
            if not _looks_empty_report(report):
                _record_success(text, "news", "configured_task", "news_task_first", report, strategy)
            return report

        from plugins.intelligence_center.news import FISCAL_OFFICIAL_SOURCES, NewsAssistantService
        from intelligence_center.personal_context import google_news_sources, load_personal_context

        news = NewsAssistantService(_data_dir())
        fiscal = any(term in text for term in ("财税", "税务", "财政", "政策", "发票", "税收"))
        week_requested = any(term in text for term in ("最近一周", "近一周", "过去一周", "这一周", "本周", "上周", "最近7天", "最近七天"))
        since_hours = 168 if week_requested else 24
        personal = load_personal_context(_data_dir()) if not fiscal else {}
        keywords = _news_keywords(text, fiscal=fiscal)
        if "我项目相关" in text or "我的项目相关" in text:
            project_topics = [str(x).strip() for x in personal.get("topics") or [] if str(x).strip()]
            if project_topics:
                keywords = project_topics[:4]
            else:
                return "尚未找到你的项目主题。请告诉我项目名称或关键词，我再查询相关的重要新闻。"
        sources = list(FISCAL_OFFICIAL_SOURCES) if fiscal else []
        if not sources:
            domains = personal.get("source_domains") or []
            sources = list(personal.get("source_urls") or []) or google_news_sources(domains, keywords, 8, days=7 if week_requested else 1)
        if not sources:
            # A one-off public news lookup must work before the user configures
            # any RSS feed or personal profile.  The feed itself supplies a
            # bounded search query; preview_sources still checks publication.
            query = "(IT OR 科技 OR 人工智能 OR AI Agent OR 软件开发 OR 云计算)"
            sources = [NewsAssistantService._google_news_url(f"{query} when:{7 if week_requested else 1}d")]
        base_payload = {
            "sources": sources[:12],
            "keywords": keywords,
            "rules": {"include": [], "exclude": ["培训", "招聘"]},
            "include": [],
            "exclude": ["培训", "招聘"],
            "since_hours": since_hours,
            "limit": 80 if not fiscal else max_items,
        }
        preview, recovery_actions = _preview_with_learning(news, base_payload, strategy or {})
        items = preview.get("items") or []
        if not fiscal and any(term in text.lower() for term in ("it", "科技", "技术", "人工智能", "ai")):
            relevant = [item for item in items if re.search(
                r"(?i)(?:\bIT\b|\bAI\b|Agent|人工智能|科技|技术|芯片|软件|云计算|大模型|机器人|网络安全|半导体|开源|开发)",
                str(item.get("title") or ""))]
            items = relevant
            items = _rank_tech_news_items(items)
        if not items and not fiscal:
            # The public search feed can be unavailable by region.  Try two
            # direct publisher feeds before reporting a source outage.
            backup_sources = ["https://www.ithome.com/rss/", "https://blogs.microsoft.com/feed/"]
            backup_payload = {**base_payload, "sources": backup_sources, "include": []}
            backup = news.preview_sources(backup_payload)
            if backup.get("items"):
                preview, items, sources = backup, backup["items"], backup_sources
                recovery_actions.append("公开检索不可用，已改查发布方订阅源")
        if not items:
            _record_failure(text, "empty_result", RuntimeError("当前条件下暂无合格结果"), "news_task_first", strategy)
            return "最近查询范围内没有找到可核实的新闻结果；已尝试公开检索和发布方订阅源。"
        task = {"name": "最新财税政策" if fiscal else "微信快速资讯", "sources": sources, "keywords": keywords, "max_items": min(max_items, len(items)), "rules": {}}
        report = (news._build_report(task, items[:max_items], preview.get("errors") or [], news._ai_overview(task, items[:max_items]))
                  if fiscal else _format_news_quick_result(items[:max_items], since_hours,
                                                           include_links=any(term in text for term in ("附原文", "链接", "网址"))))
        if recovery_actions and fiscal:
            report = report.rstrip() + "\n\n> 已自动自救：" + "；".join(recovery_actions)
        if recovery_actions:
            _record_success(text, "news", "expanded_7d", "news_task_first", "；".join(recovery_actions), strategy)
        else:
            _record_success(text, "news", "direct_preview", "news_task_first", f"命中 {len(items)} 条", strategy)
        _save_direct_report(task["name"], report, "news", ["微信", "财税" if fiscal else "新闻"])
        return _trim_weixin(report)
    except Exception as error:
        logger.warning(f"[WeixinTaskFirst] news/policy failed: {error}")
        _record_failure(text, "news_or_policy", error, "news_task_first", strategy)
        return (
            "我先尝试执行查询，但没有完成。\n"
            f"原因：{_friendly_error(error)}\n"
            "我已记录这次失败。你可以稍后重试，或在 Web 端检查「联网搜索/信息来源/通知通道」配置。"
        )


def _rank_tech_news_items(items: List[Dict]) -> List[Dict]:
    """Favor concrete technology developments over promotion and market chatter."""
    strong = ("发布", "推出", "上线", "更新", "开源", "收购", "漏洞", "安全", "芯片", "半导体", "研究", "实验", "系统", "模型", "产品", "GitHub")
    weak = ("入选", "亮相", "斩获", "榜单", "图谱", "网红", "营销", "股价", "股票", "概念股", "业绩", "暴增", "测评", "评测", "豪赌", "受益", "搞头吗", "进博")

    def score(item: Dict) -> Tuple[int, float]:
        title = str(item.get("title") or "")
        value = 2 * sum(word.lower() in title.lower() for word in strong)
        value -= 3 * sum(word in title for word in weak)
        if any(word.lower() in title.lower() for word in ("OpenAI", "Microsoft", "微软", "英伟达", "NVIDIA", "Google", "谷歌", "GitHub", "华为")):
            value += 2
        try:
            published = parsedate_to_datetime(str(item.get("published_at") or "")).timestamp()
        except (ValueError, TypeError, OverflowError, AttributeError):
            published = 0.0
        return value, published

    return sorted(items, key=score, reverse=True)


def _format_news_quick_result(items: List[Dict], since_hours: int, include_links: bool = False) -> str:
    """Show verified headlines and dates without invented generic summaries."""
    period = "最近一周" if since_hours >= 168 else "最近24小时"
    lines = [f"{period}科技新闻（{len(items)} 条）："]
    for index, item in enumerate(items, 1):
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        published = str(item.get("published_at") or "").strip()
        try:
            published = parsedate_to_datetime(published).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, TypeError, OverflowError, AttributeError):
            published = published[:16]
        link = str(item.get("link") or "").strip()
        lines.append(f"{index}. {title}" + (f"（{published}）" if published else ""))
        if include_links and link:
            lines.append("   " + link)
    return "\n".join(lines)


def _find_matching_news_task(text: str) -> Tuple[object, Optional[Dict]]:
    from plugins.intelligence_center.daily_briefing import DailyBriefingService
    from plugins.intelligence_center.extended_tasks import ExtendedTaskService
    from plugins.intelligence_center.news import NewsAssistantService

    fiscal_intent = any(term in text for term in ("财税", "税务", "财政", "政策", "发票", "税收"))
    fiscal_terms = ("财税", "税务", "财政", "政策", "发票", "税收", "关税", "所得税", "增值税")
    candidates = [
        (NewsAssistantService(_data_dir()), ("news", "新闻", "热点", "财税", "政策", "早报", "简报")),
        (DailyBriefingService(_data_dir()), ("briefing", "日报", "简报", "早报", "情报")),
        (ExtendedTaskService(_data_dir()), ("extended", "行业", "竞品", "动态")),
    ]
    for service, aliases in candidates:
        try:
            tasks = service.list_tasks() if not isinstance(service, ExtendedTaskService) else service.list_tasks("")
        except Exception:
            continue
        matches = []
        for task in tasks:
            name = str(task.get("name") or "")
            haystack = " ".join([
                name,
                " ".join(task.get("keywords") or []),
                " ".join(task.get("official_sources") or []),
                str(task.get("kind") or ""),
            ])
            if fiscal_intent and not any(term in haystack for term in fiscal_terms):
                continue
            if any(token in text for token in aliases) and _task_matches_text(haystack, text):
                matches.append(task)
        task = _pick_task(matches, text)
        if task:
            return service, task
    return None, None


def _task_matches_text(haystack: str, text: str) -> bool:
    compact = re.sub(r"\s+", "", haystack + text)
    key_terms = [term for term in NEWS_TERMS if term in text]
    if any(term in compact for term in key_terms):
        return True
    return any(word and word in haystack for word in re.split(r"[\s，,。；;：:]+", text) if len(word) >= 2)


def _pick_task(tasks: Iterable[Dict], text: str = "", default_kind: str = "") -> Optional[Dict]:
    items = [task for task in tasks if isinstance(task, dict)]
    if not items:
        return None
    enabled = [task for task in items if task.get("enabled", True)]
    pool = enabled or items
    def score(task: Dict) -> tuple:
        name = str(task.get("name") or "")
        hit = 0
        for token in re.split(r"[\s，,。；;：:]+", text):
            token = token.strip()
            if len(token) >= 2 and token in name:
                hit += 3
        if default_kind and str(task.get("task_type") or task.get("kind") or "") == default_kind:
            hit += 1
        stamp = str(task.get("last_run_at") or task.get("updated_at") or task.get("created_at") or "")
        return (hit, stamp)
    return sorted(pool, key=score, reverse=True)[0]


def _news_keywords(text: str, fiscal: bool = False) -> List[str]:
    if fiscal:
        return ["财税政策", "税务政策", "财政政策", "发票", "税收"]
    terms = []
    for token in ("AI", "Agent", "人工智能", "创业", "科技", "国内热点", "行业动态"):
        if token.lower() in text.lower() or token in text:
            terms.append(token)
    if re.search(r"(?<![A-Za-z])IT(?![A-Za-z])", text, re.I):
        terms.extend(["IT", "科技", "人工智能"])
    return terms or ["AI", "科技", "创业"]


def _learning_strategy(text: str) -> Dict:
    try:
        from channel.weixin.failure_learning import learned_weixin_route_strategy
        return learned_weixin_route_strategy(text)
    except Exception as error:
        logger.debug(f"[WeixinTaskFirst] learning strategy skipped: {error}")
        return {}


def _mail_settings_ready(settings: Dict) -> bool:
    required = ("imap_host", "username", "password")
    aliases = {
        "imap_host": ("imap_host", "imap_server", "server", "host"),
        "username": ("username", "user", "account", "email"),
        "password": ("password", "authorization_code", "auth_code"),
    }
    for key in required:
        if not any(str(settings.get(name) or "").strip() for name in aliases[key]):
            return False
    return True


def _preview_with_learning(news, payload: Dict, strategy: Dict) -> Tuple[Dict, List[str]]:
    """Run a one-off news preview and apply learned safe recovery once."""
    recovery_actions: List[str] = []
    if strategy.get("prefer_expanded_news_window"):
        payload = dict(payload)
        payload["since_hours"] = 168
        payload["include"] = []
        payload["keywords"] = list(payload.get("keywords") or [])[:3]
        payload["sources"] = list(payload.get("sources") or [])[:8]
        recovery_actions.append("已复用历史成功路径：优先放宽到最近7天并减少筛选词")
    preview = news.preview_sources(payload)
    if preview.get("items") or not strategy.get("search_recovery"):
        return preview, recovery_actions
    retry_payload = dict(payload)
    retry_payload["since_hours"] = 168
    retry_payload["include"] = []
    retry_payload["keywords"] = list(payload.get("keywords") or [])[:3]
    retry_payload["sources"] = list(payload.get("sources") or [])[:8]
    retry_payload["limit"] = max(int(payload.get("limit") or 8), 10)
    retry = news.preview_sources(retry_payload)
    if retry.get("items"):
        recovery_actions.append("历史空结果较多，已自动放宽到最近7天并减少筛选词")
        return retry, recovery_actions
    combined = dict(preview)
    combined["errors"] = list(preview.get("errors") or []) + list(retry.get("errors") or [])
    return combined, recovery_actions


def _mail_is_on_date(raw_date, day) -> bool:
    try:
        value = parsedate_to_datetime(str(raw_date or ""))
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return value.astimezone().date() == day
    except Exception:
        return str(day) in str(raw_date or "")


def _compact_report(result: Dict, empty_hint: str = "") -> str:
    report = str(result.get("report") or "").strip()
    if not report and result.get("report_path"):
        try:
            report = Path(result["report_path"]).read_text(encoding="utf-8", errors="replace")
        except Exception:
            report = ""
    if not report:
        task = result.get("task") or {}
        report = str(task.get("last_status") or result.get("message") or empty_hint or "已执行，但没有生成可展示内容。")
    if "暂无" in report or "收录 0 条" in report:
        return empty_hint or _trim_weixin(report)
    return _trim_weixin(report)


def _trim_weixin(report: str, limit: int = 3600) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(report or "").strip())
    # WeChat supports markdown poorly; preserve readable labels and avoid long raw diagnostics.
    text = re.sub(r"^## 抓取提示[\s\S]*$", "", text, flags=re.M).strip()
    return text if len(text) <= limit else text[: limit - 20].rstrip() + "\n……"


def _save_direct_report(title: str, report: str, kind: str, tags: List[str]) -> None:
    try:
        root = Path(_data_dir())
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"weixin-{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(report, encoding="utf-8")
        try:
            from intelligence_center.artifact_service import ArtifactService
            ArtifactService(str(root)).register(str(path), {
                "title": title,
                "kind": kind,
                "kind_label": "微信快速任务",
                "tags": tags,
                "source": "weixin_task_first",
            })
        except Exception:
            pass
    except Exception as error:
        logger.warning(f"[WeixinTaskFirst] save direct report failed: {error}")


def _friendly_error(error: Exception) -> str:
    text = str(error or "").strip()
    replacements = {
        "请先填写邮箱服务器、账号和授权码": "邮箱读取配置不完整",
        "timed out": "网络连接超时",
        "Authentication Fails": "模型或通道认证失败",
        "Unauthorized": "认证失败",
    }
    for raw, friendly in replacements.items():
        if raw.lower() in text.lower():
            return friendly
    return text[:120] or "未知错误"


def _looks_empty_report(text: str) -> bool:
    value = str(text or "")
    return any(token in value for token in ("暂无", "无合格", "没有匹配", "无内容", "收录 0 条"))


def _strategy_evidence(strategy: Optional[Dict]) -> str:
    notes = [str(x) for x in ((strategy or {}).get("route_notes") or []) if str(x).strip()]
    return "；".join(notes[:3])


def _record_failure(command: str, category: str, error: Exception, route: str, strategy: Optional[Dict] = None) -> None:
    _record_tool_audit(
        tool=f"weixin:{route or category or 'task_first'}",
        command=command,
        success=False,
        intent=_audit_intent(category, route),
        result=str(error),
        route=route,
    )
    try:
        from channel.weixin.failure_learning import record_weixin_failure
        record_weixin_failure(command=command, category=category, message=str(error), route=route)
    except Exception:
        pass
    try:
        from channel.web.web_channel import MissionControlHandler
        decision = {
            "intent": f"weixin_{category or 'task'}",
            "target": "weixin-assistant",
            "source": "微信助理",
            "source_channel": "weixin",
            "route": route,
            "learning_route": {"hints": (strategy or {}).get("route_notes") or []},
        }
        message = str(error)
        evidence = _strategy_evidence(strategy)
        if evidence:
            message = f"{message}；学习路由：{evidence}"
        MissionControlHandler._record_command_learning(command, decision, "failed", message, source="微信助理", channel="weixin")
    except Exception:
        pass


def _record_success(command: str, task_type: str, action: str, route: str, evidence: str = "", strategy: Optional[Dict] = None) -> None:
    _record_tool_audit(
        tool=f"weixin:{route or task_type or 'task_first'}",
        command=command,
        success=True,
        intent=_audit_intent(task_type, route),
        result=evidence or action,
        route=route,
    )
    try:
        from channel.weixin.failure_learning import record_weixin_success
        record_weixin_success(command=command, task_type=task_type, action=action, route=route, evidence=evidence)
    except Exception:
        pass


def _audit_intent(kind: str = "", route: str = "") -> str:
    text = f"{kind} {route}".lower()
    if "email" in text or "mail" in text or "邮件" in text:
        return "email"
    if "news" in text or "policy" in text or "search" in text or "财税" in text:
        return "web"
    if "file" in text or "文件" in text:
        return "file"
    return "weixin"


def _record_tool_audit(tool: str, command: str, success: bool, intent: str, result: str = "", route: str = "") -> None:
    try:
        from agent.tools.audit import classify_error, record_tool_run
        started = time.perf_counter()
        record_tool_run(
            tool=tool,
            arguments={
                "channel": "weixin",
                "command_preview": str(command or "")[:160],
                "route": route,
            },
            success=bool(success),
            status="success" if success else "error",
            result=str(result or "")[:500],
            execution_time=max(0.001, time.perf_counter() - started),
            error_code="" if success else classify_error(result),
            intent=intent,
            run_id=f"weixin-{uuid.uuid4().hex[:12]}",
        )
    except Exception:
        pass
    try:
        from channel.web.web_channel import MissionControlHandler
        decision = {
            "intent": f"weixin_{task_type or 'task'}",
            "target": "weixin-assistant",
            "source": "微信助理",
            "source_channel": "weixin",
            "route": route,
            "action": action,
            "learning_route": {"hints": (strategy or {}).get("route_notes") or []},
        }
        message = evidence or action or "微信任务执行成功"
        route_evidence = _strategy_evidence(strategy)
        if route_evidence:
            message = f"{message}；学习路由：{route_evidence}"
        MissionControlHandler._record_command_learning(command, decision, "success", message, source="微信助理", channel="weixin")
    except Exception:
        pass
