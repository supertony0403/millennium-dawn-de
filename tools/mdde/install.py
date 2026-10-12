"""Register the submod with the native (Linux) Paradox launcher.

HOI4 runs natively here, so everything lives below
``~/.local/share/Paradox Interactive/Hearts of Iron IV/`` (no Proton prefix,
no ``Z:`` paths):

* ``mod/millennium_dawn_de.mod``  pointer = descriptor + ``path="<repo>"``
* ``launcher-v2.sqlite``          mod row (source ``local``) + entry in the
                                  active playset directly behind Millennium Dawn
* ``dlc_load.json``               enabled mod list read by the game itself

The launcher must be closed (it rewrites its database on exit); a timestamped
backup of the database is taken before any change. Running it again is safe.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REGISTRY_ID = "mod/millennium_dawn_de.mod"
POINTER_NAME = "millennium_dawn_de.mod"
DISPLAY_NAME = "Millennium Dawn - Deutsche Übersetzung"
VERSION = "1.0"
SUPPORTED_VERSION = "1.19.*"
TAGS = ["Translation", "Localisation"]
MD_STEAM_ID = "2777392649"
MD_REGISTRY_ID = f"mod/ugc_{MD_STEAM_ID}.mod"
MD_NAME = "Millennium Dawn: A Modern Day Mod"
MOD_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "mdde:" + REGISTRY_ID))
LAUNCHER_MARKERS = (".paradoxlauncher", "paradox launcher", "dowser")


class InstallError(RuntimeError):
    pass


class LauncherRunning(InstallError):
    pass


@dataclass
class InstallReport:
    pointer: Path
    backup: Path
    mod_id: str
    playset: str
    position: int
    md_enabled: bool
    dlc_load_changed: bool


def launcher_running(proc_dir: Path = Path("/proc"), self_pid: int | None = None) -> list[str]:
    """``["<pid> <cmdline>", ...]`` of running Paradox launcher processes."""
    self_pid = os.getpid() if self_pid is None else self_pid
    hits = []
    try:
        entries = list(Path(proc_dir).iterdir())
    except OSError:
        return hits
    for entry in sorted(entries, key=lambda p: p.name):
        if not entry.name.isdigit() or int(entry.name) == self_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if cmdline and any(marker in cmdline.lower() for marker in LAUNCHER_MARKERS):
            hits.append(f"{entry.name} {cmdline[:120]}")
    return hits


def pointer_text(mod_root: Path) -> str:
    descriptor = (mod_root / "descriptor.mod").read_text(encoding="utf-8-sig")
    lines = [ln for ln in descriptor.splitlines() if not re.match(r"\s*path\s*=", ln)]
    return "\n".join(lines).rstrip("\n") + f'\npath="{mod_root}"\n'


def _backup(db_path: Path, stamp: datetime) -> Path:
    target = db_path.with_name(f"{db_path.name}.mdde-backup-{stamp:%Y%m%d-%H%M%S}")
    n = 2
    while target.exists():
        target = db_path.with_name(f"{db_path.name}.mdde-backup-{stamp:%Y%m%d-%H%M%S}-{n}")
        n += 1
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def _preconditions(db: sqlite3.Connection) -> tuple[str, str, str]:
    row = db.execute(
        "SELECT id FROM mods WHERE steamId=? OR gameRegistryId=? OR displayName=? "
        "ORDER BY (steamId=?) DESC LIMIT 1",
        (MD_STEAM_ID, MD_REGISTRY_ID, MD_NAME, MD_STEAM_ID),
    ).fetchone()
    if row is None:
        raise InstallError("Millennium Dawn ist im Launcher nicht registriert (Workshop-Abo pruefen)")
    md_id = row[0]
    row = db.execute(
        "SELECT id, name FROM playsets WHERE isActive=1 ORDER BY updatedOn DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise InstallError("kein aktives Playset im Launcher gefunden")
    playset_id, playset_name = row
    if db.execute("SELECT 1 FROM playsets_mods WHERE playsetId=? AND modId=?",
                  (playset_id, md_id)).fetchone() is None:
        raise InstallError(f"Millennium Dawn ist nicht im aktiven Playset '{playset_name}'")
    return md_id, playset_id, playset_name


def _upsert_mod(db: sqlite3.Connection, mod_root: Path, stamp: datetime) -> str:
    row = db.execute("SELECT id FROM mods WHERE gameRegistryId=?", (REGISTRY_ID,)).fetchone()
    fields = (DISPLAY_NAME, VERSION, SUPPORTED_VERSION, "ready_to_play", "local", str(mod_root), json.dumps(TAGS))
    if row:
        db.execute(
            "UPDATE mods SET displayName=?, version=?, requiredVersion=?, status=?, source=?, dirPath=?, "
            "tags=? WHERE id=?",
            (*fields, row[0]),
        )
        return row[0]
    db.execute(
        "INSERT INTO mods (id, gameRegistryId, name, displayName, version, requiredVersion, status, source, "
        "dirPath, tags, isNew, createdDate, isMetadataApplied, metadataStatus, keepLatest) "
        "VALUES (?,?,NULL,?,?,?,?,?,?,?,0,?,0,'not_applied',1)",
        (MOD_ID, REGISTRY_ID, *fields, int(stamp.timestamp())),
    )
    return MOD_ID


def _place_behind_md(db: sqlite3.Connection, playset_id: str, md_id: str, mod_id: str) -> tuple[int, bool]:
    rows = db.execute(
        "SELECT modId, enabled, position FROM playsets_mods WHERE playsetId=? ORDER BY position, rowid",
        (playset_id,),
    ).fetchall()
    order = [r[0] for r in rows if r[0] != mod_id]
    order.insert(order.index(md_id) + 1, mod_id)
    current = {r[0]: r[2] for r in rows}
    db.execute("DELETE FROM playsets_mods WHERE playsetId=? AND modId=?", (playset_id, mod_id))
    for pos, mid in enumerate(order):
        if mid == mod_id:
            db.execute("INSERT INTO playsets_mods (playsetId, modId, enabled, position) VALUES (?,?,1,?)",
                       (playset_id, mod_id, pos))
        elif current.get(mid) != pos:
            db.execute("UPDATE playsets_mods SET position=? WHERE playsetId=? AND modId=?",
                       (pos, playset_id, mid))
    md_enabled = any(r[0] == md_id and r[1] for r in rows)
    return order.index(mod_id), md_enabled


def _update_dlc_load(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {"enabled_mods": [], "disabled_dlcs": []}
    mods = [m for m in data.get("enabled_mods", []) if m != REGISTRY_ID]
    mods.insert(mods.index(MD_REGISTRY_ID) + 1 if MD_REGISTRY_ID in mods else len(mods), REGISTRY_ID)
    changed = mods != data.get("enabled_mods")
    data["enabled_mods"] = mods
    data.setdefault("disabled_dlcs", [])
    if changed or not path.exists():
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return changed


def install(mod_root: Path, hoi4_dir: Path, *, running: Callable[[], list[str]] = launcher_running,
            now: Callable[[], datetime] = datetime.now) -> InstallReport:
    mod_root, hoi4_dir = Path(mod_root).resolve(), Path(hoi4_dir)
    hits = running()
    if hits:
        raise LauncherRunning(f"Paradox-Launcher laeuft noch ({hits[0]}) - bitte schliessen")
    if not (mod_root / "descriptor.mod").is_file():
        raise InstallError(f"{mod_root}/descriptor.mod fehlt")
    db_path = hoi4_dir / "launcher-v2.sqlite"
    if not db_path.is_file():
        raise InstallError(f"Launcher-Datenbank nicht gefunden: {db_path}")
    stamp = now()

    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        md_id, playset_id, playset_name = _preconditions(db)
        backup = _backup(db_path, stamp)
        db.execute("BEGIN IMMEDIATE")
        try:
            mod_id = _upsert_mod(db, mod_root, stamp)
            position, md_enabled = _place_behind_md(db, playset_id, md_id, mod_id)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    finally:
        db.close()

    pointer = hoi4_dir / "mod" / POINTER_NAME
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(pointer_text(mod_root), encoding="utf-8")
    dlc_changed = _update_dlc_load(hoi4_dir / "dlc_load.json")
    return InstallReport(pointer, backup, mod_id, playset_name, position, md_enabled, dlc_changed)
