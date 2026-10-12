"""Configuration: defaults and environment overrides."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mdde.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]


def test_defaults_point_to_repo_and_workshop_copy():
    cfg = load_config({})
    assert cfg.mod_root == REPO
    assert cfg.md_root == Path(
        "/mnt/steam-library/SteamLibrary/steamapps/workshop/content/394360/2777392649"
    )
    assert cfg.output_dir == REPO / "localisation" / "german"
    assert cfg.translations_path == REPO / "cache" / "translations.jsonl"
    assert cfg.rejects_path == REPO / "cache" / "rejects.jsonl"
    assert cfg.workers == 6
    assert cfg.reserve == 100
    assert cfg.models[0] == "nvidia/nemotron-3-super-120b-a12b:free"


def test_env_overrides_paths_and_numbers(tmp_path):
    cfg = load_config(
        {
            "MDDE_MD_ROOT": str(tmp_path / "md"),
            "MDDE_MOD_ROOT": str(tmp_path / "mod"),
            "MDDE_HOI4_DIR": str(tmp_path / "hoi4"),
            "MDDE_KEY_FILE": str(tmp_path / "k.env"),
            "MDDE_WORKERS": "5",
            "MDDE_RESERVE": "0",
        }
    )
    assert cfg.md_root == tmp_path / "md"
    assert cfg.mod_root == tmp_path / "mod"
    assert cfg.hoi4_dir == tmp_path / "hoi4"
    assert cfg.key_file == tmp_path / "k.env"
    assert cfg.workers == 5
    assert cfg.reserve == 0
    assert cfg.log_dir == tmp_path / "mod" / "logs"


def test_model_chain_is_split_and_trimmed():
    cfg = load_config({"MDDE_MODELS": " a/b:free , ,c/d:free,"})
    assert cfg.models == ("a/b:free", "c/d:free")


@pytest.mark.parametrize(
    "env",
    [
        {"MDDE_WORKERS": "drei"},
        {"MDDE_WORKERS": "0"},
        {"MDDE_RESERVE": "-1"},
        {"MDDE_MODELS": " , "},
    ],
)
def test_invalid_values_raise_config_error(env):
    with pytest.raises(ConfigError):
        load_config(env)


def test_cli_is_runnable_from_tools_dir():
    """The documented call `cd tools && python3 -m mdde --help` must work."""
    proc = subprocess.run(
        [sys.executable, "-m", "mdde", "--help"],
        cwd=REPO / "tools",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    for cmd in ("status", "translate", "build", "check", "install"):
        assert cmd in proc.stdout


def test_cli_without_command_fails_with_usage():
    proc = subprocess.run(
        [sys.executable, "-m", "mdde"],
        cwd=REPO / "tools",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "usage" in (proc.stdout + proc.stderr).lower()


def test_cache_dir_can_point_to_a_copy(tmp_path):
    cfg = load_config({"MDDE_CACHE_DIR": str(tmp_path / "copy")})
    assert cfg.translations_path == tmp_path / "copy" / "translations.jsonl"
    assert cfg.rejects_path == tmp_path / "copy" / "rejects.jsonl"
    assert cfg.unchanged_path == tmp_path / "copy" / "unchanged.jsonl"
    assert cfg.lock_path == tmp_path / "copy" / "translate.lock"
