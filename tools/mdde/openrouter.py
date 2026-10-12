"""OpenRouter chat-completions client with a model chain.

Error handling (spec "Fehlerbehandlung"):

* 429 -> try the next model immediately; if every model answered 429, wait
  (60 s, growing by 60 s up to 5 min) and run the chain again; after
  ``max_waits`` such waits only permanently limited models are left
  -> ``NoModelAvailable``.
* 429 "free-models-per-day" -> ``QuotaExhausted`` (daily account limit).
* 403 with moderation metadata (``error.metadata.reasons`` /
  ``flagged_input``) -> the *input* was flagged: the batch fails with
  ``BatchResult.moderated`` and the model stays in the chain.
* other 403/404 -> drop the model for the rest of the run.
* 401/402 -> ``AuthError`` (nothing to retry).
* timeout, network error, 5xx, other HTTP codes, unusable body -> next model;
  at most ``max_rounds`` passes over the chain per batch.

The API key is never written to logs, exceptions or ``repr``; keys with
characters other than letters, digits, ``_`` and ``-`` are refused when they
are loaded.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = "https://openrouter.ai/api/v1"
CHAT_URL = f"{API_BASE}/chat/completions"
KEY_URL = f"{API_BASE}/key"

Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float], "tuple[int, bytes]"]

_KEY_FILE_RE = re.compile(r"OPENROUTER(?:_API)?_KEY\s*=\s*[\"']?([^\s\"']+)")
_DAILY_LIMIT_MARKER = "free-models-per-day"
_RATE_LIMITED, _GONE, _TRANSIENT, _MODERATED = "rate_limited", "gone", "transient", "moderated"
_VALID_KEY_RE = re.compile(r"[\w-]+")


class KeyNotFound(RuntimeError):
    pass


class InvalidKey(KeyNotFound):
    """The key contains characters that cannot be sent (line break, space ...)."""


class AuthError(RuntimeError):
    pass


class QuotaExhausted(RuntimeError):
    pass


class NoModelAvailable(RuntimeError):
    pass


def load_api_key(env: Mapping[str, str], key_file: Path) -> str:
    """``OPENROUTER_API_KEY`` from ``env``, else from ``key_file`` (dotenv style).

    Raises ``InvalidKey`` (without the key in the message) if the key contains
    anything but letters, digits, ``_`` and ``-`` - e.g. a line break would
    otherwise end up in an ``Invalid header value`` traceback.
    """
    key = env.get("OPENROUTER_API_KEY", "").strip()
    source = "OPENROUTER_API_KEY"
    if not key:
        try:
            text = Path(key_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise KeyNotFound(f"Schluesseldatei nicht lesbar: {key_file}") from exc
        m = _KEY_FILE_RE.search(text)
        if not m:
            raise KeyNotFound(f"kein OPENROUTER_API_KEY in {key_file}")
        key, source = m.group(1), str(key_file)
    if not _VALID_KEY_RE.fullmatch(key):
        raise InvalidKey(f"API-Schluessel aus {source} ist ungueltig (erlaubt: Buchstaben, Ziffern, _ "
                         f"und -; Zeilenumbruch oder Leerzeichen im Wert?)")
    return key


def _is_moderation(text: str) -> bool:
    """403 body with ``error.metadata.reasons`` or ``flagged_input`` (input flagged)."""
    try:
        data = json.loads(text)
    except ValueError:
        return False
    error = data.get("error") if isinstance(data, dict) else None
    meta = error.get("metadata") if isinstance(error, dict) else None
    return isinstance(meta, dict) and bool(meta.get("reasons") or "flagged_input" in meta)


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:  # noqa: BLE001 - body is optional diagnostics
            payload = b""
        return exc.code, payload


def extract_json_object(content: str, expected_keys: Iterable[str]) -> dict | None:
    """First JSON object in ``content`` that has at least one expected key.

    Reasoning models like to chat before (or after) the JSON, sometimes inside
    a Markdown fence, so every ``{`` is tried as a starting point.
    """
    if not isinstance(content, str):
        return None
    wanted = set(expected_keys)
    decoder = json.JSONDecoder()
    pos = content.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(content, pos)
        except json.JSONDecodeError:
            pos = content.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            if wanted & obj.keys():
                return obj
            for inner in obj.values():  # {"translations": {"0": ...}}
                if isinstance(inner, dict) and wanted & inner.keys():
                    return inner
        pos = content.find("{", end if end > pos else pos + 1)
    return None


@dataclass
class BatchResult:
    texts: dict[str, object] = field(default_factory=dict)
    model: str | None = None
    ok: bool = False
    moderated: bool = False  # the input was flagged (403 with moderation metadata)


class Client:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        *,
        system_prompt: str,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 300,
        max_rounds: int = 3,
        wait_schedule: tuple[int, ...] = (60, 120, 180, 240, 300),
        max_waits: int = 10,
        before_request: Callable[[], None] | None = None,
    ) -> None:
        if not models:
            raise ValueError("Modellkette ist leer")
        self._key = api_key
        self.models = list(models)
        self.system_prompt = system_prompt
        self.transport = transport
        self.sleep = sleep
        self.timeout = timeout
        self.max_rounds = max_rounds
        self.wait_schedule = wait_schedule
        self.max_waits = max_waits
        self.before_request = before_request
        self.stats: Counter[str] = Counter()
        self.disabled: set[str] = set()
        self.errors: list[str] = []
        self.quota_fields: list[str] = []  # field names of the last /key answer (no values)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Client(models={self.models!r}, disabled={sorted(self.disabled)!r})"

    # -- helpers -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.stats[name] += n

    def _note(self, model: str, what: str) -> None:
        with self._lock:
            self.errors.append(self._redact(f"{model}: {what}")[:300])
            del self.errors[:-200]

    # -- chat ----------------------------------------------------------------
    def _attempt(self, model: str, batch: Mapping[str, str]) -> BatchResult | str:
        if self.before_request is not None:
            self.before_request()
        self._count("requests")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(dict(batch), ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "reasoning": {"effort": "low", "exclude": True},
        }
        unsendable = False
        try:
            status, raw = self.transport("POST", CHAT_URL, self._headers(),
                                         json.dumps(body).encode("utf-8"), self.timeout)
        except TimeoutError:
            self._count("timeouts")
            self._note(model, "Timeout")
            return _TRANSIENT
        except (OSError, http.client.HTTPException) as exc:
            self._count("network_errors")
            self._note(model, f"Netzwerkfehler {type(exc).__name__}")
            return _TRANSIENT
        except ValueError:  # e.g. "Invalid header value b'Bearer <key>...'" - never echo it
            unsendable = True
        if unsendable:  # raised outside the except block: no chained exception with the key
            self._count("unsendable")
            self._note(model, "Anfrage nicht sendbar (ungueltiger Header-Wert)")
            raise AuthError("Anfrage nicht sendbar: ungueltiger Header-Wert - API-Schluessel pruefen")

        text = raw.decode("utf-8", errors="replace")
        if status != 200:
            self._count(f"http_{status}")
            self._note(model, f"HTTP {status} {text[:150]}")
            if status == 429:
                if _DAILY_LIMIT_MARKER in text.lower():
                    raise QuotaExhausted("Tageskontingent fuer Gratis-Modelle erschoepft")
                return _RATE_LIMITED
            if status in (401, 402):
                raise AuthError(f"OpenRouter lehnt die Anfrage ab (HTTP {status})")
            if status == 403 and _is_moderation(text):
                self._count("moderated")
                return _MODERATED
            if status in (403, 404):
                with self._lock:
                    self.disabled.add(model)
                return _GONE
            return _TRANSIENT

        try:
            data = json.loads(text)
            if "error" in data:
                raise ValueError("error object")
            usage = data.get("usage") or {}
            self._count("tok_in", int(usage.get("prompt_tokens") or 0))
            self._count("tok_out", int(usage.get("completion_tokens") or 0))
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            self._count("bad_response")
            self._note(model, f"unbrauchbare Antwort {text[:150]}")
            return _TRANSIENT
        obj = extract_json_object(content, batch.keys())
        if obj is None:
            self._count("bad_json")
            self._note(model, "kein JSON-Objekt in der Antwort")
            return _TRANSIENT
        self._count(f"model:{model}")
        return BatchResult({k: v for k, v in obj.items() if k in batch}, model, True)

    def translate_batch(self, batch: Mapping[str, str]) -> BatchResult:
        """Translate ``{id: english}``; values are returned unvalidated."""
        fail_rounds = waits = 0
        while True:
            chain = [m for m in self.models if m not in self.disabled]
            if not chain:
                raise NoModelAvailable("alle Modelle der Kette sind gesperrt (403/404)")
            transient = rate_limited = False
            for model in chain:
                outcome = self._attempt(model, batch)
                if isinstance(outcome, BatchResult):
                    return outcome
                if outcome == _MODERATED:
                    return BatchResult(model=model, moderated=True)
                transient |= outcome == _TRANSIENT
                rate_limited |= outcome == _RATE_LIMITED
            if transient:
                fail_rounds += 1
                if fail_rounds >= self.max_rounds:
                    return BatchResult()
            elif rate_limited:
                if waits >= self.max_waits:
                    raise NoModelAvailable(
                        f"nur noch dauerhaft rate-limitierte Modelle ({', '.join(chain)}) - "
                        f"nach {waits} Wartepausen aufgegeben"
                    )
                self._count("waits")
                self.sleep(self.wait_schedule[min(waits, len(self.wait_schedule) - 1)])
                waits += 1

    # -- account -------------------------------------------------------------
    def quota(self) -> dict | None:
        """``free_model_daily_requests`` of the key (e.g. ``{limit, remaining}``)."""
        try:
            status, raw = self.transport("GET", KEY_URL, self._headers(), None, 30)
            if status != 200:
                return None
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (OSError, http.client.HTTPException, ValueError):
            return None
        node = data.get("data", data) if isinstance(data, dict) else None
        self.quota_fields = sorted(node) if isinstance(node, dict) else []
        found = node.get("free_model_daily_requests") if isinstance(node, dict) else None
        if isinstance(found, dict) and "remaining" in found:
            return dict(found)
        return None
