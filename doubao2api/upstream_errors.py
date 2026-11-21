"""Classify upstream Dola failures with cooldown extraction and sensitive-data redaction.

Combines doubao2api's strict untrusted-payload classification with flow2api-Camoufox's
Retry-After / cooldown extraction and secret redaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Optional

from .redaction import redact_sensitive_text


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SENSITIVE_WORDS = re.compile(r"challenge|token|cookie|signature|secret|password", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[:=\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _float_positive(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_identifier(value: Any, *, limit: int = 64) -> Optional[str]:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    candidate = str(value).strip()
    if len(candidate) > limit or not _SAFE_IDENTIFIER.fullmatch(candidate):
        return None
    if _SENSITIVE_WORDS.search(candidate):
        return None
    return candidate


def _payload(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Return parsed fields and bounded scrubbed text for classification only."""
    fields: dict[str, Any] = {}
    text: list[str] = []
    pending = [value]
    seen: set[int] = set()
    steps = 0

    while pending:
        steps += 1
        if steps > 64:
            break
        current = pending.pop()
        if isinstance(current, str):
            raw = redact_sensitive_text(current[:2048])
            try:
                decoded = json.loads(raw)
            except (json.JSONDecodeError, TypeError, RecursionError):
                text.append(raw)
                continue
            if decoded is current:
                text.append(raw)
            else:
                pending.append(decoded)
            continue
        if not isinstance(current, dict) or id(current) in seen:
            continue
        seen.add(id(current))
        for key, item in current.items():
            normalized_key = str(key).lower()
            if normalized_key in {
                "error_code",
                "code",
                "error_type",
                "type",
                "error_subtype",
                "subtype",
                "log_id",
                "logid",
                "status",
                "status_code",
                "semantic_reasoning",
                "error_msg",
                "message",
                "description",
                "reason",
                "body",
                "decision",
                "retry_after",
                "retry_after_seconds",
                "cold_down_remain_minutes",
            } and normalized_key not in fields:
                fields[normalized_key] = item
            if normalized_key in {
                "error",
                "body",
                "data",
                "details",
                "detail",
                "semantic_reasoning",
                "extra",
                "decision",
            }:
                pending.append(item)
            elif normalized_key in {
                "error_msg",
                "message",
                "description",
                "reason",
                "error_type",
                "type",
                "error_subtype",
                "subtype",
            }:
                if isinstance(item, str):
                    text.append(redact_sensitive_text(item[:512]))
                elif isinstance(item, (int, float)):
                    text.append(str(item))

    return fields, text


@dataclass(frozen=True)
class UpstreamError:
    category: str
    status_code: int
    code: Optional[str] = None
    error_type: Optional[str] = None
    subtype: Optional[str] = None
    log_id: Optional[str] = None
    retry_after_seconds: Optional[float] = None

    def to_openai_error(self, admin_url: Optional[str] = None) -> dict[str, Any]:
        messages = {
            "rate_limit": "The upstream Dola service is rate limiting requests. Retry later.",
            "verification": "The upstream Dola service requires security verification.",
            "session_expired": "The Dola browser session has expired. Sign in again via /admin LiveView or cookie import.",
            "unknown": "The upstream Dola request failed.",
        }
        message = messages[self.category]
        if self.category == "verification" and admin_url:
            message += (
                f" Open {admin_url} and use Web LiveView (or desktop browser window) to complete verification."
            )
        error: dict[str, Any] = {
            "message": message,
            "type": {
                "rate_limit": "rate_limit_error",
                "verification": "verification_required",
                "session_expired": "authentication_error",
                "unknown": "upstream_error",
            }[self.category],
            "code": self.category,
            "status": self.status_code,
        }
        if self.code:
            error["upstream_code"] = self.code
        if self.error_type:
            error["upstream_type"] = self.error_type
        if self.subtype:
            error["upstream_subtype"] = self.subtype
        if self.log_id:
            error["log_id"] = self.log_id
        if self.retry_after_seconds:
            error["retry_after_seconds"] = int(round(self.retry_after_seconds))
        return {"error": error}


class UpstreamAPIError(RuntimeError):
    def __init__(self, error: UpstreamError):
        self.upstream_error = error
        super().__init__(error.category)


def parse_upstream_error(value: Any, status_code: Any = None) -> UpstreamError:
    """Classify a decoded event, JSON string, malformed value, or missing value."""
    fields, text_values = _payload(value)
    explicit_status = _number(status_code)
    if explicit_status is None:
        explicit_status = _number(fields.get("status_code"))
    if explicit_status is None:
        explicit_status = _number(fields.get("status"))

    raw_code = fields.get("error_code", fields.get("code"))
    safe_code = _safe_identifier(raw_code, limit=40)
    numeric_code = _number(raw_code)
    text = " ".join(text_values).lower()
    semantic_reasoning = fields.get("semantic_reasoning")
    decision_type = str(fields.get("type", "")).strip().lower()
    decision_subtype = str(fields.get("subtype", "")).strip().lower()

    retry_after = _float_positive(fields.get("retry_after_seconds")) or _float_positive(
        fields.get("retry_after")
    )
    if retry_after is None:
        cd_mins = _float_positive(fields.get("cold_down_remain_minutes"))
        if cd_mins:
            retry_after = cd_mins * 60.0
    if retry_after is None:
        m = _RETRY_AFTER_RE.search(text)
        if m:
            retry_after = _float_positive(m.group(1))

    if explicit_status == 429 or numeric_code == 429:
        category, mapped_status = "rate_limit", 429
    elif (
        decision_type == "verify"
        or decision_subtype == "semantic_reasoning"
        or bool(semantic_reasoning)
        or numeric_code == 710022002
    ):
        category, mapped_status = "verification", 503
    elif (
        explicit_status == 401
        or numeric_code in (401, 13)
        or re.search(
            r"session.{0,20}expir|login.{0,20}expir|auth.{0,20}expir|please sign in|account_info_error|登录已过期|会话已过期|cookie.{0,20}expir",
            text,
        )
    ):
        category, mapped_status = "session_expired", 401
    elif re.search(
        r"rate[ _-]?limit|too many requests|quota exceeded|daily limit|频率限制|请求过于频繁", text
    ):
        category, mapped_status = "rate_limit", 429
    elif re.search(r"captcha|verification|verify|human.?check|人机验证|验证码", text):
        category, mapped_status = "verification", 503
    else:
        category, mapped_status = "unknown", 502

    if retry_after is None:
        if category == "rate_limit":
            retry_after = 60.0
        elif category == "verification":
            retry_after = 300.0

    return UpstreamError(
        category=category,
        status_code=mapped_status,
        code=safe_code,
        error_type=_safe_identifier(fields.get("error_type", fields.get("type")), limit=40),
        subtype=_safe_identifier(fields.get("error_subtype", fields.get("subtype")), limit=40),
        log_id=_safe_identifier(fields.get("log_id", fields.get("logid"))),
        retry_after_seconds=retry_after,
    )
