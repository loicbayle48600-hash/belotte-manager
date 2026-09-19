"""CLI : `python -m tradinglab.api.cli STATUS` / `tradinglab WHY 123456` …"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..core.config import load_settings
from .commands import ACTION_COMMANDS, CommandHandler, READ_COMMANDS


def json_safe(obj: Any) -> Any:
    """Rend le résultat sérialisable en JSON strict : inf/nan -> None (convention UNKNOWN/UNAVAILABLE).

    `heartbeat_age()` vaut `inf` tant qu'aucun heartbeat n'existe : sans cette normalisation,
    `STATUS --json` imprimerait `Infinity`, rejeté par jq / ConvertFrom-Json (PowerShell 5.1).
    """
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return None
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Claude MT5 Trading Lab — commandes utilisateur")
    ap.add_argument("command", help="/".join(sorted(READ_COMMANDS | ACTION_COMMANDS)))
    ap.add_argument("arg", nargs="?", default=None, help="ticket / symbole selon la commande")
    ap.add_argument("--home", default=None)
    ap.add_argument("--json", action="store_true", help="sortie JSON brute")
    a = ap.parse_args(argv)
    s = load_settings(Path(a.home) if a.home else None)
    res = CommandHandler(s, source="cli").run(a.command, a.arg)
    print(json.dumps(json_safe(res), ensure_ascii=False, indent=2 if not a.json else None, allow_nan=False))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
