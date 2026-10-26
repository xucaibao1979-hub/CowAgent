import json
import secrets
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROLE_PERMISSIONS = {
    "owner": ["view", "edit", "manage_members", "share", "transfer", "delete"],
    "admin": ["view", "edit", "manage_members", "share"],
    "editor": ["view", "edit"], "viewer": ["view"],
}


class CollaborationService:
    _lock = threading.RLock()
    def __init__(self, root): self.root=Path(root); self.path=self.root/"collaboration.json"
    @staticmethod
    def _now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def overview(self, project_id):
        data=self._read(); return {"members":[x for x in data["members"] if x["project_id"]==project_id],"shares":[x for x in data["shares"] if x["project_id"]==project_id],"transfers":[x for x in data["transfers"] if x["project_id"]==project_id],"audit":[x for x in data["audit"] if x["project_id"]==project_id][:100],"role_permissions":ROLE_PERMISSIONS}

    def ensure_owner(self, project_id, name="Admin"):
        data=self._read()
        if not any(x["project_id"]==project_id and x["role"]=="owner" for x in data["members"]):
            item=self._member(project_id,name,"","owner","active");data["members"].append(item);self._write(data)

    def invite(self, project_id, name, contact, role):
        role=self._role(role); name=str(name or "").strip(); contact=str(contact or "").strip()
        if not name: raise ValueError("成员姓名不能为空")
        data=self._read(); item=self._member(project_id,name,contact,role,"invited");data["members"].append(item);self._audit(data,project_id,"member.invited",f"邀请 {name} 加入项目，角色：{role}",{"member_id":item["id"]});self._write(data);return item

    def update_member(self, project_id, member_id, role="", status=""):
        data=self._read(); item=next((x for x in data["members"] if x["id"]==member_id and x["project_id"]==project_id),None)
        if not item: raise KeyError(member_id)
        if role:
            role=self._role(role)
            if item["role"]=="owner" and role!="owner": raise ValueError("请使用工作转交更换项目负责人")
            item["role"]=role; item["permissions"]=ROLE_PERMISSIONS[role]
        if status: item["status"]=status
        item["updated_at"]=self._now();self._audit(data,project_id,"member.updated",f"更新成员 {item['name']}：{item['role']} / {item['status']}",{"member_id":item["id"]});self._write(data);return item

    def remove_member(self, project_id, member_id):
        data=self._read(); item=next((x for x in data["members"] if x["id"]==member_id and x["project_id"]==project_id),None)
        if not item: raise KeyError(member_id)
        if item["role"]=="owner": raise ValueError("项目负责人不能直接移除，请先转交项目")
        data["members"]=[x for x in data["members"] if x["id"]!=member_id];self._audit(data,project_id,"member.removed",f"移除成员 {item['name']}",{"member_id":member_id});self._write(data);return True

    def share(self, project_id, permission="view", expires_days=7):
        if permission not in ("view","comment","edit"): raise ValueError("无效的分享权限")
        days=max(1,min(int(expires_days or 7),365));now=datetime.now(timezone.utc).astimezone();item={"id":uuid.uuid4().hex[:12],"project_id":project_id,"token":secrets.token_urlsafe(24),"permission":permission,"status":"active","created_at":now.isoformat(timespec="seconds"),"expires_at":(now+timedelta(days=days)).isoformat(timespec="seconds")}
        data=self._read();data["shares"].insert(0,item);self._audit(data,project_id,"share.created",f"创建 {permission} 分享链接，有效期 {days} 天",{"share_id":item["id"]});self._write(data);return item

    def revoke_share(self, project_id, share_id):
        data=self._read();item=next((x for x in data["shares"] if x["id"]==share_id and x["project_id"]==project_id),None)
        if not item: raise KeyError(share_id)
        item["status"]="revoked";self._audit(data,project_id,"share.revoked","已撤销分享链接",{"share_id":share_id});self._write(data);return item

    def resolve_share(self, token):
        item=next((x for x in self._read()["shares"] if secrets.compare_digest(x.get("token",""),str(token or ""))),None)
        if not item or item["status"]!="active": raise ValueError("分享链接无效或已撤销")
        if datetime.fromisoformat(item["expires_at"])<datetime.now(timezone.utc).astimezone(): raise ValueError("分享链接已过期")
        return {k:v for k,v in item.items() if k!="token"}

    def transfer(self, project_id, member_id, scope="project", item_id=""):
        data=self._read();target=next((x for x in data["members"] if x["id"]==member_id and x["project_id"]==project_id),None)
        if not target: raise KeyError(member_id)
        if target["status"] not in ("active","invited"): raise ValueError("目标成员当前不可接收转交")
        item={"id":uuid.uuid4().hex[:12],"project_id":project_id,"scope":scope,"item_id":str(item_id or ""),"to_member_id":member_id,"to_name":target["name"],"status":"completed","created_at":self._now()}
        if scope=="project":
            for member in data["members"]:
                if member["project_id"]==project_id and member["role"]=="owner":member["role"]="admin";member["permissions"]=ROLE_PERMISSIONS["admin"]
            target["role"]="owner";target["permissions"]=ROLE_PERMISSIONS["owner"];target["status"]="active"
        data["transfers"].insert(0,item);self._audit(data,project_id,"work.transferred",f"将{scope}转交给 {target['name']}",{"transfer_id":item["id"],"item_id":item["item_id"]});self._write(data);return item

    def log(self, project_id, action, summary, details=None):
        data=self._read();item=self._audit(data,project_id,action,summary,details or {});self._write(data);return item

    @staticmethod
    def _member(project_id,name,contact,role,status):
        now=CollaborationService._now();return {"id":uuid.uuid4().hex[:12],"project_id":project_id,"name":name,"contact":contact,"role":role,"permissions":ROLE_PERMISSIONS[role],"status":status,"created_at":now,"updated_at":now}
    @staticmethod
    def _role(role):
        if role not in ROLE_PERMISSIONS: raise ValueError("无效的成员角色")
        return role
    def _audit(self,data,project_id,action,summary,details):
        item={"id":uuid.uuid4().hex[:14],"project_id":project_id,"actor":"Admin","action":action,"summary":summary,"details":deepcopy(details),"created_at":self._now()};data["audit"].insert(0,item);data["audit"]=data["audit"][:5000];return item
    def _read(self):
        if not self.path.exists():return {"version":1,"members":[],"shares":[],"transfers":[],"audit":[]}
        try:
            data=json.loads(self.path.read_text(encoding="utf-8"))
            for key in ("members","shares","transfers","audit"):data.setdefault(key,[])
            return data
        except (OSError,ValueError):return {"version":1,"members":[],"shares":[],"transfers":[],"audit":[]}
    def _write(self,data):
        with self._lock:
            self.root.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix(".tmp");tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8");tmp.replace(self.path)
