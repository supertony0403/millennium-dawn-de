"""Append-only translation cache and reject log."""
from __future__ import annotations

import hashlib
import json
import threading

import pytest

from mdde.cache import LockBusy, RejectLog, TranslationCache, UnchangedLog, run_lock


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def test_put_then_get_with_same_english(tmp_path):
    cache = TranslationCache(tmp_path / "cache" / "t.jsonl")
    cache.put("GER_x", "German Unity", "Deutsche Einheit", "model/a")
    assert cache.get("GER_x", "German Unity") == "Deutsche Einheit"


def test_changed_english_text_invalidates_entry(tmp_path):
    cache = TranslationCache(tmp_path / "t.jsonl")
    cache.put("GER_x", "German Unity", "Deutsche Einheit", "model/a")
    assert cache.get("GER_x", "German Unity!") is None
    assert cache.get("OTHER", "German Unity") is None


def test_record_format_on_disk(tmp_path):
    path = tmp_path / "t.jsonl"
    TranslationCache(path).put("GER_x", "German Unity", "Deutsche Einheit", "model/a")
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["key"] == "GER_x"
    assert record["src_sha1"] == sha1("German Unity")
    assert record["de"] == "Deutsche Einheit"
    assert record["model"] == "model/a"
    assert record["ts"].endswith("Z")


def test_entries_survive_reload(tmp_path):
    path = tmp_path / "t.jsonl"
    TranslationCache(path).put("k", "Hello world", "Hallo Welt", "m")
    reloaded = TranslationCache(path)
    assert reloaded.get("k", "Hello world") == "Hallo Welt"
    assert len(reloaded) == 1


def test_last_duplicate_wins(tmp_path):
    path = tmp_path / "t.jsonl"
    cache = TranslationCache(path)
    cache.put("k", "Hello world", "Hallo Welt", "m1")
    cache.put("k", "Hello world", "Servus Welt", "m2")
    assert TranslationCache(path).get("k", "Hello world") == "Servus Welt"


def test_broken_lines_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    good = {"key": "k", "src_sha1": sha1("Hello world"), "de": "Hallo Welt", "model": "m", "ts": "x"}
    path.write_text(
        "{kaputt\n"
        + json.dumps(good)
        + "\n"
        + json.dumps({"key": "nur_key"})
        + "\n\n"
        + '{"key": "abgeschnitten", "src_sh',
        encoding="utf-8",
    )
    cache = TranslationCache(path)
    assert cache.get("k", "Hello world") == "Hallo Welt"
    assert len(cache) == 1
    assert cache.skipped == 3


def test_missing_file_is_an_empty_cache(tmp_path):
    cache = TranslationCache(tmp_path / "nope" / "t.jsonl")
    assert len(cache) == 0
    assert cache.get("k", "x") is None


def test_put_is_on_disk_immediately(tmp_path):
    path = tmp_path / "t.jsonl"
    cache = TranslationCache(path)
    cache.put("k", "Hello world", "Hallo Welt", "m")
    assert "Hallo Welt" in path.read_text(encoding="utf-8")


def test_concurrent_puts_produce_whole_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    cache = TranslationCache(path)

    def worker(n: int) -> None:
        for i in range(200):
            cache.put(f"k{n}_{i}", f"English text {n} {i}", "x" * 500, "m")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1600
    assert all(json.loads(line)["de"] == "x" * 500 for line in lines)
    assert len(TranslationCache(path)) == 1600


def test_reject_log_records_and_reloads(tmp_path):
    path = tmp_path / "rejects.jsonl"
    log = RejectLog(path)
    log.add("k", "Hello world", "tokens", model="m", de="Hallo")
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["reason"] == "tokens"
    assert record["src_sha1"] == sha1("Hello world")
    assert record["en"] == "Hello world"
    assert RejectLog(path).contains("k", "Hello world")
    assert not RejectLog(path).contains("k", "Hello world, changed")


# -- review finding (small 1): a torn last line must not swallow the next record ------
@pytest.mark.parametrize("make", [
    lambda p: TranslationCache(p).put("good", "Reform the army", "Die Armee reformieren", "m"),
    lambda p: RejectLog(p).add("good", "Reform the army", "tokens"),
    lambda p: UnchangedLog(p).add("good", "Reform the army", "m"),
])
def test_append_after_torn_last_line_starts_a_new_line(tmp_path, make):
    path = tmp_path / "log.jsonl"
    path.write_text('{"key": "a", "src_sha1": "00", "de": "A"}\n{"key": "b", "src_sh', encoding="utf-8")
    make(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["key"] == "good"
    assert lines[-2] == '{"key": "b", "src_sh'


def test_torn_line_heals_for_the_cache_after_restart(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"key": "b", "src_sh', encoding="utf-8")
    TranslationCache(path).put("good", "Reform the army", "Die Armee reformieren", "m")
    reloaded = TranslationCache(path)
    assert reloaded.get("good", "Reform the army") == "Die Armee reformieren"
    assert reloaded.skipped == 1


# -- review finding (small 7): only one translate run at a time ------------------------
def test_run_lock_is_exclusive(tmp_path):
    lock = tmp_path / "cache" / "translate.lock"
    with run_lock(lock):
        with pytest.raises(LockBusy):
            with run_lock(lock):
                pass
    with run_lock(lock):  # released again
        pass


# -- review 3, K3: one line per text; the "translatable" mark survives a reload ---------
def test_unchanged_log_keeps_one_line_per_text(tmp_path):
    path = tmp_path / "unchanged.jsonl"
    log = UnchangedLog(path)
    log.add("k", "Toggle", "m1")
    log.add("k", "Toggle", "m2")
    UnchangedLog(path).add("k", "Toggle", "m1", translatable=True)
    assert len(path.read_text().splitlines()) == 1


def test_unchanged_log_remembers_the_translatable_mark(tmp_path):
    path = tmp_path / "unchanged.jsonl"
    UnchangedLog(path).add("k", "Toggle the decisions", "m1", translatable=True)
    UnchangedLog(path).add("o", "Opposition", "m1")
    log = UnchangedLog(path)
    assert log.is_translatable("k", "Toggle the decisions")
    assert not log.is_translatable("o", "Opposition")
    assert not log.is_translatable("k", "changed English text")
