"""Operator-facing CLI behaviour a new operator relies on (README commands).

Every test runs in a sandbox: INTAKE_* variables are cleared and BOTH modules' ROOT are redirected to a temp directory,
so even if the code under test regressed (or is mutated) its default paths resolve inside the sandbox and can never
reach the real store or the committed deliverables. (A mutation check once overwrote them before this was added.)
"""
import argparse
import os
import shutil
from pathlib import Path

import openai
import pytest

from conftest import REQUESTS, FakeClient, api_error, no_sleep
from intake import cli, config
from intake import mock  # noqa: F401  (import now: it is otherwise imported lazily, after the sandbox patches ROOT)
from intake.config import Config
from test_scale import make_inbox, ok

REAL = Config()  # the real default paths, captured at import time before any sandboxing


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("INTAKE_")]:
        monkeypatch.delenv(k)
    fake_root = tmp_path / "repo"
    monkeypatch.setattr(cli, "ROOT", fake_root)
    monkeypatch.setattr(config, "ROOT", fake_root)  # Config() defaults now resolve inside the sandbox as well
    return fake_root


@pytest.fixture
def samples(tmp_path):
    d = tmp_path / "samples"
    d.mkdir()
    for n in ("IT-001.json", "IT-002.json", "IT-003.json"):
        shutil.copy(REQUESTS / n, d / n)
    return d


def snapshot(paths):
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in paths}


def test_mock_mode_uses_its_own_area_by_default_not_the_real_store_or_deliverables(sandbox, samples, capsys):
    """The README's offline run must not overwrite the committed deliverables (it used to)."""
    guarded = [Path(REAL.db_path), Path(REAL.out_dir) / "extractions.jsonl", Path(REAL.out_dir) / "queue.json",
               Path(REAL.out_dir) / "history.jsonl"]
    before = snapshot(guarded)

    assert cli.main(["--mock", "intake", str(samples)]) == 0

    assert snapshot(guarded) == before  # real data and committed deliverables: untouched
    assert (sandbox / "data" / "mock" / "intake.db").exists()  # ...mock work went to its own area
    assert (sandbox / "data" / "mock" / "out" / "queue.json").exists()
    assert not (sandbox / "data" / "intake.db").exists() and not (sandbox / "deliverables").exists()
    assert "real store and deliverables untouched" in capsys.readouterr().err


def test_mock_results_are_read_back_with_mock_on_every_command(sandbox, samples, capsys):
    cli.main(["--mock", "intake", str(samples)])
    capsys.readouterr()
    assert cli.main(["--mock", "queue"]) == 0
    out = capsys.readouterr().out
    assert "IT-001.json" in out and "3 documents" in out
    assert cli.main(["--mock", "check-export"]) == 0 and "OK snapshot" in capsys.readouterr().out
    # without --mock the same command looks at the (separate, still empty) real store
    assert cli.main(["queue"]) == 0 and "0 documents" in capsys.readouterr().out


def test_explicit_paths_are_honoured_in_mock_mode(tmp_path, sandbox, samples, monkeypatch, capsys):
    monkeypatch.setenv("INTAKE_DB", str(tmp_path / "mine.db"))
    monkeypatch.setenv("INTAKE_OUT_DIR", str(tmp_path / "mine-out"))
    cli.main(["--mock", "intake", str(samples)])
    assert (tmp_path / "mine.db").exists() and (tmp_path / "mine-out" / "queue.json").exists()
    assert not sandbox.exists() and "real store" not in capsys.readouterr().err


def test_live_mode_configuration_is_unchanged(sandbox):
    assert cli._config(False) == Config()


def test_a_fresh_install_has_an_empty_queue_and_commands_still_work(sandbox, capsys):
    for cmd in (["queue"], ["status"], ["history"], ["retry"], ["refresh", "--dry-run"]):
        assert cli.main(cmd) == 0, cmd
    assert "0 documents" in capsys.readouterr().out


def test_retry_exits_non_zero_while_anything_still_fails(tmp_path, sandbox, monkeypatch):
    from intake import pipeline
    from intake.store import Store

    cfg = Config(db_path=str(tmp_path / "r.db"), out_dir=str(tmp_path / "out"), backoff_base_s=0, max_attempts=1)
    store = Store(cfg.db_path)
    files = make_inbox(tmp_path, 2)
    down = lambda m, n: api_error(openai.InternalServerError, 503)  # noqa: E731
    pipeline.run(files, store, FakeClient(down), cfg, "mock", sleep=no_sleep)
    args = argparse.Namespace(documents=[], mock=True)

    monkeypatch.setattr(cli, "_client", lambda c, a: (FakeClient(down), "mock"))
    assert cli.cmd_retry(args, cfg, store) == 1  # still failing: a script must be able to tell

    monkeypatch.setattr(cli, "_client", lambda c, a: (FakeClient(lambda m, n: ok(m)), "mock"))
    assert cli.cmd_retry(args, cfg, store) == 0  # recovered
