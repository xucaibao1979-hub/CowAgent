"""Client-local tool reliability and successful-path reuse from audit events."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from common.state_dir import state_path


class ToolReliability:
    def __init__(self, path=None, max_events: int = 3000):
        self.path = Path(path or state_path("logs", "tool_audit.jsonl"))
        self.max_events = max(100, int(max_events))

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-self.max_events:]
            return [item for line in lines if line.strip() for item in [json.loads(line)] if isinstance(item, dict)]
        except Exception:
            return []

    def scores(self, intent: str = "") -> dict[str, dict]:
        grouped = defaultdict(list)
        for event in self.events():
            if intent and event.get("intent") not in {intent, "", None}:
                continue
            grouped[str(event.get("tool") or "")].append(event)
        result = {}
        for tool, items in grouped.items():
            if not tool:
                continue
            successes = sum(bool(item.get("success")) for item in items)
            total = len(items)
            recent = items[-5:]
            consecutive_failures = 0
            for item in reversed(recent):
                if item.get("success"):
                    break
                consecutive_failures += 1
            # Bayesian smoothing prevents one lucky call from outranking a
            # long, stable history. Recent repeated failures add a temporary
            # penalty but never make the tool permanently unusable.
            success_rate = (successes + 2) / (total + 4)
            score = max(0.05, success_rate - consecutive_failures * 0.12)
            result[tool] = {
                "score": round(score, 4), "success_rate": round(success_rate, 4),
                "successes": successes, "total": total,
                "recent_failures": consecutive_failures,
            }
        return result

    def successful_paths(self, intent: str, limit: int = 3) -> list[list[str]]:
        runs = defaultdict(list)
        for event in self.events():
            if event.get("intent") != intent or not event.get("run_id"):
                continue
            runs[event["run_id"]].append(event)
        counts = defaultdict(int)
        for items in runs.values():
            if items and all(item.get("success") for item in items):
                path = tuple(dict.fromkeys(str(item.get("tool") or "") for item in items if item.get("tool")))
                if path:
                    counts[path] += 1
        ordered = sorted(counts, key=lambda path: (-counts[path], len(path), path))
        return [list(path) for path in ordered[:max(1, limit)]]

    def recovery_paths(self, intent: str = "", failed_tool: str = "",
                       error_code: str = "", limit: int = 3) -> list[dict]:
        """Find tool sequences where a failed path was recovered by another tool.

        A recovery path is learned per client from audit runs: if one tool
        failed and a later different tool in the same run succeeded, that pair
        becomes a candidate fallback for similar future work.
        """
        runs = defaultdict(list)
        for event in self.events():
            if intent and event.get("intent") not in {intent, "", None}:
                continue
            if event.get("run_id"):
                runs[event["run_id"]].append(event)

        counts = defaultdict(lambda: {"count": 0, "last_seen": "", "error_codes": set()})
        failed_tool = str(failed_tool or "")
        error_code = str(error_code or "")
        for items in runs.values():
            failures = []
            for event in items:
                tool = str(event.get("tool") or "")
                if not tool:
                    continue
                if not event.get("success"):
                    if failed_tool and tool != failed_tool:
                        continue
                    if error_code and event.get("error_code") != error_code:
                        continue
                    failures.append(event)
                    continue
                if not failures:
                    continue
                for failure in failures:
                    from_tool = str(failure.get("tool") or "")
                    if from_tool == tool:
                        continue
                    key = (from_tool, tool, str(failure.get("error_code") or ""))
                    counts[key]["count"] += 1
                    counts[key]["last_seen"] = str(event.get("timestamp") or "")
                    if failure.get("error_code"):
                        counts[key]["error_codes"].add(str(failure.get("error_code")))

        ordered = sorted(counts.items(), key=lambda item: (-item[1]["count"], item[0]))
        return [
            {
                "from": key[0],
                "to": key[1],
                "error_code": key[2],
                "count": data["count"],
                "last_seen": data["last_seen"],
                "error_codes": sorted(data["error_codes"]),
            }
            for key, data in ordered[:max(1, limit)]
        ]

    def dashboard(self) -> dict:
        scores = self.scores()
        return {
            "tools": scores,
            "recovery_paths": self.recovery_paths(limit=5),
            "summary": {
                "tracked_tools": len(scores),
                "calls": sum(item["total"] for item in scores.values()),
                "successful_calls": sum(item["successes"] for item in scores.values()),
            },
        }
