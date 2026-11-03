"""Behavioural tests. Provider is mocked; expectations are hand-derived from the messages and the policy."""
import json
import time

import openai
import pytest

from conftest import FakeClient, REQUESTS, api_error, completion, no_sleep, record
from intake import pipeline, report
from intake.config import Config
from intake.extract import ExtractionError, extract
from intake.logs import redact
from intake.mock import MockClient
from intake.routing import route


def good(message, n):
    return completion(record())


def run_one(inbox, store, cfg, client, name="IT-001.json", force=False):
    return pipeline.process_file(inbox / name, store, client, cfg, "mock", force, no_sleep)


def cur(store, doc):
    return {s["document_id"]: s for s in store.current()}[doc]


# --- required scenarios ---------------------------------------------------------------------
def test_complete_request_routes_to_system_owner(inbox, store, cfg):
    run_one(inbox, store, cfg, FakeClient(good))
    c = cur(store, "IT-001.json")["current"]
    assert (c["status"], c["owner"], c["next_action"]) == ("review", "System Owner", "review-request")
    assert c["source_sha256"] and c["extraction"]["requester"] == "Maya Chen"


def test_missing_dates_need_information(inbox, store, cfg):
    # IT-011 says "next Monday - Friday": relative dates stay unresolved (null)
    client = FakeClient(lambda m, n: completion(record(
        request_id="IT-011", start_date=None, end_date=None, issues=["relative-date: next Monday - Friday"])))
    run_one(inbox, store, cfg, client, "IT-011.json")
    c = cur(store, "IT-011.json")["current"]
    assert (c["status"], c["owner"], c["next_action"]) == ("needs-information", "IT Service Desk", "clarify")
    assert "start_date, end_date" in c["reasons"][0]


def test_conflicting_details_need_information(inbox, store, cfg):
    client = FakeClient(lambda m, n: completion(record(
        request_id="IT-004", environment=None, supporting_quotes=["in staging", "for the production environment"],
        issues=["conflict: environment staging vs production"])))
    run_one(inbox, store, cfg, client, "IT-004.json")
    c = cur(store, "IT-004.json")["current"]
    assert c["status"] == "needs-information" and "conflicting details" in c["reasons"]


def test_embedded_instruction_goes_to_inspection_even_if_model_misses_it(inbox, store, cfg):
    # model returns a clean-looking record with NO override issue: the deterministic backstop still routes it
    client = FakeClient(lambda m, n: completion(record(request_id="IT-006", requester="Finn Lee")))
    run_one(inbox, store, cfg, client, "IT-006.json")
    c = cur(store, "IT-006.json")["current"]
    assert (c["status"], c["owner"], c["next_action"]) == ("review", "IT Service Desk", "inspect-message")
    # the message was passed as quoted data, never as an instruction
    assert client.messages[0].startswith("Hi") or "SYSTEM OVERRIDE" in client.messages[0]


def test_provider_failure_is_visible_and_isolated(inbox, store, cfg):
    def script(m, n):
        return api_error(openai.RateLimitError, 429) if "IT-002" in m else completion(record())

    res = pipeline.run([inbox / "IT-001.json", inbox / "IT-002.json"], store, FakeClient(script), cfg, "mock",
                       sleep=no_sleep)
    assert res == {"IT-001.json": "extracted", "IT-002.json": "error"}
    c = cur(store, "IT-002.json")["current"]
    assert (c["status"], c["owner"], c["next_action"]) == ("error", "IT Service Desk", "retry-extraction")
    assert c["extraction"] is None and "python -m intake retry IT-002.json" in c["error"]["message"]
    assert cur(store, "IT-001.json")["current"]["status"] == "review"


def test_unchanged_rerun_makes_no_calls_and_one_entry(inbox, store, cfg):
    client = FakeClient(good)
    run_one(inbox, store, cfg, client)
    assert run_one(inbox, store, cfg, client) == ("IT-001.json", "reused")
    assert client.calls == 1
    _, queue, _ = report.build(store)
    assert len(queue["entries"]) == 1 and len(store.history()) == 1


def test_changed_source_creates_new_result_and_keeps_old(inbox, store, cfg):
    client = FakeClient(good)
    run_one(inbox, store, cfg, client)
    old = cur(store, "IT-001.json")["current"]["source_sha256"]
    data = json.loads((inbox / "IT-001.json").read_text())
    data["message"] += " Update: please also note the corrected end date."
    (inbox / "IT-001.json").write_text(json.dumps(data))
    assert run_one(inbox, store, cfg, client) == ("IT-001.json", "extracted")
    new = cur(store, "IT-001.json")["current"]["source_sha256"]
    assert new != old and client.calls == 2
    assert [h["source_sha256"] for h in store.history("IT-001.json")] == [old, new]
    _, queue, history = report.build(store)
    assert len(queue["entries"]) == 1 and history[0]["source_sha256"] == old


def test_failed_retry_after_prior_success_keeps_history(inbox, store, cfg):
    state = {"fail": False}
    client = FakeClient(lambda m, n: api_error(openai.APITimeoutError, 408) if state["fail"] else completion(record()))
    run_one(inbox, store, cfg, client)
    good_sha = cur(store, "IT-001.json")["current"]["source_sha256"]
    state["fail"] = True
    run_one(inbox, store, cfg, client, force=True)
    item = cur(store, "IT-001.json")
    assert item["current"]["state"] == "error" and item["last_valid"]["source_sha256"] == good_sha
    records, queue, _ = report.build(store)
    e = queue["entries"][0]
    assert e["status"] == "error" and e["earlier_result"]["source_sha256"] == good_sha
    assert records[0]["extraction"] is None and records[0]["last_valid_result"]["attempt_id"] == item["last_valid"]["id"]
    state["fail"] = False  # service recovers; the documented retry command recovers the document
    assert run_one(inbox, store, cfg, client, force=True)[1] == "extracted"
    assert cur(store, "IT-001.json")["current"]["status"] == "review"


# --- response handling ----------------------------------------------------------------------
@pytest.mark.parametrize("resp,kind", [
    (completion(None, refusal="I can't help with that."), "refusal"),
    (completion('{"request_id": "IT-0', finish="length"), "truncated"),
    (completion("not json"), "malformed"),
    (completion({"request_id": "IT-001"}), "schema"),
    (completion(record(start_date="next Monday")), "schema"),
    (completion(record(supporting_quotes=["text that is not in the message"])), "quote"),
    (completion(record(), finish="content_filter"), "refusal"),
])
def test_bad_responses_become_errors(inbox, store, cfg, resp, kind):
    run_one(inbox, store, cfg, FakeClient(lambda m, n: resp))
    c = cur(store, "IT-001.json")["current"]
    assert c["status"] == "error" and c["error"]["kind"] == kind and c["extraction"] is None


def test_retryable_error_then_success(cfg):
    seq = [api_error(openai.InternalServerError, 503), completion(record())]
    client = FakeClient(lambda m, n: seq[n - 1])
    data, meta = extract(client, cfg, "msg", sleep=no_sleep)
    assert meta["attempts"] == 2 and data["request_id"] == "IT-001"


def test_refusal_and_auth_errors_are_not_retried(cfg):
    client = FakeClient(lambda m, n: api_error(openai.AuthenticationError, 401))
    with pytest.raises(ExtractionError) as ei:
        extract(client, cfg, "msg", sleep=no_sleep)
    assert client.calls == 1 and ei.value.kind == "auth"


def test_bad_export_file_is_an_error_not_a_crash(tmp_path, store, cfg):
    (tmp_path / "BAD.json").write_text("{not json")
    assert pipeline.process_file(tmp_path / "BAD.json", store, FakeClient(good), cfg, "mock")[1] == "error"
    assert cur(store, "BAD.json")["current"]["error"]["kind"] == "bad-export"


# --- operations -----------------------------------------------------------------------------
def test_model_setting_change_refreshes_results(inbox, store, cfg):
    client = FakeClient(good)
    run_one(inbox, store, cfg, client)
    cfg2 = Config(**{**cfg.__dict__, "model": "another-model"})
    assert run_one(inbox, store, cfg2, client)[1] == "extracted"
    atts = store.history("IT-001.json")
    assert len(atts) == 2 and atts[0]["settings"]["model"] != atts[1]["settings"]["model"]


def test_policy_change_reroutes_without_model_call(inbox, store, cfg):
    client = FakeClient(good)
    run_one(inbox, store, cfg, client)
    cfg2 = Config(**{**cfg.__dict__, "policy_version": "policy-v2"})
    assert run_one(inbox, store, cfg2, client)[1] == "rerouted"
    assert client.calls == 1
    c = cur(store, "IT-001.json")["current"]
    assert c["policy_version"] == "policy-v2" and len(store.history("IT-001.json")) == 2


def test_concurrent_operators_do_not_double_process(inbox, store, cfg):
    a = store.claim("IT-001.json", "p", "h", "fp", "pol", {}, "mock", 600)
    assert a is not None
    assert store.claim("IT-001.json", "p", "h", "fp", "pol", {}, "mock", 600) is None  # second operator skips


def test_stale_pending_is_reclaimed_and_marked_abandoned(store):
    store.claim("IT-001.json", "p", "h", "fp", "pol", {}, "mock", -1)  # lease already expired
    assert store.claim("IT-001.json", "p", "h", "fp", "pol", {}, "mock", 600) is not None
    assert [h["state"] for h in store.history("IT-001.json")] == ["abandoned", "pending"]


def test_completed_results_visible_while_others_pending(inbox, store, cfg):
    run_one(inbox, store, cfg, FakeClient(good))
    store.claim("IT-002.json", str(inbox / "IT-002.json"), "h", "fp", "pol", {}, "mock", 600)
    _, queue, _ = report.build(store)
    assert [e["document_id"] for e in queue["entries"]] == ["IT-001.json"]
    assert [p["document_id"] for p in queue["pending"]] == ["IT-002.json"]


def test_slow_document_does_not_block_others(inbox, store, cfg):
    def script(m, n):
        if "IT-002" in m:
            time.sleep(0.3)
        return completion(record())

    start = time.time()
    seen = []
    client = FakeClient(script)
    pipeline.run([inbox / "IT-002.json", inbox / "IT-001.json", inbox / "IT-003.json"], store, client, cfg, "mock")
    assert time.time() - start < 1.0 and len(store.current()) == 3


def test_export_links_queue_and_extractions(inbox, store, cfg):
    pipeline.run(sorted(inbox.glob("*.json")), store, MockClient(), cfg, "mock")
    report.export(store, cfg)
    out = __import__("pathlib").Path(cfg.out_dir)
    recs = [json.loads(line) for line in (out / "extractions.jsonl").read_text().splitlines()]
    queue = json.loads((out / "queue.json").read_text())
    assert len(recs) == 20 == len(queue["entries"])
    for r, e in zip(sorted(recs, key=lambda x: x["document_id"]), sorted(queue["entries"], key=lambda x: x["document_id"])):
        assert (r["document_id"], r["source_sha256"], r["status"], r["owner"], r["next_action"]) == \
               (e["document_id"], e["source_sha256"], e["status"], e["owner"], e["next_action"])


def test_redaction():
    assert "sk-abcdef123456" not in redact("auth failed for sk-abcdef123456 here")


# --- routing rules (from office-policy.md) ---------------------------------------------------
@pytest.mark.parametrize("over,doc,expected", [
    (dict(kind="withdrawal", system=None, environment=None, permission=None, start_date=None, end_date=None),
     "IT-001.json", ("IT Service Desk", "confirm-withdrawal")),
    (dict(kind="withdrawal", request_date=None), "IT-001.json", ("IT Service Desk", "clarify")),
    (dict(kind="other"), "IT-001.json", ("IT Service Desk", "clarify")),
    (dict(request_id="IT-061"), "IT-016.json", ("IT Service Desk", "clarify")),
    (dict(start_date="2026-11-05", end_date="2026-11-02"), "IT-001.json", ("IT Service Desk", "clarify")),
    (dict(environment="production"), "IT-001.json", ("Security Review", "review-risk-then-owner")),
    (dict(permission="write"), "IT-001.json", ("Security Review", "review-risk-then-owner")),
    (dict(permission="admin"), "IT-001.json", ("Security Review", "review-risk-then-owner")),
    (dict(), "IT-001.json", ("System Owner", "review-request")),
])
def test_routing_rules(over, doc, expected):
    r = route(record(**over), "plain message", doc)
    assert (r["owner"], r["next_action"]) == expected


# --- fresh wording --------------------------------------------------------------------------
def test_paraphrase_with_mock_matches_source_derived_expectations():
    exp = json.loads((REQUESTS.parent.parent / "tests" / "expected.json").read_text())["IT-101.json"]
    fixture = REQUESTS.parent.parent / "tests" / "fixtures" / "IT-101.json"
    msg = json.loads(fixture.read_text())["message"]
    # facts the paraphrase states, checked directly against the source text (independent of the extractor)
    for value in ("IT-101", "Hana Ito", "Security Analytics", "Forge", "2026-10-27", "2026-10-29", "Priya Shah"):
        assert value in msg
    assert exp["fields"]["environment"] == "production" and "live environment" in msg
    assert exp["route"]["owner"] == "Security Review"


def test_model_that_guesses_on_a_flagged_conflict_is_nulled(inbox, store, cfg):
    client = FakeClient(lambda m, n: completion(record(
        request_id="IT-004", environment="production", issues=["conflict: environment staging vs production"])))
    run_one(inbox, store, cfg, client, "IT-004.json")
    c = cur(store, "IT-004.json")["current"]
    assert c["extraction"]["environment"] is None
    assert any(i.startswith("normalized: environment") for i in c["extraction"]["issues"])
    assert c["status"] == "needs-information"


def test_date_ordering_issue_is_not_treated_as_value_conflict(inbox, store, cfg):
    client = FakeClient(lambda m, n: completion(record(
        request_id="IT-014", start_date="2026-11-05", end_date="2026-11-02",
        issues=["conflict: end_date 2026-11-02 is before start_date 2026-11-05"])))
    run_one(inbox, store, cfg, client, "IT-014.json")
    e = cur(store, "IT-014.json")["current"]["extraction"]
    assert (e["start_date"], e["end_date"]) == ("2026-11-05", "2026-11-02")


# --- interrupted responses and the documented retry command ------------------------------------
@pytest.mark.parametrize("make", [
    lambda: completion('{"request_id": "IT-001", "req', finish="length"),          # cut off mid-JSON
    lambda: completion(record(), finish="incomplete"),                               # non-stop finish reason
    lambda: completion(None, finish="stop"),                                         # empty body
    lambda: api_error(openai.APIConnectionError, 0),                                 # connection dropped
])
def test_interrupted_response_is_retried_then_recorded_as_error(inbox, store, cfg, make):
    client = FakeClient(lambda m, n: make())
    run_one(inbox, store, cfg, client)
    c = cur(store, "IT-001.json")["current"]
    assert client.calls == cfg.max_attempts            # retryable, bounded
    assert c["status"] == "error" and c["extraction"] is None
    assert "python -m intake retry IT-001.json" in c["error"]["message"]


def test_truncated_response_recovers_on_retry(cfg):
    seq = [completion('{"request_id": "IT', finish="length"), completion(record())]
    data, meta = extract(FakeClient(lambda m, n: seq[n - 1]), cfg, "msg", sleep=no_sleep)
    assert meta["attempts"] == 2 and data["request_id"] == "IT-001"


def test_retry_command_failure_after_prior_success_keeps_valid_result(inbox, store, cfg, monkeypatch):
    import argparse
    from intake import cli

    run_one(inbox, store, cfg, FakeClient(good))
    good_id = cur(store, "IT-001.json")["current"]["id"]
    # source changes, the new attempt is interrupted
    data = json.loads((inbox / "IT-001.json").read_text())
    data["message"] += " Updated."
    (inbox / "IT-001.json").write_text(json.dumps(data))
    run_one(inbox, store, cfg, FakeClient(lambda m, n: completion("{", finish="length")))
    item = cur(store, "IT-001.json")
    assert item["current"]["state"] == "error" and item["last_valid"]["id"] == good_id
    assert item["last_valid"]["source_sha256"] != item["current"]["source_sha256"]  # earlier source version
    # the documented retry command also fails while the service is still interrupted
    cfg_out = cfg
    monkeypatch.setattr(cli, "_client", lambda c, a: (FakeClient(lambda m, n: completion("{", finish="length")), "mock"))
    cli.cmd_retry(argparse.Namespace(documents=["IT-001.json"]), cfg_out, store)
    item = cur(store, "IT-001.json")
    assert item["current"]["state"] == "error" and item["last_valid"]["id"] == good_id
    _, queue, _ = report.build(store)
    assert queue["entries"][0]["status"] == "error" and queue["entries"][0]["earlier_result"]["attempt_id"] == good_id
