"""CLI : `python -m tradinglab.api.cli STATUS` / `tradinglab WHY 123456` …"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from ..core.config import load_settings
from .commands import ACTION_COMMANDS, CommandHandler, READ_COMMANDS


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Claude MT5 Trading Lab — commandes utilisateur")
    ap.add_argument("command", help="/".join(sorted(READ_COMMANDS | ACTION_COMMANDS)))
    ap.add_argument("arg", nargs="?", default=None, help="ticket / symbole selon la commande")
    ap.add_argument("--home", default=None)
    ap.add_argument("--json", action="store_true", help="sortie JSON brute")
    a = ap.parse_args(argv)
    s = load_settings(Path(a.home) if a.home else None)
    res = CommandHandler(s, source="cli").run(a.command, a.arg)
    print(json.dumps(res, ensure_ascii=False, indent=2 if not a.json else None, default=str))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
