"""Small, local-first result compaction helpers for Agent outputs.

The goal is not to rewrite content with another model.  It safely trims noisy
tool output before it is shown to users or passed into later model steps.
"""
from __future__ import annotations

import re
from typing import Iterable


_NOISE_PATTERNS = (
    r"\[A [^\]]+ Description automatically generated[^\n]*",
    r"Previous results:[\s\S]*$",
    r"Commercial Specialist[\s\S]*$",
    r"@media only screen[\s\S]*$",
    r"\*\{\s*padding:0;\s*margin:0;\s*\}[\s\S]*$",
)


def compact_text(text: str, min_chars: int = 30, max_chars: int = 80) -> str:
    """Return a concise, human-readable snippet without signatures/noise."""
    value = str(text or "").replace("\ufeff", "").replace("\u200b", "")
    for pattern in _NOISE_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.I)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -—:：|｜\n\t")
    value = re.sub(r"^(hi|hello)\s+bob[,，]?\s*", "", value, flags=re.I).strip()
    value = re.sub(r"(谢谢|謝謝|thanks)[~～,，。\s]*(patricia|regards|best regards|sent from).*$", r"\1", value, flags=re.I)
    if not value:
        return "暂无可提炼正文，请打开来源核对。"
    if len(value) <= max_chars:
        return value
    cut = value[:max_chars].rstrip("，。、；;,. ")
    if len(cut) < min_chars and len(value) > min_chars:
        cut = value[:min_chars].rstrip("，。、；;,. ")
    return cut + "…"


def compact_bullets(items: Iterable[dict], title_key: str = "title", summary_key: str = "summary", limit: int = 10) -> list[dict]:
    """Normalize a list of result-like dictionaries into compact cards."""
    output = []
    for item in list(items or [])[: max(1, int(limit or 10))]:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get(title_key) or item.get("subject") or item.get("name") or "未命名").strip())
        summary = compact_text(str(item.get(summary_key) or item.get("body") or item.get("content") or ""), 30, 80)
        url = str(item.get("url") or item.get("link") or item.get("source_url") or "").strip()
        output.append({**item, "title": title[:80], "summary": summary, "url": url})
    return output
