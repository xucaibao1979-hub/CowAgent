"""Turn real tool failures into private, review-required regression drafts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent.evaluation.collect import _SECRET
from agent.execution.task_state import TaskStateStore


def collect_failure_cases(audit_path: str | Path, state_root: str | Path, limit: int = 100) -> list[dict]:
    audit_path = Path(audit_path)
    if not audit_path.exists():
        return []
    states = TaskStateStore(state_root)
    result = []
    seen = set()
    for line in reversed(audit_path.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("success") is not False:
            continue
        run_id = str(event.get("run_id") or event.get("parent_run_id") or "")
        state = states.read(run_id) if run_id else {}
        goal = str(state.get("goal") or "").strip()
        if not goal or _SECRET.search(goal):
            continue
        tool = str(event.get("tool") or "")
        error_code = str(event.get("error_code") or "execution_failed")
        key = (run_id, tool, error_code)
        if key in seen:
            continue
        seen.add(key)
        digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16]
        result.append({"id": f"failure-{digest}", "category": "failure_regression",
                       "instruction": goal[:500], "source_session": state.get("session_id") or "",
                       "run_id": run_id, "failure_tool": tool, "failure_code": error_code,
                       "expected_intent": "", "required_stages": [],
                       "annotation_status": "unreviewed"})
        if len(result) >= max(1, min(limit, 100)):
            break
    return result


def save_failure_cases(audit_path: str | Path, state_root: str | Path, output: str | Path) -> dict:
    cases = collect_failure_cases(audit_path, state_root)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"count": len(cases), "path": str(destination), "annotation_status": "unreviewed"}
