"""Ordres en attente (2026-09-28, décision utilisateur, SHADOW uniquement) : jumeaux Q, transformation du candidat,
simulation dans l'ombre et dans le backtest, refus en réel tant que l'exécution n'est pas branchée."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from tradinglab.agents.registry import AgentSpec, default_agents
from tradinglab.agents.screeners import apply_pending_entry, resolve_screener
from tradinglab.backtest.engine import BTCosts, Signal, run_backtest
from tradinglab.core.types import Side, TradeCandidate, Regime
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.shadow.shadow import ShadowTrader

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _spec(kind, off=None, expiry=3):
    p = {"entry_kind": kind, "expiry_bars": expiry}
    if off is not None:
        p["entry_offset_atr"] = off
    return AgentSpec(agent_id="Q99", family="Q", name="t", strategy="breakout_retest", markets=["XAUUSD"], sessions=["LONDON"],
                     timeframes={"entry": "H1", "trend": "H4"}, regimes=["TRENDING"], params=p)


def _cand(side=Side.BUY, entry=3800.0, sl=3790.0, atr=10.0):
    tp = entry + side.sign * 25.0
    return TradeCandidate(symbol="XAUUSD", side=side, entry=entry, sl=sl, tp_plan=[entry + side.sign * 15.0, tp], timeframes=["H1", "H4"],
                          regime=Regime.TRENDING, agent_id="Q99", atr=atr, bar_time="2026-09-28T11:00:00+00:00")


SNAP = SimpleNamespace(atr_h1=10.0, spec=SimpleNamespace(digits=2))


def test_transformation_limite_et_stop():
    c = apply_pending_entry(_spec("LIMIT", 0.3), _cand(), SNAP)
    assert c.entry_kind == "LIMIT" and c.order_price == 3797.0 and c.entry == 3797.0 and c.expiry_bars == 3
    assert c.rr == round(abs(3825.0 - 3797.0) / 7.0, 2)                       # distance au stop raccourcie
    s = apply_pending_entry(_spec("STOP", 0.15), _cand(side=Side.SELL, entry=3800.0, sl=3810.0), SNAP)
    assert s.entry_kind == "STOP" and s.order_price == 3798.5                  # vente stop sous le prix
    assert apply_pending_entry(_spec("LIMIT", 1.5), _cand(), SNAP) is None    # ordre sous le stop : refusé
    m = apply_pending_entry(AgentSpec(agent_id="X", family="X", name="m", strategy="ema_trend", markets=["XAUUSD"], sessions=["LONDON"],
                                      timeframes={"entry": "H1", "trend": "H4"}, regimes=["TRENDING"], params={}), _cand(), SNAP)
    assert m.entry_kind == "MARKET" and m.entry == 3800.0


def _bars(t0, rows):
    return pd.DataFrame([{"time": t0 + timedelta(hours=k + 1), "open": o, "high": h, "low": l, "close": c} for k, (o, h, l, c) in enumerate(rows)])


def test_ombre_remplit_a_la_touche_et_annule_a_l_expiration(tmp_path):
    events, recs = [], []
    store = SimpleNamespace(record_trade=lambda r: recs.append(r), agent_event=lambda a, k, d: events.append((a, k, d)))
    sh = ShadowTrader(store, tmp_path / "sh.json", max_open=10)
    c = apply_pending_entry(_spec("LIMIT", 0.3), _cand(), SNAP)               # achat limite à 3797
    c2 = apply_pending_entry(_spec("LIMIT", 0.3), _cand(entry=3900.0, sl=3890.0), SNAP)   # jamais touché
    c2.id, c2.symbol = "c2", "XAGUSD"
    sh.open_from_candidates([c, c2], NOW)
    assert all(p.pending for p in sh.positions.values()) and len(sh.positions) == 2
    # barre 1 ne touche pas (bas 3798), barre 2 touche (bas 3796) ; la dernière barre est en formation (exclue)
    df = _bars(NOW, [(3801, 3803, 3798, 3802), (3800, 3801, 3796, 3799), (3799, 3800, 3798, 3799)])
    sh.update({"XAUUSD": SimpleNamespace(frames={"M5": df}), "XAGUSD": SimpleNamespace(frames={"M5": _bars(NOW, [(3900, 3905, 3899, 3903)] * 3)})}, NOW + timedelta(hours=3))
    p = next(p for p in sh.positions.values() if p.symbol == "XAUUSD")
    assert not p.pending and p.entry == 3797.0 and ("Q99", "pending_filled") in [(a, k) for a, k, _ in events]
    # l'ordre argent expire après 3 barres H1 (3 h) sans être touché
    sh.update({"XAGUSD": SimpleNamespace(frames={"M5": _bars(NOW, [(3900, 3905, 3899, 3903)] * 6)})}, NOW + timedelta(hours=3, minutes=1))
    assert all(p.symbol != "XAGUSD" for p in sh.positions.values()) and any(k == "pending_expired" for _, k, _ in events)


def test_backtest_ordre_limite_rempli_ou_expire():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [(100.0, 100.0, 100.0, 100.0)] * 210
    rows += [(100.0, 100.5, 99.8, 100.2),     # barre 210 : signal à la clôture (achat limite à 99.5, stop 99.0, cible 102)
             (100.2, 100.6, 99.9, 100.4),     # 211 : pas touché
             (100.4, 100.7, 99.4, 100.1),     # 212 : touché (bas 99.4) → entrée 99.5
             (100.1, 102.5, 100.0, 102.2),    # 213 : cible 102 atteinte
             (102.2, 102.3, 102.0, 102.1), (102.1, 102.2, 102.0, 102.1)]
    df = pd.DataFrame([{"time": t0 + timedelta(hours=k), "open": o, "high": h, "low": l, "close": c, "tick_volume": 1, "real_volume": 0, "spread": 0}
                       for k, (o, h, l, c) in enumerate(rows)])
    zero = BTCosts(spread_points=0, slippage_points=0, commission_per_lot=0.0)

    def sig(d):
        return Signal(side=Side.BUY, sl=99.0, tp=102.0, entry_kind="LIMIT", order_price=99.5, expiry_bars=3) if len(d) == 211 else None
    res = run_backtest(df, sig, zero, warmup=200, management=None)
    assert len(res.trades) == 1 and abs(res.trades[0].entry - 99.5) < 1e-9 and res.trades[0].exit_reason == "tp" and res.expired_orders == 0

    def sig_loin(d):
        return Signal(side=Side.BUY, sl=98.0, tp=102.0, entry_kind="LIMIT", order_price=98.5, expiry_bars=2) if len(d) == 211 else None
    res2 = run_backtest(df, sig_loin, zero, warmup=200, management=None)
    assert len(res2.trades) == 0 and res2.expired_orders == 1


def test_famille_q_en_shadow_et_refus_en_reel():
    q = [a for a in default_agents() if a.family == "Q"]
    assert len(q) == 10 and all(a.status == "SHADOW" and resolve_screener(a) is not None for a in q)
    assert {a.params["entry_kind"] for a in q} == {"LIMIT", "STOP"}
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={})
    o.broker = SimpleNamespace(symbol_info=lambda s: SimpleNamespace(asset_class="metals", trade_allowed=True))
    o.now_fn = lambda: NOW
    c = apply_pending_entry(_spec("LIMIT", 0.3), _cand(), SNAP)
    assert "SHADOW seulement" in o._prefiltre_cout(c)
