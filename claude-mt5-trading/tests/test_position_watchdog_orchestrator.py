"""Position manager, watchdog indépendant, orchestrateur de bout en bout, commandes, restart."""
import json
from datetime import timedelta

from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore, SystemMode
from tradinglab.core.types import OrderRequest, Side
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager
from tradinglab.monitoring.watchdog import Watchdog, read_watchdog_report
from tradinglab.mt5.mock_adapter import MockBroker
from tradinglab.orchestration.orchestrator import Orchestrator
from tests.conftest import FIXED_NOW


def open_buy(broker, store, sl_dist=0.0030, vol=0.30):
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, vol, t.ask - sl_dist, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "B01", "c1", pos.price_open, pos.sl, pos.volume, 300.0, 0.3,
                           opened_at=FIXED_NOW.isoformat(), last_sl=pos.sl, regime="TRENDING")
    store.state.bot_positions[str(pos.ticket)] = plan
    store.save()
    return pos, plan


def pm_for(broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.roll_day_if_needed(100000, 100000, FIXED_NOW.date())
    store.state.update_equity(100000, 100000)
    pm = PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(), 51000)
    return pm, store


def test_tp_partials_break_even_and_runner(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    # +1.3R : break-even (structure confirmée), pas encore TP1
    broker.set_price("EURUSD", pos.price_open + 1.3 * dist)
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=True))
    assert plan.break_even_done and any("break-even" in a for a in acts)
    pos = broker.position(pos.ticket)
    assert pos.sl > plan.initial_sl
    # +1.6R : TP1 30 %
    broker.set_price("EURUSD", pos.price_open + 1.6 * dist)
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=True))
    assert plan.tp1_done and abs(broker.position(pos.ticket).volume - 0.21) < 1e-9
    # +2.6R : TP2 40 % + trailing (runner 30 %)
    broker.set_price("EURUSD", pos.price_open + 2.6 * dist)
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, last_swing_low=pos.price_open + 1.5 * dist))
    assert plan.tp2_done and plan.trailing_active and abs(broker.position(pos.ticket).volume - 0.09) < 1e-9
    assert broker.position(pos.ticket).sl >= pos.price_open + 1.0 * dist


def test_never_widen_stop_and_break_even_requires_structure(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 1.3 * dist)
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert not plan.break_even_done                       # structure non confirmée → pas de BE
    # tentative d'élargissement refusée
    from tradinglab.risk.stop_loss import is_tighter_or_equal
    assert not is_tighter_or_equal(Side.BUY, pos.sl - 0.001, pos.sl)


def test_position_manager_restores_missing_sl(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    broker._positions[pos.ticket].sl = 0.0
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, broker.symbol_info("EURUSD"), MarketContext(atr=0.0012))
    assert any("SL manquant" in a for a in acts) and broker.position(pos.ticket).has_sl


def test_early_exit_on_invalidation(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    acts = pm.manage(plan, pos, broker.symbol_info("EURUSD"), MarketContext(atr=0.0012, invalidated=True))
    assert any("invalidation" in a for a in acts) and not broker.positions()


def test_sync_adopts_positions_after_restart_without_reopening(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.1, t.ask - 0.003, magic=51000))
    broker.order_send(OrderRequest("GBPUSD", Side.BUY, 0.1, broker.tick("GBPUSD").ask - 0.003, magic=999))   # position manuelle
    closed = pm.sync()
    assert closed == [] and str(r.ticket) in store.state.bot_positions and len(store.state.bot_positions) == 1
    assert store.state.bot_positions[str(r.ticket)].agent_id == "ADOPTED"


# ---------------- watchdog ----------------
def test_watchdog_fixes_missing_sl_and_requests_safe_mode(settings, broker, tmp_path):
    store = StateStore(settings.state_dir)
    store.state.roll_day_if_needed(100000, 100000, FIXED_NOW.date())
    pos, plan = open_buy(broker, store)
    broker._positions[pos.ticket].sl = 0.0
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"), reference_symbol="EURUSD")
    rep = wd.check_once()
    assert rep.positions_without_sl == 1 and rep.safe_mode_request and broker.position(pos.ticket).has_sl
    assert read_watchdog_report(settings.state_dir)["safe_mode_request"] is True


def test_watchdog_closes_when_sl_cannot_be_restored(settings, broker, tmp_path):
    store = StateStore(settings.state_dir)
    pos, plan = open_buy(broker, store)
    broker._positions[pos.ticket].sl = 0.0
    broker.reject_modify = True
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"))
    wd.check_once()
    assert not broker.positions()


def test_watchdog_detects_disconnect_and_dead_orchestrator(settings, broker):
    store = StateStore(settings.state_dir)
    store.state.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 250.0, 0.25)
    store.state.orchestrator_heartbeat = (FIXED_NOW - timedelta(days=400)).isoformat()
    store.save()
    broker.set_connected(False)
    broker.connect = lambda: False
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"))
    rep = wd.check_once()
    assert not rep.mt5_connected and not rep.orchestrator_alive and rep.safe_mode_request


# ---------------- orchestrateur ----------------
def make_orch(settings, broker, mode="AUTO"):
    o = Orchestrator(settings, broker, mode, now_fn=broker.now)
    o.scheduler.intervals["research"] = 10**9
    assert o.startup()
    return o


def test_orchestrator_startup_safe_mode_then_auto_and_candidates(settings, broker):
    o = make_orch(settings, broker)
    assert o.state.mode == SystemMode.SAFE_MODE.value and o.state.account_trade_mode == "DEMO"
    s1 = o.cycle()
    assert "routing" in s1 and s1["routing"]["agents_registered"] >= 100
    assert s1["candidates"] >= 1 and o.state.top_setups
    # après 2 cycles sains → AUTO
    o.cycle()
    assert o.state.mode == SystemMode.AUTO.value


def test_orchestrator_executes_through_gate_and_records_journal(settings, broker):
    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    broker.set_now(broker.now() + timedelta(minutes=5))
    s = o.cycle()
    events = o.journal.read_day(kinds={"gate", "execution", "position_opened"})
    gates = [e for e in events if e["kind"] == "gate"]
    assert gates, "au moins un passage par le gate attendu"
    if s.get("entries", 0):
        assert any(e["kind"] == "position_opened" for e in events)
        for p in broker.positions(magic=51000):
            assert p.has_sl
        assert o.state.open_risk_percent() <= float(settings.risk["max_total_open_risk_percent"]) + 1e-9


def test_orchestrator_panic_and_pause_commands(settings, broker):
    o = make_orch(settings, broker)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.1, t.ask - 0.003, magic=51000))
    o.pm.sync()
    o.store.push_command("PAUSE", {}, "test")
    o.cycle()
    assert o.state.mode == SystemMode.PAUSED.value
    o.handle_command("PANIC", {})
    assert o.state.mode == SystemMode.PANIC.value and not broker.positions(magic=51000) and "PANIC" in o.state.lock_reasons


def test_orchestrator_restart_is_idempotent(settings, broker):
    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    for _ in range(3):
        broker.set_now(broker.now() + timedelta(minutes=5))
        o.cycle()
    keys_before = dict(o.state.executed_keys)
    positions_before = {p.ticket for p in broker.positions(magic=51000)}
    # redémarrage : nouvel orchestrateur sur le même état et le même broker
    o2 = make_orch(settings, broker)
    assert o2.state.restarts == 2 and o2.state.mode == SystemMode.SAFE_MODE.value
    assert set(o2.state.bot_positions) == {str(t) for t in positions_before}
    o2.cycle()
    assert {p.ticket for p in broker.positions(magic=51000)} == positions_before   # aucune ré-ouverture
    for k in keys_before:
        assert o2.state.has_executed(k)


def test_orchestrator_non_demo_account_locks_entries(settings):
    from tradinglab.core.types import TradeMode
    b = MockBroker(seed=7, trade_mode=TradeMode.REAL)
    b.connect(); b.set_now(FIXED_NOW)
    o = Orchestrator(settings, b, "AUTO", now_fn=b.now)
    o.scheduler.intervals["research"] = 10**9
    o.startup()
    assert "ACCOUNT_MISMATCH" in o.state.lock_reasons
    o.cycle(); o.cycle(); o.cycle()
    assert o.state.mode != SystemMode.AUTO.value and not b.positions(magic=51000)


def test_watchdog_request_forces_safe_mode(settings, broker):
    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    (settings.state_dir / "watchdog.json").write_text(json.dumps({"heartbeat": broker.now().isoformat(), "safe_mode_request": True, "reasons": ["test"]}), encoding="utf-8")
    o.cycle()
    assert o.state.mode == SystemMode.SAFE_MODE.value
