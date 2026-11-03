# Verification

All automated tests use a **mocked** provider (`FakeClient` in `tests/conftest.py`, scripted per test). They never call the network. Expected values are written by hand from the messages and policy, not read back from extractor output.

## Commands
```
.venv/bin/python -m pytest -q                 # 44 tests, ~1 s, offline
.venv/bin/python -m intake accuracy           # live-or-mock field comparison vs tests/expected.json
.venv/bin/python -m intake --mock intake materials/requests tests/fixtures/IT-101.json   # offline smoke run
```

## Assertions and what each compares
| Scenario (test) | Assertion |
|---|---|
| complete request | IT-001 → System Owner / review / review-request; hash recorded |
| missing dates | relative-date IT-011 with null dates → needs-information, reason lists start_date, end_date |
| conflicting details | IT-004 with null environment + `conflict:` issue → needs-information |
| embedded instruction | IT-006 routed IT Service Desk / review / inspect-message even when the (mock) model returns a clean record with no override issue |
| provider failure | 429 on IT-002 after bounded retries → status error, owner IT Service Desk, next_action retry-extraction, null extraction, message contains `python -m intake retry IT-002.json`; IT-001 in the same batch still succeeds |
| unchanged rerun | second run makes 0 model calls, one queue entry, one attempt in history |
| changed source | new hash → new attempt; history holds both hashes in order; one current entry |
| failed retry after success | forced retry fails (timeout) → current is error with `earlier_result` carrying the older attempt id and hash; recovery run restores review |
| refusal / truncated / malformed JSON / schema violation / bad date / non-substring quote / content filter | each becomes an `error` record with the expected `error.kind` and null extraction (7 parametrized cases) |
| interrupted response | cut-off JSON, non-`stop` finish reason, empty body and a dropped connection are each retried up to `max_attempts` calls, then recorded as error with the retry command; a truncated response followed by a good one recovers on attempt 2 |
| failed retry after success (CLI) | after a good result, a changed source whose attempt is interrupted, then `retry IT-001.json` also interrupted → current stays error, the older valid result (older source hash) remains linked as `earlier_result` |
| retry rules | 503 then success → 2 attempts; 401 not retried (1 call) |
| unreadable export | invalid JSON file → error record `bad-export`, no crash |
| model setting change | different `OPENAI_MODEL` → re-extraction, both settings kept in history |
| policy version change | same source+settings → re-routed from stored extraction with **0** model calls, new policy version recorded |
| shared operation | second claim on a live in-progress document is refused; an expired claim is marked `abandoned` and reclaimed |
| partial availability | with IT-002 pending, the queue lists IT-001 as completed and IT-002 under `pending` |
| slow message | one 0.3 s response does not delay the other two documents (workers=3) |
| export agreement | `extractions.jsonl` and `queue.json` agree on document_id, hash, status, owner, next_action for all 20 samples |
| conflict normalization | model that guesses on a flagged conflict gets the field nulled; ordering issues are not treated as conflicts |
| routing rules | 9 parametrized cases: withdrawal, withdrawal missing date, other, identifier mismatch, start after end, production, write, admin, staging/read |
| paraphrase | IT-101 facts checked directly against its text; expected route Security Review |
| redaction | `sk-…` tokens removed from log text |

## Observed results
- **Mock:** `pytest` → 44 passed (2026-11-04). Offline `--mock` run of 21 documents → 231/231 by construction (plumbing check only).
- **Live (provider responses, `gpt-4.1-mini`):** see `accuracy.md` — final configuration 231/231 fields, 21/21 routing, one run. Earlier configurations showed 2 factual errors and then 1 omission; both repaired in code and prompt (described there).
- Rerunning live intake after each prompt edit re-extracted all 21 documents (new settings fingerprint) and kept the earlier results in history (42 retained attempts at the last export), which exercised the refresh path against the real provider.

## Operating assumptions and limits
- **Workload:** designed for 200–400 messages/day, bursts of ~2,000. Each document is one model call; `INTAKE_WORKERS` (default 4) bounds concurrency to respect the shared rate limit; 429/5xx are retried with exponential backoff and jitter, so a slow message occupies one worker only. A 2,000-message burst at 4 workers and ~1–2 s per call is roughly 10–17 minutes; **this was not load-tested** — only 21 live documents were run.
- **Shared operation:** one SQLite file in WAL mode with an at-most-one-pending-attempt-per-document unique index; claims use `BEGIN IMMEDIATE`. This suits operators on **one host or a shared filesystem with working locking**. Operators in different offices on separate machines need a shared database service (e.g. Postgres) — not implemented. Two processes were tested via claim-level tests, not with real parallel processes.
- **Interruption:** an interrupted run leaves `pending` rows. They are shown by `queue`/`status`, ignored by exports of "current" results, taken over after `INTAKE_LEASE_S` (600 s), and picked up by `python -m intake retry`. Completed work is committed per document, so nothing completed is lost. A crash mid-call may repeat that one model call.
- **Deliverable writes** are atomic (temp file + rename) from a single read snapshot, so queue and extractions agree; there is no cross-process lock on the deliverable files (last writer wins; both are derived from the DB so content is equivalent).
- **Not covered:** concurrent multi-process stress, very large exports, provider outage longer than the retry window beyond marking documents `error`, prompt-injection variants beyond IT-006 and the regex backstop (which catches only known phrasings), non-English messages, and any model other than `gpt-4.1-mini`.
- Approvals are never made by the application; every successful record waits for human review.
