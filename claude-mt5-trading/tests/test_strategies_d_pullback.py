"""Tests des stratégies propres de la famille D (replis dans la tendance) : D01 … D07.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou None),
absence de lookahead (la barre en formation n'influence pas la décision), vitalité du module (au moins un
candidat sur l'ensemble des snapshots) et, pour les agents les plus sélectifs, vitalité individuelle sur une
fenêtre de données simulées élargie.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import FIXED_NOW
from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import d_pullback  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"D{i:02d}" for i in range(1, 8)]
TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
# Agents dont la vitalité individuelle est vérifiée sur une fenêtre élargie (voir `wide_window_candidates`)
WIDE_AGENT_IDS = ["D01", "D03"]
WIDE_STEPS = 24            # instants examinés par graine (>= 20)
WIDE_STEP_BARS = 3         # 3 barres M5 = 1 barre M15 : chaque instant apporte une nouvelle barre clôturée
WIDE_SEEDS = (7, 11)


@pytest.fixture(scope="module")
def specs():
    return {a.agent_id: a for a in default_agents() if a.agent_id in AGENT_IDS}


@pytest.fixture(scope="module")
def broker_module():
    from tradinglab.mt5.mock_adapter import MockBroker

    b = MockBroker(seed=7)
    b.connect()
    b.set_now(FIXED_NOW)
    return b


@pytest.fixture(scope="module")
def snapshots(broker_module):
    """Snapshots de tous les symboles à 3 instants différents (le broker avance de 48 barres M5 entre chaque)."""
    broker = broker_module
    out = []
    for step in range(3):
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
    # les TP sont du bon côté et croissants dans le sens du trade
    prev = c.entry
    for tp in c.tp_plan:
        assert c.side.sign * (tp - c.entry) > 0
        assert c.side.sign * (tp - prev) >= 0
        prev = tp
    assert abs(c.tp_plan[-1] - c.entry) / c.sl_distance >= 1.5 - 1e-9
    assert c.invalidation
    assert isinstance(c.arguments_for, list) and c.arguments_for
    assert isinstance(c.arguments_against, list)
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
    assert total >= 1, f"aucun candidat produit par la famille D : {per_agent}"


@pytest.fixture(scope="module")
def wide_window_candidates(specs):
    """Premier candidat valide de chaque agent de `WIDE_AGENT_IDS` sur une fenêtre de données simulées élargie.

    Parcours déterministe : graines 7 puis 11, `WIDE_STEPS` instants espacés de 15 min (le broker simulé avance de
    `WIDE_STEP_BARS` barres M5, soit exactement une barre M15, entre deux instants), tous les symboles. Le pas est
    volontairement court : le broker simulé fabrique les barres ajoutées avec une graine commune à tous les
    symboles, un pas long ferait donc évoluer les 16 symboles à l'identique et appauvrirait l'échantillon.
    L'exploration s'arrête dès que chaque agent suivi a produit un candidat : les données sont déterministes,
    l'arrêt anticipé ne change donc aucun résultat, il évite seulement de recalculer des snapshots inutiles.
    """
    from datetime import datetime, timezone

    from tradinglab.mt5.mock_adapter import MockBroker

    found: dict[str, tuple] = {}
    for seed in WIDE_SEEDS:
        if len(found) == len(WIDE_AGENT_IDS):
            break
        broker = MockBroker(seed=seed, bars=9000)
        broker.connect()
        now = datetime(2026, 1, 5, tzinfo=timezone.utc)
        for _ in range(WIDE_STEPS):
            broker.set_now(now)
            feed = MarketDataFeed(broker, TIMEFRAMES)
            snaps = feed.snapshots(broker.symbols(), now=now)
            for agent_id in WIDE_AGENT_IDS:
                if agent_id in found:
                    continue
                spec = specs[agent_id]
                fn = screeners.SCREENERS[agent_id]
                for snap in snaps.values():
                    c = fn(spec, snap)
                    if c is not None:
                        found[agent_id] = (c, spec, snap, seed)
                        break
            if len(found) == len(WIDE_AGENT_IDS):
                break
            broker.advance_bars(WIDE_STEP_BARS)
            now += timedelta(minutes=15)
    return found


@pytest.mark.parametrize("agent_id", WIDE_AGENT_IDS)
def test_declenche_sur_fenetre_elargie(agent_id, wide_window_candidates):
    """Un agent qui ne signale jamais rien est inutilisable : chacun doit produire un candidat CONFORME."""
    assert agent_id in wide_window_candidates, (
        f"{agent_id} n'a produit aucun candidat sur {WIDE_STEPS} instants x {len(WIDE_SEEDS)} graines "
        f"{WIDE_SEEDS} : stratégie inerte"
    )
    c, spec, snap, _seed = wide_window_candidates[agent_id]
    # conformité complète (côté du SL, distance en ATR, TP croissants, bar_time de la barre clôturée…)
    _check_candidate(c, spec, snap)
    # et, explicitement, les trois garde-fous non négociables
    chk = validate_stop_loss(c.side, c.entry, c.sl, snap.spec, atr=c.atr)
    assert chk.ok, f"{agent_id} : SL refusé ({chk.reason})"
    assert c.rr >= 1.5, f"{agent_id} : rr {c.rr} < 1.5"
    assert 0 <= c.setup_score <= 100, f"{agent_id} : setup_score {c.setup_score} hors bornes"


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "d_pullback" in strategies.LOADED
    assert "d_pullback" not in strategies.FAILED
