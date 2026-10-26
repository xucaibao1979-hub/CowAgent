from agent.tools.base_tool import BaseTool
from agent.tools.registry import describe_builtin, describe_mcp, provider_summaries


class _ReadTool(BaseTool):
    name = "read"
    description = "Read a file"
    params = {"type": "object", "properties": {}}


class _McpClient:
    transport = "streamable-http"


class _McpTool:
    name = "create_issue"
    description = "Create an issue"
    params = {"type": "object", "properties": {}}
    server_name = "github"
    client = _McpClient()


def test_builtin_descriptor_is_stable_and_read_only():
    item = describe_builtin("read", _ReadTool())
    assert item.id == "builtin:read"
    assert item.provider == "builtin"
    assert item.risk == "read"
    assert item.available is True
    assert item.requires_approval is False


def test_bash_is_exposed_as_local_cli_and_requires_approval():
    item = describe_builtin("bash", _ReadTool())
    assert item.id == "cli:bash"
    assert item.provider == "cli"
    assert item.risk == "local_execute"
    assert item.requires_approval is True


def test_mcp_descriptor_includes_server_transport_and_status():
    item = describe_mcp("create_issue", _McpTool(), "needs_auth")
    assert item.id == "mcp:github:create_issue"
    assert item.provider_name == "github"
    assert item.transport == "streamable-http"
    assert item.status == "needs_auth"
    assert item.available is False


def test_provider_summary_groups_tools():
    read = describe_builtin("read", _ReadTool())
    mcp = describe_mcp("create_issue", _McpTool(), "ready")
    providers = provider_summaries([read, mcp], {"github": "ready"})
    assert {item["id"] for item in providers} == {"builtin:CowAgent", "mcp:github"}


def test_manager_health_reports_missing_stdio_runtime(monkeypatch):
    from agent.tools.tool_manager import ToolManager

    ToolManager.reset_instances()
    manager = ToolManager()
    manager._mcp_active_configs = {
        "missing": {"name": "missing", "type": "stdio", "command": "definitely-not-a-real-command"}
    }
    manager._mcp_status = {"missing": "failed"}
    report = manager.health_report()
    check = next(item for item in report["checks"] if item["id"] == "mcp:missing")
    assert check["status"] == "missing_runtime"
    assert check["repairable"] is True


def test_mcp_stdio_resolves_private_workspace_runtime(tmp_path, monkeypatch):
    from agent.tools.mcp.mcp_client import McpClient

    runtime = tmp_path / ".skill-runtime" / "node" / "node_modules" / ".bin"
    runtime.mkdir(parents=True)
    command = runtime / ("example.cmd" if __import__("os").name == "nt" else "example")
    command.write_text("", encoding="utf-8")
    monkeypatch.setattr("common.state_dir.shared_root", lambda: tmp_path)
    assert McpClient._resolve_stdio_command("example") == str(command)
