"""Command handlers behind ``python3 -m mdde``."""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from . import build as buildmod
from . import install as installmod
from . import openrouter, scope
from .build import md_version
from .cache import RejectLog, TranslationCache, UnchangedLog
from .config import Config
from .translate import (
    RunOptions,
    Runner,
    build_system_prompt,
    load_glossary,
    make_batches,
    sigterm_as_interrupt,
)

FATAL_STOPS = {"auth", "no-models", "error", "quota-unknown"}
EXIT_INTERRUPTED = 130


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _client(cfg: Config, system_prompt: str = "") -> openrouter.Client:
    key = openrouter.load_api_key(os.environ, cfg.key_file)
    return openrouter.Client(key, list(cfg.models), system_prompt=system_prompt,
                             transport=openrouter.urllib_transport)


def _missing_key(cfg: Config) -> None:
    print(f"Kein OPENROUTER_API_KEY gefunden (weder in der Umgebung noch in {cfg.key_file})",
          file=sys.stderr)


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    items = scope.collect(cfg.md_root, args.scope)
    cache = TranslationCache(cfg.translations_path)
    rejects = RejectLog(cfg.rejects_path)
    unchanged = UnchangedLog(cfg.unchanged_path)
    cached, rejected, kept, todo = [], [], [], []
    for i in items:
        if cache.get(i.key, i.en) is not None:
            cached.append(i)
        elif rejects.contains(i.key, i.en):
            rejected.append(i)
        elif unchanged.contains(i.key, i.en):
            kept.append(i)
        else:
            todo.append(i)
    distinct = list(dict.fromkeys(i.en for i in todo))
    batches = make_batches([(en, en) for en in distinct], 9000, 100)

    print(f"Millennium Dawn {md_version(cfg.md_root) or '?'} - Scope {args.scope}")
    print(f"Offene Texte:            {fmt(len(items)):>9} ({fmt(sum(len(i.en) for i in items))} Zeichen)")
    print(f"  davon im Cache:        {fmt(len(cached)):>9}")
    print(f"  davon abgelehnt:       {fmt(len(rejected)):>9}")
    print(f"  davon unveraendert:    {fmt(len(kept)):>9} (Modell gab Englisch zurueck, --retry-unchanged)")
    print(f"  noch zu uebersetzen:   {fmt(len(todo)):>9} ({fmt(len(distinct))} verschiedene Texte, "
          f"{fmt(sum(len(t) for t in distinct))} Zeichen)")
    print(f"Geschaetzte Anfragen:    {fmt(len(batches)):>9} (Pakete <= 9000 Zeichen / <= 100 Texte)")
    per_file = Counter(i.base for i in todo)
    if per_file:
        print("Groesste Dateien (offen):")
        for base, count in per_file.most_common(8):
            print(f"  {fmt(count):>7}  {base}")
    if not args.no_quota:
        try:
            client = _client(cfg)
            quota = client.quota()
        except openrouter.KeyNotFound:
            print("Tageskontingent:         unbekannt (kein OPENROUTER_API_KEY)")
        else:
            if quota is None:
                print("Tageskontingent:         unbekannt (Antwort ohne free_model_daily_requests; "
                      f"Felder: {', '.join(client.quota_fields) or '-'})")
            else:
                limit = quota.get("limit")
                print(f"Tageskontingent (Gratis-Modelle): {quota['remaining']}"
                      + (f" von {limit}" if limit is not None else "")
                      + f" uebrig, Reserve {cfg.reserve}")
    return 0


def cmd_translate(args: argparse.Namespace, cfg: Config) -> int:
    try:
        glossary = load_glossary(cfg.glossary_path)
    except FileNotFoundError:
        print(f"Warnung: keine Begriffsliste unter {cfg.glossary_path}", file=sys.stderr)
        glossary = {}
    try:
        client = _client(cfg, build_system_prompt(glossary))
    except openrouter.KeyNotFound:
        _missing_key(cfg)
        return 1
    items = scope.collect(cfg.md_root, args.scope)
    runner = Runner(
        client,
        TranslationCache(cfg.translations_path),
        RejectLog(cfg.rejects_path),
        RunOptions(
            workers=cfg.workers,
            reserve=cfg.reserve,
            max_requests=args.max_requests,
            wait_for_reset=args.wait_for_reset,
            allow_unknown_quota=args.allow_unknown_quota,
            retry_rejects=args.retry_rejects,
            retry_unchanged=args.retry_unchanged,
        ),
        log_dir=cfg.log_dir,
        unchanged=UnchangedLog(cfg.unchanged_path),
    )
    try:
        with sigterm_as_interrupt():
            report = runner.run((i.key, i.en) for i in items)
    except KeyboardInterrupt:
        print("Abgebrochen - alle bis dahin gueltigen Uebersetzungen sind im Cache gesichert.",
              file=sys.stderr)
        return EXIT_INTERRUPTED
    return 1 if report.stopped in FATAL_STOPS else 0


def cmd_build(args: argparse.Namespace, cfg: Config) -> int:
    cache = TranslationCache(cfg.translations_path)
    report = buildmod.build(cfg.md_root, cfg.output_dir, cache, info_path=cfg.build_info_path)
    print(f"Build: {report.files} Dateien ({report.mirrored} gespiegelt, {report.new_files} neu) "
          f"-> {cfg.output_dir}")
    print(f"  {fmt(report.replaced)} Werte uebersetzt, {fmt(report.appended)} Schluessel angehaengt, "
          f"{fmt(report.english_fallback)} Werte noch englisch (alle Scopes und Dateien, Cache: {fmt(len(cache))})")
    if report.skipped_empty:
        print(f"  uebersprungen (englische Datei ohne Eintraege): {', '.join(report.skipped_empty)}")
    return 0


def cmd_check(args: argparse.Namespace, cfg: Config) -> int:
    report = buildmod.check(cfg.md_root, cfg.output_dir, info_path=cfg.build_info_path)
    print(f"Check: {report.files} Dateien, {fmt(report.checked_values)} geaenderte Werte geprueft, "
          f"{len(report.errors)} Fehler, {len(report.warnings)} Warnungen")
    for line in report.errors[:50]:
        print(f"  FEHLER  {line}")
    if len(report.errors) > 50:
        print(f"  ... und {len(report.errors) - 50} weitere Fehler")
    for line in report.warnings:
        print(f"  WARNUNG {line}")
    return report.exit_code


def cmd_install(args: argparse.Namespace, cfg: Config) -> int:
    if not args.yes:
        if not sys.stdin.isatty():
            print("Bestaetigung fehlt: Paradox-Launcher schliessen und mit --yes ausfuehren",
                  file=sys.stderr)
            return 1
        answer = input("Paradox-Launcher geschlossen? Launcher-Datenbank jetzt aendern? [j/N] ")
        if answer.strip().lower() not in ("j", "ja", "y", "yes"):
            print("Abgebrochen.")
            return 1
    try:
        report = installmod.install(cfg.mod_root, cfg.hoi4_dir, running=installmod.launcher_running)
    except installmod.InstallError as exc:
        print(f"Installation abgebrochen: {exc}", file=sys.stderr)
        return 1
    print(f"Zeiger:  {report.pointer}")
    print(f"Backup:  {report.backup}")
    print(f"Playset '{report.playset}': eingetragen direkt hinter Millennium Dawn "
          f"(Position {report.position}, aktiviert)")
    if not report.md_enabled:
        print("WARNUNG: Millennium Dawn selbst ist in diesem Playset deaktiviert")
    print("dlc_load.json " + ("ergaenzt" if report.dlc_load_changed else "war schon aktuell"))
    return 0


HANDLERS = {
    "status": cmd_status,
    "translate": cmd_translate,
    "build": cmd_build,
    "check": cmd_check,
    "install": cmd_install,
}


def dispatch(args: argparse.Namespace, cfg: Config) -> int:
    return HANDLERS[args.command](args, cfg)
