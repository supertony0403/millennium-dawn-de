"""Scope rules: which English texts are open for translation."""
from __future__ import annotations

import pytest
from conftest import FULL_ONLY, GERMANY_OPEN

from mdde import scope


def keyset(items):
    return {(i.base, i.key) for i in items}


def test_country_tags_are_read_from_all_tag_files(md_root):
    tags = scope.load_country_tags(md_root)
    assert tags == {"GER", "CAN", "RUS", "USA", "FRA", "MON", "SAF", "D01"}


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("MD_focus_GER_l_english.yml", ("MD_focus_GER", "english")),
        ("MD_focus_GER_l_german.yml", ("MD_focus_GER", "german")),
        ("MD_focus_MON_I_english.yml", ("MD_focus_MON", "english")),
        ("languages.yml", None),
        ("notes.txt", None),
    ],
)
def test_loc_base(filename, expected):
    assert scope.loc_base(filename) == expected


def test_discover_pairs_english_and_german_by_base(md_root):
    pairs = {p.base: p for p in scope.discover(md_root)}
    core = pairs["replaced_from_vanilla_core"]
    assert core.en_rel == "replace/replaced_from_vanilla_core_l_english.yml"
    assert core.de_rel == "replace/replaced_from_vanilla_core_l_german.yml"
    assert pairs["SAF_traits"].de_path is None
    assert pairs["SAF_traits"].en_rel == "traits/SAF_traits_l_english.yml"
    assert pairs["MD_subideology_parties"].en_path is None
    assert pairs["MD_focus_MON"].en_rel == "MD_focus_MON_I_english.yml"


def test_germany_scope_selects_exactly_the_expected_texts(md_root):
    assert keyset(scope.collect(md_root, "germany")) == GERMANY_OPEN


def test_full_scope_adds_foreign_files_and_keys_but_not_name_lists(md_root):
    assert keyset(scope.collect(md_root, "full")) == GERMANY_OPEN | FULL_ONLY


def test_item_carries_english_text_and_german_state(md_root):
    items = {(i.base, i.key): i for i in scope.collect(md_root, "germany")}
    unity = items[("MD_focus_GER", "GER_focus_unity")]
    assert unity.en == "German Unity"
    assert unity.de_current == "German Unity"
    assert unity.de_path == md_root / "localisation/german/MD_focus_GER_l_german.yml"
    assert unity.file_rel_en == "MD_focus_GER_l_english.yml"

    new = items[("MD_focus_GER", "GER_focus_new")]
    assert new.de_current is None
    assert new.de_path == md_root / "localisation/german/MD_focus_GER_l_german.yml"

    trait = items[("SAF_traits", "brave_trait")]
    assert trait.de_current is None
    assert trait.de_path is None


def test_translation_in_other_german_file_counts_as_done(md_root):
    keys = keyset(scope.collect(md_root, "full"))
    assert ("MD_events", "moved_key") not in keys


def test_unknown_scope_is_rejected(md_root):
    with pytest.raises(ValueError):
        scope.collect(md_root, "everything")


@pytest.mark.parametrize(
    "en,de,expected",
    [
        ("German Unity", None, True),
        ("German Unity", "German Unity", True),
        ("German Unity", "Deutsche Einheit", False),
        ("§Y$VALUE$§!", None, False),
        ("[ROOT.GetName] $X$ \\n", None, False),
        ("OK", None, False),
        ("Ab 12", None, False),
        ("§YStärke§!", None, True),
        ("", None, False),
    ],
)
def test_is_open(en, de, expected):
    assert scope.is_open(en, de) is expected
