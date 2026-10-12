"""Command handlers behind ``python3 -m mdde``."""
from __future__ import annotations

import argparse
import sys

from .config import Config


def _not_implemented(args: argparse.Namespace, cfg: Config) -> int:
    print(f"'{args.command}' ist noch nicht implementiert", file=sys.stderr)
    return 3


HANDLERS = {
    "status": _not_implemented,
    "translate": _not_implemented,
    "build": _not_implemented,
    "check": _not_implemented,
    "install": _not_implemented,
}


def dispatch(args: argparse.Namespace, cfg: Config) -> int:
    return HANDLERS[args.command](args, cfg)
