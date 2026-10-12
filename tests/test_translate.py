"""Translation runner: batching, cache use, budget, second round (offline)."""
from __future__ import annotations

import hashlib
import json
import re
import signal
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fakes import FakeSleep, FakeTransport, completion, error

from mdde.cache import RejectLog, TranslationCache, UnchangedLog
from mdde.openrouter import Client
from mdde.translate import (
    RunOptions,
    Runner,
    build_system_prompt,
    load_glossary,
    make_batches,
    sigterm_as_interrupt,
)

KEY = "sk-or-v1-runner-test-key"
REPO = Path(__file__).resolve().parents[1]

TABLE = {
    "German Unity": "Deutsche Einheit",
    "§YWar§! is coming": "§YKrieg§! kommt",
    "Reform the army": "Die Armee reformieren",
    "Build more factories": "Mehr Fabriken bauen",
    "Same text": "Gleicher Text",
}


def quota_payload(remaining):
    return 200, json.dumps({"data": {"free_model_daily_requests": {"limit": 1000, "remaining": remaining}}}).encode()


class Env:
    """Wires a real Client + Runner to a fake transport inside tmp_path."""

    def __init__(self, tmp_path, responder=None, quotas=(1000,), workers=1, **opts):
        self.tmp = tmp_path
        self.quotas = list(quotas)
        self.responder = responder or translator(TABLE)
        self.transport = FakeTransport(by_model={"m1": self._chat})
        self.sleep = FakeSleep()
        self.client = Client(KEY, ["m1"], system_prompt="SYS", transport=self._route, sleep=self.sleep)
        self.cache = TranslationCache(tmp_path / "cache" / "translations.jsonl")
        self.rejects = RejectLog(tmp_path / "cache" / "rejects.jsonl")
        self.now = datetime(2026, 10, 12, 22, 0, 0, tzinfo=timezone.utc)
        self.out: list[str] = []
        self.runner = Runner(
            self.client, self.cache, self.rejects,
            RunOptions(workers=workers, **opts),
            log_dir=tmp_path / "logs", sleep=self.sleep, now=lambda: self.now, out=self.out.append,
        )

    def _route(self, method, url, headers, body, timeout):
        if method == "GET":
            self.quota_calls = getattr(self, "quota_calls", 0) + 1
            return quota_payload(self.quotas.pop(0) if len(self.quotas) > 1 else self.quotas[0])
        return self.transport(method, url, headers, body, timeout)

    def _chat(self, payload):
        return self.responder(payload)

    @property
    def chat_calls(self):
        return self.transport.calls

    def sent_texts(self):
        texts = []
        for call in self.chat_calls:
            texts.extend(json.loads(call.body["messages"][1]["content"]).values())
        return texts


def translator(table, bad_first=(), always_bad=(), omit=()):
    seen = Counter()

    def respond(payload):
        batch = json.loads(payload["messages"][1]["content"])
        out = {}
        for i, en in batch.items():
            seen[en] += 1
            if en in omit:
                continue
            if en in always_bad or (en in bad_first and seen[en] == 1):
                out[i] = "kaputt ohne Tokens"
            else:
                out[i] = table.get(en, "Übersetzt")
        return completion(out)

    return respond


# -- batching ------------------------------------------------------------------
def units(n, text="x"):
    return [(f"k{i}", text) for i in range(n)]


def test_batches_respect_item_limit():
    assert [len(b) for b in make_batches(units(250), 9000, 100)] == [100, 100, 50]


def test_batches_respect_char_limit():
    big = [(f"k{i}", "a" * 4000) for i in range(5)]
    assert [len(b) for b in make_batches(big, 9000, 100)] == [2, 2, 1]


def test_char_limit_is_inclusive():
    exact = [(f"k{i}", "a" * 4500) for i in range(4)]
    assert [len(b) for b in make_batches(exact, 9000, 100)] == [2, 2]


def test_oversized_text_gets_its_own_batch():
    mixed = [("a", "x" * 10), ("b", "y" * 12000), ("c", "z" * 10)]
    assert [[k for k, _ in b] for b in make_batches(mixed, 9000, 100)] == [["a"], ["b"], ["c"]]


# -- running -------------------------------------------------------------------
def test_valid_results_are_cached_with_model(tmp_path):
    env = Env(tmp_path)
    report = env.runner.run([("GER_a", "German Unity"), ("GER_b", "§YWar§! is coming")])
    assert env.cache.get("GER_a", "German Unity") == "Deutsche Einheit"
    assert env.cache.get("GER_b", "§YWar§! is coming") == "§YKrieg§! kommt"
    assert report.translated == 2
    assert report.requests == 1
    record = json.loads((tmp_path / "cache" / "translations.jsonl").read_text().splitlines()[0])
    assert record["model"] == "m1"


def test_ids_are_per_batch_and_start_at_zero(tmp_path):
    env = Env(tmp_path, batch_items=2)
    env.runner.run([("a", "German Unity"), ("b", "Reform the army"), ("c", "Build more factories")])
    keys = [list(json.loads(c.body["messages"][1]["content"])) for c in env.chat_calls]
    assert keys == [["0", "1"], ["0"]]


def test_cached_texts_are_not_sent_again(tmp_path):
    env = Env(tmp_path)
    env.cache.put("GER_a", "German Unity", "Deutsche Einheit", "old")
    report = env.runner.run([("GER_a", "German Unity"), ("GER_b", "Reform the army")])
    assert env.sent_texts() == ["Reform the army"]
    assert report.already_cached == 1


def test_identical_english_is_sent_once_and_cached_for_every_key(tmp_path):
    env = Env(tmp_path)
    env.runner.run([("k1", "Same text"), ("k2", "Same text")])
    assert env.sent_texts() == ["Same text"]
    assert env.cache.get("k1", "Same text") == env.cache.get("k2", "Same text") == "Gleicher Text"


def test_nothing_to_do_makes_no_requests(tmp_path):
    env = Env(tmp_path)
    env.cache.put("k", "German Unity", "Deutsche Einheit", "old")
    report = env.runner.run([("k", "German Unity")])
    assert env.chat_calls == []
    assert report.translated == 0


def test_invalid_texts_get_a_second_round_in_small_batches(tmp_path):
    texts = [(f"k{i}", f"Text number {i} §Ywith§! colour") for i in range(25)]
    table = {en: f"Text Nummer {i} §Ymit§! Farbe" for i, (_, en) in enumerate(texts)}
    env = Env(tmp_path, responder=translator(table, bad_first=set(table)))
    report = env.runner.run(texts)
    sizes = [len(json.loads(c.body["messages"][1]["content"])) for c in env.chat_calls]
    assert sizes == [25, 10, 10, 5]
    assert report.translated == 25
    assert report.rejected == 0
    assert env.cache.get("k3", "Text number 3 §Ywith§! colour") == "Text Nummer 3 §Ymit§! Farbe"


def test_still_invalid_after_second_round_goes_to_rejects(tmp_path):
    env = Env(tmp_path, responder=translator(TABLE, always_bad={"§YWar§! is coming"}))
    report = env.runner.run([("a", "German Unity"), ("b", "§YWar§! is coming")])
    assert report.translated == 1
    assert report.rejected == 1
    assert env.cache.get("b", "§YWar§! is coming") is None
    record = json.loads((tmp_path / "cache" / "rejects.jsonl").read_text().splitlines()[0])
    assert (record["key"], record["reason"]) == ("b", "tokens")


def test_missing_ids_in_answer_are_retried(tmp_path):
    seen = Counter()

    def responder(payload):
        batch = json.loads(payload["messages"][1]["content"])
        seen["calls"] += 1
        return completion({i: TABLE[en] for i, en in batch.items() if seen["calls"] > 1 or i == "0"})

    env = Env(tmp_path, responder=responder)
    report = env.runner.run([("a", "German Unity"), ("b", "Reform the army")])
    assert report.translated == 2
    assert env.sent_texts() == ["German Unity", "Reform the army", "Reform the army"]


def test_failed_batches_are_retried_but_never_rejected(tmp_path):
    env = Env(tmp_path, responder=lambda payload: error(500))
    report = env.runner.run([("a", "German Unity")])
    assert report.translated == 0
    assert report.rejected == 0
    assert report.left_open == 1
    assert not (tmp_path / "cache" / "rejects.jsonl").exists()
    assert len(env.chat_calls) == 6  # 3 rounds in round one + 3 in round two


def test_known_rejects_are_skipped_unless_requested(tmp_path):
    env = Env(tmp_path)
    env.rejects.add("b", "Reform the army", "tokens")
    env.runner.run([("a", "German Unity"), ("b", "Reform the army")])
    assert env.sent_texts() == ["German Unity"]

    env2 = Env(tmp_path / "again", retry_rejects=True)
    env2.rejects.add("b", "Reform the army", "tokens")
    env2.runner.run([("b", "Reform the army")])
    assert env2.sent_texts() == ["Reform the army"]


# -- budget --------------------------------------------------------------------
def five_texts():
    return [(f"k{i}", en) for i, en in enumerate(TABLE)]


def test_max_requests_is_a_hard_limit(tmp_path):
    env = Env(tmp_path, batch_items=1, max_requests=3)
    report = env.runner.run(five_texts())
    assert len(env.chat_calls) == 3
    assert report.stopped == "max-requests"
    assert report.translated == 3
    assert report.rejected == 0


def test_quota_at_or_below_reserve_stops_before_any_request(tmp_path):
    env = Env(tmp_path, quotas=(100,), reserve=100)
    report = env.runner.run(five_texts())
    assert env.chat_calls == []
    assert report.stopped == "quota"


def test_quota_is_rechecked_every_n_requests(tmp_path):
    # start, after the first request (measures other programs), then every 2
    env = Env(tmp_path, quotas=(20, 19, 17, 1), reserve=5, quota_every=2, batch_items=1)
    report = env.runner.run(sentences(10))
    assert len(env.chat_calls) == 5
    assert env.quota_calls == 4
    assert report.stopped == "quota"


def test_wait_for_reset_sleeps_until_midnight_utc(tmp_path):
    env = Env(tmp_path, quotas=(0, 900), reserve=100, wait_for_reset=True)
    report = env.runner.run([("a", "German Unity")])
    assert env.sleep.calls == [2 * 3600 + 60]
    assert report.translated == 1
    assert report.stopped is None


def test_daily_limit_429_stops_the_run(tmp_path):
    env = Env(tmp_path, responder=lambda p: error(429, "free-models-per-day exceeded"), batch_items=1)
    report = env.runner.run([("a", "German Unity"), ("b", "Reform the army")])
    assert report.stopped == "quota"
    assert report.rejected == 0
    assert len(env.chat_calls) == 1


def test_parallel_workers_lose_nothing(tmp_path):
    texts = [(f"k{i}", f"Sentence {i}") for i in range(60)]
    env = Env(tmp_path, workers=3, batch_items=4)
    report = env.runner.run(texts)
    assert report.translated == 60
    lines = (tmp_path / "cache" / "translations.jsonl").read_text().splitlines()
    assert len(lines) == 60
    assert len(TranslationCache(tmp_path / "cache" / "translations.jsonl")) == 60


# -- logs and prompt -------------------------------------------------------------
def test_run_writes_progress_log_and_summary_without_key(tmp_path):
    env = Env(tmp_path)
    env.runner.run([("a", "German Unity")])
    summary = json.loads((tmp_path / "logs" / "last_run.json").read_text())
    assert summary["translated"] == 1
    assert summary["requests"] == 1
    log = (tmp_path / "logs" / "translate.log").read_text()
    assert log.strip()
    assert env.out
    for text in (log, json.dumps(summary), "\n".join(env.out)):
        assert KEY not in text


def test_system_prompt_contains_rules_and_glossary():
    prompt = build_system_prompt({"National Focus": "Nationaler Schwerpunkt", "Stability": "Stabilität"})
    assert "National Focus = Nationaler Schwerpunkt" in prompt
    assert "Stability = Stabilität" in prompt
    assert "JSON" in prompt
    assert "§Y" in prompt and "$NAME$" in prompt


def test_repository_glossary_is_usable():
    glossary = load_glossary(REPO / "tools" / "glossary.json")
    assert len(glossary) >= 25
    assert all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in glossary.items())
    assert glossary["National Focus"] == "Nationaler Schwerpunkt"


@pytest.mark.parametrize("bad", ["[1, 2]", '{"a": 1}', "{kaputt"])
def test_invalid_glossary_is_rejected(tmp_path, bad):
    path = tmp_path / "g.json"
    path.write_text(bad)
    with pytest.raises(ValueError):
        load_glossary(path)


# -- review finding W2: unchanged answers and shifted packages ------------------------
def test_unchanged_answer_goes_to_unchanged_log_not_cache(tmp_path):
    env = Env(tmp_path, responder=translator({"Saudi Aramco": "Saudi Aramco"}))
    report = env.runner.run([("k1", "Saudi Aramco"), ("k2", "German Unity")])
    assert env.cache.get("k1", "Saudi Aramco") is None
    assert env.cache.get("k2", "German Unity") == "Übersetzt"
    assert report.unchanged == 1
    assert report.translated == 1
    assert report.rejected == 0
    assert env.sent_texts() == ["Saudi Aramco", "German Unity"]  # no second round
    record = json.loads((tmp_path / "cache" / "unchanged.jsonl").read_text().splitlines()[0])
    assert (record["key"], record["model"]) == ("k1", "m1")
    assert record["src_sha1"] == hashlib.sha1("Saudi Aramco".encode()).hexdigest()


def test_unchanged_texts_are_skipped_next_time_unless_requested(tmp_path):
    first = Env(tmp_path, responder=translator({"Saudi Aramco": "Saudi Aramco"}))
    first.runner.run([("k1", "Saudi Aramco")])
    again = Env(tmp_path)
    report = again.runner.run([("k1", "Saudi Aramco")])
    assert again.chat_calls == []
    assert report.skipped_unchanged == 1
    retry = Env(tmp_path, retry_unchanged=True)
    retry.runner.run([("k1", "Saudi Aramco")])
    assert retry.sent_texts() == ["Saudi Aramco"]


SHIFT_TEXTS = [
    "§YGerman Unity§! is the declared goal of our government",
    "Reform the army and raise $COUNT$ new divisions for the defence of the nation",
    "Build more factories in [ROOT.GetName] to strengthen the economy",
    "Short text one",
    "Another short text",
]
SHIFT_TABLE = {
    SHIFT_TEXTS[0]: "§YDeutsche Einheit§! ist das erklärte Ziel unserer Regierung",
    SHIFT_TEXTS[1]: "Die Armee reformieren und $COUNT$ neue Divisionen zur Verteidigung der Nation aufstellen",
    SHIFT_TEXTS[2]: "Mehr Fabriken in [ROOT.GetName] bauen, um die Wirtschaft zu stärken",
    SHIFT_TEXTS[3]: "Kurzer Text eins",
    SHIFT_TEXTS[4]: "Noch ein kurzer Text",
}


def test_shifted_answers_discard_the_whole_package(tmp_path):
    calls = Counter()

    def responder(payload):
        batch = json.loads(payload["messages"][1]["content"])
        calls["n"] += 1
        texts = [batch[str(i)] for i in range(len(batch))]
        if calls["n"] == 1:  # answer i belongs to text i+1, the last id is missing
            return completion({str(i): SHIFT_TABLE[texts[i + 1]] for i in range(len(texts) - 1)})
        return completion({str(i): SHIFT_TABLE[en] for i, en in enumerate(texts)})

    env = Env(tmp_path, responder=responder)
    report = env.runner.run([(f"k{i}", en) for i, en in enumerate(SHIFT_TEXTS)])
    for i, en in enumerate(SHIFT_TEXTS):
        assert env.cache.get(f"k{i}", en) == SHIFT_TABLE[en]
    assert report.translated == 5
    assert report.rejected == 0


def test_three_length_violations_discard_the_whole_package(tmp_path):
    texts = [
        "The first long sentence about the reform of the national army and navy",
        "The second long sentence about the economy, factories and the national debt",
        "The third long sentence about elections, parties and the parliament today",
        "The fourth long sentence about diplomacy, alliances and our neighbours",
    ]
    good = {en: f"Der {n}. lange Satz über die Reform, die Wirtschaft und das Parlament heute"
            for n, en in enumerate(texts, 1)}
    calls = Counter()

    def responder(payload):
        batch = json.loads(payload["messages"][1]["content"])
        calls["n"] += 1
        if calls["n"] == 1:
            return completion({"0": "Kurz", "1": "Kurz", "2": "Kurz", "3": "Falsche, aber unauffällig lange Antwort zum vierten Satz"})
        return completion({i: good[en] for i, en in batch.items()})

    env = Env(tmp_path, responder=responder)
    env.runner.run([(f"k{i}", en) for i, en in enumerate(texts)])
    assert env.cache.get("k3", texts[3]) == good[texts[3]]


# -- review 2, W-2: unchanged answers are no neighbour hits ------------------------------
TREATY = ["Abu Dhabi National Oil Company Exploration Division",
          "Kuwait Petroleum Corporation International Holdings",
          "Saudi Arabian Mining Company Phosphate Operations"]


def test_unchanged_answers_do_not_count_as_neighbour_hits():
    assert not Runner._suspect(TREATY, list(TREATY))


def test_package_with_several_unchanged_answers_is_kept_in_one_request(tmp_path):
    units = [(f"t{i}", en) for i, en in enumerate(TREATY)] + [("a", "German Unity")]
    env = Env(tmp_path, responder=translator({**{en: en for en in TREATY}, **TABLE}))
    report = env.runner.run(units)
    assert len(env.chat_calls) == 1
    assert report.shifted_packages == 0
    assert report.unchanged == 3
    assert env.cache.get("a", "German Unity") == "Deutsche Einheit"


# -- review 2, k-2 / k-5: length alignment and a telling discard log -----------------------
SHORT = {  # all shorter than 40 characters: neither the length nor the neighbour rule applies
    "Build new roads": "Neue Straßen bauen",
    "Strengthen ties with all our neighbours": "Die Beziehungen zu allen Nachbarn stärken",
    "Raise the taxes": "Steuern erhöhen",
    "Invest heavily in renewable energies": "Stark in erneuerbare Energien investieren",
    "Hold free elections": "Freie Wahlen abhalten",
    "Modernise our whole railway network": "Das gesamte Eisenbahnnetz modernisieren",
    "Cut the defence budget": "Den Verteidigungshaushalt kürzen",
    "Expand the national research programme": "Das nationale Forschungsprogramm ausbauen",
}


def test_answers_shifted_by_one_are_found_by_their_lengths():
    texts, good = list(SHORT), list(SHORT.values())
    assert not Runner._suspect(texts, good)
    assert Runner._suspect(texts, good[1:] + [None])
    assert Runner._suspect(texts, [None] + good[:-1])


def shifted_once(table):
    calls = Counter()

    def responder(payload):
        batch = json.loads(payload["messages"][1]["content"])
        texts = [batch[str(i)] for i in range(len(batch))]
        calls["n"] += 1
        if calls["n"] == 1:
            return completion({str(i): table[texts[i + 1]] for i in range(len(texts) - 1)})
        return completion({str(i): table[en] for i, en in enumerate(texts)})

    return responder


def test_short_shifted_answers_never_reach_the_cache(tmp_path):
    env = Env(tmp_path, responder=shifted_once(SHORT))
    report = env.runner.run([(f"k{i}", en) for i, en in enumerate(SHORT)])
    for i, (en, de) in enumerate(SHORT.items()):
        assert env.cache.get(f"k{i}", en) == de
    assert report.shifted_packages == 1


def test_discard_log_names_reason_count_and_an_example_key(tmp_path):
    env = Env(tmp_path, responder=shifted_once(SHIFT_TABLE))
    env.runner.run([(f"k{i}", en) for i, en in enumerate(SHIFT_TEXTS)])
    line = next(line for line in env.out if "verworfen" in line)
    assert re.search(r"Grund Nachbar, \d+ Treffer, z\. B\. k\d", line), line


def test_discard_log_names_the_length_alignment(tmp_path):
    env = Env(tmp_path, responder=shifted_once(SHORT))
    env.runner.run([(f"k{i}", en) for i, en in enumerate(SHORT)])
    line = next(line for line in env.out if "verworfen" in line)
    assert re.search(r"Grund Verschiebung, \d+ Treffer, z\. B\. k\d", line), line


# -- review 2, W-3: doubly escaped answers are repaired, not rejected -----------------------
def test_doubly_escaped_answers_are_repaired_and_cached(tmp_path):
    table = {"Line one\\nLine two": "Zeile eins\\\\nZeile zwei",
             'He said \\"no\\"': 'Er sagte \\\\"nein\\\\"'}
    env = Env(tmp_path, responder=translator(table))
    report = env.runner.run([("a", "Line one\\nLine two"), ("b", 'He said \\"no\\"')])
    assert env.cache.get("a", "Line one\\nLine two") == "Zeile eins\\nZeile zwei"
    assert env.cache.get("b", 'He said \\"no\\"') == 'Er sagte \\"nein\\"'
    assert report.rejected == 0
    assert report.repaired_escapes == 2
    assert len(env.chat_calls) == 1


# -- review 2, W-4: unchanged but translatable answers get a second chance ----------------
TOGGLE = "§YToggle Cheat Decisions§!"


def two_models(tmp_path, m1, m2, **opts):
    """Runner over a client with the chain m1 -> m2; ``m1``/``m2`` answer a batch dict."""
    calls = []

    def route(method, url, headers, body, timeout):
        if method == "GET":
            return quota_payload(1000)
        payload = json.loads(body)
        batch = json.loads(payload["messages"][1]["content"])
        calls.append((payload["model"], sorted(batch.values())))
        return completion((m1 if payload["model"] == "m1" else m2)(batch))

    client = Client(KEY, ["m1", "m2"], system_prompt="S", transport=route, sleep=FakeSleep())
    cache = TranslationCache(tmp_path / "cache" / "translations.jsonl")
    runner = Runner(client, cache, RejectLog(tmp_path / "cache" / "rejects.jsonl"), RunOptions(workers=1, **opts),
                    log_dir=tmp_path / "logs", sleep=FakeSleep(), out=lambda line: None)
    return runner, cache, calls


def echo(batch):
    return dict(batch)


def german(batch):
    return {i: "§YCheat-Entscheidungen umschalten§!" if en == TOGGLE else f"Deutsch {en}"
            for i, en in batch.items()}


def test_translatable_unchanged_answer_is_asked_again_with_the_next_model(tmp_path):
    runner, cache, calls = two_models(tmp_path, echo, german)
    report = runner.run([("tog", TOGGLE), ("opp", "Opposition")])
    assert calls == [("m1", sorted([TOGGLE, "Opposition"])), ("m2", [TOGGLE])]
    assert cache.get("tog", TOGGLE) == "§YCheat-Entscheidungen umschalten§!"
    assert report.translated == 1 and report.unchanged == 1 and report.rejected == 0
    unchanged = UnchangedLog(tmp_path / "cache" / "unchanged.jsonl")
    assert unchanged.contains("opp", "Opposition") and not unchanged.contains("tog", TOGGLE)


def test_translatable_text_unchanged_twice_goes_to_the_unchanged_log(tmp_path):
    runner, cache, calls = two_models(tmp_path, echo, echo)
    report = runner.run([("tog", TOGGLE)])
    assert [model for model, _ in calls] == ["m1", "m2"]
    assert report.unchanged == 1 and report.rejected == 0 and report.left_open == 0
    assert UnchangedLog(tmp_path / "cache" / "unchanged.jsonl").contains("tog", TOGGLE)


@pytest.mark.parametrize("mode,expected", [("translatable", [TOGGLE]), ("all", sorted([TOGGLE, "Opposition"])),
                                           (True, sorted([TOGGLE, "Opposition"])), (False, [])])
def test_retry_unchanged_can_be_limited_to_translatable_texts(tmp_path, mode, expected):
    log = UnchangedLog(tmp_path / "cache" / "unchanged.jsonl")
    log.add("tog", TOGGLE, "m1")
    log.add("opp", "Opposition", "m1")
    runner, cache, calls = two_models(tmp_path, german, german, retry_unchanged=mode)
    runner.run([("tog", TOGGLE), ("opp", "Opposition")])
    assert sorted(en for _, batch in calls for en in batch) == expected


# -- review 3, K3: no second chance -> unchanged log, marked "translatable" ---------------
def unchanged_records(tmp_path):
    path = tmp_path / "cache" / "unchanged.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("batch_items", [1, 2])  # stop inside round one / just before round two
def test_translatable_unchanged_text_is_logged_when_the_run_ends_before_round_two(tmp_path, batch_items):
    runner, cache, calls = two_models(tmp_path, echo, echo, max_requests=1, batch_items=batch_items)
    report = runner.run([("tog", TOGGLE), ("opp", "Opposition")])
    assert [model for model, _ in calls] == ["m1"]
    records = {r["key"]: r for r in unchanged_records(tmp_path)}
    assert records["tog"]["translatable"] is True and records["tog"]["model"] == "m1"
    assert report.unchanged == len(records) and report.left_open == 2 - len(records)
    again, _, calls_again = two_models(tmp_path, echo, echo)
    again.run([("tog", TOGGLE)])
    assert calls_again == []  # before the fix: asked again in every run


def test_translatable_text_rejected_in_round_two_stays_out_of_the_unchanged_log(tmp_path):
    runner, cache, calls = two_models(tmp_path, echo, lambda batch: {i: "kaputt" for i in batch})
    report = runner.run([("tog", TOGGLE)])
    assert [model for model, _ in calls] == ["m1", "m2"]
    assert report.rejected == 1 and report.unchanged == 0 and report.left_open == 0
    assert unchanged_records(tmp_path) == []


def test_translatable_unchanged_text_is_logged_when_the_run_is_interrupted(tmp_path):
    runner, cache, calls = two_models(tmp_path, echo, echo, batch_items=1)
    runner._out = interrupt_on_first_progress(raise_keyboard_interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run([("tog", TOGGLE), ("tog2", "§YToggle the other decisions§!")])
    records = unchanged_records(tmp_path)  # tog2 may have been on the wire already
    assert "tog" in {r["key"] for r in records} and all(r["translatable"] for r in records)
    summary = json.loads((tmp_path / "logs" / "last_run.json").read_text())
    assert summary["unchanged"] == len(records)


def test_retry_unchanged_translatable_writes_no_duplicate_lines(tmp_path):
    for _ in range(3):
        runner, cache, calls = two_models(tmp_path, echo, echo, retry_unchanged="translatable")
        runner.run([("tog", TOGGLE), ("opp", "Opposition")])
    assert sorted(r["key"] for r in unchanged_records(tmp_path)) == ["opp", "tog"]


def test_retry_unchanged_translatable_asks_texts_marked_translatable(tmp_path):
    log = UnchangedLog(tmp_path / "cache" / "unchanged.jsonl")
    log.add("opp", "Opposition", "m1", translatable=True)  # no second chance in its run
    runner, cache, calls = two_models(tmp_path, german, german, retry_unchanged="translatable")
    runner.run([("opp", "Opposition")])
    assert calls == [("m1", ["Opposition"])]


# -- review 2, k-1: bounded split costs and an emergency switch -----------------------------
def moderating(flagged):
    def respond(payload):
        batch = json.loads(payload["messages"][1]["content"])
        if any(f in batch.values() for f in flagged):
            return 403, json.dumps({"error": {"code": 403, "message": "flagged",
                                              "metadata": {"reasons": ["violence"]}}}).encode()
        return completion({i: f"Satz {en}" for i, en in batch.items()})
    return respond


def test_moderation_splits_at_most_two_levels_per_round(tmp_path):
    flagged = "Harmless english sentence number 37"
    units = [(f"k{i}", flagged if i == 37 else f"Unique english sentence number {i}") for i in range(100)]
    env = Env(tmp_path, responder=moderating([flagged]), batch_items=100)
    report = env.runner.run(units)
    # round 1: 100 -> 50 -> 25 (stop, before the fix: down to 1 = 15 requests);
    # round 2: the 25 in tens -> 10 -> 5 -> 3 (stop, these 3 are rejected)
    assert len(env.chat_calls) == 1 + 2 + 2 + 3 + 2 + 2
    assert report.translated == 97
    assert report.rejected == 3
    records = [json.loads(line) for line in (tmp_path / "cache" / "rejects.jsonl").read_text().splitlines()]
    assert {r["reason"] for r in records} == {"moderation"}
    assert any(r["en"] == flagged for r in records)


def long_texts(n):
    return [(f"k{i}", f"The long english sentence number {i} about the reform of the national economy")
            for i in range(n)]


def truncating(batch):
    return {i: "Kurz" for i in batch}


def good_german(batch):
    return {i: f"Der lange deutsche Satz Nummer {en.split()[4]} über die Reform der Volkswirtschaft"
            for i, en in batch.items()}


def test_emergency_switch_window_is_twenty_packages_and_trips_at_fourteen():
    # review 3, K2: 6 of 10 tripped too easily at the real discard rate of 10.7 %
    from mdde.translate import DISCARD_TRIP, DISCARD_WINDOW

    assert (DISCARD_WINDOW, DISCARD_TRIP) == (20, 14)


def test_emergency_switch_changes_the_model_after_fourteen_discards(tmp_path):
    runner, cache, calls = two_models(tmp_path, truncating, good_german, batch_items=12)
    log = []
    runner._out = log.append
    report = runner.run(long_texts(12 * 20))
    assert runner.client.disabled == {"m1"}
    assert [m for m, _ in calls].count("m1") == 14
    assert report.translated == 240 and report.rejected == 0 and report.stopped is None
    assert any("Notschalter" in line and "m1" in line for line in log)


def test_emergency_switch_stops_the_run_when_it_trips_again(tmp_path):
    runner, cache, calls = two_models(tmp_path, truncating, truncating, batch_items=12)
    log = []
    runner._out = log.append
    report = runner.run(long_texts(12 * 30))
    assert report.stopped == "discards"
    assert len(calls) == 14 + 14
    assert any("Notschalter" in line and "gestoppt" in line for line in log)


def test_emergency_switch_stops_at_once_without_another_model(tmp_path):
    env = Env(tmp_path, responder=lambda p: completion(truncating(json.loads(p["messages"][1]["content"]))),
              batch_items=12)
    report = env.runner.run(long_texts(12 * 20))
    assert report.stopped == "discards"
    assert len(env.chat_calls) == 14


def test_emergency_switch_needs_fourteen_of_the_last_twenty_packages(tmp_path):
    runner, cache, calls = two_models(tmp_path, truncating, good_german)
    for discarded in [False] * 7 + [True] * 13:
        runner._note_package("m1", discarded)
    assert runner.client.disabled == set()  # 13 of 20
    runner._note_package("m1", True)  # the oldest (kept) package leaves the window: 14 of 20
    assert runner.client.disabled == {"m1"}


def test_late_answers_of_a_dropped_model_do_not_trip_the_switch_again(tmp_path):
    runner, cache, calls = two_models(tmp_path, truncating, good_german)
    for _ in range(14):
        runner._note_package("m1", True)
    assert runner.client.disabled == {"m1"}
    for _ in range(5):  # still on the wire when m1 was dropped (6 workers)
        runner._note_package("m1", True)
    runner._note_package("m2", True)
    assert not runner.budget.stopped


def shifted_echo(payload):
    """A model that returns the English texts, shifted by one id (it skipped the first)."""
    batch = json.loads(payload["messages"][1]["content"])
    return completion({str(j): batch[str(j + 1)] for j in range(len(batch) - 1)})


def mixed_lengths(n):
    return [(f"k{i}", f"Short english text {i}" if i % 2 else
             f"A much longer english sentence number {i} about the reform of the national economy")
            for i in range(n)]


def test_discarded_packages_of_echoed_english_never_trip_the_emergency_switch(tmp_path):
    # review 3, K2: an echo is an unchanged answer, not a model that shifts translations
    env = Env(tmp_path, responder=shifted_echo, batch_items=12)
    report = env.runner.run(mixed_lengths(12 * 20))
    assert report.shifted_packages >= 20  # every package was discarded ...
    assert report.stopped is None  # ... but none of them counted for the switch
    assert env.client.disabled == set()
    log = (tmp_path / "logs" / "translate.log").read_text()
    assert "Echo" in log and "Notschalter:" not in log


def test_discard_stop_is_a_fatal_exit():
    from mdde.cli import FATAL_STOPS

    assert "discards" in FATAL_STOPS


# -- review finding K1: the reserve is never undercut ---------------------------------
class LiveQuota:
    """Transport whose daily quota really drops by one per chat request.

    ``external`` extra requests of other programs (same key) are counted with
    every chat request; ``lag`` makes ``/key`` report the value from that many
    chat requests ago (a delayed, too high server value).
    """

    def __init__(self, remaining, key_fails_after=None, external=0, lag=0):
        self.remaining = self.lowest = remaining
        self.key_calls = self.posts = 0
        self.key_fails_after = key_fails_after
        self.external, self.lag = external, lag
        self.history = [remaining]
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            if method == "GET":
                self.key_calls += 1
                if self.key_fails_after is not None and self.key_calls > self.key_fails_after:
                    return error(500)
                return quota_payload(self.history[max(0, len(self.history) - 1 - self.lag)])
            self.remaining -= 1 + self.external
            self.posts += 1
            self.history.append(self.remaining)
            self.lowest = min(self.lowest, self.remaining)
        batch = json.loads(json.loads(body)["messages"][1]["content"])
        return completion({i: f"Satz {en}" for i, en in batch.items()})


def live_runner(tmp_path, live, **opts):
    client = Client(KEY, ["m1"], system_prompt="S", transport=live, sleep=FakeSleep())
    opts.setdefault("batch_items", 1)
    return Runner(client, TranslationCache(tmp_path / "t.jsonl"), RejectLog(tmp_path / "r.jsonl"),
                  RunOptions(**opts), log_dir=tmp_path / "logs", sleep=FakeSleep(), out=lambda line: None)


def sentences(n):
    return [(f"k{i}", f"Unique english sentence number {i}") for i in range(n)]


@pytest.mark.parametrize("start,workers", [(101, 1), (120, 1), (150, 1), (150, 3), (180, 6)])
def test_reserve_is_never_undercut(tmp_path, start, workers):
    live = LiveQuota(start)
    report = live_runner(tmp_path, live, workers=workers, reserve=100).run(sentences(120))
    assert live.lowest >= 100
    assert report.stopped == "quota"
    assert live.posts == max(0, start - 100 - workers)


def test_default_quota_check_interval_is_ten_requests():
    assert RunOptions().quota_every == 10


@pytest.mark.parametrize("start", [130, 150, 200, 400])
def test_reserve_holds_when_another_program_uses_the_same_key(tmp_path, start):
    # review 2, W-1: the Discord bot spends one request for each of ours
    live = LiveQuota(start, external=1)
    report = live_runner(tmp_path, live, workers=1, reserve=100).run(sentences(400))
    assert live.lowest >= 100
    assert live.posts > 0
    assert report.stopped == "quota"


@pytest.mark.parametrize("external", [2, 3])
@pytest.mark.parametrize("start", [120, 131, 150, 187, 260, 403])
def test_reserve_holds_when_another_program_is_busier_than_we_are(tmp_path, external, start):
    # simulation E of review 2: the bot sends ~3 requests per request of ours
    live = LiveQuota(start, external=external)
    report = live_runner(tmp_path, live, workers=1, reserve=100).run(sentences(500))
    assert live.lowest >= 100
    assert live.posts > 0
    assert report.stopped == "quota"


@pytest.mark.parametrize("workers", [3, 6])
def test_reserve_holds_with_parallel_workers_and_a_busy_bot(tmp_path, workers):
    live = LiveQuota(300, external=2)
    live_runner(tmp_path, live, workers=workers, reserve=100).run(sentences(500))
    assert live.lowest >= 100
    assert live.posts > 0


def test_close_to_the_reserve_the_quota_is_checked_before_every_request(tmp_path):
    live = LiveQuota(130)
    live_runner(tmp_path, live, workers=1, reserve=100).run(sentences(400))
    # threshold 100 + 1 worker; /key at the start, after the first request, after
    # 10 more and before every request once the estimate is <= 111 (used 19 ... 29)
    assert live.posts == 29
    assert live.key_calls == 1 + 2 + 11


def test_a_delayed_higher_quota_never_raises_the_estimate(tmp_path):
    live = LiveQuota(150, lag=15)
    report = live_runner(tmp_path, live, workers=1, reserve=100).run(sentences(400))
    assert live.lowest >= 100
    assert report.stopped == "quota"


def test_a_new_utc_day_accepts_the_higher_quota(tmp_path):
    from mdde.translate import Budget

    values = [200, 199, 500]
    day = [datetime(2026, 10, 12, 23, 59, tzinfo=timezone.utc)]
    budget = Budget(lambda: {"remaining": values.pop(0)}, RunOptions(workers=1, reserve=100), None,
                    lambda: day[0], lambda line: None)
    budget.start()
    for _ in range(5):
        budget()
    assert budget.estimate() == 195
    day[0] = datetime(2026, 10, 13, 0, 1, tzinfo=timezone.utc)
    budget._check_quota()
    assert budget.estimate() == 500


def test_unknown_quota_stops_before_any_request(tmp_path):
    live = LiveQuota(1000, key_fails_after=0)
    report = live_runner(tmp_path, live, workers=1).run(sentences(5))
    assert live.posts == 0
    assert report.stopped == "quota-unknown"


def test_unknown_quota_can_be_allowed_explicitly(tmp_path):
    live = LiveQuota(1000, key_fails_after=0)
    report = live_runner(tmp_path, live, workers=1, allow_unknown_quota=True).run(sentences(5))
    assert live.posts == 5
    assert report.stopped is None


def test_failing_quota_check_mid_run_keeps_counting_locally(tmp_path):
    live = LiveQuota(130, key_fails_after=1)
    report = live_runner(tmp_path, live, workers=1, reserve=100, quota_every=5).run(sentences(60))
    assert live.lowest >= 100
    assert report.stopped == "quota"


# -- review finding K2: Ctrl+C and SIGTERM stop the run quickly -------------------------
def slow_env(tmp_path, n=300):
    def responder(payload):
        time.sleep(0.005)
        batch = json.loads(payload["messages"][1]["content"])
        return completion({i: f"Satz {en}" for i, en in batch.items()})

    env = Env(tmp_path, responder=responder, workers=3, batch_items=1)
    return env, sentences(n)


def interrupt_on_first_progress(action):
    fired = []

    def out(line):
        if line.startswith("[R1") and not fired:
            fired.append(line)
            action()

    return out


def raise_keyboard_interrupt():
    raise KeyboardInterrupt


def test_ctrl_c_stops_queued_packages_and_writes_summary(tmp_path):
    env, units = slow_env(tmp_path)
    env.runner._out = interrupt_on_first_progress(raise_keyboard_interrupt)
    with pytest.raises(KeyboardInterrupt):
        env.runner.run(units)
    assert len(env.chat_calls) <= 10  # before the fix: all 300
    summary = json.loads((tmp_path / "logs" / "last_run.json").read_text())
    assert summary["stopped"] == "interrupted"
    assert summary["translated"] >= 1


def test_sigterm_behaves_like_ctrl_c(tmp_path):
    env, units = slow_env(tmp_path)
    env.runner._out = interrupt_on_first_progress(lambda: signal.raise_signal(signal.SIGTERM))
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), sigterm_as_interrupt():
        env.runner.run(units)
    assert signal.getsignal(signal.SIGTERM) is before
    assert len(env.chat_calls) <= 10
    assert json.loads((tmp_path / "logs" / "last_run.json").read_text())["stopped"] == "interrupted"


def test_second_interrupt_during_shutdown_still_waits_and_counts_everything(tmp_path):
    # review 2, k-4: Ctrl+C twice - the summary must show what really is in the cache
    def responder(payload):
        time.sleep(0.4)
        batch = json.loads(payload["messages"][1]["content"])
        return completion({i: f"Satz {en}" for i, en in batch.items()})

    env = Env(tmp_path, responder=responder, workers=3, batch_items=1)

    def first_interrupt():
        threading.Timer(0.1, signal.raise_signal, args=(signal.SIGTERM,)).start()  # the second one
        raise KeyboardInterrupt

    env.runner._out = interrupt_on_first_progress(first_interrupt)
    with pytest.raises(KeyboardInterrupt), sigterm_as_interrupt():
        env.runner.run(sentences(30))
    time.sleep(1.0)  # requests that might still be on the wire
    cached = len((tmp_path / "cache" / "translations.jsonl").read_text().splitlines())
    summary = json.loads((tmp_path / "logs" / "last_run.json").read_text())
    assert cached >= 2
    assert summary["translated"] == cached
    assert summary["stopped"] == "interrupted"


def test_stop_wakes_a_worker_waiting_for_rate_limits(tmp_path):
    transport = FakeTransport(by_model={"m1": lambda payload: error(429, "rate-limited upstream")})

    def route(method, url, headers, body, timeout):
        return quota_payload(1000) if method == "GET" else transport(method, url, headers, body, timeout)

    client = Client(KEY, ["m1"], system_prompt="S", transport=route, wait_schedule=(30,))  # real time.sleep
    runner = Runner(client, TranslationCache(tmp_path / "t.jsonl"), RejectLog(tmp_path / "r.jsonl"),
                    RunOptions(workers=1, batch_items=1), log_dir=tmp_path / "logs", out=lambda line: None)
    threading.Timer(0.3, runner.budget.stop, args=("interrupted",)).start()
    started = time.monotonic()
    report = runner.run(sentences(1))
    assert time.monotonic() - started < 5
    assert report.stopped == "interrupted"


def test_stop_wakes_a_run_waiting_for_the_daily_reset(tmp_path):
    live = LiveQuota(0)
    client = Client(KEY, ["m1"], system_prompt="S", transport=live)
    runner = Runner(client, TranslationCache(tmp_path / "t.jsonl"), RejectLog(tmp_path / "r.jsonl"),
                    RunOptions(workers=1, wait_for_reset=True), log_dir=tmp_path / "logs", out=lambda line: None)
    threading.Timer(0.3, runner.budget.stop, args=("interrupted",)).start()
    started = time.monotonic()
    report = runner.run(sentences(1))
    assert time.monotonic() - started < 5
    assert report.stopped == "interrupted"
    assert live.posts == 0


# -- progress line ----------------------------------------------------------------------
def test_progress_line_shows_clock_and_remaining_time(tmp_path):
    env = Env(tmp_path, batch_items=1)
    env.runner.run(sentences(3))
    progress = [line for line in env.out if line.startswith("[R1 ")]
    assert len(progress) == 3
    assert all(re.search(r"\| \d\d:\d\d:\d\d \| Rest ca\. ", line) for line in progress), progress
    started = env.now
    env.now = started + timedelta(seconds=60)
    assert env.runner._progress(1, 1, 3, 0, started).endswith("Rest ca. 2 min")
    env.now = started + timedelta(hours=2)
    assert env.runner._progress(1, 1, 3, 0, started).endswith("Rest ca. 4 h 00 min")


def test_default_run_options_use_six_workers():
    assert RunOptions().workers == 6


# -- review finding W1 in the runner --------------------------------------------------
def test_moderated_text_is_isolated_and_rejected_without_blocking_the_model(tmp_path):
    flagged = "Twelve suicide bombers set off explosions in Casablanca"

    def responder(payload):
        batch = json.loads(payload["messages"][1]["content"])
        if flagged in batch.values():
            return 403, json.dumps({"error": {"code": 403, "message": "flagged",
                                              "metadata": {"reasons": ["violence"]}}}).encode()
        return completion({i: f"Satz {en}" for i, en in batch.items()})

    env = Env(tmp_path, responder=responder, batch_items=10)
    units = [("morocco.400.d", flagged)] + [(f"k{i}", f"Harmless english sentence number {i}") for i in range(9)]
    report = env.runner.run(units)
    assert report.translated == 9
    assert report.rejected == 1
    assert env.client.disabled == set()
    record = json.loads((tmp_path / "cache" / "rejects.jsonl").read_text().splitlines()[0])
    assert (record["key"], record["reason"]) == ("morocco.400.d", "moderation")
    # round 1: 10, 5, 2 (two halvings, then on to round two); round 2: 2, 1 (isolated, rejected)
    assert sum(1 for c in env.chat_calls if flagged in c.body["messages"][1]["content"]) == 5
    assert report.stopped is None


def test_only_rate_limited_models_left_stops_the_run(tmp_path):
    env = Env(tmp_path, responder=lambda p: error(429, "rate-limited upstream"), batch_items=1)
    env.client.max_waits = 2
    report = env.runner.run([("a", "German Unity"), ("b", "Reform the army")])
    assert report.stopped == "no-models"
    assert len(env.chat_calls) == 3
