"""Deliverable exports (derived from the store) and the source-grounded accuracy check."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from .config import Config
from .pipeline import retry_command, staleness
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
            "settings": a["settings"], "mode": a["mode"],
            "status": a["status"], "owner": a["owner"], "next_action": a["next_action"]}


def error_text(a: dict) -> str | None:
    return a["error"]["message"] if a.get("error") else None


def build(store: Store, cfg: Config | None = None, mode: str | None = None) -> tuple[list[dict], dict, list[dict]]:
    """Current records, the queue and superseded attempts. With `cfg`, entries are marked stale when their
    result was produced under settings/policy other than the active ones."""
    snap = store.current()
    records, entries, pending, stale_n = [], [], [], 0
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
                 "extraction_fp": a["extraction_fp"], "mode": a["mode"], "error": error_text(a)}
        st = staleness(a, cfg, mode) if cfg else None
        if st:
            entry["stale"] = st  # still shown (it is the latest result) but flagged; `refresh` updates it
            stale_n += 1
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
             **({"active": {"extraction_fp": cfg.extraction_fp(), "policy_version": cfg.policy_version,
                            "settings": cfg.settings()}, "stale_results": stale_n} if cfg else {}),
             "note": "All entries await human review; approvals belong to the named human reviewers."}
    history = [h for h in store.history() if h["id"] not in {r["attempt_id"] for r in records}]
    return records, queue, history


LOCK_NAME = ".export.lock"


@contextmanager
def _flock(out: Path, exclusive: bool):
    """Advisory lock shared by writers (exclusive) and consistent readers (shared) of the export directory."""
    out.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # Windows: no advisory locks available; readers fall back to verify-and-retry
        yield
        return
    with open(out / LOCK_NAME, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _export_lock(out: Path):
    """Serialise exports across processes. The snapshot must be taken *inside* the lock, otherwise an operator
    who read the database earlier can finish writing after another operator and leave older data in place.
    Readers that take the shared lock (read_consistent) cannot run while files are being replaced."""
    return _flock(out, exclusive=True)


def snapshot_id(records: list[dict], queue: dict) -> str:
    """Content-derived id of one export. Identical state -> identical id; any change -> a different id.

    Stamped into queue.json and into every line of extractions.jsonl so a reader can prove the two files it
    holds come from the same export (generated_at is excluded so an unchanged state keeps its id).
    """
    body = {"records": records, "queue": {k: v for k, v in queue.items() if k not in ("generated_at", "snapshot_id")}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def export(store: Store, cfg: Config) -> dict:
    out = Path(cfg.out_dir)
    with _export_lock(out):
        records, queue, history = build(store, cfg)
        sid = snapshot_id(records, queue)
        queue["snapshot_id"] = sid
        for r in records:
            r["snapshot_id"] = sid
        # prior versions live apart from the current outputs; queue.json is written last, it completes the export
        atomic_write(out / "history.jsonl", "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in history))
        atomic_write(out / "extractions.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        atomic_write(out / "queue.json", json.dumps(queue, indent=2, ensure_ascii=False) + "\n")
    return {"records": len(records), "queue": len(queue["entries"]), "history": len(history),
            "pending": len(queue["pending"]), "snapshot_id": sid}


class InconsistentSnapshot(Exception):
    """queue.json and extractions.jsonl come from different exports (and did not settle within the retries)."""


def read_consistent(out_dir: str | Path, attempts: int = 50, pause_s: float = 0.02) -> tuple[list[dict], dict]:
    """Read (records, queue) guaranteed to come from one export.

    Two files cannot be replaced in a single atomic operation, so consistency is enforced on the read side in two
    layers: (1) hold the shared export lock, which excludes any writer for the duration of the read; (2) verify
    that every record and the queue carry the same snapshot_id, retrying if not (covers platforms without flock
    and writers that do not follow the protocol). Raises InconsistentSnapshot rather than return a mixed pair.
    """
    out = Path(out_dir)
    last = "no attempt made"
    for _ in range(max(1, attempts)):
        with _flock(out, exclusive=False):
            records = [json.loads(line) for line in (out / "extractions.jsonl").read_text("utf-8").splitlines()]
            queue = json.loads((out / "queue.json").read_text("utf-8"))
        ids = {r.get("snapshot_id") for r in records} | {queue.get("snapshot_id")}
        if len(ids) == 1 and None not in ids:
            return records, queue
        last = f"snapshot ids seen: {sorted(map(str, ids))}"
        time.sleep(pause_s)
    raise InconsistentSnapshot(f"extractions.jsonl and queue.json never agreed ({last})")


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
