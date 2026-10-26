import json
from datetime import datetime
from pathlib import Path


CATALOG = [
    {"id":"inbox_digest","roles":["all"],"title":"重要邮件摘要与待办提取","description":"按发件人、主题、附件和重要程度筛选邮件，生成摘要和待办。","prompt":"每天工作日上午整理未读重要邮件，提取待办、截止日期和需要回复的事项，发送摘要给我"},
    {"id":"reply_draft","roles":["manager","sales","service","freelancer"],"title":"邮件回复草稿","description":"结合历史会话和本地资料生成回复，发送前必须审批。","prompt":"收到需要回复的客户邮件时，结合历史会话和项目资料生成回复草稿，必须由我确认后发送"},
    {"id":"file_intake","roles":["all"],"title":"新文件读取与归档","description":"监控本地目录，提取文档内容、分类、重命名并归档。","prompt":"监控我的收件目录，发现新文件后提取内容、生成摘要、按主题分类并保存到项目资料库"},
    {"id":"meeting_prep","roles":["manager","product","sales","research"],"title":"会前准备与会后跟进","description":"会前汇总资料，会后整理结论、行动项和提醒。","prompt":"每天检查即将开始的会议，会前汇总相关邮件和项目资料，会后整理行动项并建立提醒"},
    {"id":"web_monitor","roles":["research","product","marketing","developer","manager"],"title":"网站与行业变化监控","description":"监控网页、RSS、GitHub 和接口，去重后输出变化摘要。","prompt":"每天监控我关注的网站、RSS 和 GitHub 项目，只保留新增或显著变化并生成中文摘要"},
    {"id":"content_analysis","roles":["research","product","marketing","manager"],"title":"多来源内容分析","description":"汇总文件、网页、邮件与会话，比较观点并标注证据。","prompt":"汇总最近的邮件、文件、网页和会话内容，按主题聚类，比较观点并为结论标注来源"},
    {"id":"daily_brief","roles":["all"],"title":"个人工作日报","description":"统一汇总任务、邮件、情报、会议和异常事项。","prompt":"每天下班前汇总今天的任务、邮件、会议、情报和异常事项，生成个人工作日报和明日优先级"},
    {"id":"reminders","roles":["all"],"title":"承诺与截止日期提醒","description":"从邮件、会话和文件中识别时间承诺，建立分级提醒。","prompt":"持续从邮件、会话和文件中识别截止日期和承诺事项，提前一天和一小时提醒我"},
    {"id":"data_pipeline","roles":["analyst","developer","finance","operations"],"title":"数据采集、清洗与报表","description":"定时抓取 API/CSV/网页数据，校验、去重并输出表格和异常。","prompt":"按计划抓取 API、CSV 和网页数据，校验字段、去重、计算指标并输出报表和异常清单"},
    {"id":"webhook_router","roles":["developer","operations","sales","service"],"title":"Webhook 事务路由","description":"收到外部事件后按条件分类、转换并通知或进入审批。","prompt":"收到 Webhook 后按事件类型和优先级分类，转换字段，高风险操作进入审批，其余保存并通知"},
]


class PersonalAgentService:
    def __init__(self, root): self.root=Path(root);self.path=self.root/"personal_agent_profile.json"
    def get(self):
        if not self.path.exists():return self._default()
        try:
            profile={**self._default(),**json.loads(self.path.read_text(encoding="utf-8"))}
            legacy_email_preferences = {
                "",
                "只读取未读和重要邮件；发送前确认",
                "优先处理未读和重要邮件；提取摘要、待办和截止日期；发送前请求审批",
            }
            if str(profile.get("email_preferences") or "").strip() in legacy_email_preferences:
                profile["email_preferences"]=self._default()["email_preferences"]
            return profile
        except (OSError,ValueError):return self._default()
    def save(self,payload):
        profile=self.get()
        from intelligence_center.harness_evolution_service import HarnessEvolutionService
        before = dict(profile)
        for key in ("preferred_name","role","custom_role","industry","goals","preferred_keywords","preferred_sources","work_hours","email_preferences","watched_folders","output_preferences","approval_policy","boundaries","custom_needs"):
            if key in payload:profile[key]=payload[key]
        HarnessEvolutionService(self.root).checkpoint_before_change("用户画像与记忆偏好", before, profile)
        profile["updated_at"]=datetime.now().astimezone().isoformat(timespec="seconds");self.root.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix(".tmp");tmp.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding="utf-8");tmp.replace(self.path);return profile
    def recommendations(self,profile=None):
        profile=profile or self.get();role=str(profile.get("role") or "all");goals=" ".join(profile.get("goals") or []) if isinstance(profile.get("goals"),list) else str(profile.get("goals") or "")
        items=[]
        for item in CATALOG:
            score=2 if "all" in item["roles"] else (3 if role in item["roles"] else 0)
            if any(word in goals for word in ("邮件","数据","情报","提醒","文件","会议")) and any(word in item["title"]+item["description"] for word in goals.replace("，"," ").split()):score+=2
            if score:items.append({**item,"score":score})
        return sorted(items,key=lambda x:(-x["score"],x["title"]))
    @staticmethod
    def _default():return {"preferred_name":"","role":"all","custom_role":"","industry":"","goals":[],"preferred_keywords":[],"preferred_sources":[],"work_hours":"工作日 09:00-18:00","email_preferences":"默认忽略 Bug、发票、ETL、no-reply、订阅、广告营销和系统通知；重点保留合同、续费、付款、审批、客户、故障、权限开通、用印和需要确认的邮件；发送前确认","watched_folders":[],"output_preferences":"中文；先结论后证据；保留来源链接","approval_policy":"risk_based","boundaries":"未经批准不得发送消息、删除文件、安装技能或向外部系统写入数据","custom_needs":"","updated_at":""}
