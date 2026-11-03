"""Test-only launcher: import everything first, then wait for a barrier file, then run the real CLI.

Python start-up and imports take far longer than the work in these tests, so without a barrier two
"simultaneous" operators would run one after the other. This makes them start within milliseconds.
Usage: python launch.py BARRIER_FILE [cli args...]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from intake.cli import main  # noqa: E402  (imported before waiting so start-up cost is already paid)

delay_ms = float(os.environ.get("LAUNCH_CLAIM_DELAY_MS", "0"))
if delay_ms:
    # Test-only: stretch the gap between "check for an existing result" and "claim the document", as a slow disk
    # or network filesystem would, so races that are microseconds wide in normal runs happen constantly.
    import random

    from intake.store import Store

    _claim = Store.claim

    def slow_claim(self, *a, **kw):
        time.sleep(random.uniform(0, delay_ms / 1000))
        return _claim(self, *a, **kw)

    Store.claim = slow_claim

export_delay_ms = float(os.environ.get("LAUNCH_EXPORT_DELAY_MS", "0"))
if export_delay_ms:
    # Test-only: stall an exporter after it has taken its snapshot and before it writes any file, as a slow
    # disk would. Without cross-process locking its older snapshot would land on top of newer work.
    from intake import report

    _write, _stalled = report.atomic_write, []

    def slow_write(path, text):
        if not _stalled:
            _stalled.append(1)
            time.sleep(export_delay_ms / 1000)
        return _write(path, text)

    report.atomic_write = slow_write

barrier = sys.argv[1]
deadline = time.time() + 60
while not os.path.exists(barrier):
    if time.time() > deadline:
        sys.exit("barrier never appeared")
    time.sleep(0.002)
sys.exit(main(sys.argv[2:]))
