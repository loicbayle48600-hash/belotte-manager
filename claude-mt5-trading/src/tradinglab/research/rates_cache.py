"""Chargement DOUX des historiques de prix pour la recherche (2026-09-28).

Le 28/09 à 00 h 48, l'optimiseur a demandé au terminal MT5 partagé 52 historiques de 10 000 barres d'affilée :
l'orchestrateur, qui gère les positions ouvertes sur le même terminal, n'a plus bouclé pendant 7 minutes. Ici :

- cache sur disque (`data/cache/rates/<symbole>_<tf>.csv`) : un historique déjà lu n'est pas redemandé ;
- une pause entre deux lectures du terminal ;
- garde-fou : si la boucle de trading n'a plus terminé de cycle depuis `max_cycle_age_sec`, on s'arrête (`TerminalOccupe`).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

TF_SEC = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


class TerminalOccupe(RuntimeError):
    """La boucle de trading ne boucle plus : on cesse de solliciter le terminal."""


def cycle_age_sec(state_file: Path) -> Optional[float]:
    """Âge du dernier cycle complet de l'orchestrateur (None si inconnu)."""
    try:
        d = json.loads(state_file.read_text(encoding="utf-8"))
        ts = (d.get("last_cycle") or {}).get("ts")
        if not ts:
            return None
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(ts))).total_seconds()
    except (OSError, ValueError, TypeError):
        return None


def load_rates(broker, symbol: str, tf: str, n: int, cache_dir: Path, pause_sec: float = 2.0,
               state_file: Optional[Path] = None, max_cycle_age_sec: float = 90.0) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{symbol}_{tf}.csv"
    if f.exists():
        try:
            df = pd.read_csv(f, parse_dates=["time"])
            df["time"] = pd.to_datetime(df["time"], utc=True)
            derniere = df["time"].iloc[-1].to_pydatetime()
            age = (datetime.now(timezone.utc) - derniere).total_seconds()
            if len(df) >= n and age <= 3 * TF_SEC.get(tf, 3600):
                return df.tail(n).reset_index(drop=True)
        except (OSError, ValueError, KeyError):
            pass
    if state_file is not None:
        age = cycle_age_sec(state_file)
        if age is not None and age > max_cycle_age_sec:
            raise TerminalOccupe(f"orchestrateur sans cycle depuis {age:.0f} s : lecture du terminal suspendue")
    df = broker.rates(symbol, tf, n)
    time.sleep(pause_sec)
    if df is not None and len(df):
        try:
            df.to_csv(f, index=False)
        except OSError:
            pass
    return df
