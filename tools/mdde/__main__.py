"""Command line interface: ``cd tools && python3 -m mdde <command>``."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdde",
        description="Millennium Dawn - deutsche Uebersetzung (Pipeline)",
    )
    sub = parser.add_subparsers(dest="command", metavar="<befehl>")

    p = sub.add_parser("status", help="offene Texte, Cache-Stand und Tageskontingent anzeigen")
    p.add_argument("--scope", choices=("germany", "full"), default="germany")
    p.add_argument("--no-quota", action="store_true", help="Tageskontingent nicht abfragen")

    p = sub.add_parser("translate", help="offene Texte ueber OpenRouter uebersetzen")
    p.add_argument("--scope", choices=("germany", "full"), default="germany")
    p.add_argument("--max-requests", type=int, default=None, metavar="N")
    p.add_argument("--wait-for-reset", action="store_true",
                   help="bei erschoepftem Kontingent bis 00:00 UTC schlafen und weitermachen")
    p.add_argument("--retry-rejects", action="store_true",
                   help="auch Texte erneut versuchen, die schon in rejects.jsonl stehen")

    sub.add_parser("build", help="Ausgabedateien unter localisation/german erzeugen")
    sub.add_parser("check", help="Ausgabedateien pruefen (Exit 1 = Fehler, 2 = veraltet)")

    p = sub.add_parser("install", help="Submod im nativen Paradox-Launcher eintragen")
    p.add_argument("--yes", action="store_true", help="ohne Rueckfrage ausfuehren")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return 2
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    from . import cli  # imported lazily so --help stays fast

    return cli.dispatch(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
