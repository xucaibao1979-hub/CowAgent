import hashlib
import json
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class ApiKeyStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def list_keys(self):
        return [{k: v for k, v in item.items() if k != "hash"} for item in self._read()["keys"]]

    def create(self, name: str):
        name = str(name or "").strip()
        if not name:
            raise ValueError("密钥名称不能为空")
        raw = "cow_" + secrets.token_urlsafe(32)
        item = {
            "id": secrets.token_hex(6),
            "name": name,
            "prefix": raw[:12],
            "hash": hashlib.sha256(raw.encode()).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used_at": "",
        }
        data = self._read()
        data["keys"].append(item)
        self._write(data)
        return {**{k: v for k, v in item.items() if k != "hash"}, "token": raw}

    def revoke(self, key_id: str):
        data = self._read()
        kept = [item for item in data["keys"] if item["id"] != key_id]
        if len(kept) == len(data["keys"]):
            raise KeyError(key_id)
        data["keys"] = kept
        self._write(data)

    def authenticate(self, token: str):
        digest = hashlib.sha256(str(token or "").encode()).hexdigest()
        data = self._read()
        for item in data["keys"]:
            if secrets.compare_digest(item["hash"], digest):
                item["last_used_at"] = datetime.now(timezone.utc).isoformat()
                self._write(data)
                return {k: v for k, v in item.items() if k != "hash"}
        return None

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "keys": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="api-keys-", suffix=".json", dir=str(self.path.parent))
        try:
            import os
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        finally:
            Path(temp_path).unlink(missing_ok=True)
