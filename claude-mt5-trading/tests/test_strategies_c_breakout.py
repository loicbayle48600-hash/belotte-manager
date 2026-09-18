"""Stratégies propres de la famille C (cassures) : enregistrement, contrat du TradeCandidate, anti-lookahead, vitalité.

Les snapshots sont construits avec le MockBroker déterministe (seed 7) à plusieurs instants : l'instant initial
(FIXED_NOW) puis après avancée du broker (barres M5 générées à la volée), pour couvrir des heures différentes.
"""
from __future__ import annotations

import dataclasses

import pytest

from tradinglab.agents.registry import default_agents
from tradinglab.agents.screeners import SCREENERS
from tradinglab.agents.strategies import c_breakout  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.market_data.indicators import last_closed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"C0{i}" for i in range(1, 10)]
TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
# avancées successives du broker (en barres M5) : 2 h, puis 16 h, puis 37 h → heures de barre différentes
ADVANCES = [24, 192, 444]


def _specs() -> dict:
    return {a.agent_id: a for a in default_agents() if a.agent_id in AGENT_IDS}


@pytest.fixture(scope="module")
def specs():
    return _specs()


@pytest.fixture(scope="module")
def snapshots():
    """Liste d'instants ; chaque instant = dict symbole -> MarketSnapshot."""
    from conftest import FIXED_NOW
    from tradinglab.mt5.mock_adapter import MockBroker

    broker = MockBroker(seed=7)
    broker.connect()
    broker.set_now(FIXED_NOW)
    feed = MarketDataFeed(broker, TIMEFRAMES)
    out = [feed.snapshots(broker.symbols(), now=broker.now())]
    for n in ADVANCES:
        broker.advance_bars(n)
        broker.set_now(None)
        broker.set_now(broker.now())
        out.append(feed.snapshots(broker.symbols(), now=broker.now()))
    return out


def _signature(c):
    """Champs décisionnels d'un candidat (sans id/created_at aléatoires)."""
    if c is None:
        return None
    return (c.symbol, c.side.value, round(c.entry, 10), round(c.sl, 10), tuple(round(x, 10) for x in c.tp_plan),
            c.rr, c.setup_score, c.bar_time, c.invalidation, tuple(c.arguments_for), tuple(c.arguments_against))


def _with_modified_forming_bar(snap):
    """Copie du snapshot dont la barre EN FORMATION (dernière ligne) de chaque frame est fortement modifiée."""
    frames = {}
    for tf, df in snap.frames.items():
        df2 = df.copy()
        if len(df2) >= 2:
            atr = float(last_closed(df2)["atr14"]) if "atr14" in df2.columns else 0.0
            shift = 5.0 * atr if atr and atr == atr else float(df2["close"].iloc[-1]) * 0.02
            i = df2.index[-1]
            new_close = float(df2.loc[i, "close"]) + shift
            df2.loc[i, "close"] = new_close
            df2.loc[i, "high"] = max(float(df2.loc[i, "high"]), new_close)
        frames[tf] = df2
    return dataclasses.replace(snap, frames=frames)


# ---------------------------------------------------------------- (1) enregistrement
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_agent_registered_with_own_strategy(agent_id, specs):
    assert agent_id in SCREENERS
    fn = SCREENERS[agent_id]
    assert fn.__name__ == f"strategy_{agent_id.lower()}"
    assert fn.__doc__ and agent_id in fn.__doc__
    assert specs[agent_id].strategy == agent_id


def test_module_loaded_in_strategies_package():
    import tradinglab.agents.strategies as s
    assert "c_breakout" in s.LOADED
    assert "c_breakout" not in s.FAILED


# ---------------------------------------------------------------- (2) contrat du candidat
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_candidate_contract_on_all_symbols_and_instants(agent_id, specs, snapshots):
    spec = specs[agent_id]
    fn = SCREENERS[agent_id]
    for instant in snapshots:
        for symbol, snap in instant.items():
            c = fn(spec, snap)  # ne doit jamais lever
            if c is None:
                continue
            assert isinstance(c, TradeCandidate)
            assert c.agent_id == spec.agent_id
            assert c.symbol == symbol
            chk = validate_stop_loss(c.side, c.entry, c.sl, snap.spec, atr=c.atr)
            assert chk.ok, f"{agent_id} {symbol}: {chk.reason}"
            assert c.rr >= 1.5
            assert 0 <= c.setup_score <= 100
            assert c.bar_time
            assert c.bar_time == str(last_closed(snap.frames[spec.timeframes["entry"]])["time"])
            assert c.tp_plan and len(c.tp_plan) <= 3
            # plan de TP du bon côté, croissant dans le sens du trade
            dists = [c.side.sign * (tp - c.entry) for tp in c.tp_plan]
            assert all(d > 0 for d in dists) and dists == sorted(dists)
            assert abs(c.tp_plan[-1] - c.entry) / c.sl_distance == pytest.approx(c.rr, abs=0.01)
            assert c.invalidation and c.arguments_for
            assert 0.3 * c.atr <= c.sl_distance <= 3.0 * c.atr or c.atr == 0


# ---------------------------------------------------------------- (3) anti-lookahead
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_forming_bar_does_not_change_decision(agent_id, specs, snapshots):
    spec = specs[agent_id]
    fn = SCREENERS[agent_id]
    for instant in snapshots:
        for symbol, snap in instant.items():
            before = _signature(fn(spec, snap))
            after = _signature(fn(spec, _with_modified_forming_bar(snap)))
            assert before == after, f"{agent_id} {symbol}: la barre en formation a changé la décision"


# ---------------------------------------------------------------- (4) vitalité du module
def test_module_produces_at_least_one_candidate(specs, snapshots):
    produced = {}
    for agent_id in AGENT_IDS:
        for instant in snapshots:
            for symbol, snap in instant.items():
                c = SCREENERS[agent_id](specs[agent_id], snap)
                if c is not None:
                    produced.setdefault(agent_id, []).append((symbol, c.bar_time))
    assert produced, "aucun agent de la famille C ne produit de candidat : stratégie morte"


def test_rejections_counter_is_diagnostic_only(specs, snapshots):
    """Le compteur de refus ne modifie pas les décisions : deux passes identiques donnent le même résultat."""
    snap = next(iter(snapshots[0].values()))
    for agent_id in AGENT_IDS:
        a = _signature(SCREENERS[agent_id](specs[agent_id], snap))
        b = _signature(SCREENERS[agent_id](specs[agent_id], snap))
        assert a == b
    assert all(v > 0 for v in c_breakout.REJECTIONS.values())
