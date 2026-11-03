"""Credential redaction, applied to every log line (including tracebacks), stored error and evidence file.

Two layers: (1) the exact value of OPENAI_API_KEY, whatever shape it has (gateway keys are not always `sk-…`);
(2) recognisable secret shapes, for keys we were not told about and for what a provider echoes back (masked keys,
Authorization headers, credentials inside URLs, `api_key=` parameters).

Redaction is NOT applied to the exported extraction data: that is the requester's own message text, and rewriting it
would break the exact-substring guarantee of `supporting_quotes`. Nothing that reaches that data comes from the
credential path (errors are redacted where they are created, see pipeline._error).
"""
from __future__ import annotations

import io
import logging
import os
import re

REDACTED = "[REDACTED]"
_DONE = r"(?!\[REDACTED)"  # never re-redact (or mangle) a value that has already been replaced

# (pattern, replacement) - replacement keeps any captured prefix so the surrounding text stays readable
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), REDACTED),                                   # sk-..., sk-proj-...
    (re.compile(r"sk-[A-Za-z0-9_\-]*\*{3,}[A-Za-z0-9]*"), REDACTED),                    # provider's masked echo
    # any Authorization value; the lookahead covers the optional scheme word too, otherwise the regex would backtrack
    # and treat the word "Bearer" itself as the secret in an already-redacted "Bearer [REDACTED]"
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(?!(?:bearer\s+|basic\s+)?\[REDACTED)"
                r"(?:bearer\s+|basic\s+)?[^\s'\",}]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\bbearer\s+" + _DONE + r"[A-Za-z0-9_\-\.~+/=]+"), REDACTED),      # a bare bearer token
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), REDACTED + "@"),                       # user:password@host
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key)['\"]?"
                r"\s*[:=]\s*['\"]?)" + _DONE + r"[^\s'\",&}]+"), r"\1" + REDACTED),     # api_key=..., "api-key": "..."
]


def redact(text: str) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key and len(key) > 6:  # a very short value would mangle ordinary words everywhere
        text = text.replace(key, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    """Redacts the message and its arguments for every handler that sees the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the *final* text, which also covers tracebacks and chained exceptions (`log.exception`), whose
    messages are added after the record's own message and so bypass the filter."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def make_handler(stream: io.TextIOBase | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactFilter())
    return handler


def get_logger() -> logging.Logger:
    log = logging.getLogger("intake")
    if not log.handlers:
        log.addHandler(make_handler())
        log.addFilter(RedactFilter())  # also protects any handler someone attaches later
        log.setLevel(os.environ.get("INTAKE_LOG_LEVEL", "INFO"))
    return log
