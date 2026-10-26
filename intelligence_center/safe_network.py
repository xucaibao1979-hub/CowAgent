"""Outbound URL policy for user-configured fetchers and webhooks."""

import ipaddress
import socket
import urllib.request
from urllib.parse import urljoin, urlsplit


class UnsafeTargetError(ValueError):
    pass


def validate_outbound_url(url, *, allow_private=False):
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeTargetError("仅允许有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise UnsafeTargetError("目标地址不能包含用户名或密码")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        if not allow_private:
            raise UnsafeTargetError("不允许访问本机或内网地址")
        return str(url)
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise UnsafeTargetError(f"无法解析目标主机：{host}") from error
    if not addresses:
        raise UnsafeTargetError("目标主机没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        blocked = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        if blocked and not allow_private:
            raise UnsafeTargetError("不允许访问本机、内网或保留地址")
    return str(url)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private=False):
        self.allow_private = allow_private
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_outbound_url(urljoin(req.full_url, newurl), allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(request, *, timeout=20, allow_private=False, ssl_context=None):
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    validate_outbound_url(url, allow_private=allow_private)
    handlers = [_SafeRedirectHandler(allow_private)]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)
