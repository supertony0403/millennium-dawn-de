"""revalidate: re-check cached translations with the current validator."""
from __future__ import annotations

import json

import pytest

from mdde.__main__ import main
from mdde.cache import RejectLog, TranslationCache, UnchangedLog, run_lock, src_hash
from mdde.revalidate import revalidate


def record(key, en, de, model="m1"):
    return json.dumps({"key": key, "src_sha1": src_hash(en), "de": de, "model": model, "ts": "x"},
                      ensure_ascii=False)


@pytest.fixture
def cache_dir(tmp_path):
    path = tmp_path / "cache"
    path.mkdir()
    lines = [
        record("GER_focus_unity", "German Unity", "Deutsche Einhait"),  # superseded below
        record("GER_focus_unity", "German Unity", "Deutsche Einheit"),  # valid
        record("GER_focus_new", "Brand New Focus", "Brandneuer \\„Fokus“"),  # backslash
        record("generic_event", "A generic event happens", "A generic event happens"),  # unchanged
        record("FRANCE_generic", "Generic French Text", "Hier ist die Übersetzung: Allgemeiner Text"),  # chatter
        record("GER_event_title", "Old English text", "Alter Text"),  # English changed since: kept
        "{kaputt",
    ]
    (path / "translations.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(md_root, cache_dir, apply):
    return revalidate(md_root, cache_dir / "translations.jsonl", RejectLog(cache_dir / "rejects.jsonl"),
                      UnchangedLog(cache_dir / "unchanged.jsonl"), apply=apply)


def test_dry_run_counts_and_changes_nothing(md_root, cache_dir):
    before = (cache_dir / "translations.jsonl").read_bytes()
    report = run(md_root, cache_dir, apply=False)
    assert report.checked == 4
    assert report.valid == 1
    assert report.stale == 1
    assert report.by_reason == {"backslash": 1, "chatter": 1, "unchanged": 1}
    assert report.superseded == 1
    assert report.broken == 1
    assert report.examples["backslash"][0][0] == "GER_focus_new"
    assert (cache_dir / "translations.jsonl").read_bytes() == before
    assert not (cache_dir / "rejects.jsonl").exists()
    assert not (cache_dir / "unchanged.jsonl").exists()
    assert report.backup is None


def test_apply_cleans_cache_moves_entries_and_keeps_a_backup(md_root, cache_dir):
    before = (cache_dir / "translations.jsonl").read_bytes()
    report = run(md_root, cache_dir, apply=True)
    assert report.backup is not None and report.backup.read_bytes() == before
    assert report.backup.name.startswith("translations.jsonl.bak-")
    cache = TranslationCache(cache_dir / "translations.jsonl")
    assert len(cache) == 2
    assert cache.skipped == 0
    assert cache.get("GER_focus_unity", "German Unity") == "Deutsche Einheit"
    assert cache.get("GER_focus_new", "Brand New Focus") is None
    assert cache.get("generic_event", "A generic event happens") is None
    rejects = RejectLog(cache_dir / "rejects.jsonl")
    assert rejects.contains("GER_focus_new", "Brand New Focus")
    assert rejects.contains("FRANCE_generic", "Generic French Text")
    reasons = {json.loads(line)["reason"] for line in (cache_dir / "rejects.jsonl").read_text().splitlines()}
    assert reasons == {"backslash", "chatter"}
    assert UnchangedLog(cache_dir / "unchanged.jsonl").contains("generic_event", "A generic event happens")


def test_cli_revalidate_is_a_dry_run_by_default(md_root, cache_dir, monkeypatch, capsys):
    monkeypatch.setenv("MDDE_MD_ROOT", str(md_root))
    monkeypatch.setenv("MDDE_CACHE_DIR", str(cache_dir))
    before = (cache_dir / "translations.jsonl").read_bytes()
    assert main(["revalidate"]) == 0
    out = capsys.readouterr().out
    assert "backslash" in out and "GER_focus_new" in out and "Probelauf" in out
    assert (cache_dir / "translations.jsonl").read_bytes() == before
    assert main(["revalidate", "--apply"]) == 0
    assert (cache_dir / "translations.jsonl").read_bytes() != before


def test_cli_revalidate_apply_respects_the_run_lock(md_root, cache_dir, monkeypatch, capsys):
    monkeypatch.setenv("MDDE_MD_ROOT", str(md_root))
    monkeypatch.setenv("MDDE_CACHE_DIR", str(cache_dir))
    before = (cache_dir / "translations.jsonl").read_bytes()
    with run_lock(cache_dir / "translate.lock"):
        assert main(["revalidate", "--apply"]) == 1
    assert (cache_dir / "translations.jsonl").read_bytes() == before


def test_examples_are_spread_over_the_whole_group(md_root, tmp_path):
    pairs = [("GER_focus_unity", "German Unity"), ("GER_focus_unity_desc", "We must unite."),
             ("GER_focus_new", "Brand New Focus"), ("CAN_focus_maple", "Maple Leaf Forever"),
             ("RUS_event_title", "Russian Troubles"), ("GER_event_title", "German Troubles"),
             ("USA.conservatism", "American Conservatism")]
    path = tmp_path / "translations.jsonl"
    path.write_text("".join(record(k, en, en) + "\n" for k, en in pairs), encoding="utf-8")
    report = revalidate(md_root, path, RejectLog(tmp_path / "r.jsonl"), UnchangedLog(tmp_path / "u.jsonl"),
                        examples=3)
    assert [k for k, _, _ in report.examples["unchanged"]] == ["GER_focus_unity", "CAN_focus_maple",
                                                                 "USA.conservatism"]


# -- review 2, W-3: repair doubly escaped answers from rejects.jsonl offline --------------
from mdde.revalidate import repair_rejects  # noqa: E402


def reject(key, en, reason, de, model="m1"):
    return json.dumps({"key": key, "src_sha1": src_hash(en), "en": en, "reason": reason, "model": model,
                       "de": de, "ts": "x"}, ensure_ascii=False)


@pytest.fixture
def reject_dir(tmp_path):
    path = tmp_path / "cache"
    path.mkdir()
    lines = [
        reject("a", "Line one\\nLine two", "backslash", "Zeile eins\\\\nZeile zwei"),  # repairable
        reject("b", 'Say \\"no\\"', "backslash", 'Sag \\\\"nein\\\\"'),  # repairable
        reject("c", "Brand New Focus", "backslash", "Brandneuer \\„Fokus“"),  # stays invalid
        reject("d", "German Unity", "chatter", "Hier ist die Übersetzung: Deutsche Einheit"),  # other reason
        reject("e", "Gone\\nNow", "backslash", None),  # no answer kept
        reject("f", "Done\\nAlready", "backslash", "Fertig\\\\nSchon"),  # translated since
        "{kaputt",
    ]
    (path / "rejects.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "translations.jsonl").write_text(record("f", "Done\\nAlready", "Erledigt\\nBereits") + "\n",
                                             encoding="utf-8")
    return path


def repair(path, apply):
    return repair_rejects(path / "rejects.jsonl", path / "translations.jsonl", apply=apply)


def test_repair_rejects_dry_run_counts_and_writes_nothing(reject_dir):
    before = {p.name: p.read_bytes() for p in reject_dir.iterdir()}
    report = repair(reject_dir, apply=False)
    assert report.candidates == 5
    assert report.repaired == 2
    assert report.still_invalid == {"backslash": 1}
    assert report.without_answer == 1
    assert report.already_cached == 1
    assert [key for key, _, _ in report.examples] == ["a", "b"]
    assert report.backup is None
    assert {p.name: p.read_bytes() for p in reject_dir.iterdir()} == before


def test_repair_rejects_apply_adds_valid_ones_to_the_cache_with_backup(reject_dir):
    before = (reject_dir / "translations.jsonl").read_bytes()
    rejects_before = (reject_dir / "rejects.jsonl").read_bytes()
    report = repair(reject_dir, apply=True)
    assert report.backup is not None and report.backup.read_bytes() == before
    cache = TranslationCache(reject_dir / "translations.jsonl")
    assert cache.get("a", "Line one\\nLine two") == "Zeile eins\\nZeile zwei"
    assert cache.get("b", 'Say \\"no\\"') == 'Sag \\"nein\\"'
    assert cache.get("c", "Brand New Focus") is None
    assert cache.get("f", "Done\\nAlready") == "Erledigt\\nBereits"
    assert (reject_dir / "rejects.jsonl").read_bytes() == rejects_before  # the cache wins anyway


def test_cli_repair_rejects_is_a_dry_run_and_needs_no_md(reject_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MDDE_MD_ROOT", str(tmp_path / "no_md_here"))
    monkeypatch.setenv("MDDE_CACHE_DIR", str(reject_dir))
    before = (reject_dir / "translations.jsonl").read_bytes()
    assert main(["revalidate", "--repair-rejects"]) == 0
    out = capsys.readouterr().out
    assert "Probelauf" in out and "gueltig nach Reparatur:" in out and " 2" in out
    assert (reject_dir / "translations.jsonl").read_bytes() == before
    with run_lock(reject_dir / "translate.lock"):
        assert main(["revalidate", "--repair-rejects", "--apply"]) == 1
    assert (reject_dir / "translations.jsonl").read_bytes() == before
    assert main(["revalidate", "--repair-rejects", "--apply"]) == 0
    assert TranslationCache(reject_dir / "translations.jsonl").get("a", "Line one\\nLine two") is not None
