"""Translation runner: batching, cache use, budget, second round (offline)."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fakes import FakeSleep, FakeTransport, completion, error

from mdde.cache import RejectLog, TranslationCache
from mdde.openrouter import Client
from mdde.translate import RunOptions, Runner, build_system_prompt, load_glossary, make_batches

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
    env = Env(tmp_path, quotas=(10, 10, 1), reserve=5, quota_every=2, batch_items=1)
    report = env.runner.run(five_texts())
    assert len(env.chat_calls) == 4
    assert env.quota_calls == 3
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
