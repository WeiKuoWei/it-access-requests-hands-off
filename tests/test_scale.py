"""Rate limiting, slow responses, provider outage, interruption and the 2,000-message morning (mocked provider)."""
import json
import threading
import time

import httpx2 as httpx
import openai
import pytest

from conftest import FakeClient, api_error, completion, no_sleep, record
from intake import pipeline, report
from intake.config import Config
from intake.extract import extract
from intake.throttle import Breaker, Throttle


def make_inbox(tmp_path, n):
    d = tmp_path / "bulk"
    d.mkdir()
    for i in range(n):
        (d / f"IT-{i:05d}.json").write_text(json.dumps({"export_version": "1.0", "message": f"request number {i}"}))
    return sorted(d.glob("*.json"))


def ok(message):
    """A valid response whose request_id matches the generated filename, so routing sees a complete request."""
    return completion(record(request_id=f"IT-{int(message.split()[-1]):05d}"))


def bulk_cfg(cfg, **over):
    return Config(**{**cfg.__dict__, **over})


def states(store):
    out = {}
    for s in store.current():
        c = s["current"]
        out[s["document_id"]] = (c["state"], c["error"]["kind"] if c and c["error"] else None) if c else ("pending", None)
    return out


# --- throttle ---------------------------------------------------------------------------------
class FakeTime:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_throttle_spaces_calls_and_a_429_cooldown_holds_every_caller():
    ft = FakeTime()
    th = Throttle(base_interval=0.0, clock=ft.clock, sleep=ft.sleep)
    th.wait()
    th.wait()
    assert ft.slept == []  # no pacing when healthy
    th.penalize(5.0)
    th.wait()  # first caller after the 429 waits out the whole cooldown
    assert ft.slept[0] == pytest.approx(5.0)
    th.wait()  # the next caller is spaced by the widened interval, not released simultaneously
    assert ft.slept[1] == pytest.approx(0.25)
    for _ in range(80):
        th.success()
    assert th.interval == 0.0  # spacing decays back to the configured base


def test_retry_after_header_is_honoured_and_shared(cfg):
    ft = FakeTime()
    th = Throttle(clock=ft.clock, sleep=ft.sleep)
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    limited = openai.RateLimitError(
        "slow down", response=httpx.Response(429, request=req, headers={"retry-after": "7"}), body=None)
    seq = [limited, completion(record())]
    data, meta = extract(FakeClient(lambda m, n: seq[n - 1]), cfg, "msg", sleep=no_sleep, throttle=th)
    assert meta["attempts"] == 2 and ft.slept == [pytest.approx(7.0)]  # waited the server's 7s, once, via the throttle


def test_retry_after_is_capped(cfg):
    ft = FakeTime()
    th = Throttle(clock=ft.clock, sleep=ft.sleep)
    req = httpx.Request("POST", "https://x")
    limited = openai.RateLimitError(
        "x", response=httpx.Response(429, request=req, headers={"retry-after": "9999"}), body=None)
    seq = [limited, completion(record())]
    extract(FakeClient(lambda m, n: seq[n - 1]), cfg, "msg", sleep=no_sleep, throttle=th)
    assert ft.slept == [pytest.approx(60.0)]


def test_rate_limit_without_header_falls_back_to_backoff(cfg):
    ft = FakeTime()
    th = Throttle(clock=ft.clock, sleep=ft.sleep)
    c = bulk_cfg(cfg, backoff_base_s=2.0, max_attempts=2)
    seq = [api_error(openai.RateLimitError, 429), completion(record())]
    extract(FakeClient(lambda m, n: seq[n - 1]), c, "msg", sleep=no_sleep, throttle=th)
    assert 1.0 <= ft.slept[0] <= 3.0  # base 2s * jitter (0.5..1.5)


# --- slow messages ----------------------------------------------------------------------------
def test_one_hung_message_does_not_hold_back_the_rest(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 40)
    done_at = {}
    lock = threading.Lock()

    def script(message, n):
        if message == "request number 0":
            time.sleep(1.5)  # the slow one
        with lock:
            done_at[message] = time.time()
        return ok(message)

    c = bulk_cfg(cfg, workers=4)
    start = time.time()
    pipeline.run(files, store, FakeClient(script), c, "mock")
    slow = done_at["request number 0"]
    others = [t for k, t in done_at.items() if k != "request number 0"]
    assert len(store.current()) == 40
    assert max(others) < slow  # all 39 others finished before the slow one returned
    assert time.time() - start < 3.0


def test_completed_work_is_visible_while_a_slow_message_is_still_in_flight(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 8)
    release = threading.Event()
    seen = {}

    def script(message, n):
        if message == "request number 0":
            # while this one hangs, an operator inspects the queue from another process/thread
            deadline = time.time() + 5
            while time.time() < deadline and len([s for s in store.current() if s["current"]]) < 7:
                time.sleep(0.01)
            _, queue, _ = report.build(store)
            seen["done"] = len(queue["entries"])
            seen["pending"] = [p["document_id"] for p in queue["pending"]]
        return ok(message)

    pipeline.run(files, store, FakeClient(script), bulk_cfg(cfg, workers=4), "mock")
    assert seen["done"] == 7 and seen["pending"] == ["IT-00000.json"]


# --- provider outage ------------------------------------------------------------------------------
def test_outage_opens_breaker_and_every_remaining_document_is_visible_and_retryable(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 50)
    state = {"down": True}
    client = FakeClient(lambda m, n: api_error(openai.InternalServerError, 503) if state["down"]
                        else ok(m))
    c = bulk_cfg(cfg, workers=1, breaker_threshold=5, max_attempts=2)
    res = pipeline.run(files, store, client, c, "mock", sleep=no_sleep)
    assert set(res.values()) == {"error"} and len(res) == 50
    assert client.calls == 5 * 2  # 5 documents x 2 attempts, then the breaker stopped all provider calls
    kinds = [v[1] for v in states(store).values()]
    assert kinds.count("provider-unavailable") == 5 and kinds.count("provider-down-skipped") == 45
    _, queue, _ = report.build(store)
    assert queue["counts"] == {"error": 50}
    assert all(e["retry_command"].startswith("python -m intake retry") for e in queue["entries"])
    # service recovers: the same documents are processed on retry (force), nothing lost
    state["down"] = False
    res = pipeline.run(files, store, client, c, "mock", force=True, sleep=no_sleep)
    assert set(res.values()) == {"extracted"}
    assert report.build(store)[1]["counts"] == {"review": 50}
    assert len(store.history("IT-00000.json")) == 2  # failed attempt retained next to the success


def test_auth_failure_stops_the_run_fast(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 30)
    client = FakeClient(lambda m, n: api_error(openai.AuthenticationError, 401))
    pipeline.run(files, store, client, bulk_cfg(cfg, workers=1, breaker_threshold=3), "mock", sleep=no_sleep)
    assert client.calls == 3  # not 30 identical rejections


def test_bad_messages_do_not_trip_the_breaker(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 20)
    client = FakeClient(lambda m, n: completion(None, refusal="no"))
    pipeline.run(files, store, client, bulk_cfg(cfg, workers=1, breaker_threshold=3), "mock", sleep=no_sleep)
    assert client.calls == 20  # refusals are per-message problems; every document still gets its own attempt


def test_breaker_resets_on_success():
    b = Breaker(3)
    b.failure(); b.failure(); b.success(); b.failure(); b.failure()
    assert not b.open
    b.failure()
    assert b.open


# --- interruption -------------------------------------------------------------------------------
def test_interrupt_stops_dispatch_keeps_finished_work_and_releases_in_flight(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 10)

    def script(message, n):
        if n == 3:
            raise KeyboardInterrupt
        return ok(message)

    client = FakeClient(script)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(files, store, client, bulk_cfg(cfg, workers=1), "mock")
    assert client.calls == 3  # queued documents were not started
    docs = states(store)
    assert sum(1 for s in docs.values() if s[0] == "done") == 2 and len(docs) == 3
    _, queue, _ = report.build(store)  # the interrupted document is findable, not silently missing
    assert [p["document_id"] for p in queue["pending"]] == ["IT-00002.json"]
    assert "retry" in queue["pending"][0]["note"] and len(queue["entries"]) == 2
    hist = [h["state"] for h in store.history()]
    assert hist.count("done") == 2 and hist.count("abandoned") == 1 and "pending" not in hist
    # operator reruns immediately: nothing is skipped as 'busy', finished work is reused, the rest is processed
    res = pipeline.run(files, store, FakeClient(lambda m, n: ok(m)), bulk_cfg(cfg, workers=1), "mock")
    assert list(res.values()).count("reused") == 2 and list(res.values()).count("extracted") == 8
    assert "skipped-busy" not in res.values()


# --- the 2,000-message morning ---------------------------------------------------------------------
def test_two_thousand_message_intake_and_rerun(tmp_path, store, cfg):
    files = make_inbox(tmp_path, 2000)
    lock = threading.Lock()
    in_flight = {"now": 0, "max": 0}

    def script(message, n):
        with lock:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
        time.sleep(0.005)  # stand-in for provider latency
        with lock:
            in_flight["now"] -= 1
        return ok(message)

    c = bulk_cfg(cfg, workers=8)
    client = FakeClient(script)
    t0 = time.time()
    res = pipeline.run(files, store, client, c, "mock")
    first = time.time() - t0
    assert list(res.values()).count("extracted") == 2000 and client.calls == 2000
    assert in_flight["max"] <= 8  # never exceeds the configured concurrency
    t0 = time.time()
    records, queue, _ = report.build(store)
    build = time.time() - t0
    assert len(records) == len(queue["entries"]) == 2000 and queue["counts"] == {"review": 2000}

    # unchanged rerun: one current entry per document, zero model calls, and fast (no per-document full scans)
    t0 = time.time()
    res = pipeline.run(files, store, client, c, "mock")
    rerun = time.time() - t0
    assert set(res.values()) == {"reused"} and client.calls == 2000
    assert len(report.build(store)[1]["entries"]) == 2000 and len(store.history()) == 2000
    print(f"\n2000 docs: first run {first:.1f}s, rerun {rerun:.1f}s, build {build:.1f}s")
    assert rerun < 20 and first < 60 and build < 10  # generous bounds that still catch quadratic behaviour
