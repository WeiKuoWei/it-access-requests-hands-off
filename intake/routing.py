"""Deterministic review routing per materials/office-policy.md (rules applied in order)."""
from __future__ import annotations

import re
from pathlib import PurePath

REQUIRED = [
    "request_id", "request_date", "requester", "team", "system", "environment",
    "permission", "business_purpose", "start_date", "end_date", "manager",
]

_OVERRIDE = re.compile(
    r"(system override|ignore (all |any )?(previous|prior|above) (rules|instructions)|bypass approval|"
    r"highest[- ]priority system instruction|mark (this |the )?request as approved|display the api key)",
    re.I,
)

DESK = "IT Service Desk"


def looks_like_override(message: str) -> bool:
    return bool(_OVERRIDE.search(message))


def _tagged(issues: list[str], tag: str) -> bool:
    return any(i.strip().lower().startswith(tag) for i in issues)


def route(extraction: dict, message: str, document_id: str) -> dict:
    """Return {status, owner, next_action, reasons}. Source text is data, never obeyed."""
    e, issues = extraction, extraction["issues"]
    reasons: list[str] = []

    # 1. instruction-override attempt (model-tagged or deterministic backstop)
    if _tagged(issues, "override") or looks_like_override(message):
        return dict(status="review", owner=DESK, next_action="inspect-message",
                    reasons=["instruction-override attempt in message"])

    # 2. withdrawal with request_id and request_date
    if e["kind"] == "withdrawal" and e["request_id"] and e["request_date"]:
        return dict(status="review", owner=DESK, next_action="confirm-withdrawal",
                    reasons=["withdrawal"])

    # 3. needs information
    if e["kind"] in (None, "other"):
        reasons.append("kind is other/null")
    if _tagged(issues, "conflict"):
        reasons.append("conflicting details")
    stem = PurePath(document_id).stem
    if e["request_id"] and e["request_id"] != stem:
        reasons.append(f"identifier mismatch: message says {e['request_id']}, file is {stem}")
    missing = [f for f in REQUIRED if e[f] is None]
    if e["kind"] in ("new", "change") and missing:
        reasons.append("missing: " + ", ".join(missing))
    elif e["kind"] == "withdrawal" and (not e["request_id"] or not e["request_date"]):
        reasons.append("withdrawal missing request_id/request_date")
    if e["start_date"] and e["end_date"] and e["start_date"] > e["end_date"]:
        reasons.append("start_date is after end_date")
    if reasons:
        return dict(status="needs-information", owner=DESK, next_action="clarify", reasons=reasons)

    # 4 / 5. complete new/change requests
    if e["environment"] == "production" or e["permission"] in ("write", "admin"):
        return dict(status="review", owner="Security Review", next_action="review-risk-then-owner",
                    reasons=["production, write or admin access"])
    return dict(status="review", owner="System Owner", next_action="review-request",
                reasons=["staging/read request"])


def error_route() -> dict:
    return dict(status="error", owner=DESK, next_action="retry-extraction", reasons=["extraction failed"])
