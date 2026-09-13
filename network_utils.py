from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests


def direct_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.upper().endswith("_PROXY")}
    environment.update(NO_PROXY="*", no_proxy="*")
    return environment


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def normalize_http_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    if not endpoint:
        return ""
    if "://" in endpoint and not re.match(r"^https?://", endpoint, re.I):
        raise ValueError("上报地址仅支持 HTTP/HTTPS")
    normalized = endpoint if re.match(r"^https?://", endpoint, re.I) else f"http://{endpoint}"
    parsed = urlparse(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("上报地址端口无效") from exc
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("上报地址无效")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("上报地址端口无效")
    if re.fullmatch(r"[0-9.]+", parsed.hostname):
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("上报地址 IP 无效") from exc
    return normalized


def normalize_udp_target(value: Any) -> str:
    target = str(value or "").strip()
    if not target:
        return ""
    if "://" in target and not re.match(r"^udp://", target, re.I):
        raise ValueError("实时转发仅支持 UDP")
    normalized = target if re.match(r"^udp://", target, re.I) else f"udp://{target}"
    parsed = urlparse(normalized)
    try:
        port = parsed.port or 5000
    except ValueError as exc:
        raise ValueError("实时转发端口无效") from exc
    if parsed.scheme.lower() != "udp" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("实时转发目标无效")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("实时转发目标只允许 IP/主机名和端口")
    if not 1 <= port <= 65535:
        raise ValueError("实时转发端口无效")
    if re.fullmatch(r"[0-9.]+", parsed.hostname):
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("实时转发目标 IP 无效") from exc
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"udp://{host}:{port}"
