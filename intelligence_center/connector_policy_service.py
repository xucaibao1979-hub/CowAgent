"""Unified connector authorization policies and project bindings."""
from __future__ import annotations
import json, os, threading
from copy import deepcopy
from datetime import datetime

DEFAULTS={
 "email":("邮箱",["read","write"]),"webhook":("Webhook",["write"]),"github":("GitHub",["read","write"]),
 "feishu":("飞书",["read","write"]),"dingtalk":("钉钉",["read","write"]),"wecom":("企业微信",["read","write"]),
 "mcp":("MCP 服务",["read","write","execute"]),"api":("自定义 API",["read","write","execute"]),
 "browser":("浏览器",["read","execute"]),"notion":("Notion",["read","write"]),
}
class ConnectorPolicyService:
 def __init__(self,root):self.path=os.path.join(root,"connector_policies.json");self._lock=threading.RLock()
 @staticmethod
 def now():return datetime.now().astimezone().isoformat(timespec="seconds")
 def read(self):
  try:
   with open(self.path,"r",encoding="utf-8") as f:return json.load(f).get("policies",[])
  except (OSError,ValueError,TypeError):return []
 def write(self,items):
  tmp=self.path+".tmp"
  with open(tmp,"w",encoding="utf-8") as f:json.dump({"schema_version":1,"policies":items},f,ensure_ascii=False,indent=2)
  os.replace(tmp,self.path)
 def list(self):
  saved={x["connector_id"]:x for x in self.read()}
  for cid,(name,scopes) in DEFAULTS.items():
   saved.setdefault(cid,{"connector_id":cid,"name":name,"authorization_mode":"personal","scopes":scopes,"project_ids":[],"automation_allowed":False,"approval_required":True,"enabled":False,"last_test_status":"untested","last_test_at":"","updated_at":""})
  return deepcopy(list(saved.values()))
 def update(self,cid,p):
  with self._lock:
   items=self.read();item=next((x for x in items if x.get("connector_id")==cid),None)
   if item is None:
    base=next((x for x in self.list() if x["connector_id"]==cid),None)
    if base is None:raise KeyError("连接器不存在")
    item=base;items.append(item)
   for k in {"authorization_mode","scopes","project_ids","automation_allowed","approval_required","enabled","last_test_status","last_test_at"}:
    if k in p:item[k]=deepcopy(p[k])
   item["updated_at"]=self.now();self.write(items);return deepcopy(item)
 def check(self,cid,project_id,operation,automation=False):
  p=next((x for x in self.list() if x["connector_id"]==cid),None)
  reasons=[]
  if not p or not p.get("enabled"):reasons.append("连接器未启用")
  if operation not in (p or {}).get("scopes",[]):reasons.append(f"缺少 {operation} 权限")
  if project_id and project_id not in (p or {}).get("project_ids",[]):reasons.append("未绑定当前项目")
  if automation and not (p or {}).get("automation_allowed"):reasons.append("不允许自动化调用")
  return {"allowed":not reasons,"reasons":reasons,"approval_required":bool((p or {}).get("approval_required")),"policy":p}

_service=None
def get_connector_policy_service(root):
 global _service
 if _service is None:_service=ConnectorPolicyService(root)
 return _service
