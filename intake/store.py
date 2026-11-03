"""SQLite store (WAL). Append-only attempts; 'current' is derived, history is never overwritten."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_sha256 TEXT,
  extraction_fp TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  settings TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','done','error','abandoned')),
  extraction TEXT,
  status TEXT, owner TEXT, next_action TEXT, reasons TEXT,
  error TEXT,
  mode TEXT NOT NULL,
  operator TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  lease_expires REAL,
  reused_from INTEGER
);
CREATE INDEX IF NOT EXISTS ix_attempts_doc ON attempts(document_id, id);
-- at most one live pending attempt per document, even with concurrent operators
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_pending ON attempts(document_id) WHERE state = 'pending';
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def operator() -> str:
    return f"{os.environ.get('USER', 'unknown')}@{os.uname().nodename}:{os.getpid()}"


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("settings", "extraction", "reasons", "error"):
        if d.get(k) is not None:
            d[k] = json.loads(d[k])
    return d


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        try:
            yield c
        finally:
            c.close()

    # ---- writing -------------------------------------------------------
    def claim(self, document_id, source_path, sha, fp, policy, settings, mode, lease_s) -> int | None:
        """Mark a document pending. Returns attempt id, or None if another live worker holds it."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                live = c.execute(
                    "SELECT id, lease_expires FROM attempts WHERE document_id=? AND state='pending'",
                    (document_id,)).fetchone()
                if live:
                    if live["lease_expires"] and live["lease_expires"] > time.time():
                        c.execute("ROLLBACK")
                        return None
                    c.execute("UPDATE attempts SET state='abandoned', finished_at=?, error=? WHERE id=?",
                              (now(), json.dumps({"kind": "abandoned",
                                                  "message": "worker stopped before finishing; lease expired"}),
                               live["id"]))
                cur = c.execute(
                    "INSERT INTO attempts(document_id,source_path,source_sha256,extraction_fp,policy_version,"
                    "settings,state,mode,operator,started_at,lease_expires) VALUES (?,?,?,?,?,?, 'pending',?,?,?,?)",
                    (document_id, source_path, sha, fp, policy, json.dumps(settings), mode, operator(), now(),
                     time.time() + lease_s))
                c.execute("COMMIT")
                return cur.lastrowid
            except Exception:
                c.execute("ROLLBACK")
                raise

    def finish(self, attempt_id, *, state, extraction=None, route=None, error=None, sha=None):
        route = route or {}
        with self._conn() as c:
            c.execute(
                "UPDATE attempts SET state=?, extraction=?, status=?, owner=?, next_action=?, reasons=?, error=?,"
                " source_sha256=COALESCE(?, source_sha256), finished_at=?, lease_expires=NULL"
                " WHERE id=? AND state='pending'",
                (state, json.dumps(extraction) if extraction is not None else None,
                 route.get("status"), route.get("owner"), route.get("next_action"),
                 json.dumps(route.get("reasons")) if route else None,
                 json.dumps(error) if error else None, sha, now(), attempt_id))

    def abandon_own_pending(self) -> int:
        """Release this process's in-flight claims (used on interrupt) so a rerun does not wait out the lease."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE attempts SET state='abandoned', finished_at=?, lease_expires=NULL, error=? "
                "WHERE state='pending' AND operator=?",
                (now(), json.dumps({"kind": "interrupted", "message": "run interrupted before this finished"}),
                 operator()))
            return cur.rowcount

    def add_derived(self, base: dict, *, policy, route, mode, settings=None) -> int:
        """New 'done' attempt reusing a stored extraction (unchanged source re-seen, or re-routing only)."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO attempts(document_id,source_path,source_sha256,extraction_fp,policy_version,settings,"
                "state,extraction,status,owner,next_action,reasons,mode,operator,started_at,finished_at,reused_from)"
                " VALUES (?,?,?,?,?,?, 'done',?,?,?,?,?,?,?,?,?,?)",
                (base["document_id"], base["source_path"], base["source_sha256"], base["extraction_fp"], policy,
                 json.dumps(settings or base["settings"]), json.dumps(base["extraction"]),
                 route["status"], route["owner"], route["next_action"], json.dumps(route["reasons"]),
                 mode, operator(), now(), now(), base["id"]))
            return cur.lastrowid

    # ---- reading -------------------------------------------------------
    def find_done(self, document_id, sha, fp, policy=None, mode=None) -> dict | None:
        """Latest finished result for this exact source + extraction settings (+ policy, + provider mode).

        `mode` keeps mock results from being reused as if they were live provider output, and vice versa.
        """
        q = ("SELECT * FROM attempts WHERE document_id=? AND source_sha256=? AND extraction_fp=? "
             "AND state='done'")
        args = [document_id, sha, fp]
        if mode:
            q += " AND mode=?"
            args.append(mode)
        if policy:
            q += " AND policy_version=?"
            args.append(policy)
        with self._conn() as c:
            return _row(c.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone())

    def latest_finished_id(self, document_id) -> int | None:
        """Id of the document's current (latest done/error) attempt. One indexed query, safe at 2,000+ docs."""
        with self._conn() as c:
            r = c.execute("SELECT id FROM attempts WHERE document_id=? AND state IN ('done','error') "
                          "ORDER BY id DESC LIMIT 1", (document_id,)).fetchone()
            return r["id"] if r else None

    def current(self) -> list[dict]:
        """One entry per document: the latest finished attempt, plus pending/last-valid context."""
        with self._conn() as c:
            c.execute("BEGIN")  # single read snapshot so queue and extractions agree
            rows = [_row(r) for r in c.execute("SELECT * FROM attempts ORDER BY id")]
            c.execute("COMMIT")
        by_doc: dict[str, list[dict]] = {}
        for r in rows:
            by_doc.setdefault(r["document_id"], []).append(r)
        out = []
        for doc, atts in sorted(by_doc.items()):
            finished = [a for a in atts if a["state"] in ("done", "error")]
            pending = next((a for a in reversed(atts) if a["state"] == "pending"), None)
            cur = finished[-1] if finished else None
            last_valid = None
            if cur and cur["state"] == "error":
                last_valid = next((a for a in reversed(finished) if a["state"] == "done"), None)
            out.append({"document_id": doc, "current": cur, "pending": pending, "last_valid": last_valid,
                        "last_attempt": atts[-1], "attempt_count": len(atts)})
        return out

    def history(self, document_id=None) -> list[dict]:
        with self._conn() as c:
            if document_id:
                rs = c.execute("SELECT * FROM attempts WHERE document_id=? ORDER BY id", (document_id,))
            else:
                rs = c.execute("SELECT * FROM attempts ORDER BY id")
            return [_row(r) for r in rs]
