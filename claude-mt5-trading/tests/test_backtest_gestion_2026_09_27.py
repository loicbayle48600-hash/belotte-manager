"""Le backtest reproduit la gestion du bot (2026-09-27, plan pro point 2) : break-even, TP partiels, stop suiveur.

Avant : sortie au SL / TP fixe uniquement — un trade monté à +1,6 R puis revenu au stop comptait −1 R, alors que le
bot aurait encaissé TP1 (30 % à 1,5 R) et remonté le stop au break-even."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradinglab.backtest.engine import BTCosts, Signal, run_backtest, set_default_management
from tradinglab.core.types import Side

GESTION = {"tp1_r": 1.5, "tp1_close_percent": 30, "tp2_r": 2.5, "tp2_close_percent": 40, "break_even_enabled": True,
           "break_even_r": 1.0, "break_even_offset_r": 0.05, "trailing_enabled": True, "trailing_start_r": 2.0,
           "trailing_atr_multiplier": 1.5}
ZERO = BTCosts(spread_points=0, slippage_points=0, commission_per_lot=0.0)


def _df(closes, hi=0.0, lo=0.0):
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [{"time": t0 + timedelta(hours=k), "open": c, "high": c + hi, "low": c - lo, "close": c, "tick_volume": 1,
             "real_volume": 0, "spread": 0} for k, c in enumerate(closes)]
    return pd.DataFrame(rows)


def _signal_une_fois(entry_bar: int):
    def fn(df):
        if len(df) == entry_bar:
            c = float(df["close"].iloc[-1])
            return Signal(side=Side.BUY, sl=c - 10.0, tp=c + 100.0, note="test")
        return None
    return fn


def test_tp1_et_break_even_changent_le_resultat():
    # 220 barres plates (warmup), entrée à l'open de la barre 221 à 100 ; monte à 116 (+1,6 R) puis retombe à 89
    closes = [100.0] * 221 + [100.0, 108.0, 116.0, 105.0, 100.4, 89.0, 89.0]
    df = _df(closes)
    sans = run_backtest(df, _signal_une_fois(221), ZERO, warmup=200, management=None)
    avec = run_backtest(df, _signal_une_fois(221), ZERO, warmup=200, management=GESTION)
    assert len(sans.trades) == 1 and sans.trades[0].r_multiple == pytest.approx(-1.0)
    t = avec.trades[0]
    # 30 % encaissés à 1,5 R, le reste sorti au break-even (+0,05 R) : ≈ 0,45 + 0,7 × 0,05
    assert t.r_multiple == pytest.approx(0.3 * 1.5 + 0.7 * 0.05, abs=0.02)
    assert t.exit_reason == "sl" and t.exit > 100.0


def test_sans_gestion_par_defaut_comportement_inchange():
    set_default_management(None)          # un autre test (pipeline) peut avoir branché la gestion par défaut
    closes = [100.0] * 221 + [100.0, 108.0, 116.0, 105.0, 100.4, 89.0, 89.0]
    res = run_backtest(_df(closes), _signal_une_fois(221), ZERO, warmup=200)
    assert res.trades[0].r_multiple == pytest.approx(-1.0)
