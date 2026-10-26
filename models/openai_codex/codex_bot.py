import json
import requests

from models.bot import Bot
from models.openai_compatible_bot import OpenAICompatibleBot
from .auth import CODEX_BASE_URL, get_access


class OpenAICodexBot(Bot, OpenAICompatibleBot):
    def __init__(self): super().__init__()
    def get_api_config(self): return {"api_key":"oauth","api_base":CODEX_BASE_URL,"model":"gpt-5.3-codex"}
    def call_with_tools(self,messages,tools=None,stream=False,**kwargs):
        auth=get_access(); instructions=kwargs.get("system") or "You are a helpful coding assistant."
        converted=[]
        for msg in messages or []:
            role=msg.get("role","user"); content=msg.get("content","")
            if role=="system": instructions=str(content); continue
            if role=="tool":
                converted.append({"type":"function_call_output","call_id":msg.get("tool_call_id") or msg.get("id"),"output":str(content)}); continue
            text_parts=[]
            if isinstance(content,list):
                for part in content:
                    if not isinstance(part,dict): text_parts.append(str(part)); continue
                    if part.get("type")=="tool_result": converted.append({"type":"function_call_output","call_id":part.get("tool_use_id"),"output":str(part.get("content") or "")})
                    elif part.get("type")=="tool_use": converted.append({"type":"function_call","call_id":part.get("id"),"name":part.get("name"),"arguments":json.dumps(part.get("input") or {},ensure_ascii=False)})
                    else: text_parts.append(str(part.get("text") or part.get("content") or ""))
                content="\n".join(x for x in text_parts if x)
            if content not in (None,""): converted.append({"role":role if role in ("user","assistant") else "user","content":[{"type":"input_text" if role!="assistant" else "output_text","text":str(content)}]})
            for call in msg.get("tool_calls") or []:
                function=call.get("function") or {}; converted.append({"type":"function_call","call_id":call.get("id"),"name":function.get("name"),"arguments":function.get("arguments") or "{}"})
        tool_defs=[]
        for tool in tools or []:
            function=tool.get("function",tool); tool_defs.append({"type":"function","name":function.get("name"),"description":function.get("description","") ,"parameters":function.get("parameters") or function.get("input_schema") or {"type":"object","properties":{}}})
        payload={"model":kwargs.get("model") or "gpt-5.3-codex","instructions":instructions,"input":converted,"tools":tool_defs,"tool_choice":"auto","parallel_tool_calls":True,"store":False,"stream":True}
        effort=kwargs.get("reasoning_effort");
        if effort: payload["reasoning"]={"effort":effort,"summary":"auto"}
        headers={"Authorization":f"Bearer {auth['access_token']}","Content-Type":"application/json","Accept":"text/event-stream","User-Agent":"codex_cli_rs/1.0","originator":"codex_cli_rs"}
        if auth.get("account_id"): headers["ChatGPT-Account-Id"]=auth["account_id"]
        from config import conf
        proxy=str(conf().get("proxy") or "").strip()
        session=requests.Session(); session.trust_env=False
        if proxy: session.proxies.update({"http":proxy,"https":proxy})
        response=session.post(f"{CODEX_BASE_URL}/responses",headers=headers,json=payload,stream=True,timeout=kwargs.get("timeout") or 180)
        if response.status_code>=400: return {"error":True,"message":f"Codex 请求失败 HTTP {response.status_code}: {response.text[:500]}","status_code":response.status_code}
        events=[]
        for line in response.iter_lines(decode_unicode=True):
            if isinstance(line, bytes): line=line.decode("utf-8", errors="replace")
            if not line or not line.startswith("data:"): continue
            raw=line[5:].strip()
            if raw=="[DONE]": break
            try: events.append(json.loads(raw))
            except Exception: pass
        result=self._result(events)
        if stream:
            message=result["choices"][0]["message"]
            chunk={"choices":[{"index":0,"delta":message,"finish_reason":result["choices"][0]["finish_reason"]}]}
            def generator(): yield chunk
            return generator()
        return result
    def reply_text(self, query, context=None):
        result = self.call_with_tools([{"role":"user","content":str(query)}], stream=False)
        if result.get("error"): return str(result.get("message") or "Codex 请求失败")
        return str(result.get("choices", [{}])[0].get("message", {}).get("content") or "")
    @staticmethod
    def _result(events):
        texts=[]; calls=[]
        for event in events:
            etype=event.get("type","")
            if etype=="response.output_text.delta": texts.append(event.get("delta", ""))
            if etype=="response.output_item.done":
                item=event.get("item") or {}
                if item.get("type")=="function_call": calls.append({"id":item.get("call_id") or item.get("id"),"type":"function","function":{"name":item.get("name"),"arguments":item.get("arguments") or "{}"}})
        message={"role":"assistant","content":"".join(texts) or None}
        if calls: message["tool_calls"]=calls
        return {"choices":[{"index":0,"message":message,"finish_reason":"tool_calls" if calls else "stop"}]}
