"""swing_points vectorisé (2026-09-29, accélération des backtests) : résultat identique à la boucle d'origine."""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradinglab.market_data.indicators import _swing_points_boucle, swing_points


def test_identique_a_la_boucle_sur_donnees_variees():
    rng = np.random.default_rng(7)
    for n in (0, 3, 7, 8, 50, 500):
        for left, right in ((3, 3), (2, 5), (5, 2), (1, 1)):
            h = np.round(100 + np.cumsum(rng.normal(0, 1, n)), 1)          # arrondi : beaucoup de plateaux
            lo = h - np.abs(np.round(rng.normal(0, 1, n), 1))
            if n > 20:
                h[10] = np.nan                                               # trous éventuels
            df = pd.DataFrame({"high": h, "low": lo})
            assert swing_points(df, left, right) == _swing_points_boucle(df, left, right), (n, left, right)



def test_rsi2_precalcule_egal_au_calcul_sur_chaque_prefixe():
    """La colonne rsi2 d'enrich (série entière) vaut, à chaque bougie, le RSI(2) recalculé sur le seul préfixe."""
    from tradinglab.market_data.indicators import enrich, rsi
    t = pd.date_range("2026-09-01", periods=400, freq="15min", tz="UTC")
    c = 1.1 + np.cumsum(np.random.default_rng(5).normal(0, 0.001, 400))
    c[100:110] = c[100]                                            # plateau
    e = enrich(pd.DataFrame({"time": t, "open": c, "high": c + .001, "low": c - .001, "close": c, "tick_volume": 1, "spread": 10}))
    for k in range(3, 400, 7):
        a, b = e["rsi2"].iloc[k - 1], rsi(e["close"].iloc[:k], 2).iloc[-1]
        assert (np.isnan(a) and np.isnan(b)) or a == b, k
