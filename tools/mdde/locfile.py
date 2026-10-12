"""Read and write HOI4 localisation files.

Format (one entry per line)::

    l_german:
     # comment
     some_key:0 "Wert mit \\"Zitat\\" und §YFarbe§!" # optional trailing comment

* Files are UTF-8 *with* BOM; reading tolerates a missing BOM.
* The version number after the colon is optional (``key: "x"``).
* The value runs greedily up to the **last** ``"`` of the line, so values may
  contain ``#`` and (rarely, in the wild) unescaped quotes.
* Everything that is not an entry (header, comments, blank lines, junk) is kept
  verbatim so that ``parse_text(t).render() == t`` for LF files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

ENTRY_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<key>[^\s:#"]+):(?P<ver>\d*)(?P<sep>[ \t]*)"(?P<val>.*)"(?P<trail>[^"]*)$'
)
HEADER_RE = re.compile(r"^(?P<indent>[ \t]*)l_(?P<lang>[a-z_]+):(?P<rest>[ \t]*(?:#.*)?)$")
_BARE_QUOTE_RE = re.compile(r'(?<!\\)"')


@dataclass
class Entry:
    """One ``key:ver "val"`` line; ``idx`` is its index in ``LocFile.lines``."""

    idx: int
    key: str
    ver: str
    val: str
    indent: str = " "
    sep: str = " "
    trail: str = ""

    def line(self) -> str:
        return f'{self.indent}{self.key}:{self.ver}{self.sep}"{self.val}"{self.trail}'


@dataclass
class LocFile:
    lines: list[str]
    entries: list[Entry] = field(default_factory=list)
    lang: str | None = None
    header_idx: int | None = None
    unparsed: list[int] = field(default_factory=list)

    def values(self) -> dict[str, str]:
        """``{key: value}``; the first occurrence of a duplicate key wins."""
        out: dict[str, str] = {}
        for e in self.entries:
            out.setdefault(e.key, e.val)
        return out

    def set_value(self, entry: Entry, val: str) -> None:
        """Replace the value of ``entry`` (already escaped) in place."""
        entry.val = val
        self.lines[entry.idx] = entry.line()

    def set_language(self, lang: str) -> None:
        if self.header_idx is None:
            raise ValueError("Datei hat keine Kopfzeile l_<sprache>:")
        m = HEADER_RE.match(self.lines[self.header_idx])
        assert m is not None
        self.lines[self.header_idx] = f"{m.group('indent')}l_{lang}:{m.group('rest')}"
        self.lang = lang

    def append(self, key: str, val: str, ver: str = "0") -> Entry:
        """Append ``key:ver "val"`` at the end, keeping a final newline if present."""
        idx = len(self.lines)
        if self.lines and self.lines[-1] == "":
            idx -= 1
        e = Entry(idx=idx, key=key, ver=ver, val=val)
        self.lines.insert(idx, e.line())
        self.entries.append(e)
        return e

    def render(self) -> str:
        return render(self.lines)


def parse_text(text: str) -> LocFile:
    if text.startswith("﻿"):
        text = text[1:]
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    lf = LocFile(lines=lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if lf.header_idx is None:
            h = HEADER_RE.match(line)
            if h:
                lf.header_idx = idx
                lf.lang = h.group("lang")
                continue
        m = ENTRY_RE.match(line)
        if m:
            lf.entries.append(
                Entry(
                    idx=idx,
                    key=m.group("key"),
                    ver=m.group("ver"),
                    val=m.group("val"),
                    indent=m.group("indent"),
                    sep=m.group("sep"),
                    trail=m.group("trail"),
                )
            )
        else:
            lf.unparsed.append(idx)
    return lf


def parse(path: str | Path) -> LocFile:
    return parse_text(Path(path).read_text(encoding="utf-8-sig"))


def render(lines: list[str]) -> str:
    return "\n".join(lines)


def write(path: str | Path, text: str) -> None:
    """Write ``text`` as UTF-8 with exactly one BOM and LF line endings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if text.startswith("﻿"):
        text = text[1:]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(text)


def escape_value(value: str) -> str:
    """Make ``value`` safe for ``"..."``: bare quotes become ``\\"``.

    Real line breaks and a trailing lone backslash (which would escape the
    closing quote) cannot be represented and raise ``ValueError``.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("Wert enthaelt einen echten Zeilenumbruch")
    trailing = len(value) - len(value.rstrip("\\"))
    if trailing % 2 == 1:
        raise ValueError("Wert endet mit einem einzelnen Backslash")
    return _BARE_QUOTE_RE.sub(r'\\"', value)
