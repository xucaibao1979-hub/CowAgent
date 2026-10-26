"""Fast reading agent for books, PDFs and TXT notes.

The first version focuses on safe learning compression:
- book-title based pre-reading guide when no full text is provided;
- TXT/Markdown/PDF text extraction when the user uploads a file;
- artifact + knowledge-source persistence for later review.
"""
from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode


@dataclass
class BookInput:
    title: str
    goal: str
    source_name: str
    source_type: str
    text: str
    note: str = ""


class FastReadingService:
    def __init__(self, root: str | os.PathLike[str], workspace: str | os.PathLike[str] | None = None):
        self.root = Path(root).resolve()
        project_root = self.root.parent
        self.workspace = Path(workspace or project_root / "work" / "cow-workspace").resolve()
        self.report_root = self.root / "reports"
        self.knowledge_root = self.workspace / "knowledge" / "sources" / "books"

    CATEGORY_QUERY_MAP = {
        "励志成长": ["self help", "self improvement", "personal development", "habits self improvement", "motivation success"],
        "启蒙教育": ["early education children learning", "children science education"],
        "文学小说": ["classic literature fiction novels", "literary fiction"],
        "悬疑推理": ["mystery detective fiction thriller", "crime mystery"],
        "科幻奇幻": ["science fiction fantasy", "space science fiction"],
        "历史文化": ["history culture world history", "chinese history"],
        "哲学思想": ["philosophy ethics logic", "history of philosophy"],
        "心理情感": ["psychology relationships emotional intelligence", "mental health relationships"],
        "社会科学": ["social science sociology politics", "sociology media society"],
        "商业管理": ["business management leadership marketing", "startup management"],
        "职场技能": ["communication skills career productivity", "career skills"],
        "财经投资": ["personal finance investing wealth money", "value investing stock market"],
        "科学科普": ["popular science physics biology", "science for general readers"],
        "计算机技术": ["computer science programming artificial intelligence", "software engineering programming"],
        "教材教辅": ["textbooks exam preparation study guide", "study guide"],
        "语言学习": ["language learning english writing", "english grammar writing"],
        "少儿读物": ["children books juvenile fiction picture books", "children literature"],
        "青少年读物": ["young adult coming of age study skills", "young adult fiction"],
        "家庭教育": ["parenting family education child psychology", "parenting children"],
        "生活实用": ["home cooking health lifestyle", "lifestyle practical guide"],
        "健康医学": ["health medicine nutrition", "medical health guide"],
        "艺术审美": ["art design photography art history", "art appreciation"],
        "旅行地理": ["travel geography adventure", "travel writing geography"],
        "传记纪实": ["biography memoir nonfiction", "autobiography biography"],
        "军事战争": ["military history war strategy", "war history strategy"],
        "宗教国学": ["religion buddhism confucianism taoism", "religion classics"],
        "漫画动漫": ["comics graphic novels manga", "comic books"],
        "工具参考": ["reference dictionary encyclopedia", "handbook reference"],
        "诗歌散文": ["poetry essays literary criticism", "essays poetry"],
        "幽默娱乐": ["humor entertainment games", "humorous fiction"],
    }

    FORMAT_EXTS = {
        "pdf": [".pdf"],
        "txt": [".txt"],
        "epub": [".epub"],
        "mobi": [".mobi"],
        "azw3": [".azw3"],
    }

    CATEGORY_REQUIRED_KEYWORDS = {
        "财经投资": ["finance", "invest", "investment", "investing", "money", "wealth", "stock", "economic", "securities", "wall street"],
        "商业管理": ["business", "management", "leadership", "marketing", "startup", "strategy"],
        "计算机技术": ["computer", "programming", "software", "algorithm", "database", "artificial intelligence"],
        "心理情感": ["psychology", "relationship", "emotion", "mental"],
        "健康医学": ["health", "medicine", "medical", "nutrition", "disease"],
    }

    CATEGORY_EXCLUDE_KEYWORDS = {
        "励志成长": ["programming", "software", "computer", "code", "coding", "algorithm", "database"],
        "心理情感": ["programming", "software", "computer", "code", "algorithm"],
        "商业管理": ["programming language", "source code", "compiler"],
        "财经投资": ["programming", "software", "source code"],
    }

    @staticmethod
    def _has_source_text(book: BookInput) -> bool:
        return len(str(book.text or "").strip()) >= 200

    def analyze(self, payload: dict) -> dict:
        started = time.time()
        book = self._build_input(payload)
        if not book.title and not book.text:
            raise ValueError("请输入书名，或上传 PDF/TXT 文件")
        if not book.title:
            book.title = self._title_from_text(book.text) or self._name_without_ext(book.source_name) or "快速读书"
        if len(book.text) > 280_000:
            book.note = f"原文较长，已截取前 280000 字进行学习分析；完整文件名：{book.source_name}"
            book.text = book.text[:280_000]
        extract_seconds = round(time.time() - started, 2)

        note_started = time.time()
        note = self._model_note(book) or self._fallback_note(book)
        note_seconds = round(time.time() - note_started, 2)

        artifact = None
        knowledge_path = ""
        has_source_text = self._has_source_text(book)
        if payload.get("save_artifact", True):
            save_started = time.time()
            artifact = self._save_artifact(book, note)
            if has_source_text:
                knowledge_path = self._save_knowledge(book, note, artifact)
            save_seconds = round(time.time() - save_started, 2)
        else:
            save_seconds = 0

        summary_note = self._summary_note(book, note, artifact, knowledge_path)
        return {
            "book": {
                "title": book.title,
                "goal": book.goal,
                "source_name": book.source_name,
                "source_type": book.source_type,
                "word_count": len(book.text),
            },
            "summary_note": summary_note,
            "note": note,
            "artifact": artifact,
            "knowledge_path": knowledge_path,
            "timings": {
                "extract_seconds": extract_seconds,
                "note_seconds": note_seconds,
                "save_seconds": save_seconds,
            },
            "message": (
                "快速读书笔记已生成，并保存到产物/交付物和知识库"
                if artifact and has_source_text
                else "已生成预读导读；未提供书籍原文，未作为正文读书笔记写入知识库"
                if artifact
                else "已生成预读导读"
            ),
        }

    def recommend_books(self, category: str, limit: int = 3, nonce: str | int | None = None) -> dict:
        category = str(category or "励志成长").strip() or "励志成长"
        limit = max(1, min(6, int(limit or 3)))
        rng = random.Random(f"{category}:{nonce or time.time_ns()}")
        terms = self._recommendation_queries(category, rng)
        docs: list[dict] = []
        seen: set[str] = set()
        sorts = ["trending", "rating", "ratings_count", "want_to_read", "editions"]
        rng.shuffle(sorts)
        search_plan = []
        for term in terms[:5]:
            for sort in sorts[:3]:
                search_plan.append((term, sort, rng.randint(1, 3)))
        for term, sort, page in search_plan[:12]:
            for rank, doc in enumerate(self._openlibrary_search(term, limit=50, page=page, sort=sort), 1):
                title = str(doc.get("title") or "").strip()
                if not title:
                    continue
                if not self._is_relevant_recommendation(term, doc, category):
                    continue
                authors = doc.get("author_name") or []
                author_key = " ".join(str(x) for x in authors[:2]) if isinstance(authors, list) else ""
                key = re.sub(r"\W+", "", f"{title} {author_key}".lower()) or str(doc.get("key") or title).lower()
                if key in seen:
                    continue
                seen.add(key)
                rating = float(doc.get("ratings_average") or 0)
                rating_count = int(doc.get("ratings_count") or 0)
                edition_count = int(doc.get("edition_count") or 0)
                want_to_read = int(doc.get("want_to_read_count") or 0)
                already_read = int(doc.get("already_read_count") or 0)
                currently_reading = int(doc.get("currently_reading_count") or 0)
                # Favor well-reviewed / widely held books; keep unrated classics if many editions exist.
                if rating_count < 5 and edition_count < 20 and want_to_read < 30:
                    continue
                first_year = int(doc.get("first_publish_year") or 0)
                recency_boost = 0
                if first_year >= datetime.now().year - 5:
                    recency_boost = 8
                elif first_year >= datetime.now().year - 12:
                    recency_boost = 4
                rank_boost = max(0, 28 - rank) if sort in {"trending", "ratings_count", "want_to_read"} else max(0, 16 - rank)
                score = (
                    (rating or 3.8) * 24
                    + min(80, rating_count ** 0.5 * 3)
                    + min(45, edition_count ** 0.5 * 2.4)
                    + min(45, want_to_read ** 0.5 * 2.2)
                    + min(20, already_read ** 0.5)
                    + min(12, currently_reading ** 0.5)
                    + rank_boost
                    + recency_boost
                )
                docs.append({
                    "title": title,
                    "author": "、".join(str(x) for x in authors[:2]) if isinstance(authors, list) else "",
                    "year": doc.get("first_publish_year") or "",
                    "rating": round(rating, 1) if rating else "",
                    "rating_count": rating_count,
                    "edition_count": edition_count,
                    "want_to_read_count": want_to_read,
                    "source": "Open Library",
                    "source_url": f"https://openlibrary.org{doc.get('key')}" if doc.get("key") else "",
                    "advice": self._book_recommendation_advice(category, title, rating, rating_count, edition_count),
                    "_score": score,
                    "_rank": rank,
                    "_sort": sort,
                })
        docs.sort(key=lambda x: x.get("_score", 0), reverse=True)
        pool = docs[: min(60, max(18, limit * 12))] if docs else []
        if pool:
            # Rotate among high-quality windows so "更换一批" changes results
            # while still staying close to the top-ranked / well-reviewed set.
            window_count = max(1, len(pool) - limit + 1)
            start = rng.randrange(window_count)
            candidates = pool[start:start + max(limit * 3, limit)]
            if len(candidates) < limit:
                candidates = pool[:max(limit * 3, limit)]
            rng.shuffle(candidates)
            books = sorted(candidates[:limit], key=lambda x: x.get("_score", 0), reverse=True)
        else:
            books = []
        for item in books:
            item.pop("_score", None)
            item.pop("_rank", None)
            item.pop("_sort", None)
        return {
            "category": category,
            "books": books,
            "source": "Open Library 最新公开榜单/评分数据（综合趋势、评分、评价数、想读数和版本热度）",
            "nonce": nonce,
            "candidate_count": len(pool),
            "message": "已根据公开书籍数据刷新推荐" if books else "网络推荐源暂未返回结果，可稍后重试或使用本地默认推荐。",
        }

    def _recommendation_queries(self, category: str, rng: random.Random) -> list[str]:
        base_terms = list(self.CATEGORY_QUERY_MAP.get(category) or [category])
        category_en = " ".join(base_terms[0].split()[:3]) if base_terms else category
        current_year = datetime.now().year
        variants = []
        for term in base_terms:
            variants.extend([
                term,
                f"best {term} books",
                f"popular {term} books",
                f"bestselling {term} books",
                f"award winning {term} books",
                f"{term} {current_year}",
                f"{term} {current_year - 1}",
            ])
        variants.extend([
            f"{category} 好评 书",
            f"{category} 推荐 书",
            f"{category_en} must read books",
        ])
        clean = []
        seen = set()
        for item in variants:
            value = re.sub(r"\s+", " ", str(item or "")).strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                clean.append(value)
        rng.shuffle(clean)
        return clean

    def find_source_download(self, title: str, fmt: str = "pdf") -> dict:
        title = str(title or "").strip()
        fmt = str(fmt or "pdf").lower().strip()
        if fmt not in self.FORMAT_EXTS:
            fmt = "pdf"
        if not title:
            raise ValueError("请先输入或选择书名")
        identifiers = self._find_archive_text_identifiers(title)
        for identifier in identifiers[:8]:
            try:
                metadata = self._fetch_json(f"https://archive.org/metadata/{quote(identifier, safe='')}", timeout=20)
            except Exception:
                continue
            files = metadata.get("files") or []
            chosen = self._pick_archive_file(files, fmt)
            if not chosen:
                continue
            name = str(chosen.get("name") or "")
            url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='/')}"
            return {
                "found": True,
                "title": title,
                "format": fmt,
                "filename": Path(name).name,
                "download_url": url,
                "source_url": f"https://archive.org/details/{quote(identifier, safe='')}",
                "source": "Internet Archive",
                "message": "已找到公开可直接下载的源书文件。请确认该书版权/授权状态符合你的使用场景。",
            }
        return {
            "found": False,
            "title": title,
            "format": fmt,
            "download_url": "",
            "source_url": f"https://archive.org/search?query={quote(title)}",
            "source": "Internet Archive",
            "message": "未找到可直接下载的公开版权/授权源书文件。商业书籍通常需要通过正版购买、图书馆借阅或你自己的授权文件导入。",
        }

    def _openlibrary_search(self, term: str, limit: int = 30, page: int = 1, sort: str = "rating") -> list[dict]:
        params = urlencode({
            "q": term,
            "sort": sort,
            "limit": str(limit),
            "page": str(max(1, int(page or 1))),
            "fields": "key,title,author_name,first_publish_year,ratings_average,ratings_count,edition_count,want_to_read_count,already_read_count,currently_reading_count,ebook_access,ia,language,subject",
        })
        try:
            data = self._fetch_json(f"https://openlibrary.org/search.json?{params}", timeout=15)
            docs = data.get("docs") or []
            if docs or sort == "rating":
                return docs
        except Exception:
            if sort == "rating":
                raise
        fallback = urlencode({
            "q": term,
            "sort": "rating",
            "limit": str(limit),
            "page": str(max(1, int(page or 1))),
            "fields": "key,title,author_name,first_publish_year,ratings_average,ratings_count,edition_count,want_to_read_count,already_read_count,currently_reading_count,ebook_access,ia,language,subject",
        })
        data = self._fetch_json(f"https://openlibrary.org/search.json?{fallback}", timeout=15)
        return data.get("docs") or []

    @classmethod
    def _is_relevant_recommendation(cls, term: str, doc: dict, category: str = "") -> bool:
        generic = {
            "personal", "classic", "general", "books", "book", "guide", "readers",
            "learning", "fiction", "nonfiction", "young", "children", "popular",
            "history", "science", "skills",
        }
        tokens = [x for x in re.split(r"[\s,;:/|]+", str(term or "").lower()) if len(x) >= 4 and x not in generic]
        if not tokens:
            return True
        title = str(doc.get("title") or "").lower()
        subjects = " ".join(str(x) for x in (doc.get("subject") or [])[:30]).lower()
        haystack = f"{title} {subjects}"
        required = cls.CATEGORY_REQUIRED_KEYWORDS.get(str(category or ""))
        if required and not any(word in haystack for word in required):
            return False
        excluded = cls.CATEGORY_EXCLUDE_KEYWORDS.get(str(category or ""))
        if excluded and any(word in haystack for word in excluded):
            return False
        return any(token in haystack for token in tokens[:8])

    def _find_archive_text_identifiers(self, title: str) -> list[str]:
        queries = [
            f'title:("{title}") AND mediatype:(texts)',
            f'("{title}") AND mediatype:(texts)',
        ]
        identifiers: list[str] = []
        seen: set[str] = set()
        for query in queries:
            params = urlencode({
                "q": query,
                "fl[]": ["identifier", "title"],
                "rows": "12",
                "page": "1",
                "output": "json",
            }, doseq=True)
            try:
                data = self._fetch_json(f"https://archive.org/advancedsearch.php?{params}", timeout=20)
            except Exception:
                continue
            for doc in ((data.get("response") or {}).get("docs") or []):
                identifier = str(doc.get("identifier") or "").strip()
                if identifier and identifier not in seen:
                    seen.add(identifier)
                    identifiers.append(identifier)
            if identifiers:
                break
        return identifiers

    def _pick_archive_file(self, files: list[dict], fmt: str) -> dict | None:
        exts = self.FORMAT_EXTS.get(fmt) or [".pdf"]
        candidates = []
        for item in files:
            name = str(item.get("name") or "")
            lower = name.lower()
            if not any(lower.endswith(ext) for ext in exts):
                continue
            if any(skip in lower for skip in ("_meta.", "_files.", "_reviews.", "abbyy.gz")):
                continue
            try:
                size = int(item.get("size") or 0)
            except Exception:
                size = 0
            if size and size > 120 * 1024 * 1024:
                continue
            score = 0
            if fmt == "pdf" and ("_text.pdf" in lower or lower.endswith(".pdf")):
                score += 10
            if fmt == "txt" and lower.endswith("_djvu.txt"):
                score += 10
            if fmt == "epub" and lower.endswith(".epub"):
                score += 10
            score += min(10, int(size / (1024 * 1024))) if size else 0
            candidates.append((score, item))
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1] if candidates else None

    def _fetch_json(self, url: str, timeout: int = 20) -> dict:
        try:
            from intelligence_center.safe_network import safe_urlopen
            opener = safe_urlopen
            kwargs = {"allow_private": False}
        except Exception:
            opener = urllib.request.urlopen
            kwargs = {}
        request = urllib.request.Request(url, headers={
            "User-Agent": "CowAgent/1.0 (+fast-reading)",
            "Accept": "application/json,text/plain,*/*",
        })
        with opener(request, timeout=timeout, **kwargs) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))

    @staticmethod
    def _book_recommendation_advice(category: str, title: str, rating: float, rating_count: int, edition_count: int) -> str:
        proof = []
        if rating:
            proof.append(f"公开评分约 {rating:.1f}")
        if rating_count:
            proof.append(f"{rating_count} 条评分")
        if edition_count:
            proof.append(f"{edition_count} 个版本")
        proof_text = "，".join(proof) or "公开数据热度较高"
        return f"{category}类推荐；{proof_text}。适合先生成预读导读，再上传正版/授权原文做精读。"

    def _build_input(self, payload: dict) -> BookInput:
        title = str(payload.get("book_title") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        file_name = str(payload.get("file_name") or "").strip()
        file_content = str(payload.get("file_content") or "")
        file_base64 = str(payload.get("file_base64") or "")
        source_type = "book_title"
        text = ""
        if file_content.strip():
            text = self._normalize_text(file_content)
            source_type = "txt"
        elif file_base64.strip():
            raw = self._decode_base64(file_base64)
            if file_name.lower().endswith(".pdf"):
                text = self._extract_pdf_text(raw)
                source_type = "pdf"
            else:
                text = self._normalize_text(raw.decode("utf-8", errors="replace"))
                source_type = "uploaded_text"
        return BookInput(
            title=title,
            goal=goal or "帮我快速理解这本书的核心内容、值得学习的观点、可复用的方法，并给出行动建议。",
            source_name=file_name or title or "book-title",
            source_type=source_type,
            text=text,
        )

    def _model_note(self, book: BookInput) -> str:
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
            if not key or not model:
                return ""
            has_text = len(book.text.strip()) >= 200
            excerpt = book.text[:16000] if has_text else "用户未上传原文，请基于公开常识和书名生成非替代原书阅读的学习导读。"
            source_line = f"上传文件：{book.source_name}，类型：{book.source_type}" if has_text else "未上传原文，仅输入书名"
            system = (
                "你是资深读书教练、产品经理和个人知识库顾问。"
                "你的任务是帮用户减少阅读时间，把书籍内容压缩成能理解、能学习、能复用的知识。"
                "如果未提供原文，只能生成预读导读/选书建议，并明确边界；不要假装读过用户未提供的全文。"
                "未提供原文时，禁止使用“书中提到”“作者认为”“本书指出”等暗示已读原书的表达。"
                "不要输出长篇摘抄，不要复刻书籍内容，不要空泛鸡汤。"
            )
            task_label = "快速读书笔记" if has_text else "预读导读"
            user = f"""请为用户生成一份“{task_label}”。

书名：{book.title}
来源：{source_line}
用户目标：{book.goal}

输出要求：
1. 如果提供了原文，先说明这本书主要讲什么，用户为什么值得读；如果未提供原文，只能说明“从书名可推测的主题”和“阅读前可关注的问题”。
2. 如果提供了原文，提炼 5-8 个核心观点；如果未提供原文，只能输出阅读关注点，不能写成书籍核心观点。
3. 提炼值得学习的判断、方法或模型。
4. 说明如何复用到用户的 Agent、自动化、知识库、工作流或个人管理。
5. 给出 5 条可直接执行的行动建议。
6. 如果是未上传原文，必须在“来源边界”里说明：这不是书籍内容总结，不能替代原书或上传原文后的精读。
7. 避免大段引用原文，避免标题和内容重复。

固定格式：
# 快速读书｜{book.title}

## 来源边界
说明是否基于上传原文，或仅基于书名导读。

## 这本书在讲什么
2-4 句讲清核心主题和价值。

## 核心观点
- 观点：解释。

## 值得我学习的内容
- 学习点：为什么重要，怎么帮助工作或认知。

## 可以复用的方法
- 方法：适用场景、输入、处理方式、输出。

## 可落地到我的 Agent
- 能力建议：可加入哪个 Agent/知识库/自动化/Memory/Skill。

## 行动建议
- 动作。

## 后续可让 Agent 做
- 动作。

可用内容：
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
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
            note = result["choices"][0]["message"]["content"].strip()
            return note if "## 这本书在讲什么" in note else ""
        except Exception:
            return ""

    def _fallback_note(self, book: BookInput) -> str:
        has_text = len(book.text.strip()) >= 80
        sentences = self._sentences(book.text) if has_text else []
        keywords = self._keywords(book.text or book.title)
        topic = "；".join(sentences[:3]) if sentences else f"目前只拿到了书名《{book.title}》，不能判断原书具体内容；这份内容只能帮助你做阅读前判断和提问准备。"
        points = sentences[3:10] if len(sentences) > 3 else [f"阅读时重点核对“{word}”是否真是本书核心概念，以及作者如何论证。" for word in keywords[:6]]
        lines = [
            f"# 快速读书｜{book.title}",
            "",
            "## 来源边界",
            f"- {'已基于上传文件提取文本进行分析。' if has_text else '未上传原文，本版本不是书籍内容总结，只是基于书名生成的预读导读/选书建议。'}",
        ]
        if book.note:
            lines.append(f"- {book.note}")
        lines += [
            "",
            "## 这本书在讲什么" if has_text else "## 预读判断",
            topic,
            "",
            "## 核心观点" if has_text else "## 阅读时需要验证的问题",
        ]
        for item in points[:8]:
            lines.append(f"- {self._compact(item, 90)}")
        lines += [
            "",
            "## 值得我学习的内容",
            "- 学会把书中的观点转成可执行清单，而不是只停留在摘录。",
            "- 识别书里的核心模型、判断标准和复盘方法，用来改进日常决策。",
            "- 把高频概念沉淀到知识库，后续由 Agent 在相关任务中主动提醒。",
            "",
            "## 可以复用的方法",
            "- 读书卡片：问题 → 观点 → 例子 → 可复用方法 → 下一步动作。",
            "- 工作流模板：输入一本书或章节，输出核心观点、适用场景和行动清单。",
            "- 知识库归档：按主题、关键词和适用场景保存，方便以后搜索复用。",
            "",
            "## 可落地到我的 Agent",
            "- 增加读书知识库标签，让快速读书结果能被后续任务检索。",
            "- 把行动建议转成待办或自动化提醒，避免读完后没有后续动作。",
            "- 对反复出现的关键词建立 Memory，后续搜索、写作和决策时优先引用。",
            "",
            "## 行动建议",
            "- 先上传 PDF/TXT、目录或章节内容，再生成真正基于原文的读书笔记。",
            "- 选择 1 个最有价值的方法，转成你的工作清单。",
            "- 上传原文后再把真实核心观点保存到知识库，并在一周后让 Agent 复盘是否真的使用。",
            "",
            "## 后续可让 Agent 做",
            "- 生成章节精读笔记。",
            "- 转成内部分享稿。",
            "- 生成读书行动清单和复盘问题。",
        ]
        return "\n".join(lines).strip()

    def _summary_note(self, book: BookInput, note: str, artifact: dict | None, knowledge_path: str) -> str:
        sections = {
            "core": self._section(note, "## 这本书在讲什么", 180),
            "pre": self._section(note, "## 预读判断", 180),
            "learn": self._section_items(note, "## 值得我学习的内容", 3),
            "reuse": self._section_items(note, "## 可以复用的方法", 3),
            "agent": self._section_items(note, "## 可落地到我的 Agent", 3),
        }
        has_source_text = self._has_source_text(book)
        lines = [f"{'快速读书' if has_source_text else '预读导读'}｜{book.title}", ""]
        if not has_source_text:
            lines.append("来源边界：当前未上传书籍正文、目录或章节内容，以下不是原书内容总结，只能作为阅读前判断和提问清单。")
            lines.append("")
        if artifact:
            lines.append(f"产物：{artifact.get('title') or '已保存'}")
        if knowledge_path:
            lines.append(f"知识库：{knowledge_path}")
        lines += ["", "一、这本书在讲什么" if has_source_text else "一、预读判断", sections["core"] or sections["pre"] or f"这是一份关于《{book.title}》的{'快速学习笔记' if has_source_text else '预读导读'}。"]
        if sections["learn"]:
            lines += ["", "二、值得我学习"]
            lines.extend(f"- {self._compact(item, 90)}" for item in sections["learn"])
        if sections["reuse"]:
            lines += ["", "三、可以怎么复用"]
            lines.extend(f"- {self._compact(item, 90)}" for item in sections["reuse"])
        if sections["agent"]:
            lines += ["", "四、可升级到 Agent"]
            lines.extend(f"- {self._compact(item, 90)}" for item in sections["agent"])
        if has_source_text:
            lines += ["", "完整读书笔记已保存到产物/交付物和知识库。" if artifact else "完整读书笔记已生成，当前未保存产物。"]
        else:
            lines += ["", "未提供原文，当前只保存为预读导读；上传 PDF/TXT 后可生成真正基于书籍内容的读书笔记。"]
        return "\n".join(lines)

    def _save_artifact(self, book: BookInput, note: str) -> dict:
        self.report_root.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d")
        safe_title = self._safe_name(book.title) or "fast-reading"
        path = self.report_root / f"fast-reading-{stamp}-{safe_title}.md"
        try:
            from plugins.intelligence_center.output_formatting import format_automation_output, compact_artifact_summary
            note = format_automation_output(note)
            preview = compact_artifact_summary(note, 220)
        except Exception:
            preview = " ".join(note.split())[:220]
        path.write_text(note, encoding="utf-8")
        from intelligence_center.artifact_service import get_artifact_service
        has_source_text = self._has_source_text(book)
        title_prefix = "读书笔记" if has_source_text else "预读导读"
        return get_artifact_service(str(self.root)).register(str(path), {
            "title": f"{title_prefix}：{book.title}",
            "type": "读书笔记" if has_source_text else "预读导读",
            "source": "fast-reading",
            "source_id": book.source_name or book.title,
            "source_entry": "common-function:fast-reading",
            "source_label": "快速读书 Agent",
            "tags": ["快速读书", "读书笔记" if has_source_text else "预读导读", book.source_type.upper()],
            "preview": preview,
            "kind_label": "快速读书 Agent",
            "result_summary": preview or f"已生成《{book.title}》{title_prefix}",
            "finished_at": now.isoformat(timespec="seconds"),
        })

    def _save_knowledge(self, book: BookInput, note: str, artifact: dict | None) -> str:
        now = datetime.now().astimezone()
        target_dir = self.knowledge_root / now.strftime("%Y") / now.strftime("%m")
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{now.strftime('%Y-%m-%d')}-{self._safe_name(book.title) or 'fast-reading'}.md"
        path = target_dir / file_name
        source = [
            f"# 读书知识｜{book.title}",
            "",
            f"- 来源类型：{book.source_type}",
            f"- 来源文件/书名：{book.source_name}",
            f"- 生成时间：{now.isoformat(timespec='seconds')}",
        ]
        if artifact:
            source.append(f"- 产物 ID：{artifact.get('id', '')}")
        source += ["", note]
        path.write_text("\n".join(source), encoding="utf-8")
        return str(path)

    def export_pdf(self, title: str, content: str) -> dict:
        """Render a reading note into a simple, readable PDF."""
        title = str(title or "快速读书笔记").strip()[:80] or "快速读书笔记"
        content = str(content or "").strip()
        if not content:
            raise ValueError("当前没有可导出的读书内容")
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        except Exception as error:
            data = self._fallback_pdf_bytes(title, content)
            return {
                "filename": f"{self._safe_name(title) or 'fast-reading'}.pdf",
                "content_type": "application/pdf",
                "base64": base64.b64encode(data).decode("ascii"),
                "size": len(data),
                "engine": f"builtin-fallback ({error})",
            }

        font_name = "Helvetica"
        for font_path in (
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simsun.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
        ):
            if os.path.exists(font_path):
                try:
                    pdfmetrics.registerFont(TTFont("CowAgentCJK", font_path))
                    font_name = "CowAgentCJK"
                    break
                except Exception:
                    continue
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
            title=title,
        )
        styles = getSampleStyleSheet()
        base = ParagraphStyle("CowBase", parent=styles["BodyText"], fontName=font_name, fontSize=10.5, leading=17)
        heading = ParagraphStyle("CowHeading", parent=base, fontSize=15, leading=22, spaceBefore=8, spaceAfter=6)
        small = ParagraphStyle("CowSmall", parent=base, fontSize=8.5, leading=12)
        story = [Paragraph(self._xml_escape(title), heading), Paragraph(datetime.now().astimezone().strftime("导出时间：%Y-%m-%d %H:%M"), small), Spacer(1, 6)]
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                story.append(Spacer(1, 5))
                continue
            style = heading if line.startswith("#") or line.startswith("##") else base
            clean = line.lstrip("#").strip()
            if line.startswith("- "):
                clean = "• " + line[2:].strip()
            story.append(Paragraph(self._xml_escape(clean), style))
        doc.build(story)
        data = buffer.getvalue()
        return {
            "filename": f"{self._safe_name(title) or 'fast-reading'}.pdf",
            "content_type": "application/pdf",
            "base64": base64.b64encode(data).decode("ascii"),
            "size": len(data),
            "engine": "reportlab",
        }

    def _fallback_pdf_bytes(self, title: str, content: str) -> bytes:
        """A dependency-free Unicode PDF fallback using built-in CJK font mapping."""
        lines = []
        for raw in (title + "\n\n" + content).splitlines():
            line = raw.strip().lstrip("#").strip()
            if not line:
                lines.append("")
                continue
            if line.startswith("- "):
                line = "• " + line[2:].strip()
            # Crude but robust wrapping for mixed Chinese/English.
            while len(line) > 38:
                lines.append(line[:38])
                line = line[38:]
            lines.append(line)
        pages = [lines[i:i + 42] for i in range(0, len(lines), 42)] or [[title]]

        objects: list[bytes] = []

        def add(obj: str | bytes) -> int:
            objects.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
            return len(objects)

        catalog_id = add("<< /Type /Catalog /Pages 2 0 R >>")
        pages_id = add("PAGES_PLACEHOLDER")
        font_id = add("<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H /DescendantFonts [4 0 R] >>")
        cid_id = add("<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> >>")
        page_ids = []
        content_ids = []
        for page_lines in pages:
            stream_lines = ["BT", "/F1 11 Tf", "50 790 Td", "16 TL"]
            for line in page_lines:
                if line:
                    hex_text = line.encode("utf-16-be", errors="ignore").hex().upper()
                    stream_lines.append(f"<{hex_text}> Tj")
                stream_lines.append("T*")
            stream_lines.append("ET")
            stream = "\n".join(stream_lines).encode("latin-1")
            content_id = add(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
            page_id = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>")
            content_ids.append(content_id)
            page_ids.append(page_id)
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")

        out = bytearray(b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n")
        offsets = [0]
        for idx, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{idx} 0 obj\n".encode("ascii"))
            out.extend(obj)
            out.extend(b"\nendobj\n")
        xref = len(out)
        out.extend(f"xref\n0 {len(objects)+1}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        out.extend(f"trailer\n<< /Size {len(objects)+1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF".encode("ascii"))
        return bytes(out)

    @staticmethod
    def _xml_escape(value: str) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )

    def _extract_pdf_text(self, raw: bytes) -> str:
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("PDF 文件过大，请先上传 12MB 以内的版本或转成 TXT")
        for module_name in ("pypdf", "PyPDF2"):
            try:
                if module_name == "pypdf":
                    from pypdf import PdfReader
                else:
                    from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                pages = []
                for page in reader.pages[:80]:
                    pages.append(page.extract_text() or "")
                text = self._normalize_text("\n".join(pages))
                if text.strip():
                    return text
            except Exception:
                continue
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                text = "\n".join((page.extract_text() or "") for page in pdf.pages[:80])
            text = self._normalize_text(text)
            if text.strip():
                return text
        except Exception:
            pass
        raise ValueError("PDF 文本提取失败，建议先另存为 TXT 后上传")

    @staticmethod
    def _decode_base64(value: str) -> bytes:
        if "," in value[:80]:
            value = value.split(",", 1)[1]
        raw = base64.b64decode(value, validate=False)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("上传文件过大，请控制在 12MB 以内")
        return raw

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = str(text or "").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    @staticmethod
    def _sentences(text: str) -> list[str]:
        chunks = re.split(r"(?<=[。！？.!?])\s+|\n+", str(text or ""))
        return [re.sub(r"\s+", " ", item).strip() for item in chunks if len(item.strip()) >= 20][:30]

    @staticmethod
    def _keywords(text: str) -> list[str]:
        words = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_-]{2,}", str(text or ""))
        stop = {"这个", "一个", "我们", "他们", "以及", "通过", "进行", "可以", "需要", "快速", "读书"}
        counts: dict[str, int] = {}
        for word in words:
            if word in stop or len(word) > 30:
                continue
            counts[word] = counts.get(word, 0) + 1
        return [word for word, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:12]]

    @staticmethod
    def _compact(text: str, limit: int = 90) -> str:
        value = " ".join(str(text or "").strip().lstrip("- ").split())
        return value if len(value) <= limit else value[: limit - 1] + "…"

    @staticmethod
    def _section(note: str, heading: str, limit: int = 180) -> str:
        idx = note.find(heading)
        if idx < 0:
            return ""
        tail = note[idx + len(heading):]
        next_idx = tail.find("\n## ")
        body = tail[:next_idx] if next_idx >= 0 else tail
        return FastReadingService._compact(body, limit)

    @staticmethod
    def _section_items(note: str, heading: str, limit: int = 3) -> list[str]:
        body = FastReadingService._section(note, heading, 1200)
        items = []
        for line in body.splitlines():
            clean = line.strip()
            if clean.startswith(("- ", "* ", "1.", "2.", "3.")):
                items.append(clean.lstrip("-* 1234567890.、").strip())
        return items[:limit]

    @staticmethod
    def _title_from_text(text: str) -> str:
        for line in str(text or "").splitlines()[:20]:
            clean = line.strip().strip("#").strip()
            if 2 <= len(clean) <= 80:
                return clean
        return ""

    @staticmethod
    def _name_without_ext(name: str) -> str:
        return os.path.splitext(os.path.basename(str(name or "")))[0]

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(value or "")).strip("-")[:70]


_service: FastReadingService | None = None


def get_fast_reading_service(root: str, workspace: str | None = None) -> FastReadingService:
    global _service
    root_abs = str(Path(root).resolve())
    workspace_abs = str(Path(workspace).resolve()) if workspace else ""
    if _service is None or str(_service.root) != root_abs or (workspace_abs and str(_service.workspace) != workspace_abs):
        _service = FastReadingService(root_abs, workspace_abs or None)
    return _service
