"""Unified read-only search adapters for public Agent Skill marketplaces."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlencode

import requests


MARKETS = [
    {"id": "clawhub", "name": "ClawHub", "url": "https://clawhub.ai/"},
    {"id": "cowagent", "name": "CowAgent", "url": "https://skills.cowagent.ai/"},
    {"id": "skillhub", "name": "SkillHub", "url": "https://skillhub.cloud.tencent.com/skills?sortBy=score"},
    {"id": "skills_sh", "name": "Skills", "url": "https://www.skills.sh/"},
    {"id": "agentic", "name": "AgenticSkills", "url": "https://agenticskills.io/skills/find-skills"},
]


def _get_json(url, params=None):
    response = requests.get(
        url, params=params, timeout=(4, 12),
        headers={"Accept": "application/json", "User-Agent": "CowAgent/SkillSearch"},
    )
    response.raise_for_status()
    return response.json()


def _tags(value):
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    return []


def _clawhub(query):
    data = _get_json("https://clawhub.ai/api/search", {"q": query})
    rows = data.get("results", [])
    return [{
        "id": item.get("id") or f"clawhub:{item.get('slug')}",
        "provider": "clawhub", "provider_name": "ClawHub",
        "name": item.get("slug"), "display_name": item.get("displayName") or item.get("slug"),
        "description": item.get("summary") or "ClawHub 技能",
        "tags": ["ClawHub", item.get("ownerHandle") or "社区"],
        "command": f"cow skill install clawhub:{item.get('slug')}",
        "detail_url": "https://clawhub.ai" + str(item.get("canonicalUrl") or ""),
        "downloads": item.get("downloads") or 0,
    } for item in rows if item.get("slug")]


def _cowagent(query):
    data = _get_json("https://skills.cowagent.ai/api/skills/search", {"q": query, "limit": 50})
    return [{
        "id": f"cowagent:{item.get('name')}", "provider": "cowagent", "provider_name": "CowAgent",
        "name": item.get("name"), "display_name": item.get("display_name") or item.get("name"),
        "description": item.get("description") or item.get("summary") or "CowAgent Skill Hub 技能",
        "tags": _tags(item.get("tags")) + _tags(item.get("category")),
        "command": f"cow skill install {item.get('name')}",
        "detail_url": f"https://skills.cowagent.ai/{quote(str(item.get('name') or ''))}",
        "version": item.get("version"),
    } for item in data.get("skills", []) if item.get("name")]


def _skillhub(query):
    params = {"page": 1, "pageSize": 50, "keyword": query, "sortBy": "score", "order": "desc"}
    data = _get_json("https://api.skillhub.cn/api/skills", params)
    rows = (data.get("data") or {}).get("skills", []) if data.get("code") == 0 else []
    results = []
    for item in rows:
        slug = item.get("slug")
        if not slug:
            continue
        namespace = item.get("namespace") or {}
        download_params = {"slug": slug}
        if namespace.get("handle"):
            download_params["namespace"] = namespace["handle"]
        download_url = "https://api.skillhub.cn/api/v1/download?" + urlencode(download_params)
        sub_tags = [entry.get("name") for entry in item.get("subCategories") or [] if entry.get("name")]
        results.append({
            "id": f"skillhub:{namespace.get('canonicalName') or slug}",
            "provider": "skillhub", "provider_name": "SkillHub",
            "name": slug, "display_name": item.get("name") or slug,
            "description": item.get("description_zh") or item.get("description") or "腾讯 SkillHub 技能",
            "tags": _tags(item.get("tags")) + sub_tags,
            "command": f"cow skill install {download_url}",
            "detail_url": item.get("homepage") or f"https://skillhub.cn/{quote(slug)}",
            "downloads": item.get("downloads") or 0, "stars": item.get("stars") or 0,
            "verified": bool(item.get("verified")), "version": item.get("version"),
        })
    return results


def _skills_sh(query):
    data = _get_json("https://www.skills.sh/api/search", {"q": query})
    return [{
        "id": f"skills:{item.get('id')}", "provider": "skills_sh", "provider_name": "Skills",
        "name": item.get("skillId") or item.get("name"), "display_name": item.get("name") or item.get("skillId"),
        "description": f"来自 {item.get('source')} 的开放 Agent Skill",
        "tags": ["skills.sh", str(item.get("source") or "GitHub")],
        "command": f"npx skills add {item.get('source')} --skill {item.get('skillId') or item.get('name')}",
        "detail_url": f"https://www.skills.sh/{item.get('id')}",
        "downloads": item.get("installs") or 0,
    } for item in data.get("skills", []) if item.get("source") and (item.get("skillId") or item.get("name"))]


def _agentic(query):
    data = _get_json("https://agenticskills.io/api/skills", {"search": query})
    needle = query.casefold()
    rows = []
    for item in data.get("skills", []):
        haystack = " ".join([str(item.get("name") or ""), str(item.get("description") or ""), " ".join(_tags(item.get("tags")))])
        if needle not in haystack.casefold():
            continue
        command = item.get("installCommand") or ""
        github_url = item.get("githubUrl") or ""
        if github_url:
            command = f"cow skill install {github_url}"
        rows.append({
            "id": f"agentic:{item.get('slug')}", "provider": "agentic", "provider_name": "AgenticSkills",
            "name": item.get("slug"), "display_name": item.get("name") or item.get("slug"),
            "description": item.get("description") or "AgenticSkills 精选技能",
            "tags": _tags(item.get("tags")) + _tags(item.get("category")),
            "command": command, "detail_url": f"https://agenticskills.io/skills/{item.get('slug')}",
            "quality": item.get("quality"), "verified": bool(item.get("isOfficial")),
        })
    return rows


ADAPTERS = {"clawhub": _clawhub, "cowagent": _cowagent, "skillhub": _skillhub, "skills_sh": _skills_sh, "agentic": _agentic}


def search_skill_markets(query):
    """Search all markets concurrently; one outage never hides other results."""
    results, statuses = [], {}
    with ThreadPoolExecutor(max_workers=len(ADAPTERS), thread_name_prefix="skill-market") as executor:
        futures = {executor.submit(adapter, query): source for source, adapter in ADAPTERS.items()}
        for future in as_completed(futures):
            source = futures[future]
            try:
                items = future.result()
                results.extend(items)
                statuses[source] = {"status": "success", "count": len(items)}
            except Exception as error:
                statuses[source] = {"status": "error", "count": 0, "message": str(error)}
    order = {item["id"]: index for index, item in enumerate(MARKETS)}
    results.sort(key=lambda item: (order.get(item.get("provider"), 99), -(item.get("downloads") or 0), str(item.get("display_name") or "")))
    return results, statuses
