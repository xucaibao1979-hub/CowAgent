"""Conservative automatic memory extraction.

This module only captures explicit user preferences/rules.  It intentionally
avoids credentials, one-off facts and ambiguous inferences.
"""
from __future__ import annotations

import re
from pathlib import Path

from .fact_store import get_fact_store


SENSITIVE_RE = re.compile(
    r"(密码|口令|验证码|token|secret|api[_-]?key|sk-[A-Za-z0-9]|身份证|银行卡|cookie|authorization)",
    re.I,
)

PREFERENCE_HINTS = (
    "以后", "后续", "以后所有", "后面", "默认", "固定", "统一", "优先",
    "我希望", "我需要", "不要", "必须", "都按", "按这种", "按照这种",
)

DOMAIN_HINTS = (
    "格式", "简报", "输出", "链接", "自动化", "定时", "通知", "微信", "企业微信",
    "邮件", "搜索", "学习", "产物", "交付物", "权限", "审批", "来源", "标签",
)


def extract_explicit_memories(text: str) -> list[dict]:
    """Return low-risk memory candidates from explicit user instructions."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) < 8 or len(value) > 2000:
        return []
    if SENSITIVE_RE.search(value):
        return []
    if not any(hint in value for hint in PREFERENCE_HINTS):
        return []
    if not any(hint in value for hint in DOMAIN_HINTS):
        return []

    sentences = re.split(r"(?<=[。！？!?])\s+|[\n\r]+", value)
    candidates = []
    for sentence in sentences:
        sentence = sentence.strip(" ，。；;")
        if len(sentence) < 8:
            continue
        if SENSITIVE_RE.search(sentence):
            continue
        if any(hint in sentence for hint in PREFERENCE_HINTS) and any(hint in sentence for hint in DOMAIN_HINTS):
            candidates.append(sentence)
    if not candidates:
        candidates = [value[:240]]
    return [_normalize_candidate(item) for item in candidates[:5]]


def observe_user_message(root: str | Path, text: str, *, source: str = "chat", session_id: str = "", channel: str = "") -> list[dict]:
    """Persist explicit preferences into fact_store.

    This function is safe to call from hot paths: it catches all persistence
    errors and returns an empty list when nothing should be learned.
    """
    try:
        candidates = extract_explicit_memories(text)
        if not candidates:
            return []
        store = get_fact_store(root)
        saved = []
        normalized_channel = str(channel or source or "").strip().lower()
        source_type = "user_message"
        if "wecom" in normalized_channel or "企业微信" in normalized_channel:
            source_type = "wecom_message"
        elif "weixin" in normalized_channel or "微信" in normalized_channel:
            source_type = "weixin_message"
        source_id = str(session_id or "").strip() or f"{source}:{channel}".strip(":")
        for item in candidates:
            saved.append(store.upsert(
                content=item["content"],
                fact_type=item["fact_type"],
                title=item["title"],
                source=source,
                source_url="",
                tags=item["tags"] + ([channel] if channel else []),
                scope="private",
                confidence=0.82,
                metadata={
                    "source_type": source_type,
                    "source_id": source_id,
                    "artifact_id": "",
                    "run_id": "",
                    "session_id": session_id,
                    "channel": channel,
                    "source": source,
                    "extractor": "explicit_rule_v1",
                },
            ))
        return [{"id": fact.get("id"), "type": fact.get("fact_type"), "content": fact.get("content")} for fact in saved if fact]
    except Exception:
        return []


def _normalize_candidate(sentence: str) -> dict:
    fact_type = "preference"
    tags = ["自动提取", "用户偏好"]
    title = "用户偏好"
    if any(token in sentence for token in ("格式", "简报", "输出", "链接")):
        fact_type = "output_preference"
        tags.append("输出格式")
        title = "输出偏好"
    elif any(token in sentence for token in ("搜索", "来源", "标签")):
        fact_type = "source_preference"
        tags.append("信息来源")
        title = "来源偏好"
    elif any(token in sentence for token in ("通知", "微信", "企业微信", "webhook", "邮件")):
        fact_type = "notification_preference"
        tags.append("通知偏好")
        title = "通知偏好"
    elif any(token in sentence for token in ("权限", "审批", "授权")):
        fact_type = "approval_preference"
        tags.append("权限边界")
        title = "审批偏好"
    content = sentence
    if not re.match(r"^(用户偏好|输出偏好|来源偏好|通知偏好|审批偏好)：", content):
        content = f"{title}：{content}"
    return {"fact_type": fact_type, "title": title, "content": content, "tags": tags}
