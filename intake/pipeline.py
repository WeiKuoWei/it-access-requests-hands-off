"""Per-document processing: reuse, claim, extract, route, record. Each document is independent."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .extract import ExtractionError, extract
from .logs import get_logger
from .routing import error_route, route
from .store import Store

log = get_logger()


def retry_command(document_id: str) -> str:
    return f"python -m intake retry {document_id}"


def _error(kind: str, message: str, document_id: str) -> dict:
    return {"kind": kind, "message": f"{message} Fix the cause, then run: {retry_command(document_id)}",
            "retryable": kind not in ("auth",)}


def process_file(path: Path, store: Store, client, cfg: Config, mode: str = "live",
                 force: bool = False, sleep=None) -> tuple[str, str]:
    """Returns (document_id, outcome). Outcomes: reused, rerouted, extracted, error, skipped-busy."""
    document_id = path.name
    fp, policy = cfg.extraction_fp(), cfg.policy_version
    try:
        raw = path.read_bytes()
    except OSError as e:
        return _record_failure(store, cfg, document_id, path, None, fp, mode, "unreadable", f"Cannot read file: {e}")
    sha = hashlib.sha256(raw).hexdigest()

    if not force:
        same = store.find_done(document_id, sha, fp, policy)
        if same:
            cur = {d["document_id"]: d for d in store.current()}[document_id]["current"]
            if cur["id"] == same["id"]:
                return document_id, "reused"  # nothing to do; current entry already this result
            store.add_derived(same, policy=policy, route=_stored_route(same), mode=same["mode"])
            return document_id, "reused"
        prior = store.find_done(document_id, sha, fp)  # same source+extraction settings, older policy
        if prior:
            try:
                msg = _message(raw)
            except ValueError as e:
                return _record_failure(store, cfg, document_id, path, sha, fp, mode, "bad-export", str(e))
            r = route(prior["extraction"], msg, document_id)
            store.add_derived(prior, policy=policy, route=r, mode=prior["mode"])
            return document_id, "rerouted"

    attempt = store.claim(document_id, str(path), sha, fp, policy, cfg.settings(), mode, cfg.lease_s)
    if attempt is None:
        return document_id, "skipped-busy"
    try:
        message = _message(raw)
        kwargs = {"sleep": sleep} if sleep else {}
        data, meta = extract(client, cfg, message, **kwargs)
        r = route(data, message, document_id)
        store.finish(attempt, state="done", extraction=data, route=r)
        return document_id, "extracted"
    except ExtractionError as e:
        store.finish(attempt, state="error", route=error_route(), error=_error(e.kind, e.message, document_id))
    except ValueError as e:
        store.finish(attempt, state="error", route=error_route(), error=_error("bad-export", str(e), document_id))
    except Exception as e:  # never let one document stop the batch
        log.exception("unexpected failure for %s", document_id)
        store.finish(attempt, state="error", route=error_route(),
                     error=_error("internal", f"Unexpected error: {type(e).__name__}", document_id))
    return document_id, "error"


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
    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        futures = [pool.submit(process_file, p, store, client, cfg, mode, force, sleep) for p in paths]
        for f in futures:
            doc, outcome = f.result()
            results[doc] = outcome
            log.info("%s: %s", doc, outcome)
    return results
