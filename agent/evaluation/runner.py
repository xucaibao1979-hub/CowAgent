"""Evaluate routing and recorded outcomes without replaying external side effects.

Cases without a recorded run remain `not_run`: choosing a plausible tool is
never counted as completing a task. Link a case to a real run with `run_id`.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from collections import Counter
from pathlib import Path

from agent.routing import route_request
from common.task_trace import read_events


def load_cases(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("评测集必须是非空数组")
    ids = set()
    for case in data:
        if not isinstance(case, dict) or not case.get("id") or not case.get("instruction"):
            raise ValueError("每条评测必须有 id 和 instruction")
        if case["id"] in ids:
            raise ValueError(f"重复评测 id: {case['id']}")
        ids.add(case["id"])
    return data


def grade_run(events: list[dict], required: list[str] | None = None) -> dict:
    """Only explicit verification receipts can satisfy delivery requirements."""
    required = required or []
    stages = {str(item.get("stage")) for item in events}
    statuses = [str(item.get("status") or "") for item in events]
    failed = any(status in {"failed", "error"} for status in statuses)
    accepted = any(item.get("stage") == "微信接口发送" and item.get("status") == "accepted" for item in events)
    received = any(item.get("device_received") is True and item.get("status") == "received" for item in events)
    uncertain_send = any(item.get("stage") == "微信接口发送" and item.get("status") == "unconfirmed" for item in events)
    send_ids = [str(item.get("artifact_id")) for item in events
                if item.get("stage") == "微信接口发送" and item.get("artifact_id")]
    duplicate_sends = sum(count - 1 for count in Counter(send_ids).values() if count > 1)
    missing = [stage for stage in required if stage not in stages]
    manual = sum(status in {"approval_required", "needs_user"} for status in statuses)
    duration = sum(max(0, float(item.get("duration_ms") or 0)) for item in events)
    declared = any(status in {"success", "completed", "delivered"} for status in statuses)
    false_success = bool(declared and failed and not received)
    if not events:
        outcome = "not_run"
    elif received:
        outcome = "success"
    elif missing:
        outcome = "unverified"
    elif "设备确认收到" in required:
        outcome = "unverified" if uncertain_send or accepted else "failed" if failed else "unverified"
    elif "微信接口发送" in required:
        outcome = "accepted" if accepted else "unverified" if uncertain_send else "failed" if failed else "unverified"
    elif uncertain_send:
        outcome = "unverified"
    elif failed:
        outcome = "failed"
    else:
        # A final answer alone is not proof that the external task happened.
        verified = any(item.get("stage") == "目标核验" and item.get("status") == "verified" for item in events)
        outcome = "success" if verified else "unverified"
    return {"outcome": outcome, "false_success": false_success, "manual_interventions": manual,
            "duration_ms": round(duration), "missing_stages": missing,
            "interface_accepted": accepted, "device_received": received,
            "send_unconfirmed": uncertain_send, "duplicate_send_attempts": duplicate_sends}


def load_receipts(path: str | Path) -> list[dict]:
    """Read operator-verified external receipts from an isolated test account."""
    receipts = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(receipts, list):
        raise ValueError("回执文件必须是数组")
    for receipt in receipts:
        if not isinstance(receipt, dict) or not all(receipt.get(key) for key in
            ("run_id", "evidence_type", "evidence_ref", "verified_by")):
            raise ValueError("回执需要 run_id、evidence_type、evidence_ref 和 verified_by")
        if receipt.get("status") != "received":
            raise ValueError("只有接收端已核对的 received 回执可计入完成")
    return receipts


def link_recorded_runs(cases: list[dict], task_state_root: str | Path) -> list[dict]:
    """Link an instruction to a unique run in the same session.

    Ambiguous repeats stay unlinked; guessing a run would corrupt the score.
    """
    root = Path(task_state_root)
    if not root.exists():
        return cases
    index: dict[tuple[str, str], list[dict]] = {}
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime)[-3000:]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = (str(state.get("session_id") or ""), " ".join(str(state.get("goal") or "").split()))
        if key[0] and key[1]:
            index.setdefault(key, []).append(state)
    linked = []
    for case in cases:
        row = dict(case)
        if row.get("run_id"):
            linked.append(row)
            continue
        key = (str(row.get("source_session") or ""), " ".join(str(row.get("instruction") or "").split()))
        matches = index.get(key) or []
        if len(matches) > 1 and row.get("source_at"):
            try:
                at = float(row["source_at"])
                matches = [item for item in matches if abs(
                    datetime.fromisoformat(item["created_at"]).timestamp() - at) <= 120]
            except (ValueError, TypeError, KeyError):
                matches = []
        if len(matches) == 1:
            row["run_id"] = matches[0]["run_id"]
        linked.append(row)
    return linked


def evaluate(cases: list[dict], events: list[dict], receipts: list[dict] | None = None) -> dict:
    by_run: dict[str, list[dict]] = {}
    for event in events:
        if event.get("id"):
            by_run.setdefault(str(event["id"]), []).append(event)
    known_runs = set(by_run)
    for receipt in receipts or []:
        run_id = str(receipt["run_id"])
        if run_id not in known_runs:
            continue
        by_run[run_id].append({"id": run_id, "stage": "设备确认收到", "status": "received",
                               "device_received": True, "evidence_type": receipt["evidence_type"],
                               "evidence_ref": receipt["evidence_ref"],
                               "verified_by": receipt["verified_by"]})
    results = []
    for case in cases:
        decision = route_request(str(case["instruction"]), "eval-model")
        expected = str(case.get("expected_intent") or "")
        run_id = str(case.get("run_id") or "")
        trace = grade_run(by_run.get(run_id, []), case.get("required_stages") or [])
        results.append({"id": case["id"], "category": case.get("category", ""),
                        "route_intent": decision.intent, "route_correct": not expected or decision.intent == expected,
                        "run_id": run_id, **trace})
    annotated = [item for item, case in zip(results, cases) if case.get("expected_intent")]
    routed = sum(item["route_correct"] for item in annotated)
    recorded = [item for item in results if item["outcome"] != "not_run"]
    outcomes = Counter(item["outcome"] for item in results)
    return {"total_cases": len(results), "annotated_routing_cases": len(annotated),
            "routing_accuracy": round(routed / len(annotated), 4) if annotated else None,
            "recorded_runs": len(recorded), "not_run": outcomes["not_run"],
            "end_to_end_success_rate": round(outcomes["success"] / len(recorded), 4) if recorded else None,
            "interface_accepted_count": outcomes["accepted"],
            "unverified_count": outcomes["unverified"],
            "false_success_count": sum(item["false_success"] for item in recorded),
            "duplicate_send_attempt_count": sum(item["duplicate_send_attempts"] for item in recorded),
            "manual_intervention_count": sum(item["manual_interventions"] for item in recorded),
            "average_duration_ms": round(sum(item["duration_ms"] for item in recorded) / len(recorded)) if recorded else None,
            "cases": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="CowAgent 真实任务评测：路由 + 执行轨迹")
    parser.add_argument("--cases", default=str(Path(__file__).with_name("cases.json")))
    parser.add_argument("--trace-root", help="包含 execution_trace.jsonl 的目录；不传时只评测路由")
    parser.add_argument("--receipts", help="隔离测试账号的人工核验回执 JSON；必须提供已有 run_id")
    parser.add_argument("--task-state-root", help="可选：自动关联同会话、同指令的独立任务状态目录")
    parser.add_argument("--output", help="可选：将 JSON 报告写入指定路径")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.task_state_root:
        cases = link_recorded_runs(cases, args.task_state_root)
    report = evaluate(cases, read_events(args.trace_root) if args.trace_root else [],
                      load_receipts(args.receipts) if args.receipts else None)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
