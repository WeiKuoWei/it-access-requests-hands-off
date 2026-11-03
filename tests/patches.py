"""Test-only fault injection for operator subprocesses, driven by environment variables.

Races that are microseconds wide in normal runs are stretched here (as a slow disk or network filesystem would
stretch them) so the tests exercise them every time. Nothing in the product imports this module.

  LAUNCH_CLAIM_DELAY_MS   random delay before each document claim
  LAUNCH_EXPORT_DELAY_MS  stall the first file write of an export (after its snapshot was taken)
  LAUNCH_WRITE_GAP_MS     pause after each published file of an export
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def apply() -> None:
    claim_ms = float(os.environ.get("LAUNCH_CLAIM_DELAY_MS", "0"))
    if claim_ms:
        from intake.store import Store

        _claim = Store.claim

        def slow_claim(self, *a, **kw):
            time.sleep(random.uniform(0, claim_ms / 1000))
            return _claim(self, *a, **kw)

        Store.claim = slow_claim

    stall_ms = float(os.environ.get("LAUNCH_EXPORT_DELAY_MS", "0"))
    gap_ms = float(os.environ.get("LAUNCH_WRITE_GAP_MS", "0"))
    if stall_ms or gap_ms:
        from intake import report

        _write, _stalled = report.atomic_write, []

        def patched_write(path, text):
            if stall_ms and not _stalled:
                _stalled.append(1)
                time.sleep(stall_ms / 1000)
            _write(path, text)
            if gap_ms:
                time.sleep(gap_ms / 1000)

        report.atomic_write = patched_write


def wait_for(barrier: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while not os.path.exists(barrier):
        if time.time() > deadline:
            sys.exit("barrier never appeared")
        time.sleep(0.002)
