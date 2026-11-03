"""Operators as real, simultaneous OS processes sharing one database and one output directory.

Each operator is `python -m intake ...` (via tests/launch.py, which only adds a start barrier), talking through the
real `openai` client to a local fake provider over HTTP. The in-process claim tests elsewhere check the logic; these
check that it holds when separate processes genuinely race.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_provider import FakeProvider
from intake import report
from intake.config import ROOT
from intake.store import Store
from test_scale import make_inbox

LAUNCH = Path(__file__).with_name("launch.py")
EXPORT_LOOP = Path(__file__).with_name("export_loop.py")
READER_LOOP = Path(__file__).with_name("reader_loop.py")


@pytest.fixture
def provider():
    p = FakeProvider().start()
    yield p
    p.stop()


@pytest.fixture
def world(tmp_path, provider):
    class W:
        pass

    w = W()
    w.dir, w.provider = tmp_path, provider
    w.db, w.out = str(tmp_path / "shared" / "intake.db"), str(tmp_path / "shared" / "out")
    w.barrier = str(tmp_path / "go")
    w.ops = []
    yield w
    for op in w.ops:  # never leave stray operators behind if a test fails
        if op.proc.poll() is None:
            op.proc.kill()


class Operator:
    def __init__(self, world, name, args, barrier=None, script=None, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("INTAKE_", "OPENAI_"))}
        e.update(OPENAI_API_KEY="test-key-not-real", OPENAI_BASE_URL=world.provider.base_url,
                 INTAKE_DB=world.db, INTAKE_OUT_DIR=world.out, INTAKE_WORKERS="4", INTAKE_BACKOFF_S="0")
        e.update({k: str(v) for k, v in env.items()})
        self.name, self.log_path = name, world.dir / f"{name}.log"
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen([sys.executable, str(script or LAUNCH), barrier or world.barrier, *args],
                                     cwd=ROOT, env=e,
                                     stdout=self._log, stderr=subprocess.STDOUT)
        world.ops.append(self)

    @property
    def pid(self):
        return self.proc.pid

    def wait(self, timeout=120):
        rc = self.proc.wait(timeout)
        self._log.close()
        return rc

    @property
    def log(self):
        return self.log_path.read_text()

    def outcomes(self):
        return dict(re.findall(r"(IT-\d+\.json): ([a-z\-]+)", self.log))


def go(world):
    Path(world.barrier).write_text("go")


def settle(ops, timeout=120):
    return [op.wait(timeout) for op in ops]


def coherent(world, files, *, done_per_doc: int | None = 1):
    """Whatever the interleaving, the shared state must be coherent and complete."""
    store = Store(world.db)
    hist = store.history()
    assert not [h for h in hist if h["state"] == "pending"], "a claim was left dangling"
    names = sorted(f.name for f in files)
    for n in names:
        dones = [h for h in hist if h["document_id"] == n and h["state"] == "done"]
        if done_per_doc is not None:
            assert len(dones) == done_per_doc, f"{n}: {len(dones)} done attempts"
        # never two results for the same source *and* settings, however the operators interleaved
        assert len({(h["source_sha256"], h["extraction_fp"], h["policy_version"]) for h in dones}) == len(dones)
    out = Path(world.out)
    recs = {r["document_id"]: r for r in map(json.loads, (out / "extractions.jsonl").read_text().splitlines())}
    queue = json.loads((out / "queue.json").read_text())
    entries = {e["document_id"]: e for e in queue["entries"]}
    assert sorted(recs) == sorted(entries) == names  # complete: nothing lost to an overwrite by a stale snapshot
    for f in files:  # queue and extraction records agree on the same source version
        r, e = recs[f.name], entries[f.name]
        assert (r["attempt_id"], r["source_sha256"], r["status"]) == (e["attempt_id"], e["source_sha256"], e["status"])
        import hashlib
        assert r["source_sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
        assert r["extraction"] is not None
    report.read_consistent(out, attempts=1)  # and both files come from one export (same snapshot_id)
    return hist


# ---------------------------------------------------------------------------------------------------------
def test_four_operators_starting_together_extract_each_document_exactly_once(world):
    files = make_inbox(world.dir, 60)
    world.provider.latency_s = 0.08
    ops = [Operator(world, f"op{i}", ["intake", str(files[0].parent)], LAUNCH_CLAIM_DELAY_MS=40) for i in range(4)]
    time.sleep(1.5)  # let every process finish importing and reach the barrier
    go(world)
    assert settle(ops) == [0, 0, 0, 0]

    calls = world.provider.snapshot()
    assert len(calls) == 60 and set(calls.values()) == {1}  # no document paid for twice
    outcomes = [op.outcomes() for op in ops]
    assert sum(list(o.values()).count("extracted") for o in outcomes) == 60  # each document had one owner
    assert sum(list(o.values()).count("skipped-busy") for o in outcomes) > 0, "operators never actually collided"
    assert all("locked" not in op.log.lower() for op in ops)  # SQLite contention absorbed, never surfaced
    coherent(world, files)


def test_operators_with_different_model_settings_leave_a_coherent_queue_and_flag_the_difference(world):
    files = make_inbox(world.dir, 40)
    world.provider.latency_s = 0.06
    a = Operator(world, "a", ["intake", str(files[0].parent)], OPENAI_MODEL="model-a")
    b = Operator(world, "b", ["intake", str(files[0].parent)], OPENAI_MODEL="model-b")
    time.sleep(1.5)
    go(world)
    assert settle([a, b]) == [0, 0]
    # A different configuration legitimately means another extraction when an operator reaches a document the
    # other has already finished, so a document may be extracted once per configuration - never twice for one.
    hist = coherent(world, files, done_per_doc=None)
    by_pid = {a.pid: "model-a", b.pid: "model-b"}  # every record is attributable to the operator that made it
    for h in hist:
        assert h["settings"]["model"] == by_pid[int(h["operator"].rsplit(":", 1)[1])]
    calls = world.provider.snapshot()
    for f in files:
        n_done = len([h for h in hist if h["document_id"] == f.name and h["state"] == "done"])
        assert calls[f"request number {int(f.name[3:8])}"] == n_done  # every provider call produced a kept record
    latest = {s["document_id"]: s["current"]["settings"]["model"] for s in Store(world.db).current()}
    assert set(latest.values()) <= {"model-a", "model-b"}

    # operator B's view: documents whose current result came from model-a are stale; `refresh` updates them
    stale_expected = sorted(n for n, m in latest.items() if m == "model-a")
    assert stale_expected, "operator A never produced a current result; the scenario did not interleave"
    status = Operator(world, "status", ["status"], OPENAI_MODEL="model-b")
    assert status.wait() == 0
    assert sorted(json.loads(status.log[status.log.index("{"):])["stale"]) == stale_expected
    before = world.provider.snapshot()
    r = Operator(world, "refresh", ["refresh"], OPENAI_MODEL="model-b")
    assert r.wait() == 0
    after = world.provider.snapshot()
    for f in files:
        m = f"request number {int(f.name[3:8])}"
        had_b = any(h["document_id"] == f.name and h["state"] == "done" and h["settings"]["model"] == "model-b"
                    for h in hist)
        # A stale document needs one new call - unless operator B already extracted it earlier and operator A merely
        # finished last, in which case refresh reuses B's earlier result (the rollback rule) and calls nothing.
        expected_calls = 1 if (f.name in stale_expected and not had_b) else 0
        assert after[m] - before[m] == expected_calls, f.name
    store = Store(world.db)
    assert {s["current"]["settings"]["model"] for s in store.current()} == {"model-b"}
    kept_a = [h for h in store.history() if h["settings"]["model"] == "model-a" and h["state"] == "done"]
    assert len(kept_a) == len([1 for h in hist if h["settings"]["model"] == "model-a" and h["state"] == "done"])


def test_a_hard_killed_operator_is_recoverable_by_another_without_redoing_finished_work(world):
    files = make_inbox(world.dir, 40)
    world.provider.latency_s = 0.15
    a = Operator(world, "a", ["intake", str(files[0].parent)], INTAKE_LEASE_S=3)
    time.sleep(1.5)
    go(world)
    deadline = time.time() + 30
    while world.provider.total_calls() < 12 and time.time() < deadline:
        time.sleep(0.02)
    a.proc.send_signal(signal.SIGKILL)  # no chance to clean up: no export, no release of claims
    assert a.wait() == -signal.SIGKILL

    store = Store(world.db)
    snap = store.current()
    finished = {s["document_id"] for s in snap if s["current"]}
    in_flight = {s["document_id"] for s in snap if s["pending"]}
    assert finished and in_flight and len(in_flight) <= 4 and not finished & in_flight
    # the returning operator can tell finished work from unfinished work through the documented interface
    q = Operator(world, "q", ["queue", "--json"])
    assert q.wait() == 0
    view = json.loads(q.log[q.log.index("{"):])
    assert {e["document_id"] for e in view["entries"]} == finished
    assert {p["document_id"] for p in view["pending"]} == in_flight
    assert all(p["operator"].endswith(f":{a.pid}") for p in view["pending"])

    lease_end = max(p["lease_expires"] for p in (s["pending"] for s in snap if s["pending"]))
    time.sleep(max(0, lease_end - time.time()) + 0.3)
    b = Operator(world, "b", ["intake", str(files[0].parent)])
    assert b.wait() == 0
    calls = world.provider.snapshot()
    for n in finished:  # A's committed work was never repeated
        assert calls[f"request number {int(n[3:8])}"] == 1
    for n in in_flight:  # only the calls that were mid-flight at the kill could have been repeated
        assert calls[f"request number {int(n[3:8])}"] in (1, 2)
    assert sum(calls.values()) <= 40 + len(in_flight)
    hist = coherent(world, files)
    assert sorted(h["document_id"] for h in hist if h["state"] == "abandoned") == sorted(in_flight)  # record kept


def test_inspecting_the_queue_from_another_process_while_intake_is_running(world):
    files = make_inbox(world.dir, 60)
    world.provider.latency_s = 0.25
    a = Operator(world, "a", ["intake", str(files[0].parent)])
    time.sleep(1.5)
    go(world)
    deadline = time.time() + 30
    while world.provider.total_calls() < 12 and time.time() < deadline:
        time.sleep(0.02)
    q = Operator(world, "q", ["queue", "--json"])
    assert q.wait() == 0
    view = json.loads(q.log[q.log.index("{"):])
    done = {e["document_id"] for e in view["entries"]}
    pending = {p["document_id"] for p in view["pending"]}
    assert 0 < len(done) < 60 and 0 < len(pending) <= 4 and not done & pending  # completed work visible, rest pending
    assert all(e["status"] == "review" and e["source_sha256"] for e in view["entries"])
    assert a.wait() == 0  # reading the queue never disturbed the running intake
    assert set(world.provider.snapshot().values()) == {1}
    coherent(world, files)


def test_two_operators_retrying_after_an_outage_do_not_double_process(world):
    files = make_inbox(world.dir, 30)
    world.provider.fail_status = 503
    first = Operator(world, "outage", ["intake", str(files[0].parent)], INTAKE_MAX_ATTEMPTS=1, INTAKE_BREAKER_THRESHOLD=0)
    go(world)
    assert first.wait() == 1
    assert all(s["current"]["state"] == "error" for s in Store(world.db).current())

    world.provider.fail_status, world.provider.latency_s = None, 0.05  # the service recovers
    Path(world.barrier).unlink()
    r1 = Operator(world, "r1", ["retry"], LAUNCH_CLAIM_DELAY_MS=40)
    r2 = Operator(world, "r2", ["retry"], LAUNCH_CLAIM_DELAY_MS=40)
    time.sleep(1.5)
    go(world)
    assert settle([r1, r2]) == [0, 0]
    assert set(world.provider.snapshot().values()) == {2}  # one failed call + exactly one successful call each
    hist = coherent(world, files)
    for f in files:
        assert sorted(h["state"] for h in hist if h["document_id"] == f.name) == ["done", "error"]


def test_simultaneous_exports_never_leave_older_data_in_the_deliverables(world):
    """Operators finishing together each export; the last file written must reflect all committed work."""
    files = make_inbox(world.dir, 12)
    world.provider.latency_s = 0.02
    seed = Operator(world, "seed", ["intake", str(files[0].parent)])
    go(world)
    assert seed.wait() == 0
    Path(world.barrier).unlink()
    exporters = [Operator(world, f"e{i}", ["export"]) for i in range(8)]
    time.sleep(1.5)
    go(world)
    assert settle(exporters) == [0] * 8
    coherent(world, files)
    assert not [p for p in Path(world.out).iterdir() if p.suffix not in (".json", ".jsonl") and p.name != ".export.lock"]


def test_a_slow_exporter_cannot_overwrite_newer_work_with_its_older_snapshot(world):
    """Operator 1 snapshots the database then stalls before writing; operator 2 completes new documents and exports
    meanwhile. Operator 1's stale snapshot must not end up as the final deliverables."""
    inbox = world.dir / "bulk"
    make_inbox(world.dir, 6)
    seed = Operator(world, "seed", ["intake", str(inbox)])
    go(world)
    assert seed.wait() == 0

    slow_go, late_go = str(world.dir / "slow_go"), str(world.dir / "late_go")
    slow = Operator(world, "slow", ["export"], barrier=slow_go, LAUNCH_EXPORT_DELAY_MS=2500)
    for i in range(6, 9):  # new documents arrive while the slow exporter is mid-export
        text = json.dumps({"export_version": "1.0", "message": f"request number {i}"})
        (inbox / f"IT-{i:05d}.json").write_text(text)
    late = Operator(world, "late", ["intake", str(inbox)], barrier=late_go)
    time.sleep(1.5)
    Path(slow_go).write_text("go")  # the slow exporter takes its snapshot (6 documents) and stalls
    time.sleep(0.8)
    Path(late_go).write_text("go")  # the other operator now processes the 3 new documents and exports
    assert settle([slow, late]) == [0, 0]
    all_files = sorted(inbox.glob("*.json"))
    assert len(all_files) == 9
    coherent(world, all_files)  # fails if the slow exporter's 6-document snapshot won


def test_readers_never_see_extractions_and_queue_from_different_exports(world):
    """A writer keeps publishing new exports with a deliberate 25 ms gap between the files it replaces.

    - a naive reader (two plain reads, like `cat a b`) hits the gap and sees a mixed pair: the hazard is real, and
      the snapshot_id stamped in both files makes every such pair detectable;
    - a reader using report.read_consistent, with its verify-and-retry layer switched off so that only the shared
      export lock protects it, never sees a mixed pair.
    """
    inbox = world.dir / "bulk"
    make_inbox(world.dir, 0)
    done = str(world.dir / "writer_done")
    writer = Operator(world, "writer", [str(inbox), "40", done], script=EXPORT_LOOP, LAUNCH_WRITE_GAP_MS=25)
    naive = Operator(world, "naive", [world.out, "naive", done, "60"], script=READER_LOOP)
    locked = Operator(world, "locked", [world.out, "locked", done, "60"], script=READER_LOOP)
    time.sleep(1.5)
    go(world)
    assert settle([writer, naive, locked]) == [0, 0, 0]

    def result(op):
        return json.loads(op.log.strip().splitlines()[-1])

    n, l = result(naive), result(locked)
    assert n["errors"] == 0 and l["errors"] == 0  # replacing files is atomic: nobody ever read a torn file
    assert n["mismatches"] > 0, "the gap never produced a mixed pair for a naive reader; the test proved nothing"
    assert l["reads"] > 20 and l["mismatches"] == 0  # lock-protected reads were never mixed
    final = Operator(world, "check", ["check-export"], barrier=world.barrier)
    assert final.wait() == 0 and "OK snapshot" in final.log and "40 records" in final.log
