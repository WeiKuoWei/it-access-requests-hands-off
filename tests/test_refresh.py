"""Model-configuration and policy changes: detect stale results, refresh them, keep history (mocked provider)."""
import argparse
import json
from pathlib import Path

import openai
import pytest

from conftest import FakeClient, api_error, completion, no_sleep, record
from intake import cli, config, extract, pipeline, report
from intake.config import Config
from test_scale import bulk_cfg, make_inbox, ok


def client():
    return FakeClient(lambda m, n: ok(m))


def refresh(store, cfg, fake, monkeypatch, documents=(), dry_run=False, mode="mock"):
    monkeypatch.setattr(cli, "_client", lambda c, a: (fake, mode))
    return cli.cmd_refresh(argparse.Namespace(documents=list(documents), dry_run=dry_run, mock=(mode == "mock")),
                           cfg, store)


def cur(store):
    return {s["document_id"]: s["current"] for s in store.current()}


# --- what counts as a settings change ----------------------------------------------------------------
@pytest.mark.parametrize("over", [
    dict(model="other-model"), dict(temperature=0.7), dict(max_output_tokens=800),
])
def test_output_affecting_settings_change_the_fingerprint(cfg, over):
    assert bulk_cfg(cfg, **over).extraction_fp() != cfg.extraction_fp()


def test_prompt_and_schema_changes_change_the_fingerprint(cfg, monkeypatch):
    base = cfg.extraction_fp()
    monkeypatch.setattr(extract, "SYSTEM_PROMPT", extract.SYSTEM_PROMPT + " Be extra careful.")
    assert cfg.extraction_fp() != base
    monkeypatch.undo()
    schema = config.load_schema()
    schema["properties"]["team"] = {"type": ["string", "null"], "maxLength": 80}
    monkeypatch.setattr(config, "load_schema", lambda: schema)
    assert cfg.extraction_fp() != base


@pytest.mark.parametrize("over", [
    dict(workers=16), dict(timeout_s=5.0), dict(max_attempts=9), dict(backoff_base_s=7.0),
    dict(lease_s=5), dict(min_interval_s=1.0), dict(breaker_threshold=0), dict(db_path="/x/y.db"),
])
def test_operational_settings_do_not_invalidate_results(cfg, over):
    assert bulk_cfg(cfg, **over).extraction_fp() == cfg.extraction_fp()


def test_policy_version_follows_policy_text_and_routing_rules(tmp_path, monkeypatch):
    policy, rules = tmp_path / "policy.md", tmp_path / "routing.py"
    policy.write_text("rule set 1")
    rules.write_text("code 1")
    monkeypatch.setattr(config, "POLICY_PATH", policy)
    monkeypatch.setattr(config, "ROUTING_PATH", rules)
    v1 = config._policy_version()
    assert config._policy_version() == v1  # deterministic
    policy.write_text("rule set 2")
    v2 = config._policy_version()
    rules.write_text("code 2")
    v3 = config._policy_version()
    assert len({v1, v2, v3}) == 3  # editing either file is noticed with no manual version bump
    monkeypatch.setenv("INTAKE_POLICY_LABEL", "2026-11-rev2")
    assert config._policy_version().startswith("2026-11-rev2+")  # a label never hides a change


# --- model configuration change -----------------------------------------------------------------------
def test_model_change_is_detected_previewed_refreshed_and_history_kept(tmp_path, store, cfg, monkeypatch, capsys):
    files = make_inbox(tmp_path, 3)
    a, b = bulk_cfg(cfg, model="model-a"), bulk_cfg(cfg, model="model-b")
    ca, cb = client(), client()
    pipeline.run(files, store, ca, a, "mock")
    assert ca.calls == 3 and report.build(store, a)[1]["stale_results"] == 0

    queue = report.build(store, b)[1]  # the operator's view after the config change
    assert queue["stale_results"] == 3 and queue["active"]["settings"]["model"] == "model-b"
    assert all(e["stale"]["reasons"] == ["settings"] and e["stale"]["action"] == "re-extract" and
               "model: model-a -> model-b" in e["stale"]["changes"] for e in queue["entries"])

    assert refresh(store, b, cb, monkeypatch, dry_run=True) == 0  # preview changes nothing
    assert cb.calls == 0 and len(store.history()) == 3 and "dry run" in capsys.readouterr().out

    assert refresh(store, b, cb, monkeypatch) == 0
    assert cb.calls == 3
    now = cur(store)
    assert {c["settings"]["model"] for c in now.values()} == {"model-b"}
    assert report.build(store, b)[1]["stale_results"] == 0
    old = [h for h in store.history() if h["settings"]["model"] == "model-a"]  # earlier results retained, untouched
    assert len(old) == 3 and {h["source_sha256"] for h in old} == {c["source_sha256"] for c in now.values()}
    assert all(h["state"] == "done" and h["extraction"] for h in old)

    capsys.readouterr()
    assert refresh(store, b, cb, monkeypatch) == 0  # nothing left: idempotent, no further calls
    assert cb.calls == 3 and "nothing to refresh" in capsys.readouterr().out
    _, _, superseded = report.build(store, b)  # older versions live in history, not in the current outputs
    assert len(superseded) == 3 and all(h["settings"]["model"] == "model-a" for h in superseded)


def test_refresh_can_be_limited_to_named_documents(tmp_path, store, cfg, monkeypatch):
    files = make_inbox(tmp_path, 3)
    a, b = bulk_cfg(cfg, model="model-a"), bulk_cfg(cfg, model="model-b")
    pipeline.run(files, store, client(), a, "mock")
    cb = client()
    refresh(store, b, cb, monkeypatch, documents=["IT-00001.json"])
    assert cb.calls == 1 and report.build(store, b)[1]["stale_results"] == 2


def test_intake_rerun_after_config_change_also_refreshes(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 3)
    pipeline.run(files, store, client(), bulk_cfg(cfg, model="model-a"), "mock")
    cb = client()
    res = pipeline.run(files, store, cb, bulk_cfg(cfg, model="model-b"), "mock")
    assert set(res.values()) == {"extracted"} and cb.calls == 3


def test_reverting_the_configuration_reuses_the_earlier_result(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 2)
    a, b = bulk_cfg(cfg, model="model-a"), bulk_cfg(cfg, model="model-b")
    pipeline.run(files, store, client(), a, "mock")
    pipeline.run(files, store, client(), b, "mock")
    ca = client()
    res = pipeline.run(files, store, ca, a, "mock")  # team rolls the model back
    assert set(res.values()) == {"reused"} and ca.calls == 0
    now = cur(store)
    assert all(c["settings"]["model"] == "model-a" and c["reused_from"] for c in now.values())
    assert len(store.history("IT-00000.json")) == 3  # A, B, and the A result made current again


def test_changed_source_and_changed_settings_together(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 1)
    a, b = bulk_cfg(cfg, model="model-a"), bulk_cfg(cfg, model="model-b")
    pipeline.run(files, store, client(), a, "mock")
    old_sha = cur(store)["IT-00000.json"]["source_sha256"]
    files[0].write_text(json.dumps({"export_version": "1.0", "message": "request number 0 (corrected)"}))
    cb = FakeClient(lambda m, n: ok("request number 0"))
    pipeline.run(files, store, cb, b, "mock")
    new = cur(store)["IT-00000.json"]
    assert new["source_sha256"] != old_sha and new["settings"]["model"] == "model-b"
    first = store.history("IT-00000.json")[0]
    assert first["source_sha256"] == old_sha and first["settings"]["model"] == "model-a"


# --- policy change ------------------------------------------------------------------------------------
def test_policy_change_reroutes_stored_extractions_without_model_or_credentials(tmp_path, store, cfg, monkeypatch,
                                                                                capsys):
    files = make_inbox(tmp_path, 3)
    v1 = bulk_cfg(cfg, policy_version="policy-v1")
    v2 = bulk_cfg(cfg, policy_version="policy-v2")
    pipeline.run(files, store, client(), v1, "mock")
    assert {c["owner"] for c in cur(store).values()} == {"System Owner"}

    original_route = pipeline.route

    def stricter(extraction, message, doc):  # the revised policy sends everything to Security Review
        r = original_route(extraction, message, doc)
        return {**r, "owner": "Security Review", "next_action": "review-risk-then-owner"}

    monkeypatch.setattr(pipeline, "route", stricter)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def forbidden(c, a):
        raise AssertionError("a policy-only refresh must not build a model client")

    monkeypatch.setattr(cli, "_client", forbidden)
    queue = report.build(store, v2)[1]
    assert all(e["stale"]["reasons"] == ["policy"] and e["stale"]["action"] == "re-route" for e in queue["entries"])
    assert cli.cmd_refresh(argparse.Namespace(documents=[], dry_run=False, mock=True), v2, store) == 0
    assert "0 need a model call" in capsys.readouterr().out

    now = cur(store)
    assert {c["owner"] for c in now.values()} == {"Security Review"}
    assert all(c["policy_version"] == "policy-v2" and c["reused_from"] for c in now.values())
    for doc, c in now.items():  # same extraction, same settings; only the routing decision moved
        first = store.history(doc)[0]
        assert first["owner"] == "System Owner" and first["policy_version"] == "policy-v1"
        assert first["extraction"] == c["extraction"] and first["settings"] == c["settings"]
    report.export(store, v2)
    rec = json.loads((Path(v2.out_dir) / "extractions.jsonl").read_text().splitlines()[0])
    assert rec["policy_version"] == "policy-v2" and rec["settings"]["model"] == v2.model
    q = json.loads((Path(v2.out_dir) / "queue.json").read_text())
    assert q["active"]["policy_version"] == "policy-v2" and q["stale_results"] == 0
    assert all(e["policy_version"] == "policy-v2" for e in q["entries"])


def test_policy_and_model_change_together_needs_one_new_call_per_document(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 2)
    pipeline.run(files, store, client(), bulk_cfg(cfg, model="model-a", policy_version="p1"), "mock")
    new = bulk_cfg(cfg, model="model-b", policy_version="p2")
    stale = pipeline.stale_documents(store, new)
    assert all(s["reasons"] == ["settings", "policy"] and s["action"] == "re-extract" for s in stale)
    cb = client()
    pipeline.run(files, store, cb, new, "mock")
    assert cb.calls == 2 and {c["policy_version"] for c in cur(store).values()} == {"p2"}


# --- refresh failures and interruption ---------------------------------------------------------------
def test_failed_refresh_shows_the_error_and_keeps_the_earlier_result(tmp_path, store, cfg, monkeypatch):
    files = make_inbox(tmp_path, 2)
    a, b = bulk_cfg(cfg, model="model-a"), bulk_cfg(cfg, model="model-b")
    pipeline.run(files, store, client(), a, "mock")
    down = FakeClient(lambda m, n: api_error(openai.InternalServerError, 503))
    pipeline.run(files, store, down, b, "mock", sleep=no_sleep)
    queue = report.build(store, b)[1]
    for e in queue["entries"]:
        assert e["status"] == "error" and "stale" not in e  # failed, not merely stale: shown as a failure
        assert e["earlier_result"]["settings"]["model"] == "model-a"  # the older result is labelled as such
        assert e["retry_command"].startswith("python -m intake retry")
    assert pipeline.stale_documents(store, b) == []
    # service recovers: the documented retry command completes the refresh
    good = client()
    monkeypatch.setattr(cli, "_client", lambda c, a_: (good, "mock"))
    cli.cmd_retry(argparse.Namespace(documents=[], mock=True), b, store)
    assert {c["settings"]["model"] for c in cur(store).values()} == {"model-b"} and good.calls == 2
    assert {h["state"] for h in store.history("IT-00000.json")} == {"done", "error"}


def test_interrupted_refresh_resumes_with_only_the_remaining_documents(tmp_path, store, cfg, monkeypatch):
    files = make_inbox(tmp_path, 6)
    a = bulk_cfg(cfg, model="model-a", workers=1)
    b = bulk_cfg(cfg, model="model-b", workers=1)
    pipeline.run(files, store, client(), a, "mock")

    def script(message, n):
        if n == 3:
            raise KeyboardInterrupt
        return ok(message)

    first = FakeClient(script)
    with pytest.raises(KeyboardInterrupt):
        refresh(store, b, first, monkeypatch)
    assert first.calls == 3 and report.build(store, b)[1]["stale_results"] == 4  # 2 done, 4 still stale

    second = client()
    assert refresh(store, b, second, monkeypatch) == 0
    assert second.calls == 4  # exactly the unfinished ones; finished refreshes are not repeated
    assert report.build(store, b)[1]["stale_results"] == 0
    for doc in cur(store):
        done = [h for h in store.history(doc) if h["state"] == "done"]
        assert [h["settings"]["model"] for h in done] == ["model-a", "model-b"]  # one result per configuration


# --- mock results never masquerade as provider results --------------------------------------------------
def test_mock_results_are_not_reused_for_live_runs(tmp_path, store, cfg, monkeypatch):
    files = make_inbox(tmp_path, 2)
    pipeline.run(files, store, client(), cfg, "mock")
    live = client()
    res = pipeline.run(files, store, live, cfg, "live")  # same settings, different provenance
    assert set(res.values()) == {"extracted"} and live.calls == 2
    assert [h["mode"] for h in store.history("IT-00000.json")] == ["mock", "live"]
    assert report.build(store, cfg)[1]["entries"][0]["mode"] == "live"


def test_refresh_in_live_mode_flags_mock_results(tmp_path, store, cfg, monkeypatch, capsys):
    pipeline.run(make_inbox(tmp_path, 2), store, client(), cfg, "mock")
    stale = pipeline.stale_documents(store, cfg, "live")
    assert [s["reasons"] for s in stale] == [["mode"], ["mode"]] and stale[0]["action"] == "re-extract"
    assert refresh(store, cfg, client(), monkeypatch, dry_run=True, mode="live") == 0
    assert "mode: mock -> live" in capsys.readouterr().out
    assert pipeline.stale_documents(store, cfg, "mock") == []  # viewing a mock database as mock flags nothing
    assert report.build(store, cfg)[1]["stale_results"] == 0  # queue view does not judge provenance
