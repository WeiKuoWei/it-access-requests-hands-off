# Accuracy

## Expected fields (source-derived)
Written by hand from the message text and `materials/office-policy.md` (file: `tests/expected.json`) **before** and independently of any extractor output. `IT-101` is a paraphrase I authored (`tests/fixtures/IT-101.json`): "view-only entry", "live environment" and "the person I report to" stand in for read / production / manager. Null means the message does not give a resolved value (absent, conflicting without a correction, or a relative date). Notable derivations: IT-004 environment and IT-020 manager conflict with no correction → null; IT-008 and IT-015 contain explicit corrections → corrected values used; IT-011 "next Monday - Friday" is relative → null; IT-014 start is after end → both kept, routed to clarify; IT-016 states IT-061 → kept, mismatch with filename; IT-019 change from write to read → permission `read`; IT-006 carries an override attempt → inspect-message.

| document | id | date | kind | requester | team | system | env | perm | start | end | manager | expected routing (owner / status / next action) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| IT-001.json | IT-001 | 2026-10-22 | new | Maya Chen | Analytics | Atlas | staging | read | 2026-10-23 | 2026-11-06 | Priya Shah | System Owner / review / review-request |
| IT-002.json | IT-002 | 2026-10-22 | new | Owen Reed | Payments | Ledger | production | write | 2026-10-24 | 2026-10-25 | Elena Park | Security Review / review / review-risk-then-owner |
| IT-003.json | IT-003 | 2026-10-22 | new | Lina Gomez | Customer Success | Beacon | null | null | null | null | Priya Shah | IT Service Desk / needs-information / clarify |
| IT-004.json | IT-004 | 2026-10-22 | new | Theo Malik | Quality Engineering | Atlas | null | read | 2026-10-23 | 2026-10-30 | Nia Patel | IT Service Desk / needs-information / clarify |
| IT-005.json | IT-005 | 2026-10-22 | withdrawal | Ava Brooks | Design | null | null | null | null | null | Elena Park | IT Service Desk / review / confirm-withdrawal |
| IT-006.json | IT-006 | 2026-10-22 | new | Finn Lee | Platform | Atlas | staging | read | 2026-10-23 | 2026-10-24 | Nia Patel | IT Service Desk / review / inspect-message |
| IT-007.json | IT-007 | 2026-10-22 | change | Grace Tan | Developer Experience | Forge | staging | write | 2026-10-24 | 2026-10-28 | Nia Patel | Security Review / review / review-risk-then-owner |
| IT-008.json | IT-008 | 2026-10-22 | new | Noah Kim | Finance | Atlas | staging | read | 2026-10-25 | 2026-10-31 | Priya Shah | System Owner / review / review-request |
| IT-009.json | IT-009 | 2026-10-22 | new | Isla Morgan | Support | Beacon | staging | read | 2026-10-26 | 2026-10-29 | null | IT Service Desk / needs-information / clarify |
| IT-010.json | IT-010 | 2026-10-22 | new | Ben Carter | Operations | Ledger | production | read | 2026-10-27 | null | Elena Park | IT Service Desk / needs-information / clarify |
| IT-011.json | IT-011 | 2026-10-22 | new | Zoe Adams | Research | Atlas | staging | read | null | null | Priya Shah | IT Service Desk / needs-information / clarify |
| IT-012.json | IT-012 | 2026-10-22 | new | Ravi Shah | Infrastructure | Forge | staging | admin | 2026-10-26 | 2026-10-27 | Nia Patel | Security Review / review / review-risk-then-owner |
| IT-013.json | IT-013 | 2026-10-22 | new | Emma Wilson | Compliance | Beacon | production | read | 2026-10-28 | 2026-10-30 | Elena Park | Security Review / review / review-risk-then-owner |
| IT-014.json | IT-014 | 2026-10-22 | new | Lucas Brown | Sales Operations | Atlas | staging | read | 2026-11-05 | 2026-11-02 | Priya Shah | IT Service Desk / needs-information / clarify |
| IT-015.json | IT-015 | 2026-10-22 | new | Mia Davis | Learning | Beacon | staging | read | 2026-10-29 | 2026-10-31 | Elena Park | System Owner / review / review-request |
| IT-016.json | IT-061 | 2026-10-22 | new | Alex Turner | Platform | Forge | staging | read | 2026-10-26 | 2026-10-28 | Nia Patel | IT Service Desk / needs-information / clarify |
| IT-017.json | IT-017 | 2026-10-22 | other | Chloe Martin | Communications | null | null | null | null | null | null | IT Service Desk / needs-information / clarify |
| IT-018.json | IT-018 | 2026-10-22 | new | Daniel Wright | Data Engineering | Atlas | staging | read | 2026-10-27 | 2026-11-03 | Priya Shah | System Owner / review / review-request |
| IT-019.json | IT-019 | 2026-10-22 | change | Sara Ahmed | Customer Operations | Beacon | production | read | 2026-10-28 | 2026-11-04 | Elena Park | Security Review / review / review-risk-then-owner |
| IT-020.json | IT-020 | 2026-10-22 | new | Ethan Clark | Product | Atlas | staging | read | 2026-10-29 | 2026-11-02 | null | IT Service Desk / needs-information / clarify |
| IT-101.json (paraphrase) | IT-101 | 2026-10-23 | new | Hana Ito | Security Analytics | Forge | production | read | 2026-10-27 | 2026-10-29 | Priya Shah | Security Review / review / review-risk-then-owner |

## Comparison procedure
`python -m intake accuracy` (implemented in `intake/report.py: compare`) reads the **current** record per document and compares it with `tests/expected.json`.
- **Fields compared:** the 11 scalar fields `request_id, request_date, kind, requester, team, system, environment, permission, start_date, end_date, manager`. Exact string equality; null must equal null.
- **Not exact-matched:** `business_purpose` (free text), `supporting_quotes` (checked at runtime to be exact substrings of the message), `issues` (free text; their effect is measured through routing).
- **Denominator:** 11 × documents that have both an expected entry and a current record = 11 × 21 = 231. A document with a null extraction (error) scores 0 of 11 and counts as omissions.
- **Routing:** `status`, `owner` and `next_action` must all equal the expected triple; counted per document (21), only for documents with an extraction.
- **Factual error:** a non-null extracted value that differs from the expected value, including a value where null was expected (a guess). **Omission:** null where a value was expected. Reported separately because a guess is worse for reviewers than a visible gap.

## Results
### Mocked (plumbing only)
`python -m intake --mock …` replays the gold values from `tests/expected.json`, so it scores 231/231 by construction. It proves validation, routing, storage and export work; it says **nothing** about model accuracy. Provider-failure, refusal, truncation, malformed-output and retry cases are likewise mocked (see `verification.md`).

### Live observations (provider responses)
Model `gpt-4.1-mini`, temperature 0, `max_completion_tokens` 1500, strict JSON-schema response format, on 2026-11-04. One run per configuration, all 21 documents. Settings fingerprints are in `data/intake.db` history (`python -m intake history IT-004.json`).

| prompt revision (extraction_fp) | field exact match | factual errors | omissions | routing |
|---|---|---|---|---|
| initial (`5df59168eb20bc33`) | 229/231 | 2 | 0 | 21/21 |
| + "never choose a side in a conflict" and null-enforcement (`76b9d8e4a744daa8`) | 230/231 | 0 | 1 | 21/21 |
| + conflict only for same-field contradictions; ordering is `range:` (`32d1a53445ef7558`, current) | 231/231 | 0 | 0 | 21/21 |

What went wrong and how it was repaired:
1. The model resolved the unresolved conflicts itself: IT-004 `environment` = production and IT-020 `manager` = Priya Shah (expected null). These were factual errors. Routing was still right because the model tagged a conflict. Repair: stronger prompt plus `normalize_conflicts` in `intake/extract.py`, which nulls a field the model itself flagged as conflicting.
2. The first version of that normalizer nulled IT-014 `end_date` because the model labelled a start-after-end problem as a "conflict" (1 omission). Repair: the prompt reserves `conflict:` for contradictory values and uses `range:` for ordering; the normalizer ignores ordering wording.

**Limits:** the prompt was tuned while looking at these same 20 messages, so 231/231 is optimistic for unseen wording. The single paraphrase (IT-101) is one sample. Each configuration was run once; temperature 0 is not guaranteed deterministic. Do not treat this as a general accuracy estimate.

## Withdrawal, correction and identifier-mismatch handling (fresh-wording live probe)
The 20 supplied messages contain one withdrawal (IT-005), two explicit corrections (IT-008, IT-015) and one identifier mismatch (IT-016), each in a single wording. To see whether the handling generalises, I wrote **11 further messages** (`tests/fixtures/policy_probe/`, expected facts in `tests/policy_probe_expected.json`, written from the message text and the policy, not from output; a test checks that every expected value literally appears in its message). Cases: a withdrawal with no date, a withdrawal that restates the original details, a withdrawal whose stated ID differs from the file, corrections of the permission / the manager / only the end date / the environment / a mistyped request ID, a correction that replaces dates with a relative date, an *unlabelled* change of mind (must stay unresolved), and a plain identifier mismatch.

Reproduce with a scratch database (live provider, 11 calls):
```
INTAKE_DB=/tmp/probe.db INTAKE_OUT_DIR=/tmp/probe-out python -m intake intake tests/fixtures/policy_probe
INTAKE_DB=/tmp/probe.db INTAKE_OUT_DIR=/tmp/probe-out python -m intake accuracy --expected tests/policy_probe_expected.json
```
**Live observation** (`gpt-4.1-mini`, settings fingerprint `32d1a53445ef7558`, one run, 2026-11-04): **119/121 fields exact, routing 10/11**, using the same counting rules as above (11 fields × 11 documents).

| | result |
|---|---|
| withdrawals (IT-201, IT-202, IT-203) | routed as the policy requires: no date → clarify; complete → confirm-withdrawal, including the one with a mismatched ID (rule 2 precedes the mismatch rule) |
| corrections (IT-204–207, IT-209, IT-212) | all corrected values applied correctly; each correction preserved in `supporting_quotes`; the relative-date correction left the dates null |
| unlabelled change of mind (IT-208) | left unresolved (environment null, `conflict:` issue) → clarify |
| identifier mismatches (IT-203, IT-210) | routing was correct but the model recorded **no** issue in 3 of 3 mismatch cases (these two and IT-016); intake now adds `mismatch:` itself |
| unexplained nulls | the model left a null with no issue in several records; intake now adds `missing:` for each |
| **errors** (1 factual error, 1 omission) | IT-202 `system` = `"The Beacon"` (expected `Beacon`); IT-205 `team` = null although "Sam Ortiz, Platform:" states it, which also routed IT-205 to clarify instead of System Owner. Both are model extraction errors outside these three policy areas; intake flags the null but cannot repair either. Not fixed. |

The mismatch and unexplained-null findings above were produced by the live model and then handled by deterministic code (`intake/finalize.py`); the final scores were obtained by re-applying that code to the stored live outputs through a policy refresh (no new model calls). The 20+1 supplied-message scores above are unchanged by it (231/231, 21/21).
**Limits:** 11 messages, one run, written by me while looking at the supplied samples; the probe is a sanity check on wording, not an estimate of accuracy.

## Model / configuration
`OPENAI_MODEL=gpt-4.1-mini` (default), Chat Completions, `temperature=0`, `max_completion_tokens=1500`, `response_format=json_schema` strict with `snapshot.schema.json`, timeout 60 s, up to 4 attempts. Policy version `northstar-policy-2026-10+1af024e9` (label plus a hash of `office-policy.md`, `routing.py` and `finalize.py`; the three live runs above were routed under the earlier hand-numbered `northstar-policy-2026-10-v1`, and policy-only `refresh` runs re-routed the stored extractions with no model call — field and routing scores are unchanged at 231/231 and 21/21). Full example in `request-evidence.json`.
