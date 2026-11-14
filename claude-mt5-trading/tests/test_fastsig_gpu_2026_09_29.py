"""Signaux vectorisés PAR PAQUETS et sur la carte graphique (2026-09-29, portage 3090).

Pour chaque stratégie de fastsig.FAST : un appel avec `sl_atr` et `rr` en colonnes (P, 1) doit donner, ligne par ligne,
exactement les signaux de P appels séparés (processeur, nombres simples) — d'abord en numpy, puis sur la 3090 (cupy)."""
from __future__ import annotations

import numpy as np
import pytest

from tradinglab.backtest import fastsig
from tradinglab.research.adapters import make_signal_fn

from test_fastsig_2026_09_29 import SPEC_SYM, _datasets, _spec

SL = [0.8, 1.0, 1.5, 2.5]
RR = [1.5, 2.0, 3.0]


def _cupy():
    cp = pytest.importorskip("cupy")
    try:
        cp.cuda.runtime.getDeviceCount()
    except Exception:  # noqa: BLE001
        pytest.skip("pas de GPU")
    return cp


def _ctx(strategy, params, di=0):
    nom, df, entry, trend = _datasets()[di]
    f = make_signal_fn(_spec(strategy, params, entry, trend), SPEC_SYM, entry)
    f.prepare(df)
    return f.fast_ctx()


def _ref(ctx, strategy, params):
    out = []
    for sl in SL:
        for rr in RR:
            out.append(fastsig.FAST[strategy](ctx, {**params, "sl_atr": sl, "rr": rr}))
    return (np.stack([o[0] for o in out]), np.stack([o[1] for o in out]), np.stack([o[2] for o in out]))


def _batch(ctx, strategy, params, xp):
    sl = xp.asarray(np.repeat(SL, len(RR)), dtype=float)[:, None]
    rr = xp.asarray(np.tile(RR, len(SL)), dtype=float)[:, None]
    side, s, t = fastsig.FAST[strategy](ctx.on(xp), {**params, "sl_atr": sl, "rr": rr})
    P, n = len(SL) * len(RR), ctx.n
    tonp = (lambda a: a.get()) if xp is not np else (lambda a: a)
    side = np.broadcast_to(tonp(side), (P, n)); s = np.broadcast_to(tonp(s), (P, n)); t = np.broadcast_to(tonp(t), (P, n))
    return side, s, t


def _egal(a, b):
    assert np.array_equal(a[0], b[0]), f"sens : {int((a[0] != b[0]).sum())} écarts"
    for k in (1, 2):
        assert np.allclose(a[k], b[k], rtol=1e-12, atol=0, equal_nan=True), f"niveaux : écart max {np.nanmax(np.abs(a[k] - b[k]))}"


CASES = [(s, i) for s in sorted(fastsig.FAST) for i in range(len(fastsig.TEST_PARAMS[s]))]


@pytest.mark.parametrize("strategy,pi", CASES)
@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_paquet_egal_aux_appels_separes(strategy, pi, device):
    prm = {k: v for k, v in fastsig.TEST_PARAMS[strategy][pi].items() if k not in ("sl_atr", "rr")}
    for di in (0, 2):
        ctx = _ctx(strategy, prm, di)
        xp = np if device == "cpu" else _cupy()
        _egal(_ref(ctx, strategy, prm), _batch(ctx, strategy, prm, xp))
