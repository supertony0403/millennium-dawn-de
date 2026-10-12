"""Runtime configuration from environment variables.

Every value has a sensible default for this machine (native Linux HOI4,
Millennium Dawn from the Steam workshop) and can be overridden per run:

    MDDE_MD_ROOT    Millennium Dawn mod folder (read only)
    MDDE_MOD_ROOT   this submod's folder (default: repository root)
    MDDE_HOI4_DIR   HOI4 user folder with launcher-v2.sqlite / dlc_load.json
    MDDE_KEY_FILE   file that contains OPENROUTER_API_KEY=...
    MDDE_MODELS     comma separated model chain
    MDDE_WORKERS    parallel requests (>=1)
    MDDE_RESERVE    free daily requests left untouched (>=0, for other apps)
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MD_ROOT = Path(
    "/mnt/steam-library/SteamLibrary/steamapps/workshop/content/394360/2777392649"
)
DEFAULT_HOI4_DIR = Path.home() / ".local/share/Paradox Interactive/Hearts of Iron IV"
DEFAULT_KEY_FILE = Path.home() / "Documents/Programmierung/Backup/Discord-Bot/.env"
DEFAULT_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
)
DEFAULT_WORKERS = 3
DEFAULT_RESERVE = 100


class ConfigError(ValueError):
    """Raised for invalid environment values."""


@dataclass(frozen=True)
class Config:
    md_root: Path
    mod_root: Path
    hoi4_dir: Path
    key_file: Path
    models: tuple[str, ...]
    workers: int
    reserve: int

    @property
    def cache_dir(self) -> Path:
        return self.mod_root / "cache"

    @property
    def translations_path(self) -> Path:
        return self.cache_dir / "translations.jsonl"

    @property
    def rejects_path(self) -> Path:
        return self.cache_dir / "rejects.jsonl"

    @property
    def log_dir(self) -> Path:
        return self.mod_root / "logs"

    @property
    def output_dir(self) -> Path:
        return self.mod_root / "localisation" / "german"

    @property
    def build_info_path(self) -> Path:
        return self.mod_root / "build_info.json"

    @property
    def glossary_path(self) -> Path:
        return self.mod_root / "tools" / "glossary.json"


def _path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} muss eine ganze Zahl sein, nicht {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} muss >= {minimum} sein, nicht {value}")
    return value


def _models(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("MDDE_MODELS")
    if raw is None:
        return DEFAULT_MODELS
    models = tuple(m.strip() for m in raw.split(",") if m.strip())
    if not models:
        raise ConfigError("MDDE_MODELS enthaelt kein Modell")
    return models


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build the configuration from ``env`` (default: ``os.environ``)."""
    env = os.environ if env is None else env
    return Config(
        md_root=_path(env, "MDDE_MD_ROOT", DEFAULT_MD_ROOT),
        mod_root=_path(env, "MDDE_MOD_ROOT", REPO_ROOT),
        hoi4_dir=_path(env, "MDDE_HOI4_DIR", DEFAULT_HOI4_DIR),
        key_file=_path(env, "MDDE_KEY_FILE", DEFAULT_KEY_FILE),
        models=_models(env),
        workers=_int(env, "MDDE_WORKERS", DEFAULT_WORKERS, 1),
        reserve=_int(env, "MDDE_RESERVE", DEFAULT_RESERVE, 0),
    )
