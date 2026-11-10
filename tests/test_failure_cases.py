import json

from agent.evaluation.failure_cases import collect_failure_cases
from agent.execution.task_state import TaskStateStore


def test_failure_becomes_review_required_case_without_auto_promotion(tmp_path):
    states = TaskStateStore(tmp_path / "states")
    states.begin("run-fail12345", goal="查找桌面上的合同", source="weixin", session_id="user")
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({"run_id": "run-fail12345", "tool": "search_files",
                                 "success": False, "error_code": "file_not_found"}) + "\n", encoding="utf-8")
    cases = collect_failure_cases(audit, tmp_path / "states")
    assert len(cases) == 1
    assert cases[0]["annotation_status"] == "unreviewed"
    assert cases[0]["failure_code"] == "file_not_found"
    assert "expected_intent" in cases[0] and not cases[0]["expected_intent"]


def test_secret_goal_is_not_exported_to_failure_cases(tmp_path):
    states = TaskStateStore(tmp_path / "states")
    states.begin("run-secret123", goal="我的密码是 abc123", source="weixin", session_id="user")
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({"run_id": "run-secret123", "tool": "web_search",
                                 "success": False}) + "\n", encoding="utf-8")
    assert collect_failure_cases(audit, tmp_path / "states") == []
