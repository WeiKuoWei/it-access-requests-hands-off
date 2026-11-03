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
| `INTAKE_WORKERS` | `4` | concurrent documents (rate-limit control) |
| `INTAKE_LEASE_S` | `600` | when an in-progress claim is considered abandoned |
| `INTAKE_DB` | `data/intake.db` | SQLite store (all attempts and history) |
| `INTAKE_OUT_DIR` | `deliverables/` | exported files |

## Commands
```
python -m intake intake materials/requests          # process a directory or files; exports on completion
python -m intake queue [--status needs-information] [--json]   # inspect while intake runs elsewhere
python -m intake status                              # counts, pending, failed documents, active settings
python -m intake retry [IT-007.json ...]             # retry failed/stuck documents (all if none given)
python -m intake history [IT-007.json]               # every attempt: hash, settings, policy, result
python -m intake export                              # rewrite deliverables/ from the store
python -m intake accuracy                            # compare current records with tests/expected.json
python -m pytest -q                                  # tests (mocked provider, offline)
python -m intake --mock intake materials/requests    # offline development run (no credentials)
```
Exit code of `intake` is 1 if any document ended in `error`.

## Inspecting the queue
`queue` prints one row per document ordered review → needs-information → error, then documents still pending. Each row has status, owner, next action and the reasons (or error). `deliverables/queue.json` has the same data plus pending claims and links to `extractions.jsonl` by `document_id` and `source_sha256`. Failed documents show their error, the retry command, and (if any) the earlier valid result labelled with its own attempt id and source hash. Earlier attempts live in `history.jsonl` / `python -m intake history`, never in the current outputs.

## How it works
- `intake/extract.py` — prompt, request config, response validation (refusal, finish reason, JSON, schema, ISO dates, exact-substring quotes), bounded retries.
- `intake/routing.py` — deterministic policy rules applied in order (override → withdrawal → needs-information → Security Review → System Owner). The model never decides routing; message text is data.
- `intake/store.py` — append-only SQLite attempts; "current" is derived. `intake/pipeline.py` — per-document reuse / claim / extract / route.
- Reuse: unchanged source hash + model settings + policy version → no model call. Changed source or settings → new attempt, old one retained. Changed policy only → re-route stored extraction, no model call. Change `POLICY_VERSION` in `intake/config.py` when the policy changes.

## Handover notes
- Failure recovery: run `python -m intake status`, then `python -m intake retry`. Documents interrupted mid-call appear as pending and are retried after the lease expires (or immediately with `retry <doc>`).
- Assumptions and limits (workload, shared operation, interruption) are in `deliverables/verification.md`. Key limit: SQLite supports operators sharing one host/filesystem; separate offices need a shared database service.
- Mock output (`--mock`) is plumbing-only and labelled `mode: mock` in each record; live results are `mode: live`.
- Deliverables: `deliverables/{extractions.jsonl,queue.json,accuracy.md,request-evidence.json,verification.md}`; `history.jsonl` holds earlier versions.
