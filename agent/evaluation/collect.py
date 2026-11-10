"""Collect real local user instructions into a private, unannotated eval set.

The output stays in the client data directory. It is not checked into Git and
does not call tools or send messages. Human labels are required before route
accuracy can be claimed for these cases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


_SECRET = re.compile(r"(?:api[_-]?key|password|密码|验证码|Bearer\s+\S+|sk-[A-Za-z0-9]{12,})", re.I)


def collect(store, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    result = []
    seen = set()
    sessions = store.list_sessions(page=1, page_size=200).get("sessions", [])
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if not session_id:
            continue
        try:
            page = store.load_history_page(session_id, page=1, page_size=100)
        except Exception:
            continue
        for message in reversed(page.get("messages") or []):
            if message.get("role") != "user":
                continue
            instruction = re.sub(r"\s+", " ", str(message.get("content") or "")).strip()
            if not 8 <= len(instruction) <= 500 or _SECRET.search(instruction):
                continue
            digest = hashlib.sha256(instruction.casefold().encode("utf-8")).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            result.append({"id": f"real-{digest}", "category": "unreviewed", "instruction": instruction,
                           "expected_intent": "", "required_stages": [], "run_id": "",
                           "source_session": session_id, "source_at": message.get("created_at") or 0,
                           "annotation_status": "unreviewed"})
            if len(result) >= limit:
                return result
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="从本机历史会话收集真实指令，不执行外部动作")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", help="默认保存在本机数据目录的 evaluations/real_cases.json")
    args = parser.parse_args()
    from agent.memory import get_conversation_store
    cases = collect(get_conversation_store(), args.limit)
    output = Path(args.output) if args.output else Path.home() / ".cow" / "evaluations" / "real_cases.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"collected": len(cases), "path": str(output), "annotation_status": "unreviewed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
