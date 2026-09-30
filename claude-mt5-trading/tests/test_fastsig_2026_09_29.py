"""Signaux vectorisés (backtest/fastsig.py) : équivalence EXACTE, bougie par bougie, avec les screeners (2026-09-29).

Chaque stratégie de `fastsig.FAST` est comparée au screener d'origine sur des données synthétiques (tendances, ranges,
plateaux) et, si le cache local existe, sur des données réelles. Aucun écart n'est toléré.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradinglab.agents.registry import AgentSpec
from tradinglab.backtest import fastsig
from tradinglab.core.types import Regime, SymbolSpec
from tradinglab.research.adapters import make_signal_fn

ALL_REGIMES = [r.value for r in Regime if r is not Regime.NEWS_SHOCK]
SESSIONS = ["ASIA", "LONDON", "NEWYORK", "OVERLAP_LDN_NY"]

PARAMS = fastsig.TEST_PARAMS


def _synth(n: int, seed: int, step: str = "15min") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0, 0.0004, n // 150 + 1), 150)[:n]          # alternance tendances / ranges
    c = 1.10 + np.cumsum(drift + rng.normal(0, 0.0008, n))
    c = np.round(c, 4)                                                         # plateaux fréquents
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.round(np.abs(rng.normal(0, 0.0004, n)), 4)
    lo = np.minimum(o, c) - np.round(np.abs(rng.normal(0, 0.0004, n)), 4)
    t = pd.date_range("2026-06-01", periods=n, freq=step, tz="UTC")
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": lo, "close": c, "tick_volume": 100, "spread": 10})


SPEC_SYM = SymbolSpec(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0, contract_size=100000,
                      volume_min=0.01, volume_step=0.01, volume_max=100.0, stops_level_points=0, trade_allowed=True,
                      currency_base="EUR", currency_profit="USD", currency_margin="EUR", asset_class="forex", spread_points=10)


def _datasets():
    out = [("synth1", _synth(1400, 1), "M15", "H1"), ("synth2", _synth(1400, 2), "M15", "H1"),
           ("synth3_h1h4", _synth(1300, 3, "1h"), "H1", "H4")]
    f = Path("data/cache/rates/GBPUSD_M15.csv")
    if f.exists():
        df = pd.read_csv(f, parse_dates=["time"])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        out.append(("reel_gbpusd", df.tail(1300).reset_index(drop=True), "M15", "H1"))
    return out


def _spec(strategy: str, params: dict, entry: str, trend: str, sessions=SESSIONS, regimes=ALL_REGIMES) -> AgentSpec:
    return AgentSpec(agent_id="FST", family="X", name="fast", strategy=strategy, markets=["EURUSD"], sessions=list(sessions),
                     timeframes={"entry": entry, "trend": trend}, regimes=list(regimes), params=dict(params), base_strategy=strategy)


CASES = [(s, i, d) for s in sorted(fastsig.FAST) for i in range(len(PARAMS.get(s, [{}]))) for d in range(4)]


@pytest.mark.parametrize("strategy,pi,di", CASES)
def test_equivalence_bougie_par_bougie(strategy, pi, di):
    ds = _datasets()
    if di >= len(ds):
        pytest.skip("pas de données réelles en cache")
    nom, df, entry, trend = ds[di]
    params = PARAMS.get(strategy, [{}])[pi]
    for sessions, regimes in ((SESSIONS, ALL_REGIMES), (["LONDON"], ["TRENDING", "RANGING"])):
        spec = _spec(strategy, params, entry, trend, sessions, regimes)
        lent = make_signal_fn(spec, SPEC_SYM, entry, fast=False)
        rapide = make_signal_fn(spec, SPEC_SYM, entry, fast=True)
        lent.prepare(df)
        rapide.prepare(df)
        ecarts = fastsig.compare_signals(lent, rapide, df, start=250)
        assert not ecarts, f"{strategy} {params} {nom} {sessions}: {len(ecarts)} écarts, premier {ecarts[0]}"


def test_toutes_les_strategies_rapides_ont_des_parametres_de_test():
    assert set(fastsig.FAST) <= set(PARAMS)


@pytest.mark.parametrize("strategy", sorted(fastsig.FAST))
def test_backtest_complet_identique(strategy):
    """Même liste de trades (entrée, sortie, R) avec le moteur en accès direct (signal_at) et en mode lent."""
    from tradinglab.backtest.engine import BTCosts, run_backtest
    costs = BTCosts(spread_points=10, commission_per_lot=7.0, slippage_points=3, point=SPEC_SYM.point,
                    tick_value=SPEC_SYM.tick_value, tick_size=SPEC_SYM.tick_size)
    # 2026-10-01 : filtre de session aligné sur le live (plus de signal intraday entre 21:00 et 22:00 UTC ni en SYDNEY
    # pour un agent qui ne la liste pas) : on compare sur tous les jeux (synthétiques et réel en cache), au moins un trade
    total = 0
    for nom, df, entry, trend in _datasets():
        spec = _spec(strategy, PARAMS.get(strategy, [{}])[0], entry, trend)
        res = [run_backtest(df, make_signal_fn(spec, SPEC_SYM, entry, fast=f), costs) for f in (False, True)]
        cle = [[(str(t.entry_time), t.entry, t.exit, t.r_multiple, t.exit_reason) for t in r.trades] for r in res]
        assert cle[0] == cle[1], (strategy, nom)
        total += len(cle[0])
    assert total > 0, strategy
