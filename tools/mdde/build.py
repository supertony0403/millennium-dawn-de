"""Generate the submod's localisation files and check them.

``build`` mirrors **every** German file of Millennium Dawn under exactly the
same relative path (a file with the same path in a later loaded mod replaces
the original completely, so no key is defined twice). Each file is copied line
by line; only values that are still English ("open") are replaced by the
cached translation. English files without a German counterpart become new
``<base>_l_german.yml`` files, English keys missing from every German file are
appended to the matching file. Every such key is assigned to exactly one
output file beforehand (the first English file that defines it), so no key
ends up in two files. An untranslated English fallback is left out when HOI4's
own German localisation (``vanilla_keys``) already has the key - the English
text would otherwise cover the vanilla German one. Without any cache entry
the output is byte-identical to Millennium Dawn's German files (plus appended
keys).

``check`` parses the output, verifies BOM/header and the tokens of every value
that differs from Millennium Dawn, reports keys defined in more than one
output file (an error, unless Millennium Dawn's German files already define
the key in those files - then it is listed in ``md_duplicates``), and
compares the Millennium Dawn version and file fingerprint with
``build_info.json``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import locfile, scope, validate
from .cache import TranslationCache

BOM = b"\xef\xbb\xbf"
_BARE_QUOTE_RE = re.compile(r'(?<!\\)"')
_TRAILING_BACKSLASH_RE = re.compile(r"(?<!\\)(?:\\\\)*\\$")


@dataclass
class BuildReport:
    mirrored: int = 0
    new_files: int = 0
    replaced: int = 0
    appended: int = 0
    english_fallback: int = 0
    vanilla_kept: int = 0  # untranslated keys left to HOI4's own German text
    skipped_empty: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    @property
    def files(self) -> int:
        return self.mirrored + self.new_files


@dataclass
class CheckReport:
    files: int = 0
    checked_values: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # build is stale (exit 2)
    md_duplicates: list[str] = field(default_factory=list)  # inherited from MD, exit code unaffected

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 1
        return 2 if self.warnings else 0


# -- shared helpers -------------------------------------------------------------
def md_version(md_root: Path) -> str | None:
    try:
        text = (md_root / "descriptor.mod").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    m = re.search(r'^\s*version\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else None


def md_fingerprint(md_root: Path) -> str:
    """SHA-1 over path + content hash of every English and German loc file."""
    loc = md_root / "localisation"
    digest = hashlib.sha1()
    for lang in ("english", "german"):
        base = loc / lang
        for path in sorted(base.rglob("*.yml")):
            digest.update(f"{lang}/{path.relative_to(base).as_posix()}\0".encode())
            digest.update(hashlib.sha1(path.read_bytes()).digest())
    return digest.hexdigest()


def vanilla_keys(de_dir: Path) -> frozenset[str]:
    """Keys of HOI4's own German localisation (recursive); empty if missing."""
    keys: set[str] = set()
    if Path(de_dir).is_dir():
        for path in sorted(Path(de_dir).rglob("*.yml")):
            keys.update(e.key for e in locfile.parse(path).entries)
    return frozenset(keys)


def _new_file_rel(pair: scope.LocPair) -> str:
    parent = Path(pair.en_rel or "").parent
    return (parent / f"{pair.base}_l_german.yml").as_posix()


class _Sources:
    """Parsed Millennium Dawn files plus global key indexes."""

    def __init__(self, md_root: Path) -> None:
        self.pairs = scope.discover(md_root)
        self.en: dict[str, dict[str, str]] = {}
        self.de: dict[str, dict[str, str]] = {}
        self.en_global: dict[str, str] = {}
        self.de_keys: set[str] = set()
        for pair in self.pairs:
            if pair.en_path is not None:
                self.en[pair.base] = locfile.parse(pair.en_path).values()
                for key, val in self.en[pair.base].items():
                    self.en_global.setdefault(key, val)
            if pair.de_path is not None:
                self.de[pair.base] = locfile.parse(pair.de_path).values()
                self.de_keys.update(self.de[pair.base])

    def english_for(self, base: str, key: str) -> str | None:
        own = self.en.get(base, {})
        return own[key] if key in own else self.en_global.get(key)


def _clear(out_dir: Path) -> None:
    if not out_dir.exists():
        return
    for path in out_dir.rglob("*.yml"):
        path.unlink()
    for directory in sorted((p for p in out_dir.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()


# -- build -----------------------------------------------------------------------
def build(md_root: Path, out_dir: Path, cache: TranslationCache, *, info_path: Path,
          vanilla_keys: Collection[str] = frozenset(),
          now: Callable[[], datetime] | None = None) -> BuildReport:
    now = now or (lambda: datetime.now(timezone.utc))
    src = _Sources(md_root)
    report = BuildReport()
    _clear(out_dir)
    # every English key missing from all German files goes to exactly one output file
    owner: dict[str, str] = {}
    for pair in src.pairs:
        for key in src.en.get(pair.base, {}):
            if key not in src.de_keys:
                owner.setdefault(key, pair.base)

    def translated(key: str, en: str, current: str) -> str | None:
        """Escaped translation for an open value, else ``None``."""
        if not scope.is_open(en, current):
            return None
        de = cache.get(key, en)
        if de is None:
            report.english_fallback += 1
            return None
        return locfile.escape_value(de)

    def cached(key: str, en: str) -> str | None:
        de = cache.get(key, en) if scope.is_open(en, en) else None
        return None if de is None else locfile.escape_value(de)

    for pair in src.pairs:
        if pair.de_path is not None:
            lf = locfile.parse(pair.de_path)
            for entry in lf.entries:
                en = src.english_for(pair.base, entry.key)
                if en is None:
                    continue
                value = translated(entry.key, en, entry.val)
                if value is not None:
                    lf.set_value(entry, value)
                    report.replaced += 1
            for key, en in src.en.get(pair.base, {}).items():
                if owner.get(key) != pair.base:
                    continue
                value = cached(key, en)
                if value is None and key in vanilla_keys:
                    report.vanilla_kept += 1
                    continue
                if value is None and scope.is_open(en, en):
                    report.english_fallback += 1
                lf.append(key, value if value is not None else en)
                report.appended += 1
            rel = pair.de_rel or ""
            report.mirrored += 1
        elif pair.en_path is not None:
            lf = locfile.parse(pair.en_path)
            drop = set()
            for entry in lf.entries:
                if owner.get(entry.key) != pair.base:
                    drop.add(entry.idx)
                elif entry.key in vanilla_keys and cached(entry.key, entry.val) is None:
                    drop.add(entry.idx)
                    report.vanilla_kept += 1
            if len(drop) == len(lf.entries):
                report.skipped_empty.append(pair.base)
                continue
            if drop:
                lf = locfile.parse_text(
                    locfile.render([ln for i, ln in enumerate(lf.lines) if i not in drop])
                )
            lf.set_language("german")
            for entry in lf.entries:
                value = translated(entry.key, entry.val, entry.val)
                if value is not None:
                    lf.set_value(entry, value)
                    report.replaced += 1
            rel = _new_file_rel(pair)
            report.new_files += 1
        else:  # pragma: no cover - discover() never yields empty pairs
            continue
        locfile.write(out_dir / rel, lf.render())
        report.outputs.append(rel)

    info = {
        "md_version": md_version(md_root),
        "md_fingerprint": md_fingerprint(md_root),
        "built_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": report.files,
        "mirrored": report.mirrored,
        "new_files": report.new_files,
        "replaced": report.replaced,
        "appended": report.appended,
        "english_fallback": report.english_fallback,
        "vanilla_kept": report.vanilla_kept,
        "cache_entries": len(cache),
        "skipped_empty": report.skipped_empty,
        "outputs": sorted(report.outputs),
    }
    Path(info_path).write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report


# -- check -----------------------------------------------------------------------
def _check_value(rel: str, key: str, en: str | None, val: str, report: CheckReport) -> None:
    report.checked_values += 1
    where = f"{rel}: {key}"
    if _BARE_QUOTE_RE.search(val):
        report.errors.append(f"{where}: nicht escapte Anführungszeichen")
    if _TRAILING_BACKSLASH_RE.search(val):
        report.errors.append(f"{where}: Wert endet mit Backslash")
    if en is not None and validate.tokens(en) != validate.tokens(val):
        report.errors.append(f"{where}: Token weichen vom Englischen ab")


def check(md_root: Path, out_dir: Path, *, info_path: Path) -> CheckReport:
    report = CheckReport()
    src = _Sources(md_root)
    try:
        info = json.loads(Path(info_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = None
        report.errors.append(f"{info_path} fehlt oder ist kaputt - erst 'build' ausführen")

    origin: dict[str, tuple[scope.LocPair, dict[str, str] | None]] = {}
    for pair in src.pairs:
        if pair.de_path is not None:
            origin[pair.de_rel or ""] = (pair, src.de[pair.base])
        elif pair.en_path is not None:
            origin[_new_file_rel(pair)] = (pair, None)
    expected = {rel for rel, (pair, de) in origin.items() if de is not None}
    if info:
        expected |= set(info.get("outputs", []))

    present = {p.relative_to(out_dir).as_posix(): p for p in out_dir.rglob("*.yml")} if out_dir.exists() else {}
    for rel in sorted(expected - present.keys()):
        report.errors.append(f"{rel}: Ausgabedatei fehlt")
    md_files: dict[str, set[str]] = defaultdict(set)  # key -> Millennium Dawn German files defining it
    for pair in src.pairs:
        if pair.de_path is not None:
            for key in src.de[pair.base]:
                md_files[key].add(pair.de_rel or "")
    out_files: dict[str, list[str]] = defaultdict(list)  # key -> output files defining it

    for rel, path in sorted(present.items()):
        report.files += 1
        raw = path.read_bytes()
        if not raw.startswith(BOM) or raw.startswith(BOM + BOM):
            report.errors.append(f"{rel}: kein (oder doppeltes) UTF-8-BOM")
        try:
            lf = locfile.parse_text(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            report.errors.append(f"{rel}: kein gültiges UTF-8")
            continue
        if lf.lang != "german":
            report.errors.append(f"{rel}: Kopfzeile l_german: fehlt")
        for idx in lf.unparsed[:5]:
            report.errors.append(f"{rel}: Zeile {idx + 1} nicht lesbar: {lf.lines[idx][:80]!r}")
        pair, md_values = origin.get(rel, (None, None))
        base = pair.base if pair else None
        for key in dict.fromkeys(e.key for e in lf.entries):
            out_files[key].append(rel)
        for entry in lf.entries:
            if md_values is not None and md_values.get(entry.key) == entry.val:
                continue  # untouched Millennium Dawn value
            en = src.english_for(base, entry.key) if base else src.en_global.get(entry.key)
            if en is not None and entry.val == en:
                continue  # English fallback, identical to the source
            _check_value(rel, entry.key, en, entry.val, report)

    inherited = []
    for key, rels in sorted(out_files.items()):
        if len(rels) < 2:
            continue
        if set(rels) <= md_files.get(key, set()):
            inherited.append(f"{key}: {', '.join(rels)}")
        else:
            report.errors.append(f"{key}: in {len(rels)} Ausgabedateien definiert ({', '.join(rels)})")
    if inherited:
        report.md_duplicates.append(
            f"{len(inherited)} Schluessel stehen schon bei Millennium Dawn in mehreren deutschen Dateien "
            f"(unveraendert uebernommen), z. B. {'; '.join(inherited[:3])}"
        )
        report.md_duplicates.extend(inherited)

    if info:
        version = md_version(md_root)
        if info.get("md_version") != version:
            report.warnings.append(
                f"Millennium Dawn ist jetzt {version}, gebaut wurde gegen {info.get('md_version')} - "
                "status/translate/build erneut ausführen"
            )
        elif info.get("md_fingerprint") != md_fingerprint(md_root):
            report.warnings.append(
                "Lokalisierung von Millennium Dawn hat sich seit dem letzten Build geändert - "
                "status/translate/build erneut ausführen"
            )
    return report
