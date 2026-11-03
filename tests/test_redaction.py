"""The API key must never reach a log, a stored error, an exported file or request-evidence.json.

Every test uses a made-up key. The end-to-end test (test_parallel.py) sends it over HTTP to a provider that echoes the
Authorization header back inside its error response, which is the realistic way a key could come back at us.
"""
import io
import json
import logging
import re
import sys
from pathlib import Path

import openai
import pytest

from conftest import FakeClient, REQUESTS, api_error, no_sleep
from intake import cli, pipeline, report
from intake.config import ROOT, Config
from intake.logs import get_logger, redact

# Made-up keys, assembled from pieces so no secret-shaped literal sits in the source (scanners would flag it, and the
# repository-wide scan below would, rightly, fail).
KEY = "sk-" + "proj-AbC123_def-GHI456jklMNO789pqrSTU"
GATEWAY_KEY = "gw_live_" + "9f8e7d6c5b4a3210"  # not sk- shaped: only exact-value redaction can catch it


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    return KEY


@pytest.fixture
def logged(key):
    """Capture what the real intake logger would print (same handler, formatter and filters)."""
    log = get_logger()
    stream = io.StringIO()
    handler = log.handlers[0]
    old = handler.setStream(stream)
    yield stream
    handler.setStream(old)


# ------------------------------------------------------------------------------------------------ redact()
@pytest.mark.parametrize("text,secret", [
    (f"key is {KEY} ok", KEY),
    (f"key is {KEY[:-6]}... ok", KEY[:-6]),                                       # a truncated echo of the key
    ("Incorrect API key provided: sk-proj-********************abcd. Find it at ...", "abcd"),   # provider's masked echo
    ("Authorization: Bearer abc.DEF-123_xyz~+/=", "abc.DEF-123_xyz"),
    ("headers={'Authorization': 'Bearer abcdef123456'}", "abcdef123456"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("GET https://user:s3cretpass@gateway.example.com/v1/chat", "s3cretpass"),
    ("https://gateway.example.com/v1?api_key=abcd1234efgh&x=1", "abcd1234efgh"),
    ('request failed {"api-key": "abcd1234efgh"}', "abcd1234efgh"),
    ("client_secret=abcd1234efgh", "abcd1234efgh"),
])
def test_secret_shapes_are_removed(text, secret, key):
    out = redact(text)
    assert secret not in out, out
    assert "[REDACTED]" in out


def test_the_exact_configured_key_is_removed_even_when_it_has_no_recognisable_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", GATEWAY_KEY)
    assert GATEWAY_KEY not in redact(f"upstream said: invalid credential {GATEWAY_KEY} for tenant")


def test_redaction_leaves_ordinary_text_alone_and_is_idempotent(key):
    ordinary = ("Extraction attempt 2/4 failed (rate-limit) for IT-007.json; max_output_tokens=1500; "
                "Unexpected token in model output; model gpt-4.1-mini; OPENAI_API_KEY is not set")
    assert redact(ordinary) == ordinary
    once = redact(f"x {KEY} y Authorization: Bearer zzzzzzzz1111")
    assert redact(once) == once


def test_a_very_short_configured_value_is_not_replaced_everywhere(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "abc")  # would mangle every 'abc' in every message; shape patterns still apply
    assert redact("abcdef") == "abcdef"


# ------------------------------------------------------------------------------------------------ logs
def test_log_messages_and_arguments_are_redacted(logged):
    log = get_logger()
    log.info("plain %s with key %s", "text", KEY)
    log.warning(f"fstring {KEY}")
    assert KEY not in logged.getvalue() and "[REDACTED]" in logged.getvalue()


def test_tracebacks_are_redacted(logged):
    """log.exception() prints the exception message and chained causes through the formatter, not through msg."""
    log = get_logger()
    try:
        try:
            raise ConnectionError(f"proxy rejected Authorization: Bearer {KEY}")
        except ConnectionError as inner:
            raise RuntimeError(f"upstream call failed for key {KEY}") from inner
    except RuntimeError:
        log.exception("unexpected failure for IT-007.json")
    text = logged.getvalue()
    assert "Traceback" in text and "RuntimeError" in text  # the traceback is still useful...
    assert KEY not in text and "Bearer sk-" not in text    # ...but carries no secret, including the chained cause


def test_the_pipeline_logs_nothing_secret_when_an_unexpected_error_occurs(tmp_path, logged, store, cfg):
    def boom(m, n):
        raise RuntimeError(f"connection error to https://u:{KEY}@gw.example.com with {KEY}")

    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "IT-001.json").write_text(json.dumps({"export_version": "1.0", "message": "x"}))
    pipeline.process_file(inbox / "IT-001.json", store, FakeClient(boom), cfg, "live", sleep=no_sleep)
    assert KEY not in logged.getvalue()


# ------------------------------------------------------------------------------------------------ stored errors
@pytest.mark.parametrize("make", [
    lambda: api_error(openai.BadRequestError, 400),                                   # message echoes the key below
    lambda: RuntimeError(f"SDK blew up: {KEY}"),                                       # a non-SDK exception
])
def test_stored_errors_and_every_export_are_free_of_the_key(tmp_path, key, store, cfg, make):
    def script(m, n):
        e = make()
        if isinstance(e, openai.APIStatusError):
            e.message = f"Invalid request. Authorization header was Bearer {KEY}"
            e.args = (e.message,)
        return e

    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "IT-001.json").write_text(json.dumps({"export_version": "1.0", "message": "x"}))
    pipeline.process_file(inbox / "IT-001.json", store, FakeClient(script), cfg, "live", sleep=no_sleep)
    report.export(store, cfg)

    err = {s["document_id"]: s["current"] for s in store.current()}["IT-001.json"]["error"]
    assert err["kind"] in ("provider-error", "internal") and KEY not in json.dumps(err)
    for f in Path(cfg.out_dir).iterdir():
        assert KEY.encode() not in f.read_bytes(), f.name
    assert KEY.encode() not in Path(cfg.db_path).read_bytes()


def test_every_stored_error_passes_through_redaction_whatever_its_origin(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # not configured: the shape patterns alone must catch it
    assert KEY not in json.dumps(pipeline._error("bad-export", f"cannot parse {KEY}", "IT-001.json"))
    monkeypatch.setenv("OPENAI_API_KEY", GATEWAY_KEY)    # configured, unrecognisable shape: the exact value catches it
    assert GATEWAY_KEY not in json.dumps(pipeline._error("provider-error", f"rejected {GATEWAY_KEY}", "IT-001.json"))


# ------------------------------------------------------------------------------------------------ uncaught errors
def test_an_uncaught_exception_at_the_cli_prints_a_redacted_traceback(key, monkeypatch, capsys):
    def explode(argv=None):
        raise RuntimeError(f"unexpected failure with {KEY}")

    monkeypatch.setattr(cli, "main", explode)
    assert cli.run([]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and KEY not in err


def test_argument_errors_and_exit_codes_are_not_swallowed_by_the_safety_wrapper(monkeypatch):
    with pytest.raises(SystemExit) as e:
        cli.run(["no-such-command"])
    assert e.value.code == 2


# ------------------------------------------------------------------------------------------------ request-evidence.json
def test_request_evidence_never_contains_the_key_and_shows_a_redacted_header(tmp_path, key, cfg):
    evidence = report.write_evidence(cfg)
    text = evidence.read_text()
    assert KEY not in text
    assert json.loads(text)["headers"]["Authorization"].startswith("Bearer [REDACTED")
    assert redact(text) == text  # nothing in it even looks like a secret (so the guard below cannot false-alarm)


def test_request_evidence_is_refused_if_a_credential_ever_ends_up_in_it(tmp_path, key, cfg, monkeypatch):
    from intake import extract

    real = extract.build_request

    def leaky(c, message):
        req = real(c, message)
        req["messages"][0]["content"] += f"\n(debug: api key {KEY})"
        return req

    monkeypatch.setattr(extract, "build_request", leaky)
    with pytest.raises(RuntimeError, match="credential"):
        report.write_evidence(cfg)
    assert not (Path(cfg.out_dir) / "request-evidence.json").exists()  # nothing half-written


def test_the_evidence_embeds_the_real_prompt_not_a_copy(cfg):
    from intake.extract import SYSTEM_PROMPT

    body = json.loads(report.write_evidence(cfg).read_text())["body"]
    assert body["messages"][0]["content"] == SYSTEM_PROMPT and "api_key" not in json.dumps(body).lower()


# ------------------------------------------------------------------------------------------------ the repository itself
SECRET_SHAPE = re.compile(r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}")


def _tracked_text_files():
    skip = {".git", ".venv", "data", "__pycache__", ".pytest_cache"}
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and p.suffix in (
                ".md", ".json", ".jsonl", ".py", ".txt", ".lock", ".toml", ".cfg", ".yml", ".yaml", ""):
            yield p


def test_no_secret_shaped_string_is_committed_anywhere_in_the_repository():
    hits = [str(p.relative_to(ROOT)) for p in _tracked_text_files() if SECRET_SHAPE.search(p.read_text("utf-8", "ignore"))]
    assert hits == []  # including the tests: fake keys are assembled from pieces, see KEY above


def test_the_operators_real_key_if_one_is_set_appears_in_no_committed_file_or_deliverable():
    real = __import__("os").environ.get("OPENAI_API_KEY")
    if not real or len(real) < 12 or real == KEY:
        pytest.skip("no real OPENAI_API_KEY in this environment")
    hits = [str(p.relative_to(ROOT)) for p in _tracked_text_files() if real in p.read_text("utf-8", "ignore")]
    assert hits == [], "the real API key was found in a repository file"
