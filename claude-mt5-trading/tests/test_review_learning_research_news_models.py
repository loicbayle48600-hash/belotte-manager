"""Tests de revue — zone learning / research / news / models / shadow.

Chaque test correspond à une correction et échouait sur le code précédent.
"""
import http.client
import json
import math
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradinglab.agents.registry import AgentRegistry
from tradinglab.backtest.engine import BTCosts, assert_no_lookahead, run_backtest, _signal_key
from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, SystemState
from tradinglab.core.types import AgentStatus, Deal, Regime, Side, TradeCandidate
from tradinglab.learning import post_trade as post_trade_mod
from tradinglab.learning.post_trade import ClosedTradeUnknown, PostTradeAnalyzer, build_trade_record
from tradinglab.learning.reports import daily_report
from tradinglab.learning.store import LearningStore, TradeRecord
from tradinglab.models.client import DEFAULT_PRICES, LLMClient, LLMResponse
from tradinglab.models.router import ModelRouter
from tradinglab.news import providers as providers_mod
from tradinglab.news.hub import NewsHub
from tradinglab.news.providers import FMPProvider, NewsProvider, ProviderError, parse_number
from tradinglab.research import adapters as adapters_mod
from tradinglab.research.adapters import make_signal_fn, required_bars
from tradinglab.research.pipeline import ResearchPipeline, Stage
from tradinglab.shadow.shadow import ShadowTrader

NOW = datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc)


def _plan(ticket: int = 11) -> BotPositionPlan:
    return BotPositionPlan(ticket=ticket, symbol="EURUSD", side="BUY", agent_id="B01", candidate_id="c1", entry=1.1, initial_sl=1.09,
                           initial_volume=0.1, initial_risk_money=100.0, risk_percent=0.25, opened_at=(NOW - timedelta(hours=2)).isoformat())


def _deal(position_id: int, profit: float, comment: str = "tp") -> Deal:
    return Deal(ticket=1, order=1, position_id=position_id, symbol="EURUSD", side=Side.SELL, volume=0.1, price=1.11, profit=profit,
                commission=0.0, swap=0.0, time=NOW, magic=51000, entry="OUT", comment=comment)


# ---------------------------------------------------------------------------
# learning/store.py + post_trade.py
# ---------------------------------------------------------------------------

def test_agent_stats_survives_trade_recorded_without_candidate(home):
    """Position adoptée / cache perdu : setup_score None ne doit ni planter agent_stats ni valoir 0."""
    st = LearningStore(home / "data" / "l.db")
    for i in range(12):
        st.record_trade(TradeRecord(i, "B01", "EURUSD", "BUY", 1.0, 0.99, 100, 0.1, 1.0 if i % 2 else -1.0, 0, "t", f"t{i:03d}",
                                    features={"setup_score": None, "vol_pct": None}))
    stats = st.agent_stats("B01")          # levait TypeError: float() argument ... 'NoneType'
    assert stats.sample_size == 12 and stats.confidence_calibration == 0.0
    assert st.leaderboard()[0]["agent_id"] == "B01"
    # le record construit sans candidat ne persiste aucun None (UNKNOWN = clé absente)
    rec = build_trade_record(_plan(), [_deal(11, 50.0)], None, NOW.isoformat())
    assert None not in rec.features.values() and "setup_score" not in rec.features


def test_vol_pct_nan_is_unknown_bucket_and_null_in_snapshots(home):
    st = LearningStore(home / "data" / "l.db")
    st.record_trade(TradeRecord(1, "B01", "EURUSD", "BUY", 1.0, 0.99, 100, 0.1, 1.0, 0, "t", "t1", features={"vol_pct": math.nan}))
    stats = st.agent_stats("B01")
    assert "HIGH" not in stats.by_vol_bucket and stats.by_vol_bucket["UNKNOWN"]["n"] == 1
    sid = st.store_snapshot("EURUSD", "TRENDING_UP", "LONDON", {"vol_pct": math.nan, "adx": 22.0, "setup_score": 70})
    row = dict(st.conn.execute("SELECT vol_pct, adx, features FROM market_snapshots WHERE id=?", (sid,)).fetchone())
    assert row["vol_pct"] is None and row["adx"] == 22.0
    assert json.loads(row["features"])["vol_pct"] is None      # jamais `NaN` dans le JSON


def test_unknown_close_is_not_a_zero_result(home):
    """Sans deal OUT retrouvé : résultat UNAVAILABLE, exclu des stats, revue INDETERMINE, strict → exception."""
    st = LearningStore(home / "data" / "l.db")
    with pytest.raises(ClosedTradeUnknown):
        build_trade_record(_plan(), [], None, NOW.isoformat(), strict=True)
    rec = build_trade_record(_plan(), [_deal(999, 50.0)], None, NOW.isoformat())     # deal d'une autre position
    assert rec.exit_reason == "UNKNOWN" and rec.features["outcome_provenance"] == "UNAVAILABLE"
    st.record_trade(rec)
    st.record_trade(TradeRecord(2, "B01", "EURUSD", "BUY", 1.0, 0.99, 100, 0.1, 2.0, 200, "t", "t2", exit_reason="tp"))
    stats = st.agent_stats("B01")
    assert stats.sample_size == 1 and stats.expectancy_r == 2.0      # le 0R fictif n'est pas compté

    class _NoLLM:
        def complete(self, *a, **k):
            raise AssertionError("aucun appel LLM sur un résultat inconnu")
    review = PostTradeAnalyzer(st, llm=_NoLLM()).analyze(rec)
    assert review.verdict == "INDETERMINE" and review.provenance == "UNAVAILABLE" and not review.challenger_needed


def test_daily_report_uses_calendar_day(home, settings):
    st = LearningStore(home / "data" / "l.db")
    now = datetime(2026, 1, 20, 1, 0, tzinfo=timezone.utc)
    st.record_trade(TradeRecord(1, "B01", "EURUSD", "BUY", 1.0, 0.99, 100, 0.1, -1.0, -100, "t", (now - timedelta(hours=5)).isoformat()))
    st.record_trade(TradeRecord(2, "B01", "EURUSD", "BUY", 1.0, 0.99, 100, 0.1, 1.5, 150, "t", (now - timedelta(minutes=30)).isoformat()))
    rep = daily_report(st, SystemState(), Journal(home / "logs"), now=now)
    assert rep["trades"]["trades"] == 1 and rep["trades"]["total_pnl"] == 150.0     # la veille n'est plus incluse


# ---------------------------------------------------------------------------
# research/pipeline.py + adapters.py + backtest/engine.py
# ---------------------------------------------------------------------------

def _pipeline(settings, broker, home, bars=800):
    reg = AgentRegistry(status_file=home / "data" / "agent_status.json")
    st = LearningStore(home / "data" / "l.db")
    p = ResearchPipeline(reg, st, settings.learning, settings.backtest, lambda s, tf, n: broker.rates(s, tf, n),
                         {"EURUSD": broker.symbol_info("EURUSD")}, home / "data" / "research", bars=bars)
    return reg, st, p


def _validate_all(p, st, ch):
    rec = p.record(ch.agent_id)
    for s in (Stage.BACKTEST, Stage.OUT_OF_SAMPLE, Stage.WALK_FORWARD):
        p._set(rec, s, True, {"symbol": "EURUSD", "sample_size": 30, "max_drawdown_r": 5.0, "expectancy_r": 0.3})
    p._set(rec, Stage.MONTE_CARLO, True, {"p95_max_dd_r": 6.0, "prob_negative": 0.1, "p05_total_r": 2.0})
    for i in range(25):
        st.record_trade(TradeRecord(0, ch.agent_id, "EURUSD", "BUY", 1.0, 0.99, 100, 0, 1.0 if i % 3 else -1.0, 0, "t",
                                    (NOW - timedelta(days=1)).isoformat(), mode="shadow"))
    assert p.stage_shadow(ch).passed(Stage.SHADOW)
    assert p.stage_statistical_review(ch).passed(Stage.STATISTICAL_REVIEW)
    assert p.stage_risk_review(ch).passed(Stage.RISK_REVIEW)


def test_rollback_requires_full_revalidation_before_live_again(settings, broker, home):
    reg, st, p = _pipeline(settings, broker, home)
    ch = p.generate_challengers(reg.get("B01"), 1, seed=5)[0]
    _validate_all(p, st, ch)
    p.promote(ch.agent_id)
    assert reg.get(ch.agent_id).status == "LIVE"
    p.rollback(ch.agent_id)
    assert reg.get(ch.agent_id).status == "SHADOW"
    rec = p.record(ch.agent_id)
    assert not rec.passed(Stage.SHADOW) and not rec.passed(Stage.RISK_REVIEW) and rec.history[-1]["stages"]
    # le cycle de recherche suivant (advance) ne doit pas re-promouvoir : une NOUVELLE période shadow est exigée
    # (les 25 trades shadow antérieurs au rollback ne comptent plus)
    for _ in range(4):
        p.advance(ch.agent_id)
    assert reg.get(ch.agent_id).status != "LIVE"
    assert p.record(ch.agent_id).stages[Stage.SHADOW.value]["metrics"]["shadow_sample"] == 0
    # rollback vers RESEARCH : tout le pipeline est invalidé
    p.rollback(ch.agent_id, AgentStatus.RESEARCH)
    assert p.record(ch.agent_id).stages == {}


def test_backtest_uses_agent_entry_tf_and_reports_insufficient_data(settings, broker, home):
    reg, st, p = _pipeline(settings, broker, home)
    b02 = reg.get("B02")
    assert b02.timeframes == {"entry": "H1", "trend": "H4"}
    calls = []
    p.data = lambda s, tf, n: calls.append(tf) or broker.rates(s, tf, n)
    rec = p.stage_backtest(b02)
    assert calls == ["H1"]                                        # plus jamais M15 pour un agent H1
    m = rec.stages[Stage.BACKTEST.value]["metrics"]
    assert not rec.passed(Stage.BACKTEST) and m["entry_tf"] == "H1" and "insuffisantes" in m["error"]
    assert required_bars("M15", "H1") == 260 and required_bars("H4", "D1") == 366 and required_bars("M5", "H1") == 732


def test_signal_fn_precomputes_once_without_lookahead(broker, home, monkeypatch):
    reg = AgentRegistry(status_file=home / "data" / "agent_status.json")
    ss = broker.symbol_info("EURUSD")
    df = broker.rates("EURUSD", "M15", 900)
    costs = BTCosts(point=ss.point, tick_value=ss.tick_value, tick_size=ss.tick_size)
    spec = reg.get("B01")
    counter = {"n": 0}
    real_enrich = adapters_mod.enrich

    def counting_enrich(frame):
        counter["n"] += 1
        return real_enrich(frame)
    monkeypatch.setattr(adapters_mod, "enrich", counting_enrich)
    res = run_backtest(df, make_signal_fn(spec, ss, "M15"), costs)
    assert counter["n"] == 2                                      # entrée + tendance, une seule fois (avant : 2 par barre)
    assert res.metrics.sample_size >= 1
    # même résultat avec ou sans pré-calcul, et aucune fuite du futur
    fn_p = make_signal_fn(spec, ss, "M15")
    fn_p.prepare(df)
    fn_u = make_signal_fn(spec, ss, "M15")
    for i in range(260, len(df), 37):
        assert _signal_key(fn_p(df.iloc[: i + 1])) == _signal_key(fn_u(df.iloc[: i + 1]))
    assert assert_no_lookahead(df, fn_p, samples=8, warmup=260)
    assert fn_p.data_error is None
    fn_h4 = make_signal_fn(reg.get("B02"), ss, "H1")
    fn_h4.prepare(broker.rates("EURUSD", "H1", 200))
    assert fn_h4.data_error and "H4" in fn_h4.data_error


# ---------------------------------------------------------------------------
# news/providers.py + hub.py
# ---------------------------------------------------------------------------

def test_get_json_converts_http_client_and_value_errors(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "secret-key")
    p = FMPProvider(base_url="https://financialmodelingprep.com/stable", api_key_env="FMP_API_KEY")
    for exc in (http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x"), ValueError("unknown url type")):
        def boom(req, timeout=None, _e=exc):
            raise _e
        monkeypatch.setattr(providers_mod.urllib.request, "urlopen", boom)
        with pytest.raises(ProviderError) as ei:
            p.fetch_calendar(NOW)
        assert "secret-key" not in str(ei.value)


def test_hub_never_propagates_unexpected_provider_exception():
    class Broken(NewsProvider):
        name, kind, quality = "broken", "economic_calendar", "structured_api"

        def available(self):
            return True

        def fetch_news(self, now):
            raise RuntimeError("boom")

        def fetch_calendar(self, now, days_ahead=2):
            raise RuntimeError("boom")
    cfg = {"max_age_minutes": 90, "block_minutes_before_high_impact": 30, "block_minutes_after_high_impact": 15, "degraded_after_failures": 3}
    hub = NewsHub([Broken()], cfg)
    for i in range(3):
        hub.refresh_calendar(NOW + timedelta(minutes=i))         # levait RuntimeError → cycle exception → SAFE_MODE
    assert hub.state.failures == 3 and hub.state.degraded is True


def test_parse_number_decimal_comma():
    assert parse_number("-0,5") == pytest.approx(-0.5)
    assert parse_number("1,5") == pytest.approx(1.5)
    assert parse_number("0,5%") == pytest.approx(0.5)
    assert parse_number("1,200") == pytest.approx(1200.0)
    assert parse_number("1,234,567") == pytest.approx(1234567.0)
    assert parse_number("1.2K") == pytest.approx(1200.0)


# ---------------------------------------------------------------------------
# shadow/shadow.py
# ---------------------------------------------------------------------------

def _cand(symbol: str, agent: str, bar: str) -> TradeCandidate:
    return TradeCandidate(symbol=symbol, side=Side.BUY, entry=1.1, sl=1.09, tp_plan=[1.13], timeframes=["M15"], regime=Regime.TRENDING,
                          agent_id=agent, setup_score=70, session="LONDON", bar_time=bar)


def test_shadow_state_keeps_recent_keys_and_writes_atomically(home):
    st = LearningStore(home / "data" / "l.db")
    f = home / "state" / "shadow_positions.json"
    sh = ShadowTrader(st, f, max_open=5000)
    # 2100 clés XAUUSD anciennes puis 5 clés AUDUSD récentes
    for i in range(2100):
        sh.executed[f"XAUUSD|BUY|B01|old{i:05d}"] = None
    sh.open_from_candidates([_cand("AUDUSD", f"A{i}", "2026-01-20T10:00") for i in range(5)], now=NOW)
    assert not f.with_suffix(".tmp").exists() and json.loads(f.read_text(encoding="utf-8"))
    sh2 = ShadowTrader(st, f)
    assert all(f"AUDUSD|BUY|A{i}|2026-01-20T10:00" in sh2.executed for i in range(5))      # avant : perdues (tri lexicographique)
    assert len(sh2.executed) <= 2000
    assert not sh2.open_from_candidates([_cand("AUDUSD", "A0", "2026-01-20T10:00")], now=NOW)  # pas de doublon


def test_shadow_position_without_snapshot_is_released_after_timeout(home):
    st = LearningStore(home / "data" / "l.db")
    sh = ShadowTrader(st, home / "state" / "shadow.json", max_open=1)
    sh.open_from_candidates([_cand("USDJPY", "B01", "t0")], now=NOW - timedelta(hours=80))
    assert sh.update({}, now=NOW - timedelta(hours=79)) == [] and len(sh.positions) == 1
    assert sh.update({}, now=NOW) == [] and sh.positions == {}          # slot libéré, aucun résultat inventé
    assert st.trade_count("shadow") == 0
    assert st.agent_events("B01", 5)[0]["event"] == "shadow_timeout_no_data"


# ---------------------------------------------------------------------------
# models/client.py
# ---------------------------------------------------------------------------

def test_llm_client_timeout_prices_and_bounded_cache(settings, monkeypatch, home):
    captured = {}

    class _Msgs:
        def create(self, **kw):
            return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')], usage=SimpleNamespace(input_tokens=1000000, output_tokens=0))

    class _Anthropic:
        def __init__(self, **kw):
            captured.update(kw)
            self.messages = _Msgs()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Anthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5", "claude-fable-5-1"]
    st = SystemState()
    client = LLMClient(router, st, cache_ttl_sec=240, prices={"claude-opus-5": (5.0, 25.0)}, journal=Journal(home / "logs"))
    client._client()
    assert captured == {"timeout": 30.0, "max_retries": 1}       # avant : Anthropic() → 600 s × 3 tentatives
    assert DEFAULT_PRICES["claude-fable-5-1"] == (10.0, 50.0) and DEFAULT_PRICES["claude-sonnet-5"] == (2.0, 10.0)
    assert client.prices["claude-fable-5-1"] == (10.0, 50.0)     # la surcharge partielle garde les autres tarifs
    assert client._cost("claude-fable-5-1", 1_000_000, 0) == pytest.approx(10.0)
    # cache borné et purgé
    for i in range(700):
        client._cache[f"k{i}"] = (0.0, LLMResponse("x", "m", "TIER_C", 1, 1, 0.0))
    client._cache["fresh"] = (time.time(), LLMResponse("x", "m", "TIER_C", 1, 1, 0.0))
    client._prune_cache()
    assert len(client._cache) == 1 and "fresh" in client._cache


def test_llm_client_failure_is_journaled_as_skipped(settings, monkeypatch, home):
    class _Msgs:
        def create(self, **kw):
            raise TimeoutError("lent")
    router = ModelRouter(settings.models)
    router.available_models = ["claude-opus-5"]
    st = SystemState()
    journal = Journal(home / "logs")
    client = LLMClient(router, st, sdk_client=SimpleNamespace(messages=_Msgs()), journal=journal)
    assert client.complete("bull_thesis", "sys", "user") is None
    kinds = [e["kind"] for e in journal.read_day(datetime.now(timezone.utc))]
    assert "llm_skipped" in kinds
