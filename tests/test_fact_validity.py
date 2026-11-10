from datetime import datetime, timedelta, timezone

from intelligence_center.fact_store import FactStore


def test_expired_and_other_project_facts_are_not_retrieved(tmp_path):
    store = FactStore(tmp_path)
    expired = store.upsert(content="旧提醒时间是七点", fact_type="preference",
                           valid_until=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    scoped = store.upsert(content="项目甲只用测试邮箱", fact_type="preference", applies_to="project-a")
    assert expired["id"] not in {item["id"] for item in store.search("提醒")}
    assert scoped["id"] not in {item["id"] for item in store.search("测试邮箱")}
    assert scoped["id"] in {item["id"] for item in store.search("测试邮箱", context="project-a")}
    assert expired["id"] in {item["id"] for item in store.search("提醒", include_disabled=True)}


def test_same_content_can_have_independent_project_scope(tmp_path):
    store = FactStore(tmp_path)
    first = store.upsert(content="只发简短消息", applies_to="project-a")
    second = store.upsert(content="只发简短消息", applies_to="project-b")
    assert first["id"] != second["id"]


def test_user_can_change_validity_and_scope_without_losing_source(tmp_path):
    store = FactStore(tmp_path)
    fact = store.upsert(content="工作日提醒九点", source="用户消息")
    changed = store.set_constraints(fact["id"], applies_to="project-work",
                                    valid_until=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
    assert changed["source"] == "用户消息"
    assert store.search("提醒") == []
    assert store.search("提醒", context="project-work")[0]["id"] == fact["id"]
    store.set_constraints(fact["id"])
    assert store.search("提醒")[0]["id"] == fact["id"]
