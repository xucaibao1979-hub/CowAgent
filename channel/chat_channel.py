import os
import re
import json
import datetime
import threading
import time
from asyncio import CancelledError
from concurrent.futures import Future, ThreadPoolExecutor

from bridge.context import *
from bridge.reply import *
from channel.channel import Channel
from common.dequeue import Dequeue
from common import memory
from agent.routing import AgentUnavailableError
from common.i18n import t as _t
from common.runtime_identity import RuntimeIdentity, use_identity
from common.task_trace import traced, emit as emit_trace
from plugins import *

try:
    from voice.audio_convert import any_to_wav
except Exception as e:
    pass

handler_pool = ThreadPoolExecutor(max_workers=8)  # 处理消息的线程池


def _write_direct_fast_debug(event: str, **payload):
    """Write a tiny JSONL breadcrumb for WeChat/WeCom direct-route diagnosis."""
    try:
        from config import get_data_root

        log_dir = os.path.join(get_data_root(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
            "event": event,
        }
        record.update(payload)
        with open(os.path.join(log_dir, "direct_fast_debug.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# 抽象类, 它包含了与消息通道无关的通用处理逻辑
class ChatChannel(Channel):
    name = None  # 登录的用户名
    user_id = None  # 登录的用户id

    def __init__(self):
        super().__init__()
        # Instance-level attributes so each channel subclass has its own
        # independent session queue and lock. Previously these were class-level,
        # which caused contexts from one channel (e.g. Feishu) to be consumed
        # by another channel's consume() thread (e.g. Web), leading to errors
        # like "No request_id found in context".
        self.futures = {}
        self.sessions = {}
        self.lock = threading.Lock()
        _thread = threading.Thread(target=self.consume)
        _thread.setDaemon(True)
        _thread.start()

    # 根据消息构造context，消息内容相关的触发项写在这里
    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype
        # context首次传入时，receiver是None，根据类型设置receiver
        first_in = "receiver" not in context
        # 群名匹配过程，设置session_id和receiver
        if first_in:  # context首次传入时，receiver是None，根据类型设置receiver
            config = conf()
            cmsg = context["msg"]
            user_data = conf().get_user_data(cmsg.from_user_id)
            context["openai_api_key"] = user_data.get("openai_api_key")
            context["gpt_model"] = user_data.get("gpt_model")
            if context.get("isgroup", False):
                group_name = cmsg.other_user_nickname
                group_id = cmsg.other_user_id

                group_name_white_list = config.get("group_name_white_list", [])
                group_name_keyword_white_list = config.get("group_name_keyword_white_list", [])
                if any(
                    [
                        group_name in group_name_white_list,
                        "ALL_GROUP" in group_name_white_list,
                        check_contain(group_name, group_name_keyword_white_list),
                    ]
                ):
                    # Check global group_shared_session config first
                    group_shared_session = conf().get("group_shared_session", True)
                    if group_shared_session:
                        # All users in the group share the same session
                        session_id = group_id
                    else:
                        # Check group-specific whitelist (legacy behavior)
                        group_chat_in_one_session = conf().get("group_chat_in_one_session", [])
                        session_id = cmsg.actual_user_id
                        if any(
                            [
                                group_name in group_chat_in_one_session,
                                "ALL_GROUP" in group_chat_in_one_session,
                            ]
                        ):
                            session_id = group_id
                else:
                    logger.debug(f"No need reply, groupName not in whitelist, group_name={group_name}")
                    return None
                context["session_id"] = session_id
                context["receiver"] = group_id
            else:
                context["session_id"] = cmsg.other_user_id
                context["receiver"] = cmsg.other_user_id
            e_context = PluginManager().emit_event(EventContext(Event.ON_RECEIVE_MESSAGE, {"channel": self, "context": context}))
            context = e_context["context"]
            if e_context.is_pass() or context is None:
                return context
            if cmsg.from_user_id == self.user_id and not config.get("trigger_by_self", True):
                logger.debug("[chat_channel]self message skipped")
                return None

        # 消息内容匹配过程，并处理content
        if ctype == ContextType.TEXT:
            if first_in and "」\n- - - - - - -" in content:  # 初次匹配 过滤引用消息
                logger.debug(content)
                logger.debug("[chat_channel]reference query skipped")
                return None

            nick_name_black_list = conf().get("nick_name_black_list", [])
            if context.get("isgroup", False):  # 群聊
                # 校验关键字
                match_prefix = check_prefix(content, conf().get("group_chat_prefix"))
                match_contain = check_contain(content, conf().get("group_chat_keyword"))
                flag = False
                if context["msg"].to_user_id != context["msg"].actual_user_id:
                    if match_prefix is not None or match_contain is not None:
                        flag = True
                        if match_prefix:
                            content = content.replace(match_prefix, "", 1).strip()
                    if context["msg"].is_at:
                        nick_name = context["msg"].actual_user_nickname
                        if nick_name and nick_name in nick_name_black_list:
                            # 黑名单过滤
                            logger.warning(f"[chat_channel] Nickname {nick_name} in In BlackList, ignore")
                            return None

                        logger.info("[chat_channel]receive group at")
                        if not conf().get("group_at_off", False):
                            flag = True
                        self.name = self.name if self.name is not None else ""  # 部分渠道self.name可能没有赋值
                        pattern = f"@{re.escape(self.name)}(\u2005|\u0020)"
                        subtract_res = re.sub(pattern, r"", content)
                        if isinstance(context["msg"].at_list, list):
                            for at in context["msg"].at_list:
                                pattern = f"@{re.escape(at)}(\u2005|\u0020)"
                                subtract_res = re.sub(pattern, r"", subtract_res)
                        if subtract_res == content and context["msg"].self_display_name:
                            # 前缀移除后没有变化，使用群昵称再次移除
                            pattern = f"@{re.escape(context['msg'].self_display_name)}(\u2005|\u0020)"
                            subtract_res = re.sub(pattern, r"", content)
                        content = subtract_res
                if not flag:
                    if context["origin_ctype"] == ContextType.VOICE:
                        logger.info("[chat_channel]receive group voice, but checkprefix didn't match")
                    return None
            else:  # 单聊
                nick_name = context["msg"].from_user_nickname
                if nick_name and nick_name in nick_name_black_list:
                    # 黑名单过滤
                    logger.warning(f"[chat_channel] Nickname '{nick_name}' in In BlackList, ignore")
                    return None

                match_prefix = check_prefix(content, conf().get("single_chat_prefix", [""]))
                if match_prefix is not None:  # 判断如果匹配到自定义前缀，则返回过滤掉前缀+空格后的内容
                    content = content.replace(match_prefix, "", 1).strip()
                elif context["origin_ctype"] == ContextType.VOICE:  # 如果源消息是私聊的语音消息，允许不匹配前缀，放宽条件
                    pass
                else:
                    logger.info("[chat_channel]receive single chat msg, but checkprefix didn't match")
                    return None
            content = content.strip()
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix",[""]))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            if "desire_rtype" not in context and conf().get("always_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        elif context.type == ContextType.VOICE:
            # Voice input replies with voice when either voice_reply_voice
            # (mirror voice) or the global always_reply_voice toggle is on.
            if (
                "desire_rtype" not in context
                and (conf().get("voice_reply_voice") or conf().get("always_reply_voice"))
                and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE
            ):
                context["desire_rtype"] = ReplyType.VOICE
        return context

    @traced('处理消息', 1)
    def _handle(self, context: Context):
        if context is None or not context.content:
            return
        # The single point where an inbound message is bound to an identity.
        # Everything below reads it from the ambient context instead of being
        # handed a workspace path.
        with use_identity(self._identity_for(context)):
            logger.debug("[chat_channel] handling context: {}".format(context))
            # reply的构建步骤
            reply = self._generate_reply(context)

            logger.debug("[chat_channel] decorating reply: {}".format(reply))

            # reply的包装步骤
            if reply and reply.content:
                reply = self._decorate_reply(context, reply)

                # reply的发送步骤
                self._send_reply(context, reply)
            elif context.type == ContextType.TEXT:
                self._empty_reply_fallback(context)

    def _identity_for(self, context: Context) -> RuntimeIdentity:
        """Resolve who this message is for.

        ``produce`` already routed the context, so the agent is read back here
        rather than resolved twice. Without this the bridge would serve the
        bound Agent while workspace paths still resolved to the default one.
        """
        return RuntimeIdentity(
            agent_id=context.get("agent_id"),
            session_id=context.get("session_id"),
            run_id=context.get('trace_id'),
        )

    def _generate_reply(self, context: Context, reply: Reply = Reply()) -> Reply:
        e_context = PluginManager().emit_event(
            EventContext(
                Event.ON_HANDLE_CONTEXT,
                {"channel": self, "context": context, "reply": reply},
            )
        )
        reply = e_context["reply"]
        if not e_context.is_pass():
            logger.debug("[chat_channel] type={}, content={}".format(context.type, context.content))
            if context.type == ContextType.TEXT or context.type == ContextType.IMAGE_CREATE:  # 文字和图片消息
                context["channel"] = e_context["channel"]
                reply = super().build_reply_content(context.content, context)
            elif context.type == ContextType.VOICE:  # 语音消息
                cmsg = context["msg"]
                cmsg.prepare()
                file_path = context.content
                wav_path = os.path.splitext(file_path)[0] + ".wav"
                try:
                    any_to_wav(file_path, wav_path)
                except Exception as e:  # 转换失败，直接使用mp3，对于某些api，mp3也可以识别
                    logger.warning("[chat_channel]any to wav error, use raw path. " + str(e))
                    wav_path = file_path
                # 语音识别
                reply = super().build_voice_to_text(wav_path)
                # 删除临时文件
                try:
                    os.remove(file_path)
                    if wav_path != file_path:
                        os.remove(wav_path)
                except Exception as e:
                    pass
                    # logger.warning("[chat_channel]delete temp file error: " + str(e))

                if reply.type == ReplyType.TEXT:
                    new_context = self._compose_context(ContextType.TEXT, reply.content, **context.kwargs)
                    if new_context:
                        reply = self._generate_reply(new_context)
                    else:
                        return
            elif context.type == ContextType.IMAGE:  # 图片消息，当前仅做下载保存到本地的逻辑
                memory.USER_IMAGE_CACHE[context["session_id"]] = {
                    "path": context.content,
                    "msg": context.get("msg")
                }
            elif context.type == ContextType.SHARING:  # 分享信息，当前无默认逻辑
                pass
            elif context.type == ContextType.FUNCTION or context.type == ContextType.FILE:  # 文件消息及函数调用等，当前无默认逻辑
                pass
            else:
                logger.warning("[chat_channel] unknown context type: {}".format(context.type))
                return
        return reply

    def _decorate_reply(self, context: Context, reply: Reply) -> Reply:
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_DECORATE_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            desire_rtype = context.get("desire_rtype")
            if not e_context.is_pass() and reply and reply.type:
                if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                    logger.error("[chat_channel]reply type not support: " + str(reply.type))
                    reply.type = ReplyType.ERROR
                    reply.content = _t("不支持发送的消息类型: ", "Unsupported message type: ") + str(reply.type)

                if reply.type == ReplyType.TEXT:
                    reply_text = reply.content
                    if desire_rtype == ReplyType.VOICE and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                        # Preserve original text for the "text-then-voice" pattern in _send_reply.
                        context["voice_reply_text"] = reply.content
                        reply = super().build_text_to_voice(reply.content)
                        return self._decorate_reply(context, reply)
                    if context.get("isgroup", False):
                        if not context.get("no_need_at", False):
                            reply_text = "@" + context["msg"].actual_user_nickname + "\n" + reply_text.strip()
                        reply_text = conf().get("group_chat_reply_prefix", "") + reply_text + conf().get("group_chat_reply_suffix", "")
                    else:
                        reply_text = conf().get("single_chat_reply_prefix", "") + reply_text + conf().get("single_chat_reply_suffix", "")
                    reply.content = reply_text
                elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
                    reply.content = "[" + str(reply.type) + "]\n" + reply.content
                elif reply.type == ReplyType.IMAGE_URL or reply.type == ReplyType.VOICE or reply.type == ReplyType.IMAGE or reply.type == ReplyType.FILE or reply.type == ReplyType.VIDEO or reply.type == ReplyType.VIDEO_URL:
                    pass
                else:
                    logger.error("[chat_channel] unknown reply type: {}".format(reply.type))
                    return
            if desire_rtype and desire_rtype != reply.type and reply.type not in [ReplyType.ERROR, ReplyType.INFO]:
                logger.warning("[chat_channel] desire_rtype: {}, but reply type: {}".format(context.get("desire_rtype"), reply.type))
            return reply

    def _send_reply(self, context: Context, reply: Reply):
        if reply and reply.type:
            try:
                if reply.type == ReplyType.TEXT:
                    from agent.protocol.agent_stream import _try_fast_recover_final_response
                    recovered = _try_fast_recover_final_response(
                        str(context.content or ""),
                        str(reply.content or ""),
                    )
                    if recovered:
                        logger.info("[chat_channel] final outgoing reply recovered by fast query")
                        try:
                            _write_direct_fast_debug(
                                "chat_channel_outgoing_recovery_hit",
                                content=str(context.content or "")[:200],
                                old_response=str(reply.content or "")[:200],
                                preview=recovered[:200],
                                channel_type=context.get("channel_type") or self.channel_type,
                                session_id=context.get("session_id"),
                            )
                        except Exception:
                            pass
                        reply.content = recovered
            except Exception as recovery_error:
                logger.debug(f"[chat_channel] outgoing fast recovery skipped: {recovery_error}")
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_SEND_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            if not e_context.is_pass() and reply and reply.type:
                logger.debug("[chat_channel] sending reply: {}, context: {}".format(reply, context))
                
                # 如果是文本回复，尝试提取并发送图片
                # Web channel renders images/videos inline via renderMarkdown,
                # so skip the extract-and-send step to avoid duplicate media.
                if reply.type == ReplyType.TEXT and context.get("channel_type") != "web":
                    self._extract_and_send_images(reply, context)
                elif reply.type == ReplyType.TEXT:
                    self._send(reply, context)
                # 如果是图片回复但带有文本内容，先发文本再发图片
                elif reply.type == ReplyType.IMAGE_URL and hasattr(reply, 'text_content') and reply.text_content:
                    # 先发送文本
                    text_reply = Reply(ReplyType.TEXT, reply.text_content)
                    self._send(text_reply, context)
                    # 短暂延迟后发送图片
                    time.sleep(0.3)
                    self._send(reply, context)
                # Send text bubble before voice, unless channel already streamed
                # the text (feishu) or natively renders STT under the voice (wechatcom).
                elif reply.type == ReplyType.VOICE and context.get("voice_reply_text") \
                        and not context.get("feishu_streamed") \
                        and context.get("channel_type") not in ("wechatcom_app",):
                    text_reply = Reply(ReplyType.TEXT, context.get("voice_reply_text"))
                    self._send(text_reply, context)
                    time.sleep(0.3)
                    self._send(reply, context)
                else:
                    self._send(reply, context)

                # One agent turn can produce several files (e.g. a web page plus
                # a document). Only the first rides in `reply`; the rest follow
                # as their own messages so none are silently dropped.
                for extra in getattr(reply, "extra_replies", None) or []:
                    time.sleep(0.3)
                    logger.debug("[chat_channel] sending extra reply: {}".format(extra))
                    self._send(extra, context)

    def _extract_and_send_images(self, reply: Reply, context: Context):
        """
        从文本回复中提取图片/视频URL并单独发送
        支持格式：[图片: /path/to/image.png], [视频: /path/to/video.mp4], ![](url), <img src="url">
        最多发送5个媒体文件
        """
        content = reply.content
        media_items = []  # [(url, type), ...]
        
        # 正则提取各种格式的媒体URL
        patterns = [
            (r'\[图片:\s*([^\]]+)\]', 'image'),   # [图片: /path/to/image.png]
            (r'\[视频:\s*([^\]]+)\]', 'video'),   # [视频: /path/to/video.mp4]
            (r'!\[.*?\]\(([^\)]+)\)', 'image'),   # ![alt](url) - 默认图片
            (r'<img[^>]+src=["\']([^"\']+)["\']', 'image'),  # <img src="url">
            (r'<video[^>]+src=["\']([^"\']+)["\']', 'video'),  # <video src="url">
            (r'https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp)', 'image'),  # 直接的图片URL
            (r'https?://[^\s]+\.(?:mp4|avi|mov|wmv|flv)', 'video'),  # 直接的视频URL
        ]
        
        for pattern, media_type in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                media_items.append((match, media_type))
        
        # 去重（保持顺序）并限制最多5个
        seen = set()
        unique_items = []
        for url, mtype in media_items:
            if url not in seen:
                seen.add(url)
                unique_items.append((url, mtype))
        media_items = unique_items[:5]
        
        if media_items:
            logger.info(f"[chat_channel] Extracted {len(media_items)} media item(s) from reply")
            
            # Send text first (the frontend will embed video players via renderMarkdown).
            logger.info(f"[chat_channel] Sending text content before media: {reply.content[:100]}...")
            self._send(reply, context)
            logger.info(f"[chat_channel] Text sent, now sending {len(media_items)} media item(s)")
            
            for i, (url, media_type) in enumerate(media_items):
                try:
                    # Determine whether it is a remote URL or a local file.
                    if url.startswith(('http://', 'https://')):
                        if media_type == 'video':
                            media_reply = Reply(ReplyType.FILE, url)
                            media_reply.file_name = os.path.basename(url)
                        else:
                            media_reply = Reply(ReplyType.IMAGE_URL, url)
                    elif os.path.exists(url):
                        if media_type == 'video':
                            media_reply = Reply(ReplyType.FILE, f"file://{url}")
                            media_reply.file_name = os.path.basename(url)
                        else:
                            media_reply = Reply(ReplyType.IMAGE_URL, f"file://{url}")
                    else:
                        logger.warning(f"[chat_channel] Media file not found or invalid URL: {url}")
                        continue
                    
                    if i > 0:
                        time.sleep(0.5)
                    self._send(media_reply, context)
                    logger.info(f"[chat_channel] Sent {media_type} {i+1}/{len(media_items)}: {url[:50]}...")
                    
                except Exception as e:
                    logger.error(f"[chat_channel] Failed to send {media_type} {url}: {e}")
        else:
            # 没有媒体文件，正常发送文本
                self._send(reply, context)

    def _send(self, reply: Reply, context: Context, retry_cnt=0):
        try:
            sent = self.send(reply, context)
            emit_trace(context, '发送回复', 'success' if sent is True else 'failed' if sent is False else 'unconfirmed',
                       notification_written=sent if isinstance(sent, bool) else None)
        except Exception as e:
            emit_trace(context, '发送回复', 'failed', notification_written=False)
            logger.error("[chat_channel] sendMsg error: {}".format(str(e)))
            try:
                channel_type = context.get("channel_type") or self.channel_type
                if channel_type in ("weixin", "wechatcom_app", "wecom_bot"):
                    from channel.weixin.failure_learning import record_weixin_failure
                    record_weixin_failure(
                        command=str(context.content or ""),
                        category="send_failure",
                        message=str(e),
                        route=f"{channel_type}:send_reply",
                    )
            except Exception:
                pass
            if isinstance(e, NotImplementedError):
                return
            logger.exception(e)
            if retry_cnt < 2:
                time.sleep(3 + 3 * retry_cnt)
                self._send(reply, context, retry_cnt + 1)

    def _success_callback(self, session_id, **kwargs):  # 线程正常结束时的回调函数
        logger.debug("Worker return success, session_id = {}".format(session_id))

    def _fail_callback(self, session_id, exception, **kwargs):  # 线程异常结束时的回调函数
        logger.exception("Worker return exception: {}".format(exception))
        context = kwargs.get("context")
        if context is None:
            return
        try:
            reason = re.sub(r"\s+", " ", str(exception or "")).strip()
            if len(reason) > 180:
                reason = reason[:180] + "…"
            text = (
                "我已收到这条消息，但执行过程中断了。\n"
                f"原因：{reason or '本地服务异常'}\n"
                "你可以直接重发一次；如果连续失败，我会优先走可用的兜底路径。"
            )
            self._send_reply(context, Reply(ReplyType.TEXT, text))
            channel_type = context.get("channel_type") or self.channel_type
            if channel_type in ("web", "weixin", "wechatcom_app", "wecom_bot"):
                try:
                    from channel.weixin.failure_learning import record_weixin_failure
                    record_weixin_failure(
                        command=str(context.content or ""),
                        category="worker_failure",
                        message=reason,
                        route=f"{channel_type}:model_pipeline",
                    )
                except Exception:
                    pass
            try:
                from config import conf
                if conf().get("conversation_persistence", True):
                    from agent.memory import get_conversation_store
                    get_conversation_store().append_messages(
                        context.get("session_id") or session_id,
                        [{"role": "assistant", "content": text, "extras": {"source": "worker_failure_fallback"}}],
                        channel_type=context.get("channel_type") or self.channel_type,
                    )
            except Exception as persist_error:
                logger.warning(f"[chat_channel] failed to persist worker failure: {persist_error}")
        except Exception as send_error:
            logger.warning(f"[chat_channel] failed to send worker failure fallback: {send_error}")

    def _empty_reply_fallback(self, context: Context):
        """Send a readable fallback when the worker finishes with no content.

        This is especially important for long-running remote channels such as
        WeChat: a silent completion feels like the Agent stopped thinking.
        """
        try:
            channel_type = context.get("channel_type") or self.channel_type
            if channel_type in ("weixin", "wechatcom_app", "wecom_bot"):
                try:
                    from agent.fast_queries import try_fast_query
                    fast_reply = try_fast_query(str(context.content or "").strip())
                    if fast_reply:
                        fast_reply = str(fast_reply).strip()
                        if fast_reply:
                            self._send_reply(context, Reply(ReplyType.TEXT, fast_reply))
                            self._persist_direct_fast_reply(
                                context,
                                str(context.content or "").strip(),
                                fast_reply,
                                channel_type,
                            )
                            try:
                                from channel.weixin.failure_learning import record_weixin_success
                                record_weixin_success(
                                    command=str(context.content or ""),
                                    task_type=self._classify_direct_fast_task(str(context.content or "")),
                                    action="empty_reply_fast_recovery",
                                    route=f"{channel_type}:empty_reply_fast_recovery",
                                    evidence=fast_reply[:120],
                                )
                            except Exception:
                                pass
                            logger.info("[chat_channel] empty reply recovered by direct fast query")
                            return
                except Exception as fast_error:
                    logger.warning(f"[chat_channel] empty reply fast recovery skipped: {fast_error}")
            text = (
                "我已尝试处理这条消息，但没有生成可发送的结果。\n"
                "我会把这次空回复记下来；你可以直接重发，或换成更明确的目标，例如“查今天邮件并列出前5封”。"
            )
            self._send_reply(context, Reply(ReplyType.TEXT, text))
            if channel_type in ("weixin", "wechatcom_app", "wecom_bot"):
                try:
                    from channel.weixin.failure_learning import record_weixin_failure
                    record_weixin_failure(
                        command=str(context.content or ""),
                        category="empty_reply",
                        message="没有生成可发送的结果",
                        route=f"{channel_type}:model_pipeline",
                    )
                except Exception:
                    pass
            try:
                from config import conf
                if conf().get("conversation_persistence", True):
                    from agent.memory import get_conversation_store
                    get_conversation_store().append_messages(
                        context.get("session_id") or "",
                        [{"role": "assistant", "content": text, "extras": {"source": "empty_reply_fallback"}}],
                        channel_type=context.get("channel_type") or self.channel_type,
                    )
            except Exception as persist_error:
                logger.warning(f"[chat_channel] failed to persist empty reply fallback: {persist_error}")
        except Exception as send_error:
            logger.warning(f"[chat_channel] failed to send empty reply fallback: {send_error}")

    @traced('快速路由检查', 1)
    def _try_direct_fast_reply(self, context: Context) -> bool:
        """Answer high-confidence remote chat requests before the model path.

        WeChat/WeCom are often used as lightweight command channels.  If the
        model auth is missing on a newly installed machine, simple tasks like
        greetings, weather, email, files and forex should still try the local
        deterministic tools first instead of falling through to a generic
        "unable to reply" message.
        """
        try:
            if context.type != ContextType.TEXT:
                _write_direct_fast_debug(
                    "chat_direct_fast_skip",
                    reason="type_mismatch",
                    context_type=str(context.type),
                    expected=str(ContextType.TEXT),
                    content=str(getattr(context, "content", ""))[:120],
                    channel_type=context.get("channel_type") or self.channel_type,
                )
                return False
            if context.get("is_scheduled_task"):
                _write_direct_fast_debug(
                    "chat_direct_fast_skip",
                    reason="scheduled_task",
                    content=str(context.content or "")[:120],
                    channel_type=context.get("channel_type") or self.channel_type,
                )
                return False
            text = str(context.content or "").strip()
            if not text:
                _write_direct_fast_debug(
                    "chat_direct_fast_skip",
                    reason="empty_text",
                    channel_type=context.get("channel_type") or self.channel_type,
                )
                return False
            channel_type = context.get("channel_type") or self.channel_type
            if channel_type not in ("web", "weixin", "wechatcom_app", "wecom_bot"):
                _write_direct_fast_debug(
                    "chat_direct_fast_skip",
                    reason="channel_not_enabled",
                    content=text[:120],
                    channel_type=channel_type,
                )
                return False

            productivity_reply = self._try_personal_productivity_command(context, text, channel_type)
            if productivity_reply:
                self._send_reply(context, Reply(ReplyType.TEXT, productivity_reply))
                self._persist_direct_fast_reply(context, text, productivity_reply, channel_type)
                _write_direct_fast_debug(
                    "chat_direct_fast_sent",
                    content=text[:200],
                    preview=productivity_reply[:200],
                    channel_type=channel_type,
                    session_id=context.get("session_id"),
                    route="personal_productivity",
                )
                return True

            from agent.fast_queries import try_fast_query

            _write_direct_fast_debug(
                "chat_direct_fast_try",
                content=text[:200],
                channel_type=channel_type,
                session_id=context.get("session_id"),
            )
            reply_text = try_fast_query(text)
            if reply_text is None:
                _write_direct_fast_debug(
                    "chat_direct_fast_skip",
                    reason="no_fast_reply",
                    content=text[:200],
                    channel_type=channel_type,
                    session_id=context.get("session_id"),
                )
                return False
            reply_text = str(reply_text).strip()
            if not reply_text:
                _write_direct_fast_debug(
                    "chat_direct_fast_skip",
                    reason="blank_fast_reply",
                    content=text[:200],
                    channel_type=channel_type,
                    session_id=context.get("session_id"),
                )
                return False

            self._send_reply(context, Reply(ReplyType.TEXT, reply_text))
            self._persist_direct_fast_reply(context, text, reply_text, channel_type)
            _write_direct_fast_debug(
                "chat_direct_fast_sent",
                content=text[:200],
                preview=reply_text[:200],
                channel_type=channel_type,
                session_id=context.get("session_id"),
            )
            try:
                from channel.weixin.failure_learning import record_weixin_success
                record_weixin_success(
                    command=text,
                    task_type=self._classify_direct_fast_task(text),
                    action="direct_fast_reply",
                    route=f"{channel_type}:direct_fast_query",
                    evidence=reply_text[:120],
                )
            except Exception:
                pass
            logger.info(
                "[chat_channel] direct fast reply delivered: channel=%s session=%s task=%s",
                channel_type,
                context.get("session_id"),
                self._classify_direct_fast_task(text),
            )
            return True
        except Exception as error:
            _write_direct_fast_debug(
                "chat_direct_fast_error",
                error=str(error),
                content=str(context.content or "")[:200],
                channel_type=context.get("channel_type") or self.channel_type,
                session_id=context.get("session_id"),
            )
            logger.warning(f"[chat_channel] direct fast reply skipped: {error}")
            try:
                if (context.get("channel_type") or self.channel_type) in ("web", "weixin", "wechatcom_app", "wecom_bot"):
                    from channel.weixin.failure_learning import record_weixin_failure
                    record_weixin_failure(
                        command=str(context.content or ""),
                        category="direct_fast_query_failed",
                        message=str(error),
                        route=f"{context.get('channel_type') or self.channel_type}:direct_fast_query",
                    )
            except Exception:
                pass
            return False

    def _persist_direct_fast_reply(self, context: Context, user_text: str, reply_text: str, channel_type: str) -> None:
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return
            from agent.memory import get_conversation_store
            session_id = context.get("session_id") or ""
            if not session_id:
                return
            get_conversation_store().append_messages(
                session_id,
                [
                    {"role": "user", "content": user_text, "extras": {"source": "direct_fast_query"}},
                    {"role": "assistant", "content": reply_text, "extras": {"source": "direct_fast_query"}},
                ],
                channel_type=channel_type,
            )
        except Exception as persist_error:
            logger.warning(f"[chat_channel] failed to persist direct fast reply: {persist_error}")

    def _try_personal_productivity_command(self, context: Context, text: str, channel_type: str) -> str:
        try:
            from config import get_data_root
            from plugins.intelligence_center.personal_productivity import PersonalProductivityService

            receiver = str(context.get("receiver") or context.get("session_id") or "").strip()
            if channel_type == "web" and not receiver:
                receiver = "web_user"
            service = PersonalProductivityService(os.path.join(get_data_root(), "intelligence_center"))
            result = service.try_handle_message(text, channel_type, receiver)
            if not result:
                return ""
            if result.get("action") == "reminder_created":
                reminder = result.get("reminder") or {}
                channel_label = {
                    "weixin": "微信",
                    "wechatcom_app": "企业微信",
                    "wecom_bot": "企业微信",
                    "web": "Web",
                }.get(channel_type, channel_type or "当前通道")
                return (
                    "已设置提醒 ✅\n\n"
                    f"提醒内容：{reminder.get('content') or reminder.get('title') or ''}\n"
                    f"提醒时间：{str(reminder.get('due_at') or reminder.get('schedule') or '').replace('T', ' ')}\n"
                    f"提醒通道：{channel_label}（同时保留 Web 兜底）\n"
                    f"任务 ID：{reminder.get('id') or ''}"
                )
            if result.get("action") == "todo_created":
                todo = result.get("todo") or {}
                return (
                    "已加入任务列表 ✅\n\n"
                    f"任务：{todo.get('content') or todo.get('title') or ''}\n"
                    f"状态：{todo.get('status_label') or '待办'}\n"
                    f"创建时间：{str(todo.get('created_at') or '').replace('T', ' ')}\n"
                    f"任务 ID：{todo.get('id') or ''}\n\n"
                    "你可以在「自动化 → 任务清单」里继续编辑、完成或归档。"
                )
            return ""
        except Exception as error:
            logger.warning("[chat_channel] personal productivity command skipped: %s", error)
            return ""

    @staticmethod
    def _classify_direct_fast_task(text: str) -> str:
        lowered = str(text or "").lower()
        if any(word in lowered for word in ("邮件", "邮箱", "mail", "email", "inbox")):
            return "email"
        if any(word in lowered for word in ("文件", "文档", "图片", "照片", "桌面", "下载")):
            return "file"
        if any(word in lowered for word in ("外汇", "汇率", "美元", "港币", "日元", "欧元", "英镑")):
            return "forex"
        if any(word in lowered for word in ("天气", "温度", "下雨")):
            return "weather"
        if any(word in lowered for word in ("几点", "时间", "日期", "星期几")):
            return "time"
        return "quick_reply"

    def _thread_pool_callback(self, session_id, **kwargs):
        def func(worker: Future):
            try:
                worker_exception = worker.exception()
                if worker_exception:
                    self._fail_callback(session_id, exception=worker_exception, **kwargs)
                else:
                    self._success_callback(session_id, **kwargs)
            except CancelledError as e:
                logger.info("Worker cancelled, session_id = {}".format(session_id))
            except Exception as e:
                logger.exception("Worker raise exception: {}".format(e))
            with self.lock:
                self.sessions[session_id][1].release()

        return func

    # Chat commands that must bypass the per-session serial queue,
    # otherwise /cancel would queue behind the task it tries to cancel.
    # Use /cancel (not /stop) to avoid colliding with `cow stop` CLI.
    _BYPASS_QUEUE_COMMANDS = ("/cancel",)

    def produce(self, context: Context):
        session_id = context["session_id"]
        _write_direct_fast_debug(
            "chat_produce_enter",
            content=str(context.content or "")[:200],
            context_type=str(context.type),
            expected_type=str(ContextType.TEXT),
            type_equal=bool(context.type == ContextType.TEXT),
            channel_type=context.get("channel_type") or self.channel_type,
            session_id=session_id,
        )
        if context.type == ContextType.TEXT and context.content:
            self._observe_explicit_user_memory(context, session_id)

            # High-confidence lightweight commands must run before Agent
            # routing.  The router may otherwise bind the message to a skill
            # path (for example skill:weather-query) and the model can still
            # return an empty response, producing the user-facing "无法生成回复"
            # despite a deterministic fast path being available.
            stripped = context.content.strip().lower()
            if stripped in self._BYPASS_QUEUE_COMMANDS:
                self._handle_cancel_command(context, session_id)
                return
            if re.match(r"^/steer(?:\s|$)", stripped):
                instruction = context.content.strip()[len("/steer"):].strip()
                self._handle_steer_command(context, session_id, instruction)
                return
            if self._try_direct_fast_reply(context):
                return

        try:
            from bridge.bridge import Bridge
            agent_bridge = Bridge().get_agent_bridge()
            agent_id = agent_bridge.route_context(context)
            queue_key = agent_bridge._cancel_key(
                agent_id, session_id, agent_bridge.agent_registry.default_agent_id
            )
        except AgentUnavailableError as e:
            # This conversation is bound to an agent that is off. Answering it
            # with the default agent would silently swap persona and memory, so
            # say so instead.
            logger.warning(f"[chat_channel] {e}")
            self._send_reply(context, Reply(
                ReplyType.TEXT,
                _t("该助手当前已停用，请联系管理员。",
                   "This assistant is currently disabled. Please contact an administrator."),
            ))
            return
        except Exception as e:
            logger.warning(f"[chat_channel] Agent route failed, using default: {e}")
            agent_id = None
            queue_key = session_id

        with self.lock:
            if queue_key not in self.sessions:
                self.sessions[queue_key] = [
                    Dequeue(),
                    threading.BoundedSemaphore(conf().get("concurrency_in_session", 1)),
                ]
            if context.type == ContextType.TEXT and context.content.startswith("#"):
                self.sessions[queue_key][0].putleft(context)  # 优先处理管理命令
            else:
                self.sessions[queue_key][0].put(context)

    def _observe_explicit_user_memory(self, context: Context, session_id: str) -> None:
        """Record explicit low-risk user preferences without blocking replies."""
        try:
            from config import get_data_root
            from intelligence_center.auto_memory import observe_user_message
            root = os.path.join(get_data_root(), "intelligence_center")
            observe_user_message(
                root,
                str(context.content or ""),
                source="用户消息",
                session_id=str(session_id or context.get("session_id") or ""),
                channel=str(context.get("channel_type") or self.channel_type or ""),
            )
        except Exception as error:
            logger.debug(f"[chat_channel] auto memory observe skipped: {error}")

    def _handle_cancel_command(self, context: Context, session_id: str) -> None:
        """Cancel any in-flight agent run for *session_id* and reply inline.

        Runs synchronously on the caller's thread. Reply is sent through
        _send_reply so plugins (e.g. logging) still observe it.
        """
        try:
            from agent.protocol import get_cancel_registry
            from bridge.reply import Reply, ReplyType

            from bridge.bridge import Bridge
            agent_bridge = Bridge().get_agent_bridge()
            agent_id = agent_bridge.route_context(context)
            scoped_session_id = agent_bridge._cancel_key(
                agent_id, session_id, agent_bridge.agent_registry.default_agent_id
            )
            cancelled = get_cancel_registry().cancel_session(scoped_session_id)
            text = (
                _t("🛑 已中止", "🛑 Cancelled")
                if cancelled > 0
                else _t("当前没有可中止的任务。", "Nothing to cancel.")
            )
            logger.info(
                f"[chat_channel] /cancel fast-path: session={session_id}, cancelled={cancelled}"
            )
            self._send_reply(context, Reply(ReplyType.TEXT, text))
        except Exception as e:
            logger.warning(f"[chat_channel] /cancel fast-path failed: {e}")

    def _handle_steer_command(
        self,
        context: Context,
        session_id: str,
        instruction: str,
    ) -> None:
        """Send explicit guidance to the active run without queueing it."""
        try:
            from agent.protocol import SteerStatus
            from bridge.bridge import Bridge

            agent_bridge = Bridge().get_agent_bridge()
            result = agent_bridge.steer_session(
                session_id, instruction, agent_bridge.route_context(context)
            )
            messages = {
                SteerStatus.ACCEPTED: _t(
                    "↪️ 已引导当前任务。", "↪️ Active task redirected."
                ),
                SteerStatus.INACTIVE: _t(
                    "当前没有可引导的任务。", "No active task to steer."
                ),
                SteerStatus.CLOSING: _t(
                    "当前任务已结束，无法再引导。", "The active task is already finishing."
                ),
                SteerStatus.AMBIGUOUS: _t(
                    "当前会话有多个任务在运行，无法确定引导目标。",
                    "Multiple tasks are active in this session; the steering target is ambiguous.",
                ),
                SteerStatus.FULL: _t(
                    "引导指令过多，请等待当前任务处理后再试。",
                    "Too many steering updates are pending; try again after the agent processes them.",
                ),
                SteerStatus.INVALID: _t(
                    "用法：/steer <引导指令>", "Usage: /steer <instruction>"
                ),
            }
            text = messages[result.status]
            logger.info(
                f"[chat_channel] /steer fast-path: session={session_id}, "
                f"status={result.status.value}"
            )
            self._send_reply(context, Reply(ReplyType.TEXT, text))
        except Exception as e:
            logger.warning(f"[chat_channel] /steer fast-path failed: {e}")

    # 消费者函数，单独线程，用于从消息队列中取出消息并处理
    def consume(self):
        while True:
            with self.lock:
                session_ids = list(self.sessions.keys())
            for session_id in session_ids:
                with self.lock:
                    context_queue, semaphore = self.sessions[session_id]
                if semaphore.acquire(blocking=False):  # 等线程处理完毕才能删除
                    if not context_queue.empty():
                        context = context_queue.get()
                        logger.debug("[chat_channel] consume context: {}".format(context))
                        future: Future = handler_pool.submit(self._handle, context)
                        future.add_done_callback(self._thread_pool_callback(session_id, context=context))
                        with self.lock:
                            if session_id not in self.futures:
                                self.futures[session_id] = []
                            self.futures[session_id].append(future)
                    elif semaphore._initial_value == semaphore._value + 1:  # 除了当前，没有任务再申请到信号量，说明所有任务都处理完毕
                        with self.lock:
                            self.futures[session_id] = [t for t in self.futures[session_id] if not t.done()]
                            assert len(self.futures[session_id]) == 0, "thread pool error"
                            del self.sessions[session_id]
                    else:
                        semaphore.release()
            time.sleep(0.2)

    def cancel_message(self, session_id: str, message_id: str):
        """Cancel one channel message without disturbing later queued work.

        Queued contexts are matched by their original channel message ID. An
        in-flight agent run is cancelled through the per-request token that the
        channel placed on the context before dispatch.
        """
        removed = 0
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None:
                context_queue = session[0]
                kept = []
                for _ in range(context_queue.qsize()):
                    context = context_queue.get_nowait()
                    context_queue.task_done()
                    message = context.get("msg") if context is not None else None
                    if getattr(message, "msg_id", None) == message_id:
                        removed += 1
                    else:
                        kept.append(context)
                for context in kept:
                    context_queue.put(context)

        from agent.protocol import get_cancel_registry

        active = get_cancel_registry().cancel_request(message_id)
        logger.info(
            "[chat_channel] message recall: session=%s, message=%s, queued=%s, active=%s",
            session_id,
            message_id,
            removed,
            active,
        )
        return removed, active

    # 取消session_id对应的所有任务，只能取消排队的消息和已提交线程池但未执行的任务
    def cancel_session(self, session_id):
        with self.lock:
            if session_id in self.sessions:
                # futures[session_id] is only created in consume() when a task is
                # dispatched, so it may be absent if cancel happens right after
                # produce() but before the first dispatch. Default to [].
                for future in self.futures.get(session_id, []):
                    future.cancel()
                cnt = self.sessions[session_id][0].qsize()
                if cnt > 0:
                    logger.info("Cancel {} messages in session {}".format(cnt, session_id))
                self.sessions[session_id][0] = Dequeue()

    def cancel_all_session(self):
        with self.lock:
            for session_id in self.sessions:
                for future in self.futures.get(session_id, []):
                    future.cancel()
                cnt = self.sessions[session_id][0].qsize()
                if cnt > 0:
                    logger.info("Cancel {} messages in session {}".format(cnt, session_id))
                self.sessions[session_id][0] = Dequeue()


def check_prefix(content, prefix_list):
    if not prefix_list:
        return None
    for prefix in prefix_list:
        if content.startswith(prefix):
            return prefix
    return None


def check_contain(content, keyword_list):
    if not keyword_list:
        return None
    for ky in keyword_list:
        if content.find(ky) != -1:
            return True
    return None
