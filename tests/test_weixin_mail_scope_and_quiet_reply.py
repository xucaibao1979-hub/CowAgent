"""User-facing WeChat replies must preserve the requested scope."""

from datetime import date, timedelta
from unittest.mock import patch

from agent.fast_queries import _email_request, _filter_email_messages_by_date
from channel.weixin import task_first_bridge
from channel.weixin.weixin_channel import WeixinChannel
from plugins.intelligence_center.email_secretary import EmailSecretaryService


def test_weixin_processing_notices_are_opt_in():
    channel = WeixinChannel()
    with patch("channel.weixin.weixin_channel.conf", return_value={}):
        assert channel._processing_notice_enabled() is False
    with patch("channel.weixin.weixin_channel.conf", return_value={"weixin_processing_notice": True}):
        assert channel._processing_notice_enabled() is True


def test_last_week_mail_scope_is_previous_calendar_monday_to_sunday():
    args = _email_request("查上周邮件")
    this_monday = date.today() - timedelta(days=date.today().weekday())
    assert args["date_from"] == (this_monday - timedelta(days=7)).isoformat()
    assert args["date_to"] == (this_monday - timedelta(days=1)).isoformat()
    assert "date" not in args


def test_email_date_filter_excludes_today_and_unknown_dates_from_last_week():
    args = _email_request("查上周邮件")
    messages = [
        {"subject": "last-week", "date": f"{args['date_from']}T12:00:00+08:00"},
        {"subject": "today", "date": date.today().isoformat()},
        {"subject": "unknown", "date": ""},
    ]
    found = _filter_email_messages_by_date(messages, args)
    assert [item["subject"] for item in found] == ["last-week"]


def test_compound_weixin_request_handles_mail_and_news_separately():
    command = "查上周邮件与我项目相关的三条重要新闻，附原文"
    with patch.object(task_first_bridge, "_learning_strategy", return_value={}), \
         patch.object(task_first_bridge, "_handle_email", return_value="上周邮件结果") as email, \
         patch.object(task_first_bridge, "_handle_news_or_policy", return_value="三条新闻及链接") as news:
        response = task_first_bridge.maybe_handle_weixin_task_first(command)
    assert email.call_args.args[0] == "查上周邮件"
    assert "三条重要新闻" in news.call_args.args[0]
    assert "附原文" in news.call_args.args[0]
    assert response == "【邮件】\n上周邮件结果\n\n【新闻】\n三条新闻及链接"


def test_imap_search_is_scoped_before_result_limit(tmp_path):
    class FakeImap:
        search_args = None

        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def select(self, *args): pass
        def search(self, *args):
            FakeImap.search_args = args
            return "OK", [b""]
        def logout(self): pass

    args = _email_request("查上周邮件")
    service = EmailSecretaryService(tmp_path, {
        "source_type": "imap", "imap_host": "mail.test", "username": "test", "password": "test",
    })
    with patch("plugins.intelligence_center.email_secretary.imaplib.IMAP4_SSL", FakeImap):
        assert service._receive({"source_type": "imap", "folder": "INBOX", "max_messages": 3,
                                 "date_from": args["date_from"], "date_to": args["date_to"]}) == []
    assert "SINCE" in FakeImap.search_args
    assert "BEFORE" in FakeImap.search_args


def test_compound_news_clause_limits_results_and_uses_project_topics():
    captured = {}

    class FakeNews:
        def __init__(self, *args): pass
        def preview_sources(self, payload):
            captured["payload"] = payload
            return {"items": [{"title": f"item-{i}", "link": f"https://example.com/{i}"}
                              for i in range(5)], "errors": []}
        def _ai_overview(self, task, items):
            return ""
        def _build_report(self, task, items, errors, overview):
            captured["items"] = items
            return "\n".join(item["link"] for item in items)

    with patch.object(task_first_bridge, "_find_matching_news_task", return_value=(None, None)), \
         patch("intelligence_center.personal_context.load_personal_context", return_value={
             "topics": ["MCP", "Agent"], "source_domains": ["example.com"], "source_urls": []}), \
         patch("intelligence_center.personal_context.google_news_sources", return_value=["https://example.com/feed"]), \
         patch("plugins.intelligence_center.news.NewsAssistantService", FakeNews), \
         patch.object(task_first_bridge, "_save_direct_report"), \
         patch.object(task_first_bridge, "_record_success"):
        reply = task_first_bridge._handle_news_or_policy("查我项目相关的三条重要新闻，附原文", {})

    assert captured["payload"]["keywords"] == ["MCP", "Agent"]
    assert captured["payload"]["limit"] >= 3
    assert "（3 条）" in reply
    assert "https://example.com/2" in reply
    assert "https://example.com/3" not in reply


def test_weekly_tech_news_works_without_configured_sources():
    captured = {}

    class FakeNews:
        def __init__(self, *args): pass
        _google_news_url = staticmethod(lambda query: "https://news.google.com/rss/search?q=" + query)
        def preview_sources(self, payload):
            captured["payload"] = payload
            return {"items": [{"title": f"科技新闻{i}", "link": f"https://example.com/{i}"}
                              for i in range(9)], "errors": []}
        def _ai_overview(self, task, items): return ""
        def _build_report(self, task, items, errors, overview):
            captured["rendered"] = items
            return "\n".join(item["title"] for item in items)

    with patch.object(task_first_bridge, "_find_matching_news_task", return_value=(None, None)), \
         patch("intelligence_center.personal_context.load_personal_context", return_value={}), \
         patch("plugins.intelligence_center.news.NewsAssistantService", FakeNews), \
         patch.object(task_first_bridge, "_save_direct_report"), \
         patch.object(task_first_bridge, "_record_success"):
        reply = task_first_bridge._handle_news_or_policy(
            "帮我看下最近一周重要的it科技新闻热点，整理7条即可，精简内容", {})

    assert captured["payload"]["since_hours"] == 168
    assert captured["payload"]["limit"] >= 7
    assert "when:7d" in captured["payload"]["sources"][0]
    assert "（7 条）" in reply
    assert "科技新闻6" in reply
    assert "科技新闻7" not in reply
