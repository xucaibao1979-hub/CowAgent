from agent.permission.gateway import PermissionGateway

class Policies:
    def __init__(self,saved=None): self.saved=saved or []
    def read(self): return self.saved
    def check(self,cid,project_id,operation,automation=False):
        policy=self.saved[0]; reasons=[] if policy.get("enabled") and operation in policy.get("scopes",[]) else ["连接器未授权"]
        return {"allowed":not reasons,"reasons":reasons,"approval_required":policy.get("approval_required",False),"policy":policy}

def test_unsaved_policy_preserves_behavior():
    assert PermissionGateway(Policies()).evaluate(mode="full-access",tool_name="create_issue",connector_id="mcp").allowed

def test_explicit_policy_is_enforced():
    p=Policies([{"connector_id":"mcp","enabled":False,"scopes":["read"],"approval_required":True}])
    d=PermissionGateway(p).evaluate(mode="full-access",tool_name="create_issue",connector_id="mcp")
    assert not d.allowed and d.requires_approval

def test_batch_combines_approval():
    p=Policies([{"connector_id":"mcp","enabled":True,"scopes":["read","write"],"approval_required":True}])
    r=PermissionGateway(p).evaluate_many([{"name":"search_repos","connector_id":"mcp"},{"name":"create_issue","connector_id":"mcp"}],mode="full-access")
    assert r["allowed"] and r["approval_required"] and r["approval_count"]==2
