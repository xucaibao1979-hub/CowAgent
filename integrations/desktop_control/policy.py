"""Shared, fail-closed desktop-control policy and app detection."""

import csv
import io
import json
import os
import subprocess
import threading
from pathlib import Path


APPS = {
    "wecom": {"name": "企业微信", "processes": {"wxwork.exe", "wecom.exe"}},
    "chrome": {"name": "Google Chrome", "processes": {"chrome.exe"}},
    "edge": {"name": "Microsoft Edge", "processes": {"msedge.exe"}},
    "excel": {"name": "Microsoft Excel", "processes": {"excel.exe"}},
}
_lock = threading.RLock()


def policy_path() -> Path:
    configured = os.environ.get("COW_DESKTOP_CONTROL_CONFIG", "").strip()
    if configured:
        return Path(configured)
    return Path.home() / "cow" / "integrations" / "desktop_control.json"


def read_policy(path=None) -> dict:
    result = {"any_app": False, "apps": {key: False for key in APPS}}
    try:
        with open(path or policy_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            result["any_app"] = data.get("any_app") is True
            values = data.get("apps") or {}
            for key in APPS:
                result["apps"][key] = values.get(key) is True
    except (OSError, ValueError, TypeError):
        pass
    return result


def write_policy(data: dict, path=None) -> dict:
    path = Path(path or policy_path())
    cleaned = {
        "any_app": data.get("any_app") is True,
        "apps": {key: (data.get("apps") or {}).get(key) is True for key in APPS},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(cleaned, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    return cleaned


def running_processes() -> dict:
    """Map PID to executable name without requiring a third-party dependency."""
    if os.name != "nt":
        return {}
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True,
            timeout=5, check=True,
        )
        output = result.stdout.decode("mbcs", errors="replace")
        return {int(row[1]): row[0].lower() for row in csv.reader(io.StringIO(output))
                if len(row) > 1 and row[1].isdigit()}
    except (OSError, subprocess.SubprocessError):
        return {}


def app_for_process(name: str):
    lowered = name.lower()
    for key, meta in APPS.items():
        if lowered in meta["processes"]:
            return key
    return None


def app_allowed(app: str, policy=None) -> bool:
    policy = policy or read_policy()
    return bool(policy["any_app"] or policy["apps"].get(app, False))
