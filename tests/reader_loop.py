"""Test-only reader: reads extractions.jsonl and queue.json over and over and counts mixed-version pairs.
Usage: python reader_loop.py BARRIER OUT_DIR MODE(naive|locked) DONE_FILE MAX_SECONDS
  naive  - what `cat extractions.jsonl queue.json` does: two plain reads, no coordination
  locked - report.read_consistent with its retry layer disabled (attempts=1), so only the shared lock protects it
Prints one JSON line: reads, mismatches, errors.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patches  # noqa: E402

from pathlib import Path  # noqa: E402

from intake import report  # noqa: E402

barrier, out, mode, done, max_s = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])
patches.wait_for(barrier)
reads = mismatches = errors = 0
end = time.time() + max_s
while time.time() < end and not (os.path.exists(done) and reads > 20):
    try:
        if mode == "naive":
            records = [json.loads(x) for x in (out / "extractions.jsonl").read_text().splitlines()]
            queue = json.loads((out / "queue.json").read_text())
        else:
            records, queue = report.read_consistent(out, attempts=1)
    except FileNotFoundError:
        continue  # before the first export
    except report.InconsistentSnapshot:
        reads += 1
        mismatches += 1
        continue
    except (json.JSONDecodeError, ValueError):
        errors += 1  # a torn read would show up here; atomic renames mean it never should
        continue
    reads += 1
    ids = {r.get("snapshot_id") for r in records} | {queue.get("snapshot_id")}
    by_doc = {r["document_id"]: r["attempt_id"] for r in records}
    entries_agree = all(by_doc.get(e["document_id"]) == e["attempt_id"] for e in queue["entries"])
    if len(ids) != 1 or not entries_agree or len(by_doc) != len(queue["entries"]):
        mismatches += 1
print(json.dumps({"reads": reads, "mismatches": mismatches, "errors": errors}))
