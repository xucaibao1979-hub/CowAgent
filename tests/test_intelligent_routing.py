from types import SimpleNamespace

from agent.routing import route_request


def _tool(name, description="", server_name=None):
    value = SimpleNamespace(name=name, description=description)
    if server_name:
        value.server_name = server_name
    return value


def test_weather_query_prefers_fast_weather_tool():
    route = route_request(
        "帮我查一下上海明天天气",
        "gpt-5.4-mini",
        tools=[_tool("weather", "查询实时天气"), _tool("bash", "运行命令")],
    )
    assert route.intent == "weather"
    assert route.level == "fast"
    assert route.risk == "low"
    assert route.tool_candidates[0] == "weather"


def test_complex_code_repair_uses_strong_route_and_safe_order():
    route = route_request(
        "分析这个项目的根因，然后修复代码并完成测试",
        "gpt-5.4-mini",
        tools=[_tool("read"), _tool("search_files"), _tool("edit"), _tool("bash")],
    )
    assert route.intent == "code"
    assert route.level == "strong"
    assert route.risk == "medium"
    assert route.tool_candidates.index("read") < route.tool_candidates.index("edit")
    assert route.provider_order[:2] == ["builtin", "cli"]


def test_email_send_is_marked_for_risk_aware_execution():
    route = route_request(
        "读取今天邮件并发送摘要给我",
        "gpt-5.4-mini",
        tools=[_tool("local_email", "读取和发送本地邮箱"), _tool("send", "发送消息")],
    )
    assert route.intent == "email"
    assert route.risk == "medium"
    assert "local_email" in route.tool_candidates
