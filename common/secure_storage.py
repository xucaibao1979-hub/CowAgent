"""Small cross-platform boundary for secrets persisted by CowAgent.

Windows uses DPAPI so copied files cannot be decrypted by another OS account.
Other platforms retain the restricted-file fallback; the API intentionally
keeps platform details out of channel code so macOS Keychain can replace the
fallback in the packaged build without changing callers.
"""

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes


FORMAT = "cow-secure-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_encrypt(data):
    source, source_buffer = _blob(data)
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "CowAgent local credential", None, None, None, 0,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _dpapi_decrypt(data):
    source, source_buffer = _blob(data)
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def save_secure_json(path, value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if sys.platform == "win32":
        envelope = {
            "format": FORMAT,
            "provider": "windows-dpapi",
            "payload": base64.b64encode(_dpapi_encrypt(raw)).decode("ascii"),
        }
    else:
        envelope = {"format": FORMAT, "provider": "restricted-file", "payload": value}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def load_secure_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        return value, True
    provider = value.get("provider")
    if provider == "windows-dpapi":
        if sys.platform != "win32":
            raise RuntimeError("该微信凭证由 Windows 账户加密，无法在当前系统解密")
        encrypted = base64.b64decode(value.get("payload", ""))
        return json.loads(_dpapi_decrypt(encrypted).decode("utf-8")), False
    return value.get("payload", {}), False
