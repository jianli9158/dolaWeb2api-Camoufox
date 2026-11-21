"""Sensitive credential, cookie, proxy, and signed-URL scrubbing helpers.

Adapted from flow2api-Camoufox `src/core/redaction.py` for Dola / ByteDance tokens.
"""

from __future__ import annotations

import re
from typing import Any

_AUTH_COOKIE_NAMES = (
    r"sessionid(?:_ss)?"
    r"|sid_tt"
    r"|sid_guard"
    r"|uid_tt(?:_ss)?"
    r"|ttwid"
    r"|passport_csrf_token(?:_default)?"
    r"|msToken"
    r"|s_v_web_id"
    r"|odin_tt"
    r"|session_tlb_tag"
)

_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE),
    re.compile(r"((?:https?|socks5h?)://[^:/@\s]+:)[^@\s]+(@)", re.IGNORECASE),
    re.compile(
        rf"\b(?:{_AUTH_COOKIE_NAMES})\s*=\s*[^;\s,'\"\]\}}]+",
        re.IGNORECASE,
    ),
)

_SIGNED_QUERY_PARAM_PATTERN = re.compile(
    r"([?&](?:signature|x-signature|a_bogus|x-bogus|mstoken|token|expires|key|access_key|secret_key|session_token)=)[^&#\s'\"\]\}]+",
    re.IGNORECASE,
)

_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-tt-passport-csrf-token",
        "x-amz-security-token",
        "x-ms-token",
    }
)

_SENSITIVE_FIELD_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "secret_key",
        "session_token",
        "sessionid",
        "sid_tt",
        "cookie",
        "cookies",
        "authorization",
        "set-cookie",
        "cookie_header",
        "mstoken",
        "ms_token",
        "passport_csrf_token",
    }
)

_SENSITIVE_URL_FIELD_KEYS = frozenset(
    {"signedurl", "signed_url", "downloadurl", "download_url", "main_url"}
)


def redact_sensitive_text(text: Any, *, redact_signed_urls: bool = True) -> str:
    """Scrub auth headers, Dola session cookies, proxy credentials, and signed query values."""
    raw = str(text or "")
    if not raw:
        return ""
    scrubbed = raw
    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.groups == 2:
            scrubbed = pattern.sub(r"\1<redacted>\2", scrubbed)
        else:
            scrubbed = pattern.sub(
                lambda m: (
                    "Bearer <redacted>"
                    if m.group(0).lower().startswith("bearer ")
                    else (
                        "Basic <redacted>"
                        if m.group(0).lower().startswith("basic ")
                        else f"{m.group(0).split('=', 1)[0]}=<redacted>"
                    )
                ),
                scrubbed,
            )
    if redact_signed_urls:
        scrubbed = _SIGNED_QUERY_PARAM_PATTERN.sub(r"\1<redacted>", scrubbed)
    return scrubbed


def redact_headers(headers: Any) -> dict[str, Any]:
    """Return a copy of HTTP headers with secret headers scrubbed."""
    if not isinstance(headers, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in headers.items():
        key_str = str(key)
        lowered = key_str.lower()
        if lowered in _SENSITIVE_HEADER_KEYS:
            if lowered == "authorization" and isinstance(value, str) and value.lower().startswith("bearer "):
                token = value[7:].strip()
                out[key_str] = f"Bearer {token[:6]}...{token[-4:]}" if len(token) > 12 else "Bearer <redacted>"
            else:
                out[key_str] = "<redacted>"
        else:
            out[key_str] = redact_sensitive_text(value) if isinstance(value, str) else value
    return out


def redact_structure(value: Any, *, redact_urls: bool = False) -> Any:
    """Recursively redact sensitive keys and embedded secret strings from logs/records."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            lowered = key_str.lower()
            if lowered in _SENSITIVE_FIELD_KEYS:
                out[key_str] = "<redacted>"
            elif redact_urls and lowered in _SENSITIVE_URL_FIELD_KEYS and isinstance(item, str):
                out[key_str] = "<redacted:url>"
            else:
                out[key_str] = redact_structure(item, redact_urls=redact_urls)
        return out
    if isinstance(value, list):
        return [redact_structure(item, redact_urls=redact_urls) for item in value]
    if isinstance(value, tuple):
        return [redact_structure(item, redact_urls=redact_urls) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, redact_signed_urls=redact_urls)
    return value
