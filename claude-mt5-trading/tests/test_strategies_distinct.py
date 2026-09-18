"""Garantie transversale : chaque agent générateur possède SA PROPRE stratégie, distincte des autres.

Ce test protège l'exigence « chaque agent utilise sa propre stratégie de trading » :
1. tous les modules de stratégies se chargent (aucun repli silencieux sur le screener générique) ;
2. chaque agent générateur du registre est enregistré dans SCREENERS sous son `agent_id` ;
3. deux agents n'ont jamais le même code source, et une stratégie fait au moins 12 lignes ;
4. sur des données de marché simulées, deux agents ne produisent jamais exactement le même
   ensemble (non vide) de signaux — deux agents identiques seraient donc détectés ;
5. la vitalité globale ne régresse pas (filet, la vitalité fine est testée par famille).
"""
from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from tradinglab.agents import screeners, strategies
from tradinglab.agents.registry import AgentRegistry
from tradinglab.market_data.feed import MarketDataFeed

TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
MIN_STRATEGY_LINES = 12


@pytest.fixture(scope="module")
def broker_module():
    """Broker simulé partagé par le module (horloge figée sur un mardi, session de Londres)."""
    from datetime import datetime, timezone

    from tradinglab.mt5.mock_adapter import MockBroker

    b = MockBroker(seed=7)
    b.connect()
    b.set_now(datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc))
    return b


@pytest.fixture(scope="module")
def generators():
    """Agents générateurs du registre (ceux qui produisent des TradeCandidate)."""
    reg = AgentRegistry()
    agents = list(reg.generators())
    # les agents SHADOW (famille K) doivent aussi avoir leur stratégie propre
    agents += [a for a in reg.agents.values() if a.generates_trades and a not in agents]
    return agents


@pytest.fixture(scope="module")
def own_functions(generators):
    screeners._load_own_strategies()
    return {a.agent_id: screeners.SCREENERS[a.agent_id] for a in generators if a.agent_id in screeners.SCREENERS}


def test_tous_les_modules_de_strategies_se_chargent():
    assert strategies.FAILED == {}, f"modules non chargés : {strategies.FAILED}"
    assert len(strategies.LOADED) == len(strategies.FAMILY_MODULES)


def test_chaque_agent_generateur_a_sa_propre_strategie(generators, own_functions):
    manquants = sorted(a.agent_id for a in generators if a.agent_id not in own_functions)
    assert not manquants, f"agents sans stratégie propre (repli générique) : {manquants}"


def test_les_sources_des_strategies_sont_toutes_differentes(own_functions):
    sources: dict[str, str] = {}
    for agent_id, fn in own_functions.items():
        src = inspect.getsource(fn)
        assert len(src.splitlines()) >= MIN_STRATEGY_LINES, f"{agent_id} : stratégie trop courte pour être propre"
        corps = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", '"""', "'''")))
        jumeau = sources.get(corps)
        assert jumeau is None, f"{agent_id} a le même code que {jumeau}"
        sources[corps] = agent_id


@pytest.fixture(scope="module")
def signatures(broker_module, own_functions, generators):
    """Signatures (symbole, sens, entrée, SL) produites par chaque agent sur plusieurs instants."""
    broker = broker_module
    specs = {a.agent_id: a for a in generators}
    out: dict[str, set] = {aid: set() for aid in own_functions}
    for step in range(3):
        if step:
            broker.advance_bars(48)
            broker.set_now(None)
            broker.set_now(broker.now() + timedelta(hours=step))
        feed = MarketDataFeed(broker, TIMEFRAMES)
        snaps = feed.snapshots(broker.symbols(), now=broker.now())
        for agent_id, fn in own_functions.items():
            for snap in snaps.values():
                c = screeners.run_screener(specs[agent_id], snap)
                if c is not None:
                    out[agent_id].add((c.symbol, c.side.value, round(c.entry, 5), round(c.sl, 5)))
    return out


def test_deux_agents_ne_produisent_pas_les_memes_signaux(signatures):
    vus: dict[frozenset, str] = {}
    for agent_id, sig in signatures.items():
        if not sig:
            continue
        cle = frozenset(sig)
        jumeau = vus.get(cle)
        assert jumeau is None, f"{agent_id} et {jumeau} produisent exactement les mêmes signaux"
        vus[cle] = agent_id


def test_aucune_regression_de_vitalite(signatures):
    """Filet anti-régression : le nombre d'agents qui déclenchent ne doit pas s'effondrer.

    La vitalité fine (chaque agent doit pouvoir déclencher sur des données réalistes) est vérifiée
    module par module dans les fichiers `test_strategies_<famille>.py` ; ici on garde un seuil bas mais
    ferme, qui détecte une régression globale (par exemple une condition cassée dans un helper commun).
    """
    vivants = [a for a, s in signatures.items() if s]
    assert len(vivants) >= 8, f"seulement {len(vivants)}/{len(signatures)} agents produisent un candidat"
