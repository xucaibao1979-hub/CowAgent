"""Explicit, conversation-free model probes. Never expose upstream error bodies."""
import time
from urllib.parse import quote
from urllib.parse import urlsplit

import requests


def probe_model(config, metadata, provider, model):
    started = time.monotonic()

    def result(code, ok=False):
        return {"status": "success", "available": ok, "code": code,
                "provider": provider, "model": model,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}

    if not isinstance(provider, str) or not isinstance(model, str) or not model.strip():
        return result("missing_model")
    try:
        if provider == "openai-codex":
            from models.openai_codex.auth import auth_status, CodexAuthError
            from models.openai_codex.codex_bot import OpenAICodexBot
            if auth_status().get("state") != "authenticated":
                return result("codex_login_required")
            try:
                data = OpenAICodexBot().call_with_tools(
                    [{"role": "user", "content": "Reply only OK."}],
                    tools=[], model=model, timeout=15)
            except CodexAuthError:
                return result("codex_login_required")
            if data.get("error"):
                return result(_http_code(data.get("status_code")))
        else:
            meta = metadata.get(provider)
            if provider.startswith("custom:"):
                custom = next((p for p in config.get("custom_providers", [])
                               if p.get("id") == provider[7:]), None)
                if not custom:
                    return result("missing_provider")
                key, base = custom.get("api_key"), custom.get("api_base")
            elif meta:
                key = config.get(meta.get("api_key_field"))
                base = config.get(meta.get("api_base_key")) or meta.get("api_base_default")
            else:
                return result("missing_provider")
            if not key:
                return result("missing_key")
            if provider == "gemini" or not base:
                return result("unsupported_protocol")
            parsed = urlsplit(base)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
                return result("invalid_url")
            headers = {"Authorization": "Bearer " + key.strip()}
            if provider == "claudeAPI":
                headers = {"x-api-key": key.strip(), "anthropic-version": "2023-06-01"}
                payload = {"model": model.strip(), "messages": [{"role": "user", "content": "Reply only OK."}],
                           "max_tokens": 32, "stream": False}
                endpoint = "/messages"
            else:
                payload = {"model": model.strip(), "messages": [{"role": "user", "content": "Reply only OK."}],
                           "stream": False}
                endpoint = "/chat/completions"
                payload["max_completion_tokens" if provider == "openai" else "max_tokens"] = 128
            with requests.Session() as session:
                session.trust_env = False
                proxy = config.get("proxy")
                if proxy:
                    session.proxies.update({"http": proxy, "https": proxy})
                if provider == "openai":
                    status, data = _post_openai_probe(session, base, headers, model.strip())
                    if status != 200:
                        return result(_http_code(status))
                else:
                    with session.post(base.rstrip("/") + endpoint, headers=headers,
                                      json=payload, timeout=(5, 15), allow_redirects=False) as response:
                        if response.status_code != 200:
                            return result(_http_code(response.status_code))
                        data = response.json()
            if provider == "openai":
                content = _responses_text(data)
                return result("ok" if content else "empty_reply", bool(content))
            if provider == "claudeAPI":
                return result("ok" if any(p.get("text") for p in data.get("content", [])) else "empty_reply",
                              any(p.get("text") for p in data.get("content", [])))
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        return result("ok" if content else "empty_reply", bool(content))
    except requests.exceptions.SSLError:
        return result("tls_error")
    except requests.exceptions.Timeout:
        return result("timeout")
    except requests.exceptions.ConnectionError:
        return result("connection_error")
    except Exception:
        return result("invalid_response")


def _post_openai_probe(session, base, headers, model):
    """Prefer OpenAI Responses API for newer models, then fall back to chat completions."""
    base = base.rstrip("/")
    if _is_official_openai_base(base):
        with session.get(base + "/models/" + quote(model, safe=""), headers=headers,
                         timeout=(5, 15), allow_redirects=False) as response:
            if response.status_code != 200:
                return response.status_code, {}
    responses_payload = {
        "model": model,
        "input": "Reply only OK.",
        "max_output_tokens": 128,
        "store": False,
    }
    with session.post(base + "/responses", headers=headers, json=responses_payload,
                      timeout=(5, 15), allow_redirects=False) as response:
        if response.status_code == 200:
            return response.status_code, response.json()
        first_status = response.status_code
    if first_status not in (400, 404):
        return first_status, {}
    chat_payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply only OK."}],
        "max_completion_tokens": 128,
        "stream": False,
    }
    with session.post(base + "/chat/completions", headers=headers, json=chat_payload,
                      timeout=(5, 15), allow_redirects=False) as response:
        if response.status_code == 200:
            return response.status_code, response.json()
        return response.status_code if first_status in (404, 400) else first_status, {}


def _is_official_openai_base(base):
    try:
        host = (urlsplit(base).hostname or "").lower()
    except Exception:
        return False
    return host == "api.openai.com" or host.endswith(".api.openai.com")


def _responses_text(data):
    if not isinstance(data, dict):
        return ""
    if data.get("output_text"):
        return data.get("output_text")
    for item in data.get("output", []) or []:
        for part in item.get("content", []) or []:
            if part.get("text"):
                return part.get("text")
            if part.get("type") == "output_text" and part.get("text"):
                return part.get("text")
    choices = data.get("choices") or []
    if choices:
        return choices[0].get("message", {}).get("content")
    return ""


def _http_code(status):
    return {401: "authentication_failed", 403: "permission_denied", 404: "model_or_endpoint_missing",
            429: "quota_or_rate_limit", 400: "invalid_request"}.get(status, "upstream_error")
