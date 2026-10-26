import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from bridge.reply import ReplyType
from channel.channel import Channel
from common.log import logger
from config import conf


class WebhookChannel(Channel):
    """Deliver CowAgent replies to a user-configured HTTP(S) endpoint."""

    NOT_SUPPORT_REPLYTYPE = []

    def startup(self):
        try:
            self._validate_config()
            self.report_startup_success()
            logger.info("[Webhook] Outbound channel ready")
        except Exception as error:
            self.report_startup_error(str(error))
            raise

    def send(self, reply, context):
        url, secret, timeout = self._validate_config(
            str(context.get("webhook_url") or "") if context else ""
        )
        content = reply.content
        if reply.type in (ReplyType.IMAGE, ReplyType.VOICE, ReplyType.VIDEO, ReplyType.FILE):
            content = str(content)
        payload = {
            "event": "cowagent.reply",
            "channel": "webhook",
            "reply_type": str(reply.type),
            "content": content,
            "receiver": context.get("receiver", "") if context else "",
            "timestamp": int(time.time()),
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "CowAgent-Webhook/1.0",
        }
        if secret:
            headers["X-CowAgent-Signature"] = "sha256=" + hmac.new(
                secret.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
        request = urllib.request.Request(url, data=raw, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"Webhook 返回 HTTP {response.status}")
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Webhook 返回 HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Webhook 连接失败：{error.reason}") from error

    def _validate_config(self, url_override=""):
        url = str(url_override or conf().get("webhook_url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Webhook URL 必须是有效的 HTTP 或 HTTPS 地址")
        timeout = max(1, min(int(conf().get("webhook_timeout", 15)), 120))
        return url, str(conf().get("webhook_secret") or ""), timeout

    def stop(self):
        logger.info("[Webhook] Outbound channel stopped")
