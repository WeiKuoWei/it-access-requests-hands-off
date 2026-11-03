"""Deterministic checks applied to every extraction before routing (and again when the policy changes).

The model is not trusted to follow every policy sentence, so the ones that can be checked mechanically are:

* identifier mismatch  - the stated request ID differs from the filename: record it as an issue (policy: retain the
  stated identifier, record the mismatch as an issue);
* unexplained nulls    - every null scalar must be explained by an issue (policy: identify absent facts in issues);
* corrections          - an explicit "Correction: ..." replaces the earlier value. We never overwrite the model's
  answer, but when a correction clause plainly names a value and the extraction disagrees, we flag it so a human
  looks; and the correction statement itself is kept in supporting_quotes.

All functions are idempotent (running them again changes nothing), so they can be re-run on stored extractions when
this file or routing.py changes, without calling the model. This file is part of the policy version hash.
"""
from __future__ import annotations

import re
from pathlib import PurePath

from .routing import ids_match

SCALARS = ["request_id", "request_date", "kind", "requester", "team", "system", "environment", "permission",
           "business_purpose", "start_date", "end_date", "manager"]
_ALIASES = {"request_id": ("request id", "identifier", "request number"), "business_purpose": ("purpose",),
            "request_date": ("request date", "submission date")}

# "(Correction: ...)" up to the closing parenthesis, or "Correction: ..." to the end of the line
CORRECTION = re.compile(r"\(\s*correction\b[^)]*\)|\bcorrection\b\s*[:\-–—][^\n]*", re.I)
_PAST = re.compile(r"\b(old|previous|previously|original|originally|earlier|wrong|incorrect|mistaken|was)\b", re.I)


def correction_clauses(message: str) -> list[str]:
    """Verbatim substrings of the message that are explicitly labelled as corrections."""
    return [m.group(0) for m in CORRECTION.finditer(message)]


def _mentions(issues: list[str], field: str) -> bool:
    text = " ".join(issues).lower()
    return any(n in text for n in (field, field.replace("_", " "), *_ALIASES.get(field, ())))


def _add(extraction: dict, issue: str) -> None:
    if issue not in extraction["issues"]:
        extraction["issues"].append(issue)


def _resolve(clause: str, vocab: tuple[str, ...]) -> str | None:
    """The value a correction clause plainly asks for, or None when it is not unambiguous."""
    alt = "|".join(vocab)
    words = list(dict.fromkeys(w.lower() for w in re.findall(rf"\b({alt})\b", clause, re.I)))
    if len(words) == 1:
        return words[0]
    # "... is/should be/now <value>" - the first such assertion, unless it describes the old value
    for m in re.finditer(rf"(?:\bis|\bare|should be|\bnow|to be|=)\s+(?:actually\s+|really\s+|now\s+)?({alt})\b",
                         clause, re.I):
        same_clause = re.split(r"[,;.:()]", clause[:m.start()])[-1]  # only words in the assertion's own clause
        if not _PAST.search(same_clause):
            return m.group(1).lower()
    # "<value>, not <other>" / "<value> rather than <other>" / "<value> instead of <other>"
    m = re.search(rf"\b({alt})\b[\s,;.()]*(?:and\s+)?(?:not|rather than|instead of)\s+(?:the\s+)?({alt})\b", clause, re.I)
    if m and m.group(1).lower() != m.group(2).lower():
        return m.group(1).lower()
    return None


def check_corrections(extraction: dict, message: str) -> None:
    """Flag (never overwrite) values that contradict an unambiguous correction clause."""
    for clause in correction_clauses(message):
        wants = {}
        env = _resolve(clause, ("staging", "production"))
        if env:
            wants["environment"] = env
        if re.search(r"\b(permission|access level|role)\b", clause, re.I):
            perm = _resolve(clause, ("read", "write", "admin"))
            if perm:
                wants["permission"] = perm
        ids = list(dict.fromkeys(i.upper() for i in re.findall(r"\bIT-\d+\b", clause, re.I)))
        if len(ids) == 1 and re.search(r"\b(request|number|id|identifier|reference)\b", clause, re.I):
            wants["request_id"] = ids[0]
        for field, want in wants.items():
            got = extraction[field]
            if got is not None and str(got).lower() != want.lower():
                _add(extraction, f"correction-ignored: {field} is {got!r} but the message's correction says {want!r}")
        dates = list(dict.fromkeys(re.findall(r"\d{4}-\d{2}-\d{2}", clause)))
        if dates and not re.search(r"\b(submission|submitted|request date)\b", clause, re.I):
            have = [d for d in (extraction["start_date"], extraction["end_date"]) if d]
            if have and not any(d in dates for d in have):
                _add(extraction, f"correction-ignored: dates {have} do not include any date from the correction "
                                 f"({', '.join(dates)})")
            else:
                # the model dropped a corrected date: the correction names a date the extraction does not use while a
                # date field is null (e.g. it applied the new start but nulled the end, calling it "invalid")
                unused = [d for d in dates if d not in have]
                nulls = [f for f in ("start_date", "end_date") if extraction[f] is None]
                if unused and nulls and not _conflict_recorded(extraction, "date"):
                    _add(extraction, f"correction-ignored: the correction names {', '.join(unused)} but "
                                     f"{' and '.join(nulls)} is null")


HEDGE = re.compile(
    r"\b(maybe|perhaps|possibl\w*|probabl\w*|tentativ\w*|might|wonder\w*|unsure|uncertain|undecided|considering|"
    r"leaning|depends|i think|i guess|could (?:be|need|use|do|work|have|run)|"
    r"may (?:actually|need|be|want|prefer|use|work|have)|not (?:sure|certain|decided|clear)|"
    r"haven['’]t decided|thinking (?:of|about))\b", re.I)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_NEGATED = re.compile(r"(?:\bnot|\bno|\bnever|n['’]t|\bwithout|\bexcept|instead of|rather than)\s+(?:\w+\s+){0,2}$", re.I)
_PERM_CONTEXT = re.compile(r"\b(access|permissions?|rights|privileges?|level|role|instead|rather|upgrade\w*|"
                           r"elevat\w*)\b|read[- ]only", re.I)
FIELD_WORDS = {"environment": ("staging", "production"), "permission": ("read", "write", "admin")}


def _conflict_recorded(extraction: dict, *names: str) -> bool:
    """Did the model already write a `conflict:` issue about one of these fields?"""
    return any(i.strip().lower().startswith("conflict") and any(n in i.lower() for n in names)
               for i in extraction["issues"])


def _floated_words(sentence: str, field: str, current: str) -> list[str]:
    """Environment/permission words in a hedged sentence that differ from the extracted value and are really offered
    as an alternative (not negated, not a verb, not undone by an 'only <current value>' restriction)."""
    if re.search(rf"\b(?:only|just|solely|merely)\b(?:\W+\w+){{0,3}}?\W+{re.escape(current)}\b", sentence, re.I):
        return []  # "I only need staging access": the requester has ruled the other value out
    found = []
    for m in re.finditer(r"\b(" + "|".join(FIELD_WORDS[field]) + r")\b", sentence, re.I):
        word = m.group(1).lower()
        if word == current.lower() or word in found:
            continue
        if _NEGATED.search(re.split(r"[,;:()]", sentence[:m.start()])[-1]):
            continue  # "not write", "instead of production", "rather than write"
        if field == "permission" and word in ("read", "write"):
            # read/write are also ordinary verbs ("I might read a few files"): require permission context, or the
            # word standing alone as an answer ("Maybe write?")
            if not (_PERM_CONTEXT.search(sentence) or re.match(r"\s*(?:[?.!;,)]|$)", sentence[m.end():])):
                continue
        found.append(word)
    return found


def check_hedged_alternatives(extraction: dict, message: str) -> None:
    """Flag a hedged alternative that the extraction silently ignored (dates, environment, permission).

    Policy: contradictory statements without a correction stay unresolved. "Possibly until 2026-11-09 instead" next to
    "to 2026-11-04", or "production would be better" next to "in staging", is a contradiction even though it is
    tentative; a model that keeps the stated value has guessed - and for an environment or permission the guess can
    route a production/write request to the wrong reviewer. We flag (never rewrite) when ONE sentence has a hedge cue
    and a concrete alternative the extraction does not use: a date, or an environment/permission word that differs
    from the extracted (non-null) value. We skip it when the model already recorded a conflict about that field, when
    the sentence is part of a labelled correction, and for the look-alikes handled in `_floated_words`. A hedge that
    names no concrete alternative ("it might take longer") never triggers this. One issue per kind of field.
    """
    todo = set()
    if not _conflict_recorded(extraction, "date"):
        todo.add("date")
    for field in FIELD_WORDS:
        if extraction[field] is not None and not _conflict_recorded(extraction, field):
            todo.add(field)
    used = {extraction["request_date"], extraction["start_date"], extraction["end_date"]}
    corrections = [m.span() for m in CORRECTION.finditer(message)]
    for m in re.finditer(r"[^.!?;\n]+[.!?;]?", message):
        if not todo:
            return
        if any(m.start() < ce and cs < m.end() for cs, ce in corrections):
            continue  # a labelled correction is handled by check_corrections
        sentence = m.group(0)
        if not HEDGE.search(sentence):
            continue
        snippet = sentence.strip()[:120]
        if "date" in todo:
            floated = [d for d in dict.fromkeys(_ISO.findall(sentence)) if d not in used]
            if floated:
                todo.discard("date")
                _add(extraction, f"hedged-alternative: the message tentatively floats {', '.join(floated)} but the "
                                 f"extraction used start_date={extraction['start_date']}, "
                                 f"end_date={extraction['end_date']}: {snippet}")
        for field in FIELD_WORDS:
            if field in todo:
                words = _floated_words(sentence, field, extraction[field])
                if words:
                    todo.discard(field)
                    _add(extraction, f"hedged-alternative: the message tentatively floats {field} "
                                     f"{' / '.join(words)} but the extraction used {field}={extraction[field]}: "
                                     f"{snippet}")


def preserve_correction_quotes(extraction: dict, message: str) -> None:
    """Keep each explicit correction in supporting_quotes (verbatim) so reviewers see what replaced what."""
    for clause in correction_clauses(message):
        if not any(q and (q in clause or clause[:25] in q) for q in extraction["supporting_quotes"]):
            extraction["supporting_quotes"].append(clause)
            _add(extraction, "note: correction statement added to supporting_quotes by intake")


def finalize(extraction: dict, message: str, document_id: str) -> dict:
    """Apply all deterministic policy checks. Mutates and returns `extraction`."""
    stated = extraction["request_id"]
    if stated and not ids_match(stated, PurePath(document_id).stem):
        if not any(i.strip().lower().startswith("mismatch") for i in extraction["issues"]):
            _add(extraction, f"mismatch: the message states {stated} but the document is {PurePath(document_id).stem}")
    check_corrections(extraction, message)
    check_hedged_alternatives(extraction, message)
    preserve_correction_quotes(extraction, message)
    for f in SCALARS:  # last, so it sees every issue added above
        if extraction[f] is None and not _mentions(extraction["issues"], f):
            _add(extraction, f"missing: {f} is null and the extraction gave no explanation")
    return extraction
