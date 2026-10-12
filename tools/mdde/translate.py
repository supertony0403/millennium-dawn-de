"""Translation runner: batches open texts, sends them in parallel, caches results.

Flow per run:

1. drop texts already in the cache (or already rejected, unless asked to retry)
   and send every distinct English text only once, even if several keys use it;
2. round one: packages of <= 9000 characters / <= 100 texts, ``workers`` in
   parallel; every valid answer goes to the cache immediately;
3. round two: invalid or failed texts again in packages of 10; texts that are
   still invalid go to the reject log (the English text stays in the game);
4. a budget (``Budget``) guards every request: ``--max-requests``, the free
   daily quota (checked at start and every 50 requests) and the reserve that
   is left for other applications.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import validate
from .cache import RejectLog, TranslationCache
from .openrouter import AuthError, Client, NoModelAvailable, QuotaExhausted

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
    workers: int = 3
    reserve: int = 100
    max_requests: int | None = None
    wait_for_reset: bool = False
    quota_every: int = 50
    batch_chars: int = 9000
    batch_items: int = 100
    retry_items: int = 10
    retry_rejects: bool = False


@dataclass
class RunReport:
    open_total: int = 0
    already_cached: int = 0
    skipped_rejects: int = 0
    todo: int = 0
    todo_texts: int = 0
    translated: int = 0
    unchanged: int = 0
    rejected: int = 0
    left_open: int = 0
    requests: int = 0
    stopped: str | None = None
    quota_remaining: int | None = None
    extra: dict = field(default_factory=dict)


class StopRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget:
    """Request gate installed as ``Client.before_request``."""

    def __init__(self, quota: Callable[[], dict | None], opts: RunOptions,
                 sleep: Callable[[float], None], now: Callable[[], datetime],
                 log: Callable[[str], None]) -> None:
        self._quota, self.opts, self._sleep, self._now, self._log = quota, opts, sleep, now, log
        self._lock = threading.Lock()
        self.used = 0
        self.reason: str | None = None
        self.remaining: int | None = None
        self.generation = 0

    @property
    def stopped(self) -> bool:
        return self.reason is not None

    def stop(self, reason: str) -> None:
        with self._lock:
            self.reason = self.reason or reason

    def start(self) -> None:
        with self._lock:
            self._check_quota()

    def __call__(self) -> None:
        with self._lock:
            if self.reason is None and self.opts.max_requests is not None \
                    and self.used >= self.opts.max_requests:
                self.reason = "max-requests"
            if self.reason is None and self.used and self.used % self.opts.quota_every == 0:
                self._check_quota()
            if self.reason is not None:
                raise StopRun(self.reason)
            self.used += 1

    def on_daily_limit(self, generation: int) -> None:
        """A request hit the daily free limit (HTTP 429 "free-models-per-day")."""
        with self._lock:
            if self.reason is not None or generation != self.generation:
                return
            if not self.opts.wait_for_reset:
                self.reason = "quota"
                return
            self._sleep_until_reset()
            self._check_quota()

    def _check_quota(self) -> None:  # lock held
        for attempt in range(MAX_RESET_WAITS + 1):
            info = self._quota()
            remaining = info.get("remaining") if info else None
            self.remaining = remaining if isinstance(remaining, int) else None
            if self.remaining is None:
                self._log("Tageskontingent unbekannt - fahre ohne Kontingentpruefung fort")
                return
            if self.remaining > self.opts.reserve:
                return
            self._log(f"Tageskontingent {self.remaining} <= Reserve {self.opts.reserve}")
            if not self.opts.wait_for_reset or attempt == MAX_RESET_WAITS:
                self.reason = "quota"
                return
            self._sleep_until_reset()

    def _sleep_until_reset(self) -> None:  # lock held
        now = self._now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = math.ceil((midnight - now).total_seconds()) + 60
        self._log(f"warte {seconds} s bis nach 00:00 UTC auf das neue Kontingent")
        self._sleep(seconds)
        self.generation += 1


class Runner:
    def __init__(self, client: Client, cache: TranslationCache, rejects: RejectLog,
                 opts: RunOptions, *, log_dir: Path, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] | None = None,
                 out: Callable[[str], None] = lambda line: print(line, flush=True)) -> None:
        self.client, self.cache, self.rejects, self.opts = client, cache, rejects, opts
        self.log_dir = Path(log_dir)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._out = out
        self._log_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.budget = Budget(client.quota, opts, sleep, self._now, self.log)
        client.before_request = self.budget
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
    def _process(self, texts: list[str], keys: Mapping[str, list[str]]) -> dict[str, tuple]:
        """Translate one package; returns ``{en: (reason, model, de)}`` for failures."""
        payload = {str(i): en for i, en in enumerate(texts)}
        while True:
            if self.budget.stopped:
                return {en: ("skipped", None, None) for en in texts}
            generation = self.budget.generation
            try:
                result = self.client.translate_batch(payload)
                break
            except StopRun:
                return {en: ("skipped", None, None) for en in texts}
            except QuotaExhausted:
                self.budget.on_daily_limit(generation)
            except (NoModelAvailable, AuthError) as exc:
                self.log(f"Abbruch: {exc}")
                self.budget.stop("auth" if isinstance(exc, AuthError) else "no-models")
        if not result.ok:
            return {en: ("batch", None, None) for en in texts}
        failures: dict[str, tuple] = {}
        for i, en in enumerate(texts):
            de = result.texts.get(str(i))
            reason = validate.check(en, de)
            if reason is not None:
                failures[en] = (reason, result.model, de)
                continue
            for key in keys[en]:
                self.cache.put(key, en, de, result.model or "")
            with self._stats_lock:
                self.report.translated += len(keys[en])
                if de == en:
                    self.report.unchanged += len(keys[en])
        return failures

    def _round(self, number: int, texts: list[str], keys: Mapping[str, list[str]],
               max_items: int) -> dict[str, tuple]:
        batches = make_batches([(en, en) for en in texts], self.opts.batch_chars, max_items)
        failures: dict[str, tuple] = {}
        with ThreadPoolExecutor(max_workers=self.opts.workers) as pool:
            futures = [pool.submit(self._process, [en for en, _ in b], keys) for b in batches]
            for done, future in enumerate(as_completed(futures), 1):
                try:
                    failed = future.result()
                except Exception:
                    self.budget.stop("error")
                    raise
                failures.update(failed)
                bad = sum(1 for f in failed.values() if f[0] not in ("skipped",))
                self.log(
                    f"[R{number} {done}/{len(batches)}] {bad} fehlgeschlagen | "
                    f"{self.report.translated}/{self.report.todo} Texte | "
                    f"{self.client.stats['requests']} Anfragen"
                )
        return failures

    # -- whole run -------------------------------------------------------------
    def run(self, units: Iterable[tuple[str, str]]) -> RunReport:
        started = self._now()
        rep = self.report
        unique = list(dict.fromkeys(units))
        rep.open_total = len(unique)
        keys: dict[str, list[str]] = {}
        for key, en in unique:
            if self.cache.get(key, en) is not None:
                rep.already_cached += 1
            elif not self.opts.retry_rejects and self.rejects.contains(key, en):
                rep.skipped_rejects += 1
            else:
                keys.setdefault(en, []).append(key)
        rep.todo = sum(len(k) for k in keys.values())
        rep.todo_texts = len(keys)
        self.log(f"Start: {rep.open_total} offen, {rep.already_cached} im Cache, "
                 f"{rep.skipped_rejects} bekannte Ablehner, {rep.todo} zu tun "
                 f"({rep.todo_texts} verschiedene Texte)")
        if keys:
            self.budget.start()
            if not self.budget.stopped:
                first = self._round(1, list(keys), keys, self.opts.batch_items)
                retry = [en for en, f in first.items() if f[0] != "skipped"]
                if retry and not self.budget.stopped:
                    second = self._round(2, retry, keys, self.opts.retry_items)
                    for en, (reason, model, de) in second.items():
                        if reason in ("batch", "skipped"):
                            continue
                        for key in keys[en]:
                            self.rejects.add(key, en, reason, model=model, de=de)
                        rep.rejected += len(keys[en])
        rep.left_open = rep.todo - rep.translated - rep.rejected
        rep.requests = self.client.stats["requests"]
        rep.stopped = self.budget.reason
        rep.quota_remaining = self.budget.remaining
        self._write_summary(started)
        self.log(f"Ende: {rep.translated} uebersetzt, {rep.rejected} abgelehnt, "
                 f"{rep.left_open} offen, {rep.requests} Anfragen"
                 + (f", gestoppt: {rep.stopped}" if rep.stopped else ""))
        return rep

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
