"""Expert team definitions and executable collaboration plans."""
from __future__ import annotations
import json, os, threading, uuid
from copy import deepcopy
from datetime import datetime

class ExpertTeamService:
    def __init__(self,root): self.path=os.path.join(root,"expert_teams.json");self._lock=threading.RLock()
    @staticmethod
    def now():return datetime.now().astimezone().isoformat(timespec="seconds")
    def read(self):
        try:
            with open(self.path,"r",encoding="utf-8") as f:return json.load(f).get("teams",[])
        except (OSError,ValueError,TypeError):return []
    def write(self,items):
        tmp=self.path+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump({"schema_version":1,"teams":items},f,ensure_ascii=False,indent=2)
        os.replace(tmp,self.path)
    def list(self):return deepcopy(sorted(self.read(),key=lambda x:x.get("updated_at",""),reverse=True))
    def create(self,p):
        name=str(p.get("name") or "").strip()
        if not name:raise ValueError("专家团名称不能为空")
        members=list(p.get("members") or [])
        if len(members)<2:raise ValueError("专家团至少需要两名成员")
        now=self.now();item={"id":uuid.uuid4().hex[:12],"name":name,"description":str(p.get("description") or ""),"leader":str(p.get("leader") or members[0].get("expert","")),"members":members,"execution_mode":p.get("execution_mode") or "parallel","delivery_standard":str(p.get("delivery_standard") or ""),"enabled":True,"runs":[],"created_at":now,"updated_at":now}
        with self._lock:items=self.read();items.append(item);self.write(items)
        return deepcopy(item)
    def update(self,team_id,p):
        with self._lock:
            items=self.read();item=next((x for x in items if x.get("id")==team_id),None)
            if not item:raise KeyError("专家团不存在")
            for k in {"name","description","leader","members","execution_mode","delivery_standard","enabled"}:
                if k in p:item[k]=deepcopy(p[k])
            item["updated_at"]=self.now();self.write(items);return deepcopy(item)
    def delete(self,team_id):
        with self._lock:
            items=self.read();new=[x for x in items if x.get("id")!=team_id]
            if len(new)==len(items):return False
            self.write(new);return True
    def plan(self,team_id,goal):
        with self._lock:
            items=self.read();team=next((x for x in items if x.get("id")==team_id),None)
            if not team:raise KeyError("专家团不存在")
            team=deepcopy(team)
        tasks=[{"expert":m.get("expert"),"responsibility":m.get("responsibility") or "从专业视角分析并给出结论","goal":goal,"status":"planned"} for m in team["members"]]
        prompt=(f"请以专家团“{team['name']}”执行以下目标：{goal}\n\n"
                "目标处理规则：冒号后的目标是主目标；后续若还有明确的操作句，则按出现顺序作为附加目标逐项完成。不得把附加目标替换成主目标，也不得自行扩展测试范围。\n"
                f"协作方式：{'使用 subagent tasks 并行执行所有成员任务' if team['execution_mode']=='parallel' else '按成员顺序逐项执行'}。\n"
                +"\n".join(f"- {x['expert']}：{x['responsibility']}" for x in tasks)
                +f"\n\n由团长“{team['leader']}”检查各成员结论、解决冲突并汇总。\n最终交付标准：{team['delivery_standard'] or '给出证据、结论、风险和下一步行动'}\n"
                 "回复规范：默认只输出“完成结果、需人工接管、未完成原因”三个简短部分；不要逐成员复述相同过程，不要输出排查教程。")
        plan={"id":uuid.uuid4().hex[:12],"team_id":team_id,"team_name":team["name"],"goal":str(goal or ""),"leader":team["leader"],"mode":team["execution_mode"],"tasks":tasks,"prompt":prompt,"status":"planned","created_at":self.now()}
        with self._lock:
            items=self.read();stored=next((x for x in items if x.get("id")==team_id),None)
            if not stored:raise KeyError("专家团不存在")
            stored.setdefault("runs",[]).append(deepcopy(plan))
            stored["runs"]=stored["runs"][-100:]
            stored["updated_at"]=self.now();self.write(items)
        return plan

_service=None
def get_expert_team_service(root):
    global _service
    if _service is None:_service=ExpertTeamService(root)
    return _service
