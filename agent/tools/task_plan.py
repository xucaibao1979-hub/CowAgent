"""Durable task checkpoints for long, multi-step agent runs."""
from __future__ import annotations

import json
from pathlib import Path

from agent.tools.base_tool import BaseTool, ToolResult
from common.runtime_identity import current_identity


class TaskPlanTool(BaseTool):
    name = "task_plan"
    description = (
        "For a long task, save a short ordered plan before acting. After each step, "
        "record the actual result evidence. Use get after interruption or restart to "
        "continue at next_step. Never mark a send completed from a click alone; "
        "record interface acceptance or mark it awaiting verification."
    )
    params = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set", "complete"]},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "expected": {"type": "string"}},
                "required": ["title"]}},
            "step_id": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": ["action"],
    }

    def execute(self, args: dict):
        from agent.execution.task_state import TaskStateStore
        from config import get_data_root

        run_id = current_identity().run_id
        if not run_id:
            return ToolResult.fail("当前没有独立任务 ID，无法保存计划")
        store = TaskStateStore(Path(get_data_root()) / "intelligence_center" / "task_states")
        try:
            action = str(args.get("action") or "")
            if action == "get":
                state = store.read(run_id)
            elif action == "set":
                state = store.set_plan(run_id, args.get("steps"))
            elif action == "complete":
                state = store.complete_step(run_id, str(args.get("step_id") or ""),
                                            str(args.get("evidence") or ""))
            else:
                return ToolResult.fail("不支持的计划操作")
            if not state:
                return ToolResult.fail("当前任务状态不存在")
            return ToolResult.success(json.dumps({"run_id": run_id, "status": state.get("status"),
                                                  "plan": state.get("plan") or [],
                                                  "next_step": state.get("next_step") or "",
                                                  "recovery_plan": state.get("recovery_plan") or ""},
                                                 ensure_ascii=False))
        except (TypeError, ValueError) as error:
            return ToolResult.fail(str(error))
