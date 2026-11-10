"""Tool contracts used for routing, diagnostics and the capability panel."""
from __future__ import annotations


def capability_for(name: str) -> dict:
    name = str(name or "")
    if name == "task_plan":
        return {"scenarios": ["多步骤任务检查点与恢复"],
                "preconditions": ["当前任务 ID"], "permission": "task_state_write",
                "interactive": False, "side_effect": False}
    if name.startswith("desktop_wecom_"):
        return {"scenarios": ["企业微信联系人与会话操作"],
                "preconditions": ["交互式桌面", "企业微信窗口", "核对联系人和草稿"],
                "permission": "desktop_send" if "send_staged" in name else "desktop_control",
                "interactive": True, "side_effect": "send_staged" in name}
    if name.startswith("desktop_browser_"):
        return {"scenarios": ["复用已登录浏览器窗口"],
                "preconditions": ["交互式桌面", "已授权浏览器窗口"],
                "permission": "browser_control", "interactive": True,
                "side_effect": "navigate" in name or "open_tab" in name}
    if name.startswith("desktop_"):
        return {"scenarios": ["本机软件操作"], "preconditions": ["交互式桌面", "应用已授权"],
                "permission": "desktop_control", "interactive": True,
                "side_effect": name not in {"desktop_list_windows", "desktop_inspect_window", "desktop_capture_window", "desktop_session_status"}}
    if name in {"search_files", "read", "file_search"}:
        return {"scenarios": ["授权目录中的文件查询"], "preconditions": ["目录已授权"],
                "permission": "file_read", "interactive": False, "side_effect": False}
    if name in {"web_search", "browser_read_page"}:
        return {"scenarios": ["公开网页查询"], "preconditions": ["网络可用"],
                "permission": "web_read", "interactive": False, "side_effect": False}
    if name in {"weather", "weather_query"}:
        return {"scenarios": ["天气查询"], "preconditions": ["网络可用"],
                "permission": "weather_read", "interactive": False, "side_effect": False}
    builtin = {
        "ls": ("工作目录列表", "目录可读", "file_read", False),
        "memory_search": ("本地记忆检索", "记忆库可用", "memory_read", False),
        "memory_get": ("本地记忆读取", "记忆库可用", "memory_read", False),
        "web_fetch": ("公开网页读取", "网络可用", "web_read", False),
        "vision": ("图片内容理解", "已授权图片与视觉模型", "image_read", False),
        "write": ("工作目录文件写入", "目标目录已授权", "file_write", True),
        "edit": ("工作目录文件修改", "目标文件已授权", "file_write", True),
        "send": ("消息发送", "通道已连接且收件人已确认", "external_send", True),
        "local_email": ("邮件收发", "邮箱已配置", "email_access", True),
        "scheduler": ("定时任务管理", "调度服务可用", "schedule_write", True),
        "browser": ("网页浏览与操作", "浏览器可用", "browser_control", True),
        "bash": ("本地命令执行", "工作目录与执行权限已授权", "local_execute", True),
        "env_config": ("环境配置管理", "配置目录已授权", "configuration_write", True),
        "evolution_undo": ("撤销自主修改", "有可恢复的变更", "managed_action", True),
        "subagent": ("并行独立任务", "子任务权限已授权", "delegation", True),
    }
    if name in builtin:
        scenario, precondition, permission, effect = builtin[name]
        return {"scenarios": [scenario], "preconditions": [precondition],
                "permission": permission, "interactive": False, "side_effect": effect}
    return {"scenarios": ["由工具描述确定的扩展能力"],
            "preconditions": ["工具已连接并满足其自身前置条件"],
            "permission": "external_action" if name.startswith("mcp_") else "tool_default",
            "interactive": False, "side_effect": None}


def capability_snapshot(names, scores=None, desktop_status=None) -> list[dict]:
    scores = scores or {}
    result = []
    for name in names:
        meta = capability_for(name)
        stats = scores.get(name) or {}
        blocked = meta["interactive"] and desktop_status and not desktop_status.get("available", False)
        result.append({"name": name, **meta,
                       "available": not blocked,
                       "unavailable_reason": desktop_status.get("reason", "") if blocked else "",
                       "success_rate": stats.get("success_rate"),
                       "recent_failures": stats.get("recent_failures", 0),
                       "last_error_code": stats.get("last_error_code", "")})
    return result
