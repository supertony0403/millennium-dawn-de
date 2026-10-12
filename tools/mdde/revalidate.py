"""Re-check cached translations with the current validator (``revalidate``).

Every current cache entry (the newest line per key and English text) is
checked against the English text that Millennium Dawn ships *now*:

* valid                -> stays in the cache;
* identical to English -> ``cache/unchanged.jsonl``;
* invalid              -> ``cache/rejects.jsonl`` with the reason;
* English text changed -> stays (it is never used, a new run translates anew).

Without ``apply`` nothing is written. With ``apply`` the cache file is first
copied to ``translations.jsonl.bak-<UTC timestamp>`` and then replaced
atomically by the cleaned version (older duplicates and broken lines go too).

``repair_rejects`` (``revalidate --repair-rejects``) works offline on
``rejects.jsonl``: answers rejected for ``backslash`` or ``newline`` whose only
fault is the model's double escaping (``\\\\n``, ``\\\\"``) or a real line break
where the English text has a literal ``\\n`` are repaired and, with ``apply``,
appended to the cache (after a backup). The reject log stays as it is - a
cached translation always wins over a reject.
"""
from __future__ import annotations

import json
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import locfile, scope, validate
from .cache import RejectLog, TranslationCache, UnchangedLog, src_hash


@dataclass
class RevalidateReport:
    checked: int = 0  # current entries whose English text still exists
    valid: int = 0
    stale: int = 0  # English text changed since the translation
    superseded: int = 0  # older lines of the same key + English text
    broken: int = 0  # unreadable lines
    by_reason: Counter = field(default_factory=Counter)
    examples: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    backup: Path | None = None


def english_index(md_root: Path) -> dict[str, dict[str, str]]:
    """``{key: {sha1(english): english}}`` over every English file of Millennium Dawn."""
    index: dict[str, dict[str, str]] = defaultdict(dict)
    for pair in scope.discover(md_root):
        if pair.en_path is None:
            continue
        for key, en in locfile.parse(pair.en_path).values().items():
            index[key][src_hash(en)] = en
    return index


def _record(line: str) -> dict | None:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(rec, dict) and all(isinstance(rec.get(f), str) for f in ("key", "src_sha1", "de")):
        return rec
    return None


def spread(items: list, n: int) -> list:
    """``n`` items evenly spread over ``items`` (first and last included)."""
    if n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    if n == 1:
        return [items[0]]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def _backup_path(path: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
    return candidate


def revalidate(md_root: Path, cache_path: Path, rejects: RejectLog, unchanged: UnchangedLog, *,
               apply: bool = False, examples: int = 3, now: datetime | None = None) -> RevalidateReport:
    report = RevalidateReport()
    cache_path = Path(cache_path)
    # split at "\n" only: splitlines() would also split at U+2028 inside a JSON string
    lines = cache_path.read_text(encoding="utf-8").split("\n") if cache_path.exists() else []
    if lines and lines[-1] == "":
        lines.pop()
    records: list[dict | None] = []
    latest: dict[tuple[str, str], int] = {}
    for i, line in enumerate(lines):
        rec = _record(line) if line.strip() else None
        records.append(rec)
        if rec is None:
            report.broken += bool(line.strip())
            continue
        ident = (rec["key"], rec["src_sha1"])
        report.superseded += ident in latest
        latest[ident] = i

    index = english_index(md_root)
    keep: list[int] = []
    moves: list[tuple[str, dict, str]] = []
    found: dict[str, list[tuple[str, str, str]]] = {}
    for (key, sha), i in latest.items():
        rec = records[i]
        assert rec is not None
        en = index.get(key, {}).get(sha)
        if en is None:
            report.stale += 1
            keep.append(i)
            continue
        report.checked += 1
        reason = validate.check(en, rec["de"])
        if reason is None:
            report.valid += 1
            keep.append(i)
            continue
        report.by_reason[reason] += 1
        found.setdefault(reason, []).append((key, en, rec["de"]))
        moves.append((reason, rec, en))
    report.examples = {reason: spread(items, examples) for reason, items in found.items()}

    if apply and cache_path.exists():
        report.backup = _backup_path(cache_path, now or datetime.now(timezone.utc))
        shutil.copy2(cache_path, report.backup)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text("".join(lines[i] + "\n" for i in sorted(keep)), encoding="utf-8")
        os.replace(tmp, cache_path)
        for reason, rec, en in moves:
            if reason == "unchanged":
                unchanged.add(rec["key"], en, rec.get("model"))
            else:
                rejects.add(rec["key"], en, reason, model=rec.get("model"), de=rec["de"])
    return report


REPAIRABLE_REASONS = ("backslash", "newline")


@dataclass
class RepairReport:
    candidates: int = 0  # newest reject per key + English text with a reason of REPAIRABLE_REASONS
    repaired: int = 0  # valid after the repair
    candidates_by_reason: Counter = field(default_factory=Counter)
    repaired_by_reason: Counter = field(default_factory=Counter)
    still_invalid: Counter = field(default_factory=Counter)
    without_answer: int = 0
    already_cached: int = 0
    broken: int = 0  # unreadable lines or records
    examples: list[tuple[str, str, str]] = field(default_factory=list)  # key, en, repaired de
    backup: Path | None = None


def _read_lines(path: Path) -> list[str]:
    """Lines split at ``\\n`` only (``splitlines`` would also split at U+2028 inside JSON)."""
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def repair_rejects(rejects_path: Path, cache_path: Path, *, apply: bool = False,
                   now: datetime | None = None) -> RepairReport:
    report = RepairReport()
    latest: dict[tuple[str, str], dict] = {}
    for line in _read_lines(Path(rejects_path)):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            report.broken += 1
            continue
        if not (isinstance(rec, dict) and isinstance(rec.get("key"), str) and isinstance(rec.get("en"), str)
                and rec.get("src_sha1") == src_hash(rec["en"])):
            report.broken += 1
            continue
        latest[(rec["key"], rec["src_sha1"])] = rec
    cache = TranslationCache(cache_path)
    fixes: list[dict] = []
    for rec in latest.values():
        reason = rec.get("reason")
        if reason not in REPAIRABLE_REASONS:
            continue
        report.candidates += 1
        report.candidates_by_reason[reason] += 1
        key, en, de = rec["key"], rec["en"], rec.get("de")
        if cache.get(key, en) is not None:
            report.already_cached += 1
            continue
        if not isinstance(de, str):
            report.without_answer += 1
            continue
        fixed = validate.repair_newlines(en, validate.repair_escapes(en, de))
        problem = validate.check(en, fixed)
        if problem is not None:
            report.still_invalid[problem] += 1
            continue
        report.repaired += 1
        report.repaired_by_reason[reason] += 1
        fixes.append({**rec, "de": fixed})
    report.examples = [(r["key"], r["en"], r["de"]) for r in fixes]
    if apply and fixes:
        cache_path = Path(cache_path)
        if cache_path.exists():
            report.backup = _backup_path(cache_path, now or datetime.now(timezone.utc))
            shutil.copy2(cache_path, report.backup)
        for rec in fixes:
            cache.put(rec["key"], rec["en"], rec["de"], rec.get("model") or "")
    return report
