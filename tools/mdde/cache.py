"""Append-only translation cache (``cache/translations.jsonl``) and reject log.

Each line is one JSON record ``{key, src_sha1, de, model, ts}``. A cached
translation is only used while the SHA-1 of the *current* English text matches
``src_sha1``, so changed source texts are re-translated automatically. Later
lines win over earlier ones; broken lines (e.g. from a killed process) are
skipped. Writes are serialised with a lock and hit the disk immediately.
"""
from __future__ import annotations

import hashlib
import json
import threading
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
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
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
