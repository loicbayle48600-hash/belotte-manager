"""2026-09-30, demande utilisateur « fais ce qui est le mieux » sur les propositions stop / cible.

- option `profit_management.final_target_r` (cible broker fixe en R depuis le prix réel) : exécuteur, backtest et rejeu.
  NON activée : le rejeu de la semaine (106 trades réels) donne −0,95 R avec une cible à 3 R ; sans la clé, rien ne change ;
- le rejeu simule désormais la protection du profit du bot (seuil 0,5 R, verrou max(0,25 R ; ½ meilleur R))."""
from __future__ import annotations

import pandas as pd
import pytest

from tradinglab.research.replay import ReplayCandidate, simulate
from tests.test_risk_and_gate import ctx_for, executor_for, gate_for, make_candidate


def _bars(prix):
    t = pd.date_range("2026-09-25T10:05:00+00:00", periods=len(prix), freq="5min", tz="UTC")
    return pd.DataFrame({"time": t, "open": prix, "high": [p + 0.0002 for p in prix],
                         "low": [p - 0.0002 for p in prix], "close": prix})


def _c(tp=1.1060):
    return ReplayCandidate(ts="2026-09-25T10:00:00+00:00", key="k", symbol="EURUSD", side=1, agent_id="E05",
                           entry=1.1000, sl=1.0980, tp=tp, atr=0.0010, checks={}, cost_ratio=0.0)


def test_config_n_active_pas_la_cible_fixe(settings):
    assert not float(settings.profit_management.get("final_target_r") or 0.0), "rejeu défavorable : option non activée"


def test_executeur_pose_la_cible_a_3r_du_prix_reel(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    ex.final_target_r = 3.0
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    out = ex.execute(c, res, req, 100.0, 0.1)
    assert out.executed
    pos = broker.position(out.position.ticket)
    r = abs(pos.price_open - pos.sl)
    sens = 1 if pos.side.value == "BUY" else -1
    assert pos.tp == pytest.approx(pos.price_open + sens * 3.0 * r, abs=2 * broker.symbol_info(pos.symbol).point)


def test_executeur_sans_option_garde_la_cible_du_signal(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    assert ex.final_target_r == 0.0
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    out = ex.execute(c, res, req, 100.0, 0.1)
    pos = broker.position(out.position.ticket)
    rr = abs(pos.tp - pos.price_open) / abs(pos.price_open - pos.sl)
    rr_signal = abs(c.tp_plan[-1] - c.entry) / abs(c.entry - c.sl)
    assert rr >= rr_signal - 1e-6 and abs(rr - 3.0) > 1e-3


def test_backtest_cible_finale(settings):
    from tradinglab.backtest.engine import BTCosts, Signal, run_backtest
    from tradinglab.core.types import Side

    n = 400
    prix = [1.10 + 0.00002 * i for i in range(n)]                     # hausse régulière : la cible est atteinte
    df = pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"), "open": prix,
                       "high": [p + 0.0003 for p in prix], "low": [p - 0.0003 for p in prix], "close": prix})
    fait = {"x": False}

    def sig(d):
        if len(d) == 250 and not fait["x"]:
            fait["x"] = True
            e = float(d["close"].iloc[-1])
            return Signal(side=Side.BUY, sl=e - 0.0010, tp=e + 0.0015)
        return None

    costs = BTCosts(spread_points=0, commission_per_lot=0.0, slippage_points=0, point=0.00001, tick_value=1.0, tick_size=0.00001)
    base = {"tp1_r": 1.5, "tp1_close_percent": 0, "tp2_r": 2.5, "tp2_close_percent": 0, "break_even_enabled": False,
            "trailing_enabled": False}
    t_signal = run_backtest(df, sig, costs, management=base, warmup=200).trades[0]
    fait["x"] = False
    t_3r = run_backtest(df, sig, costs, management={**base, "final_target_r": 3.0}, warmup=200).trades[0]
    assert t_signal.r_multiple == pytest.approx(1.5, abs=0.05)
    assert t_3r.r_multiple == pytest.approx(3.0, abs=0.05)


def test_rejeu_cible_fixe_et_protection_du_profit():
    # cible fixe 3 R : le TP du candidat (3 R aussi ici) est remplacé par entrée + 3 R
    monte = [1.1000 + 0.0002 * i for i in range(1, 40)]
    r_signal, _ = simulate(_c(tp=1.1030), _bars(monte), {"tp1_close_percent": 0, "tp2_close_percent": 0})
    r_3r, _ = simulate(_c(tp=1.1030), _bars(monte), {"tp1_close_percent": 0, "tp2_close_percent": 0, "final_target_r": 3.0})
    assert r_signal == pytest.approx(1.5, abs=0.01) and r_3r == pytest.approx(3.0, abs=0.01)
    # protection : monte à +0,8 R puis retombe au stop initial
    aller_retour = [1.1004, 1.1008, 1.1012, 1.1016, 1.1010, 1.1000, 1.0990, 1.0975]
    pm = {"tp1_close_percent": 0, "tp2_close_percent": 0, "break_even_r": 1.0, "trailing_start_r": 1.05,
          "trailing_atr_multiplier": 1.5}
    sans, _ = simulate(_c(), _bars(aller_retour), pm)
    avec, _ = simulate(_c(), _bars(aller_retour), {**pm, "protect_profit_risk_ratio": 0.5, "protect_profit_lock_ratio": 0.25,
                                                   "protect_profit_lock_fraction": 0.5})
    assert sans == pytest.approx(-1.0, abs=0.01), "sans protection : stop plein"
    assert avec > 0.25, "avec protection : le verrou garde au moins 0,25 R (ici ½ du meilleur R)"
