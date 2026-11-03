# IT Access Requests — intake and review queue

Turns free-text access requests (JSON exports) into schema-conforming records and a review queue for the IT team, using the OpenAI Chat Completions API. Requirements: `docs/requirements.md`; policy: `materials/office-policy.md`.

## Setup
Supported runtime: Python 3.14 (3.11+ expected to work), provider: OpenAI.
```
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock        # exact versions; requirements.txt has the ranges
export OPENAI_API_KEY=...                          # supplied by the facilitator; never committed or logged
```
| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | – (required for live) | credential, redacted from logs |
| `OPENAI_MODEL` | `gpt-4.1-mini` | model ID (supported configuration) |
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
| `INTAKE_DB` | `data/intake.db` | SQLite store (all attempts and history) |
| `INTAKE_OUT_DIR` | `deliverables/` | exported files |

## Commands
```
python -m intake intake materials/requests          # process a directory or files; exports on completion
python -m intake queue [--status needs-information] [--json]   # inspect while intake runs elsewhere
python -m intake status                              # counts, pending, failed, stale results, active settings
python -m intake refresh --dry-run                   # list results made under older settings/policy, and why
python -m intake refresh [IT-007.json ...]           # bring them up to date (all stale if none named)
python -m intake retry [IT-007.json ...]             # retry failed/stuck documents (all if none given)
python -m intake history [IT-007.json]               # every attempt: hash, settings, policy, result
python -m intake export                              # rewrite deliverables/ from the store
python -m intake check-export                        # prove extractions.jsonl and queue.json are from the same export
python -m intake accuracy [--expected FILE]          # compare current records with tests/expected.json (or another expected-facts file)
python -m pytest -q                                  # tests (mocked provider, offline)
python -m intake --mock intake materials/requests    # offline development run (no credentials)
```
Exit code of `intake` is 1 if any document ended in `error`.

## Inspecting the queue
`queue` prints one row per document ordered review → needs-information → error, then documents still pending. Each row has status, owner, next action and the reasons (or error). `deliverables/queue.json` has the same data plus pending claims and links to `extractions.jsonl` by `document_id` and `source_sha256`. Failed documents show their error, the retry command, and (if any) the earlier valid result labelled with its own attempt id and source hash. Earlier attempts live in `history.jsonl` / `python -m intake history`, never in the current outputs.

## How it works
- `intake/extract.py` — prompt, request config, response validation (refusal, finish reason, JSON, schema, ISO dates, exact-substring quotes), bounded retries.
- `intake/routing.py` — deterministic policy rules applied in order (override → withdrawal → needs-information → Security Review → System Owner). The model never decides routing; message text is data.
- `intake/finalize.py` — deterministic checks on every extraction before routing: an identifier in the message that differs from the filename is recorded as a `mismatch:` issue (the stated ID is kept); every null field gets an issue that explains it; a value that contradicts an explicit "Correction: …" clause — or a corrected date the model dropped, leaving a date field null — is flagged `correction-ignored:` (never overwritten) and the request goes to needs-information; the correction text is kept in `supporting_quotes`; a hedge cue in the same sentence as a concrete alternative the extraction did not use — a date ("possibly until 2026-11-09 instead"), an environment ("perhaps production is the better fit" next to a staging request) or a permission ("maybe write instead") — is flagged `hedged-alternative:` and goes to needs-information, because a tentative alternative still contradicts the stated value and a model that keeps the stated one has guessed (for an environment or permission that can send a production/write request to the wrong reviewer). The check only flags, never rewrites; it ignores negated alternatives ("not write"), restrictions ("I only need staging"), `read`/`write` used as ordinary verbs, a value the model already marked as a conflict, and anything inside a labelled correction. Re-applied to stored extractions, with no model call, when the policy version changes.
- Withdrawals, corrections and mismatches follow `materials/office-policy.md`: a withdrawal with `request_id` and `request_date` → IT Service Desk / `confirm-withdrawal` even if other fields are null (and even if its ID mismatches, because that rule comes first); each message is its own document, and absence from a later batch withdraws nothing.
- `intake/store.py` — append-only SQLite attempts; "current" is derived. `intake/pipeline.py` — per-document reuse / claim / extract / route.
- Reuse: unchanged source hash + model settings + policy version + provider mode (live/mock) → no model call. Changed source or settings → new attempt, old one retained. Changed policy only → re-route stored extraction, no model call.

## Changing the model configuration or the policy
- **Settings that invalidate results** (they are part of the settings fingerprint and are stored with every record): `OPENAI_MODEL`, `INTAKE_TEMPERATURE`, `INTAKE_MAX_OUTPUT_TOKENS`, the extraction prompt and `snapshot.schema.json`. Operational settings (workers, timeout, retries, backoff, lease, throttle, breaker, paths) do not invalidate anything.
- **Policy version** is derived automatically: `INTAKE_POLICY_LABEL` (default `northstar-policy-2026-10`) plus a hash of `materials/office-policy.md`, `intake/routing.py` and `intake/finalize.py`. Edit any of them and every result routed under the old text becomes stale; no number to bump. Set a new label for a named release (e.g. `INTAKE_POLICY_LABEL=2026-11-rev2`); the hash is always appended.
- **Workflow:** change the setting or policy → `python -m intake queue` / `status` now flag affected results as `[STALE: settings|policy]` → `python -m intake refresh --dry-run` shows each document, whether it needs a model call (`re-extract`) or only a re-route, and the exact change (e.g. `model: gpt-4.1-mini -> …`) → `python -m intake refresh`. A policy-only refresh makes no model calls and needs no credentials. `refresh` is idempotent and resumable: after an interruption, run it again and only unfinished documents are processed.
- Stale results stay in the queue until refreshed (they are still the latest result). Every refresh adds a new attempt; the earlier result remains in `history` with its own settings, policy version and source hash. Rolling a setting back reuses the earlier result with no model call.
- If a refresh attempt fails, the document shows as an error with its retry command and the earlier result is labelled with the settings that produced it (`earlier_result`). Use `retry` for failures, `refresh` for stale results.
- `queue.json` has an `active` block (settings, fingerprint, policy version) and `stale_results`; every record in `extractions.jsonl` carries the `settings`, `extraction_fp`, `policy_version` and `mode` that produced it. `python -m intake history <doc>` lists every version with its settings.
- Mock and live results are never mixed: a mock result is not reused by a live run (and `refresh` in live mode flags mock results as stale).

## Large intakes, rate limits and outages
- Reviewers can run `queue` at any time during an intake; finished documents appear as soon as each completes, and in-flight ones are listed as pending. A slow or hung message holds one worker for at most its timeout and retries, not the whole run.
- A 429 pauses all workers together (honouring `Retry-After`) and slows the pace until calls succeed again. For a 2,000-message morning start with `INTAKE_WORKERS=8` only if the quota owner agrees; expect roughly `documents × seconds-per-call ÷ workers`.
- If the provider is down, the run stops after `INTAKE_BREAKER_THRESHOLD` consecutive failures and records every remaining document as an error with its retry command. Once the service is back: `python -m intake status` then `python -m intake retry`.
- Ctrl-C is safe: finished work stays, in-flight documents are released and shown as pending; re-run the same `intake` command to continue (finished documents are skipped).

## Handover notes
- Failure recovery: run `python -m intake status`, then `python -m intake retry`. Documents interrupted mid-call appear as pending and are retried after the lease expires (or immediately with `retry <doc>`).
- Assumptions and limits (workload, shared operation, interruption) are in `deliverables/verification.md`. Key limit: SQLite supports operators sharing one machine's filesystem (several operator processes at once is tested); operators in separate offices need a shared database service, and `INTAKE_DB` must not be on a network share.
- Two operators at once is safe: each document is claimed by one operator, finished results are reused rather than repeated, and `retry` skips documents another operator already handled (`already-handled`). A document you see as `skipped-busy` is being worked on by someone else; re-run later if it stays pending past `INTAKE_LEASE_S`. Exports are serialised with a lock file (`<out dir>/.export.lock`), so the last export always reflects all committed work.
- **Reading the deliverables consistently:** two files cannot be swapped atomically, so `extractions.jsonl` and `queue.json` are stamped with the same `snapshot_id` (every line of the former, the top level of the latter). Use `python -m intake check-export` or `intake.report.read_consistent(out_dir)`; both hold a shared lock so no export can run mid-read and refuse a mixed pair. If you read the files some other way while operators are exporting, compare the `snapshot_id` values (they must be equal) or wrap both reads in `flock -s <out dir>/.export.lock`.
- Mock output (`--mock`) is plumbing-only and labelled `mode: mock` in each record; live results are `mode: live`.
- Deliverables: `deliverables/{extractions.jsonl,queue.json,accuracy.md,request-evidence.json,verification.md}`; `history.jsonl` holds earlier versions.
