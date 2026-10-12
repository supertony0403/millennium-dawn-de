"""Shared fixtures: a miniature Millennium Dawn tree with hand-checked content.

Expected open texts (see test_scope.py) are derived by hand from the strings
below, never from the code under test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

BOM = "﻿"

COUNTRY_TAGS = {
    "00_countries.txt": (
        "# country tags\n"
        'GER = "countries/Germany.txt"\n'
        'CAN = "countries/Canada.txt"\n'
        'RUS = "countries/Russia.txt"\n'
        'USA\t= "countries/USA.txt"\n'
        'FRA = "countries/France.txt"\n'
        'MON = "countries/Mongolia.txt"\n'
        'SAF = "countries/South Africa.txt" # comment\n'
        "dynamic_tags = yes\n"
    ),
    "zz_dynamic_countries.txt": 'D01 = "countries/D01.txt"\n',
}

# relative path below localisation/ -> file content (BOM added on write)
LOC_FILES = {
    "english/MD_focus_GER_l_english.yml": (
        "l_english:\n"
        ' GER_focus_unity:0 "German Unity"\n'
        ' GER_focus_unity_desc:0 "We must unite."\n'
        ' GER_focus_new:0 "Brand New Focus"\n'
        ' GER_tokens_only:0 "§Y$VALUE$§!"\n'
    ),
    "german/MD_focus_GER_l_german.yml": (
        "l_german:\n"
        ' GER_focus_unity: "German Unity"\n'
        ' GER_focus_unity_desc: "Wir müssen uns vereinen."\n'
        ' GER_tokens_only: "§Y$VALUE$§!"\n'
        ' GER_de_only: "Nur deutsch"\n'
    ),
    "english/MD_focus_CAN_l_english.yml": (
        "l_english:\n"
        ' CAN_focus_maple:0 "Maple Leaf Forever"\n'
    ),
    "german/MD_focus_CAN_l_german.yml": (
        "l_german:\n"
        ' CAN_focus_maple: "Maple Leaf Forever"\n'
    ),
    "english/MD_events_l_english.yml": (
        "l_english:\n"
        " # events\n"
        ' RUS_event_title:0 "Russian Troubles"\n'
        ' GER_event_title:0 "German Troubles"\n'
        ' USA.conservatism:0 "American Conservatism"\n'
        ' FRA:0 "France"\n'
        ' FRANCE_generic:0 "Generic French Text"\n'
        ' generic_event:0 "A generic event happens" # trail\n'
        ' ger_lowercase_x:0 "lower case key"\n'
        ' D01_dynamic:0 "Dynamic Country text"\n'
        ' already_done:0 "Already translated"\n'
        ' DEU_unknown_prefix:0 "Unknown prefix text"\n'
        ' moved_key:0 "Moved elsewhere"\n'
    ),
    "german/MD_events_l_german.yml": (
        "l_german:\n"
        ' RUS_event_title: "Russian Troubles"\n'
        ' GER_event_title: "German Troubles"\n'
        ' USA.conservatism: "American Conservatism"\n'
        ' FRA: "France"\n'
        ' FRANCE_generic: "Generic French Text"\n'
        ' generic_event: "A generic event happens"\n'
        ' ger_lowercase_x: "lower case key"\n'
        ' D01_dynamic: "Dynamic Country text"\n'
        ' already_done: "Schon übersetzt"\n'
        ' DEU_unknown_prefix: "Unknown prefix text"\n'
    ),
    "english/countries_l_english.yml": (
        "l_english:\n"
        ' RUS:0 "Russia"\n'
        ' RUS_DEF:0 "the Russian Federation"\n'
        ' GER:0 "Germany"\n'
    ),
    "german/countries_l_german.yml": (
        "l_german:\n"
        ' RUS: "Russia"\n'
        ' RUS_DEF: "the Russian Federation"\n'
        ' GER: "Deutschland"\n'
    ),
    "english/MD_countries_cosmetic_l_english.yml": (
        "l_english:\n"
        ' RUS_communism:0 "Soviet Union"\n'
    ),
    "german/MD_countries_cosmetic_l_german.yml": (
        "l_german:\n"
        ' RUS_communism: "Soviet Union"\n'
    ),
    "english/state_names_l_english.yml": (
        "l_english:\n"
        ' STATE_1:0 "Berlin Area"\n'
    ),
    "german/state_names_l_german.yml": (
        "l_german:\n"
        ' STATE_1: "Berlin Area"\n'
    ),
    "english/replace/replaced_from_vanilla_core_l_english.yml": (
        "l_english:\n"
        ' core_text:0 "Core vanilla text"\n'
    ),
    "german/replace/replaced_from_vanilla_core_l_german.yml": (
        "l_german:\n"
        ' core_text:0 "Core vanilla text"\n'
    ),
    "english/traits/SAF_traits_l_english.yml": (
        "l_english:\n"
        ' SAF_trait_x:0 "South African Trait"\n'
        ' brave_trait:0 "Brave and bold"\n'
    ),
    "english/MD_focus_MON_I_english.yml": (
        "l_english:\n"
        ' MON_focus_x:0 "Mongol Focus"\n'
    ),
    "german/MD_subideology_parties_l_german.yml": (
        "l_german:\n"
        ' GER_event_title: "German Troubles"\n'
        ' moved_key: "Verschoben"\n'
    ),
}

GERMANY_OPEN = {
    ("MD_focus_GER", "GER_focus_unity"),
    ("MD_focus_GER", "GER_focus_new"),
    ("MD_events", "GER_event_title"),
    ("MD_events", "FRANCE_generic"),
    ("MD_events", "generic_event"),
    ("MD_events", "ger_lowercase_x"),
    ("MD_events", "DEU_unknown_prefix"),
    ("countries", "RUS"),
    ("countries", "RUS_DEF"),
    ("MD_countries_cosmetic", "RUS_communism"),
    ("replaced_from_vanilla_core", "core_text"),
    ("SAF_traits", "brave_trait"),
}

FULL_ONLY = {
    ("MD_focus_CAN", "CAN_focus_maple"),
    ("MD_events", "RUS_event_title"),
    ("MD_events", "USA.conservatism"),
    ("MD_events", "FRA"),
    ("MD_events", "D01_dynamic"),
    ("SAF_traits", "SAF_trait_x"),
    ("MD_focus_MON", "MON_focus_x"),
}


def write_md_tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "descriptor.mod").write_text(
        'version="2.0.2"\nname="Millennium Dawn: A Modern Day Mod"\n', encoding="utf-8"
    )
    tags_dir = root / "common" / "country_tags"
    tags_dir.mkdir(parents=True)
    for name, text in COUNTRY_TAGS.items():
        (tags_dir / name).write_text(text, encoding="utf-8")
    for rel, text in LOC_FILES.items():
        path = root / "localisation" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(BOM + text, encoding="utf-8")
    return root


@pytest.fixture
def md_root(tmp_path: Path) -> Path:
    return write_md_tree(tmp_path / "md")
