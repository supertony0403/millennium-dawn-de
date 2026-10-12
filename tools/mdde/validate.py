"""Formatting tokens of HOI4 texts and validation of translated values.

Tokens that must survive translation unchanged (as a multiset):

* colour codes ``§Y`` … ``§!``
* icons ``£pol_power`` / ``£GFX_x|1``
* variables ``$NAME$`` / ``$RIGHT|+=%1$``
* scripted expressions ``[ROOT.GetName]``
* escaped line breaks ``\\n``
* flags ``@GER``

Besides the tokens, ``check`` looks for typical model failures (truncation,
chatter, untranslated English, broken colour structure, stray backslashes).
The thresholds were calibrated against Millennium Dawn's own human German
translations (39,362 pairs), see the review notes in the commit history.
"""
from __future__ import annotations

import re
from collections import Counter

TOKEN_RE = re.compile(r"§!|§[A-Za-z0-9]|£[\w|]+|\$[^$\s]+\$|\[[^\]\n]+\]|\\n|@\w+")
_LETTERS_RE = re.compile(r"[^\W\d_]{3}")
_LETTER_RE = re.compile(r"[^\W\d_]")


def tokens(text: str) -> Counter[str]:
    return Counter(TOKEN_RE.findall(text))


def strip_tokens(text: str) -> str:
    return TOKEN_RE.sub(" ", text)


def has_text(text: str) -> bool:
    """True if ``text`` still has >= 3 consecutive letters once tokens are removed."""
    return bool(_LETTERS_RE.search(strip_tokens(text)))


def normalized(text: str) -> str:
    """Whitespace-collapsed, case-folded text (for "unchanged" comparisons)."""
    return " ".join(text.split()).casefold()


# Characters that start tokens; an extra stray one breaks formatting (a dropped
# one, e.g. "$21 billion" -> "21 Mrd. US-Dollar", is harmless).
_SPECIAL_CHARS = "§£$[]"
_TRAILING_BACKSLASH_RE = re.compile(r"(?<!\\)(?:\\\\)*\\$")
_ESCAPE_RE = re.compile(r"\\(.)", re.S)
_ALLOWED_ESCAPES = frozenset('n"')
_COLOUR_RE = re.compile(r"§!|§[A-Za-z0-9]")
_WORD_RE = re.compile(r"[^\W\d_]+")
_IDENTIFIER_RE = re.compile(r"\w*_\w*")

# Lower-cased stop words. Words that are also common in German ("in", "an",
# "will", "was", "so", "all", "not" = "Not") are left out on purpose.
ENGLISH_STOPWORDS = frozenset(
    {"the", "and", "of", "our", "with", "has", "have", "this", "that", "which", "is",
     "are", "to", "for", "a", "by", "be", "we", "it", "its", "their", "from", "as",
     "at", "were", "been", "they", "them", "you", "your", "can", "must", "should",
     "would", "more"}
)
GERMAN_STOPWORDS = frozenset(
    {"der", "die", "das", "und", "ist", "sind", "mit", "von", "zu", "den", "dem", "des",
     "ein", "eine", "einen", "einer", "eines", "nicht", "auf", "für", "im", "wir",
     "unser", "unsere", "unserer", "sich", "es", "wird", "werden", "hat", "haben",
     "auch", "bei", "aus", "nach", "über", "oder", "als", "wie", "noch", "nur", "mehr",
     "sie", "er", "ihre", "ihr", "zum", "zur", "vom", "durch", "gegen", "unter", "dass",
     "wenn", "kann", "können", "muss", "müssen", "soll", "sollte"}
)
ENGLISH_MIN_SOURCE_WORDS = 6
ENGLISH_MIN_HITS = 3
SHORT_ENGLISH_MIN_WORDS = 3
SHORT_ENGLISH_MIN_HITS = 2
SHORT_ENGLISH_MIN_SHARE = 0.25

LENGTH_MIN_SOURCE = 40
LENGTH_RATIO_MIN = 0.35
LENGTH_RATIO_MAX = 3.0

_CHATTER_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"here\s+(?:is|are)\b"
    r"|(?:hier\s+(?:ist|sind)|nachfolgend|im\s+folgenden)\b[^:\n]{0,40}?übersetzung"
    r"|(?:die\s+)?(?:deutsche\s+)?übersetzung(?:en)?\s*:"
    r"|(?:german\s+)?translations?\s*:"
    r")",
    re.IGNORECASE,
)
_CHATTER_LABEL_RE = re.compile(r"\b(?:übersetzung|translation)\s*:", re.IGNORECASE)
_OPEN_QUOTES = "\"„“”«»‚‘'"
_CLOSE_QUOTES = "\"“”«»‘’'"
_ALL_QUOTES = frozenset(_OPEN_QUOTES + _CLOSE_QUOTES)


def words(text: str, *, lower: bool = True) -> list[str]:
    """Words of ``text`` without tokens and ``snake_case`` identifiers."""
    found = _WORD_RE.findall(_IDENTIFIER_RE.sub(" ", strip_tokens(text)))
    return [w.lower() for w in found] if lower else found


def _stopword_hits(text: str) -> tuple[int, int]:
    """English and German stop words in ``text``; all-caps words (acronyms such
    as "CAN", "IT", "AT", "A") are not counted."""
    found = [w.lower() for w in words(text, lower=False) if not w.isupper()]
    return (sum(w in ENGLISH_STOPWORDS for w in found), sum(w in GERMAN_STOPWORDS for w in found))


def looks_english(en: str, de: str) -> bool:
    """Heuristic for untranslated leftovers in ``de``.

    * sentences (source >= 6 words): >= 3 English stop words in ``de`` and at
      least as many as German ones (a German text may quote English);
    * short texts (``de`` >= 3 words): >= 2 English stop words making up
      >= 25 % of the words and not a single German stop word.

    Proper names such as "Bank of Canada" contribute one stop word and pass.
    """
    found = words(de)
    en_hits, de_hits = _stopword_hits(de)
    if len(words(en)) >= ENGLISH_MIN_SOURCE_WORDS and en_hits >= ENGLISH_MIN_HITS and en_hits >= de_hits:
        return True
    return (
        len(found) >= SHORT_ENGLISH_MIN_WORDS
        and de_hits == 0
        and en_hits >= SHORT_ENGLISH_MIN_HITS
        and en_hits / len(found) >= SHORT_ENGLISH_MIN_SHARE
    )


def stale_english(en: str, de: str) -> bool:
    """True if a German value of Millennium Dawn is (an old copy of) English text.

    Either >= 3 English and <= 1 German stop words, or more than 60 % of its
    lower-case words occur in the English text and it has no German stop word
    (capitalised words are skipped: names and German nouns would cause false
    alarms such as "Osama bin Laden getötet").
    """
    en_hits, de_hits = _stopword_hits(de)
    if en_hits >= 3 and de_hits <= 1:
        return True
    lower = [w for w in words(de, lower=False) if w.islower()]
    if len(lower) < 3 or de_hits:
        return False
    source = set(words(en))
    return sum(w in source for w in lower) / len(lower) > 0.6


def is_unchanged(en: str, de: str) -> bool:
    """``de`` equals ``en`` (ignoring case and whitespace) and ``en`` is real text."""
    return has_text(en) and normalized(de) == normalized(en)


def plain_length(text: str) -> int:
    """Length of ``text`` without tokens and with collapsed whitespace."""
    return len(" ".join(strip_tokens(text).split()))


_plain_length = plain_length


def length_ok(en: str, de: str) -> bool:
    """Length ratio de/en within 0.35-3.0 (only for sources >= 40 characters)."""
    source = _plain_length(en)
    if source < LENGTH_MIN_SOURCE:
        return True
    return LENGTH_RATIO_MIN <= _plain_length(de) / source <= LENGTH_RATIO_MAX


def distinctive(text: str) -> bool:
    """True if ``check`` can tell this source apart from others (tokens or >= 40 chars)."""
    return bool(TOKEN_RE.search(text)) or _plain_length(text) >= LENGTH_MIN_SOURCE


def _colour_balanced(text: str) -> bool:
    depth = 0
    for code in _COLOUR_RE.findall(text):
        if code == "§!":
            if depth == 0:
                return False
            depth -= 1
        else:
            depth += 1
    return True


def colour_structure_ok(en: str, de: str) -> bool:
    """No ``§!`` before its opening code in ``de`` if the source is well-formed.

    Colour spans may change their order (German verb-final word order moves
    them, 78 of Millennium Dawn's human translations do so), so only the
    nesting is compared, not the sequence.
    """
    return not _colour_balanced(en) or _colour_balanced(de)


def foreign_escapes(en: str, de: str) -> bool:
    """``de`` has backslash escapes other than ``\\n``/``\\"`` that ``en`` lacks."""

    def others(text: str) -> Counter[str]:
        return Counter(c for c in _ESCAPE_RE.findall(text) if c not in _ALLOWED_ESCAPES)

    return bool(others(de) - others(en))


_DOUBLE_ESCAPE_RE = re.compile(r'\\\\(?=[n"])')


def repair_escapes(en: str, de: object) -> object:
    """Undo a model's double escaping: ``\\\\n`` -> ``\\n`` and ``\\\\"`` -> ``\\"``.

    Only when the English text has no doubled backslash of its own; anything
    else (and a non-string) is returned unchanged. ``check`` decides afterwards.
    """
    if not isinstance(de, str) or "\\\\" in en or not _DOUBLE_ESCAPE_RE.search(de):
        return de
    return _DOUBLE_ESCAPE_RE.sub(lambda m: "\\", de)


def _wrapped_in_quotes(text: str) -> bool:
    t = text.strip().replace('\\"', '"')
    return (
        len(t) >= 2
        and t[0] in _OPEN_QUOTES
        and t[-1] in _CLOSE_QUOTES
        and sum(c in _ALL_QUOTES for c in t) == 2
    )


def is_chatter(en: str, de: str) -> bool:
    """Model chatter around the answer: "Hier ist die Übersetzung: …", "Translation:",
    "Here is …" or the whole answer in quotes although the source has none."""
    if _CHATTER_PREFIX_RE.search(de):
        return True
    if _CHATTER_LABEL_RE.search(de) and "translation" not in en.lower():
        return True
    source = en.strip().replace('\\"', '"')
    return _wrapped_in_quotes(de) and not (source[:1] in _ALL_QUOTES or source[-1:] in _ALL_QUOTES)


def check(en: str, de: object) -> str | None:
    """Return ``None`` if ``de`` is a usable translation of ``en``, else a reason:

    ``empty``      missing, not a string or blank
    ``newline``    contains a real line break (HOI4 needs the ``\\n`` token)
    ``backslash``  ends with a lone backslash, or has escapes other than ``\\n``/``\\"``
    ``tokens``     formatting tokens or token characters differ from the source
    ``order``      a colour span is closed before it is opened
    ``unchanged``  identical to the English text (kept apart from real translations)
    ``notext``     the source has words, the answer not a single letter
    ``chatter``    "Hier ist die Übersetzung:", "Translation:", quotes around everything
    ``length``     truncated or bloated (length ratio outside 0.35-3.0)
    ``english``    still reads like English (see ``looks_english``)
    """
    if not isinstance(de, str) or not de.strip():
        return "empty"
    if "\n" in de or "\r" in de:
        return "newline"
    if _TRAILING_BACKSLASH_RE.search(de) or foreign_escapes(en, de):
        return "backslash"
    if tokens(en) != tokens(de):
        return "tokens"
    if any(de.count(c) > en.count(c) for c in _SPECIAL_CHARS):
        return "tokens"
    if not colour_structure_ok(en, de):
        return "order"
    if is_unchanged(en, de):
        return "unchanged"
    if has_text(en) and not _LETTER_RE.search(strip_tokens(de)):
        return "notext"
    if is_chatter(en, de):
        return "chatter"
    if not length_ok(en, de):
        return "length"
    if looks_english(en, de):
        return "english"
    return None
