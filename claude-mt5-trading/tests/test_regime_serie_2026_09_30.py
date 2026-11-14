"""regime_series (2026-09-30) : même régime que classify_regime sur chaque préfixe, bougie par bougie."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from tradinglab.market_data.indicators import enrich
from tradinglab.market_data.regime import classify_regime, regime_series

from test_fastsig_2026_09_29 import _synth


def _prefixe(t: pd.DataFrame, j: int) -> pd.DataFrame:
    d = t.iloc[: j + 1]
    return pd.concat([d, d.iloc[[-1]]], ignore_index=True)


def _jeux():
    out = [enrich(_synth(1500, 1, "1h")), enrich(_synth(1200, 8, "4h"))]
    f = Path("data/cache/rates/GBPUSD_H1.csv")
    if f.exists():
        df = pd.read_csv(f, parse_dates=["time"])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        out.append(enrich(df.tail(1500).reset_index(drop=True)))
    return out


def test_egal_a_classify_regime_sur_chaque_prefixe():
    total = 0
    for t in _jeux():
        serie = regime_series(t)
        for j in range(len(t)):
            assert serie[j] == classify_regime(_prefixe(t, j)).regime.value, j
        total += len(set(serie))
    assert total >= 6                                          # plusieurs régimes différents rencontrés
