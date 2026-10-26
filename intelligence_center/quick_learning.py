"""Quick learning agent for public articles and WeChat official-account links."""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Article:
    url: str
    title: str
    source: str
    author: str
    published_at: str
    content: str
    markdown: str = ""
    extractor: str = "builtin"
    extractor_message: str = ""


class QuickLearningService:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.report_root = self.root / "reports"

    def learn(self, payload: dict) -> dict:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("请先粘贴公众号文章或网页链接")
        started_at = time.time()
        article = self.fetch_article(url)
        if len(article.content) < 80:
            raise ValueError("没有提取到足够正文，建议复制文章正文后再让 Agent 学习")
        modes = payload.get("modes") or ["summary", "notes", "actions"]
        timings = {"fetch_seconds": round(time.time() - started_at, 2)}
        note_started = time.time()
        note = self.build_note(article, modes=modes, custom_goal=str(payload.get("goal") or "").strip())
        timings["note_seconds"] = round(time.time() - note_started, 2)
        artifact = None
        if payload.get("save_artifact", True):
            artifact_started = time.time()
            artifact = self.save_artifact(article, note)
            timings["artifact_seconds"] = round(time.time() - artifact_started, 2)
        facts = []
        fact_status = "queued"
        self.save_learning_facts_async(article, note, artifact)
        return {
            "article": {
                "url": article.url,
                "title": article.title,
                "source": article.source,
                "author": article.author,
                "published_at": article.published_at,
                "word_count": len(article.content),
                "extractor": article.extractor,
                "extractor_message": article.extractor_message,
            },
            "summary_note": self.build_summary_note(article, note),
            "note": note,
            "facts": facts,
            "fact_status": fact_status,
            "artifact": artifact,
            "timings": timings,
            "message": "学习笔记已生成" + ("，并保存到产物/交付物" if artifact else ""),
        }

    def build_summary_note(self, article: Article, full_note: str) -> str:
        try:
            from agent.output_compactor import compact_text
        except Exception:
            compact_text = lambda value, min_chars=30, max_chars=80: str(value or "").strip()[:max_chars]
        topic = self._section_text(full_note, ("## 一句话结论", "## 这篇文章在讲什么", "## 一、这篇文章在讲什么", "## 一、核心结论"), limit=180)
        learn = self._section_items(full_note, ("## 我应该学什么", "## 二、最值得学习的内容", "## 二、重点知识"), limit=3)
        reuse = self._section_items(full_note, ("## 我可以怎么复用", "## 三、可以复用到我的 Agent 的做法", "## 三、值得学习的方法"), limit=3)
        actions = self._section_items(full_note, ("## 立即行动", "## 五、下一步可执行动作"), limit=3)
        lines = [
            f"快速学习｜{article.title}",
            f"来源：{article.source}" + (f"｜时间：{article.published_at}" if article.published_at else ""),
            f"原文：[查看原文]({article.url})",
            "",
            "一、这篇文章的核心",
        ]
        lines.append(compact_text(topic, 40, 120) if topic else "这篇文章围绕一个可落地的实践主题展开，重点说明问题背景、解决思路和可复用经验。")
        if learn:
            lines += ["", "二、我应该学什么"]
            lines.extend(f"- {compact_text(str(item).lstrip('- '), 30, 90)}" for item in learn)
        if reuse:
            lines += ["", "三、我可以怎么复用"]
            lines.extend(f"- {compact_text(str(item).lstrip('- '), 30, 90)}" for item in reuse)
        if actions:
            lines += ["", "四、立即行动"]
            lines.extend(f"- {compact_text(str(item).lstrip('- '), 20, 80)}" for item in actions)
        lines += ["", "完整学习笔记已保存到产物/交付物。"]
        return "\n".join(lines)

    def fetch_article(self, url: str) -> Article:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("链接格式不正确，请使用 http 或 https 开头的文章地址")
        from intelligence_center.safe_network import safe_urlopen
        scrapling_result = None
        try:
            from intelligence_center.scrapling_adapter import extract_public_article
            scrapling_result = extract_public_article(url, timeout=8)
            if scrapling_result.ok:
                return Article(
                    url=url,
                    title=scrapling_result.title or parsed.netloc,
                    source=scrapling_result.source or parsed.netloc,
                    author=scrapling_result.author or "",
                    published_at=(scrapling_result.published_at or "")[:19].replace("T", " "),
                    content=scrapling_result.text,
                    markdown=scrapling_result.markdown,
                    extractor="scrapling",
                    extractor_message=scrapling_result.message,
                )
        except Exception as error:
            scrapling_result = type("_Fallback", (), {"message": f"Scrapling 跳过：{error}"})()

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with safe_urlopen(request, timeout=10, allow_private=False) as response:
            raw = response.read(3 * 1024 * 1024)
            content_type = response.headers.get("Content-Type", "")
        charset = self._charset(content_type, raw)
        page = raw.decode(charset, errors="replace")
        title = self._title(page) or parsed.netloc
        source = self._source(page, parsed.netloc)
        author = self._meta(page, ("author", "article:author", "og:article:author")) or ""
        published = self._published(page)
        content = self._article_content(page)
        fallback_message = ""
        if scrapling_result:
            fallback_message = getattr(scrapling_result, "message", "") or "Scrapling 不可用，已使用内置解析"
        else:
            fallback_message = "已使用内置网页解析"
        return Article(url=url, title=title, source=source, author=author, published_at=published, content=content, markdown="", extractor="builtin", extractor_message=fallback_message)

    def build_note(self, article: Article, *, modes: list[str], custom_goal: str = "") -> str:
        llm_note = self._model_learning_note(article, modes=modes, custom_goal=custom_goal)
        if llm_note:
            return llm_note
        paragraphs = self._paragraphs(article.content)
        keywords = self._keywords(article.content)
        topic = self._topic_summary(article, keywords)
        learnings = self._learning_points(article, paragraphs, keywords)
        reuse = self._reuse_points(article, paragraphs, keywords)
        actions = self._action_points(article, keywords)
        mode_set = set(modes or [])
        lines = [f"# 快速学习｜{article.title}", ""]
        meta = [article.source]
        if article.author:
            meta.append(f"作者：{article.author}")
        if article.published_at:
            meta.append(f"时间：{article.published_at}")
        lines += [f"来源：{'｜'.join(meta)}", f"原文：[查看原文]({article.url})", ""]
        if custom_goal:
            lines += ["## 学习目标", custom_goal, ""]
        if "summary" in mode_set or not mode_set:
            lines.append("## 一句话结论")
            lines.append(self._one_line_takeaway(article, keywords))
            lines.append("")
            lines.append("## 这篇文章在讲什么")
            lines.append(topic)
            lines.append("")
        if "notes" in mode_set or not mode_set:
            lines.append("## 我应该学什么")
            lines.extend(f"- {item}" for item in learnings)
            lines.append("")
        if "methods" in mode_set:
            lines.append("## 我可以怎么复用")
            lines.extend(f"- {item}" for item in reuse)
            lines.append("")
        if "cards" in mode_set:
            lines.append("## 四、知识卡片")
            for word in keywords[:8]:
                lines.append(f"- {word}：本文中的高频重点概念，可加入后续学习或自动化关注词。")
            lines.append("")
        if "actions" in mode_set or not mode_set:
            lines.append("## 可直接复用模板")
            template = self._reuse_template(article, keywords)
            lines.extend(f"- {item}" for item in template)
            lines.append("")
            lines.append("## 立即行动")
            lines.extend(f"- {item}" for item in actions)
            lines.append("")
        lines.append("## 六、后续可让 Agent 做")
        lines.extend([
            "- 把这篇文章加入知识库并生成标签。",
            "- 根据本文生成一份执行清单。",
            "- 将内容改写成内部分享稿或公众号草稿。",
        ])
        return "\n".join(lines).strip()

    def _model_learning_note(self, article: Article, *, modes: list[str], custom_goal: str = "") -> str:
        """Use the configured model to transform extracted text into a true learning note.

        This deliberately asks for synthesis instead of sentence extraction.
        """
        try:
            from config import conf
            cfg = conf()
            model = str(cfg.get("model") or "")
            if model.startswith("deepseek"):
                key = cfg.get("deepseek_api_key")
                base = cfg.get("deepseek_api_base") or "https://api.deepseek.com/v1"
            else:
                key = cfg.get("open_ai_api_key")
                base = cfg.get("open_ai_api_base") or "https://api.openai.com/v1"
            if not key:
                return ""
            source_text = article.markdown if article.markdown and len(article.markdown) >= 200 else article.content
            excerpt = source_text[:8000]
            goal = custom_goal or "帮助我快速理解、学习和复用这篇文章的核心价值"
            system = (
                "你是资深学习教练、产品设计师和个人 Agent 顾问。"
                "你的目标是帮用户用最短时间吸收文章价值：压缩阅读、提炼判断、转成可复用方法。"
                "不要摘抄原文，不要堆概念，不要写读后感。必须基于原文，不编造事实。"
                "输出要像一个聪明助理在帮老板省时间：先给结论，再给可学点、可复用点和行动。"
            )
            user = f"""请阅读下面文章，按固定结构输出。

文章标题：{article.title}
来源：{article.source}
时间：{article.published_at or "未知"}
正文提取：{article.extractor}（{article.extractor_message or "未记录"}）
学习目标：{goal}

输出要求：
1. 先给一句话结论，让我不用读全文也知道文章价值。
2. 用“这篇文章在讲什么”压缩成 2-3 句，不要照搬原文。
3. 提炼“我应该学什么”：只保留真正能提升认知或工作效率的点。
4. 提炼“我可以怎么复用”：必须结合个人 Agent、自动化、Memory、Skill、工作流、知识库或产品设计。
5. 给出“可直接复用模板”：如果文章有方法论，要把它改造成模板/清单/规则。
6. 最后给出 3 条立即行动。
7. 不要输出检索过程，不要输出空泛套话，不要把原文长句直接搬出来。

格式必须如下：
# 快速学习｜{article.title}

来源：{article.source}{f"｜时间：{article.published_at}" if article.published_at else ""}
原文：[查看原文]({article.url})

## 一句话结论
一句话说明：这篇文章最值得我带走的东西是什么。

## 这篇文章在讲什么
2-3句话，用自己的话解释文章核心内容。

## 我应该学什么
- 学习点：它解决了什么问题，为什么重要。
- 学习点：它背后的判断或方法是什么。
- 学习点：它对我当前做个人 Agent 有什么启发。

## 我可以怎么复用
- 复用点：怎么迁移到我的 Agent。
- 复用点：怎么变成自动化/记忆/技能/模板。
- 复用点：怎么降低用户使用成本。

## 可直接复用模板
- 模板名称：
- 适用场景：
- 输入：
- 处理规则：
- 输出：

## 风险和边界
- 风险：说明边界。

## 立即行动
- 动作。
- 动作。
- 动作。

文章正文：
{excerpt}
"""
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.2,
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                base.rstrip("/") + "/chat/completions",
                data=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
            note = result["choices"][0]["message"]["content"].strip()
            if not any(marker in note for marker in ("## 这篇文章在讲什么", "## 一、这篇文章在讲什么", "## 一句话结论")):
                return ""
            return note
        except Exception:
            return ""

    def save_artifact(self, article: Article, note: str) -> dict:
        self.report_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        safe_title = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", article.title).strip("-")[:60] or "quick-learning"
        path = self.report_root / f"quick-learning-{stamp}-{safe_title}.md"
        try:
            from plugins.intelligence_center.output_formatting import format_automation_output, compact_artifact_summary
            note = format_automation_output(note)
            preview = compact_artifact_summary(note, 220)
        except Exception:
            preview = " ".join(article.content.split())[:220]
        path.write_text(note, encoding="utf-8")
        from intelligence_center.artifact_service import get_artifact_service

        return get_artifact_service(str(self.root)).register(str(path), {
            "title": f"学习笔记：{article.title}",
            "type": "学习笔记",
            "source": "quick-learning",
            "source_id": article.url,
            "source_url": article.url,
            "source_entry": "common-function:quick-learning",
            "source_label": "快速学习 Agent",
            "tags": ["快速学习", "网页提取", "Scrapling" if article.extractor == "scrapling" else "内置解析", "公众号学习" if "mp.weixin.qq.com" in article.url else "网站内容"],
            "preview": preview,
            "kind_label": "快速学习 Agent",
            "result_summary": preview or f"已提取 {len(article.content)} 字正文并生成学习笔记",
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "extractor": article.extractor,
            "extractor_message": article.extractor_message,
        })

    def save_learning_facts_async(self, article: Article, note: str, artifact: dict | None = None) -> None:
        def _run():
            self.save_learning_facts(article, note, artifact)
        threading.Thread(target=_run, daemon=True, name="quick-learning-facts").start()

    def save_learning_facts(self, article: Article, note: str, artifact: dict | None = None) -> list[dict]:
        """Persist reusable learning points into the local fact store."""
        try:
            from intelligence_center.fact_store import get_fact_store
            store = get_fact_store(self.root)
            facts = []
            title = article.title
            base = {
                "source": "快速学习 Agent",
                "source_url": article.url,
                "title": title,
                "scope": "private",
                "confidence": 0.75,
                "tags": ["快速学习", "文章学习", "可复用经验"],
                "metadata": {
                    "source_type": "quick_learning",
                    "source_id": article.url,
                    "source_url": article.url,
                    "artifact_id": (artifact or {}).get("id", ""),
                    "run_id": "",
                    "session_id": "",
                    "source_entry": "common-function:quick-learning",
                    "source_label": "快速学习 Agent",
                    "article_title": article.title,
                    "article_source": article.source,
                    "published_at": article.published_at,
                    "extractor": article.extractor,
                    "extractor_message": article.extractor_message,
                },
            }
            takeaway = self._section_text(note, ("## 一句话结论", "## 一、这篇文章在讲什么"), limit=180)
            if takeaway:
                facts.append(store.upsert(content=takeaway, fact_type="learning_takeaway", **base))
            for item in self._section_items(note, ("## 我应该学什么", "## 二、最值得学习的内容"), limit=5):
                facts.append(store.upsert(content=item.lstrip("- ").strip(), fact_type="learning_point", **base))
            for item in self._section_items(note, ("## 我可以怎么复用", "## 三、可以复用到我的 Agent 的做法"), limit=5):
                facts.append(store.upsert(content=item.lstrip("- ").strip(), fact_type="reuse_pattern", **base))
            return [{"id": item.get("id"), "type": item.get("fact_type"), "title": item.get("title")} for item in facts if item]
        except Exception:
            return []

    @staticmethod
    def _charset(content_type: str, raw: bytes) -> str:
        match = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
        if match:
            return match.group(1)
        head = raw[:4096].decode("ascii", errors="ignore")
        match = re.search(r"<meta[^>]+charset=[\"']?([\w.-]+)", head, re.I)
        return match.group(1) if match else "utf-8"

    @staticmethod
    def _meta(page: str, names: tuple[str, ...]) -> str:
        for name in names:
            patterns = [
                rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, page, re.I)
                if match:
                    return html.unescape(match.group(1)).strip()
        return ""

    def _title(self, page: str) -> str:
        title = self._meta(page, ("og:title", "twitter:title", "title"))
        if title:
            return title
        match = re.search(r"<title[^>]*>([\s\S]*?)</title>", page, re.I)
        return self._clean_text(match.group(1)) if match else ""

    def _source(self, page: str, fallback: str) -> str:
        return self._meta(page, ("og:site_name", "application-name")) or fallback

    def _published(self, page: str) -> str:
        value = self._meta(page, (
            "article:published_time", "pubdate", "publishdate", "publish_date",
            "date", "weixin:public_time",
        ))
        if value:
            return value[:19].replace("T", " ")
        match = re.search(r"(20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2})?)", page)
        return self._clean_text(match.group(1)) if match else ""

    def _article_content(self, page: str) -> str:
        page = re.sub(r"<script[\s\S]*?</script>", " ", page, flags=re.I)
        page = re.sub(r"<style[\s\S]*?</style>", " ", page, flags=re.I)
        candidates = []
        for pattern in (
            r'<div[^>]+id=["\']js_content["\'][^>]*>([\s\S]*?)</div>\s*(?:<script|<div[^>]+id=["\']js_sg_bar)',
            r"<article[^>]*>([\s\S]*?)</article>",
            r'<main[^>]*>([\s\S]*?)</main>',
            r'<div[^>]+class=["\'][^"\']*(?:article|content|post|entry|rich_media_content)[^"\']*["\'][^>]*>([\s\S]*?)</div>',
        ):
            candidates.extend(re.findall(pattern, page, re.I))
        if not candidates:
            body = re.search(r"<body[^>]*>([\s\S]*?)</body>", page, re.I)
            candidates = [body.group(1) if body else page]
        texts = [self._clean_text(fragment) for fragment in candidates]
        text = max(texts, key=len) if texts else ""
        text = self._remove_boilerplate(text)
        return text[:20000]

    @staticmethod
    def _clean_text(fragment: str) -> str:
        fragment = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
        fragment = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", fragment, flags=re.I)
        fragment = re.sub(r"<[^>]+>", " ", fragment)
        text = html.unescape(fragment)
        text = re.sub(r"[\u200b\ufeff\xa0]+", " ", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()

    @staticmethod
    def _remove_boilerplate(text: str) -> str:
        drop = (
            "点击上方蓝字关注", "扫码关注", "商务合作", "免责声明", "阅读原文",
            "分享、点赞、在看", "喜欢此内容的人还喜欢", "广告", "转载授权",
        )
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or any(token in stripped for token in drop):
                continue
            lines.append(stripped)
        return "\n".join(lines)

    @staticmethod
    def _paragraphs(text: str) -> list[str]:
        parts = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"[\n。！？!?]+", text) if x.strip()]
        parts = [x for x in parts if len(x) >= 18]
        return parts[:20]

    def _top_sentences(self, text: str, limit: int = 5) -> list[str]:
        paragraphs = self._paragraphs(text)
        if not paragraphs:
            return ["正文较短，建议结合原文继续阅读。"]
        scored = sorted(paragraphs, key=lambda x: self._sentence_score(x), reverse=True)
        return [self._shorten(x, 100) for x in scored[:limit]]

    @staticmethod
    def _sentence_score(sentence: str) -> int:
        words = ("核心", "关键", "原因", "方法", "趋势", "机会", "风险", "增长", "产品", "用户", "技术", "模型", "Agent", "AI")
        return sum(sentence.count(w) * 3 for w in words) + min(len(sentence), 120)

    @staticmethod
    def _keywords(text: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{1,24}|[\u4e00-\u9fff]{2,8}", text)
        stop = {"一个", "我们", "他们", "这个", "可以", "进行", "通过", "没有", "因为", "所以", "以及", "如果", "但是", "文章", "内容"}
        counts = {}
        for token in tokens:
            if token in stop or len(token) < 2:
                continue
            counts[token] = counts.get(token, 0) + 1
        return [k for k, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:12]]

    def _method_points(self, paragraphs: list[str]) -> list[str]:
        selected = [p for p in paragraphs if any(w in p for w in ("方法", "步骤", "建议", "实践", "策略", "框架", "流程", "方案"))]
        return [self._shorten(p, 90) for p in (selected or paragraphs)[:5]]

    def _topic_summary(self, article: Article, keywords: list[str]) -> str:
        material = f"{article.title} {article.content[:3000]}".lower()
        if any(term in material for term in ("memory", "记忆", "fact_store", "memory.md", "用户画像")):
            return "这篇文章主要在讲 AI Agent 的记忆体系如何从简单文本记录，升级为可结构化存储、可同步、可审计的长期记忆机制。它的价值不只是保存信息，而是让 Agent 能持续复用经验、偏好和事实，逐步变得更懂用户。"
        if any(term in material for term in ("skill", "技能", "tool", "工具", "mcp", "cli")):
            return "这篇文章主要在讲 Agent 如何通过技能、工具或外部能力扩展，把一次性问答变成可重复执行的工作流。重点在于如何管理能力边界、依赖、调用方式和失败恢复。"
        if any(term in material for term in ("产品", "用户", "增长", "商业", "竞品")):
            return "这篇文章主要围绕产品或业务变化展开，说明某类需求、方案或趋势正在形成。它值得关注的地方在于能帮助判断用户真正需要什么，以及哪些能力可以产品化。"
        topic = "、".join(keywords[:5]) if keywords else article.title
        return f"这篇文章围绕“{topic}”展开，核心是在说明相关问题的背景、关键做法和可能影响。适合快速提炼成知识点、方法清单，并迁移到个人 Agent 的自动化或知识库能力中。"

    def _one_line_takeaway(self, article: Article, keywords: list[str]) -> str:
        material = f"{article.title} {article.content[:3000]}".lower()
        if any(term in material for term in ("memory", "记忆", "fact_store", "memory.md", "用户画像")):
            return "最值得带走的是：个人 Agent 的自进化核心不是训练模型，而是把记忆、事实、偏好和失败经验做成可管理、可审计、可复用的系统。"
        if any(term in material for term in ("skill", "技能", "tool", "mcp", "cli")):
            return "最值得带走的是：Agent 能力要沉淀成可安装、可启停、可修复的技能，而不是每次都重新推理。"
        if any(term in material for term in ("自动化", "workflow", "工作流", "任务")):
            return "最值得带走的是：好 Agent 要把复杂任务拆成稳定流程，并在执行、失败、通知和留痕之间形成闭环。"
        topic = "、".join(keywords[:3]) if keywords else article.title
        return f"最值得带走的是：围绕“{topic}”提炼出可复用的方法，而不是只记住文章里的信息点。"

    def _learning_points(self, article: Article, paragraphs: list[str], keywords: list[str]) -> list[str]:
        material = f"{article.title} {article.content[:5000]}".lower()
        points = []
        if any(term in material for term in ("memory", "记忆", "fact_store", "memory.md")):
            points.extend([
                "记忆要分层管理：临时会话、长期事实、用户偏好和失败经验不应混在一个文本里。",
                "记忆需要可审计：新增、修改、删除都要能追踪来源，否则 Agent 越用越久反而越不可控。",
                "本地优先更适合个人 Agent：SQLite、Markdown 或本地文件便于备份、迁移和回滚。",
            ])
        if any(term in material for term in ("同步", "pipeline", "管线", "ledger", "审计")):
            points.append("同步管线很关键：不是把内容存进去就结束，还要能去重、校验、版本化和回滚。")
        if any(term in material for term in ("pii", "隐私", "安全", "权限")):
            points.append("隐私与权限必须前置：个人数据、账号、邮件和文件记忆要有授权范围和敏感信息保护。")
        if not points:
            points = [
                "先学结构：这类文章通常包含问题背景、解决方案、实践步骤和风险边界，可以按这四层拆解。",
                "再学判断：重点看作者为什么选择这个方案，而不是只看用了哪些工具。",
                "最后学迁移：把可重复的流程变成模板、规则或自动化任务，才是真正吸收了文章价值。",
            ]
        return points[:5]

    def _reuse_points(self, article: Article, paragraphs: list[str], keywords: list[str]) -> list[str]:
        material = f"{article.title} {article.content[:5000]}".lower()
        points = []
        if any(term in material for term in ("memory", "记忆", "fact_store", "memory.md")):
            points.extend([
                "把用户画像、偏好、常用来源、失败经验拆成独立 Wiki 页面，避免全部塞进单一 Memory。",
                "为每条记忆增加来源、更新时间、生效状态和回滚记录，让 Agent 的自进化可检查。",
                "把失败经验沉淀为可复用规则，例如搜索无结果自动放宽时间、通知失败自动补发 Web。",
            ])
        if any(term in material for term in ("skill", "技能", "patch", "evolution")):
            points.append("把高频成功流程沉淀成 Skill，并支持启用、禁用、更新和补丁记录。")
        if any(term in material for term in ("agent", "自动化", "workflow", "工作流")):
            points.append("把文章里的流程拆成“输入、判断、执行、留痕、反馈”五步，迁移到自动化任务模板。")
        if not points:
            points = [
                "提炼一套模板：把文章中的方法整理成可复用的任务模板或检查清单。",
                "加入知识库：把关键概念、来源和结论保存，后续问答和自动化创建时可复用。",
                "形成关注规则：把关键词和来源加入常用来源/关注词，后续让 Agent 自动追踪。",
            ]
        return points[:5]

    def _reuse_template(self, article: Article, keywords: list[str]) -> list[str]:
        material = f"{article.title} {article.content[:5000]}".lower()
        if any(term in material for term in ("memory", "记忆", "fact_store", "memory.md")):
            return [
                "模板名称：记忆沉淀与复用检查清单",
                "适用场景：用户画像、偏好、失败经验、常用来源和任务经验管理",
                "输入：新会话、新任务结果、失败日志、用户明确偏好",
                "处理规则：提取事实 → 去重 → 标记来源 → 审批/确认 → 写入 Memory Wiki → 定期复盘",
                "输出：可追溯记忆、可复用规则、可回滚版本记录",
            ]
        if any(term in material for term in ("skill", "技能", "tool", "mcp", "cli")):
            return [
                "模板名称：高频能力转 Skill 模板",
                "适用场景：重复出现的搜索、文件、邮箱、网页操作或数据处理流程",
                "输入：用户目标、可用工具、权限范围、成功/失败案例",
                "处理规则：拆步骤 → 固定输入输出 → 标注权限 → 增加失败兜底 → 记录成功率",
                "输出：可安装、可启用、可禁用、可更新的 Skill",
            ]
        return [
            "模板名称：文章方法复用模板",
            "适用场景：把外部文章转成自己的工作方法或自动化能力",
            "输入：文章链接、学习目标、当前业务场景",
            "处理规则：提炼结论 → 提炼方法 → 匹配我的场景 → 形成清单/模板/自动化",
            "输出：学习笔记、行动清单、可复用任务模板",
        ]

    def _action_points(self, article: Article, keywords: list[str]) -> list[str]:
        actions = []
        if keywords:
            actions.append(f"把 {', '.join(keywords[:5])} 加入后续关注词，持续跟踪相关变化。")
        actions.append("提炼其中可复用的方法，转成自己的任务模板或工作清单。")
        if "AI" in article.content or "Agent" in article.content:
            actions.append("结合现有个人 Agent 产品，判断哪些能力可加入自动化、记忆或技能模块。")
        return actions[:4]

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        return text[:limit] + ("…" if len(text) > limit else "")

    @staticmethod
    def _section_text(markdown: str, headings: str | tuple[str, ...], limit: int = 220) -> str:
        if isinstance(headings, str):
            headings = (headings,)
        start = -1
        heading = ""
        for item in headings:
            start = markdown.find(item)
            if start >= 0:
                heading = item
                break
        if start < 0:
            return ""
        rest = markdown[start + len(heading):]
        end = rest.find("\n## ")
        block = rest[:end] if end >= 0 else rest
        text = re.sub(r"^\s*[-\d.、]+\s*", "", block.strip())
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit].rstrip("，。；;,. ") + ("…" if len(text) > limit else "")

    @staticmethod
    def _section_items(markdown: str, headings: str | tuple[str, ...], limit: int = 3) -> list[str]:
        if isinstance(headings, str):
            headings = (headings,)
        start = -1
        heading = ""
        for item in headings:
            start = markdown.find(item)
            if start >= 0:
                heading = item
                break
        if start < 0:
            return []
        rest = markdown[start + len(heading):]
        end = rest.find("\n## ")
        block = rest[:end] if end >= 0 else rest
        items = []
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("- "):
                items.append(line)
            elif re.match(r"^\d+[.、]\s+", line):
                items.append("- " + re.sub(r"^\d+[.、]\s+", "", line))
            if len(items) >= limit:
                break
        return items


def get_quick_learning_service(root: str | os.PathLike[str]) -> QuickLearningService:
    return QuickLearningService(root)
