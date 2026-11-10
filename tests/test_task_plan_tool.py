from pathlib import Path

from agent.execution.task_state import TaskStateStore
from agent.tools.task_plan import TaskPlanTool
from common.runtime_identity import identity_scope


def test_task_plan_tool_persists_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_data_root", lambda: str(tmp_path))
    states = TaskStateStore(Path(tmp_path) / "intelligence_center" / "task_states")
    states.begin("longtask123", goal="先找文件再发送", source="weixin", session_id="s1")
    tool = TaskPlanTool()
    with identity_scope(run_id="longtask123", session_id="s1"):
        assert tool.execute({"action": "set", "steps": [
            {"title": "找文件", "expected": "唯一路径"},
            {"title": "发送", "expected": "接口接受"},
        ]}).status == "success"
        assert tool.execute({"action": "complete", "step_id": "1",
                             "evidence": "file:sha256-abc"}).status == "success"
        result = tool.execute({"action": "get"})
    assert result.status == "success"
    assert '"next_step": "2"' in result.result
    assert TaskStateStore(states.root).read("longtask123")["plan"][0]["evidence"] == "file:sha256-abc"


def test_task_plan_tool_needs_current_run(tmp_path, monkeypatch):
    monkeypatch.setattr("config.get_data_root", lambda: str(tmp_path))
    assert TaskPlanTool().execute({"action": "get"}).status == "error"
