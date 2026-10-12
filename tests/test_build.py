"""build: generated localisation files; check: validation of the output."""
from __future__ import annotations

import json

import pytest

from mdde import build as buildmod
from mdde.__main__ import main
from mdde.cache import TranslationCache

BOM = b"\xef\xbb\xbf"

TRANSLATIONS = {
    ("GER_focus_unity", "German Unity"): "Deutsche Einheit",
    ("GER_focus_new", "Brand New Focus"): "Brandneuer Fokus",
    ("GER_event_title", "German Troubles"): 'Deutsche "Probleme"',
    ("brave_trait", "Brave and bold"): "Mutig und kühn",
    ("RUS_communism", "Soviet Union"): "Sowjetunion",
    ("core_text", "Core vanilla text"): "Kerntext",
}


@pytest.fixture
def mod(tmp_path):
    return tmp_path / "mod"


@pytest.fixture
def cache(mod):
    c = TranslationCache(mod / "cache" / "translations.jsonl")
    for (key, en), de in TRANSLATIONS.items():
        c.put(key, en, de, "m1")
    return c


def run_build(md_root, mod, cache):
    return buildmod.build(md_root, mod / "localisation" / "german", cache, info_path=mod / "build_info.json")


def out(mod, rel):
    return (mod / "localisation" / "german" / rel).read_bytes()


def test_german_focus_file_gets_translations_and_appended_key(md_root, mod, cache):
    run_build(md_root, mod, cache)
    expected = (
        "l_german:\n"
        ' GER_focus_unity: "Deutsche Einheit"\n'
        ' GER_focus_unity_desc: "Wir müssen uns vereinen."\n'
        ' GER_tokens_only: "§Y$VALUE$§!"\n'
        ' GER_de_only: "Nur deutsch"\n'
        ' GER_focus_new:0 "Brandneuer Fokus"\n'
    )
    assert out(mod, "MD_focus_GER_l_german.yml") == BOM + expected.encode()


def test_quotes_in_translation_are_escaped(md_root, mod, cache):
    run_build(md_root, mod, cache)
    text = out(mod, "MD_events_l_german.yml").decode("utf-8-sig")
    assert ' GER_event_title: "Deutsche \\"Probleme\\""\n' in text
    assert ' already_done: "Schon übersetzt"\n' in text
    assert ' RUS_event_title: "Russian Troubles"\n' in text


def test_duplicate_key_in_german_only_file_is_translated_too(md_root, mod, cache):
    run_build(md_root, mod, cache)
    text = out(mod, "MD_subideology_parties_l_german.yml").decode("utf-8-sig")
    assert text == 'l_german:\n GER_event_title: "Deutsche \\"Probleme\\""\n moved_key: "Verschoben"\n'


def test_replace_folder_is_mirrored(md_root, mod, cache):
    run_build(md_root, mod, cache)
    assert out(mod, "replace/replaced_from_vanilla_core_l_german.yml") == BOM + (
        'l_german:\n core_text:0 "Kerntext"\n'
    ).encode()


def test_english_only_file_becomes_new_german_file(md_root, mod, cache):
    report = run_build(md_root, mod, cache)
    assert out(mod, "traits/SAF_traits_l_german.yml") == BOM + (
        "l_german:\n"
        ' SAF_trait_x:0 "South African Trait"\n'
        ' brave_trait:0 "Mutig und kühn"\n'
    ).encode()
    assert out(mod, "MD_focus_MON_l_german.yml").startswith(BOM + b"l_german:\n")
    assert report.new_files == 2


def test_english_only_file_drops_keys_already_in_german(md_root, mod, cache):
    (md_root / "localisation/english/extra_l_english.yml").write_text(
        '﻿l_english:\n GER_focus_unity:0 "German Unity"\n fresh_key:0 "Fresh text"\n', encoding="utf-8"
    )
    run_build(md_root, mod, cache)
    assert out(mod, "extra_l_german.yml").decode("utf-8-sig") == 'l_german:\n fresh_key:0 "Fresh text"\n'


def test_english_only_file_without_entries_is_skipped(md_root, mod, cache):
    (md_root / "localisation/english/empty_l_english.yml").write_text(
        "﻿l_english:\n # nothing\n", encoding="utf-8"
    )
    report = run_build(md_root, mod, cache)
    assert not (mod / "localisation/german/empty_l_german.yml").exists()
    assert "empty" in report.skipped_empty


def test_without_cache_output_equals_millennium_dawn(md_root, mod):
    empty = TranslationCache(mod / "cache" / "none.jsonl")
    run_build(md_root, mod, empty)
    german = md_root / "localisation" / "german"
    for src in german.rglob("*.yml"):
        rel = src.relative_to(german).as_posix()
        if rel == "MD_focus_GER_l_german.yml":
            continue  # gets the missing key GER_focus_new appended
        assert out(mod, rel) == src.read_bytes(), rel
    focus = out(mod, "MD_focus_GER_l_german.yml")
    assert focus == (german / "MD_focus_GER_l_german.yml").read_bytes() + b' GER_focus_new:0 "Brand New Focus"\n'


def test_output_folder_is_cleared_of_old_yml_only(md_root, mod, cache):
    target = mod / "localisation" / "german"
    (target / "gone").mkdir(parents=True)
    (target / "gone" / "old_l_german.yml").write_text("l_german:\n")
    (target / "NOTES.txt").write_text("keep me")
    run_build(md_root, mod, cache)
    assert not (target / "gone").exists()
    assert (target / "NOTES.txt").read_text() == "keep me"


def test_report_and_build_info(md_root, mod, cache):
    report = run_build(md_root, mod, cache)
    assert report.mirrored == 8
    assert report.new_files == 2
    assert report.files == 10
    assert report.replaced == 6  # unity, event title x2, cosmetic, core, brave
    assert report.appended == 1
    info = json.loads((mod / "build_info.json").read_text())
    assert info["md_version"] == "2.0.2"
    assert info["files"] == 10
    assert len(info["md_fingerprint"]) == 40


# -- check ---------------------------------------------------------------------
def run_check(md_root, mod):
    return buildmod.check(md_root, mod / "localisation" / "german", info_path=mod / "build_info.json")


def test_fresh_build_passes_check(md_root, mod, cache):
    run_build(md_root, mod, cache)
    report = run_check(md_root, mod)
    assert report.errors == []
    assert report.warnings == []
    assert report.files == 10
    assert report.exit_code == 0


def edit(mod, rel, old, new, encoding="utf-8-sig"):
    path = mod / "localisation" / "german" / rel
    text = path.read_text(encoding="utf-8-sig")
    assert old in text
    path.write_text(text.replace(old, new), encoding=encoding)


@pytest.mark.parametrize(
    "old,new,needle",
    [
        ('"Deutsche Einheit"', '"Deutsche §YEinheit"', "Token"),
        ('"Deutsche Einheit"', '"Deutsche "Einheit""', "Anführungszeichen"),
        ('"Deutsche Einheit"', '"Deutsche Einheit\\"', "Backslash"),
        (" GER_de_only:", " kaputte zeile\n GER_de_only:", "nicht lesbar"),
        ("l_german:", "l_english:", "Kopfzeile"),
    ],
)
def test_broken_output_is_reported(md_root, mod, cache, old, new, needle):
    run_build(md_root, mod, cache)
    edit(mod, "MD_focus_GER_l_german.yml", old, new)
    report = run_check(md_root, mod)
    assert report.exit_code == 1
    assert any(needle in e and "MD_focus_GER_l_german.yml" in e for e in report.errors), report.errors


def test_missing_bom_is_an_error(md_root, mod, cache):
    run_build(md_root, mod, cache)
    edit(mod, "countries_l_german.yml", "Deutschland", "Deutschland", encoding="utf-8")
    report = run_check(md_root, mod)
    assert any("BOM" in e and "countries_l_german.yml" in e for e in report.errors)


def test_missing_output_file_is_an_error(md_root, mod, cache):
    run_build(md_root, mod, cache)
    (mod / "localisation/german/replace/replaced_from_vanilla_core_l_german.yml").unlink()
    report = run_check(md_root, mod)
    assert any("fehlt" in e and "replaced_from_vanilla_core" in e for e in report.errors)


def test_changed_millennium_dawn_makes_build_stale(md_root, mod, cache):
    run_build(md_root, mod, cache)
    path = md_root / "localisation/english/MD_events_l_english.yml"
    path.write_text(path.read_text(encoding="utf-8-sig") + ' brand_new:0 "Brand new"\n', encoding="utf-8-sig")
    report = run_check(md_root, mod)
    assert report.errors == []
    assert report.warnings
    assert report.exit_code == 2


def test_missing_build_info_is_an_error(md_root, mod, cache):
    run_build(md_root, mod, cache)
    (mod / "build_info.json").unlink()
    assert run_check(md_root, mod).exit_code == 1


def test_cli_build_then_check(md_root, mod, cache, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MDDE_MD_ROOT", str(md_root))
    monkeypatch.setenv("MDDE_MOD_ROOT", str(mod))
    monkeypatch.setenv("MDDE_VANILLA_DE", str(tmp_path / "no_vanilla"))
    assert main(["build"]) == 0
    assert main(["check"]) == 0
    text = capsys.readouterr().out
    assert "10" in text
    edit(mod, "MD_focus_GER_l_german.yml", '"Deutsche Einheit"', '"Deutsche §YEinheit"')
    assert main(["check"]) == 1


# -- review finding (small 3): untranslated fallback must not cover vanilla German -------
def test_untranslated_key_known_to_vanilla_german_is_not_appended(md_root, mod):
    empty = TranslationCache(mod / "cache" / "none.jsonl")
    report = buildmod.build(md_root, mod / "localisation" / "german", empty,
                            info_path=mod / "build_info.json", vanilla_keys={"GER_focus_new", "SAF_trait_x"})
    focus = out(mod, "MD_focus_GER_l_german.yml").decode("utf-8-sig")
    assert "GER_focus_new" not in focus
    traits = out(mod, "traits/SAF_traits_l_german.yml").decode("utf-8-sig")
    assert "SAF_trait_x" not in traits and "brave_trait" in traits
    assert report.vanilla_kept == 2


def test_translated_key_is_appended_even_if_vanilla_knows_it(md_root, mod, cache):
    buildmod.build(md_root, mod / "localisation" / "german", cache,
                   info_path=mod / "build_info.json", vanilla_keys={"GER_focus_new"})
    assert ' GER_focus_new:0 "Brandneuer Fokus"' in out(mod, "MD_focus_GER_l_german.yml").decode("utf-8-sig")


def test_vanilla_keys_are_read_recursively(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a_l_german.yml").write_text('\ufeffl_german:\n A_KEY:0 "A"\n', encoding="utf-8")
    (tmp_path / "b_l_german.yml").write_text('l_german:\n B_KEY: "B"\n', encoding="utf-8")
    assert buildmod.vanilla_keys(tmp_path) == {"A_KEY", "B_KEY"}
    assert buildmod.vanilla_keys(tmp_path / "missing") == frozenset()


def test_cli_build_respects_vanilla_dir(md_root, mod, cache, monkeypatch, tmp_path):
    vanilla = tmp_path / "vanilla"
    vanilla.mkdir()
    (vanilla / "x_l_german.yml").write_text('l_german:\n brave_trait: "Tapfer"\n', encoding="utf-8")
    monkeypatch.setenv("MDDE_MD_ROOT", str(md_root))
    monkeypatch.setenv("MDDE_MOD_ROOT", str(mod))
    monkeypatch.setenv("MDDE_VANILLA_DE", str(vanilla))
    (mod / "cache" / "translations.jsonl").unlink()
    assert main(["build"]) == 0
    assert "brave_trait" not in out(mod, "traits/SAF_traits_l_german.yml").decode("utf-8-sig")


# -- review finding (small 4): no key in two output files --------------------------------
def shared_key_tree(md_root):
    en = md_root / "localisation" / "english"
    (en / "aaa_l_english.yml").write_text('\ufeffl_english:\n SHARED_key:0 "Shared text"\n', encoding="utf-8")
    (en / "zzz_l_english.yml").write_text('\ufeffl_english:\n SHARED_key:0 "Shared text"\n OWN:0 "Own"\n',
                                          encoding="utf-8")
    (md_root / "localisation" / "german" / "zzz_l_german.yml").write_text(
        '\ufeffl_german:\n OWN:0 "Eigen"\n', encoding="utf-8")


def defining_files(mod, key):
    root = mod / "localisation" / "german"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.yml")
                  if f" {key}:" in p.read_text(encoding="utf-8-sig"))


def test_key_missing_in_german_is_written_to_one_output_file_only(md_root, mod, cache):
    shared_key_tree(md_root)
    run_build(md_root, mod, cache)
    assert defining_files(mod, "SHARED_key") == ["aaa_l_german.yml"]
    assert run_check(md_root, mod).exit_code == 0


def test_check_reports_a_key_defined_in_two_output_files(md_root, mod, cache):
    run_build(md_root, mod, cache)
    path = mod / "localisation" / "german" / "countries_l_german.yml"
    path.write_text(path.read_text(encoding="utf-8-sig") + ' GER_focus_unity: "Doppelt"\n', encoding="utf-8-sig")
    report = run_check(md_root, mod)
    assert report.exit_code == 1
    assert any("GER_focus_unity" in e and "Ausgabedateien" in e for e in report.errors), report.errors


def test_duplicates_inherited_from_millennium_dawn_are_only_a_warning(md_root, mod, cache):
    run_build(md_root, mod, cache)  # GER_event_title is in MD_events and MD_subideology_parties
    report = run_check(md_root, mod)
    assert report.errors == []
    assert report.exit_code == 0
    assert any("GER_event_title" in w for w in report.md_duplicates), report.md_duplicates
