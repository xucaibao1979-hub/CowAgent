"""Persistent custom expert definitions."""
from __future__ import annotations
import json, os, threading, uuid
from copy import deepcopy
from datetime import datetime


class ExpertService:
    def __init__(self, root: str):
        self.path=os.path.join(root,"experts.json"); self._lock=threading.RLock()
    @staticmethod
    def _now(): return datetime.now().astimezone().isoformat(timespec="seconds")
    def _read(self):
        try:
            with open(self.path,"r",encoding="utf-8") as f: data=json.load(f)
            return data.get("experts",[])
        except (OSError,ValueError,TypeError): return []
    def _write(self,items):
        temp=self.path+".tmp"
        with open(temp,"w",encoding="utf-8") as f: json.dump({"schema_version":1,"experts":items},f,ensure_ascii=False,indent=2)
        os.replace(temp,self.path)
    def list(self):
        with self._lock:return deepcopy(sorted(self._read(),key=lambda x:x.get("updated_at",""),reverse=True))
    def create(self,p):
        now=self._now(); name=str(p.get("name") or "").strip()
        if not name: raise ValueError("专家名称不能为空")
        item={"id":uuid.uuid4().hex[:12],"name":name,"category":str(p.get("category") or "自定义"),"description":str(p.get("description") or ""),"role":str(p.get("role") or ""),"methodology":str(p.get("methodology") or ""),"output_standard":str(p.get("output_standard") or ""),"constraints":str(p.get("constraints") or ""),"model":str(p.get("model") or ""),"skills":list(p.get("skills") or []),"connectors":list(p.get("connectors") or []),"knowledge":list(p.get("knowledge") or []),"recommended_questions":list(p.get("recommended_questions") or []),"enabled":bool(p.get("enabled",True)),"builtin":False,"created_at":now,"updated_at":now}
        with self._lock: items=self._read();items.append(item);self._write(items)
        return deepcopy(item)
    def update(self,expert_id,p):
        allowed={"name","category","description","role","methodology","output_standard","constraints","model","skills","connectors","knowledge","recommended_questions","enabled"}
        with self._lock:
            items=self._read();item=next((x for x in items if x.get("id")==expert_id),None)
            if not item: raise KeyError("专家不存在")
            for k in allowed:
                if k in p:item[k]=deepcopy(p[k])
            item["updated_at"]=self._now();self._write(items);return deepcopy(item)
    def delete(self,expert_id):
        with self._lock:
            items=self._read();new=[x for x in items if x.get("id")!=expert_id]
            if len(new)==len(items):return False
            self._write(new);return True

_service=None
def get_expert_service(root):
    global _service
    if _service is None:_service=ExpertService(root)
    return _service
