"""One decision point for session, tool and connector permissions."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Optional
from agent.permission.policy import check_tool_call, normalize_mode

@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    risk: str = "unknown"
    connector_id: str = ""
    scopes: list = field(default_factory=list)
    source: str = "session"
    def to_dict(self): return asdict(self)

def operation_for(tool_name: str, arguments: dict) -> str:
    name=str(tool_name or "").casefold(); action=str((arguments or {}).get("action") or "").casefold()
    # An explicit destructive action must never inherit a read scope merely
    # because the tool is named search_* or get_*.
    if action in {"send","create","update","delete","remove","write","publish","purchase","pay","transfer"}: return "write"
    if any(x in name for x in ("send","create","update","delete","remove","write","publish","purchase","pay","transfer")): return "write"
    if any(x in name for x in ("list","get","read","search","fetch","query")) or action in {"list","get","read","search","fetch","query","status","snapshot"}: return "read"
    return "execute"


def risk_for(tool_name: str, arguments: dict) -> str:
    name = str(tool_name or "").casefold()
    action = str((arguments or {}).get("action") or "").casefold()
    instruction = " ".join(str((arguments or {}).get(key) or "") for key in
                           ("action", "label", "button", "name", "text", "description")).casefold()
    if any(word in instruction for word in ("付款", "支付", "转账", "购买", "删除账户",
                                            "publish", "purchase", "payment", "transfer")):
        return "high"
    if action in {"delete","remove","publish","purchase","pay","transfer"} or any(
        part in name for part in ("delete", "remove", "publish", "purchase", "payment", "transfer")
    ):
        return "high"
    return "medium" if operation_for(tool_name, arguments) == "write" else "low"

class PermissionGateway:
    """Combines existing session boundaries with explicit connector policy."""
    def __init__(self, connector_service=None): self.connector_service=connector_service
    def evaluate(self, *, mode: str, tool_name: str, arguments: Optional[Dict[str,Any]]=None,
                 cwd: str="", write_roots: Optional[Iterable[str]]=None,
                 connector_id: str="", project_id: str="", automation: bool=False,
                 risk: str="unknown", task_run_id: str="") -> PermissionDecision:
        args=arguments if isinstance(arguments,dict) else {}
        computed_risk = risk_for(tool_name, args)
        risk = max((risk if risk in {"low", "medium", "high"} else "low", computed_risk),
                   key={"low": 0, "medium": 1, "high": 2}.get)
        session=check_tool_call(normalize_mode(mode),tool_name,args,cwd,write_roots)
        if not session.allowed:
            return PermissionDecision(False,session.reason,True,risk,connector_id,source="session")
        if task_run_id and tool_name in {"desktop_wecom_send_staged_text", "desktop_wecom_send_staged_file"}:
            try:
                from pathlib import Path
                from config import get_data_root
                from agent.execution.task_state import TaskStateStore
                state = TaskStateStore(Path(get_data_root()) / "intelligence_center" / "task_states").read(task_run_id)
                allowed = set(state.get("authorized_recipients") or [])
                target = str(args.get("confirmed_contact") or "").strip()
                if allowed and target not in allowed:
                    return PermissionDecision(False, "发送对象与当前用户指令不一致", True, risk,
                                              connector_id, source="task_target")
            except (OSError, ValueError):
                pass
        if connector_id and self.connector_service is not None:
            saved=next((x for x in self.connector_service.read() if x.get("connector_id")==connector_id),None)
            if saved is not None:
                result=self.connector_service.check(connector_id,project_id,operation_for(tool_name,args),automation=automation)
                scopes=list((result.get("policy") or {}).get("scopes") or [])
                if not result["allowed"]:
                    return PermissionDecision(False,"；".join(result["reasons"]),bool(result.get("approval_required")),risk,connector_id,scopes,"connector")
                return PermissionDecision(True,"",bool(result.get("approval_required")) or risk=="high",risk,connector_id,scopes,"connector")
        return PermissionDecision(True,requires_approval=risk=="high",risk=risk,connector_id=connector_id)
    def evaluate_many(self,calls:Iterable[dict],**context)->dict:
        decisions=[self.evaluate(tool_name=c.get("name",""),arguments=c.get("arguments") or {},connector_id=c.get("connector_id",""),risk=c.get("risk","unknown"),**context).to_dict() for c in calls]
        blocked=[x for x in decisions if not x["allowed"]]; approval=[x for x in decisions if x["requires_approval"]]
        return {"allowed":not blocked,"decisions":decisions,"blocked_count":len(blocked),"approval_required":bool(approval),"approval_count":len(approval),"approval_scopes":sorted({s for x in approval for s in x["scopes"]})}
