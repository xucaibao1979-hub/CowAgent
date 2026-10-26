"""Read recent mail from local desktop mail clients without loading giant stores."""
from __future__ import annotations

import configparser
import email
import json
import os
import re
from datetime import date, datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult


DEFAULT_IGNORE_TERMS = (
    "bugs@", "courseware next - bug", "bug #", "问题 #", "电子发票", "发票",
    "etl", "running report", "test_connection", "no_reply", "noreply",
    "no-reply", "notification", "newsletter", "订阅", "自动报告", "系统通知",
)
DEFAULT_KEEP_TERMS = (
    "合同", "续费", "付款", "支付", "报价", "采购", "审批", "客户",
    "故障", "投诉", "失败", "紧急", "deadline", "urgent", "asap",
    "权限", "开通", "请确认", "请查收", "用印",
)


class LocalEmailTool(BaseTool):
    name = "local_email"
    description = (
        "Read and search recent email stored by local desktop clients, especially Thunderbird. "
        "Use this first when the user asks for today's/local/Thunderbird mail. It safely parses "
        "large mbox stores incrementally instead of reading INBOX as a normal file. Read-only. "
        "Return a prioritized digest first: ignore low-value system/no-reply/invoice/ETL/bug-tracker notices "
        "unless they contain an actionable assignment, incident, renewal, payment, deadline, or reply request."
    )
    params = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date as YYYY-MM-DD or 'today' (default today)"},
            "date_from": {"type": "string", "description": "Optional start date YYYY-MM-DD for a date range"},
            "date_to": {"type": "string", "description": "Optional end date YYYY-MM-DD for a date range"},
            "folder": {"type": "string", "description": "Folder name, default INBOX"},
            "query": {"type": "string", "description": "Optional sender/subject/body keyword"},
            "unread_only": {"type": "boolean", "description": "Only unread messages"},
            "max_messages": {"type": "integer", "description": "Maximum results, 1-100 (default 30)"},
        },
        "required": [],
    }

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        try:
            start = self._date(args.get("date_from") or args.get("date"))
            end = self._date(args.get("date_to") or args.get("date_from") or args.get("date"))
            if start > end:
                start, end = end, start
            maximum = max(1, min(int(args.get("max_messages") or 30), 100))
            messages, stores = self._read_thunderbird(
                start, end, str(args.get("folder") or "INBOX"),
                str(args.get("query") or "").strip(), bool(args.get("unread_only")), maximum,
            )
            result = {
                "client": "Thunderbird", "date": start.isoformat() if start == end else f"{start.isoformat()}..{end.isoformat()}", "count": len(messages),
                "messages": messages, "stores_checked": stores,
                "digest": self._digest(messages),
                "note": "只读解析；未加载完整邮箱文件，也不会改变邮件已读状态。",
            }
            return ToolResult.success(result, display=self._display(result))
        except Exception as exc:
            return ToolResult.fail(f"本地邮件读取失败：{exc}")

    @staticmethod
    def _date(value: Any) -> date:
        text = str(value or "today").strip().lower()
        return date.today() if text in ("", "today", "今天") else date.fromisoformat(text)

    def _read_thunderbird(self, start: date, end: date, folder: str, query: str, unread: bool, maximum: int):
        roots = self._profile_roots()
        candidates = []
        wanted = folder.lower().strip()
        aliases = {
            "sent": ("sent", "sent messages", "sent items", "已发送", "已发送邮件"),
            "inbox": ("inbox", "收件箱"),
            "drafts": ("drafts", "草稿", "草稿箱"),
        }
        wanted_names = aliases.get(wanted, (wanted,))
        for root in roots:
            for base in (root / "ImapMail", root / "Mail"):
                if not base.exists():
                    continue
                for path in base.rglob("*"):
                    name = path.name.lower()
                    matches_folder = wanted in ("all", "*") or any(
                        name == alias or name.startswith(alias + "-") for alias in wanted_names
                    )
                    if path.is_file() and path.suffix.lower() not in (".msf", ".sqlite", ".json") and matches_folder:
                        candidates.append(path)
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        output = []
        checked = []
        for path in candidates:
            checked.append({"path": str(path), "size": path.stat().st_size})
            output.extend(self._tail_mbox(path, start, end, query, unread, maximum - len(output)))
            if len(output) >= maximum:
                break
        output.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return output[:maximum], checked

    @staticmethod
    def _profile_roots():
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        base = appdata / "Thunderbird"
        ini = base / "profiles.ini"
        roots = []
        if ini.exists():
            cfg = configparser.RawConfigParser(); cfg.read(ini, encoding="utf-8")
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
        return roots

    def _tail_mbox(self, path: Path, start: date, end: date, query: str, unread: bool, maximum: int):
        if maximum <= 0:
            return []
        size = path.stat().st_size
        # Recent mbox messages are appended. Start with 32 MiB and expand up to
        # 256 MiB only when necessary; this keeps multi-GB stores responsive.
        window = min(size, 32 * 1024 * 1024)
        results = []
        while window and window <= min(size, 256 * 1024 * 1024):
            with path.open("rb") as handle:
                handle.seek(max(0, size - window)); blob = handle.read(window)
            if size > window:
                first = blob.find(b"\nFrom ")
                blob = blob[first + 1:] if first >= 0 else b""
            chunks = re.split(br"(?m)^From .*(?:\r?\n)", blob)
            results = []
            for raw in reversed(chunks):
                if not raw.strip():
                    continue
                item = self._parse(raw, start, end, query, unread)
                if item:
                    results.append(item)
                    if len(results) >= maximum:
                        return results
            if window >= size or window >= 256 * 1024 * 1024:
                break
            window = min(size, window * 2)
        return results

    @staticmethod
    def _parse(raw: bytes, start: date, end: date, query: str, unread: bool):
        try:
            msg = email.message_from_bytes(raw)
            dt = parsedate_to_datetime(msg.get("Date", ""))
            if dt is None or not (start <= dt.astimezone().date() <= end):
                return None
            status = msg.get("X-Mozilla-Status", "0000")
            try: is_read = bool(int(status, 16) & 0x0001)
            except ValueError: is_read = False
            if unread and is_read:
                return None
            subject = str(make_header(decode_header(msg.get("Subject", "（无主题）"))))
            sender = str(make_header(decode_header(msg.get("From", ""))))
            body = LocalEmailTool._body(msg)
            if query and query.lower() not in f"{subject} {sender} {body}".lower():
                return None
            cleaned = LocalEmailTool._clean_text(body)
            priority, reason = LocalEmailTool._priority(subject, sender, cleaned)
            return {
                "subject": subject,
                "sender": sender,
                "date": dt.astimezone().isoformat(timespec="seconds"),
                "timestamp": dt.timestamp(),
                "unread": not is_read,
                "summary": cleaned[:500] or "无可读取正文",
                "priority": priority,
                "priority_reason": reason,
                "system_like": LocalEmailTool._is_system_like(subject, sender, cleaned),
                "actionable": LocalEmailTool._is_actionable(subject, sender, cleaned),
            }
        except Exception:
            return None

    @staticmethod
    def _body(msg):
        for part in (msg.walk() if msg.is_multipart() else [msg]):
            if part.get_content_type() not in ("text/plain", "text/html") or "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", text) if part.get_content_type() == "text/html" else text
        return ""

    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r"<style[\s\S]*?</style>", " ", text or "", flags=re.I)
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\{[^{}]{0,80}\}", " ", text)
        text = re.sub(r"\bpadding\s*:\s*[^;]+;|\bmargin\s*:\s*[^;]+;|@media[\s\S]{0,300}", " ", text, flags=re.I)
        text = text.replace("\ufeff", "").replace("\u200b", "")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _is_system_like(subject: str, sender: str, body: str) -> bool:
        text = f"{subject} {sender} {body}".lower()
        patterns = DEFAULT_IGNORE_TERMS + (
            "finish]", "[finish]", "账单", "invoice",
            "自动生成，请勿直接回复", "do not reply", "donotreply", "invoicing@",
            "billing@", "usage summary", "adnotify@", "域账号过期", "密码已过期",
            "weixinmphelper@", "微信团队", "synology", "群晖系统", "it-email@",
        )
        return any(p in text for p in patterns)

    @staticmethod
    def _is_actionable(subject: str, sender: str, body: str) -> bool:
        text = f"{subject} {sender} {body}".lower()
        if "bugs@" in text or "courseware next - bug" in text or "bug #" in text or "问题 #" in text:
            return False
        if "edw_no_reply" in text or "etl" in text or "running report" in text or "test_connection" in text:
            return False
        if ("closed" in text or "变更为 closed" in text) and not any(
            k in text for k in ("reopened", "重新打开", "指派给", "assigned to me", "请确认", "续费", "合同")
        ):
            return False
        keywords = DEFAULT_KEEP_TERMS + ("renewal", "确认", "指派给", "assigned", "reopened", "[fail]", "failed", "error")
        return any(k in text for k in keywords)

    @staticmethod
    def _priority(subject: str, sender: str, body: str):
        text = f"{subject} {sender} {body}".lower()
        if "bugs@" in text or "courseware next - bug" in text or "bug #" in text or "问题 #" in text:
            return "低", "Bug 系统通知"
        if "edw_no_reply" in text or "etl" in text or "running report" in text or "test_connection" in text:
            return "低", "系统运行/ETL 自动通知"
        if any(k in text for k in ("续费", "renewal", "合同", "用印", "报价", "采购", "发票抬头")):
            return "高", "涉及合同/续费/采购事项，需要人工确认"
        if any(k in text for k in ("urgent", "asap", "故障", "投诉", "[fail]", "failed", "error", "失败", "付款", "支付", "续费", "deadline")):
            return "高", "涉及故障、失败、付款/续费或截止风险"
        if any(k in text for k in ("合同", "用印", "审批", "确认", "请查收", "指派给", "assigned", "bug #", "reopened", "权限", "开通")):
            return "中", "涉及合同、确认、权限开通或工作事项"
        if LocalEmailTool._is_system_like(subject, sender, body):
            return "低", "系统/通知类邮件"
        return "普通", "普通邮件"

    @staticmethod
    def _short_title(subject: str) -> str:
        subject = re.sub(r"\s+", " ", subject or "（无主题）").strip()
        subject = re.sub(r"^\[Courseware Next - Bug #(\d+)\]\s*", r"Bug #\1：", subject)
        subject = re.sub(r"^\[BETA\]|\[FINISH\]|\[FAIL\]", "", subject, flags=re.I).strip()
        return subject[:80] + ("…" if len(subject) > 80 else "")

    @staticmethod
    def _business_title(subject: str) -> str:
        subject = LocalEmailTool._short_title(subject)
        subject = re.sub(r"\s+", " ", subject).strip(" -｜|")
        if "tableau" in subject.lower() and "续费" in subject:
            return "Tableau 续费合同确认"
        subject = re.sub(r"^\[.*?\]\s*", "", subject).strip()
        return subject[:38] + ("…" if len(subject) > 38 else "")

    @staticmethod
    def _human_summary(subject: str, sender: str, body: str) -> str:
        text = LocalEmailTool._clean_text(body)
        text = re.sub(r"(?i)\bhi\s+bob[,，]?", "", text).strip()
        text = re.sub(r"(?i)\bthanks?[,\s\S]*$", "", text).strip()
        for token in ("Patricia Sun", "Commercial Specialist", "SoftwareONE China", "上海市长宁区", "[A black", "Description automatically"):
            pos = text.find(token)
            if pos > 0:
                text = text[:pos].strip()
        material = f"{subject} {sender} {text}".lower()
        if "tableau" in material and ("续费" in material or "合同" in material or "用印" in material):
            return "供应商已返回 Tableau 续费双章合同，需要确认合同附件、金额周期和后续付款/归档流程。"
        if "合同" in material or "用印" in material:
            return "邮件涉及合同或盖章文件流转，建议优先确认附件完整性和下一步处理人。"
        if "付款" in material or "支付" in material or "报价" in material:
            return "邮件涉及费用或报价事项，建议确认金额、截止时间和审批流程。"
        if "权限" in material or "开通" in material:
            return "邮件涉及权限或账号开通事项，建议确认影响范围并安排处理。"
        if text:
            text = text[:70] + ("…" if len(text) > 70 else "")
            return text
        return "正文可读信息较少，建议打开原邮件核对附件或上下文。"

    @staticmethod
    def _suggested_action(subject: str, sender: str, body: str) -> str:
        material = f"{subject} {sender} {body}".lower()
        if "tableau" in material and ("续费" in material or "合同" in material):
            return "先查收双章件附件，核对金额/周期/主体；如无误，回复确认收到并推进付款或归档。"
        if "合同" in material or "用印" in material:
            return "确认附件是否齐全，再决定是否回复确认或提交内部审批。"
        if "付款" in material or "支付" in material or "报价" in material:
            return "核对费用与截止时间，必要时发起审批。"
        if "确认" in material or "请查收" in material:
            return "阅读原邮件后回复是否确认收到。"
        return "保留关注即可；如需要，我可以继续帮你生成回复草稿或待办。"

    @staticmethod
    def _digest(messages):
        policy = LocalEmailTool._profile_email_policy()
        ignored = []
        kept = []
        for item in messages:
            item = dict(item)
            text = f"{item.get('subject','')} {item.get('sender','')} {item.get('summary','')}".lower()
            keep_hit = next((term for term in policy["keep_terms"] if term and term.lower() in text), "")
            ignore_hit = next((term for term in policy["ignore_terms"] if term and term.lower() in text), "")
            if keep_hit:
                item["system_like"] = False
                item["actionable"] = True
                if item.get("priority") not in ("高", "中"):
                    item["priority"] = "高"
                    item["priority_reason"] = f"命中用户画像重点词：{keep_hit}"
            elif ignore_hit:
                item["system_like"] = True
                item["actionable"] = False
                item["priority"] = "低"
                item["priority_reason"] = f"命中用户画像忽略词：{ignore_hit}"
            if item.get("system_like") and not item.get("actionable"):
                ignored.append(item)
            else:
                kept.append(item)
        order = {"高": 0, "中": 1, "普通": 2, "低": 3}
        kept.sort(key=lambda x: (order.get(x.get("priority", "普通"), 2), -float(x.get("timestamp") or 0)))
        return {
            "kept_count": len(kept),
            "ignored_count": len(ignored),
            "items": kept,
            "ignored_examples": [LocalEmailTool._short_title(x.get("subject", "")) for x in ignored[:5]],
        }

    @staticmethod
    def _profile_email_policy():
        """Return default + user-profile mail preferences.

        The personal profile is preference data. It can change ranking and
        filtering, but it cannot authorize sending, deleting, or writing.
        """
        ignore_terms = list(DEFAULT_IGNORE_TERMS)
        keep_terms = list(DEFAULT_KEEP_TERMS)
        preference = ""
        try:
            from config import get_data_root
            path = Path(get_data_root()) / "intelligence_center" / "personal_agent_profile.json"
            if path.exists():
                profile = json.loads(path.read_text(encoding="utf-8"))
                preference = str(profile.get("email_preferences") or "")
        except Exception:
            preference = ""
        if preference:
            known_ignore = ("Bug", "bug", "发票", "ETL", "系统", "通知", "no-reply", "noreply", "订阅", "newsletter", "广告", "营销")
            known_keep = ("合同", "续费", "付款", "支付", "报价", "采购", "审批", "客户", "故障", "投诉", "失败", "紧急", "权限", "开通", "确认", "请查收", "用印", "待办")
            for term in known_ignore:
                if term in preference and term.lower() not in [x.lower() for x in ignore_terms]:
                    ignore_terms.append(term)
            for term in known_keep:
                if term in preference and term.lower() not in [x.lower() for x in keep_terms]:
                    keep_terms.append(term)
            for prefix, target in (("忽略", ignore_terms), ("过滤", ignore_terms), ("排除", ignore_terms), ("保留", keep_terms), ("重点", keep_terms), ("优先", keep_terms), ("关注", keep_terms)):
                for match in re.finditer(prefix + r"[:：]?\s*([^。；;\n]+)", preference):
                    for term in re.split(r"[,，、/\s]+", match.group(1)):
                        term = term.strip()
                        if 1 < len(term) <= 24 and term.lower() not in [x.lower() for x in target]:
                            target.append(term)
        return {"ignore_terms": ignore_terms[:80], "keep_terms": keep_terms[:80]}

    @staticmethod
    def _display(result):
        digest = result.get("digest") or {}
        items = digest.get("items") or []
        lines = [
            f"邮件重点汇报｜{result['date']}",
            f"共读取 {result['count']} 封，已忽略系统/低价值通知 {digest.get('ignored_count', 0)} 封，保留重点 {digest.get('kept_count', len(items))} 封。",
            "",
        ]
        if not items:
            lines.append("当前条件下暂无需要优先处理的非系统邮件。")
            return "\n".join(lines)
        groups = [("高", "高优先级"), ("中", "中优先级"), ("普通", "普通关注")]
        for key, title in groups:
            group = [x for x in items if x.get("priority") == key]
            if not group:
                continue
            lines += [f"## {title}", ""]
            for index, item in enumerate(group, 1):
                summary = LocalEmailTool._human_summary(item.get("subject", ""), item.get("sender", ""), item.get("summary", ""))
                lines += [
                    f"{index}. {LocalEmailTool._business_title(item.get('subject', ''))}",
                    f"   重点：{summary}",
                    f"   来源：{item.get('sender', '')}｜时间：{item.get('date', '')}",
                    f"   判断：{item.get('priority_reason', '')}",
                    f"   建议：{LocalEmailTool._suggested_action(item.get('subject', ''), item.get('sender', ''), item.get('summary', ''))}",
                ]
        ignored = digest.get("ignored_examples") or []
        if ignored:
            lines += ["", "## 已忽略系统/低价值邮件", ""]
            lines.append("已过滤 Bug 系统通知、电子发票、ETL 自动报告等；默认不展开原文，避免干扰重点。")
        return "\n".join(lines)
