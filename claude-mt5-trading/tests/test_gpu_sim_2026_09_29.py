"""Simulation en masse (backtest/gpu_sim.py) : mêmes métriques que engine.run_backtest, sur processeur et sur GPU."""
from __future__ import annotations

import math

import numpy as np
import pytest

from tradinglab.backtest import fastsig, gpu_sim
from tradinglab.backtest.engine import BTCosts, _atr_causal, run_backtest
from tradinglab.research.adapters import make_signal_fn

from test_fastsig_2026_09_29 import SPEC_SYM, _datasets, _spec

GESTION = {"tp1_r": 1.5, "tp1_close_percent": 30, "tp2_r": 2.5, "tp2_close_percent": 40, "break_even_enabled": True,
           "break_even_r": 1.0, "break_even_offset_r": 0.05, "trailing_enabled": True, "trailing_start_r": 1.05,
           "trailing_atr_multiplier": 1.5}
COSTS = BTCosts(spread_points=10, commission_per_lot=7.0, slippage_points=3, point=SPEC_SYM.point,
                tick_value=SPEC_SYM.tick_value, tick_size=SPEC_SYM.tick_size)


def _cas():
    nom, df, entry, trend = _datasets()[0]
    specs = [_spec(st, prm, entry, trend) for st in sorted(fastsig.FAST) for prm in fastsig.TEST_PARAMS[st][:2]]
    fns = [make_signal_fn(sp, SPEC_SYM, entry) for sp in specs]
    for f in fns:
        f.prepare(df)
    arr = [f.fast_arrays() for f in fns]
    side = np.stack([a[0] for a in arr]); sl = np.stack([a[1] for a in arr]); tp = np.stack([a[2] for a in arr])
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    return df, fns, (o, h, l, c, _atr_causal(h, l, c), side, sl, tp)


@pytest.mark.parametrize("gestion", [None, GESTION])
@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_memes_metriques_que_run_backtest(gestion, device):
    if device == "gpu":
        pytest.importorskip("numba.cuda")
        from numba import cuda
        if not cuda.is_available():
            pytest.skip("pas de GPU")
    df, fns, a = _cas()
    out = gpu_sim.simulate(*a, spread=COSTS.spread, slip=COSTS.slippage, mgmt=gestion, device=device)
    total = 0
    for k, f in enumerate(fns):
        ref = run_backtest(df, f, COSTS, management=gestion).metrics
        m = gpu_sim.metrics_from(out[k])
        total += ref.sample_size
        assert m["sample_size"] == ref.sample_size and m["wins"] == ref.wins, k
        for cle in ("expectancy_r", "total_r", "max_drawdown_r", "win_rate"):
            assert math.isclose(m[cle], getattr(ref, cle), rel_tol=1e-9, abs_tol=1e-9), (k, cle, m[cle], getattr(ref, cle))
        assert (math.isinf(m["profit_factor"]) and math.isinf(ref.profit_factor)) or \
            math.isclose(m["profit_factor"], ref.profit_factor, rel_tol=1e-9, abs_tol=1e-9), k
    assert total > 100
