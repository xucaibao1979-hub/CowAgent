"""Registered, auditable execution boundary for approved high-risk actions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict


class ApprovalExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalAction:
    name: str
    handler: Callable[[dict, dict], object]
    description: str = ""


class ApprovalExecutorRegistry:
    """Only explicitly registered actions can run after approval."""

    def __init__(self):
        self._actions: Dict[str, ApprovalAction] = {}

    def register(self, name, handler, description=""):
        name = str(name or "").strip()
        if not name or not callable(handler):
            raise ValueError("审批动作名称和处理器不能为空")
        self._actions[name] = ApprovalAction(name, handler, str(description or ""))
        return self

    def describe(self):
        return [{"action": item.name, "description": item.description}
                for item in self._actions.values()]

    def execute(self, item):
        action_name = str(item.get("action") or "").strip()
        action = self._actions.get(action_name)
        if action is None:
            raise ApprovalExecutionError(
                f"审批动作 {action_name or 'unknown'} 尚未注册执行器，已阻止执行"
            )
        result = action.handler(dict(item.get("preview") or {}), item)
        if result is None:
            return {"success": True}
        if isinstance(result, dict):
            return result
        return {"success": True, "result": str(result)}
