"""Translation runner: batches open texts, sends them in parallel, caches results.

Flow per run:

1. drop texts already in the cache (or already rejected, unless asked to retry)
   and send every distinct English text only once, even if several keys use it;
2. round one: packages of <= 9000 characters / <= 100 texts, ``workers`` in
   parallel; every valid answer goes to the cache immediately, answers equal
   to the English text go to the unchanged log instead (not requested again
   unless ``retry_unchanged`` is ``"all"`` - or ``"translatable"`` for those
   that ``validate.looks_translatable`` or carry the ``translatable`` mark) -
   except texts that obviously still need German: they are asked again in
   round two, with the next model of the chain first, and only an unchanged
   answer there goes to the log. If the run ends before they got that second
   chance (quota, ``--max-requests``, Ctrl+C), they go to the log marked
   ``translatable`` instead of being asked again in every run;
3. a package whose answers look shifted (>= 3 length-ratio violations,
   >= 2 answers that fit a neighbouring text but not their own, or answer
   lengths that match the neighbouring texts far better than their own) is
   discarded as a whole - the log names the rule, the count and an example
   key: big packages go to round two, small ones are split in halves;
4. a package that the content moderation of every model refuses (HTTP 403
   with moderation metadata; the client tries the next model first) is split
   in halves, at most ``MAX_SPLIT_DEPTH`` levels per round: a text found alone
   goes straight to the reject log (reason ``moderation``); the texts of a
   package still refused at the deepest level go to round two (round one) or
   one by one to the reject log (round two). The model stays in the chain;
   round two: invalid or failed texts again in packages of 10; texts that are
   still invalid go to the reject log (the English text stays in the game);
   emergency switch: when ``DISCARD_TRIP`` (14) of the last ``DISCARD_WINDOW``
   (20) packages were discarded as shifted, the model is dropped for the run;
   if it happens again (or no other model is left) the run stops
   (``discards``, exit code 1). A package that only looks shifted because of
   echoed English texts (answers equal to texts of the package) is discarded
   but never counts for the switch - an echo is an unchanged answer;
5. a budget (``Budget``) guards every request: ``--max-requests`` and the
   free daily quota. The quota is queried at the start and every 10 requests
   and counted down locally in between; within ``quota_every`` of
   ``reserve + workers`` it is queried before *every* request, and the run
   stops once the estimate reaches ``reserve + workers`` (requests still in
   flight are covered by ``workers``). Within a quota period a server value
   never raises the local estimate (``/key`` may lag behind); after 00:00 UTC
   a jump of more than ``quota_every + workers`` above it is the daily reset.
   A failed query is retried after ``quota_every`` requests (near the end
   after ``KEY_RETRY_EVERY``), outside the budget lock; the other workers wait
   up to ``KEY_WAIT`` for its answer, so the server cannot count requests of
   ours that look like another program's. Other programs using
   the same key between two queries can still eat into the reserve, so it is
   kept on a best-effort basis ("nach bestem Bemuehen"), not guaranteed. An
   unknown quota stops the run unless ``allow_unknown_quota`` is set;
6. Ctrl+C (and SIGTERM inside ``sigterm_as_interrupt``) stops the run: queued
   packages are cancelled, waiting workers wake up, requests already on the
   wire finish and are cached - a second Ctrl+C does not cut that short -
   and then ``logs/last_run.json`` is written with the real numbers.
"""
from __future__ import annotations

import json
import math
import signal
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import validate
from .cache import RejectLog, TranslationCache, UnchangedLog
from .openrouter import AuthError, BatchResult, Client, NoModelAvailable, QuotaExhausted

SYSTEM_RULES = """Du übersetzt Lokalisierungstexte der Hearts-of-Iron-IV-Mod "Millennium Dawn" (moderne Geopolitik, 2000–2050) aus dem Englischen ins Deutsche.
Regeln:
- Gib NUR ein JSON-Objekt zurück, mit exakt denselben Schlüsseln wie die Eingabe, Werte = deutsche Übersetzung.
- Platzhalter und Formatierung EXAKT unverändert übernehmen: Farbcodes §Y … §! (§ + ein Zeichen, Ende §!), Icons wie £pol_power, Variablen $NAME$, Skriptausdrücke in eckigen Klammern [ROOT.GetName], Zeilenumbrüche \\n, @TAG. Nichts davon übersetzen, verschieben ist erlaubt, löschen oder hinzufügen nicht.
- Keine echten Zeilenumbrüche im Wert; \\n nur dort, wo es im Original steht.
- Eigennamen (Personen, Städte, Parteien, Firmen, Schiffs- und Waffennamen) im Original lassen.
- Natürliches, flüssiges Deutsch im Stil eines Strategiespiels; Du-Ansprache vermeiden, neutral formulieren.
- Doppelte Anführungszeichen im Text als „…“ setzen.
- Verwende die Begriffe der offiziellen deutschen HOI4-Version:"""

MAX_RESET_WAITS = 3
SUSPECT_LENGTH_VIOLATIONS = 3  # length-ratio violations that discard a package
SUSPECT_NEIGHBOUR_FITS = 2  # answers that fit a neighbour instead of their own text
# length alignment, calibrated on Millennium Dawn's 39,018 human pairs and the cache
# (two run orders, packages of 10 and 100, three package boundaries): no false alarm;
# 0.25 instead of 0.45 found more shifts but discarded 5 correct packages
ALIGN_MIN_SOURCE = 15  # characters of both texts compared
ALIGN_MIN_ANSWER = 5
ALIGN_MIN_PAIRS = 4
ALIGN_MIN_OWN_COST = 0.45  # mean |log(length ratio)| against the own texts
ALIGN_RATIO = 0.6  # a neighbour must fit this much better
SUSPICION_LABELS = {"length": "Laenge", "neighbour": "Nachbar", "alignment": "Verschiebung"}
FINAL_REASONS = ("moderation",)  # rejected at once, a second round would not help
MAX_SPLIT_DEPTH = 2  # halving levels per round for a package refused by moderation
# emergency switch: DISCARD_TRIP of the last DISCARD_WINDOW packages discarded. Review 3:
# no false trip up to a 20 % discard rate (the live run saw 10.7 %), a model that shifts
# every package is still dropped after 14 packages
DISCARD_WINDOW = 20
DISCARD_TRIP = 14
KEY_RETRY_EVERY = 5  # near the threshold: own requests between two tries after a failed /key
KEY_FAIL_LOG_EVERY = 10  # log the first failed /key query in a row and every 10th
KEY_WAIT = 1.0  # s other workers wait for a /key answer before they go on without it


def load_glossary(path: Path) -> dict[str, str]:
    """``{english: german}`` from a JSON object; raises ``ValueError`` otherwise."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v for k, v in data.items()
    ):
        raise ValueError(f"{path}: erwartet ein JSON-Objekt Englisch -> Deutsch")
    return data


def build_system_prompt(glossary: Mapping[str, str]) -> str:
    terms = "\n".join(f"  {en} = {de}" for en, de in glossary.items())
    return f"{SYSTEM_RULES}\n{terms}" if terms else SYSTEM_RULES


def make_batches(units: list[tuple[str, str]], max_chars: int, max_items: int) -> list[list[tuple[str, str]]]:
    """Split ``(key, text)`` pairs into packages of <= max_chars / <= max_items."""
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for unit in units:
        length = len(unit[1])
        if current and (size + length > max_chars or len(current) >= max_items):
            batches.append(current)
            current, size = [], 0
        current.append(unit)
        size += length
    if current:
        batches.append(current)
    return batches


@dataclass
class RunOptions:
    workers: int = 6
    reserve: int = 100
    max_requests: int | None = None
    wait_for_reset: bool = False
    allow_unknown_quota: bool = False
    quota_every: int = 10
    batch_chars: int = 9000
    batch_items: int = 100
    retry_items: int = 10
    retry_rejects: bool = False
    retry_unchanged: bool | str = False  # False | "all" (or True) | "translatable"


@dataclass
class RunReport:
    open_total: int = 0
    already_cached: int = 0
    skipped_rejects: int = 0
    skipped_unchanged: int = 0
    todo: int = 0
    todo_texts: int = 0
    translated: int = 0
    unchanged: int = 0
    rejected: int = 0
    left_open: int = 0
    shifted_packages: int = 0
    repaired_escapes: int = 0  # answers with doubled backslashes (\\\\n) repaired
    requests: int = 0
    stopped: str | None = None
    quota_remaining: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Suspicion:
    """Why a package looks shifted: rule, number of hits, index of one example."""

    reason: str  # "length" | "neighbour" | "alignment"
    count: int
    index: int


def _length_cost(texts: list[str], answers: list[object], offset: int) -> tuple[float | None, list[int]]:
    """Mean |log(len(answer) / len(text[i + offset]))| over comparable pairs and their indices."""
    total, used = 0.0, []
    for i, de in enumerate(answers):
        j = i + offset
        if not isinstance(de, str) or not 0 <= j < len(texts):
            continue
        source, own, answer = validate.plain_length(texts[j]), validate.plain_length(texts[i]), validate.plain_length(de)
        if own < ALIGN_MIN_SOURCE or source < ALIGN_MIN_SOURCE or answer < ALIGN_MIN_ANSWER:
            continue
        total += abs(math.log(answer / source))
        used.append(i)
    return (total / len(used) if len(used) >= ALIGN_MIN_PAIRS else None), used


def misaligned(texts: list[str], answers: list[object]) -> Suspicion | None:
    """Answer lengths fit the next (or previous) texts far better than their own."""
    own, _ = _length_cost(texts, answers, 0)
    if own is None or own < ALIGN_MIN_OWN_COST:
        return None
    for offset in (1, -1):
        cost, used = _length_cost(texts, answers, offset)
        if cost is None or cost >= ALIGN_RATIO * own:
            continue
        better = [i for i in used
                  if abs(math.log(validate.plain_length(answers[i]) / validate.plain_length(texts[i + offset])))
                  < abs(math.log(validate.plain_length(answers[i]) / validate.plain_length(texts[i])))]
        return Suspicion("alignment", len(better), better[0] if better else used[0])
    return None


class StopRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget:
    """Request gate installed as ``Client.before_request``.

    ``_lock`` serialises the accounting of requests; ``stop`` never takes it,
    so a stop request is never blocked by a worker that sleeps until the daily
    reset while holding it. A regular ``/key`` query runs *outside* the lock
    (it may hang up to the 30 s timeout): one worker asks, the others wait up
    to ``KEY_WAIT`` for the answer - a request passing the gate meanwhile may
    reach the server before /key is answered and would look like another
    program's (review 4, W1) - and then go on with the local estimate. The
    answer is taken in under the lock - an answer from before a reset wait
    (``generation``) is dropped. Only the start and the queries right after a
    reset wait run under the lock.

    A failed query is retried after ``quota_every`` requests, within
    ``quota_every`` of the stop threshold after ``KEY_RETRY_EVERY``; the log
    names the first and every ``KEY_FAIL_LOG_EVERY``-th failure in a row.

    Other programs may use the same key (the Discord bot). Every query
    compares the server value with what our own requests explain; the
    difference is counted as external requests. Once ``quota_every`` own
    requests are measured, the *external rate* is all external requests over
    all of our own since the run start (or the daily reset) - a quotient, so
    a burst in one short interval is diluted instead of being kept as the
    day's maximum - or, if higher, the same quotient over the latest
    ``quota_every`` own requests, so a busy phase of the other program near
    the end is not averaged away. Before that (a few requests are no
    measurement) the busiest interval so far counts, so a quiet moment right
    after the start cannot hide a busy program. Between two queries the estimate
    subtracts our requests plus the expected external ones, and the stop
    threshold covers the requests in flight together with the external
    requests made meanwhile. The second query comes after the first request,
    so the rate is known early.
    """

    def __init__(self, quota: Callable[[], dict | None], opts: RunOptions,
                 sleep: Callable[[float], None] | None, now: Callable[[], datetime],
                 log: Callable[[str], None]) -> None:
        self._quota, self.opts, self._now, self._log = quota, opts, now, log
        self._stopped = threading.Event()
        self._sleep = sleep if sleep is not None else self.wait
        self._lock = threading.Lock()
        self._answered = threading.Condition(self._lock)  # a /key answer was taken in
        self._reason_lock = threading.Lock()
        self.used = 0
        self.reason: str | None = None
        self.remaining: int | None = None  # last value reported by /key
        self._base: int | None = None  # upper bound of remaining at the last successful query
        self._period_day = None  # UTC date the current quota period was first seen
        self._used_at_check = 0  # ``used`` when the last successful query was sent
        self._last_try = 0  # ``used`` when the last query was sent (successful or not)
        self._failures = 0  # failed queries in a row
        self._querying = False  # a worker is waiting for /key outside the lock
        self._query_until = 0.0  # time.monotonic() until which the others wait for it
        self._own = 0  # own requests between successful queries, since the run start / reset
        self._external = 0  # external requests measured over the same intervals
        # (own, external) of the latest intervals; each has >= 1 own request
        self._intervals: deque[tuple[int, int]] = deque(maxlen=max(1, opts.quota_every))
        self._measured = False  # at least one own request measured
        self.generation = 0

    @property
    def stopped(self) -> bool:
        return self.reason is not None

    @property
    def _rate(self) -> float:
        """External requests per own request: since the run start or the daily reset, or
        over the latest ``quota_every`` own requests if that is higher (never decided by a
        single short interval, never held for the rest of the day). Fewer than
        ``quota_every`` own requests measured: the busiest interval so far."""
        if not self._own:
            return 0.0
        if self._own < self.opts.quota_every:  # all intervals are still in the deque
            return max(external / own for own, external in self._intervals)
        rate = self._external / self._own
        own = external = 0
        for interval_own, interval_external in reversed(self._intervals):
            own, external = own + interval_own, external + interval_external
            if own >= self.opts.quota_every:
                return max(rate, external / own)
        return rate

    @property
    def threshold(self) -> int:
        """Estimated quota at which the run stops: reserve + requests in flight
        (and the external requests expected while they are on the way)."""
        return self.opts.reserve + math.ceil(self.opts.workers * (1 + self._rate))

    def estimate(self) -> int | None:
        """Quota left: last queried value minus our requests since then and the
        requests other programs are expected to have made meanwhile."""
        if self._base is None:
            return None
        sent = self.used - self._used_at_check
        return self._base - sent - math.ceil(sent * self._rate)

    def stop(self, reason: str) -> None:
        with self._reason_lock:
            if self.reason is None:
                self.reason = reason
        self._stopped.set()

    def wait(self, seconds: float) -> None:
        """Sleep that ends early when the run is stopped."""
        self._stopped.wait(seconds)

    def start(self) -> None:
        with self._lock:
            self._check_quota()

    def __call__(self) -> None:
        with self._lock:
            self._check_max_requests()
            self._await_answer()
            query = not self.stopped and self._query_due()
            if query:
                self._querying = True
                self._query_until = time.monotonic() + KEY_WAIT
                self._last_try = at = self.used
                generation = self.generation
        if query:
            info = None
            try:
                info = self._quota()
            finally:
                with self._lock:
                    self._querying = False
                    self._answered.notify_all()
                    if generation == self.generation and not self.stopped:
                        self._take(info, at)
        with self._lock:
            self._check_max_requests()
            self._enforce()
            if self.reason is not None:
                raise StopRun(self.reason)
            self.used += 1

    def on_daily_limit(self, generation: int) -> None:
        """A request hit the daily free limit (HTTP 429 "free-models-per-day")."""
        with self._lock:
            if self.stopped or generation != self.generation:
                return
            if not self.opts.wait_for_reset:
                self.stop("quota")
                return
            self._sleep_until_reset()
            self._check_quota()

    def _check_max_requests(self) -> None:  # lock held
        if not self.stopped and self.opts.max_requests is not None \
                and self.used >= self.opts.max_requests:
            self.stop("max-requests")

    def _await_answer(self) -> None:  # lock held
        """Wait up to ``KEY_WAIT`` for a /key answer on the way: a request passing the gate
        meanwhile may reach the server before /key is answered and would then look like
        another program's request."""
        while self._querying and not self.stopped:
            left = self._query_until - time.monotonic()
            if left <= 0:
                return
            self._answered.wait(left)

    def _query_due(self) -> bool:  # lock held
        """Time for a /key query before the next request?"""
        if self._querying:
            return False  # another worker is asking; go on with the local estimate
        estimate = self.estimate()
        near = estimate is not None and estimate <= self.threshold + self.opts.quota_every
        if self._failures:
            interval = KEY_RETRY_EVERY if near else self.opts.quota_every
        else:
            interval = 1 if near or not self._measured else self.opts.quota_every
        return self.used - self._last_try >= interval

    def _check_quota(self) -> None:  # lock held
        """Query /key under the lock (start, after a reset wait) and enforce the threshold."""
        if self.stopped:
            return
        self._last_try = self.used
        self._take(self._quota(), self.used)
        self._enforce()

    def _take(self, info: dict | None, at: int) -> None:  # lock held
        """Take in a /key answer for a query sent when ``used`` was ``at``."""
        remaining = info.get("remaining") if info else None
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            if at < self._used_at_check:
                return  # overtaken by a newer answer
            if self._failures:
                self._log(f"Kontingent-Abfrage wieder erfolgreich nach {self._failures} Fehlschlaegen")
            self._failures = 0
            self._accept(remaining, at)
            return
        self._failures += 1
        first_or_tenth = self._failures == 1 or self._failures % KEY_FAIL_LOG_EVERY == 0
        if self._base is not None:
            if first_or_tenth:
                self._log(f"Kontingent-Abfrage fehlgeschlagen ({self._failures}x in Folge) - rechne "
                          f"lokal weiter (geschaetzt {self.estimate()}), neuer Versuch nach "
                          f"{self.opts.quota_every} Anfragen, nahe der Reserve nach {KEY_RETRY_EVERY}")
        elif self.opts.allow_unknown_quota:
            if first_or_tenth:
                self._log("Tageskontingent unbekannt - fahre ohne Kontingentpruefung fort "
                          "(--allow-unknown-quota)")
        else:
            self._log("Tageskontingent unbekannt (Abfrage /key fehlgeschlagen) - Abbruch, "
                      "damit die Reserve nicht angetastet wird; --allow-unknown-quota erlaubt "
                      "den Lauf ohne Pruefung")
            self.stop("quota-unknown")

    def _enforce(self) -> None:  # lock held
        """Stop (or wait for the daily reset) once the estimate reaches the threshold."""
        for attempt in range(MAX_RESET_WAITS + 1):
            estimate = self.estimate()
            if self.stopped or estimate is None or estimate > self.threshold:
                return
            self._log(f"Tageskontingent {estimate} <= Reserve {self.opts.reserve} "
                      f"+ {self.threshold - self.opts.reserve} (parallele Anfragen"
                      + (f", Fremdnutzung {self._rate:.1f} je eigene Anfrage)" if self._rate else ")"))
            if not self.opts.wait_for_reset or attempt == MAX_RESET_WAITS:
                self.stop("quota")
                return
            self._sleep_until_reset()
            if self.stopped:
                return
            self._last_try = self.used
            self._take(self._quota(), self.used)

    def _accept(self, remaining: int, at: int) -> None:  # lock held
        """Take a server value. Within a quota period it may only lower the estimate
        (``/key`` may lag behind). After 00:00 UTC a value more than ``quota_every +
        workers`` above what our own requests leave is the daily reset - even when the
        first query of the new day still showed the old value: a new base, and the
        external rate starts from zero."""
        self.remaining = remaining
        day = self._now().date()
        sent = at - self._used_at_check
        bound = None if self._base is None else self._base - sent  # our requests are gone
        if bound is None or (day > self._period_day
                             and remaining > bound + self.opts.quota_every + self.opts.workers):
            if bound is not None:
                self._log(f"Neues Tageskontingent erkannt: {remaining} statt hoechstens {bound} "
                          f"- Fremdnutzung wird neu gemessen")
            self._own = self._external = 0
            self._intervals.clear()
            self._measured = False
            self._period_day = day
        else:
            if sent:
                # a negative difference (requests still on the way, a lagging /key) is
                # carried by the min() below and evens out at the next query
                external = max(0, bound - remaining)
                self._own += sent
                self._external += external
                self._intervals.append((sent, external))
                self._measured = True
            remaining = min(remaining, bound)  # a lagging /key must not raise it
        self._base = remaining
        self._used_at_check = at

    def _sleep_until_reset(self) -> None:  # lock held
        now = self._now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = math.ceil((midnight - now).total_seconds()) + 60
        self._log(f"warte {seconds} s bis nach 00:00 UTC auf das neue Kontingent")
        self._sleep(seconds)
        self._base = None  # the old value belongs to the previous day
        self.generation += 1


@contextmanager
def sigterm_as_interrupt() -> Iterator[None]:
    """Turn SIGTERM into ``KeyboardInterrupt`` so a run stops like on Ctrl+C."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(signal.Signals(signum).name)

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class Runner:
    def __init__(self, client: Client, cache: TranslationCache, rejects: RejectLog,
                 opts: RunOptions, *, log_dir: Path, unchanged: UnchangedLog | None = None,
                 sleep: Callable[[float], None] | None = None,
                 now: Callable[[], datetime] | None = None,
                 out: Callable[[str], None] = lambda line: print(line, flush=True)) -> None:
        self.client, self.cache, self.rejects, self.opts = client, cache, rejects, opts
        self.unchanged = unchanged if unchanged is not None else UnchangedLog(
            cache.path.parent / "unchanged.jsonl"
        )
        self.log_dir = Path(log_dir)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._out = out
        self._log_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.budget = Budget(client.quota, opts, sleep, self._now, self.log)
        client.before_request = self.budget
        if client.sleep is time.sleep:  # real waits for rate limits end on stop
            client.sleep = self.budget.wait
        self.report = RunReport()
        self._recent: deque[bool] = deque(maxlen=DISCARD_WINDOW)  # discarded?, per package
        self._trips = 0
        # translatable texts answered unchanged in round one, until round two settles them:
        # en -> (model, keys)
        self._second_chance: dict[str, tuple[str | None, list[str]]] = {}

    # -- logging -------------------------------------------------------------
    def log(self, line: str) -> None:
        stamp = self._now().strftime("%Y-%m-%d %H:%M:%S")
        with self._log_lock:
            self._out(line)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / "translate.log", "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {line}\n")

    # -- one package -----------------------------------------------------------
    def _request(self, texts: list[str], avoid: str | None = None) -> BatchResult | None:
        """One package through the client; ``None`` once the run is stopped."""
        payload = {str(i): en for i, en in enumerate(texts)}
        while True:
            if self.budget.stopped:
                return None
            generation = self.budget.generation
            try:
                return self.client.translate_batch(payload, avoid=avoid)
            except StopRun:
                return None
            except QuotaExhausted:
                self.budget.on_daily_limit(generation)
            except (NoModelAvailable, AuthError) as exc:
                self.log(f"Abbruch: {exc}")
                self.budget.stop("auth" if isinstance(exc, AuthError) else "no-models")

    @staticmethod
    def _diagnose(texts: list[str], answers: list[object]) -> Suspicion | None:
        """Why the answers of a package look shifted against their ids, if they do."""
        bad_length = [i for i, (en, de) in enumerate(zip(texts, answers))
                      if isinstance(de, str) and not validate.length_ok(en, de)]
        if len(bad_length) >= SUSPECT_LENGTH_VIOLATIONS:
            return Suspicion("length", len(bad_length), bad_length[0])
        neighbour_fits = []
        for i, (en, de) in enumerate(zip(texts, answers)):
            if not isinstance(de, str) or validate.check(en, de) in (None, "unchanged"):
                continue  # fits its own text (an unchanged answer is no neighbour's)
            for j in (i - 1, i + 1):
                other = texts[j] if 0 <= j < len(texts) else None
                if (other is not None and other != en and validate.distinctive(other)
                        and validate.check(other, de) is None):
                    neighbour_fits.append(i)
                    break
        if len(neighbour_fits) >= SUSPECT_NEIGHBOUR_FITS:
            return Suspicion("neighbour", len(neighbour_fits), neighbour_fits[0])
        return misaligned(texts, answers)

    @staticmethod
    def _without_echoes(texts: list[str], answers: list[object]) -> list[object]:
        """``answers`` with English echoes (equal to any text of the package) left out."""
        english = {validate.normalized(en) for en in texts if validate.has_text(en)}
        return [None if isinstance(de, str) and validate.normalized(de) in english else de
                for de in answers]

    @staticmethod
    def _suspect(texts: list[str], answers: list[object]) -> bool:
        """True if the answers of a package look shifted against their ids."""
        return Runner._diagnose(texts, answers) is not None

    def _split(self, texts: list[str], keys: Mapping[str, list[str]], final: bool,
               avoid: str | None, depth: int) -> dict[str, tuple]:
        mid = len(texts) // 2
        failures = self._process(texts[:mid], keys, final, avoid, depth + 1)
        failures.update(self._process(texts[mid:], keys, final, avoid, depth + 1))
        return failures

    def _note_package(self, model: str | None, discarded: bool) -> None:
        """Emergency switch: too many discarded packages -> next model, then stop."""
        with self._stats_lock:
            if model in self.client.disabled:
                return  # late answers of a model the switch already dropped
            self._recent.append(discarded)
            bad = sum(self._recent)
            if bad < DISCARD_TRIP or self.budget.stopped:
                return
            self._recent.clear()
            self._trips += 1
            others = [m for m in self.client.models if m not in self.client.disabled and m != model]
            if self._trips == 1 and model is not None and others:
                self.client.disable(model)
                self.log(f"Notschalter: {bad} der letzten {DISCARD_WINDOW} Pakete verworfen ({model}) - "
                         f"Modell fuer diesen Lauf gesperrt, weiter mit {others[0]}")
                return
        self.log(f"Notschalter: {bad} der letzten {DISCARD_WINDOW} Pakete verworfen ({model}) - "
                 + ("erneut, " if self._trips > 1 else "kein anderes Modell, ")
                 + "Lauf gestoppt. Modellkette und Antworten pruefen (logs/translate.log).")
        self.budget.stop("discards")

    def _process(self, texts: list[str], keys: Mapping[str, list[str]], final: bool = False,
                 avoid: str | None = None, depth: int = 0) -> dict[str, tuple]:
        """Translate one package; returns ``{en: (reason, model, de)}`` for failures.

        ``final`` is the last round: an unchanged answer always goes to the
        unchanged log there; before, a translatable one is a failure (retried).
        """
        result = self._request(texts, avoid)
        if result is None:
            return {en: ("skipped", None, None) for en in texts}
        if result.moderated:
            if len(texts) == 1:
                self.log(f"Moderation ({result.model}) sperrt einen Text - kommt in die Ablehnungen")
                return {texts[0]: ("moderation", result.model, None)}
            if depth >= MAX_SPLIT_DEPTH:
                self.log(f"Moderation ({result.model}) sperrt ein Paket mit {len(texts)} Texten nach "
                         f"{depth} Teilungen - " + ("Texte kommen einzeln in die Ablehnungen" if final
                                                     else "weiter in Runde zwei"))
                return {en: ("moderation-group", result.model, None) for en in texts}
            self.log(f"Moderation ({result.model}) sperrt ein Paket mit {len(texts)} Texten - teile auf")
            return self._split(texts, keys, final, avoid, depth)
        if not result.ok:
            return {en: ("batch", None, None) for en in texts}
        raw = [result.texts.get(str(i)) for i in range(len(texts))]
        answers = [validate.repair_escapes(en, de) for en, de in zip(texts, raw)]
        repaired = sum(1 for a, b in zip(raw, answers) if a is not b)
        suspicion = self._diagnose(texts, answers) if len(texts) > 1 else None
        # shifted echoes of the English texts are unchanged answers, no broken model
        echo = suspicion is not None and self._diagnose(texts, self._without_echoes(texts, answers)) is None
        self._note_package(result.model, suspicion is not None and not echo)
        if suspicion is not None:
            with self._stats_lock:
                self.report.shifted_packages += 1
            self.log(f"Paket mit {len(texts)} Texten verworfen ({result.model}): Antworten wirken verschoben "
                     f"- Grund {SUSPICION_LABELS[suspicion.reason]}, {suspicion.count} Treffer, "
                     f"z. B. {keys[texts[suspicion.index]][0]}"
                     + (" (Echo des Englischen, zaehlt nicht fuer den Notschalter)" if echo else ""))
            if len(texts) > self.opts.retry_items:
                return {en: ("shifted", result.model, None) for en in texts}
            return self._split(texts, keys, final, avoid, depth)
        if repaired:
            with self._stats_lock:
                self.report.repaired_escapes += repaired
        failures: dict[str, tuple] = {}
        for en, de in zip(texts, answers):
            reason = validate.check(en, de)
            if reason == "unchanged" and not final and validate.looks_translatable(en):
                failures[en] = (reason, result.model, de)  # second chance, other model first
                with self._stats_lock:
                    self._second_chance[en] = (result.model, keys[en])
            elif reason == "unchanged":
                for key in keys[en]:
                    self.unchanged.add(key, en, result.model)
                with self._stats_lock:
                    self.report.unchanged += len(keys[en])
                    self._second_chance.pop(en, None)
            elif reason is not None:
                failures[en] = (reason, result.model, de)
            else:
                for key in keys[en]:
                    self.cache.put(key, en, de, result.model or "")
                with self._stats_lock:
                    self.report.translated += len(keys[en])
                    self._second_chance.pop(en, None)
        return failures

    def _round(self, number: int, groups: Mapping[str | None, list[str]], keys: Mapping[str, list[str]],
               max_items: int, final: bool) -> dict[str, tuple]:
        """One round; ``groups`` maps a model to avoid (or ``None``) to its texts."""
        batches = [([en for en, _ in b], avoid) for avoid, texts in groups.items()
                   for b in make_batches([(en, en) for en in texts], self.opts.batch_chars, max_items)]
        failures: dict[str, tuple] = {}
        started = self._now()
        pool = ThreadPoolExecutor(max_workers=self.opts.workers)
        try:
            futures = [pool.submit(self._process, texts, keys, final, avoid) for texts, avoid in batches]
            for done, future in enumerate(as_completed(futures), 1):
                failed = future.result()
                failures.update(failed)
                bad = sum(1 for f in failed.values() if f[0] not in ("skipped",))
                self.log(self._progress(number, done, len(batches), bad, started))
        except BaseException as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            self.budget.stop("interrupted" if interrupted else "error")
            if interrupted:
                self.log("Abbruch angefordert - keine neuen Anfragen mehr, "
                         "warte auf die Antworten, die schon unterwegs sind")
            self._shutdown(pool)
            raise
        pool.shutdown(wait=True)
        return failures

    def _shutdown(self, pool: ThreadPoolExecutor) -> None:
        """Cancel queued packages and wait for those on the wire, even through a
        second Ctrl+C - otherwise the summary would be written while answers
        are still being cached (hard stop: ``kill -9``)."""
        warned = False
        while True:
            try:
                pool.shutdown(wait=True, cancel_futures=True)
                return
            except KeyboardInterrupt:
                if not warned:
                    warned = True
                    self.log("Bitte warten - die Antworten, die schon unterwegs sind, werden noch "
                             "gesichert (weiteres Strg+C wird ignoriert, hart beenden: kill -9)")

    def _progress(self, number: int, done: int, total: int, bad: int, started: datetime) -> str:
        now = self._now()
        elapsed = (now - started).total_seconds()
        rest = format_duration(elapsed / done * (total - done)) if elapsed > 0 else "?"
        return (f"[R{number} {done}/{total}] {bad} fehlgeschlagen | "
                f"{self.report.translated}/{self.report.todo} Texte | "
                f"{self.client.stats['requests']} Anfragen | "
                f"{now.astimezone():%H:%M:%S} | Rest ca. {rest}")

    # -- whole run -------------------------------------------------------------
    def run(self, units: Iterable[tuple[str, str]]) -> RunReport:
        started = self._now()
        rep = self.report
        try:
            self._run(units)
        except BaseException as exc:
            self.budget.stop("interrupted" if isinstance(exc, KeyboardInterrupt) else "error")
            raise
        finally:
            self._log_missed_second_chances()
            rep.left_open = rep.todo - rep.translated - rep.rejected - rep.unchanged
            rep.requests = self.client.stats["requests"]
            rep.stopped = self.budget.reason
            rep.quota_remaining = self.budget.remaining
            self._write_summary(started)
            self.log(f"Ende: {rep.translated} uebersetzt, {rep.unchanged} unveraendert, "
                     f"{rep.rejected} abgelehnt, {rep.left_open} offen, {rep.requests} Anfragen"
                     + (f", gestoppt: {rep.stopped}" if rep.stopped else ""))
        return rep

    def _run(self, units: Iterable[tuple[str, str]]) -> None:
        rep = self.report
        unique = list(dict.fromkeys(units))
        rep.open_total = len(unique)
        keys: dict[str, list[str]] = {}
        mode = self.opts.retry_unchanged
        for key, en in unique:
            if self.cache.get(key, en) is not None:
                rep.already_cached += 1
            elif not self.opts.retry_rejects and self.rejects.contains(key, en):
                rep.skipped_rejects += 1
            elif self.unchanged.contains(key, en) and not (
                    mode is True or mode == "all"
                    or (mode == "translatable" and (validate.looks_translatable(en)
                                                    or self.unchanged.is_translatable(key, en)))):
                rep.skipped_unchanged += 1
            else:
                keys.setdefault(en, []).append(key)
        rep.todo = sum(len(k) for k in keys.values())
        rep.todo_texts = len(keys)
        self.log(f"Start: {rep.open_total} offen, {rep.already_cached} im Cache, "
                 f"{rep.skipped_rejects} bekannte Ablehner, {rep.skipped_unchanged} bekannt unveraendert, "
                 f"{rep.todo} zu tun ({rep.todo_texts} verschiedene Texte), {self.opts.workers} parallel")
        if not keys:
            return
        self.budget.start()
        if self.budget.stopped:
            return
        first = self._round(1, {None: list(keys)}, keys, self.opts.batch_items, final=False)
        self._reject({en: f for en, f in first.items() if f[0] in FINAL_REASONS}, keys)
        retry: dict[str | None, list[str]] = {}
        for en, (reason, model, _) in first.items():
            if reason not in ("skipped", *FINAL_REASONS):
                retry.setdefault(model if reason == "unchanged" else None, []).append(en)
        if not retry or self.budget.stopped:
            return
        second = self._round(2, retry, keys, self.opts.retry_items, final=True)
        self._reject({en: f for en, f in second.items() if f[0] not in ("batch", "skipped")}, keys)

    def _reject(self, failures: Mapping[str, tuple], keys: Mapping[str, list[str]]) -> None:
        for en, (reason, model, de) in failures.items():
            reason = "moderation" if reason == "moderation-group" else reason
            for key in keys[en]:
                self.rejects.add(key, en, reason, model=model, de=de)
            self.report.rejected += len(keys[en])
            self._second_chance.pop(en, None)

    def _log_missed_second_chances(self) -> None:
        """Translatable texts answered unchanged in round one that round two never
        settled (the run ended before it, or their package was skipped or failed
        there) go to the unchanged log marked ``translatable`` - otherwise every
        run would ask them again. ``--retry-unchanged=translatable`` finds them."""
        with self._stats_lock:
            missed, self._second_chance = self._second_chance, {}
        for en, (model, keys) in missed.items():
            for key in keys:
                self.unchanged.add(key, en, model, translatable=True)
            self.report.unchanged += len(keys)
        if missed:
            self.log(f"{len(missed)} unveraenderte, aber uebersetzbar wirkende Texte ohne zweite Runde "
                     f"-> unchanged.jsonl (markiert, --retry-unchanged=translatable fragt sie erneut)")

    def _write_summary(self, started: datetime) -> None:
        summary = asdict(self.report)
        summary.pop("extra", None)
        summary.update(
            started=started.isoformat(),
            finished=self._now().isoformat(),
            client_stats=dict(self.client.stats),
            disabled_models=sorted(self.client.disabled),
            last_errors=self.client.errors[-20:],
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "last_run.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )

