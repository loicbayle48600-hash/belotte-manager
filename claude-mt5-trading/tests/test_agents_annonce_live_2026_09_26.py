"""Trading des annonces par les seuls agents spécialisés (2026-09-26, décision utilisateur, option news FOXX)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tradinglab.agents.registry import default_agents
from tradinglab.news.hub import NewsCheck
from tradinglab.orchestration.orchestrator import Orchestrator

NOW = datetime(2026, 9, 28, 12, 30, tzinfo=timezone.utc)


def _orch(state):
    o = Orchestrator.__new__(Orchestrator)
    o.news = SimpleNamespace(check=lambda cur, now, news_sensitive_strategy=False: NewsCheck(ok=False, state=state, reason="USD CPI"))
    o._symbol_currencies = lambda sym: ["BTC", "USD"]
    return o


def test_seuls_les_agents_d_annonce_passent_la_fenetre():
    agents = {a.agent_id: a for a in default_agents()}
    c = SimpleNamespace(symbol="BTCUSD")
    for etat in ("BLOCKED_PRE_NEWS", "BLOCKED_POST_NEWS", "SHOCK"):
        o = _orch(etat)
        assert o._news_check(c, agents["K03"], NOW).ok is True
        assert o._news_check(c, agents["K08"], NOW).state == "NEWS_TRADING"
        assert o._news_check(c, agents["P01"], NOW).ok is False           # agent ordinaire : toujours bloqué
    assert _orch("DEGRADED")._news_check(c, agents["K03"], NOW).ok is False  # calendrier absent : jamais


def test_agents_d_annonce_live_et_specialistes_crypto():
    agents = {a.agent_id: a for a in default_agents()}
    traders = sorted(a.agent_id for a in agents.values() if a.news_trader)
    assert traders == [f"K{i:02d}" for i in range(3, 13)]
    assert all(agents[k].status == "LIVE" for k in traders)
    assert all(agents[f"K{i:02d}"].markets == ["crypto"] for i in range(8, 13))
    assert not any(a.news_trader for a in agents.values() if not a.agent_id.startswith("K"))
