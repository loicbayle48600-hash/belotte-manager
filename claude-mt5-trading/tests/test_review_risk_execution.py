"""Tests de la revue « risk-execution » : chaque test échouait avant la correction correspondante."""
import math

import pandas as pd
import pytest

from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore, SystemMode, SystemState
from tradinglab.core.types import OrderRequest, Side, SymbolSpec
from tradinglab.execution.executor import Executor
from tradinglab.execution.gate import ExecutionGate, GateContext
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager
from tradinglab.risk.correlation_guard import CorrelationGuard, CorrelationLimits
from tradinglab.risk.daily_guard import DailyGuard
from tradinglab.risk.prop_guard import PropGuard, PropProfile
from tradinglab.risk.risk_manager import RiskLimits, RiskManager, compute_volume, loss_per_lot
from tradinglab.risk.stop_loss import validate_stop_loss
from tests.conftest import FIXED_NOW
from tests.test_risk_and_gate import FakeNews, ctx_for, executor_for, gate_for, make_candidate, make_state


def pm_for(broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.roll_day_if_needed(100000, 100000, FIXED_NOW.date())
    store.state.update_equity(100000, 100000)
    pm = PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(), 51000)
    return pm, store


def open_buy(broker, store, sl_dist=0.0030, vol=0.30):
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, vol, t.ask - sl_dist, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "B01", "c1", pos.price_open, pos.sl, pos.volume, 300.0, 0.3,
                           opened_at=FIXED_NOW.isoformat(), last_sl=pos.sl, regime="TRENDING")
    store.state.bot_positions[str(pos.ticket)] = plan
    store.save()
    return pos, plan


# ---------------- stop_loss : NaN / inf refusés ----------------
def test_validate_stop_loss_refuses_nan_and_inf(broker):
    spec = broker.symbol_info("EURUSD")
    assert not validate_stop_loss(Side.BUY, 1.085, float("nan"), spec, atr=0.001).ok
    assert not validate_stop_loss(Side.BUY, float("nan"), 1.082, spec, atr=0.001).ok
    assert not validate_stop_loss(Side.SELL, 1.085, float("inf"), spec).ok
    assert not validate_stop_loss(Side.BUY, 1.085, 1.082, spec, atr=0.001, reference_price=float("nan")).ok
    assert validate_stop_loss(Side.BUY, 1.085, 1.082, spec, atr=0.001).ok


# ---------------- risk_manager : sizing sans exception ----------------
def test_compute_volume_refuses_nan_and_zero_step_without_exception(broker):
    spec = broker.symbol_info("EURUSD")
    r = compute_volume(100000, 0.25, 1.085, float("nan"), spec, 0.35)
    assert not r.ok and r.volume == 0.0 and "invalides" in r.reason
    assert not compute_volume(100000, 0.25, float("inf"), 1.082, spec, 0.35).ok
    assert not compute_volume(float("nan"), 0.25, 1.085, 1.082, spec, 0.35).ok
    bad = SymbolSpec(**{**spec.__dict__, "volume_step": 0.0})
    assert not compute_volume(100000, 0.25, 1.085, 1.082, bad, 0.35).ok
    bad2 = SymbolSpec(**{**spec.__dict__, "volume_min": 0.0})
    assert not compute_volume(100000, 0.25, 1.085, 1.082, bad2, 0.35).ok


# ---------------- daily_guard : giveback seulement après un pic significatif ----------------
def test_giveback_ignores_tiny_peak_but_keeps_real_peak(settings):
    st = make_state()
    st.update_equity(100004, 100004)      # pic +4 EUR = 0.004 % : bruit de flottant
    st.update_equity(99999, 99999)
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    assert d.entries_allowed and "GIVEBACK_FLOOR" not in st.lock_reasons
    # un vrai pic (0.8 %) reste protégé
    st2 = make_state()
    st2.update_equity(100800, 100800)
    st2.update_equity(100500, 100500)
    d2 = DailyGuard(settings.risk, settings.daily_profit).evaluate(st2)
    assert not d2.entries_allowed and "GIVEBACK_FLOOR" in st2.lock_reasons


# ---------------- prop_guard : null / vide = règle non renseignée ----------------
def test_prop_null_or_empty_critical_rule_is_unknown(settings):
    cfg = dict(settings.prop)
    for r in ("ea_allowed", "news_trading_window_minutes", "trading_day_definition", "daily_loss_basis"):
        cfg[r] = "renseignée"
    assert not PropProfile.from_config(cfg).ambiguous
    for bad in (None, "", "  ", "null", "None"):
        p = PropProfile.from_config({**cfg, "ea_allowed": bad})
        assert p.ambiguous and p.unknown_rules == ["ea_allowed"], bad
    p = PropProfile.from_config({**cfg, "ea_allowed": None, "prop_rules_verified": True,
                                 "user_explicitly_authorized_prop_automation": True})
    assert not PropGuard(p, True, True, 1.0).prop_automation_allowed


# ---------------- correlation_guard : NaN / symbole absent → repli devise ----------------
def test_correlation_nan_or_missing_symbol_falls_back_to_currency_rule(settings):
    cg = CorrelationGuard(CorrelationLimits(max_correlated_cluster_risk_percent=0.4, correlation_threshold=0.7))
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 250.0, 0.25)
    # corrélation NaN (historique insuffisant) : EURUSD BUY et GBPUSD BUY partagent USD dans le même sens → cluster
    corr = pd.DataFrame([[1.0, float("nan")], [float("nan"), 1.0]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"])
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.BUY, 250.0, corr)}
    assert not checks["correlated_cluster_risk"]
    # symbole existant absent de la matrice : même repli
    corr2 = pd.DataFrame([[1.0, 0.1], [0.1, 1.0]], index=["GBPUSD", "AUDUSD"], columns=["GBPUSD", "AUDUSD"])
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.BUY, 250.0, corr2)}
    assert not checks["correlated_cluster_risk"]
    # corrélation connue et faible : pas de cluster (règle signée conservée)
    corr3 = pd.DataFrame([[1.0, 0.1], [0.1, 1.0]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"])
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.BUY, 250.0, corr3)}
    assert checks["correlated_cluster_risk"]


def test_open_lines_use_spec_currencies(settings, broker):
    """Une position GER40 existante est comptée en EUR (spec broker), pas en USD deviné."""
    def lookup(sym):
        spec = broker.symbol_info(sym)
        if spec is None:
            return None
        return SymbolSpec(**{**spec.__dict__, "currency_base": "GER40", "currency_profit": "EUR"})
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "GER40", "BUY", "a", "c", 18000.0, 17900.0, 0.1, 300.0, 0.3)
    cg_naive = CorrelationGuard(CorrelationLimits.from_config(settings.correlation))
    assert cg_naive.exposure_summary(st)["currency_factor"].get("USD", 0.0) != 0.0
    cg = CorrelationGuard(CorrelationLimits.from_config(settings.correlation), spec_lookup=lookup)
    factors = cg.exposure_summary(st)["currency_factor"]
    assert factors.get("USD", 0.0) == 0.0 and factors["EUR"] == pytest.approx(-300.0)
    # le gate injecte symbol_info du broker dans le guard s'il n'a pas de lookup
    gate, _ = gate_for(settings, broker)
    assert gate.corr.spec_lookup == broker.symbol_info


# ---------------- gate : détail 01_health et identité du compte ----------------
def test_gate_health_detail_names_the_right_cause(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, watchdog_alive=False))
    health = next(ch for ch in res.checks if ch.name == "01_health")
    assert not health.ok and "watchdog absent" in health.detail and "déconnecté" not in health.detail
    broker.set_connected(False)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, watchdog_alive=True))
    health = next(ch for ch in res.checks if ch.name == "01_health")
    assert not health.ok and "broker déconnecté" in health.detail and "watchdog" not in health.detail


def test_gate_refuses_unexpected_account_identity(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    ok, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, expected_login=broker.login, expected_server=broker.server))
    assert ok.approved, ok.reason
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr, expected_login=123456))
    assert not res.approved and req is None and "02_account" in res.reason and "compte inattendu" in res.reason
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr, expected_server="Autre-Serveur"))
    assert not res.approved and req is None and "02_account" in res.reason


# ---------------- executor : risque réel après fill, position trouvée après latence ----------------
def test_executor_records_actual_risk_from_fill(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    out = ex.execute(c, res, req, 999.0, 0.999)     # risque planifié volontairement faux
    assert out.executed
    plan = store.state.bot_positions[str(out.position.ticket)]
    pos = out.position
    expected = loss_per_lot(pos.price_open, pos.sl, broker.symbol_info("EURUSD")) * pos.volume
    assert plan.initial_risk_money == pytest.approx(expected) and plan.initial_risk_money < 999.0
    assert plan.risk_percent == pytest.approx(100.0 * expected / store.state.equity)


def test_executor_retries_when_position_not_visible_immediately(settings, broker, tmp_path, monkeypatch):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    real_positions = broker.positions
    calls = {"n": 0}

    def delayed_positions(magic=None):
        calls["n"] += 1
        return [] if calls["n"] <= 2 else real_positions(magic=magic)   # latence terminal : 2 lectures vides

    monkeypatch.setattr(broker, "positions", delayed_positions)
    monkeypatch.setattr("tradinglab.execution.executor.FIND_POSITION_DELAY_SEC", 0.0)
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert out.executed and out.position is not None and out.sl_verified
    assert store.state.mode != SystemMode.SAFE_MODE.value
    assert str(out.position.ticket) in store.state.bot_positions


# ---------------- position_manager : sync robuste ----------------
def test_sync_ignores_transient_empty_list_when_disconnected(broker, tmp_path, monkeypatch):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    broker.set_connected(False)
    monkeypatch.setattr(broker, "positions", lambda magic=None: [])
    assert pm.sync() == [] and str(pos.ticket) in store.state.bot_positions
    broker.set_connected(True)
    monkeypatch.undo()
    assert pm.sync() == [] and str(pos.ticket) in store.state.bot_positions


def test_sync_ignores_positions_api_error(broker, tmp_path, monkeypatch):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)

    def boom(magic=None):
        raise RuntimeError("positions_get: (-10004, 'IPC timeout')")

    monkeypatch.setattr(broker, "positions", boom)
    assert pm.sync() == [] and str(pos.ticket) in store.state.bot_positions
    monkeypatch.undo()
    broker.close_position(pos.ticket)
    assert [p.ticket for p in pm.sync()] == [pos.ticket] and str(pos.ticket) not in store.state.bot_positions


def test_sync_adopts_with_estimated_risk_counted_by_limits(settings, broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 1.0, t.ask - 0.0100, magic=51000))   # ~1000 EUR de risque
    pm.sync()
    plan = store.state.bot_positions[str(r.ticket)]
    pos = broker.position(r.ticket)
    expected = loss_per_lot(pos.price_open, pos.sl, broker.symbol_info("EURUSD")) * pos.volume
    assert plan.agent_id == "ADOPTED" and plan.initial_risk_money == pytest.approx(expected) and expected > 0
    assert plan.risk_percent == pytest.approx(100.0 * expected / 100000)
    rm = RiskManager(RiskLimits.from_config(settings.risk))
    checks = {c.name: c.ok for c in rm.check_limits(store.state, "GBPUSD", Side.BUY, 250.0, 0, 1)}
    assert not checks["max_total_open_risk"]      # 1 % + 0.25 % > 1 %


# ---------------- position_manager : break-even retenté, close() tracé, BE défensif ----------------
def test_break_even_retried_after_broker_refusal(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open + 1.3 * dist)
    broker.reject_modify = True
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=True))
    assert any("modify refusé" in a for a in acts) and not plan.break_even_done and plan.last_sl == plan.initial_sl
    broker.reject_modify = False
    pos = broker.position(pos.ticket)
    acts = pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=True))
    assert plan.break_even_done and any("SL →" in a for a in acts) and broker.position(pos.ticket).sl > plan.initial_sl


def test_close_command_keeps_plan_until_sync_detects_closure(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    assert pm.close(pos.ticket, "manual") and not broker.positions(magic=51000)
    kept = store.state.bot_positions.get(str(pos.ticket))
    assert kept is not None and "close_command:manual" in kept.notes
    closed = pm.sync()
    assert [p.ticket for p in closed] == [pos.ticket] and str(pos.ticket) not in store.state.bot_positions


def test_close_all_bot_positions_are_reported_closed_by_sync(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    assert pm.close_all_bot("panic") == [pos.ticket]
    assert "close_command:panic" in store.state.bot_positions[str(pos.ticket)].notes
    assert [p.ticket for p in pm.sync()] == [pos.ticket]


def test_move_to_break_even_without_spec_or_beyond_price_is_refused(broker, tmp_path):
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    assert pm.move_to_break_even(pos.ticket, None) is False          # plantait (AttributeError) auparavant
    assert broker.position(pos.ticket).sl == plan.initial_sl
    # prix sous l'entrée : le BE serait au-dessus du prix courant → refus propre, SL inchangé
    dist = pos.price_open - pos.sl
    broker.set_price("EURUSD", pos.price_open - 0.5 * dist)
    assert pm.move_to_break_even(pos.ticket, broker.symbol_info("EURUSD")) is False
    assert broker.position(pos.ticket).sl == plan.initial_sl and not plan.break_even_done
    # prix favorable : BE accepté
    broker.set_price("EURUSD", pos.price_open + 1.5 * dist)
    assert pm.move_to_break_even(pos.ticket, broker.symbol_info("EURUSD")) is True
    assert plan.break_even_done and broker.position(pos.ticket).sl > plan.initial_sl
