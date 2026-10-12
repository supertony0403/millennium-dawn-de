"""Checks against the real Millennium Dawn installation (skipped when it is missing).

They pin the stale-English detection (review finding W3) to real keys and guard
against false alarms on Millennium Dawn's human German translations.
"""
from __future__ import annotations

import re

import pytest

from mdde import locfile, scope
from mdde.config import DEFAULT_MD_ROOT

MD = DEFAULT_MD_ROOT
pytestmark = pytest.mark.skipif(not (MD / "localisation").is_dir(), reason="Millennium Dawn nicht installiert")

STALE_GER_KEYS = {
    "GER_swis_uprising",
    "germany.38.d",
    "germany.49.d",
    "germany.51.d",
    "GER_Bundeswehr_modifier_desc",
}

# Short stale copies (2-5 words), all really English, spread over several files.
SHORT_STALE_KEYS = {
    "germany.38.a",               # "tough hit on the CDU"
    "germany.49.t",               # "Expulsion from the faction"
    "GER_Bundeswehr_modifier",    # "State of the Bundeswehr"
    "germany.39.a",               # "Our trains are safe again"
    "EH_event.422.a",             # "So be it"
    "gcc_invite.3.a",             # "So be it."
    "khanti_mansi_focus",         # "Tree of Focuses of Ugra"
    "AIRWING_MISSION_IN_REGION",  # "In Mission in $REG|G$"
}

# Independent of the implementation's stop-word lists: an umlaut/ß plus at least
# two of these function words marks a value as German.
_GERMAN_MARK = re.compile(r"[äöüÄÖÜß]")
_GERMAN_WORDS = frozenset({"der", "die", "das", "und", "mit", "nicht", "ist", "wird", "werden", "den", "dem"})


def _clearly_german(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text.lower())
    return bool(_GERMAN_MARK.search(text)) and sum(w in _GERMAN_WORDS for w in words) >= 2


@pytest.fixture(scope="module")
def germany_open():
    return scope.collect(MD, "germany")


def test_stale_english_values_in_md_focus_ger_are_open(germany_open):
    open_keys = {i.key for i in germany_open if i.base == "MD_focus_GER"}
    assert STALE_GER_KEYS <= open_keys


def test_short_stale_english_values_are_open(germany_open):
    open_keys = {i.key for i in germany_open}
    assert SHORT_STALE_KEYS <= open_keys


def test_real_german_md_translations_are_not_open():
    german: list[tuple[str, str, str]] = []
    for pair in scope.discover(MD):
        if pair.en_path is None or pair.de_path is None:
            continue
        en_values = locfile.parse(pair.en_path).values()
        for key, de in locfile.parse(pair.de_path).values().items():
            en = en_values.get(key)
            if en is not None and de != en and _clearly_german(de):
                german.append((key, en, de))
    german.sort()
    sample = german[:: max(1, len(german) // 300)][:300]
    assert len(sample) >= 200
    false_alarms = [(k, de) for k, en, de in sample if scope.is_open(en, de)]
    assert false_alarms == []


def test_every_real_md_line_is_rebuilt_byte_identically():
    """Round trip over all English and German files: render() and each entry line."""
    loc = MD / "localisation"
    files = sorted((loc / "english").rglob("*.yml")) + sorted((loc / "german").rglob("*.yml"))
    assert len(files) > 100
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        lf = locfile.parse_text(text)
        assert lf.render() == text.replace("\r\n", "\n"), path
        for entry in lf.entries:
            assert entry.line() == lf.lines[entry.idx], (path, entry.idx)


def test_bare_quotes_in_real_traits_line_still_parse():
    lf = locfile.parse(MD / "localisation/english/replace/replaced_from_vanilla_traits_l_english.yml")
    assert lf.values()["loading_drill_master_desc"] == '§L"Shoot them again, they\'re not dead yet!"§!'
