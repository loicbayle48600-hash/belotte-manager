"""Recherche en masse (research/massive.py) : grille, garde-fous (seuil selon le nombre d'essais, plateau, contrôle)."""
from __future__ import annotations

import math

import numpy as np

from tradinglab.backtest.engine import BTCosts
from tradinglab.research import massive as m

from test_fastsig_2026_09_29 import SPEC_SYM, _synth


def test_grille_et_parametres():
    g = m.grid(["ema_trend", "sr_rejection"], ["forex"], [("M15", "H1")])
    assert len(g) == (4 + 3 * 2) * len(m.SL_ATR) * len(m.RR)
    assert all(c["params"]["sl_atr"] in m.SL_ATR for c in g)
    assert any(c["params"].get("rsi_lo") == 20 for c in m.grid(["bollinger_mr"], ["forex"], [("M15", "H1")]))


def test_seuil_croit_avec_le_nombre_d_essais():
    assert m.seuil_multiple(100) < m.seuil_multiple(30000) < m.seuil_multiple(1_000_000)
    assert 4.4 < m.seuil_multiple(30000) < 4.7


def test_selection_plateau_et_controle():
    def cfg(sl):
        return {"strategy": "ema_trend", "entry_tf": "H4", "trend_tf": "D1", "asset_class": "metals", "params": {"adx_min": 20, "sl_atr": sl, "rr": 2.0}}
    configs = [cfg(sl) for sl in m.SL_ATR]
    bon = np.array([200, 120, 80, 260.0, 100.0, 160.0, 400.0, 5.0])          # t ≈ 11 : très significatif
    mauvais = np.array([200, 80, 120, 100.0, 140.0, -40.0, 400.0, 20.0])
    res = {i: [(bon, bon * [0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 1])] for i in range(len(configs))}
    res[0] = [(mauvais, mauvais)]                                              # 0,8 ATR perdant
    retenues, etapes = m.select(configs, res)
    stops = [c["params"]["sl_atr"] for c, _, _ in retenues]
    assert 0.8 not in stops and 1.0 not in stops            # 1,0 : voisine 0,8 perdante → pas de plateau
    assert 1.5 in stops and etapes["controle"] == len(retenues)


def test_tache_sur_donnees_synthetiques():
    df = _synth(3000, 4)
    m._init({("EURUSD", "M15"): df}, {"EURUSD": SPEC_SYM},
            {"EURUSD": BTCosts(spread_points=10, slippage_points=3, point=SPEC_SYM.point, tick_value=1.0, tick_size=SPEC_SYM.tick_size)}, None)
    cfgs = m.grid(["ema_trend"], ["forex"], [("M15", "H1")])[:6]
    out = m._task(("EURUSD", "M15", "H1", "ema_trend", cfgs))
    assert len(out) == 6 and all(o is not None and o[0].shape == (8,) for o in out)
    assert sum(o[0][0] for o in out) > 0                                        # des trades en apprentissage
