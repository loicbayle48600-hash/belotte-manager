"""Tests des stratégies propres de la famille K (news-sensibles, statut SHADOW) : K01, K02.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou
None), absence de lookahead (la barre en formation n'influence pas la décision) et vitalité du module (au moins
un candidat produit sur l'ensemble des snapshots).
"""
from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import k_news  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import Side, TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = ["K01", "K02"]
TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]


@pytest.fixture(scope="module")
def specs():
    return {a.agent_id: a for a in default_agents() if a.agent_id in AGENT_IDS}


@pytest.fixture(scope="module")
def broker_module():
    from datetime import datetime, timezone

    from tradinglab.mt5.mock_adapter import MockBroker

    b = MockBroker(seed=7)
    b.connect()
    b.set_now(datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc))
    return b


@pytest.fixture(scope="module")
def snapshots(broker_module):
    """Snapshots de tous les symboles à 5 instants différents (le broker avance de 48 barres M5 entre chaque)."""
    broker = broker_module
    out = []
    for step in range(5):
        if step:
            broker.advance_bars(48)
            broker.set_now(None)
            broker.set_now(broker.now() + timedelta(hours=step))
        feed = MarketDataFeed(broker, TIMEFRAMES)
        out.append(feed.snapshots(broker.symbols(), now=broker.now()))
    return out


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_registered(agent_id, specs):
    assert agent_id in screeners.SCREENERS
    assert specs[agent_id].strategy == agent_id
    fn = screeners.SCREENERS[agent_id]
    assert fn.__name__ == f"strategy_{agent_id.lower()}"
    assert fn.__doc__ and len(fn.__doc__) > 100


def _check_candidate(c, spec, snap):
    assert isinstance(c, TradeCandidate)
    assert c.agent_id == spec.agent_id
    assert c.symbol == snap.symbol
    assert c.bar_time
    assert 0 <= c.setup_score <= 100
    assert c.rr >= 1.5
    assert len(c.tp_plan) >= 1
    chk = validate_stop_loss(c.side, c.entry, c.sl, snap.spec, atr=c.atr)
    assert chk.ok, chk.reason
    # distance entrée→SL entre 0,3 et 3 ATR du tf d'entrée
    atr_entry = float(snap.frames[spec.timeframes["entry"]].iloc[-2]["atr14"])
    assert 0.3 * atr_entry <= c.sl_distance <= 3.0 * atr_entry + 1e-12
    # SL du bon côté et TP croissants dans le sens du trade
    assert c.side.sign * (c.entry - c.sl) > 0
    for tp in c.tp_plan:
        assert c.side.sign * (tp - c.entry) > 0
    assert c.invalidation
    assert isinstance(c.arguments_for, list) and c.arguments_for
    assert isinstance(c.arguments_against, list) and c.arguments_against
    # bar_time = horodatage de la barre clôturée du tf d'entrée
    assert c.bar_time == str(snap.frames[spec.timeframes["entry"]].iloc[-2]["time"])


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_exception_and_valid_candidates(agent_id, specs, snapshots):
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    for snaps in snapshots:
        for sym, snap in snaps.items():
            c = fn(spec, snap)
            if c is not None:
                _check_candidate(c, spec, snap)


def _signature(c):
    if c is None:
        return None
    return (c.symbol, c.side, round(c.entry, 8), round(c.sl, 8), tuple(round(x, 8) for x in c.tp_plan), c.setup_score,
            c.rr, c.bar_time, c.invalidation, tuple(c.arguments_for), tuple(c.arguments_against))


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_lookahead(agent_id, specs, snapshots):
    """Modifier la dernière ligne (barre en formation) de chaque frame ne change pas la décision."""
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    for snaps in snapshots:
        for sym, snap in snaps.items():
            before = _signature(fn(spec, snap))
            saved = {}
            for tf, df in snap.frames.items():
                if len(df) == 0:
                    continue
                saved[tf] = df.copy()
                last = len(df) - 1
                move = float(df["atr14"].iloc[-2]) * 5 if len(df) >= 2 else 1.0
                df.loc[last, "close"] = float(df["close"].iloc[-1]) + move
                df.loc[last, "high"] = max(float(df["high"].iloc[-1]), float(df.loc[last, "close"]))
                df.loc[last, "low"] = min(float(df["low"].iloc[-1]), float(df.loc[last, "close"]) - move * 2)
                df.loc[last, "tick_volume"] = float(df["tick_volume"].iloc[-1]) * 10
            try:
                after = _signature(fn(spec, snap))
            finally:
                for tf, df in saved.items():
                    snap.frames[tf] = df
            assert before == after, f"{agent_id}/{sym} : la barre en formation a changé la décision"


def test_module_alive(specs, snapshots):
    """Au moins un agent du module produit un candidat sur l'ensemble des snapshots."""
    total = 0
    per_agent = {}
    for agent_id in AGENT_IDS:
        spec = specs[agent_id]
        fn = screeners.SCREENERS[agent_id]
        n = sum(1 for snaps in snapshots for snap in snaps.values() if fn(spec, snap) is not None)
        per_agent[agent_id] = n
        total += n
    assert total >= 1, f"aucun candidat produit par la famille K : {per_agent}"


def _scenario_post_news(snap):
    """Injecte dans le frame M15 un motif post-événement : barre de choc, base de digestion, sortie de base.

    Seules des barres DÉJÀ CLÔTURÉES sont réécrites (la dernière ligne, barre en formation, n'est pas touchée)
    et les horodatages sont conservés : le scénario reste cohérent avec le reste du snapshot.
    """
    from tradinglab.market_data.indicators import enrich

    df = snap.frames["M15"][["time", "open", "high", "low", "close", "tick_volume", "spread"]].copy().reset_index(drop=True)
    n = len(df)
    ref = float((df["high"] - df["low"]).iloc[-80:-10].median())
    i = n - 7                                   # barre de choc : 5 barres avant la barre de signal
    p0 = float(df["close"].iloc[i - 1])
    df.loc[i, ["open", "high", "low", "close"]] = [p0, p0 + 2.4 * ref, p0 - 0.1 * ref, p0 + 2.0 * ref]
    base = float(df["close"].iloc[i])
    for k in range(1, 5):                       # digestion : 4 barres étroites qui conservent le déplacement
        cc = base - 0.05 * ref
        df.loc[i + k, ["open", "high", "low", "close"]] = [base, base + 0.25 * ref, base - 0.30 * ref, cc]
        base = cc
    top = float(df["high"].iloc[i + 1:i + 5].max())
    sig = i + 5                                 # barre de signal = dernière barre clôturée (indice n-2)
    df.loc[sig, ["open", "high", "low", "close"]] = [base, top + 0.6 * ref, base - 0.15 * ref, top + 0.45 * ref]
    snap.frames["M15"] = enrich(df)
    return snap


def test_k01_produit_un_candidat_sur_un_scenario_post_news(specs, snapshots):
    """K01 sur un motif post-événement construit : choc directionnel, digestion contractée, sortie de la base."""
    spec = specs["K01"]
    fn = screeners.SCREENERS["K01"]
    snap = _scenario_post_news(copy.deepcopy(next(iter(snapshots[0].values()))))
    c = fn(spec, snap)
    assert c is not None, "K01 ne reconnaît pas un motif post-événement complet"
    assert c.side is Side.BUY
    _check_candidate(c, spec, snap)


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "k_news" in strategies.LOADED
    assert "k_news" not in strategies.FAILED
