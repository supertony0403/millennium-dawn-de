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
