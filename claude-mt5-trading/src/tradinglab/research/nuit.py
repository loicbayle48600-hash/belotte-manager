"""Recherches de nuit sur la 3090 (2026-09-30, demande utilisateur : « des recherches sur la 3090, beaucoup, sur tout,
toute la nuit »).

Enchaîne des passages de la recherche en masse (research/massive.py, --device gpu), chacun dans un processus séparé
(mémoire rendue entre deux passages), en priorité basse, jusqu'à l'heure de fin. Entre deux passages, si la boucle de
trading ne boucle plus (dernier cycle > 120 s), on attend qu'elle reparte. Chaque passage écrit son rapport
(reports/massive_<étiquette>_*.json) et ses propositions SHADOW distinctes ; le journal garde massive_start / massive_done.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

#: passages dans l'ordre (étiquette, arguments) ; la liste est rejouée en boucle si la nuit n'est pas finie
PASSAGES = [
    ("univers_fin", ["--univers", "--sessions", "--fin"]),
    ("univers_ut_alt", ["--univers", "--sessions", "--timeframes", "M5:M15,M15:H4,H1:D1,H4:H4"]),
    ("univers_fin_ut_alt", ["--univers", "--sessions", "--fin", "--timeframes", "M5:M15,M15:H4,H1:D1"]),
    ("univers_std", ["--univers", "--sessions"]),
]


def _age_cycle(home: Path):
    try:
        d = json.loads((home / "state" / "system_state.json").read_text(encoding="utf-8"))
        from datetime import timezone
        return (datetime.now(timezone.utc) - datetime.fromisoformat(d["last_cycle"]["ts"])).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:  # pragma: no cover - pilotage de processus
    ap = argparse.ArgumentParser(description="Recherches de nuit sur la 3090")
    ap.add_argument("--fin", default="07:30", help="heure locale de fin (HH:MM)")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args(argv)
    home = Path(__file__).resolve().parents[3]
    h, mi = (int(x) for x in args.fin.split(":"))
    fin = datetime.now().replace(hour=h, minute=mi, second=0, microsecond=0)
    if fin <= datetime.now():
        fin += timedelta(days=1)
    k = 0
    while datetime.now() < fin - timedelta(minutes=20):
        age = _age_cycle(home)
        if age is not None and age > 120:
            print(f"{datetime.now():%H:%M} boucle de trading silencieuse ({age:.0f} s) : attente", flush=True)
            time.sleep(120)
            continue
        etiquette, extra = PASSAGES[k % len(PASSAGES)]
        k += 1
        cmd = [sys.executable, "-m", "tradinglab.research.massive", "--device", "gpu", "--workers", str(args.workers),
               "--top", "10", "--cache-age-h", "18", "--etiquette", f"{etiquette}_{k}", *extra]
        print(f"{datetime.now():%H:%M} passage {k} : {etiquette}", flush=True)
        creation = 0x00004000 if sys.platform == "win32" else 0            # BELOW_NORMAL_PRIORITY_CLASS
        proc = subprocess.Popen(cmd, cwd=str(home), creationflags=creation)
        while proc.poll() is None:
            if datetime.now() > fin + timedelta(minutes=30):
                proc.terminate()
                print("heure de fin dépassée : passage interrompu", flush=True)
                break
            time.sleep(30)
        print(f"{datetime.now():%H:%M} passage {k} terminé (code {proc.returncode})", flush=True)
    print(f"{datetime.now():%H:%M} nuit terminée : {k} passage(s)", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
