"""Operator CLI: python -m intake <command>."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from . import pipeline, report
from .config import ROOT, Config
from .extract import make_client
from .store import Store


def _config(mock: bool) -> Config:
    """Active configuration. `--mock` must never write into the real store or the committed deliverables by
    accident, so unless INTAKE_DB / INTAKE_OUT_DIR are set explicitly it uses its own area under data/mock/
    (gitignored). Use `--mock` on every command (intake, queue, accuracy, ...) to look at mock results."""
    cfg = Config()
    if not mock:
        return cfg
    over = {}
    if "INTAKE_DB" not in os.environ:
        over["db_path"] = str(ROOT / "data" / "mock" / "intake.db")
    if "INTAKE_OUT_DIR" not in os.environ:
        over["out_dir"] = str(ROOT / "data" / "mock" / "out")
    if over:
        cfg = dataclasses.replace(cfg, **over)
        print(f"mock mode: using {cfg.db_path} and {cfg.out_dir} (real store and deliverables untouched)",
              file=sys.stderr)
    return cfg


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
    paths, expect = [], {}
    known = {s["document_id"]: s for s in snap}
    for d in targets:
        item = known.get(Path(d).name)
        if not item:
            sys.exit(f"unknown document {d}")
        att = item["current"] or item["pending"] or item["last_attempt"]
        paths.append(Path(att["source_path"]))
        # remember what we saw: if another operator handles this document first, we must not repeat the work
        expect[item["document_id"]] = item["current"]["id"] if item["current"] else 0
    if not paths:
        print("nothing to retry")
        return 0
    client, mode = _client(cfg, args)
    res = pipeline.run(paths, store, client, cfg, mode, force=True, expect=expect)
    print("retry:", res)
    print("exported:", report.export(store, cfg))
    return 1 if "error" in res.values() else 0  # like intake/refresh: non-zero while anything still fails


def cmd_queue(args, cfg, store):
    _, queue, _ = report.build(store, cfg)
    if args.json:
        print(json.dumps(queue, indent=2))
        return 0
    rows = [e for e in queue["entries"] if not args.status or e["status"] == args.status]
    print(f"{'DOCUMENT':<16}{'STATUS':<19}{'OWNER':<17}{'NEXT ACTION':<24}DETAIL")
    for e in rows:
        detail = e["error"] or "; ".join(e.get("reasons") or [])
        if e.get("stale"):
            detail = f"[STALE: {'+'.join(e['stale']['reasons'])}] " + detail
        print(f"{e['document_id']:<16}{e['status']:<19}{e['owner']:<17}{e['next_action']:<24}{detail[:90]}")
    for p in queue["pending"]:
        print(f"{p['document_id']:<16}{'pending':<19}(in progress since {p['since']} by {p['operator']})")
    print(f"\n{queue['documents']} documents; counts={queue['counts']}; pending={len(queue['pending'])}")
    if queue["stale_results"]:
        print(f"{queue['stale_results']} result(s) were produced under different settings/policy than the active "
              "ones. Preview: python -m intake refresh --dry-run")
    return 0


def cmd_status(args, cfg, store):
    _, queue, _ = report.build(store, cfg)
    print(json.dumps({"counts": queue["counts"], "pending": queue["pending"],
                      "failed": [e["document_id"] for e in queue["entries"] if e["status"] == "error"],
                      "stale": [e["document_id"] for e in queue["entries"] if e.get("stale")],
                      "policy_version": cfg.policy_version, "extraction_fp": cfg.extraction_fp(),
                      "settings": cfg.settings()}, indent=2))
    return 0


def cmd_refresh(args, cfg, store):
    """Bring results produced under old settings/policy up to date. Earlier results stay in history."""
    mode = "mock" if args.mock else "live"
    stale = pipeline.stale_documents(store, cfg, mode)
    if args.documents:
        wanted = {Path(d).name for d in args.documents}
        stale = [s for s in stale if s["document_id"] in wanted]
    if not stale:
        print("nothing to refresh: every current result matches the active settings and policy")
        return 0
    for s in stale:
        print(f"{s['document_id']:<16}{s['action']:<11}{'; '.join(s['changes'])}")
    n_model = sum(1 for s in stale if s["action"] == "re-extract")
    print(f"\n{len(stale)} stale result(s): {n_model} need a model call, {len(stale) - n_model} only re-route")
    if args.dry_run:
        print("dry run: nothing changed. Run without --dry-run to refresh.")
        return 0
    if n_model == 0:  # policy-only refresh: no model call, so no credentials needed
        from types import SimpleNamespace

        def _no_model(**_):
            raise RuntimeError("a source file changed during refresh and needs a model call; set OPENAI_API_KEY "
                               "and run: python -m intake intake <directory>")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_no_model)))
    else:
        client, mode = _client(cfg, args)
    res = pipeline.run([Path(s["source_path"]) for s in stale], store, client, cfg, mode)
    summary: dict[str, int] = {}
    for o in res.values():
        summary[o] = summary.get(o, 0) + 1
    print("refresh:", ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    print("exported:", report.export(store, cfg))
    return 1 if summary.get("error") else 0


def cmd_history(args, cfg, store):
    for h in store.history(args.document):
        print(json.dumps({k: h[k] for k in ("id", "document_id", "state", "source_sha256", "extraction_fp",
                                              "policy_version", "settings", "status", "owner", "next_action",
                                              "mode", "finished_at", "error", "reused_from")}))
    return 0


def cmd_export(args, cfg, store):
    print(report.export(store, cfg))
    print("evidence:", report.write_evidence(cfg))
    return 0


def cmd_check_export(args, cfg, store):
    """Read extractions.jsonl and queue.json the way a reviewer's tooling should, and prove they agree."""
    try:
        records, queue = report.read_consistent(cfg.out_dir)
    except FileNotFoundError as e:
        print(f"no export found in {cfg.out_dir}: {e.filename}. Run: python -m intake export")
        return 1
    except report.InconsistentSnapshot as e:
        print(f"MISMATCH: {e}")
        return 1
    by_doc = {r["document_id"]: r for r in records}
    bad = [e["document_id"] for e in queue["entries"]
           if e["document_id"] not in by_doc or by_doc[e["document_id"]]["attempt_id"] != e["attempt_id"]
           or by_doc[e["document_id"]]["source_sha256"] != e["source_sha256"]]
    if bad or len(by_doc) != len(queue["entries"]):
        print(f"MISMATCH: queue and records disagree for {bad or 'the document set'}")
        return 1
    print(f"OK snapshot {queue['snapshot_id']}: {len(records)} records, {len(queue['entries'])} queue entries, "
          f"{len(queue['pending'])} pending")
    return 0


def cmd_accuracy(args, cfg, store):
    expected = json.loads(Path(args.expected).read_text()) if args.expected else \
        json.loads((ROOT / "tests" / "expected.json").read_text())
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
    sub.add_parser("status", help="counts, pending, failures, stale results, active settings")
    p = sub.add_parser("refresh", help="re-process results made under older settings/policy")
    p.add_argument("documents", nargs="*")
    p.add_argument("--dry-run", action="store_true", help="list what would change, do nothing")
    p = sub.add_parser("history", help="all attempts, optionally for one document")
    p.add_argument("document", nargs="?")
    sub.add_parser("export", help="write deliverables/ from the store")
    sub.add_parser("check-export", help="verify extractions.jsonl and queue.json come from the same export")
    p = sub.add_parser("accuracy", help="compare current extractions with tests/expected.json (or --expected FILE)")
    p.add_argument("--expected", help="alternative expected-facts file, e.g. tests/policy_probe_expected.json")
    args = ap.parse_args(argv)
    cfg = _config(args.mock)
    store = Store(cfg.db_path)
    return {"intake": cmd_intake, "retry": cmd_retry, "queue": cmd_queue, "status": cmd_status,
            "refresh": cmd_refresh, "check-export": cmd_check_export,
            "history": cmd_history, "export": cmd_export, "accuracy": cmd_accuracy}[args.cmd](args, cfg, store)
