"""Namespaced encrypted credential vault used by MCP/CLI integrations."""
from __future__ import annotations

import threading
from pathlib import Path

from common.secure_storage import load_secure_json, save_secure_json
from common.state_dir import shared_root


class CredentialVault:
    def __init__(self, path=None):
        self.path = Path(path or (shared_root() / "credentials" / "tool_credentials.secure.json"))
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "items": {}}
        value, _legacy = load_secure_json(str(self.path))
        return value if isinstance(value, dict) else {"version": 1, "items": {}}

    def _write(self, value: dict) -> None:
        save_secure_json(str(self.path), value)

    def set(self, namespace: str, name: str, value: str) -> None:
        namespace, name = str(namespace).strip(), str(name).strip()
        if not namespace or not name:
            raise ValueError("凭据命名空间和名称不能为空")
        with self._lock:
            data = self._read()
            data.setdefault("items", {}).setdefault(namespace, {})[name] = str(value)
            self._write(data)

    def get(self, namespace: str, name: str, default=None):
        with self._lock:
            return self._read().get("items", {}).get(str(namespace), {}).get(str(name), default)

    def delete(self, namespace: str, name: str) -> bool:
        with self._lock:
            data = self._read(); bucket = data.get("items", {}).get(str(namespace), {})
            existed = str(name) in bucket
            bucket.pop(str(name), None)
            if existed: self._write(data)
            return existed

    def list_names(self, namespace: str) -> list[str]:
        with self._lock:
            return sorted(self._read().get("items", {}).get(str(namespace), {}).keys())

    def resolve(self, namespace: str, value):
        """Resolve ``credential://NAME`` without exposing values in config files."""
        if not isinstance(value, str) or not value.startswith("credential://"):
            return value
        name = value[len("credential://"):].strip()
        secret = self.get(namespace, name)
        if secret is None:
            raise KeyError(f"凭据 {namespace}/{name} 未配置")
        return secret


credential_vault = CredentialVault()
