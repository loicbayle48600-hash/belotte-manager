"""Stratégies crypto issues de la recherche (famille P, 2026-09-26) : Donchian, RSI(2), fenêtre du week-end."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradinglab.agents import crypto_strategies as cs
from tradinglab.agents.registry import default_agents
from tradinglab.core.types import Regime, Side
from tradinglab.market_data.indicators import enrich
from tradinglab.market_data.regime import RegimeResult
from tradinglab.core.clock import Session
from tradinglab.market_data.feed import MarketSnapshot

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _frame(closes, hours, spread=0.002):
    rows = [{"time": T0 + timedelta(hours=hours * k), "open": c, "high": c * (1 + spread), "low": c * (1 - spread),
             "close": c, "tick_volume": 100, "real_volume": 0, "spread": 1} for k, c in enumerate(closes)]
    return enrich(pd.DataFrame(rows))


def _snap(entry, trend, tfe="H4", tft="D1"):
    return MarketSnapshot(symbol="BTCUSD", spec=None, tick=None, frames={tfe: entry, tft: trend},
                          regime=RegimeResult(regime=Regime.TRENDING, confidence=0.6), session=Session.ASIA,
                          spread_points=1, atr_h1=0.0, data_fresh=True, data_quality="OK",
                          fetched_at=T0, bar_times={}, bar_counts={})


@pytest.fixture(scope="module")
def agents():
    return {a.agent_id: a for a in default_agents() if a.family == "P"}


def test_donchian_cassure_fraiche_seulement(agents):
    spec = agents["P12"]
    base = [100 + 0.02 * i for i in range(260)]                 # lente hausse (tendance UP)
    cassure = base + [base[-1] * 1.03, base[-1] * 1.03]        # dernière clôturée casse le plus haut ; barre en formation
    c = cs.donchian_breakout(spec, _snap(_frame(cassure, 4), _frame(base, 24)))
    assert c is not None and c.side is Side.BUY and c.sl < c.entry
    deja = base + [base[-1] * 1.03, base[-1] * 1.035, base[-1] * 1.035]   # 2e barre au-dessus : pas une cassure fraîche
    assert cs.donchian_breakout(spec, _snap(_frame(deja, 4), _frame(base, 24))) is None


def test_rsi2_achat_en_tendance_haussiere_seulement(agents):
    spec = agents["P14"]
    hausse = [100 + 0.05 * i for i in range(260)]
    creux = hausse + [hausse[-1] * 0.99, hausse[-1] * 0.985, hausse[-1] * 0.985]   # deux baisses : RSI(2) très bas
    c = cs.rsi2_reversion(spec, _snap(_frame(creux, 1), _frame(hausse, 4), "H1", "H4"))
    assert c is not None and c.side is Side.BUY
    baisse = [200 - 0.05 * i for i in range(260)]
    creux_b = baisse + [baisse[-1] * 0.99, baisse[-1] * 0.985, baisse[-1] * 0.985]  # sous l'EMA200 : pas d'achat
    c2 = cs.rsi2_reversion(spec, _snap(_frame(creux_b, 1), _frame(baisse, 4), "H1", "H4"))
    assert c2 is None or c2.side is Side.SELL


def test_fenetre_du_week_end(agents):
    spec = agents["P15"]
    hausse = [100 + 0.05 * i for i in range(300)]
    def frame_fin(dernier_ouverture):
        f = _frame(hausse, 1)
        f["time"] = [dernier_ouverture - timedelta(hours=len(f) - 2 - k) for k in range(len(f))]
        return f
    samedi_14h = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)   # barre 14-15 h clôturée → entrée à 15 h
    assert cs.weekend_window(spec, _snap(frame_fin(samedi_14h), _frame(hausse, 4), "H1", "H4")) is not None
    mardi_14h = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    assert cs.weekend_window(spec, _snap(frame_fin(mardi_14h), _frame(hausse, 4), "H1", "H4")) is None


def test_rsi2_en_shadow_les_autres_live(agents):
    """P14 (RSI(2), PF 0,75 en backtest) passe en SHADOW le 2026-09-26 à la demande de l'utilisateur."""
    assert agents["P14"].status == "SHADOW"
    assert all(a.status == "LIVE" for k, a in agents.items() if k != "P14")



def test_crypto_multi_unites_de_temps(agents):
    """2026-09-26 : 16 agents crypto en M5, M15, H4, D1 ; M5/M15 limités aux cryptos à spread faible."""
    mt = {k: a for k, a in agents.items() if int(k[1:]) >= 16}
    assert len(mt) == 16
    for a in mt.values():
        if a.timeframes["entry"] in ("M5", "M15"):
            assert a.markets == ["BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD"]
        if a.timeframes["entry"] == "D1":
            assert a.params["sl_atr"] <= 0.75
