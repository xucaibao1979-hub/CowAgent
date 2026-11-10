"""Detect whether Windows UI automation has an interactive desktop."""
from __future__ import annotations

import os


def _wts_connection_state() -> int | None:
    import ctypes
    from ctypes import wintypes

    session_id = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        return None
    buffer = ctypes.c_void_p()
    size = wintypes.DWORD()
    # WTSConnectState = 8; WTSActive = 0, WTSDisconnected = 4.
    if not ctypes.windll.wtsapi32.WTSQuerySessionInformationW(
        None, session_id.value, 8, ctypes.byref(buffer), ctypes.byref(size)
    ):
        return None
    try:
        return ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    finally:
        ctypes.windll.wtsapi32.WTSFreeMemory(buffer)


def _input_desktop_name() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    desktop = user32.OpenInputDesktop(0, False, 0x0001)
    if not desktop:
        return ""
    try:
        name = ctypes.create_unicode_buffer(128)
        needed = wintypes.DWORD()
        if user32.GetUserObjectInformationW(
            desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
        ):
            return name.value
        return ""
    finally:
        user32.CloseDesktop(desktop)


def desktop_session_status() -> dict:
    if os.name != "nt":
        return {"available": False, "state": "unsupported", "reason": "电脑操控仅支持 Windows"}
    state = _wts_connection_state()
    if state is not None and state != 0:
        return {"available": False, "state": "disconnected", "reason": "远程桌面已断开，依赖画面的操作需重新连接"}
    desktop = _input_desktop_name()
    if desktop and desktop.casefold() != "default":
        return {"available": False, "state": "locked", "reason": "电脑已锁屏，依赖画面的操作需解锁"}
    if not desktop:
        return {"available": False, "state": "unavailable", "reason": "当前没有可交互的桌面画面"}
    return {"available": True, "state": "active", "reason": "", "desktop": desktop}
