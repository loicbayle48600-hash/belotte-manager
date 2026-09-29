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


def test_univers_complet():
    u = m.univers_classes({"forex_majors": ["EURUSD"], "forex_minors": ["EURGBP", "AUDNZD"], "indices": ["US500"],
                           "metals": [], "energies": ["XBRUSD"], "crypto": ["BTCUSD"]})
    assert u == {"u_forex": ["EURUSD", "EURGBP", "AUDNZD"], "u_indices": ["US500"], "u_energies": ["XBRUSD"], "u_crypto": ["BTCUSD"]}


def test_variante_de_session_identique_au_filtre_de_l_agent():
    """Effacer les signaux hors session = agent restreint à la session (exactement)."""
    from tradinglab.core.clock import current_session
    from tradinglab.research.adapters import make_signal_fn
    from test_fastsig_2026_09_29 import _spec
    df = _synth(2500, 6)
    sess = np.array([current_session(t.to_pydatetime()).value for t in df["time"]], dtype=object)
    for st in ("ema_trend", "bollinger_mr", "structure_bos"):
        tout = make_signal_fn(_spec(st, {}, "M15", "H1"), SPEC_SYM, "M15"); tout.prepare(df)
        for ses in (["LONDON"], ["ASIA"], ["OVERLAP_LDN_NY"]):
            seul = make_signal_fn(_spec(st, {}, "M15", "H1", sessions=ses), SPEC_SYM, "M15"); seul.prepare(df)
            masque = np.where(np.isin(sess, ses) | (sess == "OFF"), tout.fast_arrays()[0], 0)
            assert np.array_equal(masque, seul.fast_arrays()[0]), (st, ses)


def test_grille_par_session():
    g = m.grid(["ema_trend"], ["forex"], [("M15", "H1")], sessions=m.SESSION_VARIANTS)
    assert len(g) == 4 * len(m.SL_ATR) * len(m.RR) * 5 and {str(c["sessions"]) for c in g} == {str(v) for v in m.SESSION_VARIANTS}
    m._init({("EURUSD", "M15"): _synth(2500, 4)}, {"EURUSD": SPEC_SYM},
            {"EURUSD": BTCosts(spread_points=10, slippage_points=3, point=SPEC_SYM.point, tick_value=1.0, tick_size=SPEC_SYM.tick_size)}, None)
    out = m._task(("EURUSD", "M15", "H1", "ema_trend", g[:10]))
    assert len(out) == 10 and out[0][0][0] >= max(o[0][0] for o in out[1:5])     # toutes sessions ≥ chaque session


def test_tache_gpu_egale_tache_cpu():
    """Même sorties (apprentissage et contrôle) sur la 3090 par paquets que sur le processeur config par config."""
    import pytest
    cp = pytest.importorskip("cupy")
    try:
        cp.cuda.runtime.getDeviceCount()
    except Exception:  # noqa: BLE001
        pytest.skip("pas de GPU")
    m._init({("EURUSD", "M15"): _synth(3000, 4)}, {"EURUSD": SPEC_SYM},
            {"EURUSD": BTCosts(spread_points=10, slippage_points=3, point=SPEC_SYM.point, tick_value=1.0, tick_size=SPEC_SYM.tick_size)},
            {"trailing_start_r": 1.05})
    cfgs = m.grid(["ema_trend"], ["forex"], [("M15", "H1")], sessions=m.SESSION_VARIANTS)[:60]
    a = m._task(("EURUSD", "M15", "H1", "ema_trend", cfgs))
    b = m._task_gpu(("EURUSD", "M15", "H1", "ema_trend", cfgs))
    for x, y in zip(a, b):
        assert np.allclose(x[0], y[0], rtol=1e-9, atol=1e-9) and np.allclose(x[1], y[1], rtol=1e-9, atol=1e-9)
    assert sum(x[0][0] for x in a) > 0
