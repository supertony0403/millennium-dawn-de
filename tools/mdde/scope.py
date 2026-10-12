"""Find the English texts that are still "open" (untranslated) per scope.

A text is open when the English text contains real words (or is a short English
phrase such as "So be it", see ``validate.short_english``) and its German value
in Millennium Dawn is missing, equal to the English one (ignoring case and
whitespace) or an old English copy that no longer matches the current English
text exactly (see ``validate.stale_english``).

Scopes:
    germany  everything except foreign focus trees (``MD_focus_<TAG>``), pure
             name lists and keys that start with a foreign country tag
             (``RUS_x``, ``USA.x``, ``FRA``); country-name files are exempt
             from the key rule because country names should be translated.
    full     everything except pure name lists.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import locfile
from . import validate

SCOPES = ("germany", "full")
OWN_TAG = "GER"

NAME_LISTS = frozenset(
    {"victory_points", "state_names", "MD_terrain_cities", "strategic_region_names", "supply_area_names"}
)
# Files whose keys are country tags on purpose. The spec names "countries" and
# "countries_cosmetic"; Millennium Dawn ships the cosmetic names as
# MD_countries_cosmetic (+ a replace/ variant), so those count as well.
COUNTRY_NAME_FILES = frozenset(
    {
        "countries",
        "countries_cosmetic",
        "MD_countries_cosmetic",
        "replaced_from_vanilla_countries_cosmetic",
    }
)

_TAG_LINE_RE = re.compile(r"^\s*([A-Z][A-Z0-9]{2})\s*=")
_FOCUS_FILE_RE = re.compile(r"^MD_focus_([A-Z][A-Z0-9]{2})")
_KEY_TAG_RE = re.compile(r"^([A-Z][A-Z0-9]{2})(?:[_.]|$)")
# "_l_english.yml" plus the "_I_english.yml" typo found in MD 2.0.2 (MON focus)
_LOC_NAME_RE = re.compile(r"^(?P<base>.+?)_[lI]_(?P<lang>english|german)\.yml$")


@dataclass(frozen=True)
class LocPair:
    """An English file and its German counterpart, matched by base name."""

    base: str
    en_path: Path | None
    de_path: Path | None
    en_rel: str | None  # relative to localisation/english, POSIX style
    de_rel: str | None  # relative to localisation/german, POSIX style


@dataclass(frozen=True)
class Item:
    file_rel_en: str
    base: str
    key: str
    en: str
    de_current: str | None
    de_path: Path | None


def load_country_tags(md_root: Path) -> frozenset[str]:
    tags: set[str] = set()
    for path in sorted((md_root / "common" / "country_tags").glob("*.txt")):
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            m = _TAG_LINE_RE.match(line.split("#", 1)[0])
            if m:
                tags.add(m.group(1))
    return frozenset(tags)


def loc_base(filename: str) -> tuple[str, str] | None:
    """``"X_l_english.yml" -> ("X", "english")``; ``None`` for other files."""
    m = _LOC_NAME_RE.match(filename)
    return (m.group("base"), m.group("lang")) if m else None


def _scan(lang_dir: Path, lang: str) -> dict[str, tuple[Path, str]]:
    found: dict[str, tuple[Path, str]] = {}
    for path in sorted(lang_dir.rglob("*.yml")):
        parsed = loc_base(path.name)
        if parsed and parsed[1] == lang:
            found.setdefault(parsed[0], (path, path.relative_to(lang_dir).as_posix()))
    return found


def discover(md_root: Path) -> list[LocPair]:
    loc = md_root / "localisation"
    en = _scan(loc / "english", "english")
    de = _scan(loc / "german", "german")
    pairs = []
    for base in sorted(set(en) | set(de)):
        en_path, en_rel = en.get(base, (None, None))
        de_path, de_rel = de.get(base, (None, None))
        pairs.append(LocPair(base, en_path, de_path, en_rel, de_rel))
    return pairs


def is_open(en: str, de_current: str | None) -> bool:
    # "So be it" has no word of three letters but is a short English phrase
    if not (validate.has_text(en) or validate.short_english(en)):
        return False
    if de_current is None or validate.normalized(de_current) == validate.normalized(en):
        return True
    return validate.stale_english(en, de_current)


def file_in_scope(base: str, scope: str, tags: Iterable[str]) -> bool:
    if base in NAME_LISTS:
        return False
    if scope == "full":
        return True
    m = _FOCUS_FILE_RE.match(base)
    return not (m and m.group(1) in tags and m.group(1) != OWN_TAG)


def key_in_scope(key: str, base: str, scope: str, tags: Iterable[str]) -> bool:
    if scope == "full" or base in COUNTRY_NAME_FILES:
        return True
    m = _KEY_TAG_RE.match(key)
    return not (m and m.group(1) in tags and m.group(1) != OWN_TAG)


def german_index(pairs: Iterable[LocPair]) -> tuple[dict[Path, dict[str, str]], dict[str, tuple[str, Path]]]:
    """Per-file German values and a global ``key -> (value, file)`` index."""
    per_file: dict[Path, dict[str, str]] = {}
    global_index: dict[str, tuple[str, Path]] = {}
    for pair in pairs:
        if pair.de_path is None:
            continue
        values = locfile.parse(pair.de_path).values()
        per_file[pair.de_path] = values
        for key, val in values.items():
            global_index.setdefault(key, (val, pair.de_path))
    return per_file, global_index


def collect(md_root: Path, scope: str, tags: Iterable[str] | None = None) -> list[Item]:
    if scope not in SCOPES:
        raise ValueError(f"unbekannter Scope {scope!r}, erlaubt: {', '.join(SCOPES)}")
    tags = frozenset(load_country_tags(md_root) if tags is None else tags)
    pairs = discover(md_root)
    per_file, global_de = german_index(pairs)
    items: list[Item] = []
    for pair in pairs:
        if pair.en_path is None or not file_in_scope(pair.base, scope, tags):
            continue
        own = per_file.get(pair.de_path, {}) if pair.de_path else {}
        for key, en in locfile.parse(pair.en_path).values().items():
            if not key_in_scope(key, pair.base, scope, tags):
                continue
            if key in own:
                de_current, de_path = own[key], pair.de_path
            elif key in global_de:
                de_current, de_path = global_de[key]
            else:
                de_current, de_path = None, pair.de_path
            if is_open(en, de_current):
                items.append(Item(pair.en_rel or "", pair.base, key, en, de_current, de_path))
    return items
