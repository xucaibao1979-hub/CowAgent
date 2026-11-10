from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge.context import ContextType
from channel.weixin.weixin_channel import WeixinChannel
from common.local_file_index import LocalFileIndex
from PIL import Image


def test_identity_photo_followups_search_without_sending_wrong_file(tmp_path, monkeypatch):
    root = tmp_path / "files"
    owner = root / "徐才宝"
    other = root / "其他人"
    desktop = root / "Desktop"
    owner.mkdir(parents=True)
    other.mkdir(parents=True)
    desktop.mkdir(parents=True)
    for path in (owner / "正面.jpg", owner / "其他.jpg", other / "正面.jpg"):
        Image.new("RGB", (2, 2), "white").save(path)
    contract = desktop / "房屋租赁合同.docx"
    contract.write_bytes(b"test-contract")
    index = LocalFileIndex()
    index.path = tmp_path / "index.sqlite3"
    index._ensure_schema()
    monkeypatch.setattr(index, "roots", lambda: [root])
    monkeypatch.setattr(index, "_desktop_path", lambda: desktop)
    index.refresh()

    channel = WeixinChannel()
    for name, value in {
        "api": Mock(cdn_base_url=""),
        "_recent_file_searches": {},
        "_raw_msg_has_media": Mock(return_value=False),
        "_wait_for_pending_media": Mock(),
        "_send_text": Mock(return_value=True),
        "_send_image": Mock(return_value=True),
        "_send_file": Mock(return_value=True),
        "_persist_direct_exchange": Mock(),
        "_record_weixin_tool_audit": Mock(),
        "_record_weixin_route_success": Mock(),
    }.items():
        monkeypatch.setattr(channel, name, value)

    messages = [
        "帮我查询我电脑里面一张徐才宝的身份证照片发给我",
        "文件好像在D盘，是一张png还是jpeg的照片",
        "文件名 正面.jpg",
    ]
    parsed = [SimpleNamespace(content=message, ctype=ContextType.TEXT) for message in messages]
    file_cache = Mock()
    file_cache.get.return_value = []
    with patch("channel.weixin.weixin_channel.WeixinMessage", side_effect=parsed), \
         patch("channel.file_cache.get_file_cache", return_value=file_cache), \
         patch("common.local_file_index.get_local_file_index", return_value=index):
        for message in messages:
            channel._process_authorized_message({"content": message}, "test-user", "test-token")

    assert channel._send_text.call_count == 3
    assert channel._send_image.call_count == 0
    final_reply = channel._send_text.call_args_list[-1].args[0]
    assert "正面.jpg" in final_reply
    assert "不会仅凭文件名自动发送" in final_reply
    assert str(owner / "正面.jpg") in final_reply
    assert str(other / "正面.jpg") not in final_reply

    new_task = "把我电脑桌面上有个房屋租赁合同发给我"
    with patch("channel.weixin.weixin_channel.WeixinMessage", return_value=SimpleNamespace(content=new_task, ctype=ContextType.TEXT)), \
         patch("channel.file_cache.get_file_cache", return_value=file_cache), \
         patch("common.local_file_index.get_local_file_index", return_value=index):
        channel._process_authorized_message({"content": new_task}, "test-user", "test-token")
    channel._send_file.assert_called_once_with(str(contract), "test-user", "test-token")

    missing_path = owner / "正面0.jpg"
    missing_task = f"这个发给我 {missing_path}"
    with patch("channel.weixin.weixin_channel.WeixinMessage", return_value=SimpleNamespace(content=missing_task, ctype=ContextType.TEXT)), \
         patch("channel.file_cache.get_file_cache", return_value=file_cache), \
         patch("common.local_file_index.get_local_file_index", return_value=index):
        channel._process_authorized_message({"content": missing_task}, "test-user", "test-token")
    missing_reply = channel._send_text.call_args_list[-1].args[0]
    assert "目录可访问，但没有名为" in missing_reply
    assert "正面.jpg" in missing_reply
    assert "其他人" not in missing_reply
    channel._send_image.assert_not_called()

    selection = "发送第1个"
    with patch("channel.weixin.weixin_channel.WeixinMessage", return_value=SimpleNamespace(content=selection, ctype=ContextType.TEXT)), \
         patch("channel.file_cache.get_file_cache", return_value=file_cache), \
         patch("common.local_file_index.get_local_file_index", return_value=index):
        channel._process_authorized_message({"content": selection}, "test-user", "test-token")
    channel._send_image.assert_called_once_with(str(owner / "正面.jpg"), "test-user", "test-token")

    exact_task = f"这个发给我 {owner / '正面.jpg'}"
    with patch("channel.weixin.weixin_channel.WeixinMessage", return_value=SimpleNamespace(content=exact_task, ctype=ContextType.TEXT)), \
         patch("channel.file_cache.get_file_cache", return_value=file_cache), \
         patch("common.local_file_index.get_local_file_index", return_value=index):
        channel._process_authorized_message({"content": exact_task}, "test-user", "test-token")
    assert channel._send_image.call_count == 2
    assert channel._send_image.call_args_list[-1].args == (str(owner / "正面.jpg"), "test-user", "test-token")


def test_legacy_interactive_replies_are_not_replayed_on_new_message(monkeypatch):
    channel = WeixinChannel()
    monkeypatch.setattr(channel, "_read_pending_pushes", Mock(return_value=[
        {"kind": "", "receiver": "test-user", "text": "旧任务结果", "queued_ts": __import__("time").time()},
        {"kind": "proactive", "receiver": "test-user", "text": "有效提醒", "queued_ts": __import__("time").time()},
    ]))
    monkeypatch.setattr(channel, "_write_pending_pushes", Mock())
    monkeypatch.setattr(channel, "_send_text", Mock(return_value=True))
    channel._flush_pending_pushes("test-user", "new-token")
    channel._send_text.assert_called_once_with("有效提醒", "test-user", "new-token")
    channel._write_pending_pushes.assert_called_once_with([])
