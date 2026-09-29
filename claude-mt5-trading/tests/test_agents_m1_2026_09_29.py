"""Agents M1 en micro-positions (2026-09-29, demande utilisateur) : famille S, M1 chargé pour quelques symboles,
risque au quart, aucune fermeture volontaire avant 60 s (FOXX), spread d'entrée déduit en ombre."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import yaml

from tradinglab.agents.registry import default_agents
from tradinglab.agents.screeners import resolve_screener
from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore
from tradinglab.core.types import OrderRequest, Side
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.shadow.shadow import ShadowTrader


def test_famille_s_toutes_les_strategies():
    s = [a for a in default_agents() if a.family == "S"]
    assert len(s) == 38 and len({a.base_strategy or a.strategy for a in s}) == 19
    assert all(a.status == "SHADOW" and a.timeframes["entry"] == "M1" and resolve_screener(a) is not None for a in s)
    syms = set(yaml.safe_load(open("config/system.yaml", encoding="utf-8"))["system"]["extra_timeframes"]["M1"])
    assert {m for a in s for m in a.markets} <= syms


def test_m1_charge_seulement_pour_les_symboles_listes(broker):
    feed = MarketDataFeed(broker, ["M5", "H1"], bars=300, min_bars=10, extra_timeframes={"M1": ["EURUSD"]})
    assert "M1" in feed.snapshot("EURUSD").frames and "M1" not in feed.snapshot("GBPUSD").frames


def test_gate_micro_position(settings, broker):
    from test_prop_foxx_lots_coherence_2026_09_23 import _gate
    from test_risk_and_gate import ctx_for, make_candidate, make_state
    gate, _ = _gate(settings, broker)
    c, atr = make_candidate(broker, rr=2.5)
    plein, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    micro, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, agent_risk_factor=0.25))
    assert micro.approved and micro.risk_money < 0.3 * plein.risk_money


def test_pas_de_tp_partiel_avant_une_minute(broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.roll_day_if_needed(100000, 100000, datetime.now(timezone.utc).date()); store.state.update_equity(100000, 100000)
    pm = PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(), 51000)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 1.0, t.ask - 0.0020, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "S01", "c1", pos.price_open, pos.sl, pos.volume, 200.0, 0.05,
                           opened_at=datetime.now(timezone.utc).isoformat(), last_sl=pos.sl)
    store.state.bot_positions[str(pos.ticket)] = plan
    broker.set_price("EURUSD", pos.price_open + 1.7 * (pos.price_open - pos.sl))
    spec = broker.symbol_info("EURUSD")
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert not plan.tp1_done and broker.position(pos.ticket).volume == 1.0          # trop jeune : pas de partiel
    plan.opened_at = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert plan.tp1_done and broker.position(pos.ticket).volume < 1.0


def test_ombre_m1_deduit_le_spread(tmp_path):
    recs = []
    store = SimpleNamespace(record_trade=lambda r: recs.append(r), agent_event=lambda *a, **k: None)
    sh = ShadowTrader(store, tmp_path / "sh.json", max_open=10)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    c = SimpleNamespace(id="c1", agent_id="S01", symbol="BTCUSD", side=Side.BUY, entry=100.0, sl=99.0, tp_plan=[101.5],
                        sl_distance=1.0, regime=SimpleNamespace(value="TRENDING"), session="LONDON", setup_score=70.0,
                        bar_time="x", idempotency_key="k1", timeframes=["M1", "M15"], spread_points=20, spread_price=0.2)
    sh.open_from_candidates([c], now)
    assert list(sh.positions.values())[0].cost_r == 0.2
    import pandas as pd
    df = pd.DataFrame({"time": [now + timedelta(minutes=k) for k in (1, 2, 3)], "open": 100.0, "high": [101.6, 100, 100],
                       "low": [99.9, 100, 100], "close": 100.0})
    closed = sh.update({"BTCUSD": SimpleNamespace(frames={"M5": df})}, now + timedelta(minutes=4))
    assert abs(closed[0].result_r - 1.3) < 1e-9                                    # 1,5 R − 0,2 R de spread
