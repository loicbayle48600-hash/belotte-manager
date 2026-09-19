"""Tests des stratégies propres de la famille F (structure de marché et price action) : F01 … F06.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou None),
absence de lookahead (la barre en formation n'influence pas la décision) et vitalité du module (au moins un
candidat sur l'ensemble des snapshots).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import f_structure  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import Regime, Side, SymbolSpec, TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.market_data.indicators import enrich
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"F{i:02d}" for i in range(1, 7)]
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
    """Snapshots de tous les symboles à plusieurs instants (le broker avance de 48 barres M5 entre chaque)."""
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
    # SL du bon côté de l'entrée
    assert c.side.sign * (c.entry - c.sl) > 0
    # distance entrée→SL entre 0,3 et 3 ATR du tf d'entrée
    atr_entry = float(snap.frames[spec.timeframes["entry"]].iloc[-2]["atr14"])
    assert 0.3 * atr_entry - 1e-12 <= c.sl_distance <= 3.0 * atr_entry + 1e-12
    # les TP sont du bon côté et le dernier respecte le rr annoncé
    for tp in c.tp_plan:
        assert c.side.sign * (tp - c.entry) > 0
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
    assert total >= 1, f"aucun candidat produit par la famille F : {per_agent}"


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "f_structure" in strategies.LOADED
    assert "f_structure" not in strategies.FAILED


# --------------------------------------------------------------------------------------------------------------
# Configurations synthétiques : géométries construites à la main, conformes à la thèse documentée de chaque agent.
# Elles servent à prouver qu'AUCUN agent n'est structurellement mort (le broker simulé ne produit pas forcément la
# géométrie exigée) et à verrouiller les corrections de relecture.
# --------------------------------------------------------------------------------------------------------------
SYNTH_SPEC = SymbolSpec(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
                        contract_size=100000.0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                        stops_level_points=10, trade_allowed=True, currency_base="EUR", currency_profit="USD",
                        currency_margin="EUR", asset_class="forex", spread_points=3)
SYNTH_T0 = datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)


def _bars(closes, wicks=None, tf_min=15):
    """Frame OHLC déterministe : `open` = clôture précédente, `wicks[i]` = (mèche basse, mèche haute)."""
    closes = [float(x) for x in closes]
    w = list(wicks) if wicks is not None else [0.0] * len(closes)
    w = [(x, x) if not isinstance(x, (tuple, list)) else x for x in w]
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append({"time": SYNTH_T0 + timedelta(minutes=tf_min * i), "open": o, "high": max(o, c) + w[i][1],
                     "low": min(o, c) - w[i][0], "close": c, "tick_volume": 100.0, "spread": 3})
        prev = c
    return enrich(pd.DataFrame(rows))


def _zig(points, per_leg):
    """Chemin linéaire par morceaux passant par `points`, `per_leg` barres par jambe."""
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        out.extend(a + (b - a) * k / per_leg for k in range(1, per_leg + 1))
    return out


def _trend_bars(points, e, ratio=2.0, per_leg=14):
    """Frame H1 dont l'ATR vaut `ratio` x celui du tf d'entrée.

    L'ATR H1 pilote la borne basse de la distance au SL (`SL_MIN_H1_ATR`) et la revalidation du gate : un frame
    H1 dont l'ATR serait disproportionné rendrait tout stop impossible, ce qui testerait autre chose.
    """
    path = _zig(points, per_leg)
    flat = float(_bars(path, tf_min=60)["atr14"].iloc[-2])
    wick = max(0.0, (ratio * float(e["atr14"].iloc[-2]) - flat) / 2.0)
    return _bars(path, wicks=[wick] * len(path), tf_min=60)


def _synth(e, trend_points, spread_points=3, per_leg=14):
    """Snapshot minimal offrant la même surface que `MarketSnapshot` aux screeners."""
    t = _trend_bars(trend_points, e, per_leg=per_leg)
    return SimpleNamespace(symbol="EURUSD", spec=SYNTH_SPEC, frames={"M15": e, "H1": t},
                           regime=SimpleNamespace(regime=Regime.TRENDING), session=SimpleNamespace(value="LONDON"),
                           spread_points=spread_points, atr_h1=float(t["atr14"].iloc[-2]), data_quality="OK",
                           tick=None, bar_times={}, bar_counts={})


def synth_f01():
    """HH-HL : repli de 20 % de l'impulsion, puis reprise du haut du repli en restant SOUS le sommet."""
    closes = _zig([1.0960, 1.0990, 1.0975, 1.1020, 1.1005, 1.1060, 1.1040], 12) + [
        1.1050, 1.1060, 1.1070, 1.1080, 1.1090, 1.1100,       # impulsion jusqu'au sommet
        1.1096, 1.1092, 1.1088,                               # repli -> pivot HL
        1.1090, 1.1092, 1.1093,                               # reprise, toujours sous le sommet
        1.1096,                                               # barre de signal (clôture > haut du repli)
        1.1099]                                               # barre en formation
    return _synth(_bars(closes), [1.0950, 1.1000, 1.0980, 1.1050, 1.1030, 1.1100])


def synth_f02():
    """LH-LL : creux 1.0960 cassé, retour sur le niveau 5 barres plus tard, rejet par mèche haute."""
    closes = _zig([1.1150, 1.1100, 1.1130, 1.1000, 1.1060], 12) + [
        1.1040, 1.1010, 1.0980, 1.0960,                       # creux structurel (niveau de polarité)
        1.0975, 1.0985, 1.0970,                               # rebond : le creux devient un pivot confirmé
        1.0950, 1.0940,                                       # cassure en clôture sous le niveau
        1.0948, 1.0955, 1.0962,                               # retour sur le niveau devenu résistance
        1.0945,                                               # barre de signal : rejet
        1.0942]
    wicks = [0.0] * len(closes)
    wicks[-2] = (0.0, 0.0006)                                 # mèche haute de rejet sur la barre de signal
    return _synth(_bars(closes, wicks=wicks), [1.1250, 1.1200, 1.1220, 1.1100, 1.1130, 1.1000])


def synth_f03():
    """BOS : base de 10 barres contractée sous le pivot 1.1100, puis barre de cassure impulsive."""
    closes = _zig([1.0960, 1.1000, 1.0985, 1.1040, 1.1020, 1.1100], 10) + [
        1.1096, 1.1094, 1.1093, 1.1091, 1.1090, 1.1089, 1.1088, 1.1087, 1.1086, 1.1085,   # base contractée
        1.1130,                                               # barre de signal : cassure portée par le corps
        1.1135]
    return _synth(_bars(closes), [1.0900, 1.1000, 1.0970, 1.1060, 1.1030, 1.1120])


def synth_f04():
    """CHoCH baissier : jambe finale à 58 % de la précédente, puis cassure du dernier creux confirmé."""
    closes = (_zig([1.0960, 1.1000, 1.0980, 1.1100], 14) + _zig([1.1100, 1.1050, 1.1120], 8)[1:]
              + [1.1105, 1.1090, 1.1075, 1.1060, 1.1040, 1.1035])
    return _synth(_bars(closes), [1.1200, 1.1150, 1.1170, 1.1090, 1.1110, 1.1060])


def synth_f05(far_spike=False):
    """Rejet d'une zone de deux creux superposés (1.1000 / 1.1002), confirmé par la barre suivante.

    `far_spike` place en plus un ancien sommet à 1.1250 dans la fenêtre : c'était la cible finale retenue avant
    correction, et elle fixait à elle seule le `rr` annoncé.
    """
    head = (_zig([1.0900, 1.1250, 1.0950], 12) + _zig([1.0950, 1.1000, 1.1040], 12)[1:]) if far_spike \
        else _zig([1.0900, 1.0960, 1.1000, 1.1040], 14)
    closes = head + [1.1030, 1.1015, 1.1000, 1.1010, 1.1025, 1.1040, 1.1030, 1.1014, 1.1002, 1.1016, 1.1030,
                     1.1045, 1.1060, 1.1050, 1.1035, 1.1020,
                     1.1018,                                  # barre de rejet (mèche basse dans la zone)
                     1.1030,                                  # barre de signal : clôture au-delà du rejet
                     1.1040]
    wicks = [0.0] * len(closes)
    wicks[-3] = (0.0017, 0.0003)
    return _synth(_bars(closes, wicks=wicks), [1.0850, 1.0950, 1.0920, 1.1020, 1.0990, 1.1090])


def synth_f06():
    """Structures HH-HL alignées M15 et H1, plus haute clôture de 20 barres, pivot H1 laissant la place."""
    closes = _zig([1.0900, 1.0960, 1.0940, 1.1010, 1.0990, 1.1060, 1.1040], 11) + [
        1.1050, 1.1060, 1.1070, 1.1085, 1.1100, 1.1105]
    return _synth(_bars(closes), [1.0800, 1.0950, 1.0900, 1.1100, 1.1050, 1.1300, 1.1150],
                  spread_points=2, per_leg=10)


SYNTH = {"F01": synth_f01, "F02": synth_f02, "F03": synth_f03,
         "F04": synth_f04, "F05": synth_f05, "F06": synth_f06}


def _blank_forming_bar(df):
    """Copie du frame dont TOUTES les colonnes de la barre en formation sont NaN (sauf `time`)."""
    out = df.copy()
    for col in out.columns:
        if col != "time":
            out.loc[len(out) - 1, col] = np.nan
    return out


# --------------------------------------------------------------------------------------------------------------
# Corrections issues de la relecture contradictoire
# --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_each_agent_is_reachable(agent_id, specs):
    """Aucun agent du module n'est structurellement mort : chacun produit un candidat VALIDE sur sa géométrie.

    Le broker simulé ne présente pas toutes ces géométries ; un agent qui ne se déclenche jamais sur les
    snapshots du mock n'est pas forcément mort, mais un agent qui ne se déclenche pas sur sa propre
    configuration idéale l'est.
    """
    spec = specs[agent_id]
    snap = SYNTH[agent_id]()
    c = screeners.SCREENERS[agent_id](spec, snap)
    assert c is not None, f"{agent_id} ne produit aucun candidat sur sa propre configuration idéale"
    _check_candidate(c, spec, snap)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
@pytest.mark.parametrize("col", ["open", "high", "low", "close"])
def test_nan_price_on_signal_bar_returns_none(agent_id, col, specs, snapshots):
    """Un prix NaN sur la dernière barre CLÔTURÉE interdit tout candidat.

    Correction : `_ctx` ne valide que les colonnes d'indicateurs. Toute comparaison avec NaN étant fausse, les
    filtres d'anatomie de bougie (`close <= open`, `_close_pos(...) < 0.6`, `_body_ratio(...) < 0.4`) ne
    rejetaient rien et l'agent émettait un candidat dont la confirmation n'avait jamais été vérifiée.
    """
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    cases = [SYNTH[agent_id]()] + [snap for snaps in snapshots[:2] for snap in snaps.values()]
    for snap in cases:
        saved = dict(snap.frames)
        snap.frames = {tf: df.copy() for tf, df in saved.items()}
        for df in snap.frames.values():
            if len(df) >= 2:
                df.loc[len(df) - 2, col] = np.nan
        try:
            assert fn(spec, snap) is None, f"{agent_id} : candidat avec {col}=NaN sur la barre de signal"
        finally:
            snap.frames = saved


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_lookahead_forming_bar_fully_unknown(agent_id, specs, snapshots):
    """Anti-lookahead renforcé : TOUTES les colonnes de la barre en formation deviennent NaN.

    Le test d'origine ne modifiait qu'OHLC et le volume : une lecture de `iloc[-1]` sur un indicateur
    (atr14, adx14, ema…) serait passée inaperçue.
    """
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    cases = [SYNTH[agent_id]()] + [snap for snaps in snapshots for snap in snaps.values()]
    for snap in cases:
        before = _signature(fn(spec, snap))
        saved = dict(snap.frames)
        snap.frames = {tf: _blank_forming_bar(df) for tf, df in saved.items() if len(df)}
        try:
            after = _signature(fn(spec, snap))
        finally:
            snap.frames = saved
        assert before == after, f"{agent_id}/{snap.symbol} : la barre en formation a changé la décision"


def test_f02_level_is_the_pivot_actually_broken(specs):
    """F02 : le niveau de polarité est le dernier creux CASSÉ, pas forcément `swing_lows[-1]`.

    Correction : avec `swing_lows[-1]`, `break_lookback` (15 barres) était inopérant — dès que le nouvel
    extrême creusé par la cassure est confirmé (3 barres), le niveau cassé n'est plus le dernier pivot de la
    liste — et l'agent ne voyait plus la cassure au moment même où le retour sur le niveau devient observable.
    """
    snap = synth_f02()
    c = screeners.SCREENERS["F02"](specs["F02"], snap)
    assert c is not None, "F02 ne se déclenche pas sur une inversion de polarité conforme à sa thèse"
    _check_candidate(c, specs["F02"], snap)
    ages = [int(a.split("il y a ")[1].split(" ")[0]) for a in c.arguments_for if "cassé en clôture il y a" in a]
    assert ages and ages[0] > 3, f"cassure vue seulement à {ages} barre(s) : break_lookback resterait inopérant"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_decision_independent_of_frame_index(agent_id, specs, snapshots):
    """Aucun screener ne suppose que l'index des frames vaut leur position, ni qu'il est unique.

    Correction : F02 résolvait la barre de cassure via `closed.index.get_loc(label)`. Sur un index non unique
    — celui que produit un `pd.concat` sans `ignore_index`, cf. `research/adapters._with_forming_bar` —
    `get_loc` renvoie une tranche et `int()` levait TypeError au lieu de renvoyer None.
    """
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    cases = [SYNTH[agent_id]()] + [snap for snaps in snapshots[:2] for snap in snaps.values()]
    for snap in cases:
        ref = _signature(fn(spec, snap))
        saved = dict(snap.frames)
        shifted = {}
        for tf, df in saved.items():
            d = df.copy()
            idx = list(range(1000, 1000 + len(d)))
            if len(idx) >= 2:
                idx[-1] = idx[-2]                             # index non unique (barre en formation dupliquée)
            d.index = pd.Index(idx)
            shifted[tf] = d
        snap.frames = shifted
        try:
            assert _signature(fn(spec, snap)) == ref, f"{agent_id} : décision dépendante de l'index du frame"
        finally:
            snap.frames = saved


def test_f05_targets_are_the_nearest_opposite_pivots(specs):
    """F05 : la cible finale est un pivot opposé PROCHE, jamais l'extrême de toute la fenêtre.

    Correction : le second objectif valait `max(pivots)`, c'est-à-dire l'extrême de plusieurs centaines de
    barres. Il fixait à lui seul le `rr` annoncé au gate, à la revue et au classement des candidats.
    """
    from tradinglab.market_data.indicators import swing_points

    snap = synth_f05(far_spike=True)
    c = screeners.SCREENERS["F05"](specs["F05"], snap)
    assert c is not None and c.side is Side.BUY
    _check_candidate(c, specs["F05"], snap)
    extreme = max(px for _, px in swing_points(snap.frames["M15"].iloc[:-1])[0])
    assert abs(extreme - c.entry) / c.sl_distance > 10, "la configuration doit contenir un sommet très éloigné"
    assert max(c.tp_plan) < extreme, "la cible finale ne doit pas être l'extrême de la fenêtre"
    assert c.rr <= 4.0


def test_far_structural_target_is_disclosed(specs):
    """Honnêteté : un `rr` porté par une projection structurelle lointaine est dit dans les arguments contre."""
    snap = synth_f01()
    c = screeners.SCREENERS["F01"](specs["F01"], snap)
    assert c is not None and c.rr > f_structure.FAR_TARGET_R
    assert any("cible structurelle lointaine" in a for a in c.arguments_against)
    proche = screeners.SCREENERS["F05"](specs["F05"], synth_f05())
    assert proche is not None and proche.rr <= f_structure.FAR_TARGET_R
    assert not any("cible structurelle lointaine" in a for a in proche.arguments_against)
