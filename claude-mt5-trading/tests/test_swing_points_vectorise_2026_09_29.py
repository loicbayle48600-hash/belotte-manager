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

