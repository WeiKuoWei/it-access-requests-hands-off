"""Deliverable exports (derived from the store) and the source-grounded accuracy check."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .config import Config
from .pipeline import retry_command
from .store import Store, now

SCALARS = ["request_id", "request_date", "kind", "requester", "team", "system", "environment",
           "permission", "start_date", "end_date", "manager"]
PRIORITY = {"review": 0, "needs-information": 1, "error": 2}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _ref(a: dict | None) -> dict | None:
    if not a:
        return None
    return {"attempt_id": a["id"], "source_sha256": a["source_sha256"], "finished_at": a["finished_at"],
            "extraction_fp": a["extraction_fp"], "policy_version": a["policy_version"],
            "status": a["status"], "owner": a["owner"], "next_action": a["next_action"]}


def error_text(a: dict) -> str | None:
    return a["error"]["message"] if a.get("error") else None


def build(store: Store) -> tuple[list[dict], dict, list[dict]]:
    snap = store.current()
    records, entries, pending = [], [], []
    for item in snap:
        a, doc = item["current"], item["document_id"]
        if item["pending"]:
            p = item["pending"]
            pending.append({"document_id": doc, "since": p["started_at"], "operator": p["operator"],
                            "source_sha256": p["source_sha256"]})
        if not a:
            la = item["last_attempt"]
            if not item["pending"] and la["state"] == "abandoned":  # interrupted before any result existed
                pending.append({"document_id": doc, "since": la["started_at"], "operator": la["operator"],
                                "source_sha256": la["source_sha256"],
                                "note": "interrupted before finishing; re-run intake or: " + retry_command(doc)})
            continue
        rec = {"document_id": doc, "source_sha256": a["source_sha256"], "extraction": a["extraction"],
               "status": a["status"], "owner": a["owner"], "next_action": a["next_action"],
               "error": error_text(a), "attempt_id": a["id"], "source_path": a["source_path"],
               "extraction_fp": a["extraction_fp"], "policy_version": a["policy_version"],
               "settings": a["settings"], "mode": a["mode"], "finished_at": a["finished_at"]}
        entry = {"document_id": doc, "source_sha256": a["source_sha256"], "attempt_id": a["id"],
                 "status": a["status"], "owner": a["owner"], "next_action": a["next_action"],
                 "reasons": a["reasons"], "policy_version": a["policy_version"],
                 "extraction_fp": a["extraction_fp"], "error": error_text(a)}
        e = a["extraction"]
        if e:
            entry.update(request_id=e["request_id"], requester=e["requester"], kind=e["kind"],
                         system=e["system"], environment=e["environment"], permission=e["permission"],
                         issues=e["issues"])
        if a["state"] == "error":
            entry["retry_command"] = retry_command(doc)
            lv = _ref(item["last_valid"])
            rec["last_valid_result"] = lv
            entry["earlier_result"] = lv  # belongs to an earlier attempt/source version, not current
            if lv and lv["source_sha256"] != a["source_sha256"]:
                entry["earlier_result_note"] = "earlier result is for a different source version"
        records.append(rec)
        entries.append(entry)
    entries.sort(key=lambda x: (PRIORITY.get(x["status"], 9), x["document_id"]))
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    queue = {"generated_at": now(), "documents": len(entries), "counts": counts,
             "pending": pending, "entries": entries,
             "note": "All entries await human review; approvals belong to the named human reviewers."}
    history = [h for h in store.history() if h["id"] not in {r["attempt_id"] for r in records}]
    return records, queue, history


def export(store: Store, cfg: Config) -> dict:
    records, queue, history = build(store)
    out = Path(cfg.out_dir)
    atomic_write(out / "extractions.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    atomic_write(out / "queue.json", json.dumps(queue, indent=2, ensure_ascii=False) + "\n")
    # prior versions live apart from the current outputs
    atomic_write(out / "history.jsonl", "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in history))
    return {"records": len(records), "queue": len(queue["entries"]), "history": len(history),
            "pending": len(queue["pending"])}


# ---------------------------------------------------------------- accuracy
def compare(records: list[dict], expected: dict) -> dict:
    """Exact-match field comparison against hand-authored, source-derived expectations.

    Denominator: 11 scalar fields x documents that have an expected entry and a current record.
    A null extraction (error) scores 0 for its 11 fields. Free text (business_purpose, quotes, issues)
    is not exact-matched. Factual error = a non-null extracted value that differs from the expected value
    (including a value where null was expected); omission = null where a value was expected.
    """
    by = {r["document_id"]: r for r in records}
    per_doc, hits, total, factual, omissions, route_hits = {}, 0, 0, [], [], 0
    for doc, exp in expected.items():
        r = by.get(doc)
        if r is None:
            continue
        ex = r["extraction"]
        miss = []
        for f in SCALARS:
            total += 1
            got = ex[f] if ex else None
            want = exp["fields"][f]
            if got == want:
                hits += 1
            else:
                miss.append(f)
                (omissions if got is None else factual).append(
                    {"document_id": doc, "field": f, "expected": want, "got": got})
        route_ok = all(r[k] == exp["route"][k] for k in ("status", "owner", "next_action"))
        if ex is not None:
            route_hits += route_ok
        per_doc[doc] = {"missed_fields": miss, "route_ok": route_ok, "mode": r["mode"]}
    n_docs = len(per_doc)
    return {"documents": n_docs, "field_hits": hits, "field_total": total, "factual_errors": factual,
            "omissions": omissions, "route_hits": route_hits, "route_total": n_docs, "per_doc": per_doc}


def write_evidence(cfg: Config) -> Path:
    """Credential-redacted example request (built by the same code path as live calls) + response handling."""
    from .extract import build_request

    src = Path(__file__).resolve().parent.parent / "materials" / "requests" / "IT-001.json"
    msg = json.loads(src.read_text(encoding="utf-8"))["message"]
    doc = {
        "kind": "example request built by the live code path, not a recorded transcript; no credential is in the body",
        "endpoint": "POST (OPENAI_BASE_URL or https://api.openai.com/v1)/chat/completions",
        "headers": {"Authorization": "Bearer [REDACTED - taken from OPENAI_API_KEY]"},
        "body": build_request(cfg, msg),
        "settings": {"extraction_fp": cfg.extraction_fp(), **cfg.settings(), "policy_version": cfg.policy_version},
        "retry_policy": {
            "max_attempts": cfg.max_attempts, "backoff": "base*2^n with jitter", "timeout_s": cfg.timeout_s,
            "retried": ["429", "timeout", "connection", "5xx", "truncated", "malformed JSON", "schema", "quote"],
            "not_retried": ["refusal", "content_filter", "auth", "other 4xx"]},
        "response_handling": [
            "no choices -> error 'malformed'",
            "message.refusal set -> error 'refusal' (not retried)",
            "finish_reason 'length' -> 'truncated'; 'content_filter' -> 'refusal'; anything but 'stop' -> 'incomplete'",
            "content not JSON -> 'malformed'; fails snapshot.schema.json -> 'schema'",
            "non-ISO date -> 'schema'; supporting quote not an exact substring of the message -> 'quote'",
            "issue 'conflict: <field>' on a non-null field -> field nulled plus a 'normalized:' issue",
            "final failure -> status error, owner IT Service Desk, next_action retry-extraction, null extraction"],
    }
    p = Path(cfg.out_dir) / "request-evidence.json"
    atomic_write(p, json.dumps(doc, indent=2) + "\n")
    return p
