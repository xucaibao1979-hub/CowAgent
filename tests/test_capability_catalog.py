from types import SimpleNamespace
from unittest.mock import patch

from agent.routing import route_request
from agent.tools.capability_catalog import capability_for, capability_snapshot
from agent.tools.reliability import ToolReliability


def test_catalog_exposes_preconditions_and_history():
    item = capability_snapshot(["desktop_wecom_send_staged_text"], {
        "desktop_wecom_send_staged_text": {"success_rate": 0.7, "recent_failures": 2,
                                           "last_error_code": "window_closed"}
    }, {"available": False, "reason": "远程桌面已断开"})[0]
    assert not item["available"]
    assert item["permission"] == "desktop_send"
    assert item["recent_failures"] == 2
    assert item["last_error_code"] == "window_closed"


def test_dashboard_lists_registered_tools_even_without_history(tmp_path):
    manager = SimpleNamespace(tool_classes={"task_plan": object()}, _mcp_tool_instances={})
    with patch("agent.tools.tool_manager.ToolManager.instances", return_value=[manager]):
        result = ToolReliability(tmp_path / "absent.jsonl").dashboard()
    assert any(item["name"] == "task_plan" for item in result["capabilities"])


def test_repeated_read_failure_switches_to_healthy_peer():
    tools = [SimpleNamespace(name="web_search", description="搜索网页"),
             SimpleNamespace(name="browser_read_page", description="读取网页")]

    class Reliability:
        def scores(self, _intent):
            return {"web_search": {"score": 0.1, "total": 10, "recent_failures": 3},
                    "browser_read_page": {"score": 0.8, "total": 10, "recent_failures": 0}}

        def successful_paths(self, *_args):
            return []

    route = route_request("帮我搜索网页上的政策", "test-model", tools=tools, reliability=Reliability())
    assert "browser_read_page" in route.tool_candidates
    assert "web_search" not in route.tool_candidates


def test_desktop_route_does_not_offer_ui_tools_while_disconnected():
    tools = [SimpleNamespace(name="desktop_wecom_search_contact", description="搜索联系人")]
    with patch("agent.routing.os.name", "nt"), \
         patch("integrations.desktop_control.session.desktop_session_status",
               return_value={"available": False, "reason": "远程桌面已断开"}):
        route = route_request("打开企业微信搜索联系人", "test-model", tools=tools)
    assert "desktop_wecom_search_contact" not in route.tool_candidates
    assert any("断开" in reason for reason in route.reasons)
