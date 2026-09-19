"""Routeur de modèles, pipeline de recherche (pas de contournement), agents sans accès exécution, commandes CLI."""
import inspect
from pathlib import Path

import pytest

from tradinglab.agents import registry as registry_mod, review as review_mod, screeners as screeners_mod
from tradinglab.agents.registry import AgentRegistry, AgentStatus
from tradinglab.agents.review import AdversarialReview
from tradinglab.api.commands import CommandHandler
from tradinglab.core.state import StateStore, SystemState
from tradinglab.core.types import Regime, Side, TradeCandidate, Verdict
from tradinglab.learning.store import LearningStore, TradeRecord
from tradinglab.models.client import LLMClient
from tradinglab.models.router import ModelRouter
from tradinglab.research import pipeline as pipeline_mod
from tradinglab.research.pipeline import DegradationManager, PromotionError, ResearchPipeline, Stage
from tradinglab.mcp import server as mcp_mod


def test_model_router_fallback_without_key(settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = ModelRouter(settings.models)
    assert r.refresh_availability() == [] and "ANTHROPIC_API_KEY" in r.verification_error
    d = r.route("trade_arbiter", SystemState())
    assert not d.use_llm and d.tier == "TIER_D"


def test_model_router_degrades_tier_and_budget(settings):
    r = ModelRouter(settings.models)
    r.available_models = ["claude-sonnet-5"]
    st = SystemState()
    d = r.route("chief_orchestrator", st)
    assert d.tier == "TIER_C" and d.model == "claude-sonnet-5"
    r.record_call(st, "TIER_C", 100.0)                      # budget dépassé
    assert r.budget_exhausted(st) and not r.route("worker", st).use_llm
    assert r.route("trade_arbiter", st, financial_importance="high").use_llm   # décision financière importante autorisée


def test_model_router_hourly_cap(settings):
    r = ModelRouter(dict(settings.models, max_worker_calls_per_hour=1))
    r.available_models = ["claude-sonnet-5"]
    st = SystemState()
    assert r.route("worker", st).use_llm
    r.record_call(st, "TIER_C", 0.01)
    assert not r.route("worker", st).use_llm


def test_llm_client_returns_none_when_no_model(settings):
    r = ModelRouter(settings.models)
    c = LLMClient(r, SystemState())
    assert c.complete("worker", "sys", "user") is None


def test_review_deterministic_verdicts(settings):
    rv = AdversarialReview(None, 65, 1.5)
    c = TradeCandidate("EURUSD", Side.BUY, 1.08, 1.077, [1.0875], ["M15"], Regime.TRENDING, "B01", setup_score=80, rr=2.5)
    assert rv.review(c).verdict is Verdict.APPROVE
    c.setup_score = 58
    assert rv.review(c).verdict is Verdict.WAIT
    c.setup_score = 30
    assert rv.review(c).verdict is Verdict.REJECT
    c.setup_score = 90; c.rr = 1.0
    assert rv.review(c).verdict is Verdict.REJECT
    c.rr = 2.5; c.news_state = "BLOCKED_PRE_NEWS"
    assert rv.review(c).verdict is Verdict.WAIT
    c.news_state = "OK"; c.data_quality = "STALE"
    assert rv.review(c).verdict is Verdict.REJECT


def test_registry_has_100_plus_agents_and_statuses(home):
    reg = AgentRegistry(status_file=home / "data" / "agent_status.json")
    assert len(reg) >= 100 and reg.summary()["generators"] >= 50
    reg.set_status("B01", AgentStatus.SUSPENDED)
    reg2 = AgentRegistry(status_file=home / "data" / "agent_status.json")
    assert reg2.get("B01").status == "SUSPENDED"
    assert "B01" not in [a.agent_id for a in reg2.generators()]


def test_research_agents_cannot_call_execution():
    """Aucun module agents/recherche/revue n'importe le broker ni l'exécuteur (pas d'accès à order_send)."""
    for mod in (registry_mod, screeners_mod, review_mod, pipeline_mod):
        src = inspect.getsource(mod)
        assert "order_send" not in src and "execution.executor" not in src and "mt5.adapter" not in src
    src = inspect.getsource(mcp_mod)
    assert "order_send(" not in src and "modify_position(" not in src


def _pipeline(settings, broker, home):
    reg = AgentRegistry(status_file=home / "data" / "agent_status.json")
    st = LearningStore(home / "data" / "l.db")
    p = ResearchPipeline(reg, st, settings.learning, settings.backtest, lambda s, tf, n: broker.rates(s, tf, n),
                         {"EURUSD": broker.symbol_info("EURUSD")}, home / "data" / "research", bars=800)
    return reg, st, p


def test_promotion_cannot_bypass_pipeline(settings, broker, home):
    reg, st, p = _pipeline(settings, broker, home)
    with pytest.raises(PromotionError):
        p.promote("B01")
    ch = p.generate_challengers(reg.get("B01"), 1, seed=3)[0]
    assert ch.status == "RESEARCH" and ch.parent_id == "B01"
    rec = p.record(ch.agent_id)
    # étapes marquées artificiellement sauf SHADOW → toujours refusé
    for s in (Stage.BACKTEST, Stage.OUT_OF_SAMPLE, Stage.WALK_FORWARD, Stage.MONTE_CARLO):
        p._set(rec, s, True, {"symbol": "EURUSD"})
    with pytest.raises(PromotionError):
        p.promote(ch.agent_id)
    assert reg.get(ch.agent_id).status != "LIVE"
    # shadow insuffisant → étape SHADOW échoue
    rec = p.stage_shadow(ch)
    assert not rec.passed(Stage.SHADOW)


def test_shadow_stage_needs_sample_and_full_promotion(settings, broker, home):
    reg, st, p = _pipeline(settings, broker, home)
    ch = p.generate_challengers(reg.get("B01"), 1, seed=4)[0]
    rec = p.record(ch.agent_id)
    for s in (Stage.BACKTEST, Stage.OUT_OF_SAMPLE, Stage.WALK_FORWARD):
        p._set(rec, s, True, {"symbol": "EURUSD", "sample_size": 30, "max_drawdown_r": 5.0, "expectancy_r": 0.3})
    p._set(rec, Stage.MONTE_CARLO, True, {"p95_max_dd_r": 6.0, "prob_negative": 0.1, "p05_total_r": 2.0})
    for i in range(25):
        st.record_trade(TradeRecord(0, ch.agent_id, "EURUSD", "BUY", 1.0, 0.99, 100, 0, 1.0 if i % 3 else -1.0, 0, "t", "t", mode="shadow"))
    assert p.stage_shadow(ch).passed(Stage.SHADOW)
    assert p.stage_statistical_review(ch).passed(Stage.STATISTICAL_REVIEW)
    assert p.stage_risk_review(ch).passed(Stage.RISK_REVIEW)
    p.promote(ch.agent_id)
    assert reg.get(ch.agent_id).status == "LIVE"
    p.rollback(ch.agent_id)
    assert reg.get(ch.agent_id).status == "SHADOW"


def test_degradation_manager_demotes(settings, broker, home):
    reg, st, p = _pipeline(settings, broker, home)
    seq = [1.5, -1, 1.5, -1, 1.2] * 12 + [-1, -1, -1, 0.2] * 8
    for i, r in enumerate(seq):
        st.record_trade(TradeRecord(i, "B02", "EURUSD", "BUY", 1.0, 0.99, 100, 0.1, r, r * 100, "t", f"t{i:04d}"))
    changes = DegradationManager(reg, st, settings.learning).run()
    assert any(c["agent_id"] == "B02" and c["to"] == "DEGRADED" for c in changes)


def test_commands_read_and_queue(settings, broker):
    store = StateStore(settings.state_dir)
    store.state.equity = 100000
    store.save()
    h = CommandHandler(settings, source="test")
    assert h.run("STATUS")["equity"] == 100000
    assert "positions" in h.run("POSITIONS")
    assert h.run("AGENTS")["summary"]["total"] >= 100
    res = h.run("PAUSE")
    assert res["ok"] and store.pop_commands()[0]["command"] == "PAUSE"
    assert h.run("FOO")["ok"] is False
    assert "file" in h.run("REPORT_DAY")


def test_emergency_panic_when_orchestrator_dead(settings, broker, monkeypatch):
    from tradinglab.core.types import OrderRequest
    t = broker.tick("EURUSD")
    broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.1, t.ask - 0.003, magic=51000))
    monkeypatch.setattr("tradinglab.api.commands.make_broker", lambda kind, s: broker)
    h = CommandHandler(settings, source="test")
    res = h.run("PANIC")
    assert res.get("emergency", {}).get("ok") and not broker.positions(magic=51000)
