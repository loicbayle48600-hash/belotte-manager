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
    lim = float(settings.risk["max_daily_loss_internal_percent"])
    st.update_equity(100000 * (1 - (lim + 0.1) / 100), 100000 * (1 - (lim + 0.1) / 100))   # limite + 0,1 %
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    assert not d.entries_allowed and "DAILY_LOSS_LIMIT" in st.lock_reasons


def test_consecutive_losses_lock(settings):
    st = make_state()
    st.consecutive_losses = int(settings.risk["max_consecutive_losses"])   # la valeur vient de config/risk.yaml (3, puis 5 en test le 28/09)
    assert not DailyGuard(settings.risk, settings.daily_profit).evaluate(st).entries_allowed


def test_profit_scaling_and_giveback(settings):
    st = make_state()
    st.update_equity(100800, 100800)
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    base = float(settings.risk["risk_per_trade_percent"])
    assert d.entries_allowed and d.risk_percent == min(settings.daily_profit["level_1_new_risk_percent"], base)
    assert d.setup_score_bonus == 5
    assert d.risk_percent <= base, "un palier après profit ne peut jamais AUGMENTER le risque (2026-09-25)"
    st.update_equity(100500, 100500)   # rendu > 25 % du pic (+800 → floor +600)
    d = DailyGuard(settings.risk, settings.daily_profit).evaluate(st)
    # config du dépôt (2026-09-22, décision utilisateur « continue normalement ») : giveback "off" — signalé,
    # aucune restriction ; seul le barème de profit du jour module le risque
    assert settings.daily_profit["giveback_action"] == "off"
    assert d.entries_allowed and "GIVEBACK_FLOOR" not in st.lock_reasons
    assert d.risk_percent == settings.risk["risk_per_trade_percent"] and st.daily.giveback_breached
    assert any("aucune restriction" in r for r in d.reasons)
    # mode "reduce_risk" : on continue mais à risque minimal, et le marqueur est COLLANT (un rebond
    # au-dessus du plancher ne réarme pas le plein risque)
    st.update_equity(100750, 100750)
    d = DailyGuard(settings.risk, dict(settings.daily_profit, giveback_action="reduce_risk")).evaluate(st)
    assert d.entries_allowed and d.risk_percent == settings.daily_profit["giveback_risk_percent"]
    # mode "lock" historique (celui du futur mode prop réel) : mêmes conditions → entrées gelées
    st2 = make_state()
    st2.update_equity(100800, 100800)
    st2.update_equity(100500, 100500)
    d2 = DailyGuard(settings.risk, dict(settings.daily_profit, giveback_action="lock")).evaluate(st2)
    assert not d2.entries_allowed and "GIVEBACK_FLOOR" in st2.lock_reasons
    # bascule lock → off : le verrou hérité est levé, plus aucune restriction
    d3 = DailyGuard(settings.risk, settings.daily_profit).evaluate(st2)
    assert d3.entries_allowed and "GIVEBACK_FLOOR" not in st2.lock_reasons


def test_total_open_risk_and_max_positions(settings):
    """Les deux plafonds se déclenchent quand l'inventaire atteint la limite configurée.

    Les valeurs viennent de `config/risk.yaml` : figer un nombre de positions en dur ferait
    échouer ce test au moindre ajustement du budget de risque, sans qu'aucune règle soit cassée.
    """
    limites = RiskLimits.from_config(settings.risk)
    rm = RiskManager(limites)
    st = make_state()
    n = int(settings.risk["max_open_positions"])
    part = float(settings.risk["max_total_open_risk_percent"]) / n        # sature pile le budget
    for i in range(n):
        st.bot_positions[str(i)] = BotPositionPlan(i, f"S{i}", "BUY", "a", "c", 1.0, 0.99, 0.1, 300.0, part)
    checks = {c.name: c.ok for c in rm.check_limits(st, "EURUSD", Side.BUY, 250.0, 0, n)}
    assert not checks["max_open_positions"], "le nombre de positions doit être plafonné"
    assert not checks["max_total_open_risk"], "le budget de risque total doit être plafonné"


def test_duplicate_symbol_position_refused(settings):
    rm = RiskManager(RiskLimits.from_config(settings.risk))
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 250.0, 0.25)
    checks = {c.name: c.ok for c in rm.check_limits(st, "EURUSD", Side.BUY, 250.0, 1, 1)}
    assert not checks["max_positions_per_symbol"] and not checks["no_averaging_or_grid"]


# ---------------- prop ----------------
def test_non_demo_account_blocks_execution(settings):
    # sans le drapeau AUTONOMOUS_TRADING_PROP, un compte REAL reste bloqué quel que soit le profil
    pg = PropGuard(PropProfile.from_config(settings.prop), True, False, 1.0)
    assert pg.authorization(TradeMode.DEMO).ok
    assert not pg.authorization(TradeMode.REAL).ok
    assert not pg.authorization(TradeMode.UNKNOWN).ok
    # trade_mode UNKNOWN : refus même avec tous les verrous ouverts
    assert not PropGuard(PropProfile.from_config(settings.prop), True, settings.autonomous_prop, 1.0).authorization(TradeMode.UNKNOWN).ok


def test_prop_unknown_rules_block_even_with_flags(settings):
    base = dict(settings.prop, prop_rules_verified=True, user_explicitly_authorized_prop_automation=True)
    # une règle critique effacée du profil → ambiguïté → exécution prop bloquée malgré tous les drapeaux
    pg = PropGuard(PropProfile.from_config(dict(base, ea_allowed="UNKNOWN", daily_loss_basis=None)), True, True, 1.0)
    assert not pg.prop_automation_allowed and pg.profile.ambiguous
    # règles relevées mais approbation de l'EA par la prop firm non obtenue → toujours bloqué
    pg2 = PropGuard(PropProfile.from_config(dict(base, ea_approval_obtained=False)), True, True, 1.0)
    assert not pg2.profile.ambiguous and not pg2.prop_automation_allowed
    assert any("EA_APPROVAL_OBTAINED" in r for r in pg2.blocking_reasons)
    # approbation obtenue + drapeaux + autorisation explicite → seul cas autorisé
    pg3 = PropGuard(PropProfile.from_config(dict(base, ea_approval_obtained=True)), True, True, 1.0)
    assert pg3.prop_automation_allowed and pg3.authorization(TradeMode.REAL).ok


def test_settings_autonomous_prop_requires_all_flags(settings):
    # dépôt du 2026-09-21 : drapeau + règles vérifiées + autorisation explicite → vrai ; chaque drapeau manquant → faux
    assert settings.autonomous_prop is True
    for flag in ("prop_rules_verified", "user_explicitly_authorized_prop_automation"):
        saved = settings.prop[flag]
        settings.prop[flag] = False
        assert settings.autonomous_prop is False, flag
        settings.prop[flag] = saved
    settings.system["autonomous_prop"] = False
    assert settings.autonomous_prop is False


# ---------------- corrélation ----------------
def test_currency_factor_guard(settings):
    """Les montants dérivent du plafond configuré : figer 300 $ casserait ce test au moindre
    ajustement du budget de risque, sans qu'aucune règle ne soit violée."""
    cg = CorrelationGuard(CorrelationLimits.from_config(settings.correlation))
    st = make_state()
    plafond = st.equity * settings.correlation["max_currency_factor_risk_percent"] / 100.0
    part = plafond * 0.4                              # deux positions = 0,8 x plafond : sous la limite
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, part, 0.1)
    st.bot_positions["2"] = BotPositionPlan(2, "GBPUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, part, 0.1)
    # une troisième position short USD dépasse le plafond
    checks = {c.name: c.ok for c in cg.check(st, "AUDUSD", Side.BUY, part)}
    assert not checks["currency_factor_risk"]
    # une position long USD réduit l'exposition nette : elle doit passer
    checks2 = {c.name: c.ok for c in cg.check(st, "USDCHF", Side.BUY, part)}
    assert checks2["currency_factor_risk"]


def test_correlation_matrix_cluster(settings):
    cg = CorrelationGuard(CorrelationLimits(max_correlated_cluster_risk_percent=0.4, correlation_threshold=0.7))
    st = make_state()
    st.bot_positions["1"] = BotPositionPlan(1, "EURUSD", "BUY", "a", "c", 1.0, 0.99, 0.1, 250.0, 0.25)
    corr = pd.DataFrame([[1.0, 0.9], [0.9, 1.0]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"])
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.BUY, 250.0, corr)}
    assert not checks["correlated_cluster_risk"]
    # sens opposé sur un instrument corrélé à 0,9 : cluster quand même (corrélation en valeur absolue depuis
    # le 2026-09-21 : long USTEC + short US30 restent deux paris sur le même thème)
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.SELL, 250.0, corr)}
    assert not checks["correlated_cluster_risk"]
    # corrélation faible : pas de cluster, quel que soit le sens
    corr_faible = pd.DataFrame([[1.0, 0.2], [0.2, 1.0]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"])
    checks = {c.name: c.ok for c in cg.check(st, "GBPUSD", Side.SELL, 250.0, corr_faible)}
    assert checks["correlated_cluster_risk"]
    # corrélation fortement négative, même sens (EURUSD BUY + USDCHF BUY = couverture) : cluster aussi
    corr_neg = pd.DataFrame([[1.0, -0.9], [-0.9, 1.0]], index=["EURUSD", "USDCHF"], columns=["EURUSD", "USDCHF"])
    checks = {c.name: c.ok for c in cg.check(st, "USDCHF", Side.BUY, 250.0, corr_neg)}
    assert not checks["correlated_cluster_risk"]


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



def test_controle_desactive_ne_refuse_plus_mais_reste_journalise(settings, broker):
    """2026-09-25, retour au 19/09 : un contrôle listé dans `disabled_checks` passe, avec la raison qu'il aurait donnée."""
    import dataclasses
    import pandas as pd
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    tick, spec = broker.tick("EURUSD"), broker.symbol_info("EURUSD")
    dist = tick.spread_points(spec) * spec.point * 1.5            # coût d'entrée = 67 % du risque
    c.sl, c.tp_plan = c.entry - dist, [c.entry + 3 * dist]
    ctx = dataclasses.replace(ctx_for(broker, c, make_state(), atr, correlations=pd.DataFrame()),
                              disabled_checks=("07b_spread_vs_sl",))
    res, _ = gate.evaluate(ctx)
    ch = {x.name: x for x in res.checks}["07b_spread_vs_sl"]
    assert ch.ok is True and "aurait refusé" in ch.detail and "% du risque" in ch.detail
