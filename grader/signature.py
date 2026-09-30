"""Machine-checkable symptom signatures (incident-spec.md `symptom_signature`, T11).

A signature is a list of entries, all of which must hold on one trace:

    {kind: log, event: job_stuck, min_count: 3}
    {kind: client, outcome: timeout, tenant: B}

`kind` is the trace event kind. Every other key except `min_count` (default 1)
and `max_count` must equal the event's field of the same name; fields are looked
up at top level and then in nested dicts (fields, payload, detail, outcome...).
String values may use shell-style globs (`job_*`).

`signature.json` may be the list itself or {"signature": [...]}/{"symptom_signature": [...]}.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

from common import load_json

META_KEYS = {"kind", "min_count", "max_count", "note", "bug"}
_MISSING = object()


def load_signature(path: Path) -> list[dict]:
    data = load_json(path)
    if isinstance(data, dict):
        data = data.get("signature", data.get("symptom_signature", data.get("entries")))
    if not isinstance(data, list) or not all(isinstance(e, dict) and "kind" in e for e in data):
        raise ValueError(f"{path}: signature must be a list of {{kind: ..., ...}} entries")
    return data


def _lookup(ev: dict, key: str):
    if key in ev:
        return ev[key]
    for v in ev.values():
        if isinstance(v, dict):
            got = _lookup(v, key)
            if got is not _MISSING:
                return got
    return _MISSING


def _eq(want, got) -> bool:
    if got is _MISSING:
        return False
    if isinstance(want, str) and isinstance(got, str):
        return fnmatchcase(got, want)
    if isinstance(want, (int, float)) and isinstance(got, (int, float)) and not isinstance(want, bool):
        return float(want) == float(got)
    return want == got


def entry_count(entry: dict, events: list[dict]) -> int:
    conds = {k: v for k, v in entry.items() if k not in META_KEYS}
    n = 0
    for ev in events:
        if ev.get("kind") != entry["kind"]:
            continue
        if all(_eq(v, _lookup(ev, k)) for k, v in conds.items()):
            n += 1
    return n


def match(signature: list[dict], events: list[dict]) -> tuple[bool, list[str]]:
    """Return (all entries hold, per-entry report lines)."""
    ok, report = True, []
    for e in signature:
        n = entry_count(e, events)
        lo = int(e.get("min_count", 1))
        hi = e.get("max_count")
        good = n >= lo and (hi is None or n <= int(hi))
        ok &= good
        desc = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("min_count", "max_count"))
        report.append(f"{'ok ' if good else 'MISS'} {desc}: {n} (need >= {lo}"
                      + (f", <= {hi}" if hi is not None else "") + ")")
    return ok, report
