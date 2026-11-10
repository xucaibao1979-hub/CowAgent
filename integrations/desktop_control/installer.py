"""Fixed allow-list of desktop applications the settings page can install."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from integrations.desktop_control.policy import APPS


PACKAGE_IDS = {
    "wecom": "Tencent.WeCom",
    "chrome": "Google.Chrome",
    "edge": "Microsoft.Edge",
}
EXCEL_INSTALL_URL = "https://m365.cloud.microsoft/apps"
_DISPLAY_NAMES = {
    "wecom": ("企业微信", "wecom"),
    "chrome": ("google chrome",),
    "edge": ("microsoft edge",),
    "excel": ("microsoft office", "microsoft 365", "office 16"),
}


def winget_path() -> str:
    if os.name != "nt":
        return ""
    candidate = shutil.which("winget")
    if candidate and Path(candidate).is_file():
        return candidate
    fallback = Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "winget.exe"
    return str(fallback) if fallback.is_file() else ""


def _clean_icon_path(value: str) -> str:
    raw = str(value or "").strip().strip('"')
    raw = raw.rsplit(",", 1)[0].strip().strip('"')
    return raw if Path(raw).is_file() else ""


def installed_executable(app: str) -> str:
    """Use Windows registration, including nonstandard install directories."""
    if os.name != "nt" or app not in APPS:
        return ""
    import winreg

    for process in APPS[app]["processes"]:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for branch in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                           r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"):
                try:
                    with winreg.OpenKey(hive, branch + "\\" + process) as key:
                        path = _clean_icon_path(winreg.QueryValueEx(key, "")[0])
                    if path:
                        return path
                except OSError:
                    pass
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for branch in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                       r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, branch) as parent:
                    count = winreg.QueryInfoKey(parent)[0]
                    for index in range(count):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0]).lower()
                                if not any(label in name for label in _DISPLAY_NAMES[app]):
                                    continue
                                icon = _clean_icon_path(winreg.QueryValueEx(key, "DisplayIcon")[0])
                                if icon and Path(icon).name.lower() in APPS[app]["processes"]:
                                    return icon
                        except OSError:
                            continue
            except OSError:
                continue
    return ""


def install_command(app: str) -> list[str]:
    if app not in PACKAGE_IDS:
        raise ValueError("此应用不支持自动安装")
    winget = winget_path()
    if not winget:
        raise RuntimeError("系统未安装 WinGet，请先从 Microsoft App Installer 安装")
    return [winget, "install", "--id", PACKAGE_IDS[app], "--exact", "--source", "winget",
            "--silent", "--disable-interactivity", "--no-upgrade",
            "--accept-source-agreements", "--accept-package-agreements"]


def install_app(app: str) -> tuple[bool, str]:
    command = install_command(app)
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=900, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "安装超时，请检查网络或安装窗口"
    output = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode:
        return False, (output[-500:] or f"WinGet 返回代码 {result.returncode}")
    return True, "安装命令已完成"


def launch_app(app: str) -> str:
    path = installed_executable(app)
    if not path:
        raise FileNotFoundError("未找到已安装的应用程序，请完成安装后刷新状态")
    subprocess.Popen([path], close_fds=True)
    return path
