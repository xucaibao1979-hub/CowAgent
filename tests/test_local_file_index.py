import os
from pathlib import Path

from common.local_file_index import LocalFileIndex, format_file_search_reply, parse_file_search_intent


def test_parse_wechat_file_search_conditions():
    intent = parse_file_search_intent("帮我找文件：技术部周报，最近7天，Word，按最新排序")
    assert intent is not None
    assert intent.query == "技术部周报"
    assert intent.recent_days == 7
    assert intent.extensions == {".doc", ".docx"}


def test_non_file_question_is_not_intercepted():
    assert parse_file_search_intent("帮我查一下上海天气") is None


def test_multiple_identity_photo_candidates_offer_numbered_followup():
    intent = parse_file_search_intent("帮我查询我电脑里面一张徐才宝的身份证照片发给我")
    assert intent is not None
    hits = [
        {"name": "徐才宝身份证.jpg", "path": "D:/资料新/徐才宝身份证.jpg", "mtime": 1},
        {"name": "徐才宝身份证-水印.jpg", "path": "D:/资料新/徐才宝身份证-水印.jpg", "mtime": 1},
    ]
    reply = format_file_search_reply(intent, hits, requested_send=True, sensitive_identity=True)
    assert "发送第N个" in reply
    assert "尚未核对图片内容" in reply
    assert "当前 D 盘仅授权搜索" not in reply


def test_parse_named_desktop_image_send_request():
    intent = parse_file_search_intent("帮我将桌面的一个叫IT小爱的图片发给我")
    assert intent.query == "IT小爱"
    assert ".png" in intent.extensions


def test_parse_standalone_exact_filename_as_followup():
    intent = parse_file_search_intent("网络安全应急预案框架.docx")
    assert intent is not None
    assert intent.query == "网络安全应急预案框架.docx"
    assert intent.extensions == {".docx"}


def test_parse_filename_label_and_person_id_photo():
    for message in ("文件名 正面.jpg", "文件名：正面.jpg", "文件名称是正面.jpg"):
        intent = parse_file_search_intent(message)
        assert intent is not None
        assert intent.query == "正面.jpg"
        assert intent.extensions == {".jpg"}
    intent = parse_file_search_intent("帮我查询我电脑里面一张徐才宝的身份证照片发给我")
    assert intent is not None
    assert intent.query == "徐才宝"
    assert {".jpg", ".jpeg", ".png"}.issubset(intent.extensions)


def test_desktop_file_send_to_contact_is_not_a_filename_search():
    assert parse_file_search_intent("打开企业微信，给刘伟发送我桌面上的文件，文件名 2.png") is None
    assert parse_file_search_intent("给刘伟发送桌面上的 2.png") is None


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


def test_person_name_can_match_directory_without_reading_image(tmp_path, monkeypatch):
    root = tmp_path / "files"
    target = root / "徐才宝" / "正面.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"test-only")
    index = LocalFileIndex()
    index.path = tmp_path / "path-index.sqlite3"
    index._ensure_schema()
    monkeypatch.setattr(index, "roots", lambda: [root])
    index.refresh()
    hits = index.search(parse_file_search_intent("帮我找徐才宝的身份证照片"))
    assert [Path(item["path"]) for item in hits] == [target]


def test_ambiguous_identity_photos_are_not_automatically_selected():
    intent = parse_file_search_intent("文件名 正面.jpg")
    hits = [
        {"name": "正面.jpg", "path": r"D:\甲\正面.jpg", "mtime": 1},
        {"name": "正面.jpg", "path": r"D:\乙\正面.jpg", "mtime": 2},
    ]
    reply = format_file_search_reply(intent, hits)
    assert "多份同名证件照片" in reply
    assert "暂不自动发送" in reply
    broad = parse_file_search_intent("帮我找徐才宝的身份证照片发给我")
    assert "暂不自动发送" in format_file_search_reply(broad, hits, requested_send=True)
    assert "不会仅凭文件名自动发送" in format_file_search_reply(intent, hits[:1], sensitive_identity=True)


def test_desktop_contract_request_uses_desktop_scope(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    elsewhere = tmp_path / "Documents"
    desktop.mkdir()
    elsewhere.mkdir()
    target = desktop / "转Word_房屋租赁合同-测试.docx"
    target.write_bytes(b"test-only")
    (elsewhere / "房屋租赁合同-其他.docx").write_bytes(b"other")
    index = LocalFileIndex()
    index.path = tmp_path / "desktop-index.sqlite3"
    index._ensure_schema()
    monkeypatch.setattr(index, "roots", lambda: [desktop, elsewhere])
    monkeypatch.setattr(index, "_desktop_path", lambda: desktop)
    index.refresh()
    intent = parse_file_search_intent("把我电脑桌面上有个房屋租赁合同发给我")
    assert intent is not None
    assert intent.query == "房屋租赁合同"
    assert intent.location == "desktop"
    assert [Path(hit["path"]) for hit in index.search(intent)] == [target]


def test_explicit_path_is_checked_directly_and_missing_path_stays_in_same_folder(tmp_path, monkeypatch):
    root = tmp_path / "files"
    folder = root / "个人资料"
    folder.mkdir(parents=True)
    actual = folder / "徐才宝-正面.jpg"
    actual.write_bytes(b"test-image")
    typo = folder / "正面n.jpg"
    typo.write_bytes(b"test-typo")
    index = LocalFileIndex()
    index.path = tmp_path / "explicit-index.sqlite3"
    index._ensure_schema()
    monkeypatch.setattr(index, "roots", lambda: [root])

    found = parse_file_search_intent(f"这个发给我 {actual}")
    assert found.explicit_path == str(actual)
    assert [Path(hit["path"]) for hit in index.search(found)] == [actual]

    missing = parse_file_search_intent(f"这个发给我 {folder / '正面.jpg'}")
    assert index.search(missing) == []
    assert {Path(hit["path"]) for hit in index.suggest_nearby(missing)} == {actual, typo}
