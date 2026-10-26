import html
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from intelligence_center.safe_network import safe_urlopen
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ExplorationService:
    """Inspect a public page and turn the observed structure into a reviewable workflow draft."""

    def __init__(self, data_dir):
        self.root = Path(data_dir); self.path = self.root / "exploration_drafts.json"

    @staticmethod
    def _now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def analyze(self, url, goal="", notes=""):
        url = str(url or "").strip(); goal = str(goal or "").strip()
        self._validate_public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "CowAgent-Explorer/1.0"})
        with safe_urlopen(request, timeout=15) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            if "html" not in content_type.lower(): raise ValueError("当前仅支持分析公开 HTML 页面")
            raw = response.read(1_500_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        title = self._text(title_match.group(1)) if title_match else urllib.parse.urlparse(url).netloc
        forms = []
        for form in re.findall(r"<form\b.*?</form>", raw, re.I | re.S)[:10]:
            labels = [self._text(x) for x in re.findall(r"<(?:label|button)\b[^>]*>(.*?)</(?:label|button)>", form, re.I | re.S)]
            inputs = re.findall(r"<(?:input|select|textarea)\b[^>]*?(?:name|placeholder)=[\"']([^\"']+)", form, re.I)
            forms.append({"fields": list(dict.fromkeys(inputs))[:20], "labels": [x for x in labels if x][:10]})
        links = []
        for href, text in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", raw, re.I | re.S):
            label = self._text(text)
            if label and not href.lower().startswith(("javascript:", "mailto:")):
                links.append({"label": label[:80], "url": urllib.parse.urljoin(url, href)})
        links = links[:30]
        steps = [{"order": 1, "action": "open", "description": f"打开 {title}", "target": url}]
        if forms: steps.append({"order": 2, "action": "review_form", "description": "识别并填写目标表单（提交前必须确认）", "target": ", ".join(forms[0]["fields"][:6]) or "页面表单"})
        if goal: steps.append({"order": len(steps)+1, "action": "extract", "description": goal, "target": "页面公开内容"})
        steps.append({"order": len(steps)+1, "action": "deliver", "description": "整理结果并保存到产物中心", "target": "CowAgent"})
        draft = {"id": uuid.uuid4().hex[:14], "name": f"复刻：{title}"[:80], "url": url, "page_title": title,
                 "goal": goal, "notes": str(notes or ""), "status": "draft", "created_at": self._now(), "confirmed_at": "",
                 "observations": {"forms": forms, "links": links, "form_count": len(forms), "link_count": len(links)},
                 "steps": steps, "permissions": ["读取公开网页", "保存本地产物"],
                 "risks": (["页面包含表单；任何提交、登录或发送动作都需要人工确认"] if forms else ["仅分析公开页面，不执行外部写入"])}
        data = self._read(); data["drafts"].insert(0, draft); self._write(data); return draft

    def confirm(self, draft_id):
        data = self._read(); draft = next((x for x in data["drafts"] if x["id"] == draft_id), None)
        if not draft: raise KeyError(draft_id)
        draft["status"] = "confirmed"; draft["confirmed_at"] = self._now(); self._write(data)
        return draft

    def list(self): return self._read()["drafts"]

    def delete(self, draft_id):
        data = self._read(); before = len(data["drafts"]); data["drafts"] = [x for x in data["drafts"] if x["id"] != draft_id]
        if len(data["drafts"]) == before: raise KeyError(draft_id)
        self._write(data); return True

    @staticmethod
    def _text(value):
        value = re.sub(r"<[^>]+>", " ", value or ""); return re.sub(r"\s+", " ", html.unescape(value)).strip()

    @staticmethod
    def _validate_public_url(url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname: raise ValueError("请输入有效的 http/https 公开页面地址")
        if parsed.username or parsed.password: raise ValueError("页面地址不能包含账号或密码")
        try: addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))}
        except socket.gaierror as error: raise ValueError("无法解析页面地址") from error
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global: raise ValueError("为保护本机数据，不能探索本地、内网或保留地址")

    def _read(self):
        if not self.path.exists(): return {"version": 1, "drafts": []}
        try: data = json.loads(self.path.read_text(encoding="utf-8")); data.setdefault("drafts", []); return data
        except (OSError, ValueError): return {"version": 1, "drafts": []}

    def _write(self, data):
        self.root.mkdir(parents=True, exist_ok=True); temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"); temp.replace(self.path)
