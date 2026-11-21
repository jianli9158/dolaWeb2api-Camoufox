"""Proxy URL normalization, masking, and relay detection helpers.

Adapted from flow2api-Camoufox `src/services/proxy_manager.py`.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse


def parse_proxy_line(line: str) -> Optional[str]:
    """Convert flexible user proxy input into a canonical proxy URL.

    Supported formats:
    - http://user:pass@host:port
    - https://user:pass@host:port
    - socks5://user:pass@host:port
    - socks5h://user:pass@host:port
    - socks5://host:port:user:pass
    - st5 host:port:user:pass
    - host:port
    - host:port:user:pass
    """
    if not line:
        return None

    line = line.strip()
    if not line:
        return None

    # st5 host:port:user:pass
    st5_match = re.match(r"^st5\s+(.+)$", line, re.IGNORECASE)
    if st5_match:
        rest = st5_match.group(1).strip()
        if "@" in rest:
            return f"socks5://{rest}"
        parts = rest.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, username = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            return f"socks5://{username}:{password}@{host}:{port}"
        if len(parts) == 2 and parts[1].isdigit():
            return f"socks5://{parts[0]}:{parts[1]}"
        return None

    # Explicit scheme prefix
    if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
        if "@" in line:
            return line
        try:
            protocol_end = line.index("://") + 3
            protocol = line[:protocol_end]
            rest = line[protocol_end:]
            parts = rest.split(":")
            if len(parts) >= 4 and parts[1].isdigit():
                host, port, username = parts[0], parts[1], parts[2]
                password = ":".join(parts[3:])
                return f"{protocol}{username}:{password}@{host}:{port}"
            if len(parts) == 2 and parts[1].isdigit():
                return line
        except Exception:
            return None
        return None

    # Bare host:port:user:pass (check before '@' so passwords containing '@' parse cleanly)
    parts = line.split(":")
    if len(parts) >= 4 and parts[1].isdigit() and "@" not in parts[0]:
        host, port, username = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{username}:{password}@{host}:{port}"

    # Bare user:pass@host:port
    if "@" in line:
        return f"http://{line}"

    # Bare host:port
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"

    return None


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """Normalize proxy URL; empty input returns None, invalid syntax raises ValueError."""
    if proxy_url is None:
        return None
    raw = str(proxy_url).strip()
    if not raw:
        return None
    parsed = parse_proxy_line(raw)
    if not parsed:
        raise ValueError(
            "Invalid proxy URL format. Examples: "
            "http://user:pass@host:port, socks5://user:pass@host:port, "
            "host:port:user:pass, st5 host:port:user:pass"
        )
    return parsed


def proxy_needs_relay(proxy_url: Optional[str]) -> bool:
    """Return True when Camoufox requires LocalSocks5Relay (auth credentials or socks5h)."""
    if not proxy_url:
        return False
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if parsed.username or parsed.password:
        return True
    scheme = (parsed.scheme or "").lower()
    return scheme in ("socks5", "socks5h", "https")


def mask_proxy_url(proxy_url: Optional[str]) -> str:
    """Mask proxy password for safe display in UI and logs."""
    if not proxy_url:
        return ""
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if not parsed.hostname:
        return ""
    scheme = parsed.scheme or "http"
    port = f":{parsed.port}" if parsed.port else ""
    if parsed.username:
        return f"{scheme}://{parsed.username}:***@{parsed.hostname}{port}"
    return f"{scheme}://{parsed.hostname}{port}"
