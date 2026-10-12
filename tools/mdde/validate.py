"""Formatting tokens of HOI4 texts and validation of translated values.

Tokens that must survive translation unchanged (as a multiset):

* colour codes ``§Y`` … ``§!``
* icons ``£pol_power`` / ``£GFX_x|1``
* variables ``$NAME$`` / ``$RIGHT|+=%1$``
* scripted expressions ``[ROOT.GetName]``
* escaped line breaks ``\\n``
* flags ``@GER``
"""
from __future__ import annotations

import re
from collections import Counter

TOKEN_RE = re.compile(r"§!|§[A-Za-z0-9]|£[\w|]+|\$[^$\s]+\$|\[[^\]\n]+\]|\\n|@\w+")
_LETTERS_RE = re.compile(r"[^\W\d_]{3}")


def tokens(text: str) -> Counter[str]:
    return Counter(TOKEN_RE.findall(text))


def strip_tokens(text: str) -> str:
    return TOKEN_RE.sub(" ", text)


def has_text(text: str) -> bool:
    """True if ``text`` still has >= 3 consecutive letters once tokens are removed."""
    return bool(_LETTERS_RE.search(strip_tokens(text)))


# Characters that start tokens; an extra stray one breaks formatting (a dropped
# one, e.g. "$21 billion" -> "21 Mrd. US-Dollar", is harmless).
_SPECIAL_CHARS = "§£$[]"
_TRAILING_BACKSLASH_RE = re.compile(r"(?<!\\)(?:\\\\)*\\$")
_WORD_RE = re.compile(r"[^\W\d_]+")
ENGLISH_STOPWORDS = frozenset(
    {"the", "and", "of", "our", "with", "will", "has", "have", "this", "that",
     "which", "is", "are", "to", "for"}
)
ENGLISH_MIN_SOURCE_WORDS = 6
ENGLISH_MIN_HITS = 3


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(strip_tokens(text).lower())


def looks_english(en: str, de: str) -> bool:
    """Heuristic for untranslated leftovers in ``de``.

    Only applies to sentences (source with >= 6 words); proper names such as
    "Bank of Canada" contribute a single stop word and do not trigger it.
    """
    if len(_words(en)) < ENGLISH_MIN_SOURCE_WORDS:
        return False
    hits = sum(1 for w in _words(de) if w in ENGLISH_STOPWORDS)
    return hits >= ENGLISH_MIN_HITS


def check(en: str, de: object) -> str | None:
    """Return ``None`` if ``de`` is a usable translation of ``en``, else a reason:

    ``empty``      missing, not a string or blank
    ``newline``    contains a real line break (HOI4 needs the ``\\n`` token)
    ``backslash``  ends with a lone backslash (would escape the closing quote)
    ``tokens``     formatting tokens or token characters differ from the source
    ``english``    still reads like English (see ``looks_english``)
    """
    if not isinstance(de, str) or not de.strip():
        return "empty"
    if "\n" in de or "\r" in de:
        return "newline"
    if _TRAILING_BACKSLASH_RE.search(de):
        return "backslash"
    if tokens(en) != tokens(de):
        return "tokens"
    if any(de.count(c) > en.count(c) for c in _SPECIAL_CHARS):
        return "tokens"
    if looks_english(en, de):
        return "english"
    return None
