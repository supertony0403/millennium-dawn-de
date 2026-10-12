"""install: pointer file, launcher DB and dlc_load.json against a schema copy."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from mdde import install as inst
from mdde.__main__ import main

SCHEMA = (Path(__file__).parent / "fixtures" / "launcher_schema.sql").read_text(encoding="utf-8")
MD_ID = "882b1d9b-359f-4de3-bc41-7233cbaed7d7"
A_ID, B_ID = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
ACTIVE, INACTIVE = "91069843-9090-4fa9-9e5c-df6d8c6b9288", "5f1f8d77-eb9e-4e2e-b0d1-ff6fc9ad3c93"


def make_hoi4(tmp_path: Path, md_in_playset: bool = True, active: bool = True) -> Path:
    hoi4 = tmp_path / "hoi4"
    (hoi4 / "mod").mkdir(parents=True)
    db = sqlite3.connect(hoi4 / "launcher-v2.sqlite")
    db.executescript(SCHEMA)
    mods = [
        (MD_ID, "2777392649", "mod/ugc_2777392649.mod", "Millennium Dawn: A Modern Day Mod", "steam"),
        (A_ID, "111", "mod/ugc_111.mod", "Other A", "steam"),
        (B_ID, "222", "mod/ugc_222.mod", "Other B", "steam"),
    ]
    for mid, steam, reg, name, source in mods:
        db.execute(
            "INSERT INTO mods (id, steamId, gameRegistryId, displayName, version, requiredVersion, status,"
            " source, dirPath, tags, isNew, createdDate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, steam, reg, name, "2.0.2", "1.19.*", "ready_to_play", source, f"/ws/{steam}", "[]", 0,
             1647049618),
        )
    db.execute("INSERT INTO playsets (id, name, isActive, loadOrder, createdOn) VALUES (?,?,?,?,?)",
               (ACTIVE, "d", 1 if active else 0, "custom", 1786229738176))
    db.execute("INSERT INTO playsets (id, name, isActive, createdOn) VALUES (?,?,?,?)",
               (INACTIVE, "Initial playset", 0, 1764792532609))
    order = [(A_ID, 0)] + ([(MD_ID, 1)] if md_in_playset else []) + [(B_ID, 1)]
    for pos, (mid, enabled) in enumerate(order):
        db.execute("INSERT INTO playsets_mods (playsetId, modId, enabled, position) VALUES (?,?,?,?)",
                   (ACTIVE, mid, enabled, pos))
    db.execute("INSERT INTO playsets_mods (playsetId, modId, enabled, position) VALUES (?,?,?,?)",
               (INACTIVE, MD_ID, 1, 0))
    db.commit()
    db.close()
    (hoi4 / "dlc_load.json").write_text('{"enabled_mods":["mod/ugc_111.mod","mod/ugc_2777392649.mod",'
                                        '"mod/ugc_222.mod"],"disabled_dlcs":[]}')
    return hoi4


@pytest.fixture
def mod_root(tmp_path):
    root = tmp_path / "Mod Ordner" / "millennium_dawn_de"
    root.mkdir(parents=True)
    (root / "descriptor.mod").write_text(
        'version="1.0"\ntags={\n\t"Translation"\n}\nname="Millennium Dawn - Deutsche Übersetzung"\n'
        'dependencies={\n\t"Millennium Dawn: A Modern Day Mod"\n}\nsupported_version="1.19.*"\n',
        encoding="utf-8",
    )
    return root


def not_running():
    return []


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 12, 18, 0, 0)

    def __call__(self):
        self.t = self.t.replace(second=self.t.second + 1)
        return self.t


def playset(hoi4, pid=ACTIVE):
    db = sqlite3.connect(hoi4 / "launcher-v2.sqlite")
    rows = db.execute("SELECT modId, enabled, position FROM playsets_mods WHERE playsetId=? ORDER BY position",
                      (pid,)).fetchall()
    db.close()
    return rows


def our_rows(hoi4):
    db = sqlite3.connect(hoi4 / "launcher-v2.sqlite")
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM mods WHERE gameRegistryId='mod/millennium_dawn_de.mod'")]
    db.close()
    return rows


def test_pointer_file_points_to_native_mod_folder(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    report = inst.install(mod_root, hoi4, running=not_running, now=Clock())
    text = (hoi4 / "mod" / "millennium_dawn_de.mod").read_text(encoding="utf-8")
    assert text.endswith(f'path="{mod_root}"\n')
    assert 'name="Millennium Dawn - Deutsche Übersetzung"' in text
    assert '"Millennium Dawn: A Modern Day Mod"' in text
    assert "Z:" not in text
    assert report.pointer == hoi4 / "mod" / "millennium_dawn_de.mod"


def test_mod_row_is_local_and_ready(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    inst.install(mod_root, hoi4, running=not_running, now=Clock())
    (row,) = our_rows(hoi4)
    assert row["source"] == "local"
    assert row["status"] == "ready_to_play"
    assert row["dirPath"] == str(mod_root)
    assert row["displayName"] == "Millennium Dawn - Deutsche Übersetzung"
    assert row["requiredVersion"] == "1.19.*"
    assert json.loads(row["tags"]) == ["Translation", "Localisation"]
    assert row["metadataStatus"] == "not_applied"


def test_inserted_directly_behind_millennium_dawn_in_active_playset(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    report = inst.install(mod_root, hoi4, running=not_running, now=Clock())
    our_id = our_rows(hoi4)[0]["id"]
    assert playset(hoi4) == [(A_ID, 0, 0), (MD_ID, 1, 1), (our_id, 1, 2), (B_ID, 1, 3)]
    assert playset(hoi4, INACTIVE) == [(MD_ID, 1, 0)]
    assert report.position == 2


def test_install_is_idempotent(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    clock = Clock()
    inst.install(mod_root, hoi4, running=not_running, now=clock)
    first = playset(hoi4)
    inst.install(mod_root, hoi4, running=not_running, now=clock)
    assert len(our_rows(hoi4)) == 1
    assert playset(hoi4) == first
    data = json.loads((hoi4 / "dlc_load.json").read_text())
    assert data["enabled_mods"].count("mod/millennium_dawn_de.mod") == 1


def test_reinstall_moves_misplaced_entry_back_behind_md(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    inst.install(mod_root, hoi4, running=not_running, now=Clock())
    our_id = our_rows(hoi4)[0]["id"]
    db = sqlite3.connect(hoi4 / "launcher-v2.sqlite")
    db.execute("UPDATE playsets_mods SET position=9, enabled=0 WHERE modId=?", (our_id,))
    db.commit()
    db.close()
    inst.install(mod_root, hoi4, running=not_running, now=Clock())
    assert playset(hoi4) == [(A_ID, 0, 0), (MD_ID, 1, 1), (our_id, 1, 2), (B_ID, 1, 3)]


def test_dlc_load_gets_entry_behind_md_in_launcher_format(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    inst.install(mod_root, hoi4, running=not_running, now=Clock())
    raw = (hoi4 / "dlc_load.json").read_text()
    assert raw == ('{"enabled_mods":["mod/ugc_111.mod","mod/ugc_2777392649.mod","mod/millennium_dawn_de.mod",'
                   '"mod/ugc_222.mod"],"disabled_dlcs":[]}')


def test_missing_dlc_load_is_created(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    (hoi4 / "dlc_load.json").unlink()
    inst.install(mod_root, hoi4, running=not_running, now=Clock())
    data = json.loads((hoi4 / "dlc_load.json").read_text())
    assert data == {"enabled_mods": ["mod/millennium_dawn_de.mod"], "disabled_dlcs": []}


def test_backup_is_made_before_changes(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    original = (hoi4 / "launcher-v2.sqlite").read_bytes()
    report = inst.install(mod_root, hoi4, running=not_running, now=Clock())
    assert report.backup.name == "launcher-v2.sqlite.mdde-backup-20261012-180001"
    db = sqlite3.connect(report.backup)
    assert db.execute("SELECT COUNT(*) FROM mods WHERE gameRegistryId='mod/millennium_dawn_de.mod'").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM mods").fetchone() == (3,)
    db.close()
    assert (hoi4 / "launcher-v2.sqlite").read_bytes() != original


def test_running_launcher_aborts_without_touching_anything(tmp_path, mod_root):
    hoi4 = make_hoi4(tmp_path)
    before = (hoi4 / "launcher-v2.sqlite").read_bytes()
    with pytest.raises(inst.LauncherRunning):
        inst.install(mod_root, hoi4, running=lambda: ["4711 Paradox Launcher"], now=Clock())
    assert (hoi4 / "launcher-v2.sqlite").read_bytes() == before
    assert not (hoi4 / "mod" / "millennium_dawn_de.mod").exists()
    assert not list(hoi4.glob("*.mdde-backup-*"))


@pytest.mark.parametrize(
    "kwargs,needle",
    [({"md_in_playset": False}, "Playset"), ({"active": False}, "aktives Playset")],
)
def test_missing_preconditions_abort_and_leave_db_unchanged(tmp_path, mod_root, kwargs, needle):
    hoi4 = make_hoi4(tmp_path, **kwargs)
    with pytest.raises(inst.InstallError) as excinfo:
        inst.install(mod_root, hoi4, running=not_running, now=Clock())
    assert needle in str(excinfo.value)
    assert our_rows(hoi4) == []


def test_missing_database_aborts(tmp_path, mod_root):
    hoi4 = tmp_path / "empty"
    hoi4.mkdir()
    with pytest.raises(inst.InstallError):
        inst.install(mod_root, hoi4, running=not_running, now=Clock())


def write_proc(proc: Path, pid: int, cmdline: bytes) -> None:
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(cmdline)


def test_launcher_detection_reads_proc(tmp_path):
    proc = tmp_path / "proc"
    write_proc(proc, 100, b"/home/t/.paradoxlauncher/launcher-v2.2026.12/Paradox Launcher\0--type=gpu\0")
    write_proc(proc, 200, b"/usr/bin/zsh\0-c\0ls\0")
    write_proc(proc, 300, b"/home/t/.paradoxlauncher/launcher-v2.2026.12/Paradox Launcher\0")
    write_proc(proc, 400, b"")
    (proc / "self").mkdir()
    (proc / "not-a-pid").mkdir()
    hits = inst.launcher_running(proc, self_pid=300)
    assert len(hits) == 1 and hits[0].startswith("100 ")


def test_cli_install_needs_confirmation_and_then_runs(tmp_path, mod_root, monkeypatch, capsys):
    hoi4 = make_hoi4(tmp_path)
    monkeypatch.setenv("MDDE_HOI4_DIR", str(hoi4))
    monkeypatch.setenv("MDDE_MOD_ROOT", str(mod_root))
    monkeypatch.setattr(inst, "launcher_running", not_running)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["install"]) == 1
    assert our_rows(hoi4) == []
    assert main(["install", "--yes"]) == 0
    assert len(our_rows(hoi4)) == 1
    assert "hinter" in capsys.readouterr().out
