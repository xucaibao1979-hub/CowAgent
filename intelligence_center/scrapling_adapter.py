"""Optional Scrapling integration for public webpage extraction.

The adapter is intentionally conservative:
- it never sends cookies/tokens or login state;
- it validates outbound URLs with the existing safe-network policy;
- it works as an optional enhancement and reports clear fallback reasons.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
from dataclasses import dataclass, asdict
from typing import Any

from intelligence_center.safe_network import validate_outbound_url


@dataclass
class ScraplingExtractResult:
    ok: bool
    url: str = ""
    title: str = ""
    source: str = ""
    author: str = ""
    published_at: str = ""
    text: str = ""
    markdown: str = ""
    extractor: str = "scrapling"
    strategy: str = "fetcher"
    message: str = ""
    error: str = ""
    dependency: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["word_count"] = len(self.text or "")
        return data


def scrapling_status() -> dict:
    """Return a small health payload for UI and health-center reuse."""
    spec = importlib.util.find_spec("scrapling")
    installed = spec is not None
    payload = {
        "id": "scrapling",
        "name": "Scrapling 网页提取",
        "installed": installed,
        "ready": installed,
        "browser_ready": False,
        "mcp_ready": False,
        "message": "Scrapling 可用于普通网页正文提取" if installed else "未安装 Scrapling，网页提取会使用内置降级解析",
        "install_hint": "建议先安装基础包：pip install scrapling；浏览器/stealth/MCP 后续按需启用。",
        "risk_note": "默认不读取 Cookie、账号密码或登录页面；反爬/浏览器能力需要进入权限中心审批。",
    }
    if not installed:
        return payload
    try:
        module = importlib.import_module("scrapling")
        payload["version"] = str(getattr(module, "__version__", "") or "")
        payload["has_fetcher"] = hasattr(module, "Fetcher")
        payload["ready"] = bool(payload["has_fetcher"])
        if not payload["ready"]:
            payload["message"] = "Scrapling 已安装，但未检测到 Fetcher 接口，建议检查版本。"
        try:
            importlib.import_module("scrapling.fetchers")
            payload["browser_ready"] = True
        except Exception:
            payload["browser_ready"] = False
        try:
            importlib.import_module("scrapling_mcp")
            payload["mcp_ready"] = True
        except Exception:
            payload["mcp_ready"] = False
    except Exception as error:
        payload["ready"] = False
        payload["message"] = f"Scrapling 检测失败：{error}"
    return payload


def extract_public_article(url: str, *, timeout: int = 18) -> ScraplingExtractResult:
    """Extract a public article with Scrapling when available.

    Raises no exception for missing Scrapling; callers can simply fall back to
    their existing parser when ``ok`` is False.
    """
    url = validate_outbound_url(str(url or "").strip(), allow_private=False)
    status = scrapling_status()
    if not status.get("ready"):
        return ScraplingExtractResult(
            ok=False,
            url=url,
            message=status.get("message") or "Scrapling 不可用",
            error="scrapling_unavailable",
            dependency=status,
        )
    try:
        scrapling = importlib.import_module("scrapling")
        fetcher = getattr(scrapling, "Fetcher", None)
        if not fetcher:
            return ScraplingExtractResult(ok=False, url=url, message="Scrapling Fetcher 不可用", error="missing_fetcher", dependency=status)
        page = _call_fetcher(fetcher, url, timeout=timeout)
        title = _extract_title(page)
        text = _extract_text(page)
        markdown = _extract_markdown(page, title=title, text=text)
        if len(text) < 80 and markdown:
            text = _markdown_to_text(markdown)
        if len(text) < 80:
            return ScraplingExtractResult(
                ok=False,
                url=url,
                title=title,
                text=text,
                markdown=markdown,
                message="Scrapling 未提取到足够正文，已准备降级到内置解析",
                error="content_too_short",
                dependency=status,
            )
        return ScraplingExtractResult(
            ok=True,
            url=url,
            title=title,
            source=_extract_source(page, url),
            author=_extract_meta(page, ("author", "article:author", "og:article:author")),
            published_at=_extract_meta(page, ("article:published_time", "pubdate", "publishdate", "publish_date", "date", "weixin:public_time")),
            text=_clean_text(text)[:30000],
            markdown=markdown[:60000],
            message="Scrapling 提取成功",
            dependency=status,
        )
    except Exception as error:
        return ScraplingExtractResult(
            ok=False,
            url=url,
            message="Scrapling 提取失败，已准备降级到内置解析",
            error=str(error)[:500],
            dependency=status,
        )


def _call_fetcher(fetcher: Any, url: str, *, timeout: int) -> Any:
    get = getattr(fetcher, "get", None)
    if not callable(get):
        raise RuntimeError("Scrapling Fetcher.get 不可用")
    attempts = (
        {"timeout": timeout, "stealthy_headers": True, "proxy": None, "proxies": {}},
        {"timeout": timeout, "proxy": None, "proxies": {}},
        {"timeout": timeout},
    )
    last_error: Exception | None = None
    proxy_env_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    old_proxy_env = {key: os.environ.get(key) for key in proxy_env_keys}
    try:
        for key in proxy_env_keys:
            os.environ.pop(key, None)
        for kwargs in attempts:
            try:
                return get(url, **kwargs)
            except TypeError as error:
                last_error = error
                continue
    finally:
        for key, value in old_proxy_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if last_error:
        raise last_error
    raise RuntimeError("Scrapling Fetcher.get 未能在限定时间内完成")


def _safe_value(value: Any) -> str:
    try:
        if callable(value):
            value = value()
    except TypeError:
        return ""
    except Exception:
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _page_attr(page: Any, names: tuple[str, ...]) -> str:
    for name in names:
        if hasattr(page, name):
            value = _safe_value(getattr(page, name))
            if value:
                return value
    return ""


def _selector_text(page: Any, selectors: tuple[str, ...]) -> str:
    for method in ("css", "xpath"):
        fn = getattr(page, method, None)
        if not callable(fn):
            continue
        for selector in selectors:
            try:
                result = fn(selector)
            except Exception:
                continue
            for candidate in (result, getattr(result, "first", None), result[0] if hasattr(result, "__len__") and len(result) else None):
                value = _page_attr(candidate, ("text", "get_text", "text_content")) if candidate is not None else ""
                if value:
                    return value
    return ""


def _extract_title(page: Any) -> str:
    return _clean_text(_page_attr(page, ("title", "page_title")) or _selector_text(page, ("title", "//title"))).splitlines()[0][:160] if page else ""


def _extract_source(page: Any, url: str) -> str:
    meta = _extract_meta(page, ("og:site_name", "application-name"))
    if meta:
        return meta
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return ""


def _extract_meta(page: Any, names: tuple[str, ...]) -> str:
    meta = getattr(page, "meta", None)
    if isinstance(meta, dict):
        for name in names:
            value = meta.get(name) or meta.get(name.replace(":", "_"))
            if value:
                return _clean_text(str(value))[:200]
    html = _page_attr(page, ("html", "html_content", "content", "body"))
    if not html:
        return ""
    import html as html_lib
    for name in names:
        patterns = [
            rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.I)
            if match:
                return _clean_text(html_lib.unescape(match.group(1)))[:200]
    return ""


def _extract_text(page: Any) -> str:
    value = _page_attr(page, ("text", "body_text", "get_text", "text_content"))
    if value:
        return _clean_text(value)
    selected = _selector_text(page, ("article", "main", "#js_content", ".rich_media_content", ".article", ".content", ".post", ".entry"))
    if selected:
        return _clean_text(selected)
    html = _page_attr(page, ("html", "html_content", "content", "body"))
    return _clean_html_text(html)


def _extract_markdown(page: Any, *, title: str, text: str) -> str:
    value = _page_attr(page, ("markdown", "to_markdown", "md"))
    if value and len(value) > 80:
        return value.strip()
    lines = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for para in [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]:
        if len(para) <= 80 and re.search(r"[:：]$", para):
            lines.append(f"## {para.rstrip('：:')}")
        else:
            lines.append(para)
        lines.append("")
    return "\n".join(lines).strip()


def _clean_html_text(value: str) -> str:
    import html as html_lib
    value = re.sub(r"<script[\s\S]*?</script>", " ", value or "", flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return _clean_text(html_lib.unescape(value))


def _markdown_to_text(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value or "")
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"^#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"[*_`>\-]+", " ", value)
    return _clean_text(value)


def _clean_text(value: str) -> str:
    text = re.sub(r"[\u200b\ufeff\xa0]+", " ", value or "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    drop = ("点击上方蓝字关注", "扫码关注", "商务合作", "免责声明", "阅读原文", "分享、点赞、在看", "喜欢此内容的人还喜欢", "转载授权")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or any(token in stripped for token in drop):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()
