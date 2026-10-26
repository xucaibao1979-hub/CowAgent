from plugins.intelligence_center.hub import AutomationHubService


def test_update_rules_and_search_persistent_records(tmp_path):
    root = tmp_path / "intelligence_center"
    root.mkdir()
    (root / "news_tasks.json").write_text('{"version":1,"tasks":[{"id":"abc","name":"旧名称"}]}', encoding="utf-8")
    reports = root / "reports"
    reports.mkdir()
    (reports / "abc-20260825-090000.md").write_text("# AI 行业日报\n\nOpenAI 发布新模型", encoding="utf-8")
    service = AutomationHubService(root)

    task = service.update_task("news", "abc", {"name": "新名称", "rules": {"include": ["发布"], "exclude": ["广告"], "instruction": "按影响排序"}})
    records = service.records(query="OpenAI", date="2026-08-25")

    assert task["name"] == "新名称"
    assert task["rules"]["instruction"] == "按影响排序"
    assert len(records) == 1
    assert records[0]["title"] == "AI 行业日报"
