"""Auteur de stratégies : variantes proposées UNIQUEMENT en RESEARCH, validation stricte, pipeline obligatoire."""
import json

import pytest

from tradinglab.agents.registry import AgentRegistry, AgentStatus
from tradinglab.agents.screeners import SCREENERS
from tradinglab.learning.store import LearningStore
from tradinglab.models.client import LLMResponse
from tradinglab.research.pipeline import PromotionError, ResearchPipeline
from tradinglab.research.strategy_author import SAFE_BOUNDS, StrategyAuthor


class FakeLLM:
    """complete() renvoie les réponses fournies dans l'ordre (None une fois épuisées)."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, role, system, user, **kw):
        self.calls.append({"role": role, "system": system, "user": user, **kw})
        if not self.texts:
            return None
        return LLMResponse(self.texts.pop(0), "fake-model", "TIER_A", 10, 10, 0.0)


def _setup(settings, home):
    reg = AgentRegistry(status_file=home / "data" / "agent_status.json")
    st = LearningStore(home / "data" / "l.db")
    return reg, st


def _pipeline(settings, broker, home, reg, st):
    return ResearchPipeline(reg, st, settings.learning, settings.backtest, lambda s, tf, n: broker.rates(s, tf, n),
                            {"EURUSD": broker.symbol_info("EURUSD")}, home / "data" / "research", bars=800)


def test_without_llm_creates_research_variants_only(settings, home):
    reg, st = _setup(settings, home)
    parent = reg.get("B01")
    author = StrategyAuthor(reg, st, None, settings.learning)
    out = author.propose(parent, {"reason": "test"})
    assert 1 <= len(out) <= int(settings.learning["challenger_generation"]["max_new_per_cycle"])
    live_ids = {a.agent_id for a in reg.generators()}
    for v in out:
        assert v.status == AgentStatus.RESEARCH.value and v.status != "LIVE"
        assert v.parent_id == "B01" and v.created_by == "strategy_author"
        assert v.agent_id.startswith("CH") and reg.get(v.agent_id) is v
        assert v.agent_id not in live_ids                       # jamais dans generators() (LIVE)
        assert v.base_strategy in SCREENERS and v.family == parent.family
        assert "strategy_author" in v.description and "deterministic" in v.description
        assert "thèse" in v.entry_rules
        for k, val in v.params.items():
            if k in SAFE_BOUNDS:
                assert SAFE_BOUNDS[k][0] <= val <= SAFE_BOUNDS[k][1]
    # repli déterministe : les paramètres ET un filtre (session ou régime) varient
    assert any(v.params != parent.params for v in out)
    assert any(v.sessions != parent.sessions or v.regimes != parent.regimes for v in out)
    assert all(set(v.sessions) <= set(parent.sessions) and set(v.regimes) <= set(parent.regimes) for v in out)
    # persistance : rechargé en RESEARCH, jamais LIVE
    reg2 = AgentRegistry(status_file=home / "data" / "agent_status.json")
    for v in out:
        assert reg2.get(v.agent_id).status == "RESEARCH"
        assert v.agent_id not in {a.agent_id for a in reg2.generators()}
    assert not author.last_rejections


def test_fake_llm_valid_accepted_invalid_rejected(settings, home):
    reg, st = _setup(settings, home)
    parent = reg.get("B10")      # ema_trend_gold : markets ["metals"]
    assert parent.markets == ["metals"]
    good = {"name": "Gold London Trend", "base_strategy": parent.base_strategy,
            "params": {"adx_min": 30, "sl_atr": 2.0, "rr": 2.5}, "markets": ["metals"], "sessions": ["LONDON", "OVERLAP_LDN_NY"],
            "regimes": ["TRENDING", "BREAKOUT"], "timeframes": {"entry": "M15", "trend": "H1"},
            "thesis": "L'or tend mieux pendant Londres ; ADX plus strict.", "invalidation_rules": "clôture sous EMA50"}
    forced_live = {**good, "name": "forced_live", "status": "LIVE", "params": {"adx_min": 20}}
    bad_strategy = {**good, "name": "bad_strategy", "base_strategy": "martingale_grid"}
    bad_param = {**good, "name": "bad_param", "params": {"sl_atr": 9.0}}
    bad_market = {**good, "name": "bad_market", "markets": ["crypto"]}
    bad_session = {**good, "name": "bad_session", "sessions": ["TOKYO"]}
    unknown_param = {**good, "name": "unknown_param", "params": {"lot_multiplier": 3}}
    valid_json = json.dumps({"variants": [good, forced_live, bad_strategy, bad_param, bad_market, bad_session, unknown_param]})
    llm = FakeLLM([valid_json, "désolé, pas de JSON ici"])
    author = StrategyAuthor(reg, st, llm, settings.learning)

    out = author.propose(parent, {"ctx": 1})
    assert llm.calls[0]["role"] == "strategy_research" and "JSON" in llm.calls[0]["system"]
    assert [v.name for v in out] == ["gold_london_trend", "forced_live"]
    for v in out:
        assert v.status == "RESEARCH" and v.parent_id == "B10" and v.created_by == "strategy_author"
        assert v.markets == ["metals"] and "MODEL_INTERPRETATION" in v.description
        assert v.agent_id not in {a.agent_id for a in reg.generators()}
    assert out[0].params == {"adx_min": 30, "sl_atr": 2.0, "rr": 2.5} and out[0].sessions == ["LONDON", "OVERLAP_LDN_NY"]
    assert out[1].params["adx_min"] == 20 and out[1].params["sl_atr"] == parent.params["sl_atr"]
    reasons = " | ".join(r.reason for r in author.last_rejections)
    assert len(author.last_rejections) == 5
    assert "martingale_grid" in reasons and "sl_atr" in reasons and "crypto" in reasons and "TOKYO" in reasons and "lot_multiplier" in reasons

    # réponse LLM inexploitable → repli déterministe (jamais de variante inventée à partir d'un texte libre)
    parent2 = reg.get("B02")
    out2 = author.propose(parent2, {})
    assert out2 and all(v.status == "RESEARCH" and "deterministic" in v.description and v.parent_id == "B02" for v in out2)
    assert any("inexploitable" in r.reason for r in author.last_rejections)


def test_variants_never_live_and_promotion_refused(settings, broker, home):
    reg, st = _setup(settings, home)
    p = _pipeline(settings, broker, home, reg, st)
    author = StrategyAuthor(reg, st, None, settings.learning)
    out = author.propose(reg.get("B01"), {}, n=1)
    assert len(out) == 1
    v = out[0]
    assert v not in reg.generators() and reg.get(v.agent_id).status == "RESEARCH"
    with pytest.raises(PromotionError):
        p.promote(v.agent_id)
    assert reg.get(v.agent_id).status != "LIVE"
    # plafond max_new_per_cycle respecté même si n est plus grand
    out2 = author.propose(reg.get("B03"), {}, n=50)
    assert len(out2) <= int(settings.learning["challenger_generation"]["max_new_per_cycle"])
