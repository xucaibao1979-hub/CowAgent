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
    if any(x in name for x in ("list","get","read","search","fetch","query")) or action in {"list","get","read","search","fetch","query","status","snapshot"}: return "read"
    if any(x in name for x in ("send","create","update","delete","write")): return "write"
    return "execute"

class PermissionGateway:
    """Combines existing session boundaries with explicit connector policy."""
    def __init__(self, connector_service=None): self.connector_service=connector_service
    def evaluate(self, *, mode: str, tool_name: str, arguments: Optional[Dict[str,Any]]=None,
                 cwd: str="", write_roots: Optional[Iterable[str]]=None,
                 connector_id: str="", project_id: str="", automation: bool=False,
                 risk: str="unknown") -> PermissionDecision:
        args=arguments if isinstance(arguments,dict) else {}
        session=check_tool_call(normalize_mode(mode),tool_name,args,cwd,write_roots)
        if not session.allowed:
            return PermissionDecision(False,session.reason,True,risk,connector_id,source="session")
        if connector_id and self.connector_service is not None:
            saved=next((x for x in self.connector_service.read() if x.get("connector_id")==connector_id),None)
            if saved is not None:
                result=self.connector_service.check(connector_id,project_id,operation_for(tool_name,args),automation=automation)
                scopes=list((result.get("policy") or {}).get("scopes") or [])
                if not result["allowed"]:
                    return PermissionDecision(False,"；".join(result["reasons"]),bool(result.get("approval_required")),risk,connector_id,scopes,"connector")
                return PermissionDecision(True,"",bool(result.get("approval_required")),risk,connector_id,scopes,"connector")
        return PermissionDecision(True,risk=risk,connector_id=connector_id)
    def evaluate_many(self,calls:Iterable[dict],**context)->dict:
        decisions=[self.evaluate(tool_name=c.get("name",""),arguments=c.get("arguments") or {},connector_id=c.get("connector_id",""),risk=c.get("risk","unknown"),**context).to_dict() for c in calls]
        blocked=[x for x in decisions if not x["allowed"]]; approval=[x for x in decisions if x["requires_approval"]]
        return {"allowed":not blocked,"decisions":decisions,"blocked_count":len(blocked),"approval_required":bool(approval),"approval_count":len(approval),"approval_scopes":sorted({s for x in approval for s in x["scopes"]})}
