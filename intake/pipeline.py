"""Per-document processing: reuse, claim, extract, route, record. Each document is independent."""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import Config
from .extract import ExtractionError, extract
from .logs import get_logger
from .routing import error_route, route
from .store import Store
from .throttle import PROVIDER_KINDS, Breaker, Throttle

log = get_logger()


def retry_command(document_id: str) -> str:
    return f"python -m intake retry {document_id}"


def _error(kind: str, message: str, document_id: str) -> dict:
    return {"kind": kind, "message": f"{message} Fix the cause, then run: {retry_command(document_id)}",
            "retryable": kind not in ("auth",)}


def process_file(path: Path, store: Store, client, cfg: Config, mode: str = "live",
                 force: bool = False, sleep=None, throttle: Throttle | None = None,
                 breaker: Breaker | None = None) -> tuple[str, str]:
    """Returns (document_id, outcome). Outcomes: reused, rerouted, extracted, error, skipped-busy."""
    document_id = path.name
    fp, policy = cfg.extraction_fp(), cfg.policy_version
    try:
        raw = path.read_bytes()
    except OSError as e:
        return _record_failure(store, cfg, document_id, path, None, fp, mode, "unreadable", f"Cannot read file: {e}")
    sha = hashlib.sha256(raw).hexdigest()

    if not force:
        same = store.find_done(document_id, sha, fp, policy, mode)
        if same:
            if store.latest_finished_id(document_id) == same["id"]:
                return document_id, "reused"  # nothing to do; current entry already this result
            store.add_derived(same, policy=policy, route=_stored_route(same), mode=same["mode"])
            return document_id, "reused"
        prior = store.find_done(document_id, sha, fp, mode=mode)  # same source+extraction settings, older policy
        if prior:
            try:
                msg = _message(raw)
            except ValueError as e:
                return _record_failure(store, cfg, document_id, path, sha, fp, mode, "bad-export", str(e))
            r = route(prior["extraction"], msg, document_id)
            store.add_derived(prior, policy=policy, route=r, mode=prior["mode"])
            return document_id, "rerouted"

    if breaker and breaker.open:
        # provider is down: do not wait through retries for every remaining document; make them visible instead
        return _record_failure(store, cfg, document_id, path, sha, fp, mode, "provider-down-skipped",
                               "Skipped: the provider failed repeatedly earlier in this run, so no call was made.")

    attempt = store.claim(document_id, str(path), sha, fp, policy, cfg.settings(), mode, cfg.lease_s)
    if attempt is None:
        return document_id, "skipped-busy"
    try:
        message = _message(raw)
        kwargs = {"sleep": sleep} if sleep else {}
        data, meta = extract(client, cfg, message, throttle=throttle, **kwargs)
        r = route(data, message, document_id)
        store.finish(attempt, state="done", extraction=data, route=r)
        if breaker:
            breaker.success()
        return document_id, "extracted"
    except ExtractionError as e:
        if breaker and e.kind in PROVIDER_KINDS:
            breaker.failure()
        store.finish(attempt, state="error", route=error_route(), error=_error(e.kind, e.message, document_id))
    except ValueError as e:
        store.finish(attempt, state="error", route=error_route(), error=_error("bad-export", str(e), document_id))
    except Exception as e:  # never let one document stop the batch
        log.exception("unexpected failure for %s", document_id)
        store.finish(attempt, state="error", route=error_route(),
                     error=_error("internal", f"Unexpected error: {type(e).__name__}", document_id))
    return document_id, "error"


def staleness(attempt: dict | None, cfg: Config, mode: str | None = None) -> dict | None:
    """Why a finished result no longer matches the active configuration, or None if it is current.

    reasons: 'settings' (model/prompt/schema/limits changed -> needs a new model call),
             'policy'   (policy text or routing rules changed -> re-route stored extraction, no model call),
             'mode'     (produced by mock but a live run is requested, or the reverse -> new model call).
    """
    if not attempt or attempt["state"] != "done":
        return None
    reasons, changes = [], []
    if attempt["extraction_fp"] != cfg.extraction_fp():
        reasons.append("settings")
        old, new = attempt["settings"], cfg.settings()
        changes = [f"{k}: {old.get(k)} -> {new[k]}" for k in new if old.get(k) != new[k]]
    if mode and attempt["mode"] != mode:
        reasons.append("mode")
        changes.append(f"mode: {attempt['mode']} -> {mode}")
    if attempt["policy_version"] != cfg.policy_version:
        reasons.append("policy")
        changes.append(f"policy: {attempt['policy_version']} -> {cfg.policy_version}")
    if not reasons:
        return None
    action = "re-extract" if ("settings" in reasons or "mode" in reasons) else "re-route"
    return {"reasons": reasons, "action": action, "changes": changes}


def stale_documents(store: Store, cfg: Config, mode: str | None = None) -> list[dict]:
    """Documents whose current result was produced under different settings/policy than the active ones."""
    out = []
    for item in store.current():
        s = staleness(item["current"], cfg, mode)
        if s and not item["pending"]:
            out.append({"document_id": item["document_id"], "source_path": item["current"]["source_path"], **s})
    return out


def _message(raw: bytes) -> str:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Export is not valid UTF-8 JSON: {e}")
    if not isinstance(obj, dict) or not isinstance(obj.get("message"), str) or not obj["message"].strip():
        raise ValueError("Export has no non-empty 'message' string.")
    return obj["message"]


def _stored_route(a: dict) -> dict:
    return {"status": a["status"], "owner": a["owner"], "next_action": a["next_action"], "reasons": a["reasons"]}


def _record_failure(store, cfg, document_id, path, sha, fp, mode, kind, message):
    attempt = store.claim(document_id, str(path), sha, fp, cfg.policy_version, cfg.settings(), mode, cfg.lease_s)
    if attempt is None:
        return document_id, "skipped-busy"
    store.finish(attempt, state="error", route=error_route(), error=_error(kind, message, document_id))
    return document_id, "error"


def run(paths: list[Path], store: Store, client, cfg: Config, mode: str = "live", force: bool = False,
        sleep=None) -> dict[str, str]:
    """Process documents with a bounded worker pool; results are visible in the store as each finishes."""
    results: dict[str, str] = {}
    throttle = Throttle(cfg.min_interval_s)
    breaker = Breaker(cfg.breaker_threshold)
    pool = ThreadPoolExecutor(max_workers=max(1, cfg.workers))
    stop = threading.Event()

    def guarded(p: Path):
        if stop.is_set():  # an interrupt/fatal error happened: do not claim anything new
            return p.name, "cancelled"
        try:
            return process_file(p, store, client, cfg, mode, force, sleep, throttle, breaker)
        except BaseException:
            stop.set()  # set in the worker itself, before it can pick up another document
            raise

    try:
        futures = [pool.submit(guarded, p) for p in paths]
        for f in as_completed(futures):  # report in completion order: a slow document never hides finished ones
            doc, outcome = f.result()
            results[doc] = outcome
            log.info("%s: %s", doc, outcome)
    except BaseException:
        # Ctrl-C / fatal error: stop starting new documents. Finished work is already committed; documents
        # in flight stay 'pending' (visible, reclaimed after the lease or by `retry`); the rest were never claimed.
        pool.shutdown(wait=False, cancel_futures=True)
        released = store.abandon_own_pending()
        log.error("run interrupted; %d in-flight document(s) released. Re-run intake or `retry` to continue.",
                  released)
        raise
    pool.shutdown(wait=True)
    if breaker.open:
        log.error("provider circuit breaker opened; remaining documents were recorded as errors. "
                  "Run: python -m intake retry")
    return results
