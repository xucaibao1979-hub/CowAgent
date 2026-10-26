"""Unified metadata for every capability the Agent can execute.

The runtime still owns execution (``ToolManager.create_tool``); this module is
the read-only catalogue used by the UI, routing, permissions and audit layers.
Keeping metadata separate means MCP and local CLI tools can be presented in
the same way without changing their execution contracts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List

from agent.tools.base_tool import is_tool_available


READ_ONLY_TOOLS = frozenset({
    "read", "ls", "search_files", "memory_search", "memory_get",
    "web_search", "web_fetch", "vision", "weather",
})
WORKSPACE_WRITE_TOOLS = frozenset({"write", "edit"})
EXTERNAL_ACTION_TOOLS = frozenset({
    "send", "browser", "scheduler", "env_config", "local_email",
})


@dataclass(frozen=True)
class ToolDescriptor:
    """Stable, serialisable description shared by all tool providers."""

    id: str
    name: str
    description: str
    provider: str
    provider_name: str
    status: str
    available: bool
    risk: str
    requires_approval: bool
    parameters: Dict[str, Any] = field(default_factory=dict)
    transport: str = "in_process"
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def classify_builtin(name: str) -> tuple[str, bool]:
    """Return the conservative default risk and approval requirement."""
    if name in READ_ONLY_TOOLS:
        return "read", False
    if name in WORKSPACE_WRITE_TOOLS:
        return "workspace_write", False
    if name in EXTERNAL_ACTION_TOOLS:
        return "external_action", True
    if name == "bash":
        return "local_execute", True
    if name in {"evolution_undo", "subagent"}:
        return "managed_action", True
    return "unknown", True


def describe_builtin(name: str, tool: Any) -> ToolDescriptor:
    provider = "cli" if name == "bash" else "builtin"
    provider_name = "本地命令行" if provider == "cli" else "CowAgent"
    risk, approval = classify_builtin(name)
    available = is_tool_available(tool)
    schema = tool.get_json_schema() if hasattr(tool, "get_json_schema") else {}
    return ToolDescriptor(
        id=f"{provider}:{name}",
        name=name,
        description=str(getattr(tool, "description", "") or ""),
        provider=provider,
        provider_name=provider_name,
        status="ready" if available else "unavailable",
        available=available,
        risk=risk,
        requires_approval=approval,
        parameters=dict(schema.get("parameters") or {}),
        transport="local_process" if provider == "cli" else "in_process",
        tags=[provider, risk],
    )


def describe_mcp(name: str, tool: Any, server_status: str = "ready") -> ToolDescriptor:
    server_name = str(getattr(tool, "server_name", "unknown") or "unknown")
    client = getattr(tool, "client", None)
    transport = str(getattr(client, "transport", "mcp") or "mcp")
    available = server_status == "ready"
    # An MCP server may perform arbitrary external side effects. Its detailed
    # risk is refined later from the tool verb and per-server policy; fail
    # closed at catalogue level so a new server never appears implicitly safe.
    return ToolDescriptor(
        id=f"mcp:{server_name}:{name}",
        name=name,
        description=str(getattr(tool, "description", "") or ""),
        provider="mcp",
        provider_name=server_name,
        status=server_status,
        available=available,
        risk="external_action",
        requires_approval=True,
        parameters=dict(getattr(tool, "params", {}) or {}),
        transport=transport,
        tags=["mcp", server_name, transport],
    )


def provider_summaries(tools: Iterable[ToolDescriptor], mcp_status: dict) -> list:
    """Aggregate catalogue entries into cards suitable for connector UI."""
    grouped: dict[tuple[str, str], list[ToolDescriptor]] = {}
    for tool in tools:
        grouped.setdefault((tool.provider, tool.provider_name), []).append(tool)

    result = []
    for (kind, name), entries in grouped.items():
        ready = sum(1 for item in entries if item.available)
        status = "ready" if ready == len(entries) else ("partial" if ready else "unavailable")
        if kind == "mcp":
            status = str(mcp_status.get(name, status))
        result.append({
            "id": f"{kind}:{name}",
            "type": kind,
            "name": name,
            "status": status,
            "tool_count": len(entries),
            "ready_count": ready,
            "transports": sorted({item.transport for item in entries}),
        })
    return sorted(result, key=lambda item: (item["type"], item["name"].lower()))
