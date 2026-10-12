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
from .cache import RejectLog, UnchangedLog, src_hash


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
    lines = cache_path.read_text(encoding="utf-8").splitlines() if cache_path.exists() else []
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
        shown = report.examples.setdefault(reason, [])
        if len(shown) < examples:
            shown.append((key, en, rec["de"]))
        moves.append((reason, rec, en))

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
