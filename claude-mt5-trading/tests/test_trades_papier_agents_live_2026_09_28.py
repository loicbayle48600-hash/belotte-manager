"""Trades papier des agents LIVE et réglages temporaires de test (2026-09-28, décision utilisateur « 1 et 2 »).

1. Un signal d'agent LIVE que le compte ne peut pas prendre pour une raison de CAPACITÉ est suivi en papier
   (mode « paper », raison conservée) ; un refus de QUALITÉ ne l'est jamais.
2. Risque par trade 0,125 → 0,05 % et verrou de pertes consécutives 3 → 5, TEMPORAIRES, plafonds de concentration
   inchangés en % d'equity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import yaml

from tradinglab.core.types import Side
from tradinglab.learning.store import LearningStore
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.shadow.shadow import ShadowTrader

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _cand(agent="B02", symbol="XAUUSD", key="k1"):
    return SimpleNamespace(id="c1", agent_id=agent, symbol=symbol, side=Side.SELL, entry=3800.0, sl=3810.0, tp_plan=[3780.0],
                           sl_distance=10.0, regime=SimpleNamespace(value="TRENDING"), session="LONDON", setup_score=70.0,
                           bar_time="2026-09-28T11:45:00+00:00", idempotency_key=key)


def test_signal_live_non_pris_suivi_en_papier_avec_sa_raison(tmp_path):
    recs = []
    store = SimpleNamespace(record_trade=lambda r: recs.append(r), agent_event=lambda *a, **k: None)
    sh = ShadowTrader(store, tmp_path / "sh.json", max_open=10)
    ouverts = sh.open_from_candidates([_cand()], NOW, mode="paper", reason="entrées verrouillées : 3 pertes consécutives")
    assert len(ouverts) == 1 and ouverts[0].mode == "paper"
    # le prix touche le TP (vente) sur la barre suivante
    t = [NOW + timedelta(minutes=5 * k) for k in range(1, 4)]
    df = pd.DataFrame({"time": t, "open": 3800.0, "high": [3805.0, 3790.0, 3785.0], "low": [3795.0, 3775.0, 3780.0], "close": 3785.0})
    closed = sh.update({"XAUUSD": SimpleNamespace(frames={"M5": df})}, NOW + timedelta(minutes=20))
    assert len(closed) == 1 and closed[0].mode == "paper" and closed[0].exit_reason == "tp"
    assert closed[0].features["paper_reason"] == "entrées verrouillées : 3 pertes consécutives"
    # la position rechargée depuis le disque garde mode et raison ; l'ancien format (sans ces champs) reste lisible
    sh2 = ShadowTrader(store, tmp_path / "sh.json", max_open=10)
    sh2.open_from_candidates([_cand(key="k2")], NOW)
    assert all(p.mode == "shadow" for p in sh2.positions.values())


def test_refus_du_gate_capacite_ou_qualite():
    r = Orchestrator._refus_capacite
    assert r(["09_daily_guard", "14_max_consecutive_losses", "20_final_approval"])
    assert r(["14_max_positions_per_symbol", "14_no_averaging_or_grid", "17_existing_position"])
    assert r(["15_currency_factor_risk", "15_correlated_cluster_risk"])
    assert not r(["07_spread", "09_daily_guard"])                    # le spread est une question de qualité
    assert not r(["10_stop_loss"]) and not r(["08_news"]) and not r([])
    assert not r(["20_final_approval"])                              # seul : score ou RR insuffisant, pas la capacité


def test_filtre_de_mode_du_magasin():
    assert LearningStore._mode_clause("live") == ("mode=?", ["live"])
    assert LearningStore._mode_clause("live+paper") == ("mode IN ('live', 'paper')", [])
    assert LearningStore._mode_clause("all") == ("1=1", []) and LearningStore._mode_clause(None) == ("1=1", [])


def test_reglages_temporaires_du_28_09():
    risk = yaml.safe_load(open("config/risk.yaml", encoding="utf-8"))
    # 01/10, décision utilisateur : fin de la période de test (0,05 % et 5 pertes du 28/09 au 01/10)
    assert risk["risk"]["risk_per_trade_percent"] == 0.125
    assert risk["risk"]["max_consecutive_losses"] == 3
    assert risk["risk"]["max_daily_loss_internal_percent"] == 2.5
    assert risk["risk"]["max_total_open_risk_percent"] / risk["risk"]["risk_per_trade_percent"] <= risk["risk"]["max_open_positions"]
    strat = yaml.safe_load(open("config/strategies.yaml", encoding="utf-8"))
    assert strat["learning"]["paper_trades_live_agents"] is True
