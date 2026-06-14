"""Safe logging helpers: never emit credentials or inline JSON secrets."""
from __future__ import annotations

import json
import re
from typing import Any

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN PRIVATE KEY-----[\s\S]*?-----END PRIVATE KEY-----",
    re.MULTILINE,
)
_JSON_INLINE_RE = re.compile(r"\{\s*\"type\"\s*:\s*\"service_account\"[\s\S]*?\}", re.MULTILINE)
_SENSITIVE_JSON_KEYS = (
    "private_key",
    "private_key_id",
    "client_email",
    "client_id",
    "client_secret",
    "refresh_token",
    "access_token",
)


def sanitize_log_text(text: str, *, max_len: int = 500) -> str:
    """Remove/redact secret material from arbitrary log text."""
    if not text:
        return ""
    out = str(text)
    out = _PRIVATE_KEY_RE.sub("[REDACTED:private_key]", out)
    out = _JSON_INLINE_RE.sub("[REDACTED:service_account_json]", out)
    for key in _SENSITIVE_JSON_KEYS:
        out = re.sub(
            rf'("{re.escape(key)}"\s*:\s*")([^"]*)(")',
            rf'\1[REDACTED:{key}]\3',
            out,
        )
    if len(out) > max_len:
        return out[: max_len - 3] + "..."
    return out


def describe_service_account_path(path: str) -> str:
    """Describe a service-account path/env value without leaking inline JSON."""
    raw = str(path or "").strip()
    if not raw:
        return "missing"
    if raw.startswith("{"):
        return "inline_json_env(unresolved_file)"
    if len(raw) > 120:
        return f"file_path(len={len(raw)})"
    return raw


def format_error_for_log(exc: BaseException, *, max_len: int = 500) -> str:
    """Exception type + sanitized message safe for stdout logs."""
    name = type(exc).__name__
    msg = sanitize_log_text(str(exc), max_len=max_len)
    return f"{name}({msg})"


def safe_repr(value: Any, *, max_len: int = 200) -> str:
    return sanitize_log_text(repr(value), max_len=max_len)
