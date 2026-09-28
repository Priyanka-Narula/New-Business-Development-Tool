"""Personal-contact detection and redaction at the storage boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d[\d\s().-]{7,}\d|0\d{8,10}|(?:\d{2,4}[\s()]){2,}\d{2,4})(?!\w)"
)
SENSITIVE_KEYS = {
    "email",
    "contact_email",
    "recruiter_email",
    "phone",
    "phone_number",
    "mobile",
    "telephone",
}


def has_contact_email(text: str) -> bool:
    return bool(EMAIL_RE.search(text or ""))


def redact_personal_contacts(text: str) -> str:
    value = EMAIL_RE.sub("[email redacted]", text or "")
    return PHONE_RE.sub("[phone redacted]", value)


def privacy_safe(value: Any) -> Any:
    """Recursively redact strings and remove explicit contact-detail fields."""

    if isinstance(value, Mapping):
        return {
            str(key): privacy_safe(item)
            for key, item in value.items()
            if str(key).casefold() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [privacy_safe(item) for item in value]
    if isinstance(value, tuple):
        return [privacy_safe(item) for item in value]
    if isinstance(value, str):
        return redact_personal_contacts(value)
    return value
