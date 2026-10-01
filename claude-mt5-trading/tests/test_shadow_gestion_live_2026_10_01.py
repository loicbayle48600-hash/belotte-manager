"""2026-10-01, décision utilisateur (« 1 oui ») : shadow et papier gérés comme le live (sorties partielles, break-even,
protection du gain, stop suiveur, sortie sans progression), pour juger les agents sur la même base que le réel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from tradinglab.execution.position_manager import PMConfig
from tradinglab.learning.store import LearningStore
from tradinglab.shadow.shadow import ShadowPosition, ShadowTrader

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
CFG = PMConfig(tp1_r=1.5, tp1_close_percent=30, tp2_r=2.5, tp2_close_percent=40, break_even_r=1.0, break_even_offset_r=0.05,
               trailing_start_r=1.05, trailing_atr_multiplier=1.5, protect_profit_risk_ratio=0.5,
               protect_profit_lock_ratio=0.25, protect_profit_lock_fraction=0.5)


def _barres(prix, t0=T0 + timedelta(minutes=5)):
    t = pd.date_range(t0, periods=len(prix) + 1, freq="5min", tz="UTC")
    p = list(prix) + [prix[-1]]                              # dernière bougie = bougie en cours (ignorée)
    return pd.DataFrame({"time": t, "open": p, "high": [x + 0.0001 for x in p], "low": [x - 0.0001 for x in p], "close": p})


def _trader(tmp_path, gestion=True, npe=None):
    st = ShadowTrader(LearningStore(tmp_path / "l.db"), tmp_path / "shadow.json")
    if gestion:
        st.gestion = CFG
        st.sans_progression = npe or {}
    return st


def _pos(tf="M15"):
    return ShadowPosition(id="p1", agent_id="A1", symbol="EURUSD", side="BUY", entry=1.1000, sl=1.0980, tp=1.1060,
                          opened_at=T0.isoformat(), regime="TRENDING", session="LONDON", setup_score=70.0, entry_tf=tf)


def test_monte_puis_redescend_finit_en_gain(tmp_path):
    montee = [1.1004, 1.1010, 1.1016, 1.1022, 1.1028, 1.1031]          # jusqu'à ≈ +1,6 R (TP1 à +1,5 R)
    descente = [1.1024, 1.1016, 1.1008, 1.0998, 1.0990, 1.0985]        # retour sous l'entrée
    df = _barres(montee + descente)
    snap = SimpleNamespace(frames={"M5": df}, atr_h1=0.0010)
    st = _trader(tmp_path)
    st.positions["p1"] = _pos()
    fermes = st.update({"EURUSD": snap}, T0 + timedelta(hours=2))
    assert len(fermes) == 1, "le verrou du gain a fermé la position"
    r = fermes[0]
    assert r.exit_reason == "stop_suiveur" and r.features["realise_r"] >= 0.44
    assert 0.8 <= r.result_r <= 1.3, r.result_r

    sans = _trader(tmp_path / "sans", gestion=False)
    sans.positions["p1"] = _pos()
    assert sans.update({"EURUSD": snap}, T0 + timedelta(hours=2)) == [], "stop et cible fixes : toujours ouverte"


def test_sortie_sans_progression_m15_apres_8_h(tmp_path):
    plat = [1.1001, 1.0999] * 60                                         # 10 h de bougies M5 à ± 0,1 R
    df = _barres(plat)
    snap = SimpleNamespace(frames={"M5": df}, atr_h1=0.0010)
    st = _trader(tmp_path, npe={"enabled": True, "max_hours": 8, "min_r": 0.2, "timeframes": ["M15"]})
    st.positions["p1"] = _pos("M15")
    fermes = st.update({"EURUSD": snap}, T0 + timedelta(hours=11))
    assert len(fermes) == 1 and fermes[0].exit_reason == "sans_progression" and abs(fermes[0].result_r) < 0.2

    st2 = _trader(tmp_path / "h1", npe={"enabled": True, "max_hours": 8, "min_r": 0.2, "timeframes": ["M15"]})
    st2.positions["p1"] = _pos("H1")
    assert st2.update({"EURUSD": snap}, T0 + timedelta(hours=11)) == [], "agent H1 : pas de sortie sans progression"


def test_traitement_incremental_et_etat_sauvegarde(tmp_path):
    df1 = _barres([1.1004, 1.1010, 1.1016, 1.1022, 1.1028, 1.1031])
    st = _trader(tmp_path)
    st.positions["p1"] = _pos()
    st.update({"EURUSD": SimpleNamespace(frames={"M5": df1}, atr_h1=0.0010)}, T0 + timedelta(hours=1))
    p = st.positions["p1"]
    assert p.tp1_fait and p.protege and p.sl_courant > p.entry and p.derniere_barre
    relu = ShadowTrader(LearningStore(tmp_path / "l.db"), tmp_path / "shadow.json")       # redémarrage : état relu
    q = relu.positions["p1"]
    assert q.tp1_fait and q.sl_courant == p.sl_courant and q.derniere_barre == p.derniere_barre
