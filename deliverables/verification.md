# Verification

All automated tests use a **mocked** provider (`FakeClient` in `tests/conftest.py`, scripted per test). They never call the network. Expected values are written by hand from the messages and policy, not read back from extractor output.

## Commands
```
.venv/bin/python -m pytest -q                 # 56 tests, ~11 s, offline (the 2,000-document test is ~8 s of that)
.venv/bin/python -m pytest -q -s tests/test_scale.py   # rate limit / slow response / outage / interrupt / 2,000 messages
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
| shared 429 cooldown | after a 429 every worker's next call is held until the cooldown ends, then spaced by a widened interval; spacing decays back to the base after successes (fake clock, `tests/test_scale.py`) |
| `Retry-After` | a numeric header is honoured once through the shared throttle (7 s → one 7 s wait); capped at 60 s; without the header the exponential backoff (base × jitter) is used |
| one hung message | 40 documents, one blocks 1.5 s with 4 workers: the other 39 all finish before it returns and total time stays under 3 s |
| inspect while pending | with one document still in flight, a queue read from another thread shows the 7 finished entries and lists the in-flight one under `pending` |
| provider outage | 50 documents, provider returns 503: the breaker opens after 5 consecutive failed documents (10 calls), the other 45 are recorded as `provider-down-skipped` errors without any call; all 50 show a retry command; after recovery a forced retry completes all 50 and history keeps the failed attempts |
| auth failure / refusals | a 401 stops the run after 3 calls instead of 30; per-message refusals do not trip the breaker (all 20 get their own attempt) |
| interrupt | Ctrl-C on the 3rd call with 10 queued documents: no further call is made, the 2 finished results stay, the in-flight document is released (`abandoned`) and listed under `pending` with a retry note; an immediate rerun reuses the 2, processes the other 8 and skips nothing as busy |
| 2,000-message morning | 2,000 generated exports, 8 workers, 5 ms mock latency: all 2,000 extracted, never more than 8 calls in flight, 2,000 current entries; an unchanged rerun makes 0 calls and finishes in under 1 s |
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
- **Mock:** `pytest` → 56 passed (2026-11-04), run repeatedly with the same result. 2,000-document mocked run on the development machine: first intake 7.0 s, unchanged rerun 0.6 s, queue/extraction build 0.1 s (these measure the application and SQLite overhead only, with 5 ms of simulated latency). Offline `--mock` run of 21 documents → 231/231 by construction (plumbing check only).
- **Live (provider responses, `gpt-4.1-mini`):** see `accuracy.md` — final configuration 231/231 fields, 21/21 routing, one run. Earlier configurations showed 2 factual errors and then 1 omission; both repaired in code and prompt (described there).
- Rerunning live intake after each prompt edit re-extracted all 21 documents (new settings fingerprint) and kept the earlier results in history (42 retained attempts at the last export), which exercised the refresh path against the real provider.

## Operating assumptions and limits
- **Workload:** designed for 200–400 messages/day, bursts of ~2,000. Each document is one model call. `INTAKE_WORKERS` (default 4) bounds concurrency. A slow message occupies one worker for at most `INTAKE_TIMEOUT_S` × `INTAKE_MAX_ATTEMPTS` plus backoff (default ≈ 4 min worst case), never the whole run. Rate limits are handled by one throttle shared by all workers: a 429 (honouring `Retry-After`, capped at 60 s) pauses every worker and widens call spacing, which decays after successes; `INTAKE_MIN_INTERVAL_S` sets a permanent floor if the shared model quota needs one. Elapsed time for a burst is about `documents × latency ÷ workers`: 2,000 messages at 1.5 s per call is ~12 minutes with 4 workers, ~6 with 8 — **an estimate; the live provider was only exercised with 21 documents and never driven into a real 429**, so the throttle is verified against simulated 429s only. Raise workers only with the quota owner's agreement, because the model is shared with other internal tools.
- **Outage:** after `INTAKE_BREAKER_THRESHOLD` (default 10) consecutive documents fail with a provider-level error (429, timeout, connection, 5xx, auth) the run stops calling the provider and records every remaining document as an error (`provider-down-skipped`) with its retry command, so a 2,000-message intake ends in minutes with every document accounted for. Per-message problems (refusal, malformed output) do not trip it. Breaker state is per run; `python -m intake retry` starts fresh.
- **Shared operation:** one SQLite file in WAL mode with an at-most-one-pending-attempt-per-document unique index; claims use `BEGIN IMMEDIATE`. This suits operators on **one host or a shared filesystem with working locking**. Operators in different offices on separate machines need a shared database service (e.g. Postgres) — not implemented. Two processes were tested via claim-level tests, not with real parallel processes.
- **Interruption:** Ctrl-C stops new documents from starting, keeps everything already committed, and releases this process's in-flight claims so a rerun does not wait; those documents appear under `pending` with a retry note. After a hard kill (no chance to release) claims remain `pending` and are taken over after `INTAKE_LEASE_S` (600 s) or by `python -m intake retry <doc>` once the lease has expired. Documents that were never started have no record yet — the way to find them is to re-run `intake` on the same export directory, which skips finished work. A crash mid-call may repeat that one model call.
- **Deliverable writes** are atomic (temp file + rename) from a single read snapshot, so queue and extractions agree; there is no cross-process lock on the deliverable files (last writer wins; both are derived from the DB so content is equivalent).
- **Not covered:** concurrent multi-process stress, very large exports, provider outage longer than the retry window beyond marking documents `error`, prompt-injection variants beyond IT-006 and the regex backstop (which catches only known phrasings), non-English messages, and any model other than `gpt-4.1-mini`.
- Approvals are never made by the application; every successful record waits for human review.
