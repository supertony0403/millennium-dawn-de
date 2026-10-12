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
