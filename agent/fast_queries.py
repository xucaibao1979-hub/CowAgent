"""Deterministic low-latency paths for simple, structured queries.

These handlers deliberately cover only high-confidence requests.  Anything
ambiguous falls through to the normal Agent loop, preserving its reasoning,
skills and approval behavior.
"""
from __future__ import annotations

import re
import ast
import operator
import time
import uuid
from datetime import date, timedelta
from datetime import datetime
from typing import Optional


_WEATHER_WORDS = ("天气", "气温", "温度", "下雨", "降雨", "weather", "temperature")
_EMAIL_WORDS = ("邮件", "邮箱", "收件箱", "未读", "新邮件", "email", "mail", "inbox")
_FOREX_WORDS = ("外汇", "汇率", "牌价", "银行买入价", "银行卖出价", "forex", "exchange rate")
_FILE_WORDS = ("文件", "图片", "照片", "文档", "桌面", "下载", "目录", "找一下", "查找", "发给我")
_POLICY_WORDS = ("政策", "财税", "税务", "财政", "税收", "法规", "新规", "补贴", "申报")
_FOREX_CURRENCIES = {
    "美元": ("USD", "美元"), "美金": ("USD", "美元"), "usd": ("USD", "美元"),
    "港币": ("HKD", "港币"), "港元": ("HKD", "港币"), "hkd": ("HKD", "港币"),
    "日元": ("JPY", "日元"), "jpy": ("JPY", "日元"),
    "欧元": ("EUR", "欧元"), "eur": ("EUR", "欧元"),
    "英镑": ("GBP", "英镑"), "gbp": ("GBP", "英镑"),
}
_COMPLEX_WEATHER = (
    "对比", "比较", "分析", "原因", "趋势", "历史", "去年", "气候", "报告",
    "多个", "分别", "以及", "和北京", "与北京", "compare", "analysis", "history",
)
_GREETING_PATTERNS = (
    r"^(你好|您好|hello|hi|嗨|在吗|在不在|早上好|下午好|晚上好)[！!。.\s]*$",
    r"^(谢谢|感谢|thanks|thank you)[！!。.\s]*$",
)

_CAPABILITY_PATTERNS = (
    r"^(你能做什么|你会什么|怎么用|有什么功能|功能介绍|help|what can you do)[？?！!。.\s]*$",
)


def _fast_tool_allowed(tool: str, intent: str) -> bool:
    """Use client-local reliability to avoid repeating recently bad fast paths."""
    try:
        from agent.tools.reliability import ToolReliability
        stats = ToolReliability().scores(intent).get(tool) or ToolReliability().scores().get(tool) or {}
        total = int(stats.get("total") or 0)
        recent_failures = int(stats.get("recent_failures") or 0)
        score = float(stats.get("score") or 1)
        # New tools should still get a chance.  Only cool down paths with a
        # small history and repeated recent failures, then let the normal Agent
        # pick a fallback.
        return not (total >= 3 and recent_failures >= 2 and score < 0.45)
    except Exception:
        return True


def _record_fast_tool(tool: str, intent: str, args: dict, success: bool, result, started: float, run_id: str) -> None:
    try:
        from agent.tools.audit import record_tool_run, classify_error
        record_tool_run(
            tool,
            args,
            bool(success),
            status="success" if success else "fallback",
            result=result,
            execution_time=time.monotonic() - started,
            error_code="" if success else classify_error(result),
            intent=intent,
            run_id=run_id,
        )
    except Exception:
        return


def _run_fast_tool(tool: str, intent: str, args: dict, runner, *, success_check=None):
    if not _fast_tool_allowed(tool, intent):
        return None
    started = time.monotonic()
    run_id = f"web-fast-{uuid.uuid4().hex[:12]}"
    try:
        result = runner()
        success = bool(success_check(result) if success_check else result is not None)
        _record_fast_tool(tool, intent, args, success, result if result is not None else "no_result", started, run_id)
        return result
    except Exception as exc:
        _record_fast_tool(tool, intent, args, False, exc, started, run_id)
        return None


def _weather_request(message: str) -> Optional[dict]:
    text = re.sub(r"\s+", "", str(message or "").strip())
    lowered = text.lower()
    if not text or len(text) > 50 or not any(word in lowered for word in _WEATHER_WORDS):
        return None
    if any(word in lowered for word in _COMPLEX_WEATHER):
        return None

    wanted = "today"
    if "后天" in text:
        wanted = (date.today() + timedelta(days=2)).isoformat()
    elif "明天" in text or "tomorrow" in lowered:
        wanted = "tomorrow"
    elif any(word in text for word in ("未来", "这几天", "一周")) or "forecast" in lowered:
        wanted = "forecast"

    # Chinese weather queries normally put a location immediately next to the
    # date or 天气.  Strip conversational filler and time words, leaving only
    # the place.  Conservative validation prevents whole sentences becoming a
    # geocoding query.
    location = text
    for token in (
        "帮我查询一下", "帮我查一下", "帮我查询", "查询一下", "帮我查", "查一下", "查询", "查看", "看一下", "看看", "请问", "告诉我", "一下",
        "今天", "今日", "明天", "后天", "未来一周", "未来几天", "这几天", "的天气",
        "天气预报", "天气", "气温", "温度", "怎么样", "如何", "情况", "？", "?",
    ):
        location = location.replace(token, "")
    location = location.strip("，,。！!：:的")

    # Small English fallback: "weather in Shanghai today".
    if re.search(r"[a-z]", lowered) and any(x in lowered for x in ("weather", "temperature")):
        match = re.search(r"(?:weather|temperature)(?:in|for)?([a-z\- ]+?)(?:today|tomorrow|forecast|$)", lowered)
        location = (match.group(1).strip() if match else "")

    if not location or len(location) > 20 or any(mark in location for mark in ("、", "/", "和", "与")):
        return None
    return {"location": location, "date": wanted, "days": 3 if wanted == "forecast" else 1}


def _file_request(message: str) -> Optional[dict]:
    text = str(message or "").strip()
    lowered = text.lower()
    if not text or len(text) > 160 or not any(word in lowered for word in _FILE_WORDS):
        return None
    if not any(word in text for word in ("找", "查", "搜索", "在哪里", "发给我", "读取", "打开")):
        return None
    # Keep this conservative: broad "整理文件/删除文件" should go to the normal
    # approval-aware Agent path, not the low-latency read-only shortcut.
    if any(word in text for word in ("删除", "移动", "重命名", "写入", "修改", "整理到", "清理")):
        return None
    query = text
    for token in (
        "帮我", "请", "一下", "找一下", "查找", "查询", "搜索", "看看", "找找", "读取",
        "电脑里的", "电脑上", "本地", "桌面上的", "桌面上", "桌面", "下载里的", "下载",
        "文件", "图片", "照片", "文档", "发给我", "在哪里", "在哪", "的",
    ):
        query = query.replace(token, " ")
    query = re.sub(r"\s+", " ", query).strip(" ，,。?？")
    if not query:
        return None
    file_glob = "*"
    if any(word in text for word in ("图片", "照片", "截图")):
        file_glob = "*.{png,jpg,jpeg,gif,webp,bmp}"
    elif any(word in text for word in ("word", "文档", "doc")):
        file_glob = "*.{doc,docx,pdf,txt,md}"
    return {"query": query[:60], "file_glob": file_glob}


def _try_file_query(message: str) -> Optional[str]:
    args = _file_request(message)
    if not args:
        return None
    try:
        from config import conf
        roots = [str(x).strip() for x in (conf().get("file_search_roots") or []) if str(x).strip()]
    except Exception:
        roots = []
    if not roots:
        return "尚未添加本地文件授权目录。请到设置 → 数据管理 → 本地文件授权，添加只读目录后再查找。"

    try:
        from pathlib import Path
        from agent.tools.search_files.search_files import SearchFiles
        hits = []
        query = args["query"]
        for root in roots[:8]:
            if not Path(root).exists():
                continue
            result = SearchFiles({"cwd": root, "timeout": 4}).execute({
                "pattern": query,
                "target": "files",
                "path": root,
                "file_glob": args.get("file_glob") or "*",
                "ignore_case": True,
                "max_results": 6,
            })
            if result.status == "success":
                for item in (result.result or {}).get("files") or []:
                    if item not in hits:
                        hits.append(item)
            if len(hits) >= 8:
                break
        if not hits:
            return f"没有在已授权目录中找到“{query}”相关文件。可以补充更准确的文件名，或到设置里增加授权目录后重建索引。"
        lines = [f"找到 {len(hits[:8])} 个相关文件："]
        for index, path in enumerate(hits[:8], 1):
            name = Path(path).name
            lines.append(f"{index}. {name}")
            lines.append(f"   位置：{path}")
        if "发给我" in message or "发送" in message:
            lines.append("如需发送文件，请点开确认后再发送；文件发送属于外发动作，需要走发送通道确认。")
        return "\n".join(lines)
    except Exception as exc:
        return f"我已尝试查找本地文件，但本地索引/授权目录暂时不可用：{exc}"


def _try_policy_or_news_hint(message: str) -> Optional[str]:
    """Route policy/news requests away from the slow generic chat path.

    Real web search can take longer and depends on configured providers.  For
    normal chat, the fast path should immediately hand off to the dedicated
    capability instead of starting a long model-only answer.
    """
    text = str(message or "").strip()
    lowered = text.lower()
    if not text or len(text) > 120:
        return None
    if not (any(word in lowered for word in _POLICY_WORDS) or any(word in lowered for word in ("新闻", "热点", "资讯", "news"))):
        return None
    if not any(word in text for word in ("查", "查询", "搜索", "最新", "今天", "最近", "了解", "看看")):
        return None
    return _run_fast_tool(
        "web:policy_news_route",
        "web",
        {"message": text},
        lambda: (
            "我会按“先执行、失败再说明”的策略处理这类信息查询："
            "优先使用常用功能里的网络搜索 Agent / 财税政策追踪，读取已配置来源并生成简短卡片。"
            "如果当前没有搜索厂商或来源不可用，系统会自动尝试本地候选、放宽时间范围或切换来源。"
        ),
    )


def try_fast_query(message: str) -> Optional[str]:
    """Return a ready-to-display answer, or None for the normal Agent path."""
    text = str(message or "").strip()
    lowered = text.lower()

    if len(text) <= 20 and any(re.fullmatch(pattern, text, re.I) for pattern in _GREETING_PATTERNS):
        if any(word in lowered for word in ("谢谢", "感谢", "thanks", "thank you")):
            return "不客气，我在。你直接说要查什么、整理什么或自动做什么就行。"
        return "我在。你可以直接说需求，比如“查今天上海天气”“看今天新邮件”“每天 8 点发 AI 早报”。"

    if len(text) <= 30 and any(re.fullmatch(pattern, text, re.I) for pattern in _CAPABILITY_PATTERNS):
        return (
            "我可以帮你做几类事：\n"
            "1. 快速问答：天气、时间、计算、简单查询。\n"
            "2. 自动化：定时简报、网站监控、外汇查询、任务提醒。\n"
            "3. 个人资料：读取已授权文件、邮件、微信消息并整理。\n"
            "4. 专业任务：深度研究、代码助手、IT 运维、MCP/CLI 工具调用。\n"
            "你不用选功能，直接描述目标，我会先尝试执行；缺配置时再告诉你怎么补。"
        )

    # Local date/time needs neither a model nor a network request.
    compact = re.sub(r"\s+", "", text)
    if len(compact) <= 24 and re.fullmatch(r"(?:现在|当前|今天)?(?:是)?(?:几点|什么时间|日期|几号|星期几)[？?]?", compact):
        now = datetime.now().astimezone()
        weekdays = "一二三四五六日"
        return f"现在是 {now:%Y年%m月%d日 %H:%M:%S}，星期{weekdays[now.weekday()]}。"

    calculated = _simple_calculation(text)
    if calculated is not None:
        return _run_fast_tool("web:calculator_fast_query", "calculation", {"message": text}, lambda: calculated)

    if _email_request(text):
        email_response = _run_fast_tool(
            "web:email_fast_query",
            "email",
            {"message": text},
            lambda: _try_email_query(text),
            success_check=lambda value: bool(value) and "还没有拿到可用邮件结果" not in str(value),
        )
        if email_response is not None:
            return email_response

    if _file_request(text):
        file_response = _run_fast_tool(
            "web:file_fast_query",
            "file",
            {"message": text},
            lambda: _try_file_query(text),
            success_check=lambda value: bool(value) and "尚未添加本地文件授权目录" not in str(value),
        )
        if file_response is not None:
            return file_response

    if any(word in lowered for word in _FOREX_WORDS):
        forex_response = _run_fast_tool(
            "web:forex_fast_query",
            "forex",
            {"message": text},
            lambda: _try_forex_query(text),
            success_check=lambda value: bool(value) and "当前数据源暂时不可用" not in str(value),
        )
        if forex_response is not None:
            return forex_response

    return try_weather_fast_query(message)


def try_weather_fast_query(message: str, *, ignore_reliability: bool = False) -> Optional[str]:
    """Return weather result for high-confidence weather requests.

    ``ignore_reliability`` is reserved for final recovery paths.  If the normal
    skill/model path has already failed with a generic apology, the weather
    fast query must still be allowed to run even when historical reliability
    cooling would otherwise skip it.
    """
    args = _weather_request(message)
    if not args:
        return None

    def run_weather():
        from agent.tools.weather.weather import WeatherTool
        result = WeatherTool().execute(args)
        if result.status != "success":
            # Let the normal Agent use search or another source on service failure.
            return None
        return result.display or str(result.result)

    if ignore_reliability:
        started = time.monotonic()
        run_id = f"web-fast-force-{uuid.uuid4().hex[:12]}"
        try:
            result = run_weather()
            success = bool(result)
            _record_fast_tool(
                "web:weather_fast_query",
                "weather",
                args,
                success,
                result if result is not None else "no_result",
                started,
                run_id,
            )
            return result
        except Exception as exc:
            _record_fast_tool("web:weather_fast_query", "weather", args, False, exc, started, run_id)
            return None

    return _run_fast_tool("web:weather_fast_query", "weather", args, run_weather)


def _try_forex_query(message: str) -> Optional[str]:
    text = str(message or "").strip()
    lowered = text.lower()
    if not text or len(text) > 80 or not any(word in lowered for word in _FOREX_WORDS):
        return None
    if any(word in text for word in ("走势", "趋势", "预测", "分析", "为什么", "原因")):
        return None
    currencies = []
    for token, (code, name) in _FOREX_CURRENCIES.items():
        if token in lowered and code not in {x["code"] for x in currencies}:
            currencies.append({"code": code, "name": name})
    if not currencies:
        currencies = [{"code": x, "name": name} for x, name in (("USD", "美元"), ("HKD", "港币"), ("JPY", "日元"), ("EUR", "欧元"))]
    try:
        from pathlib import Path
        from config import get_data_root
        from plugins.intelligence_center.forex_query import ForexQueryService
        service = ForexQueryService(Path(get_data_root()) / "intelligence_center")
        result = service.query_once(currencies=currencies)
        rows = result.get("rates") or []
        if not rows:
            return "当前没有读取到外汇牌价结果，请稍后再试。"
        lines = [f"外汇查询｜{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}"]
        for row in rows:
            lines.append(f"- {row.get('currency')}：银行买入价 {row.get('buy')}，银行卖出价 {row.get('sell')}，发布时间 {row.get('published_at')}")
        lines.append("数据源：工商银行。汇率仅供参考，实际成交价以银行交易结果为准。")
        return "\n".join(lines)
    except Exception as exc:
        return f"我已尝试查询外汇牌价，但当前数据源暂时不可用：{exc}"


def _email_request(message: str) -> Optional[dict]:
    text = str(message or "").strip()
    lowered = text.lower()
    if not text or len(text) > 240 or not any(word in lowered for word in _EMAIL_WORDS):
        return None
    if not re.search(r"(看|查|查询|读取|整理|有没有|新|未读|today|今日|今天|邮件|邮箱|mail|email|inbox)", lowered, re.I):
        return None
    query = ""
    keyword = re.search(r"(?:关于|包含|关键词|搜索)\s*[：:]?\s*([^，。；;]{1,30})", text)
    if keyword:
        query = keyword.group(1).strip()
    maximum = 10
    count = re.search(r"(\d{1,2})\s*(?:封|条|个)", text)
    if count:
        maximum = max(1, min(int(count.group(1)), 30))
    args = {
        "date": "today",
        "folder": "INBOX",
        "query": query,
        "unread_only": bool(any(x in text for x in ("新邮件", "未读", "新的")) or "unread" in lowered),
        "max_messages": maximum,
        "briefing": bool(any(x in text for x in ("整理", "汇报", "重点", "重要", "优先级", "分析", "总结"))),
        "strict_ignore_system": bool(
            any(x in text for x in ("系统邮件忽略", "忽略系统邮件", "系统类忽略", "系统通知忽略"))
            or ("系统" in text and "忽略" in text)
        ),
    }
    explicit_range = _extract_email_date_range(text)
    if explicit_range:
        args["date_from"], args["date_to"] = explicit_range
        args.pop("date", None)
        args["max_messages"] = max(maximum, 50)
    elif any(x in text for x in ("这两天", "最近两天", "近两天", "两天", "2天")):
        args["date_from"] = (date.today() - timedelta(days=1)).isoformat()
        args["date_to"] = date.today().isoformat()
        args.pop("date", None)
        args["max_messages"] = max(maximum, 50)
    elif any(x in text for x in ("这周", "本周", "这一周", "最近一周", "近一周", "这个星期", "这一星期", "本星期")):
        args["date_from"] = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        args["date_to"] = date.today().isoformat()
        args.pop("date", None)
        args["max_messages"] = max(maximum, 80)
    return args


def _extract_email_date_range(text: str) -> Optional[tuple[str, str]]:
    current_year = date.today().year

    def normalize(raw: str) -> Optional[str]:
        value = str(raw or "").strip()
        m = re.fullmatch(r"(?:(\d{4})[./年-])?\s*(\d{1,2})[./月-]\s*(\d{1,2})\s*(?:日|号)?", value)
        if not m:
            return None
        year = int(m.group(1) or current_year)
        month = int(m.group(2))
        day = int(m.group(3))
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    m = re.search(
        r"((?:\d{4}[./年-])?\s*\d{1,2}[./月-]\s*\d{1,2}\s*(?:日|号)?)\s*(?:-|~|—|至|到|～)\s*((?:\d{4}[./年-])?\s*\d{1,2}[./月-]\s*\d{1,2}\s*(?:日|号)?)",
        text,
    )
    if not m:
        return None
    start = normalize(m.group(1))
    end = normalize(m.group(2))
    if not start or not end:
        return None
    if start > end:
        start, end = end, start
    return start, end


def _try_email_query(message: str) -> Optional[str]:
    args = _email_request(message)
    if not args:
        return None

    attempts = []

    # 1) Try local Thunderbird / local mailbox files first.
    # The user expects the Agent to work from this computer's existing mail data
    # before asking for online IMAP configuration.
    try:
        from agent.tools.local_email.local_email import LocalEmailTool
        result = LocalEmailTool().execute(args)
        if result.status == "success":
            report = _format_local_email_result(result.result, args)
            _save_email_query_artifact(report, result.result.get("client", "本地邮箱") if isinstance(result.result, dict) else "本地邮箱", message, (result.result or {}).get("messages") if isinstance(result.result, dict) else [])
            return report
        raise RuntimeError(result.error or "本地邮箱读取失败")
    except Exception as exc:
        attempts.append(f"本地邮箱读取失败：{exc}")

    # 2) Then try online IMAP if the user has configured it.
    try:
        from config import conf, get_data_root
        settings = dict(conf().get("email_secretary") or {})
        if settings.get("imap_host") and settings.get("username") and settings.get("password"):
            from pathlib import Path
            from plugins.intelligence_center.email_secretary import EmailSecretaryService
            service = EmailSecretaryService(Path(get_data_root()) / "intelligence_center", settings)
            task = {
                "folder": args["folder"],
                "unread_only": args["unread_only"],
                "max_messages": args["max_messages"],
            }
            messages = service._receive(task)
            messages = _filter_email_messages_by_date(messages, args)
            report = _format_email_messages("在线邮箱", messages, args)
            _save_email_query_artifact(report, "在线邮箱", message, messages)
            return report
    except Exception as exc:
        attempts.append(f"在线邮箱读取失败：{exc}")

    details = "；".join(x for x in attempts if x) or "没有可用邮箱来源"
    return (
        "我已尝试读取邮件，但这台电脑还没有拿到可用邮件结果。\n"
        f"原因：{details}\n"
        "可选修复：在 CowAgent 电脑端配置一种邮箱来源："
        "在线邮箱填 IMAP 地址、账号和授权码；本地邮箱请安装并登录 Thunderbird。"
    )


def _save_email_query_artifact(report: str, source: str, query: str, messages: list) -> None:
    """Save useful email query output to Artifact Center without blocking chat."""
    if not report or "当前没有需要优先处理" in report or "暂未找到符合条件" in report:
        return
    try:
        from config import get_data_root
        from intelligence_center.artifact_service import ArtifactService
        from pathlib import Path
        root = Path(get_data_root()) / "intelligence_center"
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"email-query-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(report, encoding="utf-8")
        ArtifactService(str(root)).register(str(path), {
            "title": "邮件重点汇报",
            "kind": "email",
            "kind_label": "邮件整理",
            "tags": ["邮件", "会话", "重点汇报"],
            "source": "email_fast_query",
            "source_label": source,
            "source_entry": query[:180],
            "metadata": {"message_count": len(messages or [])},
        })
    except Exception:
        pass


def _format_email_messages(source: str, messages: list, args: dict) -> str:
    if not messages:
        return f"{source}今天暂未找到符合条件的{'未读' if args.get('unread_only') else ''}邮件。"
    try:
        from agent.tools.local_email.local_email import LocalEmailTool
        normalized = []
        for item in messages[: int(args.get("max_messages") or 10)]:
            subject = str(item.get("subject") or "（无主题）").strip()
            sender = str(item.get("sender") or "未知发件人").strip()
            summary = LocalEmailTool._clean_text(str(item.get("summary") or item.get("body") or ""))
            priority, reason = LocalEmailTool._priority(subject, sender, summary)
            normalized.append({
                **item,
                "subject": subject,
                "sender": sender,
                "summary": summary[:500] or "无可读取正文",
                "priority": priority,
                "priority_reason": reason,
                "system_like": LocalEmailTool._is_system_like(subject, sender, summary),
                "actionable": LocalEmailTool._is_actionable(subject, sender, summary),
            })
        digest = LocalEmailTool._digest(normalized)
        if args.get("strict_ignore_system"):
            digest = _strict_ignore_system_digest(digest)
        date_label = args.get("date") or f"{args.get('date_from')}..{args.get('date_to')}"
        result = {"client": source, "date": date_label, "count": len(messages), "digest": digest}
        if args.get("briefing"):
            return _format_email_briefing(result)
        return LocalEmailTool._display(result)
    except Exception:
        pass
    lines = [f"{source}邮件重点汇报："]
    lines.append("当前条件下暂无需要优先处理的非系统邮件。")
    return "\n".join(lines)


def _format_local_email_result(data: dict, args: dict) -> str:
    if isinstance(data, dict) and data.get("digest"):
        result = {
            "client": data.get("client") or "本地邮箱",
            "date": data.get("date") or args.get("date") or f"{args.get('date_from')}..{args.get('date_to')}",
            "count": data.get("count", len(data.get("messages") or [])),
            "digest": data.get("digest") or {},
        }
        if args.get("strict_ignore_system"):
            result["digest"] = _strict_ignore_system_digest(result["digest"])
        if args.get("briefing"):
            return _format_email_briefing(result)
        from agent.tools.local_email.local_email import LocalEmailTool
        return LocalEmailTool._display(result)
    messages = data.get("messages") if isinstance(data, dict) else []
    source = data.get("client") or "本地邮箱"
    return _format_email_messages(source, messages or [], args)


def _strict_ignore_system_digest(digest: dict) -> dict:
    items = list((digest or {}).get("items") or [])
    ignored_examples = list((digest or {}).get("ignored_examples") or [])
    kept = []
    moved = []
    for item in items:
        if item.get("system_like"):
            moved.append(item)
        else:
            kept.append(item)
    try:
        from agent.tools.local_email.local_email import LocalEmailTool
        ignored_examples.extend(LocalEmailTool._short_title(x.get("subject", "")) for x in moved[:5])
    except Exception:
        ignored_examples.extend(str(x.get("subject", ""))[:80] for x in moved[:5])
    return {
        **(digest or {}),
        "items": kept,
        "kept_count": len(kept),
        "ignored_count": int((digest or {}).get("ignored_count") or 0) + len(moved),
        "ignored_examples": ignored_examples[:8],
    }


def _format_email_briefing(result: dict) -> str:
    digest = result.get("digest") or {}
    items = digest.get("items") or []
    ignored_count = int(digest.get("ignored_count") or 0)
    kept_count = int(digest.get("kept_count") or len(items))
    lines = [
        f"邮件重点汇报｜{result.get('date') or '今天'}",
        f"已过滤系统/低价值邮件 {ignored_count} 封，保留需要关注邮件 {kept_count} 封。",
        "",
    ]
    if not items:
        lines.append("当前没有需要优先处理的非系统邮件。")
        return "\n".join(lines)

    high = [x for x in items if x.get("priority") == "高"]
    medium = [x for x in items if x.get("priority") == "中"]
    normal = [x for x in items if x.get("priority") not in ("高", "中")]

    lines.append("一、需要优先处理")
    focus = high or medium or normal
    for idx, item in enumerate(focus[:5], 1):
        lines.extend(_email_briefing_item(idx, item))

    if normal and (high or medium):
        lines.extend(["", "二、普通关注"])
        for idx, item in enumerate(normal[:5], 1):
            lines.extend(_email_briefing_item(idx, item))

    ignored = digest.get("ignored_examples") or []
    if ignored:
        lines.extend(["", "三、已忽略系统/低价值邮件"])
        lines.append("已过滤 Bug 系统通知、电子发票、ETL 自动报告等；默认不展开，避免干扰重点。")

    lines.extend(["", "建议动作"])
    actions = _email_briefing_actions(items)
    lines.extend([f"- {x}" for x in actions])
    return "\n".join(lines)


def _email_briefing_item(index: int, item: dict) -> list[str]:
    subject = str(item.get("subject") or "（无主题）").strip()
    try:
        from agent.tools.local_email.local_email import LocalEmailTool
        title = LocalEmailTool._business_title(subject)
    except Exception:
        title = re.sub(r"\s+", " ", subject)
        title = re.sub(r"\s*｜.*$", "", title).strip() or title
    sender = str(item.get("sender") or "未知发件人").strip()
    summary = re.sub(r"\s+", " ", str(item.get("summary") or "无可读取正文")).strip()
    try:
        from agent.tools.local_email.local_email import LocalEmailTool
        summary = LocalEmailTool._human_summary(subject, sender, summary)
        action = LocalEmailTool._suggested_action(subject, sender, str(item.get("summary") or ""))
    except Exception:
        summary = _trim_email_summary(summary)
        action = "打开原邮件确认附件、截止时间和是否需要回复。"
    try:
        from agent.output_compactor import compact_text
        summary = compact_text(summary, 30, 80)
    except Exception:
        summary = _trim_email_summary(summary)
    reason = str(item.get("priority_reason") or "需要关注").replace("故障、失败、", "")
    return [
        f"{index}. {title}",
        f"   重点：{summary}",
        f"   来源：{sender}",
        f"   判断：{reason}",
        f"   建议：{action}",
    ]


def _trim_email_summary(text: str) -> str:
    if not text:
        return "无可读取正文"
    text = text.replace("\ufeff", "").replace("\u200b", "")
    cut_tokens = ("Commercial Specialist", "上海市", "[A black", "Description automatically", "Previous results")
    for token in cut_tokens:
        pos = text.find(token)
        if pos > 0:
            text = text[:pos].strip()
    text = text.replace("Hi Bob,", "").replace("Hi Bob，", "").strip()
    return text[:80] + ("…" if len(text) > 80 else "")


def _email_briefing_actions(items: list) -> list[str]:
    actions = []
    joined = " ".join(str(x.get("subject", "")) + " " + str(x.get("summary", "")) for x in items).lower()
    if "tableau" in joined or "续费" in joined or "合同" in joined:
        actions.append("优先查看 Tableau 续费双章合同，确认合同文件是否完整、金额/周期是否无误。")
        actions.append("如需继续推进，可让 Agent 帮你起草确认收到合同的回复。")
    if not actions:
        actions.append("先处理高优先级邮件；普通通知暂不需要动作。")
    return actions


def _read_thunderbird_fast(args: dict) -> dict:
    import configparser
    import email
    import os
    from email.header import decode_header, make_header
    from email.utils import parsedate_to_datetime
    from pathlib import Path

    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    base = appdata / "Thunderbird"
    roots = []
    ini = base / "profiles.ini"
    if ini.exists():
        cfg = configparser.RawConfigParser()
        cfg.read(ini, encoding="utf-8")
        for section in cfg.sections():
            if not section.lower().startswith("profile"):
                continue
            raw = cfg.get(section, "Path", fallback="").replace("/", os.sep)
            if raw:
                roots.append((base / raw) if cfg.getboolean(section, "IsRelative", fallback=True) else Path(raw))
    if not roots and (base / "Profiles").exists():
        roots = [p for p in (base / "Profiles").iterdir() if p.is_dir()]
    if not roots:
        raise FileNotFoundError("未找到 Thunderbird 用户配置")

    wanted = str(args.get("folder") or "INBOX").lower().strip()
    wanted_names = ("inbox", "收件箱") if wanted == "inbox" else (wanted,)
    candidates = []
    for root in roots:
        for store_root in (root / "ImapMail", root / "Mail"):
            if not store_root.exists():
                continue
            for path in store_root.rglob("*"):
                name = path.name.lower()
                if path.is_file() and path.suffix.lower() not in (".msf", ".sqlite", ".json") and any(
                    name == alias or name.startswith(alias + "-") for alias in wanted_names
                ):
                    candidates.append(path)
    if not candidates:
        raise FileNotFoundError("未找到 Thunderbird 收件箱文件")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    maximum = int(args.get("max_messages") or 10)
    target_day = date.today()
    query = str(args.get("query") or "").strip().lower()
    unread_only = bool(args.get("unread_only"))
    messages = []

    def body_of(msg):
        for part in (msg.walk() if msg.is_multipart() else [msg]):
            if part.get_content_type() not in ("text/plain", "text/html"):
                continue
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", text) if part.get_content_type() == "text/html" else text
        return ""

    for path in candidates[:8]:
        size = path.stat().st_size
        window = min(size, 32 * 1024 * 1024)
        with path.open("rb") as handle:
            handle.seek(max(0, size - window))
            blob = handle.read(window)
        if size > window:
            first = blob.find(b"\nFrom ")
            blob = blob[first + 1:] if first >= 0 else b""
        chunks = re.split(br"(?m)^From .*(?:\r?\n)", blob)
        for raw in reversed(chunks):
            if not raw.strip():
                continue
            try:
                msg = email.message_from_bytes(raw)
                dt = parsedate_to_datetime(msg.get("Date", ""))
                if not dt or dt.astimezone().date() != target_day:
                    continue
                status = msg.get("X-Mozilla-Status", "0000")
                try:
                    is_read = bool(int(status, 16) & 0x0001)
                except ValueError:
                    is_read = False
                if unread_only and is_read:
                    continue
                subject = str(make_header(decode_header(msg.get("Subject", "（无主题）"))))
                sender = str(make_header(decode_header(msg.get("From", ""))))
                body = body_of(msg)
                if query and query not in f"{subject} {sender} {body}".lower():
                    continue
                messages.append({
                    "subject": subject,
                    "sender": sender,
                    "date": dt.astimezone().isoformat(timespec="seconds"),
                    "summary": re.sub(r"\s+", " ", body).strip()[:280] or "无可读取正文",
                })
                if len(messages) >= maximum:
                    return {"client": "Thunderbird", "messages": messages}
            except Exception:
                continue
    return {"client": "Thunderbird", "messages": messages}


def _filter_email_messages_by_date(messages: list, args: dict) -> list:
    if args.get("date") != "today":
        return messages
    from email.utils import parsedate_to_datetime
    today = date.today()
    output = []
    for item in messages or []:
        raw = str(item.get("date") or "").strip()
        if not raw:
            output.append(item)
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt and dt.astimezone().date() == today:
                output.append(item)
        except Exception:
            output.append(item)
    return output


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.UAdd: operator.pos, ast.USub: operator.neg,
}


def _eval_number(node):
    if isinstance(node, ast.Expression):
        return _eval_number(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_number(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval_number(node.left), _eval_number(node.right)
        if type(node.op) is ast.Pow and abs(right) > 12:
            raise ValueError("exponent too large")
        value = _OPS[type(node.op)](left, right)
        if abs(value) > 1e100:
            raise ValueError("result too large")
        return value
    raise ValueError("unsupported expression")


def _simple_calculation(message: str) -> Optional[str]:
    text = str(message or "").strip().lower()
    if len(text) > 80:
        return None
    expression = text
    for token in ("帮我算一下", "帮我计算", "计算一下", "计算", "请问", "等于多少", "是多少", "结果", "？", "?"):
        expression = expression.replace(token, "")
    expression = expression.replace("×", "*").replace("÷", "/").replace("^", "**")
    expression = re.sub(r"\s+", "", expression)
    if not expression or not re.fullmatch(r"[0-9+\-*/().%]+", expression):
        return None
    try:
        value = _eval_number(ast.parse(expression, mode="eval"))
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, float):
        value = round(value, 10)
    return f"{expression} = {value}"
