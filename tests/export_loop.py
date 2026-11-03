"""Test-only writer: repeatedly add a document, process it and publish an export, so the deliverables keep changing.
Usage: python export_loop.py BARRIER INBOX_DIR COUNT DONE_FILE
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patches  # noqa: E402

patches.apply()
from pathlib import Path  # noqa: E402

from conftest import FakeClient  # noqa: E402
from intake import pipeline, report  # noqa: E402
from intake.config import Config  # noqa: E402
from intake.store import Store  # noqa: E402
from test_scale import ok  # noqa: E402

barrier, inbox, count, done = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
cfg, client = Config(), FakeClient(lambda m, n: ok(m))
store = Store(cfg.db_path)
patches.wait_for(barrier)
for i in range(count):
    p = inbox / f"IT-{i:05d}.json"
    p.write_text(json.dumps({"export_version": "1.0", "message": f"request number {i}"}))
    pipeline.process_file(p, store, client, cfg, "mock")
    report.export(store, cfg)
Path(done).write_text("done")
