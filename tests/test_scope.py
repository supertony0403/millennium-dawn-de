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


# -- review finding W3: stale English copies in Millennium Dawn's German files --------
@pytest.mark.parametrize(
    "en,de",
    [
        # >= 3 English and <= 1 German stop words
        (
            "We have attracted lots of investors thanks to our deregulation policies",
            "We have attracted many investors thanks to our deregulation policies",
        ),
        (
            "The collapse of the USSR brought our country into a new geopolitical environment",
            "The collapse of the USSR brought Kazakhstan into a new geopolitical environment",
        ),
        # lower-case word overlap > 60 % with the English text
        (
            "§gwill launch an uprising in favor of a§! §Ymonarchy§!",
            "§gwill launch an uprising in favour of a§! §Ymonarch§!",
        ),
        # equal up to case and whitespace
        ("Reform the army", "Reform the Army "),
    ],
)
def test_stale_english_german_value_is_open(en, de):
    assert scope.is_open(en, de) is True


@pytest.mark.parametrize(
    "en,de",
    [
        ("Petr Pavel Completed Mission in Afghanistan", "Petr Pavel hat die Mission in Afghanistan abgeschlossen"),
        ("Osama bin Laden Killed", "Osama bin Laden getötet"),
        ("Execute Operation Enduring Freedom (OEF)", "Durchführung der Operation Enduring Freedom (OEF)"),
        ("Large-Scale TEG Integration", "Großangelegte TEG-Integration"),
        (
            "Mu isamaa, mu õnn ja rõõm (My Fatherland, my Happiness and Joy)!",
            "Mu isamaa, mu õnn ja rõõm (Mein Vaterland, mein Glück und meine Freude)!",
        ),
        (
            "£tank_design_cost_texticon modular_light_sp_anti_air_brigade Unit Cost",
            "£tank_design_cost_texticon modular_light_sp_anti_air_brigade Einheitskosten",
        ),
        ("View the latest changes in Millennium Dawn: A Modern Day Mod",
         "Siehe die neuesten Änderungen in Millennium Dawn: A Modern Day Mod."),
    ],
)
def test_real_german_translation_is_not_open(en, de):
    assert scope.is_open(en, de) is False


# -- short stale English copies (2-5 words): >= 2 English stop words, no German
# stop word, no umlaut/ß. Hand-picked from Millennium Dawn's German files.
@pytest.mark.parametrize(
    "en,de",
    [
        ("We must heed the public's demand for transparency.", "tough hit on the CDU"),
        ("The 2001 Cebit", "Expulsion from the faction"),
        ("Heer", "State of the Bundeswehr"),
        ("Trust Wowereit and rebuild through reforms.", "Our trains are safe again"),
        ("Ugra Focus Tree", "Tree of Focuses of Ugra"),
        ("On Mission in $REG|G$", "In Mission in $REG|G$"),
        # no word of three letters, so has_text() alone does not see it as text
        ("So be it", "So be it"),
        ("So be it.", "So be it."),
    ],
)
def test_short_stale_english_value_is_open(en, de):
    assert scope.is_open(en, de) is True


def test_short_english_source_without_german_value_is_open():
    assert scope.is_open("So be it", None) is True


@pytest.mark.parametrize(
    "en,de",
    [
        ("So be it", "So sei es"),
        ("On Mission in $REG|G$", "Im Einsatz in $REG|G$"),
        ("Operation in Mali", "Einsatz in Mali"),
        # two stop words of the short rule, but a German stop word ("und")
        ("In Europe and Asia", "In Europa und in Asien"),
        ("The Bank of Canada", "Bank of Canada"),
        # umlaut/ß marks German even without a German stop word
        ("Alliance for the Future", "Bündnis for the Future"),
        # more than five words is not a short value any more
        ("Ugra Focus Tree", "Tree of Focuses of Ugra Region"),
    ],
)
def test_short_german_or_name_value_is_not_open(en, de):
    assert scope.is_open(en, de) is False
