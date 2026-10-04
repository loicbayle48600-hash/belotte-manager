"""Chargement doux des historiques (2026-09-28) : cache disque, pause, arrêt si la boucle de trading ne boucle plus."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from tradinglab.research.rates_cache import TerminalOccupe, load_rates


def _df(n, fin):
    t = pd.date_range(end=fin, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"time": t, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "tick_volume": 1, "real_volume": 0, "spread": 1})


def test_cache_evite_une_seconde_lecture(tmp_path):
    appels = []
    broker = SimpleNamespace(rates=lambda s, tf, n: appels.append((s, tf, n)) or _df(n, datetime.now(timezone.utc)))
    d1 = load_rates(broker, "EURUSD", "H1", 50, tmp_path, pause_sec=0.0)
    d2 = load_rates(broker, "EURUSD", "H1", 50, tmp_path, pause_sec=0.0)
    assert len(appels) == 1 and len(d1) == 50 and len(d2) == 50


def test_cache_perime_relu(tmp_path):
    appels = []
    vieux = datetime.now(timezone.utc) - timedelta(days=3)
    broker = SimpleNamespace(rates=lambda s, tf, n: appels.append(1) or _df(n, vieux if len(appels) == 1 else datetime.now(timezone.utc)))
    load_rates(broker, "EURUSD", "H1", 50, tmp_path, pause_sec=0.0)
    load_rates(broker, "EURUSD", "H1", 50, tmp_path, pause_sec=0.0)     # cache trop vieux : relu
    assert len(appels) == 2


def test_arret_si_l_orchestrateur_ne_boucle_plus(tmp_path):
    etat = tmp_path / "system_state.json"
    etat.write_text(json.dumps({"last_cycle": {"ts": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}}), encoding="utf-8")
    broker = SimpleNamespace(rates=lambda s, tf, n: _df(n, datetime.now(timezone.utc)))
    with pytest.raises(TerminalOccupe):
        load_rates(broker, "EURUSD", "H1", 50, tmp_path / "c", pause_sec=0.0, state_file=etat, max_cycle_age_sec=90)
