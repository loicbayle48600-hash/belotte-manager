"""Tests de sécurité déterministes (section 33) : SL, sizing, limites, prop, corrélation, gate, exécution."""
import pandas as pd
import pytest

from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore, SystemMode, SystemState
from tradinglab.core.types import OrderRequest, Regime, Side, TradeCandidate, TradeMode, Verdict
from tradinglab.execution.executor import Executor
from tradinglab.execution.gate import ExecutionGate, GateContext
from tradinglab.risk.correlation_guard import CorrelationGuard, CorrelationLimits
from tradinglab.risk.daily_guard import DailyGuard
from tradinglab.risk.prop_guard import PropGuard, PropProfile
from tradinglab.risk.risk_manager import RiskLimits, RiskManager, compute_volume
from tradinglab.risk.stop_loss import validate_stop_loss
from tests.conftest import FIXED_NOW


class FakeNews:
    def __init__(self, ok=True, state="OK", reason=""):
        self.ok, self.state, self.reason = ok, state, reason


def make_state(equity=100000.0):
    st = SystemState()
    st.roll_day_if_needed(equity, equity, FIXED_NOW.date())
    st.update_equity(equity, equity)
    st.set_mode(SystemMode.AUTO, "")
    st.account_trade_mode = "DEMO"
    return st


def make_candidate(broker, symbol="EURUSD", side=Side.BUY, score=80.0, rr=2.5, sl_atr=1.5):
    t = broker.tick(symbol)
    entry = t.ask if side is Side.BUY else t.bid
    atr = 0.0012
    sl = entry - side.sign * sl_atr * atr
    tp = entry + side.sign * rr * sl_atr * atr
    c = TradeCandidate(symbol=symbol, side=side, entry=entry, sl=sl, tp_plan=[tp], timeframes=["M15", "H1"], regime=Regime.TRENDING,
                       agent_id="B01", setup_score=score, rr=rr, atr=atr, bar_time="2026-01-20T09:45:00", session="LONDON")
    c.verdict = Verdict.APPROVE
    return c, atr


def gate_for(settings, broker):
    risk = RiskManager(RiskLimits.from_config(settings.risk))
    prop = PropGuard(PropProfile.from_config(settings.prop), True, settings.autonomous_prop, 1.0)
    daily = DailyGuard(settings.risk, settings.daily_profit)
    corr = CorrelationGuard(CorrelationLimits.from_config(settings.correlation))
    return ExecutionGate(broker, risk, prop, daily, corr), risk


def ctx_for(broker, c, st, atr, **kw):
    spec = broker.symbol_info(c.symbol)
    base = dict(candidate=c, state=st, account=broker.account_info(), spec=spec, tick=broker.tick(c.symbol), atr=atr, data_quality="OK",
                market_open=True, news_check=FakeNews(), now=FIXED_NOW, watchdog_alive=True)
    base.update(kw)
    return GateContext(**base)


# ---------------- SL ----------------
def test_sl_missing_zero_wrong_side_too_close_refused(broker):
    spec = broker.symbol_info("EURUSD")
    assert not validate_stop_loss(Side.BUY, 1.085, None, spec).ok
    assert not validate_stop_loss(Side.BUY, 1.085, 0, spec).ok
    assert not validate_stop_loss(Side.BUY, 1.085, 1.09, spec).ok
    assert not validate_stop_loss(Side.SELL, 1.085, 1.08, spec).ok
    assert not validate_stop_loss(Side.BUY, 1.085, 1.08495, spec).ok           # < stops_level
    assert not validate_stop_loss(Side.BUY, 1.085, 1.0848, spec, atr=0.001).ok  # < 0.25 ATR
    assert validate_stop_loss(Side.BUY, 1.085, 1.082, spec, atr=0.001).ok


# ---------------- sizing ----------------
def test_volume_calculation_and_rounding_down(broker):
    spec = broker.symbol_info("EURUSD")
    r = compute_volume(100000, 0.25, 1.0850, 1.0820, spec, 0.35)
    assert r.ok and r.volume == 0.83 and r.risk_money <= 250.0 + 1e-6


def test_min_volume_exceeding_max_risk_refused(broker):
    spec = broker.symbol_info("EURUSD")
    r = compute_volume(500, 0.25, 1.0850, 1.0750, spec, 0.35)   # 0.01 lot = 10 EUR = 2 % > 0.35 %
    assert not r.ok and "REFUS" in r.reason


def test_risk_max_never_exceeded(broker):
    spec = broker.symbol_info("EURUSD")
    rm = RiskManager(RiskLimits(risk_per_trade_percent=0.25, max_risk_per_trade_percent=0.35))
    r = rm.size(100000, 1.085, 1.082, spec, risk_percent=5.0)   # demande abusive → plafonnée
    assert r.ok and r.risk_percent_effective <= 0.35


# ---------------- limites ----------------
def test_daily_loss_locks_new_trades(settings):
    st = make_state()
    st.update_equity(98900, 98900)   # -1.1 %
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    assert not d.entries_allowed and "DAILY_LOSS_LIMIT" in st.lock_reasons


def test_consecutive_losses_lock(settings):
    st = make_state()
    st.consecutive_losses = 3
    assert not DailyGuard(settings.risk, settings.daily_profit).evaluate(st).entries_allowed


def test_profit_scaling_and_giveback(settings):
    st = make_state()
    st.update_equity(100800, 100800)
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    assert d.entries_allowed and d.risk_percent == 0.15 and d.setup_score_bonus == 5
    st.update_equity(100500, 100500)   # rendu > 25 % du pic (+800 → floor +600)
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    assert not d.entries_allowed and "GIVEBACK_FLOOR" in st.lock_reasons


def test_total_open_risk_and_max_positions(settings):
    rm = RiskManager(RiskLimits.from_config(settings.risk))
    st = make_state()
    for i in range(3):
        st.bot_positions[str(i)] = BotPositionPlan(i, f"S{i}", "BUY", "a", "c", 1.0, 0.99, 0.1, 300.0, 0.3)
    checks = {c.name: c.ok for c in rm.check_limits(st, "EURUSD", Side.BUY, 250.0, 0, 3)}
    assert not checks["max_open_positions"] and not checks["max_total_open_risk"]


def test_duplicate_symbol_position_refused(settings):
    rm = RiskManager(RiskLimits.from_config(settings.risk))
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 250.0, 0.25)
    checks = {c.name: c.ok for c in rm.check_limits(st, "EURUSD", Side.BUY, 250.0, 1, 1)}
    assert not checks["max_positions_per_symbol"] and not checks["no_averaging_or_grid"]


# ---------------- prop ----------------
def test_non_demo_account_blocks_execution(settings):
    pg = PropGuard(PropProfile.from_config(settings.prop), True, settings.autonomous_prop, 1.0)
    assert pg.authorization(TradeMode.DEMO).ok
    assert not pg.authorization(TradeMode.REAL).ok
    assert not pg.authorization(TradeMode.UNKNOWN).ok


def test_prop_unknown_rules_block_even_with_flags(settings):
    base = dict(settings.prop, prop_rules_verified=True, user_explicitly_authorized_prop_automation=True)
    # une règle critique effacée du profil → ambiguïté → exécution prop bloquée malgré tous les drapeaux
    pg = PropGuard(PropProfile.from_config(dict(base, ea_allowed="UNKNOWN", daily_loss_basis=None)), True, True, 1.0)
    assert not pg.prop_automation_allowed and pg.profile.ambiguous
    # règles relevées mais approbation de l'EA par la prop firm non obtenue → toujours bloqué
    pg2 = PropGuard(PropProfile.from_config(base), True, True, 1.0)
    assert not pg2.profile.ambiguous and not pg2.prop_automation_allowed
    assert any("EA_APPROVAL_OBTAINED" in r for r in pg2.blocking_reasons)
    # approbation obtenue + drapeaux + autorisation explicite → seul cas autorisé
    pg3 = PropGuard(PropProfile.from_config(dict(base, ea_approval_obtained=True)), True, True, 1.0)
    assert pg3.prop_automation_allowed and pg3.authorization(TradeMode.REAL).ok


def test_settings_autonomous_prop_requires_all_flags(settings):
    assert settings.autonomous_prop is False


# ---------------- corrélation ----------------
def test_currency_factor_guard(settings):
    cg = CorrelationGuard(CorrelationLimits.from_config(settings.correlation))
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 300.0, 0.3)
    st.bot_positions["2"] = BotPositionPlan(2, "GBPUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 300.0, 0.3)
    checks = {c.name: c.ok for c in cg.check(st, "AUDUSD", Side.BUY, 300.0)}
    assert not checks["currency_factor_risk"]        # USD net 0.9 % > 0.6 %
    checks2 = {c.name: c.ok for c in cg.check(st, "USDCHF", Side.BUY, 100.0)}
    assert checks2["currency_factor_risk"]          # réduit l'exposition USD


def test_correlation_matrix_cluster(settings):
    cg = CorrelationGuard(CorrelationLimits(max_correlated_cluster_risk_percent=0.4, correlation_threshold=0.7))
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 250.0, 0.25)
    corr = pd.DataFrame([[1.0, 0.9], [0.9, 1.0]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"])
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.BUY, 250.0, corr)}
    assert not checks["correlated_cluster_risk"]
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.SELL, 250.0, corr)}
    assert checks["correlated_cluster_risk"]


# ---------------- gate ----------------
def test_gate_approves_clean_candidate(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert res.approved, res.reason
    assert req is not None and req.sl > 0 and req.volume >= 0.01


@pytest.mark.parametrize("field,value,check", [
    ("data_quality", "STALE", "04_data_fresh"),
    ("market_open", False, "05_symbol_tradable"),
    ("watchdog_alive", False, "01_health"),
])
def test_gate_refuses_on_context(settings, broker, field, value, check):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr, **{field: value}))
    assert not res.approved and req is None and check in res.reason


def test_gate_news_block(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, news_check=FakeNews(False, "BLOCKED_PRE_NEWS", "NFP")))
    assert not res.approved and "08_news" in res.reason
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, news_check=None))
    assert not res.approved


def test_gate_refuses_without_sl_and_wrong_side(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    c.sl = 0.0
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert not res.approved and "10_stop_loss" in res.reason
    c2, atr = make_candidate(broker)
    c2.sl = c2.entry + 0.002
    res, _ = gate.evaluate(ctx_for(broker, c2, make_state(), atr))
    assert not res.approved and "10_stop_loss" in res.reason


def test_gate_refuses_disconnected_broker(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    ctx = ctx_for(broker, c, make_state(), atr)
    broker.set_connected(False)
    res, _ = gate.evaluate(ctx)
    assert not res.approved and "01_health" in res.reason


def test_gate_duplicate_idea_and_mode(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    st = make_state()
    st.mark_executed(c.idempotency_key)
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    assert not res.approved and "16_duplicate_idea" in res.reason
    st2 = make_state()
    st2.set_mode(SystemMode.SAFE_MODE, "test")
    res, _ = gate.evaluate(ctx_for(broker, c, st2, atr))
    assert not res.approved and "20_final_approval" in res.reason


def test_gate_verdict_and_score_required(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, score=50)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert not res.approved
    c2, atr = make_candidate(broker)
    c2.verdict = Verdict.WAIT
    res, _ = gate.evaluate(ctx_for(broker, c2, make_state(), atr))
    assert not res.approved


def test_gate_spread_too_wide(settings, broker):
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    broker.specs["EURUSD"].spread_points = 80
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert not res.approved and "07_spread" in res.reason


# ---------------- exécution ----------------
def executor_for(settings, broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state = make_state()
    store.save()
    return Executor(broker, store, Journal(tmp_path / "logs", component="test")), store


def test_execute_then_position_has_sl(settings, broker, tmp_path):
    gate, risk = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert out.executed and out.sl_verified and out.position.has_sl
    assert str(out.position.ticket) in store.state.bot_positions
    assert store.state.has_executed(c.idempotency_key)


def test_order_rejection_handled(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    broker.fail_next_order = 10018
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert not out.executed and out.order.retcode == 10018 and not broker.positions()


def test_missing_sl_after_fill_is_fixed(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    broker.drop_sl_on_fill = True
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert out.executed and out.sl_verified and out.position.has_sl


def test_missing_sl_unfixable_closes_and_safe_mode(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    broker.drop_sl_on_fill = True
    broker.reject_modify = True
    out = ex.execute(c, res, req, 250.0, 0.25)
    assert not out.sl_verified and not broker.positions() and store.state.mode == SystemMode.SAFE_MODE.value


def test_no_reentry_after_restart(settings, broker, tmp_path):
    gate, _ = gate_for(settings, broker)
    ex, store = executor_for(settings, broker, tmp_path)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, store.state, atr))
    assert ex.execute(c, res, req, 250.0, 0.25).executed
    # "restart" : nouvel objet state rechargé depuis le disque
    store2 = StateStore(tmp_path / "state")
    assert store2.state.has_executed(c.idempotency_key)
    res2, req2 = gate.evaluate(ctx_for(broker, c, store2.state, atr, open_positions_symbol=1, open_positions_total=1))
    assert not res2.approved and "16_duplicate_idea" in res2.reason


def test_close_always_allowed_even_when_locked(settings, broker, tmp_path):
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.1, t.ask - 0.003, magic=51000))
    from tradinglab.execution.position_manager import PMConfig, PositionManager
    store = StateStore(tmp_path / "state")
    store.state = make_state()
    store.state.lock_entries("DAILY_LOSS_LIMIT")
    store.state.set_mode(SystemMode.SAFE_MODE, "x")
    pm = PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(), 51000)
    assert pm.close(r.ticket, "manual") and not broker.positions()
