"""Translation runner: batches open texts, sends them in parallel, caches results.

Flow per run:

1. drop texts already in the cache (or already rejected, unless asked to retry)
   and send every distinct English text only once, even if several keys use it;
2. round one: packages of <= 9000 characters / <= 100 texts, ``workers`` in
   parallel; every valid answer goes to the cache immediately, answers equal
   to the English text go to the unchanged log instead (not requested again
   unless ``retry_unchanged``);
3. a package whose answers look shifted (>= 3 length-ratio violations, or
   >= 2 answers that fit a neighbouring text but not their own) is discarded
   as a whole: big packages go to round two, small ones are split in halves;
4. a package that the provider's content moderation refuses (HTTP 403 with
   moderation metadata) is split in halves until the flagged text is alone;
   that text goes straight to the reject log (reason ``moderation``), the
   model stays in the chain;
   round two: invalid or failed texts again in packages of 10; texts that are
   still invalid go to the reject log (the English text stays in the game);
5. a budget (``Budget``) guards every request: ``--max-requests`` and the
   free daily quota. The quota is queried at the start and every 50 requests
   and counted down locally in between; once the estimate reaches
   ``reserve + workers`` it is queried again and the run stops there, so the
   reserve for other applications is never used up (requests that are still
   in flight are covered by ``workers``). An unknown quota stops the run
   unless ``allow_unknown_quota`` is set;
6. Ctrl+C (and SIGTERM inside ``sigterm_as_interrupt``) stops the run: queued
   packages are cancelled, waiting workers wake up, requests already on the
   wire finish and are cached, ``logs/last_run.json`` is written.
"""
from __future__ import annotations

import json
import math
import signal
import threading
import time
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
FINAL_REASONS = ("moderation",)  # rejected at once, a second round would not help


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
    quota_every: int = 50
    batch_chars: int = 9000
    batch_items: int = 100
    retry_items: int = 10
    retry_rejects: bool = False
    retry_unchanged: bool = False


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
    requests: int = 0
    stopped: str | None = None
    quota_remaining: int | None = None
    extra: dict = field(default_factory=dict)


class StopRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget:
    """Request gate installed as ``Client.before_request``.

    ``_lock`` serialises the accounting of requests and the quota queries;
    ``stop`` never takes it, so a stop request is never blocked by a worker
    that sleeps until the daily reset while holding it.
    """

    def __init__(self, quota: Callable[[], dict | None], opts: RunOptions,
                 sleep: Callable[[float], None] | None, now: Callable[[], datetime],
                 log: Callable[[str], None]) -> None:
        self._quota, self.opts, self._now, self._log = quota, opts, now, log
        self._stopped = threading.Event()
        self._sleep = sleep if sleep is not None else self.wait
        self._lock = threading.Lock()
        self._reason_lock = threading.Lock()
        self.used = 0
        self.reason: str | None = None
        self.remaining: int | None = None  # last value reported by /key
        self._base: int | None = None  # remaining at the last successful query
        self._used_at_check = 0
        self.generation = 0

    @property
    def stopped(self) -> bool:
        return self.reason is not None

    @property
    def threshold(self) -> int:
        """Estimated quota at which the run stops: reserve + requests in flight."""
        return self.opts.reserve + self.opts.workers

    def estimate(self) -> int | None:
        """Quota left: last queried value minus the requests sent since then."""
        if self._base is None:
            return None
        return self._base - (self.used - self._used_at_check)

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
            if not self.stopped and self.opts.max_requests is not None \
                    and self.used >= self.opts.max_requests:
                self.stop("max-requests")
            if not self.stopped:
                estimate = self.estimate()
                due = self.used and self.used % self.opts.quota_every == 0
                if due or (estimate is not None and estimate <= self.threshold):
                    self._check_quota()
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

    def _check_quota(self) -> None:  # lock held
        for attempt in range(MAX_RESET_WAITS + 1):
            if self.stopped:
                return
            info = self._quota()
            remaining = info.get("remaining") if info else None
            if isinstance(remaining, int) and not isinstance(remaining, bool):
                self.remaining = self._base = remaining
                self._used_at_check = self.used
            elif self._base is None:
                if self.opts.allow_unknown_quota:
                    self._log("Tageskontingent unbekannt - fahre ohne Kontingentpruefung fort "
                              "(--allow-unknown-quota)")
                    return
                self._log("Tageskontingent unbekannt (Abfrage /key fehlgeschlagen) - Abbruch, "
                          "damit die Reserve nicht angetastet wird; --allow-unknown-quota erlaubt "
                          "den Lauf ohne Pruefung")
                self.stop("quota-unknown")
                return
            else:
                self._log(f"Kontingent-Abfrage fehlgeschlagen - rechne lokal weiter "
                          f"(geschaetzt {self.estimate()})")
            estimate = self.estimate()
            assert estimate is not None
            if estimate > self.threshold:
                return
            self._log(f"Tageskontingent {estimate} <= Reserve {self.opts.reserve} "
                      f"+ {self.opts.workers} parallele Anfragen")
            if not self.opts.wait_for_reset or attempt == MAX_RESET_WAITS:
                self.stop("quota")
                return
            self._sleep_until_reset()

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

    # -- logging -------------------------------------------------------------
    def log(self, line: str) -> None:
        stamp = self._now().strftime("%Y-%m-%d %H:%M:%S")
        with self._log_lock:
            self._out(line)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / "translate.log", "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {line}\n")

    # -- one package -----------------------------------------------------------
    def _request(self, texts: list[str]) -> BatchResult | None:
        """One package through the client; ``None`` once the run is stopped."""
        payload = {str(i): en for i, en in enumerate(texts)}
        while True:
            if self.budget.stopped:
                return None
            generation = self.budget.generation
            try:
                return self.client.translate_batch(payload)
            except StopRun:
                return None
            except QuotaExhausted:
                self.budget.on_daily_limit(generation)
            except (NoModelAvailable, AuthError) as exc:
                self.log(f"Abbruch: {exc}")
                self.budget.stop("auth" if isinstance(exc, AuthError) else "no-models")

    @staticmethod
    def _suspect(texts: list[str], answers: list[object]) -> bool:
        """True if the answers of a package look shifted against their ids."""
        bad_length = sum(
            1 for en, de in zip(texts, answers) if isinstance(de, str) and not validate.length_ok(en, de)
        )
        if bad_length >= SUSPECT_LENGTH_VIOLATIONS:
            return True
        neighbour_fits = 0
        for i, (en, de) in enumerate(zip(texts, answers)):
            if not isinstance(de, str) or validate.check(en, de) is None:
                continue
            for j in (i - 1, i + 1):
                other = texts[j] if 0 <= j < len(texts) else None
                if (other is not None and other != en and validate.distinctive(other)
                        and validate.check(other, de) is None):
                    neighbour_fits += 1
                    break
        return neighbour_fits >= SUSPECT_NEIGHBOUR_FITS

    def _split(self, texts: list[str], keys: Mapping[str, list[str]]) -> dict[str, tuple]:
        mid = len(texts) // 2
        failures = self._process(texts[:mid], keys)
        failures.update(self._process(texts[mid:], keys))
        return failures

    def _process(self, texts: list[str], keys: Mapping[str, list[str]]) -> dict[str, tuple]:
        """Translate one package; returns ``{en: (reason, model, de)}`` for failures."""
        result = self._request(texts)
        if result is None:
            return {en: ("skipped", None, None) for en in texts}
        if result.moderated:
            if len(texts) == 1:
                self.log(f"Moderation ({result.model}) sperrt einen Text - kommt in die Ablehnungen")
                return {texts[0]: ("moderation", result.model, None)}
            self.log(f"Moderation ({result.model}) sperrt ein Paket mit {len(texts)} Texten - teile auf")
            return self._split(texts, keys)
        if not result.ok:
            return {en: ("batch", None, None) for en in texts}
        answers = [result.texts.get(str(i)) for i in range(len(texts))]
        if len(texts) > 1 and self._suspect(texts, answers):
            with self._stats_lock:
                self.report.shifted_packages += 1
            self.log(f"Paket mit {len(texts)} Texten verworfen ({result.model}): Antworten wirken verschoben")
            if len(texts) > self.opts.retry_items:
                return {en: ("shifted", result.model, None) for en in texts}
            return self._split(texts, keys)
        failures: dict[str, tuple] = {}
        for en, de in zip(texts, answers):
            reason = validate.check(en, de)
            if reason == "unchanged":
                for key in keys[en]:
                    self.unchanged.add(key, en, result.model)
                with self._stats_lock:
                    self.report.unchanged += len(keys[en])
            elif reason is not None:
                failures[en] = (reason, result.model, de)
            else:
                for key in keys[en]:
                    self.cache.put(key, en, de, result.model or "")
                with self._stats_lock:
                    self.report.translated += len(keys[en])
        return failures

    def _round(self, number: int, texts: list[str], keys: Mapping[str, list[str]],
               max_items: int) -> dict[str, tuple]:
        batches = make_batches([(en, en) for en in texts], self.opts.batch_chars, max_items)
        failures: dict[str, tuple] = {}
        started = self._now()
        pool = ThreadPoolExecutor(max_workers=self.opts.workers)
        try:
            futures = [pool.submit(self._process, [en for en, _ in b], keys) for b in batches]
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
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        return failures

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
        for key, en in unique:
            if self.cache.get(key, en) is not None:
                rep.already_cached += 1
            elif not self.opts.retry_rejects and self.rejects.contains(key, en):
                rep.skipped_rejects += 1
            elif not self.opts.retry_unchanged and self.unchanged.contains(key, en):
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
        first = self._round(1, list(keys), keys, self.opts.batch_items)
        self._reject({en: f for en, f in first.items() if f[0] in FINAL_REASONS}, keys)
        retry = [en for en, f in first.items() if f[0] not in ("skipped", *FINAL_REASONS)]
        if not retry or self.budget.stopped:
            return
        second = self._round(2, retry, keys, self.opts.retry_items)
        self._reject({en: f for en, f in second.items() if f[0] not in ("batch", "skipped")}, keys)

    def _reject(self, failures: Mapping[str, tuple], keys: Mapping[str, list[str]]) -> None:
        for en, (reason, model, de) in failures.items():
            for key in keys[en]:
                self.rejects.add(key, en, reason, model=model, de=de)
            self.report.rejected += len(keys[en])

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

