"""Operator CLI: python -m intake <command>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline, report
from .config import ROOT, Config
from .extract import make_client
from .store import Store


def _files(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        p = Path(t)
        out += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return out


def _client(cfg: Config, args):
    if getattr(args, "mock", False):
        from .mock import MockClient

        return MockClient(), "mock"
    import os

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Export it (or use --mock for offline development).")
    return make_client(cfg), "live"


def cmd_intake(args, cfg, store):
    client, mode = _client(cfg, args)
    files = _files(args.paths)
    if not files:
        sys.exit("no .json exports found")
    res = pipeline.run(files, store, client, cfg, mode, force=args.force)
    summary: dict[str, int] = {}
    for o in res.values():
        summary[o] = summary.get(o, 0) + 1
    print("intake:", ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    print("exported:", report.export(store, cfg))
    return 1 if summary.get("error") else 0


def cmd_retry(args, cfg, store):
    snap = store.current()
    targets = args.documents
    if not targets:  # all failed, or stuck pending past their lease
        import time

        targets = [s["document_id"] for s in snap
                   if (s["current"] and s["current"]["state"] == "error")
                   or (s["pending"] and (s["pending"]["lease_expires"] or 0) < time.time())]
    paths = []
    known = {s["document_id"]: s for s in snap}
    for d in targets:
        item = known.get(Path(d).name)
        if not item:
            sys.exit(f"unknown document {d}")
        att = item["current"] or item["pending"]
        paths.append(Path(att["source_path"]))
    if not paths:
        print("nothing to retry")
        return 0
    client, mode = _client(cfg, args)
    res = pipeline.run(paths, store, client, cfg, mode, force=True)
    print("retry:", res)
    print("exported:", report.export(store, cfg))
    return 0


def cmd_queue(args, cfg, store):
    _, queue, _ = report.build(store)
    if args.json:
        print(json.dumps(queue, indent=2))
        return 0
    rows = [e for e in queue["entries"] if not args.status or e["status"] == args.status]
    print(f"{'DOCUMENT':<16}{'STATUS':<19}{'OWNER':<17}{'NEXT ACTION':<24}DETAIL")
    for e in rows:
        detail = e["error"] or "; ".join(e.get("reasons") or [])
        print(f"{e['document_id']:<16}{e['status']:<19}{e['owner']:<17}{e['next_action']:<24}{detail[:90]}")
    for p in queue["pending"]:
        print(f"{p['document_id']:<16}{'pending':<19}(in progress since {p['since']} by {p['operator']})")
    print(f"\n{queue['documents']} documents; counts={queue['counts']}; pending={len(queue['pending'])}")
    return 0


def cmd_status(args, cfg, store):
    _, queue, _ = report.build(store)
    print(json.dumps({"counts": queue["counts"], "pending": queue["pending"],
                      "failed": [e["document_id"] for e in queue["entries"] if e["status"] == "error"],
                      "policy_version": cfg.policy_version, "extraction_fp": cfg.extraction_fp(),
                      "settings": cfg.settings()}, indent=2))
    return 0


def cmd_history(args, cfg, store):
    for h in store.history(args.document):
        print(json.dumps({k: h[k] for k in ("id", "state", "source_sha256", "extraction_fp", "policy_version",
                                              "status", "owner", "next_action", "mode", "finished_at", "error",
                                              "reused_from")}))
    return 0


def cmd_export(args, cfg, store):
    print(report.export(store, cfg))
    print("evidence:", report.write_evidence(cfg))
    return 0


def cmd_accuracy(args, cfg, store):
    expected = json.loads((ROOT / "tests" / "expected.json").read_text())
    records, _, _ = report.build(store)
    res = report.compare(records, expected)
    print(f"field exact match: {res['field_hits']}/{res['field_total']}")
    print(f"routing match: {res['route_hits']}/{res['route_total']}")
    print(f"factual errors: {len(res['factual_errors'])}; omissions: {len(res['omissions'])}")
    for x in res["factual_errors"] + res["omissions"]:
        print("  ", x)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m intake")
    ap.add_argument("--mock", action="store_true", help="use the offline mock model (no network)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("intake", help="process export files or directories")
    p.add_argument("paths", nargs="+")
    p.add_argument("--force", action="store_true", help="re-run even if source and settings are unchanged")
    p = sub.add_parser("retry", help="retry failed/stuck documents (all if none given)")
    p.add_argument("documents", nargs="*")
    p = sub.add_parser("queue", help="show the current review queue")
    p.add_argument("--status")
    p.add_argument("--json", action="store_true")
    sub.add_parser("status", help="counts, pending, failures, settings")
    p = sub.add_parser("history", help="all attempts, optionally for one document")
    p.add_argument("document", nargs="?")
    sub.add_parser("export", help="write deliverables/ from the store")
    sub.add_parser("accuracy", help="compare current extractions with tests/expected.json")
    args = ap.parse_args(argv)
    cfg = Config()
    store = Store(cfg.db_path)
    return {"intake": cmd_intake, "retry": cmd_retry, "queue": cmd_queue, "status": cmd_status,
            "history": cmd_history, "export": cmd_export, "accuracy": cmd_accuracy}[args.cmd](args, cfg, store)
