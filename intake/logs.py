"""Credential-redacted logging."""
from __future__ import annotations

import logging
import os
import re

_PATTERNS = [re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]+")]


def redact(text: str) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key and len(key) > 6:
        text = text.replace(key, "[REDACTED]")
    for p in _PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def get_logger() -> logging.Logger:
    log = logging.getLogger("intake")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        h.addFilter(RedactFilter())
        log.addHandler(h)
        log.setLevel(os.environ.get("INTAKE_LOG_LEVEL", "INFO"))
    return log
