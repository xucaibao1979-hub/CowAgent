"""Small local MCP stdio server for Windows UI Automation.

The server reads the policy for every tool call so toggles take effect without
restarting CowAgent. It never interprets arbitrary shell commands or paths.
"""

import json
import hashlib
import os
import subprocess
import sys
import time
import traceback
from functools import lru_cache
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integrations.desktop_control.policy import APPS, app_allowed, app_for_process, policy_path, read_policy, running_processes
from integrations.desktop_control.session import desktop_session_status

_pending_wecom_contact = None
_selected_wecom_contact = None
_staged_wecom_file = None
_staged_wecom_text = None
_selected_wecom_header = None


def _desktop():
    if os.name != "nt":
        raise RuntimeError("电脑操控仅支持 Windows")
    try:
        from pywinauto import Desktop
    except ImportError as error:
        raise RuntimeError("缺少 pywinauto，请安装桌面操控依赖") from error
    return Desktop(backend="uia")


def _windows(app=None):
    policy = read_policy()
    if app and not app_allowed(app, policy):
        raise PermissionError(f"{app} 的电脑操控权限未开启")
    if not policy["any_app"] and not any(policy["apps"].values()):
        return []
    import win32gui
    import win32process
    from pywinauto.controls.hwndwrapper import HwndWrapper
    pids = running_processes()
    result = []
    def collect(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            process = pids.get(pid, "")
            key = app_for_process(process)
            if app and key != app and process != app:
                return
            if not app_allowed(key or process, policy):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if title:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                result.append((HwndWrapper(hwnd), {"app": key or process, "title": title, "pid": pid,
                                                  "width": right - left, "height": bottom - top}))
        except Exception:
            return
    win32gui.EnumWindows(collect, None)
    result.sort(key=lambda item: item[1]["width"] * item[1]["height"], reverse=True)
    return result


def _window(app, title):
    matches = [(window, info) for window, info in _windows(app)
               if title.casefold() in info["title"].casefold()]
    if len(matches) != 1:
        raise ValueError(f"找到 {len(matches)} 个匹配窗口，请提供更准确的窗口标题")
    return matches[0][0]


def _controls(window, app, limit=120):
    if app == "wecom":
        return []  # WeCom's current custom window exposes no UIA descendants.
    items = []
    for control in _desktop().window(handle=window.handle).descendants():
        if len(items) >= limit:
            break
        try:
            info = control.element_info
            name = (control.window_text() or "").strip()[:120]
            control_id = (info.automation_id or "").strip()
            if name or control_id:
                items.append({"name": name, "id": control_id,
                              "type": info.control_type})
        except Exception:
            continue
    return items


def _control(window, name, control_type=""):
    matches = []
    for control in _desktop().window(handle=window.handle).descendants():
        try:
            info = control.element_info
            if control_type and info.control_type != control_type:
                continue
            if name in {(control.window_text() or "").strip(),
                        (info.automation_id or "").strip()}:
                matches.append(control)
        except Exception:
            continue
    if len(matches) != 1:
        raise ValueError(f"找到 {len(matches)} 个匹配控件，请先检查窗口控件列表")
    return matches[0]


def _capture(window, filename):
    directory = Path(policy_path()).parent / "desktop_captures"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    window.capture_as_image().save(target)
    rectangle = window.rectangle()
    return {"path": str(target), "width": rectangle.width(), "height": rectangle.height()}


def _browser_window(app, handle):
    if app not in ("chrome", "edge"):
        raise ValueError("仅支持 Chrome 或 Edge")
    for window, info in _windows(app):
        if window.handle == int(handle):
            if _browser_is_isolated_profile(info["pid"]):
                raise ValueError("这是 CowAgent 的独立浏览器窗口，请选择用户已打开的浏览器窗口")
            return window, info
    raise ValueError("浏览器窗口不存在或未获授权")


@lru_cache(maxsize=64)
def _browser_is_isolated_profile(pid):
    """Identify CowAgent's separate Chrome profile by its window process command line."""
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
        capture_output=True, timeout=6,
    )
    if result.returncode != 0:
        raise RuntimeError("无法核实浏览器配置目录，请稍后重试")
    command_line = result.stdout.decode("utf-8", errors="replace").replace("\\", "/").lower()
    return "/.cow/browser_profile" in command_line or "/cow/browser_profile" in command_line


def _browser_tabs(app="chrome", query=""):
    if app not in ("chrome", "edge"):
        raise ValueError("仅支持 Chrome 或 Edge")
    results = []
    for window, info in _windows(app):
        try:
            if _browser_is_isolated_profile(info["pid"]):
                continue
            root = _desktop().window(handle=window.handle)
            tabs = root.descendants(control_type="TabItem")
            for index, tab in enumerate(tabs):
                title = (tab.window_text() or "").strip()
                if query and query.casefold() not in title.casefold():
                    continue
                try:
                    selected = bool(tab.iface_selection_item.CurrentIsSelected)
                except Exception:
                    selected = False
                results.append({"app": app, "window_handle": window.handle,
                                "window_title": info["title"], "tab_index": index,
                                "title": title, "selected": selected})
        except Exception:
            continue
    return results[:80]


def _browser_open_tab(app, handle, url):
    """Open a page inside the user's running browser and reuse its cookies."""
    from urllib.parse import urlsplit
    url = str(url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 2000:
        raise ValueError("需要有效的 HTTP(S) 地址")
    window, _ = _browser_window(app, handle)
    from pywinauto.keyboard import send_keys
    window.set_focus()
    send_keys("^t")
    _paste_focused(url)
    send_keys("{ENTER}")
    time.sleep(2)
    return _browser_read_page(app, handle)


def _browser_select_tab(app, handle, index):
    window, info = _browser_window(app, handle)
    tabs = _desktop().window(handle=window.handle).descendants(control_type="TabItem")
    index = int(index)
    if index < 0 or index >= len(tabs):
        raise ValueError("标签页序号无效，请重新列出标签页")
    window.set_focus()
    tabs[index].click_input()
    title = (tabs[index].window_text() or "").strip()
    verified = False
    for _ in range(5):
        time.sleep(0.2)
        try:
            verified = bool(tabs[index].iface_selection_item.CurrentIsSelected)
        except Exception:
            verified = bool(title and title.casefold() in (window.window_text() or "").casefold())
        if verified:
            break
    if not verified:
        raise RuntimeError("浏览器标签页点击后未确认切换，请重新列出标签页再试")
    return {"selected": True, "verified": True, "title": title,
            "window_handle": window.handle, "screenshot": _capture(window, "browser-selected-tab.png")}


def _browser_wake_tab(app, handle, index):
    """Restore a sleeping tab in the user's existing browser profile."""
    _browser_select_tab(app, handle, index)
    from pywinauto.keyboard import send_keys
    send_keys("^r")
    time.sleep(2)
    return _browser_read_page(app, handle)


def _browser_navigate_tab(app, handle, url):
    """Navigate the selected existing tab without launching another profile."""
    from urllib.parse import urlsplit
    url = str(url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 2000:
        raise ValueError("需要有效的 HTTP(S) 地址")
    window, _ = _browser_window(app, handle)
    from pywinauto.keyboard import send_keys
    window.set_focus()
    send_keys("^l")
    _paste_focused(url)
    send_keys("{ENTER}")
    time.sleep(2)
    return _browser_read_page(app, handle)


def _browser_read_page(app, handle):
    window, _ = _browser_window(app, handle)
    root = _desktop().window(handle=window.handle)
    url = ""
    for control in root.descendants(control_type="Edit")[:12]:
        try:
            value = str(control.iface_value.CurrentValue or "")
            if value.startswith(("http://", "https://")) or "." in value and " " not in value:
                url = value
                break
        except Exception:
            continue
    documents = root.descendants(control_type="Document")
    content = []
    for document in documents:
        try:
            descendants = document.descendants()
            if len(descendants) > len(content):
                content = descendants
        except Exception:
            continue
    lines = []
    seen = set()
    for control in (content or root.descendants())[:1200]:
        try:
            if control.element_info.control_type not in ("Text", "Hyperlink", "Button", "Document"):
                continue
            value = (control.window_text() or "").strip()
            if value and value not in seen and len(value) < 1000:
                seen.add(value)
                lines.append(value)
        except Exception:
            continue
        if sum(map(len, lines)) > 12000:
            break
    return {"url": url, "text": "\n".join(lines)[:12000],
            "screenshot": _capture(window, "browser-current-page.png")}


def _wecom_main():
    windows = [(window, info) for window, info in _windows("wecom")
               if info["title"] != "SearchResultWindow2"]
    if not windows:
        raise RuntimeError("企业微信主窗口未打开")
    return windows[0][0]


def _wecom_header_digest(image):
    if image.width < 800 or image.height < 100:
        raise RuntimeError("企业微信窗口过小，无法核对当前会话")
    return hashlib.sha256(image.crop((350, 15, 800, 60)).tobytes()).hexdigest()


def _assert_selected_wecom_chat(main):
    if not _selected_wecom_contact or not _selected_wecom_header:
        raise RuntimeError("请先搜索并选择收件人，再核对会话顶部姓名")
    if _wecom_header_digest(main.capture_as_image()) != _selected_wecom_header:
        raise RuntimeError("当前会话已变化，请重新搜索并核对收件人")


def _wecom_input_point(main, x, y):
    rectangle = main.rectangle()
    x, y = int(x), int(y)
    if not (340 <= x < rectangle.width() and rectangle.height() * 0.6 <= y < rectangle.height()):
        raise ValueError("输入框坐标不在聊天编辑区")
    return rectangle.left + x, rectangle.top + y


def _clear_wecom_editor():
    from pywinauto.keyboard import send_keys
    send_keys("^a{BACKSPACE}")


def _wecom_search_contact(contact):
    global _pending_wecom_contact, _selected_wecom_contact, _selected_wecom_header
    global _staged_wecom_file, _staged_wecom_text
    if not contact or len(contact) > 80:
        raise ValueError("请提供联系人姓名")
    from pywinauto.keyboard import send_keys
    from pywinauto.mouse import click
    _pending_wecom_contact = None
    _selected_wecom_contact = None
    _selected_wecom_header = None
    _staged_wecom_file = None
    _staged_wecom_text = None
    main = _wecom_main()
    rectangle = main.rectangle()
    click(coords=(rectangle.left + 150, rectangle.top + 38))
    send_keys("^a{BACKSPACE}")
    _paste_focused(contact)
    time.sleep(0.3)
    popup = None
    for _ in range(12):
        popup = next((window for window, info in _windows("wecom")
                      if info["title"] == "SearchResultWindow2"), None)
        if popup:
            break
        time.sleep(0.2)
    if not popup:
        raise RuntimeError("企业微信没有显示联系人搜索结果")
    _pending_wecom_contact = contact
    return {"contact": contact, "search_results": _capture(popup, "wecom-search.png"),
            "next_step": "先用 vision 核对搜索结果中的联系人姓名和部门，再调用 desktop_wecom_select_contact 点选该人。"}


def _wecom_select_contact(contact, x, y):
    global _pending_wecom_contact, _selected_wecom_contact, _selected_wecom_header
    global _staged_wecom_file, _staged_wecom_text
    if contact != _pending_wecom_contact:
        raise ValueError("请先搜索同名联系人并核对搜索结果")
    popup = next((window for window, info in _windows("wecom")
                  if info["title"] == "SearchResultWindow2"), None)
    if not popup:
        raise RuntimeError("联系人搜索结果已关闭，请重新搜索")
    rectangle = popup.rectangle()
    if not (0 <= x < rectangle.width() and 0 <= y < rectangle.height()):
        raise ValueError("联系人坐标超出搜索结果窗口")
    from pywinauto.mouse import click
    click(coords=(rectangle.left + x, rectangle.top + y))
    time.sleep(0.25)
    _pending_wecom_contact = None
    _selected_wecom_contact = contact
    _staged_wecom_file = None
    _staged_wecom_text = None
    result = _capture(_wecom_main(), "wecom-selected.png")
    from PIL import Image
    with Image.open(result["path"]) as image:
        _selected_wecom_header = _wecom_header_digest(image)
    return {"selected_candidate": contact, "conversation": result,
            "verified": False,
            "next_step": "必须用 vision 确认会话顶部姓名是目标联系人；确认后才能点击输入框、输入消息或附件并发送。"}


def _wecom_choose_file(path):
    """Stage one authorized local file in WeCom's current conversation."""
    global _staged_wecom_file
    from common.local_file_index import LocalFileIndex

    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_file():
        raise ValueError("指定路径不是文件")
    if not any(target.is_relative_to(root.resolve()) for root in LocalFileIndex().roots()):
        raise PermissionError("文件不在已授权的本地目录中")
    pickers = [(window, info) for window, info in _windows("wecom")
               if "选择文件" in info["title"]]
    if len(pickers) != 1:
        raise RuntimeError("请先在已核对收件人的企业微信会话中点击附件文件夹图标，再选择文件")
    picker = pickers[0][0]
    controls = _desktop().window(handle=picker.handle)
    edits = [control for control in controls.descendants(control_type="Edit")
             if control.rectangle().top >= picker.rectangle().bottom - 100]
    if len(edits) != 1:
        raise RuntimeError("无法确认文件选择框的文件名输入栏")
    edits[0].set_focus()
    _paste_focused(str(target))
    if edits[0].iface_value.CurrentValue != str(target):
        raise RuntimeError("文件选择框未正确填入文件路径")
    send_buttons = [control for control in controls.descendants(control_type="Button")
                    if control.window_text() == "发送"
                    and control.rectangle().top >= picker.rectangle().bottom - 100]
    if len(send_buttons) != 1:
        raise RuntimeError("无法确认文件选择框的发送按钮")
    send_buttons[0].click_input()
    time.sleep(0.3)
    _staged_wecom_file = target.name
    return {"file": target.name, "staged": True,
            "conversation": _capture(_wecom_main(), "wecom-file-staged.png"),
            "next_step": "文件仅进入输入区，尚未发送。先用 vision 核对会话顶部收件人和输入区文件名，再点击聊天输入区的发送按钮，最后截图核对发送结果。"}


def _resolve_authorized_file(path, location=""):
    """Resolve an exact name from the local metadata index, without opening it."""
    from common.local_file_index import FileSearchIntent, LocalFileIndex

    requested = str(path or "").strip()
    if not requested:
        raise ValueError("请提供文件名或绝对路径")
    index = LocalFileIndex()
    roots = [root.resolve() for root in index.roots()]
    hint = str(location or "").strip().lower()
    desktop_only = hint.startswith("桌面") or hint == "desktop"
    desktop = index._desktop_path().resolve()
    given = Path(requested).expanduser()
    if given.is_absolute():
        candidates = [given.resolve(strict=True)]
    else:
        if given.name != requested:
            raise ValueError("相对路径仅支持文件名；请提供绝对路径或具体文件名")
        direct_roots = [desktop] if desktop_only else roots
        candidates = [root / requested for root in direct_roots if (root / requested).is_file()]
        if not candidates:
            intent = FileSearchIntent(query=requested, extensions={given.suffix.lower()} if given.suffix else set(), limit=20)
            hits = index.search(intent)
            candidates = [Path(hit["path"]) for hit in hits
                          if str(hit.get("name") or "").casefold() == requested.casefold()]
            if desktop_only:
                candidates = [candidate for candidate in candidates if candidate.resolve().is_relative_to(desktop)]
    matches = []
    for candidate in candidates:
        try:
            target = candidate.resolve(strict=True)
        except OSError:
            continue
        if target.is_file() and any(target.is_relative_to(root) for root in roots) and target not in matches:
            matches.append(target)
    if not matches:
        raise FileNotFoundError("授权目录中未找到该文件")
    if len(matches) > 1:
        raise ValueError("找到多个同名文件，请提供绝对路径：" + "；".join(str(item) for item in matches[:3]))
    return matches[0]


def _wecom_prepare_file(path, input_x, input_y, location=""):
    """Find an authorized file in the background and paste it into chat."""
    global _staged_wecom_file
    target = _resolve_authorized_file(path, location)
    from common.file_evidence import inspect_file_evidence
    evidence = inspect_file_evidence(target, path)
    main = _wecom_main()
    _assert_selected_wecom_chat(main)
    from pywinauto.mouse import click
    click(coords=_wecom_input_point(main, input_x, input_y))
    _clear_wecom_editor()
    _paste_focused(str(target), as_file=True)
    _staged_wecom_file = target.name
    return {"file": target.name, "path": str(target), "staged": True,
            "file_evidence": evidence,
            "presentation": "image" if target.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file",
            "conversation": _capture(main, "wecom-file-staged.png"),
            "next_step": "已先清空聊天输入区并直接粘贴文件，尚未发送。用 vision 核对收件人和待发送内容，再调用 desktop_wecom_send_staged_file。图片可能显示为图片预览。"}


def _wecom_send_staged_file(confirmed_contact, confirmed_file, send_x, send_y):
    """Send only the staged attachment after the caller checks its screenshot."""
    global _staged_wecom_file
    if confirmed_contact != _selected_wecom_contact or confirmed_file != _staged_wecom_file:
        raise ValueError("收件人或文件与本次操作不符，请重新核对截图")
    from pywinauto.mouse import click
    main = _wecom_main()
    _assert_selected_wecom_chat(main)
    rectangle = main.rectangle()
    x, y = int(send_x), int(send_y)
    if not (0 <= x < rectangle.width() and 0 <= y < rectangle.height()):
        raise ValueError("发送按钮坐标超出窗口")
    click(coords=(rectangle.left + x, rectangle.top + y))
    _staged_wecom_file = None
    time.sleep(0.7)
    return {"clicked_send": True, "delivered": False,
            "conversation": _capture(main, "wecom-file-after-send.png"),
            "next_step": "核对截图中的文件消息及发送状态；若显示撤回或失败，不得声称送达。"}


def _wecom_prepare_text(text, input_x, input_y):
    """Focus the verified chat and paste the message in one tool call."""
    global _staged_wecom_text
    if not _selected_wecom_contact:
        raise RuntimeError("请先搜索并选择收件人，再核对会话顶部姓名")
    if not text or len(text) > 20000:
        raise ValueError("消息内容不能为空或过长")
    from pywinauto.mouse import click
    main = _wecom_main()
    _assert_selected_wecom_chat(main)
    click(coords=_wecom_input_point(main, input_x, input_y))
    _clear_wecom_editor()
    _paste_focused(text)
    _staged_wecom_text = text
    return {"staged": True, "conversation": _capture(main, "wecom-text-staged.png"),
            "next_step": "先用 vision 核对收件人和输入区完整内容，再调用 desktop_wecom_send_staged_text。"}


def _wecom_send_staged_text(confirmed_contact, confirmed_text, send_x, send_y):
    global _staged_wecom_text
    if confirmed_contact != _selected_wecom_contact or confirmed_text != _staged_wecom_text:
        raise ValueError("收件人或文字与本次操作不符，请重新核对截图")
    from pywinauto.mouse import click
    main = _wecom_main()
    _assert_selected_wecom_chat(main)
    rectangle = main.rectangle()
    x, y = int(send_x), int(send_y)
    if not (0 <= x < rectangle.width() and 0 <= y < rectangle.height()):
        raise ValueError("发送按钮坐标超出窗口")
    click(coords=(rectangle.left + x, rectangle.top + y))
    _staged_wecom_text = None
    time.sleep(0.5)
    return {"clicked_send": True, "delivered": False,
            "conversation": _capture(main, "wecom-text-after-send.png"),
            "next_step": "核对截图中的消息气泡和发送状态后再报告结果。"}


TOOLS = [
    {"name": "desktop_session_status", "description": "检查当前电脑画面是否可交互；远程断开或锁屏时只返回状态，不尝试点击。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "desktop_browser_list_tabs", "description": "先查找用户已经打开的 Chrome/Edge 标签页，复用原有登录状态；查询账户、账单、余额等网站数据前优先调用。不会新开浏览器。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "enum": ["chrome", "edge"]}, "query": {"type": "string", "description": "标签页标题关键词，例如阿里云"}}}},
    {"name": "desktop_browser_select_tab", "description": "选择 desktop_browser_list_tabs 返回的现有标签页，保持原浏览器登录会话。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "enum": ["chrome", "edge"]}, "window_handle": {"type": "integer"}, "tab_index": {"type": "integer"}}, "required": ["app", "window_handle", "tab_index"]}},
    {"name": "desktop_browser_wake_tab", "description": "唤醒已休眠的现有标签页并刷新，保留用户浏览器的登录状态；发现休眠的目标控制台时先调用，再判断是否登录。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "enum": ["chrome", "edge"]}, "window_handle": {"type": "integer"}, "tab_index": {"type": "integer"}}, "required": ["app", "window_handle", "tab_index"]}},
    {"name": "desktop_browser_navigate_tab", "description": "在用户已打开的浏览器当前标签页中访问 HTTP(S) 地址，不创建新的浏览器配置。先选择或唤醒与目标网站同一登录会话的标签页。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "enum": ["chrome", "edge"]}, "window_handle": {"type": "integer"}, "url": {"type": "string"}}, "required": ["app", "window_handle", "url"]}},
    {"name": "desktop_browser_open_tab", "description": "在用户已经打开的 Chrome/Edge 窗口中新建标签页访问网站，直接复用用户浏览器的登录状态；没有匹配标签时用此工具，不启动 CowAgent 独立浏览器。window_handle 取自 desktop_browser_list_tabs。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "enum": ["chrome", "edge"]}, "window_handle": {"type": "integer"}, "url": {"type": "string"}}, "required": ["app", "window_handle", "url"]}},
    {"name": "desktop_browser_read_page", "description": "读取已打开浏览器当前标签页的地址、可访问文本和截图；读取后核实是否登录。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "enum": ["chrome", "edge"]}, "window_handle": {"type": "integer"}}, "required": ["app", "window_handle"]}},
    {"name": "desktop_list_windows", "description": "列出当前获准操控的 Windows 应用窗口。先调用此工具确认目标应用及窗口标题。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string", "description": "应用 ID：wecom、chrome、edge、excel；可省略"}}}},
    {"name": "desktop_open_app", "description": "打开或激活已授权的本机应用。优先激活当前窗口；未运行时从 Windows 注册信息寻找安装路径。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}},
    {"name": "desktop_wecom_search_contact", "description": "企业微信第 1 步：先清空搜索框，再搜索联系人并截取结果。务必用 vision 核对姓名、部门和是否重名；不能直接输入或发送消息。", "inputSchema": {"type": "object", "properties": {"contact": {"type": "string", "description": "要搜索的联系人姓名"}}, "required": ["contact"]}},
    {"name": "desktop_wecom_select_contact", "description": "企业微信第 2 步：点击已核对的联系人搜索结果，打开其对话框并返回会话截图。必须再用 vision 核对会话顶部姓名，随后才可输入消息或附件。", "inputSchema": {"type": "object", "properties": {"contact": {"type": "string"}, "x": {"type": "integer", "description": "搜索结果截图内联系人的横坐标"}, "y": {"type": "integer", "description": "搜索结果截图内联系人的纵坐标"}}, "required": ["contact", "x", "y"]}},
    {"name": "desktop_wecom_prepare_file", "description": "企业微信快捷文件流程：选中并核对联系人后，传入文件名或绝对路径及聊天输入框坐标；工具在授权目录中后台查找文件，先清空输入区，再直接把文件粘贴到输入区。不打开文件或文件选择页面。返回截图供发送前核对。图片会作为图片预览。", "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "文件名或授权目录中的绝对路径"}, "location": {"type": "string", "description": "可选：桌面/desktop，用于限定文件位置"}, "input_x": {"type": "integer"}, "input_y": {"type": "integer"}}, "required": ["path", "input_x", "input_y"]}},
    {"name": "desktop_wecom_send_staged_file", "description": "企业微信快捷发送：仅在用 vision 核对加入输入区的文件截图后调用。传入已核对的收件人、文件名和聊天区发送按钮坐标；返回发送后截图，仍需核对是否成功或被撤回。", "inputSchema": {"type": "object", "properties": {"confirmed_contact": {"type": "string"}, "confirmed_file": {"type": "string"}, "send_x": {"type": "integer"}, "send_y": {"type": "integer"}}, "required": ["confirmed_contact", "confirmed_file", "send_x", "send_y"]}},
    {"name": "desktop_wecom_prepare_text", "description": "企业微信快捷文字流程：选中并核对联系人后，从截图识别输入框坐标；本工具一次完成点击输入框、清空旧草稿、后台复制粘贴新文字并返回截图。必须核对收件人和完整文字后才可发送。", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "input_x": {"type": "integer"}, "input_y": {"type": "integer"}}, "required": ["text", "input_x", "input_y"]}},
    {"name": "desktop_wecom_send_staged_text", "description": "企业微信快捷文字发送：仅在用 vision 核对输入区截图后调用；传入已核对收件人、完整文字和发送按钮坐标。返回发送后截图，仍需核对是否成功。", "inputSchema": {"type": "object", "properties": {"confirmed_contact": {"type": "string"}, "confirmed_text": {"type": "string"}, "send_x": {"type": "integer"}, "send_y": {"type": "integer"}}, "required": ["confirmed_contact", "confirmed_text", "send_x", "send_y"]}},
    {"name": "desktop_inspect_window", "description": "读取目标窗口的可访问控件名称、ID 和类型。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}}, "required": ["app", "title"]}},
    {"name": "desktop_capture_window", "description": "截取已授权应用窗口，返回本地 PNG 路径。若控件列表为空，先截图，再用 vision 工具识别画面。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}}, "required": ["app", "title"]}},
    {"name": "desktop_click_control", "description": "按唯一控件名称或 ID 点击目标窗口中的控件。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}, "name": {"type": "string"}, "control_type": {"type": "string"}}, "required": ["app", "title", "name"]}},
    {"name": "desktop_click_at", "description": "按窗口截图中的相对坐标点击。必须先截图识别目标位置；坐标范围限定在目标窗口内。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["app", "title", "x", "y"]}},
    {"name": "desktop_type_text", "description": "向唯一输入控件输入文本；不会自动按回车发送。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}, "name": {"type": "string"}, "text": {"type": "string"}}, "required": ["app", "title", "name", "text"]}},
    {"name": "desktop_type_focused", "description": "向当前焦点输入框粘贴文本；用于控件不可访问的应用。不会自动发送，粘贴后应截图核对。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"}}, "required": ["app", "title", "text"]}},
    {"name": "desktop_press_key", "description": "向目标窗口发送受限快捷键（Enter、Ctrl+F、Ctrl+A 或 Esc）。发送聊天消息前必须确认联系人身份和内容。", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}, "title": {"type": "string"}, "key": {"type": "string", "enum": ["enter", "ctrl+f", "ctrl+a", "esc"]}}, "required": ["app", "title", "key"]}},
]


def call_tool(name, args):
    if name == "desktop_session_status":
        return desktop_session_status()
    app = str(args.get("app") or "").strip().lower()
    if app and app not in APPS and not read_policy()["any_app"]:
        raise PermissionError("未知应用未获准操控")
    status = desktop_session_status()
    if not status["available"]:
        raise RuntimeError(status["reason"])
    if name == "desktop_list_windows":
        return [info for _, info in _windows(app or None)]
    if name == "desktop_browser_list_tabs":
        return _browser_tabs(app or "chrome", str(args.get("query") or "").strip())
    if name == "desktop_browser_select_tab":
        return _browser_select_tab(app, args.get("window_handle"), args.get("tab_index"))
    if name == "desktop_browser_wake_tab":
        return _browser_wake_tab(app, args.get("window_handle"), args.get("tab_index"))
    if name == "desktop_browser_navigate_tab":
        return _browser_navigate_tab(app, args.get("window_handle"), args.get("url"))
    if name == "desktop_browser_open_tab":
        return _browser_open_tab(app, args.get("window_handle"), args.get("url"))
    if name == "desktop_browser_read_page":
        return _browser_read_page(app, args.get("window_handle"))
    if name == "desktop_wecom_search_contact":
        return _wecom_search_contact(str(args.get("contact") or "").strip())
    if name == "desktop_wecom_select_contact":
        return _wecom_select_contact(str(args.get("contact") or "").strip(),
                                     int(args.get("x")), int(args.get("y")))
    if name == "desktop_wecom_prepare_file":
        return _wecom_prepare_file(str(args.get("path") or ""), args.get("input_x"),
                                   args.get("input_y"), str(args.get("location") or ""))
    if name == "desktop_wecom_send_staged_file":
        return _wecom_send_staged_file(str(args.get("confirmed_contact") or ""),
                                       str(args.get("confirmed_file") or ""),
                                       args.get("send_x"), args.get("send_y"))
    if name == "desktop_wecom_prepare_text":
        return _wecom_prepare_text(str(args.get("text") or ""), args.get("input_x"), args.get("input_y"))
    if name == "desktop_wecom_send_staged_text":
        return _wecom_send_staged_text(str(args.get("confirmed_contact") or ""),
                                       str(args.get("confirmed_text") or ""),
                                       args.get("send_x"), args.get("send_y"))
    if name == "desktop_open_app":
        if not app:
            raise ValueError("需要应用 ID")
        windows = _windows(app)
        if windows:
            windows[0][0].set_focus()
            return {"opened": True, "window": windows[0][1]}
        executable = _find_installed_executable(app)
        if not executable:
            raise RuntimeError("未找到应用安装路径，请先在电脑上启动该应用")
        subprocess.Popen([executable], close_fds=True)
        for _ in range(20):
            time.sleep(0.5)
            windows = _windows(app)
            if windows:
                return {"opened": True, "window": windows[0][1]}
        raise RuntimeError("应用已启动，但暂未找到可操作窗口")
    title = str(args.get("title") or "").strip()
    if not app or not title:
        raise ValueError("需要应用 ID 和窗口标题")
    window = _window(app, title)
    if name == "desktop_inspect_window":
        return _controls(window, app)
    if name == "desktop_capture_window":
        return _capture(window, f"{app}.png")
    if name == "desktop_click_control":
        window.set_focus()
        _control(window, str(args.get("name") or ""), str(args.get("control_type") or "")).click_input()
        return {"clicked": True}
    if name == "desktop_click_at":
        from pywinauto.mouse import click
        rectangle = window.rectangle()
        x, y = int(args.get("x")), int(args.get("y"))
        if x < 0 or y < 0 or x >= rectangle.width() or y >= rectangle.height():
            raise ValueError("点击坐标超出目标窗口")
        click(coords=(rectangle.left + x, rectangle.top + y))
        return {"clicked": True, "x": x, "y": y}
    if name == "desktop_type_text":
        window.set_focus()
        target = _control(window, str(args.get("name") or ""))
        target.set_focus()
        _paste_focused(str(args.get("text") or ""))
        return {"typed": True, "characters": len(str(args.get("text") or ""))}
    if name == "desktop_type_focused":
        window.set_focus()
        _paste_focused(str(args.get("text") or ""))
        return {"typed": True, "characters": len(str(args.get("text") or ""))}
    if name == "desktop_press_key":
        window.set_focus()
        from pywinauto.keyboard import send_keys
        allowed = {"enter": "{ENTER}", "ctrl+f": "^f", "ctrl+a": "^a", "esc": "{ESC}"}
        key = str(args.get("key") or "").lower()
        if key not in allowed:
            raise ValueError("不支持的快捷键")
        send_keys(allowed[key])
        return {"pressed": key}
    raise ValueError("未知桌面工具")


def _paste_focused(text, *, as_file=False):
    """Paste text or a local file, then restore common clipboard formats."""
    import win32clipboard
    import struct
    from pywinauto.keyboard import send_keys
    saved = []
    def open_clipboard():
        for attempt in range(8):
            try:
                win32clipboard.OpenClipboard()
                return
            except Exception:
                if attempt == 7:
                    raise
                time.sleep(0.05)

    changed = False
    try:
        open_clipboard()
        try:
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                try:
                    name = win32clipboard.GetClipboardFormatName(fmt)
                except Exception:
                    name = ""
                if fmt == win32clipboard.CF_HDROP:
                    try:
                        paths = win32clipboard.GetClipboardData(fmt)
                        payload = struct.pack("<IiiII", 20, 0, 0, 0, 1)
                        payload += ("\0".join(paths) + "\0\0").encode("utf-16le")
                        saved.append((fmt, payload))
                    except Exception:
                        pass
                elif fmt in (win32clipboard.CF_UNICODETEXT, win32clipboard.CF_TEXT,
                           win32clipboard.CF_DIB, win32clipboard.CF_DIBV5) or name.lower() in ("png", "image/png"):
                    try:
                        saved.append((fmt, win32clipboard.GetClipboardData(fmt)))
                    except Exception:
                        pass
            win32clipboard.EmptyClipboard()
            changed = True
            if as_file:
                payload = struct.pack("<IiiII", 20, 0, 0, 0, 1)
                payload += (str(text) + "\0\0").encode("utf-16le")
                win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
            else:
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        send_keys("^v")
        if as_file:
            time.sleep(0.3)
    finally:
        if changed:
            open_clipboard()
            try:
                win32clipboard.EmptyClipboard()
                for fmt, data in saved:
                    try:
                        win32clipboard.SetClipboardData(fmt, data)
                    except Exception:
                        pass
            finally:
                win32clipboard.CloseClipboard()


def _find_installed_executable(app):
    if os.name != "nt" or app not in APPS:
        return ""
    import winreg
    for process in APPS[app]["processes"]:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for location in (rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{process}",
                             rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{process}"):
                try:
                    with winreg.OpenKey(hive, location) as key:
                        executable = winreg.QueryValueEx(key, "")[0]
                    if os.path.isfile(executable):
                        return executable
                except OSError:
                    continue
    return ""


def respond(message):
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "cow-desktop-control", "version": "0.1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        try:
            value = call_tool(name, params.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        except Exception as error:
            return {"content": [{"type": "text", "text": f"Error: {error}"}], "isError": True}
    if method == "ping":
        return {}
    raise ValueError("未知 MCP 方法")


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            output = {"jsonrpc": "2.0", "id": message["id"], "result": respond(message)}
        except Exception as error:
            print(traceback.format_exc(), file=sys.stderr)
            output = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                      "error": {"code": -32603, "message": str(error)}}
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
