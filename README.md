# IT Access Requests — intake and review queue

Turns free-text access requests (JSON exports) into schema-conforming records and a review queue for the IT team, using the OpenAI Chat Completions API. Every successful record waits for a human reviewer; the application never approves anything. Requirements: `docs/requirements.md`; policy: `materials/office-policy.md`.

## Setup
**Supported runtime and provider:** Python 3.14 (developed and tested on 3.14.8; other versions are untested) and the OpenAI API (default model `gpt-4.1-mini`). A live run needs network access to OpenAI and an API key; tests and `--mock` runs need neither.
```
git clone <repository-url> && cd <repository>
python3 -m venv .venv
source .venv/bin/activate                 # every command below assumes the venv is active (or prefix with .venv/bin/)
pip install -r requirements.lock          # the exact versions this was tested with; requirements.txt has the ranges
export OPENAI_API_KEY=...                 # supplied by the facilitator; keep it in the environment, never in a file or a log
```
Check the install (offline, about a minute, no key needed): `python -m pytest -q` — everything should pass; without `OPENAI_API_KEY` set, one test (the scan for your real key in the repository) is reported as skipped.

### Configuration (environment variables)
Everything is optional except `OPENAI_API_KEY` for live runs. Run all commands from the repository root.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | – (required for live runs) | credential; redacted from logs |
| `OPENAI_MODEL` | `gpt-4.1-mini` | model ID (the supported, tested configuration) |
| `OPENAI_BASE_URL` | OpenAI | optional gateway/proxy |
| `INTAKE_TEMPERATURE` | `0` | |
| `INTAKE_MAX_OUTPUT_TOKENS` | `1500` | |
| `INTAKE_TIMEOUT_S` | `60` | per request |
| `INTAKE_MAX_ATTEMPTS` | `4` | per document, retryable failures only |
| `INTAKE_BACKOFF_S` | `2` | exponential base, with jitter |
| `INTAKE_WORKERS` | `4` | concurrent documents; the model quota is shared with other tools, so raise with care |
| `INTAKE_MIN_INTERVAL_S` | `0` | minimum spacing between model calls across all workers (a 429 widens it automatically, then it decays back) |
| `INTAKE_BREAKER_THRESHOLD` | `10` | consecutive provider-level failures before the run stops calling the model and records the rest as retryable errors; `0` disables |
| `INTAKE_LEASE_S` | `600` | when an in-progress claim is considered abandoned |
| `INTAKE_POLICY_LABEL` | `northstar-policy-2026-10` | name of the policy release; a hash of the policy text and routing code is always appended (see "Changing the model configuration or the policy") |
| `INTAKE_LOG_LEVEL` | `INFO` | log level (logs are credential-redacted) |
| `INTAKE_DB` | `data/intake.db` | SQLite store: every attempt and its history (not committed) |
| `INTAKE_OUT_DIR` | `deliverables/` | exported files |

## Credential handling
- The key lives only in the `OPENAI_API_KEY` environment variable: it is not a command-line argument (so it is not in `ps` output), and it is never written to the database, the exports, `request-evidence.json` or the logs.
- Everything that could echo it back is redacted: every log line **including tracebacks**, every error message stored in the database and exports (a provider or gateway can reflect the request headers in an error), and the traceback printed if the CLI itself crashes. Redaction removes the exact configured key whatever its shape, plus recognisable secret shapes (`sk-…`, masked `sk-…****`, `Authorization:` and `Bearer` values, `user:password@` in URLs, `api_key=…`).
- `request-evidence.json` shows `Authorization: Bearer [REDACTED …]`, and writing it is refused if anything credential-shaped would end up in it.
- **Not redacted, by design:** the requesters' own message text in `extractions.jsonl` (`supporting_quotes` must stay exact substrings of the source). If a requester pastes a secret into a request, it is stored verbatim; treat the exports as sensitive.
- **Outside this protection:** the OpenAI SDK's and `httpx`'s own debug logging (for example `OPENAI_LOG=debug`) does not go through this redaction; do not enable it on shared machines or paste its output anywhere.
- Checks: `python -m pytest -q tests/test_redaction.py` (plus an end-to-end test in `tests/test_parallel.py` that runs the real CLI against a provider reflecting the key); the repository-wide scan in that file fails if a secret-shaped string or your real key is ever committed. If a key does leak, rotate it with the facilitator; redaction is a safety net, not a substitute.

## First run
The 20 supplied exports are in `materials/requests/`; `tests/fixtures/IT-101.json` is the paraphrase added for the accuracy check. Each file's **name** is its `document_id` (e.g. `IT-007.json`).
```
python -m intake intake materials/requests tests/fixtures/IT-101.json   # live: 21 model calls, about a minute
python -m intake queue                                                 # the review queue
python -m intake accuracy                                              # compare with the source-derived expectations
```
- `intake` processes a directory or individual files, prints a summary (`extracted=21`, or `reused`, `rerouted`, `error`, `skipped-busy`), and rewrites `deliverables/` (they are exports of the store; `git diff` shows what changed). Run it again on the same inputs: unchanged documents are reused with no model call.
- `queue` lists one row per document: status, owner, next action and the reasons. You can run it from another terminal while an intake is still going.
- `accuracy` compares the **current** records with `tests/expected.json` (hand-written from the messages and policy) and prints `field exact match: 231/231`, `routing match: 21/21` and any factual errors or omissions. It only scores documents that are in the store, so process all 21 files to get 231; with only `materials/requests` it prints `220/220`. Live results vary a little from run to run; `deliverables/accuracy.md` explains the procedure and records the observed results.

## Commands
Run `python -m intake --help` (or `<command> --help`). Documents are named by file name.

| Command | What it does |
|---|---|
| `python -m intake intake PATH... [--force]` | process export files or directories; exits 1 if any document ended in `error` |
| `python -m intake queue [--status review\|needs-information\|error] [--json]` | the current review queue, then documents still pending; stale results are marked `[STALE: …]` |
| `python -m intake status` | counts, pending, **failed**, **stale**, and the active settings and policy version |
| `python -m intake retry [IT-007.json ...]` | re-run failed or stuck documents (all of them if none named); exits 1 if any still fail |
| `python -m intake refresh [--dry-run] [IT-007.json ...]` | bring results made under older settings or policy up to date; `--dry-run` only lists them |
| `python -m intake history [IT-007.json]` | every attempt (JSON lines): source hash, settings, policy version, result, error |
| `python -m intake export` | rewrite `deliverables/` (and `request-evidence.json`) from the store |
| `python -m intake check-export` | prove `extractions.jsonl` and `queue.json` come from the same export; exits 1 if not |
| `python -m intake accuracy [--expected FILE]` | field-by-field comparison with `tests/expected.json` or another expected-facts file |
| `python -m pytest -q` | the test suite: offline, mocked provider, about a minute (`tests/test_policy_cases.py` alone runs in a second) |

**Offline development (`--mock`).** `--mock` is a global flag that goes *before* the command and replaces the model with a canned one that replays the expected facts for the known sample messages. It checks the plumbing and says **nothing about model accuracy**. So that it can never overwrite real work, a mock run uses its own store and output directory, `data/mock/` (gitignored), unless you set `INTAKE_DB` / `INTAKE_OUT_DIR` yourself. Use `--mock` on every command when you want to look at mock results, for example:
```
python -m intake --mock intake materials/requests tests/fixtures/IT-101.json
python -m intake --mock queue
python -m intake --mock accuracy          # 231/231 by construction
```
A plain `python -m intake queue` afterwards still shows the real store, which is separate.

## When something goes wrong
- **A document failed** (provider error, refusal, malformed output): its queue row has status `error`, owner IT Service Desk, next action `retry-extraction`, and a message ending in the exact retry command. `python -m intake status` lists the failed documents; fix the cause (for example the key, or wait for a rate limit), then `python -m intake retry` (all) or `python -m intake retry IT-007.json`. The last valid result, if there was one, stays visible as `earlier_result` with its own source hash.
- **The provider is down:** the run stops after `INTAKE_BREAKER_THRESHOLD` consecutive failures and records every remaining document as a retryable error, so a large intake ends quickly with everything accounted for. Once the service is back: `status`, then `retry`.
- **The run was interrupted** (Ctrl-C, deployment): finished documents stay done. After Ctrl-C the in-flight documents are released and shown as `pending` with a retry note; re-running the same `intake` command continues and skips finished work. After a hard kill the claims stay `pending` until `INTAKE_LEASE_S` (default 600 s) expires; then `retry` or re-running `intake` picks them up. A document that says `skipped-busy` is being worked on by another operator.
- **`OPENAI_API_KEY is not set`:** export the key (live runs) or use `--mock`.
- **The queue is empty on a new install:** nothing has been processed yet; run `intake`.

## Inspecting the queue
`queue` prints one row per document ordered review → needs-information → error, then documents still pending. Each row has status, owner, next action and the reasons (or error). `deliverables/queue.json` has the same data plus pending claims and links to `extractions.jsonl` by `document_id` and `source_sha256`. Failed documents show their error, the retry command, and (if any) the earlier valid result labelled with its own attempt id and source hash. Earlier attempts live in `history.jsonl` / `python -m intake history`, never in the current outputs.

Routing, in order (`materials/office-policy.md`): instruction-override attempt → IT Service Desk / `inspect-message`; withdrawal with request ID and date → IT Service Desk / `confirm-withdrawal`; other/null kind, conflicting details, identifier mismatch or any missing required fact → IT Service Desk / `needs-information`, `clarify`; complete production, write or admin request → Security Review / `review-risk-then-owner`; complete staging/read request → System Owner / `review-request`.

## Deliverables
| File | What it is |
|---|---|
| `deliverables/extractions.jsonl` | one current record per document: `document_id`, `source_sha256`, `extraction` (null on error), `status`, `owner`, `next_action`, `error`, plus the settings and policy version that produced it |
| `deliverables/queue.json` | the review queue, linked to the records by `document_id` and `source_sha256` |
| `deliverables/history.jsonl` | earlier attempts, kept apart from the current outputs |
| `deliverables/request-evidence.json` | credential-redacted example model request, retry policy and response handling |
| `deliverables/accuracy.md` | expected facts, comparison procedure, mocked vs live results (written by hand around the numbers `accuracy` prints) |
| `deliverables/verification.md` | what the tests assert, observed results, operating assumptions and limits (written by hand) |

The first four are generated: `python -m intake export` rewrites them from the store.

## How it works
- `intake/extract.py` — prompt, request config, response validation (refusal, finish reason, JSON, schema, ISO dates, exact-substring quotes), bounded retries.
- `intake/routing.py` — deterministic policy rules applied in order (override → withdrawal → needs-information → Security Review → System Owner). The model never decides routing; message text is data.
- `intake/finalize.py` — deterministic checks on every extraction before routing: an identifier in the message that differs from the filename is recorded as a `mismatch:` issue (the stated ID is kept); every null field gets an issue that explains it; a value that contradicts an explicit "Correction: …" clause — or a corrected date the model dropped, leaving a date field null — is flagged `correction-ignored:` (never overwritten) and the request goes to needs-information; the correction text is kept in `supporting_quotes`; a hedge cue in the same sentence as a concrete alternative the extraction did not use — a date ("possibly until 2026-11-09 instead"), an environment ("perhaps production is the better fit" next to a staging request) or a permission ("maybe write instead") — is flagged `hedged-alternative:` and goes to needs-information, because a tentative alternative still contradicts the stated value and a model that keeps the stated one has guessed (for an environment or permission that can send a production/write request to the wrong reviewer). The checks only flag, never rewrite; the hedge check ignores negated alternatives ("not write"), restrictions ("I only need staging"), `read`/`write` used as ordinary verbs, a value the model already marked as a conflict, and anything inside a labelled correction. Re-applied to stored extractions, with no model call, when the policy version changes.
- Withdrawals, corrections and mismatches follow `materials/office-policy.md`: a withdrawal with `request_id` and `request_date` → IT Service Desk / `confirm-withdrawal` even if other fields are null (and even if its ID mismatches, because that rule comes first); each message is its own document, and absence from a later batch withdraws nothing.
- `intake/store.py` — append-only SQLite attempts; "current" is derived. `intake/pipeline.py` — per-document reuse / claim / extract / route.
- Reuse: unchanged source hash + model settings + policy version + provider mode (live/mock) → no model call. Changed source or settings → new attempt, old one retained. Changed policy only → re-route stored extraction, no model call.

## Changing the model configuration or the policy
- **Settings that invalidate results** (they are part of the settings fingerprint and are stored with every record): `OPENAI_MODEL`, `INTAKE_TEMPERATURE`, `INTAKE_MAX_OUTPUT_TOKENS`, the extraction prompt and `snapshot.schema.json`. Operational settings (workers, timeout, retries, backoff, lease, throttle, breaker, paths) do not invalidate anything.
- **Policy version** is derived automatically: `INTAKE_POLICY_LABEL` plus a hash of `materials/office-policy.md`, `intake/routing.py` and `intake/finalize.py`. Edit any of them and every result routed under the old text becomes stale; no number to bump. Set a new label for a named release (e.g. `INTAKE_POLICY_LABEL=2026-11-rev2`); the hash is always appended.
- **Workflow:** change the setting or policy → `python -m intake queue` / `status` now flag affected results as `[STALE: settings|policy]` → `python -m intake refresh --dry-run` shows each document, whether it needs a model call (`re-extract`) or only a re-route, and the exact change (e.g. `model: gpt-4.1-mini -> …`) → `python -m intake refresh`. A policy-only refresh makes no model calls and needs no credentials. `refresh` is idempotent and resumable: after an interruption, run it again and only unfinished documents are processed.
- Stale results stay in the queue until refreshed (they are still the latest result). Every refresh adds a new attempt; the earlier result remains in `history` with its own settings, policy version and source hash. Rolling a setting back reuses the earlier result with no model call.
- If a refresh attempt fails, the document shows as an error with its retry command and the earlier result is labelled with the settings that produced it (`earlier_result`). Use `retry` for failures, `refresh` for stale results.
- `queue.json` has an `active` block (settings, fingerprint, policy version) and `stale_results`; every record in `extractions.jsonl` carries the `settings`, `extraction_fp`, `policy_version` and `mode` that produced it.
- Mock and live results are never mixed: a mock result is not reused by a live run (and `refresh` in live mode flags mock results as stale).

## Large intakes, rate limits and outages
- Reviewers can run `queue` at any time during an intake; finished documents appear as soon as each completes, and in-flight ones are listed as pending. A slow or hung message holds one worker for at most its timeout and retries, not the whole run.
- A 429 pauses all workers together (honouring `Retry-After`) and slows the pace until calls succeed again. For a 2,000-message morning start with `INTAKE_WORKERS=8` only if the quota owner agrees; expect roughly `documents × seconds-per-call ÷ workers`. (Only mocked load has been tested; see `deliverables/verification.md`.)

## Handover notes
- Assumptions and limits (workload, shared operation, interruption) are in `deliverables/verification.md`. Key limit: SQLite supports operators sharing one machine's filesystem (several operator processes at once is tested); operators in separate offices need a shared database service, and `INTAKE_DB` must not be on a network share.
- Two operators at once is safe: each document is claimed by one operator, finished results are reused rather than repeated, and `retry` skips documents another operator already handled (`already-handled`). Exports are serialised with a lock file (`<out dir>/.export.lock`), so the last export always reflects all committed work.
- **Reading the deliverables consistently:** two files cannot be swapped atomically, so `extractions.jsonl` and `queue.json` are stamped with the same `snapshot_id` (every line of the former, the top level of the latter). Use `python -m intake check-export` or `intake.report.read_consistent(out_dir)`; both hold a shared lock so no export can run mid-read and refuse a mixed pair. If you read the files some other way while operators are exporting, compare the `snapshot_id` values (they must be equal) or wrap both reads in `flock -s <out dir>/.export.lock`.
- Mock output is plumbing-only and labelled `mode: mock` in each record; live results are `mode: live`.
