import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, getaddresses

from bridge.reply import ReplyType
from channel.channel import Channel
from common.log import logger
from config import conf


class EmailChannel(Channel):
    """Send CowAgent task reports and replies through a configured SMTP account."""

    NOT_SUPPORT_REPLYTYPE = [ReplyType.IMAGE, ReplyType.VOICE, ReplyType.VIDEO]

    def startup(self):
        try:
            self._settings()
            self.report_startup_success()
            logger.info("[Email] Outbound SMTP channel ready")
        except Exception as error:
            self.report_startup_error(str(error))
            raise

    def send(self, reply, context):
        settings = self._settings()
        receiver = str(context.get("receiver", "") if context else "").strip() or settings["default_to"]
        recipients = [address for _, address in getaddresses([receiver]) if address]
        if not recipients:
            raise ValueError("邮箱通知缺少有效收件地址，请在任务中填写或配置默认收件人")
        subject = str(context.get("subject", "") if context else "").strip()
        if not subject:
            prefix = settings["subject_prefix"]
            subject = f"{prefix} 自动任务执行报告".strip()
        message = EmailMessage()
        message["From"] = formataddr((settings["from_name"], settings["username"])) if settings["from_name"] else settings["username"]
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        content = str(reply.content)
        message.set_content(content)
        if reply.type == ReplyType.FILE:
            message.set_content(f"任务生成了文件：\n{content}")
        context_ssl = ssl.create_default_context()
        if settings["ssl"]:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=settings["timeout"], context=context_ssl) as server:
                server.login(settings["username"], settings["password"]); server.send_message(message)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=settings["timeout"]) as server:
                if settings["starttls"]: server.starttls(context=context_ssl)
                server.login(settings["username"], settings["password"]); server.send_message(message)

    @staticmethod
    def _settings():
        config = conf()
        host = str(config.get("email_smtp_host") or "").strip(); username = str(config.get("email_smtp_username") or "").strip(); password = str(config.get("email_smtp_password") or "")
        if not host or not username or not password: raise ValueError("请填写 SMTP 服务器、发件账号和密码或授权码")
        port = int(config.get("email_smtp_port") or (465 if config.get("email_smtp_ssl", True) else 587))
        if not 1 <= port <= 65535: raise ValueError("SMTP 端口无效")
        return {"host":host,"port":port,"username":username,"password":password,"ssl":bool(config.get("email_smtp_ssl",True)),
                "starttls":bool(config.get("email_smtp_starttls",False)),"from_name":str(config.get("email_from_name") or "CowAgent"),
                "default_to":str(config.get("email_default_recipient") or ""),"subject_prefix":str(config.get("email_subject_prefix") or "[CowAgent]"),
                "timeout":max(1,min(int(config.get("email_smtp_timeout") or 20),120))}

    def stop(self): logger.info("[Email] Outbound SMTP channel stopped")
