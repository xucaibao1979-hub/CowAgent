import json

from agent.tools.reliability import ToolReliability


def _write(path, events):
    path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")


def test_scores_are_smoothed_and_recent_failures_are_penalized(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(path, [
        {"tool": "stable", "intent": "web", "success": True, "run_id": "a"},
        {"tool": "stable", "intent": "web", "success": True, "run_id": "b"},
        {"tool": "flaky", "intent": "web", "success": True, "run_id": "c"},
        {"tool": "flaky", "intent": "web", "success": False, "run_id": "d"},
        {"tool": "flaky", "intent": "web", "success": False, "run_id": "e"},
    ])
    scores = ToolReliability(path).scores("web")
    assert scores["stable"]["score"] > scores["flaky"]["score"]
    assert scores["stable"]["success_rate"] < 1
    assert scores["flaky"]["recent_failures"] == 2


def test_successful_paths_reuse_only_complete_success_runs(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write(path, [
        {"tool": "search", "intent": "web", "success": True, "run_id": "ok"},
        {"tool": "fetch", "intent": "web", "success": True, "run_id": "ok"},
        {"tool": "search", "intent": "web", "success": True, "run_id": "bad"},
        {"tool": "browser", "intent": "web", "success": False, "run_id": "bad"},
    ])
    assert ToolReliability(path).successful_paths("web") == [["search", "fetch"]]
