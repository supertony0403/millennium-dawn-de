"""Append-only translation cache (``cache/translations.jsonl``), reject log and
the log of texts the model returned unchanged (``cache/unchanged.jsonl``).

Each line is one JSON record ``{key, src_sha1, de, model, ts}``. A cached
translation is only used while the SHA-1 of the *current* English text matches
``src_sha1``, so changed source texts are re-translated automatically. Later
lines win over earlier ones; broken lines (e.g. from a killed process) are
skipped, and the next append starts on a fresh line so it is not swallowed by
a torn one. Writes are serialised with a lock and hit the disk immediately.

``run_lock`` is the inter-process lock (``cache/translate.lock``) that keeps
two ``translate`` runs - or ``revalidate --apply`` and a run - apart.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def src_hash(en: str) -> str:
    return hashlib.sha1(en.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.skipped = 0

    def _records(self):
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    self.skipped += 1
                    continue
                if isinstance(record, dict):
                    yield record
                else:
                    self.skipped += 1

    def _append(self, record: dict) -> None:
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab+") as fh:
                if fh.seek(0, os.SEEK_END):
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":  # torn last line: do not glue onto it
                        data = b"\n" + data
                fh.write(data)  # "a" mode: always at the end
                fh.flush()


class TranslationCache(_JsonlLog):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._data: dict[tuple[str, str], str] = {}
        for record in self._records():
            key, sha, de = record.get("key"), record.get("src_sha1"), record.get("de")
            if isinstance(key, str) and isinstance(sha, str) and isinstance(de, str):
                self._data[(key, sha)] = de
            else:
                self.skipped += 1

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, en: str) -> str | None:
        return self._data.get((key, src_hash(en)))

    def put(self, key: str, en: str, de: str, model: str) -> None:
        sha = src_hash(en)
        self._append({"key": key, "src_sha1": sha, "de": de, "model": model, "ts": _now()})
        with self._lock:
            self._data[(key, sha)] = de


class RejectLog(_JsonlLog):
    """Texts that stayed invalid after the second round (English fallback)."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._known: set[tuple[str, str]] = set()
        for record in self._records():
            key, sha = record.get("key"), record.get("src_sha1")
            if isinstance(key, str) and isinstance(sha, str):
                self._known.add((key, sha))

    def __len__(self) -> int:
        return len(self._known)

    def contains(self, key: str, en: str) -> bool:
        return (key, src_hash(en)) in self._known

    def add(self, key: str, en: str, reason: str, model: str | None = None, de: object = None) -> None:
        sha = src_hash(en)
        self._append(
            {"key": key, "src_sha1": sha, "en": en, "reason": reason, "model": model,
             "de": de if isinstance(de, str) else None, "ts": _now()}
        )
        with self._lock:
            self._known.add((key, sha))


class UnchangedLog(_JsonlLog):
    """Texts the model returned unchanged (``{key, src_sha1, model, ts}``).

    They are kept out of the translation cache, so the English text stays in
    the game, and are not requested again unless ``--retry-unchanged`` is used.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._known: set[tuple[str, str]] = set()
        for record in self._records():
            key, sha = record.get("key"), record.get("src_sha1")
            if isinstance(key, str) and isinstance(sha, str):
                self._known.add((key, sha))

    def __len__(self) -> int:
        return len(self._known)

    def contains(self, key: str, en: str) -> bool:
        return (key, src_hash(en)) in self._known

    def add(self, key: str, en: str, model: str | None = None) -> None:
        sha = src_hash(en)
        self._append({"key": key, "src_sha1": sha, "model": model, "ts": _now()})
        with self._lock:
            self._known.add((key, sha))



class LockBusy(RuntimeError):
    """Another process holds the run lock."""


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Non-blocking exclusive ``flock`` on ``path`` for the duration of the block."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(f"{path} ist gesperrt - ein anderer Lauf ist aktiv") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield  # closing the file releases the lock
