"""extractions.jsonl and queue.json must be readable as one consistent pair (in-process checks).

The cross-process behaviour (a writer publishing continuously while readers poll) is in test_parallel.py.
"""
import argparse
import json
import threading
import time
from pathlib import Path

import pytest

from conftest import FakeClient
from intake import cli, pipeline, report
from test_scale import bulk_cfg, make_inbox, ok


def make_export(tmp_path, store, cfg, n=3):
    files = make_inbox(tmp_path, n)
    pipeline.run(files, store, FakeClient(lambda m, k: ok(m)), cfg, "mock")
    return files, report.export(store, cfg)


def read(cfg):
    out = Path(cfg.out_dir)
    recs = [json.loads(x) for x in (out / "extractions.jsonl").read_text().splitlines()]
    return recs, json.loads((out / "queue.json").read_text())


def test_both_files_carry_the_same_snapshot_id(tmp_path, store, cfg):
    _, summary = make_export(tmp_path, store, cfg)
    recs, queue = read(cfg)
    assert {r["snapshot_id"] for r in recs} == {queue["snapshot_id"]} == {summary["snapshot_id"]}
    assert len(queue["snapshot_id"]) == 12


def test_snapshot_id_is_content_derived(tmp_path, store, cfg):
    files, first = make_export(tmp_path, store, cfg)
    assert report.export(store, cfg)["snapshot_id"] == first["snapshot_id"]  # same state, same id (stable diffs)
    (tmp_path / "bulk" / "IT-00003.json").write_text(json.dumps({"export_version": "1.0", "message": "request number 3"}))
    pipeline.run(sorted((tmp_path / "bulk").glob("*.json")), store, FakeClient(lambda m, k: ok(m)), cfg, "mock")
    added = report.export(store, cfg)["snapshot_id"]
    assert added != first["snapshot_id"]  # new document -> new export identity
    other = bulk_cfg(cfg, model="another-model")  # same data, but the queue now flags results stale
    assert report.export(store, other)["snapshot_id"] != added


def test_read_consistent_returns_a_matching_pair(tmp_path, store, cfg):
    _, summary = make_export(tmp_path, store, cfg)
    recs, queue = report.read_consistent(cfg.out_dir)
    assert queue["snapshot_id"] == summary["snapshot_id"] and len(recs) == len(queue["entries"]) == 3


def test_an_empty_export_is_consistent_too(store, cfg):
    report.export(store, cfg)
    recs, queue = report.read_consistent(cfg.out_dir)
    assert recs == [] and queue["entries"] == [] and queue["snapshot_id"]


def test_a_mixed_pair_is_refused_not_returned(tmp_path, store, cfg):
    files, _ = make_export(tmp_path, store, cfg)
    out = Path(cfg.out_dir)
    older_queue = (out / "queue.json").read_text()
    (tmp_path / "bulk" / "IT-00003.json").write_text(json.dumps({"export_version": "1.0", "message": "request number 3"}))
    pipeline.run(sorted((tmp_path / "bulk").glob("*.json")), store, FakeClient(lambda m, k: ok(m)), cfg, "mock")
    report.export(store, cfg)
    (out / "queue.json").write_text(older_queue)  # exactly what a reader straddling a publish would hold
    with pytest.raises(report.InconsistentSnapshot):
        report.read_consistent(out, attempts=2, pause_s=0)


def test_a_single_foreign_record_is_detected(tmp_path, store, cfg):
    make_export(tmp_path, store, cfg)
    out = Path(cfg.out_dir)
    lines = (out / "extractions.jsonl").read_text().splitlines()
    rec = json.loads(lines[1])
    rec["snapshot_id"] = "000000000000"
    lines[1] = json.dumps(rec)
    (out / "extractions.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(report.InconsistentSnapshot):
        report.read_consistent(out, attempts=2, pause_s=0)


def test_files_without_a_snapshot_id_are_not_trusted(tmp_path, store, cfg):
    make_export(tmp_path, store, cfg)
    out = Path(cfg.out_dir)
    q = json.loads((out / "queue.json").read_text())
    del q["snapshot_id"]
    (out / "queue.json").write_text(json.dumps(q))
    with pytest.raises(report.InconsistentSnapshot):
        report.read_consistent(out, attempts=1)


def test_a_reader_waits_for_an_export_in_progress_and_gets_the_new_pair(tmp_path, store, cfg, monkeypatch):
    make_export(tmp_path, store, cfg)
    old_id = read(cfg)[1]["snapshot_id"]
    (tmp_path / "bulk" / "IT-00003.json").write_text(json.dumps({"export_version": "1.0", "message": "request number 3"}))
    pipeline.run(sorted((tmp_path / "bulk").glob("*.json")), store, FakeClient(lambda m, k: ok(m)), cfg, "mock")

    real_write = report.atomic_write

    def slow_write(path, text):  # each file takes 0.25 s to publish, all inside the exclusive lock
        time.sleep(0.25)
        real_write(path, text)

    monkeypatch.setattr(report, "atomic_write", slow_write)
    writer = threading.Thread(target=report.export, args=(store, cfg))
    writer.start()
    time.sleep(0.1)  # the writer now holds the lock and is mid-publish
    started = time.time()
    recs, queue = report.read_consistent(cfg.out_dir, attempts=1)  # no retry layer: only the lock can save us
    waited = time.time() - started
    writer.join()
    assert queue["snapshot_id"] != old_id and len(recs) == 4  # it got the *new* complete pair, not a mix
    assert waited > 0.4  # and it had to wait for the export to finish


def test_queue_json_is_written_last(tmp_path, store, cfg, monkeypatch):
    files = make_inbox(tmp_path, 2)
    pipeline.run(files, store, FakeClient(lambda m, k: ok(m)), cfg, "mock")
    order, real = [], report.atomic_write
    monkeypatch.setattr(report, "atomic_write", lambda p, t: (order.append(Path(p).name), real(p, t)))
    report.export(store, cfg)
    assert order == ["history.jsonl", "extractions.jsonl", "queue.json"]  # queue.json completes an export


def run_check(cfg, store, capsys):
    rc = cli.cmd_check_export(argparse.Namespace(), cfg, store)
    return rc, capsys.readouterr().out


def test_check_export_command(tmp_path, store, cfg, capsys):
    assert run_check(cfg, store, capsys)[0] == 1 and "no export found" in run_check(cfg, store, capsys)[1]
    make_export(tmp_path, store, cfg)
    rc, out = run_check(cfg, store, capsys)
    assert rc == 0 and out.startswith("OK snapshot") and "3 records" in out

    # forged: ids agree but a queue entry points at a different attempt than the record it claims to describe
    out_dir = Path(cfg.out_dir)
    q = json.loads((out_dir / "queue.json").read_text())
    q["entries"][0]["attempt_id"] += 100
    (out_dir / "queue.json").write_text(json.dumps(q))
    rc, text = run_check(cfg, store, capsys)
    assert rc == 1 and "MISMATCH" in text

    # a stale queue.json (older export) next to newer records
    report.export(store, cfg)  # re-export restores consistency
    assert run_check(cfg, store, capsys)[0] == 0
    stale = json.loads((out_dir / "queue.json").read_text())
    stale["snapshot_id"] = "feedfacecafe"
    (out_dir / "queue.json").write_text(json.dumps(stale))
    rc, text = run_check(cfg, store, capsys)
    assert rc == 1 and "MISMATCH" in text
