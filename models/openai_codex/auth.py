import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from threading import RLock

from config import conf, get_data_root

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
TOKEN_URL = f"{ISSUER}/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEVICE_URL = f"{ISSUER}/codex/device"
_pending = {}
_lock = RLock()


class CodexAuthError(RuntimeError): pass


def _path(): return Path(get_data_root()) / "openai_codex_auth.json"
def _request(url, *, data=None, json_data=None, timeout=20):
    headers={"Accept":"application/json","User-Agent":"CowAgent-Codex/1.0"}
    if json_data is not None:
        raw=json.dumps(json_data).encode(); headers["Content-Type"]="application/json"
    elif data is not None:
        raw=urllib.parse.urlencode(data).encode(); headers["Content-Type"]="application/x-www-form-urlencoded"
    else: raw=None
    request=urllib.request.Request(url,data=raw,headers=headers,method="POST" if raw is not None else "GET")
    # Do not inherit unrelated process-level HTTP_PROXY/HTTPS_PROXY values.
    # CowAgent has its own explicit proxy setting; use it only when the user
    # configured one. This also avoids desktop/sandbox placeholder proxies
    # such as 127.0.0.1:9 breaking the browser login flow.
    configured_proxy = str(conf().get("proxy") or "").strip()
    proxy_map = {"http": configured_proxy, "https": configured_proxy} if configured_proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy_map))
    try:
        with opener.open(request,timeout=timeout) as response: return response.status,json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try: payload=json.loads(error.read().decode())
        except Exception: payload={}
        return error.code,payload


def start_device_login():
    status,payload=_request(f"{ISSUER}/api/accounts/deviceauth/usercode",json_data={"client_id":CLIENT_ID})
    if status==429: raise CodexAuthError("OpenAI 暂时限制了登录请求，请稍后再试")
    if status!=200: raise CodexAuthError(f"无法启动 OpenAI 登录（HTTP {status}）")
    user_code=str(payload.get("user_code") or ""); device_id=str(payload.get("device_auth_id") or "")
    if not user_code or not device_id: raise CodexAuthError("OpenAI 登录响应缺少设备代码")
    with _lock:
        _pending.clear(); _pending.update({"user_code":user_code,"device_auth_id":device_id,"interval":max(3,int(payload.get("interval") or 5)),"created_at":time.time(),"last_poll":0})
    return {"verification_url":DEVICE_URL,"user_code":user_code,"expires_in":900}


def poll_device_login():
    with _lock: pending=dict(_pending)
    if not pending: return auth_status()
    if time.time()-pending["created_at"]>900:
        with _lock:_pending.clear()
        return {"configured":False,"state":"expired","message":"登录已超时，请重新开始"}
    if time.time()-pending["last_poll"]<pending["interval"]: return {"configured":False,"state":"pending","user_code":pending["user_code"]}
    with _lock:_pending["last_poll"]=time.time()
    status,payload=_request(f"{ISSUER}/api/accounts/deviceauth/token",json_data={"device_auth_id":pending["device_auth_id"],"user_code":pending["user_code"]})
    if status in (403,404): return {"configured":False,"state":"pending","user_code":pending["user_code"]}
    if status!=200: return {"configured":False,"state":"error","message":f"登录状态检查失败（HTTP {status}）"}
    code=payload.get("authorization_code"); verifier=payload.get("code_verifier")
    status,tokens=_request(TOKEN_URL,data={"grant_type":"authorization_code","code":code,"redirect_uri":f"{ISSUER}/deviceauth/callback","client_id":CLIENT_ID,"code_verifier":verifier})
    if status!=200 or not tokens.get("access_token"): return {"configured":False,"state":"error","message":f"OpenAI 令牌交换失败（HTTP {status}）"}
    _save_tokens(tokens); 
    with _lock:_pending.clear()
    result=auth_status(); result["state"]="authenticated"; return result


def _claims(token):
    try:
        part=token.split('.')[1]; part += '='*(-len(part)%4); return json.loads(base64.urlsafe_b64decode(part).decode())
    except Exception:return {}
def _save_tokens(tokens):
    access=tokens.get("access_token",""); claims=_claims(access); auth_claim=claims.get("https://api.openai.com/auth") or {}
    data={"access_token":access,"refresh_token":tokens.get("refresh_token","") ,"expires_at":int(claims.get("exp") or time.time()+int(tokens.get("expires_in") or 3600)),"account_id":auth_claim.get("chatgpt_account_id","")}
    from common.secure_storage import save_secure_json
    save_secure_json(str(_path()), data)
def _read():
    try:
        from common.secure_storage import load_secure_json, save_secure_json
        data, needs_migration = load_secure_json(str(_path()))
        if needs_migration and data:
            save_secure_json(str(_path()), data)
        return data if isinstance(data, dict) else {}
    except Exception:return {}
def auth_status():
    data=_read(); claims=_claims(data.get("access_token",'')); email=claims.get("email") or claims.get("https://api.openai.com/profile",{}).get("email","")
    return {"configured":bool(data.get("access_token") and data.get("refresh_token")),"state":"authenticated" if data.get("access_token") else "signed_out","email":email,"account_id":data.get("account_id","")}
def get_access():
    data=_read()
    if not data.get("access_token"): raise CodexAuthError("OpenAI Codex 尚未登录")
    if int(data.get("expires_at") or 0)<=time.time()+120:
        status,tokens=_request(TOKEN_URL,data={"grant_type":"refresh_token","refresh_token":data.get("refresh_token"),"client_id":CLIENT_ID})
        if status!=200 or not tokens.get("access_token"): raise CodexAuthError("OpenAI Codex 登录已失效，请重新登录")
        if not tokens.get("refresh_token"): tokens["refresh_token"]=data.get("refresh_token")
        _save_tokens(tokens); data=_read()
    return data
def logout():
    with _lock:_pending.clear()
    path=_path()
    if path.exists(): path.unlink()
