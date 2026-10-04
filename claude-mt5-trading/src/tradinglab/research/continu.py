"""Recherche CONTINUE sur la 3090 (2026-09-30, demande utilisateur : « continue les recherches, des milliards de tests
sur le GPU »).

Boucle sans fin (jusqu'à arrêt) de passages de la recherche en masse à réglages TIRÉS AU HASARD dans des espaces larges
(research/massive.py --tirage), sur tout l'univers et toutes les sessions, en priorité basse et sur la carte graphique.

- Compteur cumulé (state/recherche_continue.json) : configurations et backtests testés depuis le début ; le seuil
  statistique de chaque passage tient compte de TOUT ce qui a déjà été testé (--m-cumul) — un milliard d'essais ne doit
  pas fabriquer de faux gagnants.
- Au plus `--top` idées nouvelles par passage, jamais un doublon d'une idée existante (registre ou propositions).
- Si la boucle de trading ne boucle plus (> 120 s), on attend. Arrêt propre : créer state/recherche_continue.stop.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _age_cycle(home: Path):
    try:
        d = json.loads((home / "state" / "system_state.json").read_text(encoding="utf-8"))
        return (datetime.now(timezone.utc) - datetime.fromisoformat(d["last_cycle"]["ts"])).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def semaine_m1_a_faire(maintenant: datetime, etat: dict):
    """Passage M1 dédié (2026-10-01, décision utilisateur) : une fois par week-end, le samedi ou le dimanche (heure locale),
    quand les marchés hors crypto sont fermés et que télécharger l'historique M1 ne gêne pas le bot. Renvoie
    l'identifiant de la semaine si le passage reste à faire, sinon None."""
    if maintenant.weekday() < 5:
        return None
    iso = maintenant.isocalendar()
    semaine = f"{iso[0]}-S{iso[1]:02d}"
    return None if etat.get("week_end_m1") == semaine else semaine


def main(argv=None) -> int:  # pragma: no cover - pilotage de processus
    ap = argparse.ArgumentParser(description="Recherche continue sur la 3090")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--reglages", type=int, default=400, help="réglages clés tirés par passage")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)
    home = Path(__file__).resolve().parents[3]
    etat_f = home / "state" / "recherche_continue.json"
    stop_f = home / "state" / "recherche_continue.stop"
    etat = json.loads(etat_f.read_text(encoding="utf-8")) if etat_f.exists() else {"passages": 0, "configurations": 0, "seed": 1000}
    while not stop_f.exists():
        age = _age_cycle(home)
        if age is not None and age > 120:
            time.sleep(120)
            continue
        etat["seed"] += 1
        etat["passages"] += 1
        semaine_m1 = semaine_m1_a_faire(datetime.now(), etat)
        if semaine_m1:
            # week-end : passage M1 dédié (toutes les stratégies, grille fine, toutes les sessions), une fois
            cmd = [sys.executable, "-m", "tradinglab.research.massive", "--device", "gpu", "--workers", str(args.workers),
                   "--univers", "--sessions", "--fin", "--timeframes", "M1:M5,M1:M15",
                   "--m-cumul", str(etat["configurations"]), "--top", str(args.top), "--cache-age-h", "24",
                   "--etiquette", f"m1_week_end_{semaine_m1}"]
        else:
            cmd = [sys.executable, "-m", "tradinglab.research.massive", "--device", "gpu", "--workers", str(args.workers),
                   "--univers", "--sessions", "--tirage", str(args.reglages), "--seed", str(etat["seed"]),
                   "--m-cumul", str(etat["configurations"]), "--top", str(args.top), "--cache-age-h", "24",
                   "--etiquette", f"continu_{etat['passages']}"]
        print(f"{datetime.now():%d/%m %H:%M} passage {etat['passages']} (graine {etat['seed']}, cumul {etat['configurations']:,})", flush=True)
        creation = 0x00004000 if sys.platform == "win32" else 0
        debut = time.time()
        proc = subprocess.run(cmd, cwd=str(home), creationflags=creation, capture_output=True, text=True, encoding="utf-8", errors="replace")
        # le passage imprime {'testees': N, ...} en dernière ligne
        testees = 0
        for ligne in (proc.stdout or "").splitlines()[::-1]:
            if "'testees':" in ligne:
                try:
                    testees = int(ligne.split("'testees':")[1].split(",")[0])
                except ValueError:
                    pass
                break
        etat["configurations"] += testees
        if semaine_m1 and proc.returncode == 0:
            etat["week_end_m1"] = semaine_m1
        etat["derniere"] = {"date": datetime.now(timezone.utc).isoformat(), "testees": testees, "code": proc.returncode,
                            "duree_sec": round(time.time() - debut), "sortie": (proc.stdout or "")[-400:]}
        if proc.returncode != 0:
            # 2026-10-01 : deux passages en échec (code 1) sans aucune trace ; la fin de la sortie d'erreur est gardée
            erreur = "\n".join(l for l in (proc.stderr or "").splitlines() if "Warning" not in l and "warn(" not in l)
            etat["derniere"]["erreur"] = erreur[-1500:]
            print("   erreur : " + (erreur.strip().splitlines() or ["(vide)"])[-1][:300], flush=True)
        etat_f.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"   → {testees:,} configurations, code {proc.returncode}, {round(time.time() - debut)} s", flush=True)
        if proc.returncode != 0:
            time.sleep(300)                      # échec (terminal occupé…) : on laisse respirer avant de reprendre
    print("arrêt demandé (state/recherche_continue.stop)", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
