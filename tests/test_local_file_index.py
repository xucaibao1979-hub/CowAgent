import os
from pathlib import Path

from common.local_file_index import LocalFileIndex, parse_file_search_intent


def test_parse_wechat_file_search_conditions():
    intent = parse_file_search_intent("帮我找文件：技术部周报，最近7天，Word，按最新排序")
    assert intent is not None
    assert intent.query == "技术部周报"
    assert intent.recent_days == 7
    assert intent.extensions == {".doc", ".docx"}


def test_non_file_question_is_not_intercepted():
    assert parse_file_search_intent("帮我查一下上海天气") is None


def test_parse_named_desktop_image_send_request():
    intent = parse_file_search_intent("帮我将桌面的一个叫IT小爱的图片发给我")
    assert intent.query == "IT小爱"
    assert ".png" in intent.extensions


def test_parse_standalone_exact_filename_as_followup():
    intent = parse_file_search_intent("网络安全应急预案框架.docx")
    assert intent is not None
    assert intent.query == "网络安全应急预案框架.docx"
    assert intent.extensions == {".docx"}


def test_index_searches_metadata_and_orders_newest(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    old = root / "技术部周报-旧版.docx"
    new = root / "技术部周报-最新版.docx"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))

    index = LocalFileIndex()
    index.path = tmp_path / "index.sqlite3"
    index._ensure_schema()
    monkeypatch.setattr(index, "roots", lambda: [root])
    assert index.refresh() == 2

    hits = index.search(parse_file_search_intent("找文件：技术部周报 Word"))
    assert [Path(item["path"]).name for item in hits] == [new.name, old.name]


def test_exact_filename_falls_back_to_authorized_disk_when_index_misses(tmp_path, monkeypatch):
    root = tmp_path / "disk"
    target = root / "later-folder" / "网络安全应急预案框架.docx"
    target.parent.mkdir(parents=True)
    target.write_text("content", encoding="utf-8")

    index = LocalFileIndex()
    index.path = tmp_path / "empty-index.sqlite3"
    index._ensure_schema()
    monkeypatch.setattr(index, "roots", lambda: [root])
    # Keep a non-matching row so search does not rebuild the test index first.
    with index._connect() as db:
        db.execute(
            "INSERT INTO files(path,name,ext,mtime,size,root) VALUES(?,?,?,?,?,?)",
            (str(root / "other.txt"), "other.txt", ".txt", 1, 1, str(root)),
        )
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_refresh',?)", (str(__import__('time').time()),))

    hits = index.search(parse_file_search_intent(target.name))
    assert [Path(item["path"]).name for item in hits] == [target.name]
