import json
import time
from pathlib import Path
from types import SimpleNamespace

from channel.web.ppt_master_handoff import PptMasterHandoff, project_from_daemon_command
from channel.web import web_channel


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_daemon_command_only_accepts_workspace_project(tmp_path):
    project = tmp_path / "projects" / "deck"
    project.mkdir(parents=True)
    command = f'python "C:/skills/ppt-master/scripts/confirm_ui/server.py" "{project}" --daemon'
    assert project_from_daemon_command(command, str(tmp_path)) == project.resolve()
    assert project_from_daemon_command(command.replace(" --daemon", ""), str(tmp_path)) is None
    outside = tmp_path / "outside"
    outside.mkdir()
    assert project_from_daemon_command(command.replace(str(project), str(outside)), str(tmp_path)) is None


def test_real_confirmation_receipts_resume_same_agent_then_export(tmp_path):
    project = tmp_path / "projects" / "deck"
    confirm = project / "confirm_ui"
    confirm.mkdir(parents=True)
    calls = []

    def dispatch(session_id, agent_id, prompt):
        calls.append((session_id, agent_id, prompt))
        if len(calls) == 1:
            _save(confirm / "recommendations.stage2.json", {"stage": "stage2"})
        else:
            exported = project / "exports" / "deck.pptx"
            exported.parent.mkdir()
            exported.write_bytes(b"pptx")

    handoff = PptMasterHandoff(str(tmp_path), dispatch, poll_seconds=0.01, wait_seconds=2)
    handoff.register(project, "session-one", "agent-one")
    handoff.start_for_session("session-one")
    time.sleep(0.04)
    assert calls == []  # Opening the page is not user confirmation.

    _save(confirm / "template_selection.json", {"status": "confirmed"})
    _save(confirm / "result.json", {"stage": "stage1", "status": "stage1-confirmed"})
    deadline = time.monotonic() + 2
    while len(calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    assert calls[0][:2] == ("session-one", "agent-one")

    _save(confirm / "result.json", {"stage": "final", "status": "confirmed"})
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2
    while json.loads((confirm / ".cowagent_handoff.json").read_text())["status"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads((confirm / ".cowagent_handoff.json").read_text())["status"] == "complete"
    handoff.resume_pending()
    assert len(calls) == 2  # Export and restart never duplicate a send/generation.


def test_web_agent_registers_only_successful_confirmation_server(tmp_path, monkeypatch):
    project = tmp_path / "projects" / "deck"
    project.mkdir(parents=True)
    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: str(tmp_path))
    channel_class = dict(zip(
        web_channel.WebChannel.__code__.co_freevars,
        (cell.cell_contents for cell in web_channel.WebChannel.__closure__),
    ))["cls"]
    recorded = []
    handoff = SimpleNamespace(
        register=lambda *args: recorded.append(("register", args)),
        start_for_session=lambda session: recorded.append(("start", session)),
    )
    channel = SimpleNamespace(
        sse_streams={"request": web_channel.SSEStreamState()},
        request_to_session={"request": "session"},
        request_to_agent={"request": "agent"},
        _ppt_handoff=handoff,
        _publish_sse_event=lambda *_: True,
    )
    callback = channel_class._make_sse_callback(channel, "request")
    command = f'python "C:/skills/ppt-master/scripts/confirm_ui/server.py" "{project}" --daemon'
    callback({"type": "tool_execution_start", "data": {
        "tool_name": "bash", "tool_call_id": "one", "arguments": {"command": command},
    }})
    callback({"type": "tool_execution_end", "data": {
        "tool_name": "bash", "tool_call_id": "one", "status": "error", "result": "failed",
    }})
    assert recorded == []
    callback({"type": "tool_execution_start", "data": {
        "tool_name": "bash", "tool_call_id": "two", "arguments": {"command": command},
    }})
    callback({"type": "tool_execution_end", "data": {
        "tool_name": "bash", "tool_call_id": "two", "status": "success", "result": "started",
    }})
    callback({"type": "agent_end", "data": {"final_response": "等待确认"}})
    assert recorded == [
        ("register", (project.resolve(), "session", "agent")),
        ("start", "session"),
    ]
