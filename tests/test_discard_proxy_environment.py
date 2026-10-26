import importlib.util
import os
from pathlib import Path


def _load_config_module():
    path = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location("config_proxy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discard_proxy_is_removed_and_real_proxy_is_preserved(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:7890")
    monkeypatch.setenv("ALL_PROXY", "http://[::1]:9")

    module = _load_config_module()

    assert "HTTP_PROXY" not in os.environ
    assert "ALL_PROXY" not in os.environ
    assert os.environ["HTTPS_PROXY"] == "http://localhost:7890"
    assert set(module._DISCARDED_PROXY_ENV) == {"HTTP_PROXY", "ALL_PROXY"}
