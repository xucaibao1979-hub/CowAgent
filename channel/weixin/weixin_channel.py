"""
Weixin channel implementation.

Uses HTTP long-poll (getUpdates) to receive messages and sendMessage to reply.
Login via QR code scan through the ilink bot API.
"""

import json
import datetime
import os
import re
import threading
import time
import uuid

import requests

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.weixin.weixin_api import (
    WeixinApi, upload_media_to_cdn,
    DEFAULT_BASE_URL, CDN_BASE_URL,
)
from channel.weixin.weixin_message import WeixinMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from config import conf, get_data_root, get_weixin_credentials_path

MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY = 30
RETRY_DELAY = 2
SESSION_EXPIRED_ERRCODE = -14
TEXT_CHUNK_LIMIT = 4000
# How long a text message will wait for a concurrently-sent image/file to
# finish downloading into the cache before being handled without it.
PENDING_MEDIA_WAIT_S = 3.0
PENDING_MEDIA_POLL_S = 0.1
QR_LOGIN_TIMEOUT_S = 480
QR_MAX_REFRESHES = 10
PROCESSING_NOTICE_DELAY_S = 2.0
PROCESSING_TIMEOUT_NOTICE_S = 25.0


def _load_credentials(cred_path: str) -> dict:
    """Load saved credentials from JSON file."""
    try:
        if os.path.exists(cred_path):
            from common.secure_storage import load_secure_json
            credentials, needs_migration = load_secure_json(cred_path)
            if needs_migration and isinstance(credentials, dict):
                _save_credentials(cred_path, credentials)
                logger.info("[Weixin] Migrated credentials to protected storage")
            return credentials
    except Exception as e:
        logger.warning(f"[Weixin] Failed to load credentials: {e}")
    return {}


def _save_credentials(cred_path: str, data: dict):
    """Atomically save credentials to JSON file (tmp + rename)."""
    from common.secure_storage import save_secure_json
    save_secure_json(cred_path, data)


@singleton
class WeixinChannel(ChatChannel):

    # ilink bot protocol has no outbound voice item; deliver TTS as a file.
    NOT_SUPPORT_REPLYTYPE = []

    LOGIN_STATUS_IDLE = "idle"
    LOGIN_STATUS_WAITING = "waiting_scan"
    LOGIN_STATUS_SCANNED = "scanned"
    LOGIN_STATUS_OK = "logged_in"

    def __init__(self):
        super().__init__()
        self.api = None
        self._stop_event = threading.Event()
        self._poll_thread = None
        # user_id -> context_token. Guarded by _context_tokens_lock for any
        # mutation that races with disk persistence.
        self._context_tokens = {}
        self._context_token_seen_at = {}
        self._context_tokens_lock = threading.Lock()
        self._received_msgs = ExpiredDict(60 * 60 * 7.1)
        # session_id -> deadline(ts) until which an inbound image/file is
        # expected to finish downloading into the file cache. Lets a text
        # message that arrives together with (but slightly ahead of) an image
        # wait for the media path instead of reaching the agent without it.
        self._pending_media = {}
        self._pending_media_lock = threading.Lock()
        # msg_id -> processing notice state.  WeChat replies are produced by the
        # shared async ChatChannel worker, so this local watchdog gives users a
        # visible "I'm working" signal without touching the model pipeline.
        self._processing_notices = {}
        self._processing_notices_lock = threading.Lock()
        self._get_updates_buf = ""
        self._credentials_path = ""
        self.login_status = self.LOGIN_STATUS_IDLE
        self._current_qr_url = ""
        self._last_send_error = ""
        self._last_error = ""
        self._last_error_at = 0
        self._last_poll_at = 0
        self._last_poll_success_at = 0
        self._last_message_at = 0
        self._consecutive_poll_failures = 0
        self._reconnect_lock = threading.Lock()
        self._watchdog_thread = None
        self._last_soft_reconnect_at = 0
        self._last_session_expired_at = 0

        conf()["single_chat_prefix"] = [""]

    # ── Lifecycle ──────────────────────────────────────────────────────

    def startup(self):
        self._stop_event.clear()

        # Build filename metadata in the background.  WeChat file lookups can
        # then return in one local query instead of spending several LLM/tool
        # turns walking the disk on demand.
        try:
            from common.local_file_index import get_local_file_index
            get_local_file_index().start_background_refresh()
        except Exception as e:
            logger.warning(f"[Weixin] Failed to start local file index: {e}")

        base_url = conf().get("weixin_base_url") or DEFAULT_BASE_URL
        cdn_base_url = conf().get("weixin_cdn_base_url", CDN_BASE_URL)
        token = conf().get("weixin_token", "")

        self._credentials_path = get_weixin_credentials_path()

        # Always load credentials so we can restore context_tokens even when
        # the bot token itself comes from config.
        creds = _load_credentials(self._credentials_path)
        owner_id = creds.get("user_id", "")
        if owner_id:
            from channel.channel_access_service import get_channel_access_service
            get_channel_access_service().auto_approve_owner("weixin", owner_id)
        if not token:
            token = creds.get("token", "")
            if creds.get("base_url"):
                base_url = creds["base_url"]

        # Restore persisted context_tokens so scheduler can deliver pushes
        # immediately after restart, without waiting for the user to ping
        # the bot first.
        self._restore_context_tokens_from_creds(creds)

        if not token:
            token, base_url = self._login_with_retry(base_url)
            if not token:
                return

        self.api = WeixinApi(base_url=base_url, token=token, cdn_base_url=cdn_base_url)
        self.login_status = self.LOGIN_STATUS_OK

        logger.info(f"[Weixin] 微信通道已启动，凭证保存在 {self._credentials_path}，"
                     f"如需重新扫码登录请删除该文件后重启")
        self.report_startup_success()
        self._start_connection_watchdog()

        self._poll_loop()

    def _login_with_retry(self, base_url: str) -> tuple:
        """Attempt QR login, then wait for stop if failed.
        Returns (token, base_url) on success, or ("", "") if stopped."""
        logger.info("[Weixin] No token found, starting QR login...")
        self.login_status = self.LOGIN_STATUS_WAITING
        login_result = self._qr_login(base_url)
        if login_result:
            return login_result["token"], login_result.get("base_url", base_url)

        self.login_status = self.LOGIN_STATUS_IDLE
        if not self._stop_event.is_set():
            logger.info("[Weixin] QR login timed out, waiting for stop or reconnect...")
            print("  二维码登录超时，请通过控制台重新接入\n")
            self._stop_event.wait()

        logger.info("[Weixin] Login cancelled by stop event")
        return "", ""

    def stop(self):
        logger.info("[Weixin] stop() called")
        self._stop_event.set()

    def get_health_status(self):
        now = time.time()
        token_count = 0
        with self._context_tokens_lock:
            token_count = len([value for value in self._context_tokens.values() if value])
        poll_stale_seconds = int(conf().get("weixin_poll_stale_seconds") or 900)
        poll_stale_seconds = max(120, poll_stale_seconds)
        stale = (
            self.login_status == self.LOGIN_STATUS_OK
            and self._last_poll_success_at
            and now - self._last_poll_success_at > poll_stale_seconds
        )
        ok = bool(self.api) and self.login_status == self.LOGIN_STATUS_OK and not stale
        state = "stale" if stale else self.login_status
        return {
            "ok": ok,
            "connected": ok,
            "state": state,
            "mode": "long_poll",
            "last_poll_at": self._last_poll_at,
            "last_poll_success_at": self._last_poll_success_at,
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "consecutive_failures": self._consecutive_poll_failures,
            "context_token_count": token_count,
        }

    def reconnect_now(self, reason: str = "") -> bool:
        """Best-effort recovery for the long-poll session without requiring UI interaction."""
        if not self._reconnect_lock.acquire(blocking=False):
            return False
        try:
            logger.warning(f"[Weixin] reconnect_now requested: {reason}")
            self._last_error = str(reason or "manual reconnect")[:500]
            self._last_error_at = time.time()
            self._get_updates_buf = ""
            ok = self._soft_reconnect_saved_token(f"manual reconnect: {reason}")
            if ok:
                self._last_error = ""
                self._consecutive_poll_failures = 0
                return True
            # Do not force QR login from background recovery. A scheduled task
            # should not turn a transient long-poll issue into repeated
            # authorization prompts. If the saved credential is gone, surface
            # the state and wait for the user to rebind from the connection
            # center.
            self.login_status = self.LOGIN_STATUS_IDLE
            return False
        finally:
            self._reconnect_lock.release()

    def _start_connection_watchdog(self):
        """Keep the Weixin channel close to an OpenClaw/Hermes-style session.

        The ilink protocol has two layers: bot login token and per-conversation
        context_token. The watchdog keeps the login token/session alive with
        soft reconnects from persisted credentials, while context_token remains
        cached until the server explicitly rejects it.
        """
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._connection_watchdog_loop,
            name="WeixinConnectionWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _connection_watchdog_loop(self):
        while not self._stop_event.is_set():
            interval = int(conf().get("weixin_keepalive_interval_seconds") or 60)
            interval = max(30, min(interval, 300))
            self._stop_event.wait(interval)
            if self._stop_event.is_set():
                break
            try:
                self._ensure_long_connection_alive()
            except Exception as e:
                logger.warning(f"[Weixin] connection watchdog failed: {e}")

    def _ensure_long_connection_alive(self):
        now = time.time()
        stale_seconds = int(conf().get("weixin_auto_reconnect_stale_seconds") or 360)
        stale_seconds = max(180, stale_seconds)
        missing_api = self.api is None
        idle_with_saved_token = self.login_status in (self.LOGIN_STATUS_IDLE, self.LOGIN_STATUS_WAITING) and self._saved_token_available()
        poll_stale = (
            self.login_status == self.LOGIN_STATUS_OK
            and self._last_poll_success_at
            and now - self._last_poll_success_at > stale_seconds
        )
        too_many_failures = self._consecutive_poll_failures >= MAX_CONSECUTIVE_FAILURES
        if missing_api or idle_with_saved_token or poll_stale or too_many_failures:
            self._soft_reconnect_saved_token(
                "watchdog: "
                + ("missing_api" if missing_api else "idle" if idle_with_saved_token else "stale" if poll_stale else "failures")
            )

    def _saved_token_available(self) -> bool:
        try:
            token = conf().get("weixin_token", "")
            if token:
                return True
            creds = _load_credentials(self._credentials_path or get_weixin_credentials_path())
            return bool(creds.get("token"))
        except Exception:
            return False

    def _soft_reconnect_saved_token(self, reason: str = "") -> bool:
        now = time.time()
        min_interval = int(conf().get("weixin_soft_reconnect_min_interval_seconds") or 60)
        min_interval = max(15, min_interval)
        if now - self._last_soft_reconnect_at < min_interval and self.api and self.login_status == self.LOGIN_STATUS_OK:
            return True
        token = conf().get("weixin_token", "")
        base_url = conf().get("weixin_base_url") or DEFAULT_BASE_URL
        cdn_base_url = conf().get("weixin_cdn_base_url", CDN_BASE_URL)
        try:
            creds = _load_credentials(self._credentials_path or get_weixin_credentials_path())
            if not token:
                token = creds.get("token", "")
            if creds.get("base_url"):
                base_url = creds.get("base_url")
        except Exception:
            pass
        if not token:
            logger.warning(f"[Weixin] soft reconnect skipped, no saved token: {reason}")
            return False
        self.api = WeixinApi(base_url=base_url, token=token, cdn_base_url=cdn_base_url)
        self.login_status = self.LOGIN_STATUS_OK
        self._get_updates_buf = ""
        self._last_soft_reconnect_at = now
        self._last_error = ""
        self._consecutive_poll_failures = 0
        logger.warning(f"[Weixin] soft reconnect with saved token completed: {reason}")
        return True

    def _handle_session_expired(self) -> bool:
        """Handle server-side -14 without repeatedly forcing QR authorization."""
        now = time.time()
        self._last_session_expired_at = now
        if self._soft_reconnect_saved_token("session expired"):
            return True
        if bool(conf().get("weixin_auto_qr_relogin", False)):
            return self._relogin()
        self.login_status = self.LOGIN_STATUS_IDLE
        self._last_error = "微信登录凭证已失效，请在通道与连接里重新扫码"
        self._last_error_at = now
        return False

    def _relogin(self) -> bool:
        """Re-login after session expiry. Returns True on success."""
        base_url = self.api.base_url if self.api else DEFAULT_BASE_URL
        # Keep cached context_tokens across a best-effort reconnect.  Scheduled
        # reminders rely on those per-chat tokens for proactive pushes; if a
        # token is truly invalid, the send API returns -14/-2 and we invalidate
        # only that receiver in _check_send_response().
        self.login_status = self.LOGIN_STATUS_WAITING
        result = self._qr_login(base_url)
        if not result:
            self.login_status = self.LOGIN_STATUS_IDLE
            return False
        self.api = WeixinApi(
            base_url=result.get("base_url", base_url),
            token=result["token"],
            cdn_base_url=self.api.cdn_base_url if self.api else CDN_BASE_URL,
        )
        self.login_status = self.LOGIN_STATUS_OK
        with self._context_tokens_lock:
            self._persist_context_tokens_locked()
        return True

    # ── Context token persistence ──────────────────────────────────────
    # ilink requires every outbound send to echo the context_token from the
    # user's latest inbound message. We mirror the in-memory map into the
    # credentials JSON so scheduled pushes survive process restarts.
    # All mutation + disk IO is serialized via _context_tokens_lock so that
    # concurrent updates can never lose each other's writes.

    def _restore_context_tokens_from_creds(self, creds: dict) -> None:
        if not isinstance(creds, dict):
            return
        tokens = creds.get("context_tokens")
        if not isinstance(tokens, dict):
            return
        seen_at_map = creds.get("context_token_seen_at")
        if not isinstance(seen_at_map, dict):
            seen_at_map = {}
        restored = 0
        with self._context_tokens_lock:
            for user_id, token in tokens.items():
                if isinstance(user_id, str) and isinstance(token, str) and token:
                    self._context_tokens[user_id] = token
                    seen_at = seen_at_map.get(user_id)
                    if isinstance(seen_at, str) and seen_at:
                        self._context_token_seen_at[user_id] = seen_at
                    restored += 1
        if restored:
            logger.info(f"[Weixin] Restored {restored} context_tokens from credentials")

    def _persist_context_tokens_locked(self) -> None:
        """Flush the token map to disk. Caller must hold _context_tokens_lock."""
        if not self._credentials_path:
            return
        try:
            creds = _load_credentials(self._credentials_path) or {}
            creds["context_tokens"] = dict(self._context_tokens)
            creds["context_token_seen_at"] = dict(self._context_token_seen_at)
            _save_credentials(self._credentials_path, creds)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to persist context_tokens: {e}")

    def _update_context_token(self, user_id: str, token: str) -> None:
        """Update the in-memory token for a user; flush to disk only on change."""
        if not user_id or not token:
            return
        with self._context_tokens_lock:
            aliases = self._receiver_aliases(user_id)
            now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            if all(self._context_tokens.get(alias) == token for alias in aliases):
                for alias in aliases:
                    self._context_token_seen_at[alias] = now
                self._persist_context_tokens_locked()
                self._start_pending_push_flush(user_id, token)
                return
            for alias in aliases:
                self._context_tokens[alias] = token
                self._context_token_seen_at[alias] = now
            self._persist_context_tokens_locked()
        self._start_pending_push_flush(user_id, token)

    def _invalidate_context_token(self, user_id: str) -> None:
        """Drop the cached token for a user (used after -14 / send rejection)."""
        if not user_id:
            return
        with self._context_tokens_lock:
            aliases = self._receiver_aliases(user_id)
            removed = False
            for alias in aliases:
                if alias in self._context_tokens:
                    del self._context_tokens[alias]
                    removed = True
                if alias in self._context_token_seen_at:
                    del self._context_token_seen_at[alias]
            if not removed:
                return
            logger.info(f"[Weixin] Invalidated stale context_token for {user_id}")
            self._persist_context_tokens_locked()

    # ── Pending push queue ─────────────────────────────────────────────
    #
    # The ilink Weixin API requires a recent per-conversation context_token for
    # outbound pushes. A long-poll connection can stay online while the cached
    # push token is missing/invalid. To avoid losing scheduled task results, we
    # queue failed proactive pushes and flush them as soon as the user sends any
    # message that refreshes the context_token.

    @staticmethod
    def _pending_push_path() -> str:
        root = os.path.join(get_data_root(), "intelligence_center")
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, "weixin_pending_pushes.json")

    def _read_pending_pushes(self) -> list:
        try:
            path = self._pending_push_path()
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except Exception as error:
            logger.warning(f"[Weixin] Failed to read pending pushes: {error}")
            return []

    def _write_pending_pushes(self, items: list) -> None:
        try:
            path = self._pending_push_path()
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(items[-50:], handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as error:
            logger.warning(f"[Weixin] Failed to write pending pushes: {error}")

    def queue_pending_push(self, receiver: str, text: str, reason: str = "") -> bool:
        receiver = str(receiver or "").strip()
        text = str(text or "")
        if not receiver or not text:
            return False
        try:
            now_ts = int(time.time())
            cutoff = now_ts - 24 * 60 * 60
            aliases = set(self._receiver_aliases(receiver))
            items = [
                item for item in self._read_pending_pushes()
                if int(item.get("queued_ts") or 0) >= cutoff
            ]
            digest = str(uuid.uuid5(uuid.NAMESPACE_URL, receiver + "\n" + text))
            if any(str(item.get("id") or "") == digest for item in items):
                return True
            items.append({
                "id": digest,
                "receiver": receiver,
                "aliases": sorted(aliases),
                "text": text,
                "reason": str(reason or "")[:200],
                "queued_ts": now_ts,
                "queued_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "attempts": 0,
            })
            self._write_pending_pushes(items)
            logger.warning(f"[Weixin] Queued pending push for receiver={receiver}: {reason}")
            return True
        except Exception as error:
            logger.warning(f"[Weixin] Failed to queue pending push: {error}")
            return False

    def _start_pending_push_flush(self, receiver: str, context_token: str) -> None:
        if not receiver or not context_token or not self.api:
            return
        thread = threading.Thread(
            target=self._flush_pending_pushes,
            args=(receiver, context_token),
            name="weixin-pending-push-flush",
            daemon=True,
        )
        thread.start()

    def _flush_pending_pushes(self, receiver: str, context_token: str) -> None:
        try:
            aliases = set(self._receiver_aliases(receiver))
            now_ts = int(time.time())
            cutoff = now_ts - 24 * 60 * 60
            remaining = []
            sent = 0
            for item in self._read_pending_pushes():
                item_aliases = set(item.get("aliases") or self._receiver_aliases(item.get("receiver", "")))
                if int(item.get("queued_ts") or 0) < cutoff:
                    continue
                if not aliases.intersection(item_aliases):
                    remaining.append(item)
                    continue
                text = str(item.get("text") or "")
                if not text:
                    continue
                ok = self._send_text(text, receiver, context_token)
                if ok:
                    sent += 1
                    time.sleep(0.5)
                    continue
                item["attempts"] = int(item.get("attempts") or 0) + 1
                if item["attempts"] < 3:
                    remaining.append(item)
            self._write_pending_pushes(remaining)
            if sent:
                logger.info(f"[Weixin] Flushed {sent} pending push(es) for receiver={receiver}")
        except Exception as error:
            logger.warning(f"[Weixin] Failed to flush pending pushes: {error}")

    # ── QR Login ───────────────────────────────────────────────────────

    @staticmethod
    def _print_qr(qrcode_url: str):
        """Print QR code to terminal for scanning."""
        print("\n" + "=" * 60)
        print("  请使用微信扫描二维码登录 (二维码约2分钟后过期)")
        print("=" * 60)
        try:
            import qrcode as qr_lib
            import io
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=1, border=1)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            buf = io.StringIO()
            qr.print_ascii(out=buf, invert=True)
            try:
                print(buf.getvalue())
            except UnicodeEncodeError:
                # Windows GBK terminals cannot render Unicode block characters
                print(f"\n  (终端不支持显示二维码，请使用链接扫码)")
                print(f"  二维码链接: {qrcode_url}\n")
        except ImportError:
            print(f"\n  二维码链接: {qrcode_url}")
            print("  (安装 'qrcode' 包可在终端显示二维码)\n")

    def _notify_cloud_qrcode(self, qrcode_url: str):
        """Send QR code URL to cloud console when running in cloud mode."""
        if not self.cloud_mode:
            return
        try:
            from common import cloud_client
            client = getattr(cloud_client, "chat_client", None)
            if client and getattr(client, "client_id", None):
                client.send_channel_qrcode("weixin", qrcode_url)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to notify cloud QR code: {e}")

    def _notify_cloud_connected(self):
        """Send connected status to cloud console when login succeeds."""
        if not self.cloud_mode:
            return
        try:
            from common import cloud_client
            client = getattr(cloud_client, "chat_client", None)
            if client and getattr(client, "client_id", None):
                client.send_channel_status("weixin", "connected")
        except Exception as e:
            logger.warning(f"[Weixin] Failed to notify cloud connected: {e}")

    def _qr_login(self, base_url: str) -> dict:
        """Perform interactive QR code login. Returns dict with token/base_url or empty dict."""
        api = WeixinApi(base_url=base_url)
        try:
            qr_resp = api.fetch_qr_code()
        except Exception as e:
            logger.error(f"[Weixin] Failed to fetch QR code: {e}")
            return {}

        qrcode = qr_resp.get("qrcode", "")
        qrcode_url = qr_resp.get("qrcode_img_content", "")

        if not qrcode:
            logger.error("[Weixin] No QR code returned from server")
            return {}

        self._current_qr_url = qrcode_url
        logger.info(f"[Weixin] 微信二维码链接: {qrcode_url}")
        self._print_qr(qrcode_url)
        self._notify_cloud_qrcode(qrcode_url)
        print("  等待扫码...\n")

        scanned_printed = False
        refresh_count = 0
        deadline = time.time() + QR_LOGIN_TIMEOUT_S

        while not self._stop_event.is_set():
            if time.time() >= deadline:
                logger.warning(f"[Weixin] QR login timed out after {QR_LOGIN_TIMEOUT_S}s")
                print(f"\n  二维码登录超时（{QR_LOGIN_TIMEOUT_S}s），请重启后重试")
                break

            try:
                status_resp = api.poll_qr_status(qrcode)
            except Exception as e:
                logger.error(f"[Weixin] QR status poll error: {e}")
                return {}

            status = status_resp.get("status", "wait")

            if status == "wait":
                pass
            elif status == "scaned":
                self.login_status = self.LOGIN_STATUS_SCANNED
                if not scanned_printed:
                    print("  已扫码，请在手机上确认...")
                    scanned_printed = True
            elif status == "expired":
                refresh_count += 1
                if refresh_count >= QR_MAX_REFRESHES:
                    logger.warning(f"[Weixin] QR code refreshed {QR_MAX_REFRESHES} times, giving up")
                    print(f"\n  二维码已刷新 {QR_MAX_REFRESHES} 次仍未扫码，请重启后重试")
                    break
                print(f"  二维码已过期，正在刷新（{refresh_count}/{QR_MAX_REFRESHES}）...")
                try:
                    qr_resp = api.fetch_qr_code()
                    qrcode = qr_resp.get("qrcode", "")
                    qrcode_url = qr_resp.get("qrcode_img_content", "")
                    scanned_printed = False
                    self._current_qr_url = qrcode_url
                    logger.info(f"[Weixin] 微信二维码链接 ({refresh_count}/{QR_MAX_REFRESHES}): {qrcode_url}")
                    self._print_qr(qrcode_url)
                    self._notify_cloud_qrcode(qrcode_url)
                except Exception as e:
                    logger.error(f"[Weixin] QR refresh failed: {e}")
                    return {}
            elif status == "confirmed":
                bot_token = status_resp.get("bot_token", "")
                bot_id = status_resp.get("ilink_bot_id", "")
                result_base_url = status_resp.get("baseurl", base_url)
                user_id = status_resp.get("ilink_user_id", "")

                if not bot_token or not bot_id:
                    logger.error("[Weixin] Login confirmed but missing token/bot_id")
                    return {}

                self._current_qr_url = ""
                print(f"\n  ✅ 微信登录成功！bot_id={bot_id}")
                logger.info(f"[Weixin] Login confirmed: bot_id={bot_id}")
                self._notify_cloud_connected()

                creds = {
                    "token": bot_token,
                    "base_url": result_base_url,
                    "bot_id": bot_id,
                    "user_id": user_id,
                }
                _save_credentials(self._credentials_path, creds)
                if user_id:
                    from channel.channel_access_service import get_channel_access_service
                    get_channel_access_service().auto_approve_owner("weixin", user_id)
                logger.info(f"[Weixin] Credentials saved to {self._credentials_path}")

                return {"token": bot_token, "base_url": result_base_url}

            self._stop_event.wait(1)

        self._current_qr_url = ""
        if self._stop_event.is_set():
            logger.info("[Weixin] QR login cancelled by stop event")
        return {}

    # ── Long-poll loop ─────────────────────────────────────────────────

    def _poll_loop(self):
        """Main long-poll loop: getUpdates -> parse -> produce."""
        logger.info("[Weixin] Starting long-poll loop")
        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                self._last_poll_at = time.time()
                resp = self.api.get_updates(self._get_updates_buf)

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)

                is_error = (ret != 0) or (errcode != 0)
                if is_error:
                    if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                        logger.error("[Weixin] Session expired (errcode -14), attempting saved-token recovery...")
                        if self._handle_session_expired():
                            logger.info("[Weixin] Session recovery successful, resuming long-poll")
                            self._get_updates_buf = ""
                            consecutive_failures = 0
                            continue
                        else:
                            logger.error("[Weixin] Session recovery failed, waiting before retry")
                            self._stop_event.wait(300)
                            continue

                    consecutive_failures += 1
                    self._consecutive_poll_failures = consecutive_failures
                    errmsg = resp.get("errmsg", "")
                    self._last_error = errmsg or f"ret={ret} errcode={errcode}"
                    self._last_error_at = time.time()
                    logger.error(f"[Weixin] getUpdates error: ret={ret} errcode={errcode} "
                                 f"errmsg={errmsg} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                        self._stop_event.wait(BACKOFF_DELAY)
                    else:
                        self._stop_event.wait(RETRY_DELAY)
                    continue

                consecutive_failures = 0
                self._consecutive_poll_failures = 0
                self._last_poll_success_at = time.time()
                self._last_error = ""

                # Update sync cursor
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    self._get_updates_buf = new_buf

                # Process messages. Media (image/file/video) messages are
                # handled before text ones within the same batch so that a
                # text message sent together with an image finds the image
                # already in the cache (the media download would otherwise let
                # the text win the race and reach the agent without the media).
                msgs = resp.get("msgs", [])
                for raw_msg in self._order_msgs(msgs):
                    try:
                        self._last_message_at = time.time()
                        self._process_message(raw_msg)
                    except Exception as e:
                        logger.error(f"[Weixin] Failed to process message: {e}", exc_info=True)

            except Exception as e:
                if self._stop_event.is_set():
                    break
                consecutive_failures += 1
                self._consecutive_poll_failures = consecutive_failures
                self._last_error = str(e)[:500]
                self._last_error_at = time.time()
                logger.error(f"[Weixin] getUpdates exception: {e} "
                             f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    self._stop_event.wait(BACKOFF_DELAY)
                else:
                    self._stop_event.wait(RETRY_DELAY)

        logger.info("[Weixin] Long-poll loop ended")

    @staticmethod
    def _raw_msg_has_media(raw_msg: dict) -> bool:
        """Whether a raw message carries a media item (image/voice/file/video)."""
        from channel.weixin.weixin_message import (
            ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO,
        )
        media_types = {ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO}
        for item in raw_msg.get("item_list", []):
            if item.get("type") in media_types:
                return True
            ref_mi = item.get("ref_msg", {}).get("message_item", {})
            if ref_mi.get("type") in media_types:
                return True
        return False

    def _order_msgs(self, msgs: list) -> list:
        """Stable-sort a batch so media-bearing messages come before pure text.

        Only USER messages (message_type == 1) are considered media candidates;
        ordering is otherwise stable to preserve the original sequence.
        """
        def is_media(m):
            return m.get("message_type", 0) == 1 and self._raw_msg_has_media(m)
        media = [m for m in msgs if is_media(m)]
        rest = [m for m in msgs if not is_media(m)]
        return media + rest

    def _mark_pending_media(self, session_id: str):
        with self._pending_media_lock:
            self._pending_media[session_id] = time.time() + PENDING_MEDIA_WAIT_S

    def _clear_pending_media(self, session_id: str):
        with self._pending_media_lock:
            self._pending_media.pop(session_id, None)

    def _wait_for_pending_media(self, session_id: str, file_cache):
        """Block briefly if a media download for this session is in flight.

        Handles the case where an image and a text message are delivered in
        separate poll batches and the text is processed first: give the media
        a short window to land in the cache before the text reaches the agent.
        """
        with self._pending_media_lock:
            deadline = self._pending_media.get(session_id)
        if not deadline:
            return
        while time.time() < deadline:
            if file_cache.get(session_id):
                return
            time.sleep(PENDING_MEDIA_POLL_S)

    def _process_message(self, raw_msg: dict):
        """Parse a single inbound message and produce to the handling queue."""
        msg_type = raw_msg.get("message_type", 0)
        if msg_type != 1:  # Only process USER messages (type=1)
            return

        msg_id = str(raw_msg.get("message_id", raw_msg.get("seq", "")))
        if self._received_msgs.get(msg_id):
            return
        self._received_msgs[msg_id] = True

        from_user = raw_msg.get("from_user_id", "")
        context_token = raw_msg.get("context_token", "")

        if context_token and from_user:
            self._update_context_token(from_user, context_token)

        # Personal WeChat is an instruction surface, so unknown users must be
        # paired in the console before their messages can reach the Agent.
        from channel.channel_access_service import get_channel_access_service
        decision = get_channel_access_service().authorize_or_request(
            "weixin", from_user, from_user
        )
        if not decision.get("allowed"):
            if decision.get("notify") and context_token and self.api:
                status = decision.get("status")
                if status == "pending":
                    entry = decision.get("entry", {})
                    text = (
                        "🔐 此微信账号尚未获得 CowAgent 使用权限。\n\n"
                        f"配对码：{entry.get('pairing_code', '')}\n"
                        "请让管理员打开 CowAgent → 通道管理 → 微信用户授权，批准本次请求。\n"
                        "配对码 15 分钟内有效。"
                    )
                else:
                    text = "⛔ 此微信账号当前已被停用或撤销授权，请联系 CowAgent 管理员。"
                try:
                    self.api.send_text(from_user, text, context_token)
                except Exception as e:
                    logger.warning(f"[Weixin] Failed to send pairing notice: {e}")
            return

        return self._process_authorized_message(raw_msg, from_user, context_token)

    from common.task_trace import weixin_entry as _trace_entry, send_observer as _trace_send

    @_trace_entry
    def _process_authorized_message(self, raw_msg, from_user, context_token):
        session_id = from_user
        record_session_id = self._conversation_session_id(from_user)
        # Mark media as in-flight *before* the (slow) synchronous download so a
        # text message from the same user that is processed first can wait for
        # it (see _wait_for_pending_media).
        has_media = self._raw_msg_has_media(raw_msg)
        if has_media and session_id:
            self._mark_pending_media(session_id)

        cdn_base_url = self.api.cdn_base_url if self.api else CDN_BASE_URL
        try:
            wx_msg = WeixinMessage(raw_msg, cdn_base_url=cdn_base_url)
        except Exception as e:
            logger.error(f"[Weixin] Failed to parse WeixinMessage: {e}", exc_info=True)
            if has_media and session_id:
                self._clear_pending_media(session_id)
            return

        logger.info(f"[Weixin] Received: from={from_user} ctype={wx_msg.ctype} "
                     f"content={str(wx_msg.content)[:50]}")

        # File cache logic
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        if wx_msg.ctype == ContextType.IMAGE:
            if hasattr(wx_msg, "image_path") and wx_msg.image_path:
                file_cache.add(session_id, wx_msg.image_path, file_type="image")
                logger.info(f"[Weixin] Image cached for session {session_id}")
            self._clear_pending_media(session_id)
            return

        if wx_msg.ctype == ContextType.FILE:
            wx_msg.prepare()
            file_cache.add(session_id, wx_msg.content, file_type="file")
            logger.info(f"[Weixin] File cached for session {session_id}: {wx_msg.content}")
            self._clear_pending_media(session_id)
            return

        if wx_msg.ctype == ContextType.TEXT:
            if has_media:
                # This message already carries its own media inline; its ref was
                # attached during parsing, so just release the in-flight marker.
                self._clear_pending_media(session_id)
            else:
                # A media message from the same user may still be downloading (it
                # can arrive in a separate poll batch); wait briefly so its path
                # gets attached instead of the agent answering without the image.
                self._wait_for_pending_media(session_id, file_cache)
            cached_files = file_cache.get(session_id)
            if cached_files:
                refs = []
                for fi in cached_files:
                    ftype, fpath = fi["type"], fi["path"]
                    if ftype == "image":
                        refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        refs.append(f"[视频: {fpath}]")
                    else:
                        refs.append(f"[文件: {fpath}]")
                wx_msg.content = wx_msg.content + "\n" + "\n".join(refs)
                file_cache.clear(session_id)

            # Deterministic fast path for read-only filename searches.  It is
            # deliberately placed after media association but before produce(),
            # so ordinary requests and file actions still use the full Agent.
            try:
                from common.local_file_index import (
                    format_file_search_reply, get_local_file_index,
                    parse_file_search_intent,
                )
                intent = parse_file_search_intent(wx_msg.content)
                if intent is not None:
                    if not intent.query and not intent.extensions:
                        reply_text = "请告诉我要找的文件名、关键词或类型，例如：找文件：技术部周报，最近7天，Word。"
                    else:
                        file_index = get_local_file_index()
                        search_started = time.perf_counter()
                        hits = file_index.search(intent)
                        refreshed_for_retry = False
                        if not hits and self._should_refresh_file_index_before_miss(wx_msg.content):
                            try:
                                file_index.refresh()
                                hits = file_index.search(intent)
                                refreshed_for_retry = True
                                logger.info("[Weixin] File search retried after learned index refresh")
                            except Exception as refresh_error:
                                logger.warning(f"[Weixin] Learned file refresh failed: {refresh_error}")
                        if hits:
                            self._record_weixin_tool_audit(
                                "weixin:file_fast_path",
                                wx_msg.content,
                                True,
                                f"命中 {len(hits)} 个文件",
                                "file",
                                time.perf_counter() - search_started,
                            )
                            self._record_weixin_route_success(
                                wx_msg.content,
                                "file",
                                "refresh_then_search" if refreshed_for_retry else "index_search",
                                "file_fast_path",
                                f"命中 {len(hits)} 个文件",
                            )
                        else:
                            self._record_weixin_tool_audit(
                                "weixin:file_fast_path",
                                wx_msg.content,
                                False,
                                "本地文件未命中",
                                "file",
                                time.perf_counter() - search_started,
                            )
                        standalone_exact_name = (
                            len(hits) == 1
                            and intent.query
                            and os.path.basename(intent.query).casefold()
                            == str(hits[0].get("name") or "").casefold()
                        )
                        # A bare exact filename is normally the user's answer
                        # to our preceding "please provide the filename"
                        # question.  With one unambiguous hit, send it directly
                        # instead of asking the user to issue another command.
                        wants_send = bool(
                            __import__("re").search(r"(?:发|传|发送)给我|帮我发送", wx_msg.content)
                            or standalone_exact_name
                        )
                        if wants_send and hits:
                            selected = hits[0]
                            notice = f"已找到最新匹配文件：{selected['name']}，正在发送。"
                            self._send_text(notice, from_user, context_token)
                            suffix = os.path.splitext(selected["path"])[1].lower()
                            if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                                self._send_image(selected["path"], from_user, context_token)
                            else:
                                self._send_file(selected["path"], from_user, context_token)
                            self._record_weixin_tool_audit(
                                "weixin:file_send",
                                wx_msg.content,
                                True,
                                f"已发送文件：{selected['name']}",
                                "file",
                                0,
                            )
                            self._persist_direct_exchange(record_session_id, wx_msg.content, notice)
                            logger.info(f"[Weixin] Fast file sent path={selected['path']}")
                            return
                        reply_text = format_file_search_reply(intent, hits)
                    if self.api and context_token:
                        self._send_text(reply_text, from_user, context_token)
                        self._persist_direct_exchange(record_session_id, wx_msg.content, reply_text)
                        logger.info(f"[Weixin] Fast file search query={intent.query!r}")
                        return
            except Exception as e:
                # Search acceleration must never block the normal Agent path.
                logger.warning(f"[Weixin] Fast file search fallback: {e}")

            try:
                from channel.weixin.mission_control_bridge import maybe_handle_weixin_mission_command
                mission_reply = maybe_handle_weixin_mission_command(wx_msg.content)
                if mission_reply:
                    if self.api and context_token:
                        self._send_text(mission_reply, from_user, context_token)
                        self._persist_direct_exchange(record_session_id, wx_msg.content, mission_reply)
                        logger.info("[Weixin] Mission Control command handled directly")
                        return
            except Exception as e:
                logger.warning(f"[Weixin] Mission Control bridge fallback: {e}")

            try:
                from channel.weixin.task_first_bridge import maybe_handle_weixin_task_first
                task_reply = maybe_handle_weixin_task_first(wx_msg.content)
                if task_reply:
                    if self.api and context_token:
                        self._send_text(task_reply, from_user, context_token)
                        self._persist_direct_exchange(record_session_id, wx_msg.content, task_reply)
                        logger.info("[Weixin] Common task handled before model fallback")
                        return
            except Exception as e:
                logger.warning(f"[Weixin] Task-first bridge fallback: {e}")

            try:
                # Lightweight commands such as "明天上海天气" should not depend
                # on the full model pipeline.  Answer them deterministically
                # before queueing the message, so WeChat remains useful even
                # when the model returns an empty response or is temporarily
                # unavailable.
                try:
                    from channel.chat_channel import _write_direct_fast_debug
                    _write_direct_fast_debug(
                        "weixin_pre_model_fast_enter",
                        content=str(wx_msg.content or "")[:200],
                        ctype=str(wx_msg.ctype),
                        from_user=from_user,
                        has_api=bool(self.api),
                        has_context_token=bool(context_token),
                        record_session_id=record_session_id,
                    )
                except Exception:
                    pass
                from agent.fast_queries import try_fast_query
                fast_reply = try_fast_query(wx_msg.content)
                try:
                    from channel.chat_channel import _write_direct_fast_debug
                    _write_direct_fast_debug(
                        "weixin_pre_model_fast_result",
                        content=str(wx_msg.content or "")[:200],
                        has_reply=bool(fast_reply),
                        preview=str(fast_reply or "")[:200],
                        has_api=bool(self.api),
                        has_context_token=bool(context_token),
                        record_session_id=record_session_id,
                    )
                except Exception:
                    pass
                if fast_reply:
                    fast_reply = str(fast_reply).strip()
                    if fast_reply and self.api and context_token:
                        self._send_text(fast_reply, from_user, context_token)
                        self._persist_direct_exchange(record_session_id, wx_msg.content, fast_reply)
                        try:
                            from channel.chat_channel import _write_direct_fast_debug
                            _write_direct_fast_debug(
                                "weixin_pre_model_fast_sent",
                                content=str(wx_msg.content or "")[:200],
                                preview=fast_reply[:200],
                                record_session_id=record_session_id,
                            )
                        except Exception:
                            pass
                        try:
                            from channel.weixin.failure_learning import record_weixin_success
                            record_weixin_success(
                                command=str(wx_msg.content or ""),
                                task_type="direct_fast_query",
                                action="weixin_pre_model_fast_reply",
                                route="weixin:pre_model_fast_query",
                                evidence=fast_reply[:120],
                            )
                        except Exception:
                            pass
                        logger.info("[Weixin] Fast query handled before model fallback")
                        return
                    try:
                        from channel.chat_channel import _write_direct_fast_debug
                        _write_direct_fast_debug(
                            "weixin_pre_model_fast_unsent",
                            reason="missing_api_or_context_token_or_blank_reply",
                            content=str(wx_msg.content or "")[:200],
                            has_api=bool(self.api),
                            has_context_token=bool(context_token),
                            reply_blank=not bool(fast_reply),
                            record_session_id=record_session_id,
                        )
                    except Exception:
                        pass
            except Exception as e:
                try:
                    from channel.chat_channel import _write_direct_fast_debug
                    _write_direct_fast_debug(
                        "weixin_pre_model_fast_error",
                        content=str(wx_msg.content or "")[:200],
                        error=str(e),
                        record_session_id=record_session_id,
                    )
                except Exception:
                    pass
                logger.warning(f"[Weixin] Direct fast query fallback skipped: {e}")

        context = self._compose_context(
            wx_msg.ctype,
            wx_msg.content,
            isgroup=False,
            msg=wx_msg,
            no_need_at=True,
            record_session_id=record_session_id,
        )
        if context:
            from common.runtime_identity import current_identity
            context['trace_id'] = current_identity().run_id
            self._persist_inbound_message(record_session_id, wx_msg.content)
            self._start_processing_notice(context, from_user, context_token)
            self.produce(context)

    @staticmethod
    def _should_refresh_file_index_before_miss(text: str) -> bool:
        try:
            from channel.weixin.failure_learning import learned_weixin_route_strategy
            return bool(learned_weixin_route_strategy(text).get("file_refresh_before_miss"))
        except Exception:
            return False

    @staticmethod
    def _record_weixin_route_success(command: str, task_type: str, action: str, route: str, evidence: str = ""):
        try:
            from channel.weixin.failure_learning import record_weixin_success
            record_weixin_success(
                command=command,
                task_type=task_type,
                action=action,
                route=route,
                evidence=evidence,
            )
        except Exception:
            pass

    @staticmethod
    def _record_weixin_tool_audit(tool: str, command: str, success: bool, result: str = "", intent: str = "weixin", execution_time: float = 0):
        try:
            from agent.tools.audit import classify_error, record_tool_run
            record_tool_run(
                tool=tool,
                arguments={
                    "channel": "weixin",
                    "command_preview": str(command or "")[:160],
                },
                success=bool(success),
                status="success" if success else "error",
                result=str(result or "")[:500],
                execution_time=float(execution_time or 0),
                error_code="" if success else classify_error(result),
                intent=str(intent or "weixin"),
                run_id=f"weixin-{uuid.uuid4().hex[:12]}",
            )
        except Exception:
            pass

    # ── _compose_context ───────────────────────────────────────────────

    @staticmethod
    def _conversation_session_id(user_id: str, when=None) -> str:
        """Return the weekly conversation bucket used by the WeChat assistant UI.

        The raw WeChat user id is still used for sending messages and tokens.
        Only the persisted conversation id is rotated weekly, so old records
        stay readable while the current week starts as a fresh session.
        """
        base = str(user_id or "").split("#week:", 1)[0].strip()
        if not base:
            return ""
        now = when or datetime.datetime.now().astimezone()
        iso_year, iso_week, _ = now.isocalendar()
        return f"{base}#week:{iso_year}-W{iso_week:02d}"

    @staticmethod
    def _base_user_id(session_id: str) -> str:
        return str(session_id or "").split("#week:", 1)[0].strip()

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]
        context["session_id"] = kwargs.get("record_session_id") or self._conversation_session_id(cmsg.from_user_id)
        context["receiver"] = cmsg.other_user_id or cmsg.from_user_id

        if ctype == ContextType.TEXT:
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            if "desire_rtype" not in context and conf().get("always_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE

        elif ctype == ContextType.VOICE:
            if "desire_rtype" not in context and (
                conf().get("voice_reply_voice") or conf().get("always_reply_voice")
            ):
                context["desire_rtype"] = ReplyType.VOICE

        return context

    # ── Send reply ─────────────────────────────────────────────────────

    @staticmethod
    def _persist_direct_exchange(session_id: str, user_text: str, assistant_text: str):
        """Persist deterministic fast-path replies that bypass ChatService."""
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return
            from agent.memory import get_conversation_store
            get_conversation_store().append_messages(
                session_id,
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ],
                channel_type="weixin",
            )
        except Exception as error:
            logger.warning(f"[Weixin] Failed to persist direct exchange: {error}")

    @staticmethod
    def _persist_inbound_message(session_id: str, user_text: str):
        """Persist inbound WeChat text before the Agent runs.

        This keeps the WeChat conversation viewer useful even if the model,
        tool chain, or outbound send fails later. ConversationStore skips the
        duplicate user turn when ChatService persists the successful run.
        """
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return
            from agent.memory import get_conversation_store
            get_conversation_store().append_messages(
                session_id,
                [{
                    "role": "user",
                    "content": user_text,
                    "extras": {"source": "weixin_inbound_pending"},
                }],
                channel_type="weixin",
            )
        except Exception as error:
            logger.warning(f"[Weixin] Failed to persist inbound message: {error}")

    def send(self, reply: Reply, context: Context):
        self._finish_processing_notice(context)
        receiver = context.get("receiver", "")
        msg = context.get("msg")
        context_token = self._get_context_token(receiver, msg)

        if not context_token:
            self._last_send_error = "微信会话发送令牌缺失或已过期，请先从手机微信给 Agent 发送一条消息刷新授权"
            logger.error(f"[Weixin] No context_token for receiver={receiver}, cannot send")
            return False

        # A media reply can carry an accompanying message (the agent's summary of
        # the file it just sent). ilink has no caption field, so it goes out as a
        # separate bubble first. Image replies are skipped: chat_channel already
        # splits their text off before we get here, and doing it again would
        # duplicate the bubble.
        text_content = getattr(reply, "text_content", "") or ""
        if text_content and reply.type not in (ReplyType.TEXT, ReplyType.IMAGE_URL, ReplyType.IMAGE):
            self._send_text(text_content, receiver, context_token)
            time.sleep(0.3)

        if reply.type == ReplyType.TEXT:
            ok = self._send_text(reply.content, receiver, context_token)
            if not ok:
                self._record_weixin_send_failure(context, "文本消息发送失败")
            return ok
        elif reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            self._send_image(reply.content, receiver, context_token)
        elif reply.type == ReplyType.FILE:
            self._send_file(reply.content, receiver, context_token)
        elif reply.type in (ReplyType.VIDEO, ReplyType.VIDEO_URL):
            self._send_video(reply.content, receiver, context_token)
        elif reply.type == ReplyType.VOICE:
            # ilink has no outbound voice item; deliver TTS as a file attachment.
            self._send_file(reply.content, receiver, context_token)
        else:
            logger.warning(f"[Weixin] Unsupported reply type: {reply.type}, fallback to text")
            ok = self._send_text(str(reply.content), receiver, context_token)
            if not ok:
                self._record_weixin_send_failure(context, "降级文本消息发送失败")
            return ok

    @staticmethod
    def _record_weixin_send_failure(context: Context, message: str):
        try:
            from channel.weixin.failure_learning import record_weixin_failure
            record_weixin_failure(
                command=str(context.content or ""),
                category="send_failure",
                message=message,
                route="weixin_send",
            )
        except Exception:
            pass

    def _get_context_token(self, receiver: str, msg=None) -> str:
        """Get the context_token for a receiver, required for all sends."""
        if msg and hasattr(msg, "context_token") and msg.context_token:
            return msg.context_token
        with self._context_tokens_lock:
            for alias in self._receiver_aliases(receiver):
                token = self._context_tokens.get(alias, "")
                if token and not self._is_context_token_stale_locked(alias):
                    return token
        return ""

    @staticmethod
    def _context_token_max_age_minutes() -> int:
        try:
            # Default to no artificial expiry.  The ilink server will still
            # reject truly invalid context_tokens with -14/-2, at which point
            # we drop the token and ask the user to refresh.  A short local TTL
            # caused reminders to be marked expired simply because the user had
            # not chatted with the bot for a couple of hours.
            value = int(conf().get("weixin_context_token_ttl_minutes", 0) or 0)
            return max(value, 0)
        except Exception:
            return 0

    @staticmethod
    def _parse_context_token_seen_at(value: str):
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
            return parsed
        except Exception:
            return None

    def _context_token_age_seconds_locked(self, alias: str):
        seen_at = self._parse_context_token_seen_at(self._context_token_seen_at.get(alias, ""))
        if not seen_at:
            return None
        return max(0, int((datetime.datetime.now().astimezone() - seen_at).total_seconds()))

    def _is_context_token_stale_locked(self, alias: str) -> bool:
        max_age = self._context_token_max_age_minutes()
        if max_age <= 0:
            return False
        age = self._context_token_age_seconds_locked(alias)
        return bool(age is not None and age > max_age * 60)

    def context_token_status(self, receiver: str) -> dict:
        """Return a conservative sendability status for scheduled Weixin pushes."""
        aliases = self._receiver_aliases(receiver)
        max_age_minutes = self._context_token_max_age_minutes()
        max_age_seconds = max_age_minutes * 60
        with self._context_tokens_lock:
            for alias in aliases:
                token = self._context_tokens.get(alias, "")
                if not token:
                    continue
                seen_at = self._context_token_seen_at.get(alias, "")
                age = self._context_token_age_seconds_locked(alias)
                if max_age_minutes > 0 and age is not None and age > max_age_seconds:
                    return {
                        "ready": False,
                        "token_exists": True,
                        "stale": True,
                        "seen_at": seen_at,
                        "age_seconds": age,
                        "message": "微信会话发送令牌已超过有效保护时间，请先从手机微信给 Agent 发送一条消息刷新授权",
                    }
                return {
                    "ready": True,
                    "token_exists": True,
                    "stale": False,
                    "seen_at": seen_at,
                    "age_seconds": age,
                    "message": "微信会话可发送",
                }
        return {
            "ready": False,
            "token_exists": False,
            "stale": False,
            "seen_at": "",
            "age_seconds": None,
            "message": "该微信会话没有可用发送令牌，请先从手机微信给 Agent 发送一条消息后再试",
        }

    @staticmethod
    def _receiver_aliases(receiver: str):
        """Return stable aliases for Weixin receiver ids.

        Some UI surfaces persist the full ilink id (xxx@im.wechat), while older
        tasks may keep only the base id. Store and lookup both forms so a valid
        token is not missed because of that harmless representation difference.
        """
        raw = str(receiver or "").strip()
        if not raw:
            return []
        aliases = [raw]
        if raw.endswith("@im.wechat"):
            aliases.append(raw.split("@", 1)[0])
        elif "@" not in raw:
            aliases.append(f"{raw}@im.wechat")
        seen = set()
        return [item for item in aliases if item and not (item in seen or seen.add(item))]

    def has_context_token(self, receiver: str) -> bool:
        """Whether this receiver currently has a usable outbound token cached."""
        try:
            return bool(self.context_token_status(receiver).get("ready"))
        except Exception:
            return bool(self._get_context_token(receiver))

    def _check_send_response(self, resp, receiver: str) -> bool:
        """Inspect a send-API response; drop stale context_token on -14.

        ilink uses ret/errcode = -14 to signal that the session (and any
        cached context_token) is no longer valid. The plugin keeps running
        because the bot itself can re-login; we just need to forget the
        per-user token so the next push won't retry forever.
        """
        if not isinstance(resp, dict):
            return True
        ret = resp.get("ret")
        errcode = resp.get("errcode")
        message = ""
        if isinstance(resp, dict):
            message = str(resp.get("message") or resp.get("errmsg") or resp.get("error") or "")
        if ret == -14 or errcode == -14 or ret == -2 or errcode == -2:
            self._last_send_error = "微信会话发送令牌已失效，请先从手机微信给 Agent 发送一条消息刷新授权"
            logger.warning(
                f"[Weixin] Send token invalid for receiver={receiver}: "
                f"ret={ret}, errcode={errcode}, message={message}; dropping cached context_token"
            )
            self._invalidate_context_token(receiver)
            return False
        if ret not in (None, 0) or errcode not in (None, 0):
            self._last_send_error = f"微信接口拒绝发送：ret={ret}, errcode={errcode}" + (f"，{message}" if message else "")
            logger.warning(f"[Weixin] Send rejected for receiver={receiver}: ret={ret}, errcode={errcode}, message={message}")
            return False
        self._last_send_error = ""
        return True

    @_trace_send
    def _send_text(self, text: str, receiver: str, context_token: str):
        if len(text) <= TEXT_CHUNK_LIMIT:
            try:
                resp = self.api.send_text(receiver, text, context_token)
                if not self._check_send_response(resp, receiver):
                    return False
                logger.debug(f"[Weixin] Text sent to {receiver}, len={len(text)}")
            except Exception as e:
                self._last_send_error = f"微信文本发送异常：{e}"
                logger.error(f"[Weixin] Failed to send text: {e}")
                return False
            return True

        chunks = self._split_text(text, TEXT_CHUNK_LIMIT)
        for i, chunk in enumerate(chunks):
            try:
                resp = self.api.send_text(receiver, chunk, context_token)
                if not self._check_send_response(resp, receiver):
                    return False
                logger.debug(f"[Weixin] Text chunk {i+1}/{len(chunks)} sent to {receiver}, len={len(chunk)}")
            except Exception as e:
                self._last_send_error = f"微信文本分段发送异常：{e}"
                logger.error(f"[Weixin] Failed to send text chunk {i+1}/{len(chunks)}: {e}")
                return False
            if i < len(chunks) - 1:
                time.sleep(0.5)
        return True

    # ── Processing notices ─────────────────────────────────────────────

    def _processing_notice_enabled(self) -> bool:
        return bool(conf().get("weixin_processing_notice", True))

    @staticmethod
    def _context_msg_id(context: Context) -> str:
        msg = context.get("msg") if context else None
        return str(getattr(msg, "msg_id", "") or context.get("request_id") or "")

    def _start_processing_notice(self, context: Context, receiver: str, context_token: str):
        if not self._processing_notice_enabled() or not receiver or not context_token:
            return
        if context.type != ContextType.TEXT:
            return
        content = str(context.content or "").strip()
        if not content or content.lower() in {"/cancel", "/stop"}:
            return
        msg_id = self._context_msg_id(context)
        if not msg_id:
            return
        state = {
            "receiver": receiver,
            "context_token": context_token,
            "content": content[:80],
            "started_at": time.time(),
            "first_sent": False,
            "timeout_sent": False,
            "done": False,
        }
        with self._processing_notices_lock:
            self._processing_notices[msg_id] = state
        first_timer = threading.Timer(
            PROCESSING_NOTICE_DELAY_S,
            self._send_processing_notice_if_pending,
            args=(msg_id, "first"),
        )
        first_timer.daemon = True
        first_timer.start()
        timeout_timer = threading.Timer(
            PROCESSING_TIMEOUT_NOTICE_S,
            self._send_processing_notice_if_pending,
            args=(msg_id, "timeout"),
        )
        timeout_timer.daemon = True
        timeout_timer.start()

    def _finish_processing_notice(self, context: Context):
        msg_id = self._context_msg_id(context)
        if not msg_id:
            return
        with self._processing_notices_lock:
            state = self._processing_notices.get(msg_id)
            if state is not None:
                state["done"] = True
                self._processing_notices.pop(msg_id, None)

    def _send_processing_notice_if_pending(self, msg_id: str, stage: str):
        with self._processing_notices_lock:
            state = self._processing_notices.get(msg_id)
            if not state or state.get("done"):
                return
            if stage == "first":
                if state.get("first_sent"):
                    return
                state["first_sent"] = True
                text = "收到，我正在处理。会先尝试可用路径执行，完成后直接把结果发给你。"
            else:
                if state.get("timeout_sent"):
                    return
                state["timeout_sent"] = True
                elapsed = int(max(1, time.time() - float(state.get("started_at") or time.time())))
                text = f"还在处理中，已用时约 {elapsed} 秒。可能正在读取本地数据、联网查询或等待工具返回；如果失败，我会直接说明原因和可重试方式。"
            receiver = state.get("receiver", "")
            context_token = state.get("context_token", "")
        if not receiver or not context_token:
            return
        try:
            self._send_text(text, receiver, context_token)
        except Exception as error:
            logger.warning(f"[Weixin] processing notice failed: {error}")

    @staticmethod
    def _split_text(text: str, limit: int) -> list:
        """Split text into chunks, preferring to break at paragraph or line boundaries."""
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            cut = text.rfind("\n\n", 0, limit)
            if cut <= 0:
                cut = text.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    def _send_image(self, img_path_or_url: str, receiver: str, context_token: str):
        local_path = self._resolve_media_path(img_path_or_url)
        if not local_path:
            self._send_text("[Image send failed: file not found]", receiver, context_token)
            return
        try:
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=1)
            resp = self.api.send_image_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                ciphertext_size=result["ciphertext_size"],
            )
            self._check_send_response(resp, receiver)
            logger.info(f"[Weixin] Image sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] Image send failed: {e}")
            self._send_text("[Image send failed]", receiver, context_token)

    def _send_file(self, file_path_or_url: str, receiver: str, context_token: str):
        local_path = self._resolve_media_path(file_path_or_url)
        if not local_path:
            self._send_text("[File send failed: file not found]", receiver, context_token)
            return
        try:
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=3)
            resp = self.api.send_file_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                file_name=os.path.basename(local_path),
                file_size=result["raw_size"],
            )
            self._check_send_response(resp, receiver)
            logger.info(f"[Weixin] File sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] File send failed: {e}")
            self._send_text("[File send failed]", receiver, context_token)

    def _send_video(self, video_path_or_url: str, receiver: str, context_token: str):
        local_path = self._resolve_media_path(video_path_or_url)
        if not local_path:
            self._send_text("[Video send failed: file not found]", receiver, context_token)
            return
        try:
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=2)
            resp = self.api.send_video_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                ciphertext_size=result["ciphertext_size"],
            )
            self._check_send_response(resp, receiver)
            logger.info(f"[Weixin] Video sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] Video send failed: {e}")
            self._send_text("[Video send failed]", receiver, context_token)

    @staticmethod
    def _resolve_media_path(path_or_url: str) -> str:
        """Resolve a file path or URL to a local file path. Downloads if needed."""
        if not path_or_url:
            return ""

        local_path = path_or_url
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        if local_path.startswith(("http://", "https://")):
            try:
                resp = requests.get(local_path, timeout=60)
                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "")
                ext = ".bin"
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "png" in ct:
                    ext = ".png"
                elif "gif" in ct:
                    ext = ".gif"
                elif "webp" in ct:
                    ext = ".webp"
                elif "mp4" in ct:
                    ext = ".mp4"
                elif "pdf" in ct:
                    ext = ".pdf"

                tmp_path = f"/tmp/wx_media_{uuid.uuid4().hex[:8]}{ext}"
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
                return tmp_path
            except Exception as e:
                logger.error(f"[Weixin] Failed to download media: {e}")
                return ""

        if os.path.exists(local_path):
            return local_path

        logger.warning(f"[Weixin] Media file not found: {local_path}")
        return ""
