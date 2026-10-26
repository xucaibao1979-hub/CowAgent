"""Project-scoped document extraction, chunking and lightweight retrieval."""
from __future__ import annotations
import csv, io, json, os, re, threading, uuid
from datetime import datetime

class ProjectKnowledgeService:
    def __init__(self,root):
        self.root=os.path.join(root,"project_knowledge");self.index=os.path.join(root,"project_knowledge.json");self._lock=threading.RLock();os.makedirs(self.root,exist_ok=True)
    @staticmethod
    def now():return datetime.now().astimezone().isoformat(timespec="seconds")
    def read(self):
        try:
            with open(self.index,"r",encoding="utf-8") as f:return json.load(f).get("documents",[])
        except (OSError,ValueError,TypeError):return []
    def write(self,items):
        tmp=self.index+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump({"schema_version":1,"documents":items},f,ensure_ascii=False,indent=2)
        os.replace(tmp,self.index)
    def extract(self,path,ext):
        if ext in {".txt",".md",".csv",".json",".html",".xml",".yaml",".yml"}:
            with open(path,"r",encoding="utf-8",errors="replace") as f:return f.read()
        if ext==".docx":
            from docx import Document
            d=Document(path);return "\n".join(p.text for p in d.paragraphs if p.text.strip())
        if ext==".pdf":
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        raise ValueError("该文件类型暂不支持文本抽取")
    @staticmethod
    def chunks(text,size=900,overlap=120):
        clean=re.sub(r"\r\n?","\n",text).strip();out=[];start=0
        while start<len(clean):
            end=min(len(clean),start+size);part=clean[start:end].strip()
            if part:out.append(part)
            if end==len(clean):break
            start=max(start+1,end-overlap)
        return out
    def add(self,project_id,filename,data):
        if isinstance(data,str):data=data.encode("utf-8")
        safe=os.path.basename(filename);doc_id=uuid.uuid4().hex[:12];folder=os.path.join(self.root,project_id);os.makedirs(folder,exist_ok=True);path=os.path.join(folder,f"{doc_id}-{safe}")
        with open(path,"wb") as f:f.write(data)
        status="indexed";error="";parts=[]
        try:parts=self.chunks(self.extract(path,os.path.splitext(safe)[1].lower()))
        except Exception as e:status="stored";error=str(e)
        item={"id":doc_id,"project_id":project_id,"name":safe,"path":path,"size":len(data),"status":status,"error":error,"chunks":parts,"chunk_count":len(parts),"created_at":self.now()}
        with self._lock:items=self.read();items.append(item);self.write(items)
        return self.public(item)
    @staticmethod
    def public(x):return {k:v for k,v in x.items() if k not in {"path","chunks"}}
    def list(self,project_id):return [self.public(x) for x in self.read() if x.get("project_id")==project_id]
    @staticmethod
    def terms(text):
        text=text.lower();words=re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}",text);chars=[w[i:i+2] for w in words for i in range(max(0,len(w)-1))];return set(words+chars)
    def search(self,project_id,query,limit=6):
        q=self.terms(query);hits=[]
        for doc in self.read():
            if doc.get("project_id")!=project_id:continue
            for i,chunk in enumerate(doc.get("chunks") or []):
                terms=self.terms(chunk);score=len(q&terms)/(max(1,len(q))**0.5)
                if score:hits.append({"document_id":doc["id"],"document_name":doc["name"],"chunk_index":i,"score":round(score,4),"content":chunk})
        return sorted(hits,key=lambda x:x["score"],reverse=True)[:limit]
    def delete(self,project_id,doc_id):
        with self._lock:
            items=self.read();target=next((x for x in items if x.get("project_id")==project_id and x.get("id")==doc_id),None)
            if not target:return False
            try:os.remove(target["path"])
            except OSError:pass
            self.write([x for x in items if x is not target]);return True

_service=None
def get_project_knowledge_service(root):
    global _service
    if _service is None:_service=ProjectKnowledgeService(root)
    return _service
