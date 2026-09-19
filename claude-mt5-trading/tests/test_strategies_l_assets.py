"""Tests des stratégies propres de la famille L (spécialistes par classe d'actif) : L01 … L12.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou None),
absence de lookahead (la barre en formation n'influence pas la décision) et vitalité du module (au moins un
candidat sur l'ensemble des snapshots).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import l_assets  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"L{i:02d}" for i in range(1, 13)]
TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
# Les agents de la famille L sont sélectifs (classe d'actif, fenêtre horaire de la barre clôturée, régime) :
# on balaie plusieurs graines de marché simulé et plusieurs instants de la journée pour les solliciter.
SEEDS = (3, 5, 13)      # graines retenues : elles sollicitent plusieurs agents du module
STEPS = 12


@pytest.fixture(scope="module")
def specs():
    return {a.agent_id: a for a in default_agents() if a.agent_id in AGENT_IDS}


@pytest.fixture(scope="module")
def snapshots():
    """Snapshots de tous les symboles simulés, pour plusieurs graines et plusieurs instants de la journée.

    Entre deux instants le broker avance de 30 barres M5 (2 h 30) : les barres clôturées balaient ainsi les
    sessions asiatique, européenne et américaine.
    """
    from datetime import datetime, timezone

    from tradinglab.mt5.mock_adapter import MockBroker

    out = []
    for seed in SEEDS:
        broker = MockBroker(seed=seed)
        broker.connect()
        broker.set_now(datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc))
        for step in range(STEPS):
            if step:
                broker.advance_bars(30)
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
    assert c.tp_plan == sorted(c.tp_plan, reverse=c.side.sign < 0)
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
    assert total >= 1, f"aucun candidat produit par la famille L : {per_agent}"


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "l_assets" in strategies.LOADED
    assert "l_assets" not in strategies.FAILED


# ==============================================================================================================
# Tests ajoutés par la relecture contradictoire
#
# Motif : la fixture `snapshots` ci-dessus ne fait travailler que 4 des 12 agents (le broker simulé n'expose
# aucun symbole crypto et ne place jamais la barre clôturée dans les fenêtres horaires de L02/L03/L06/L10/L11,
# ni dans les configurations de L12). `test_module_alive` (total >= 1) passait donc avec 11 agents morts, et
# `test_no_exception_and_valid_candidates` / `test_no_lookahead` étaient vides pour eux. On ajoute un scénario
# synthétique déterministe PAR AGENT, plus les batteries de robustesse, de déterminisme et d'honnêteté.
# ==============================================================================================================
import math  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tradinglab.agents.strategies import l_assets as L  # noqa: E402
from tradinglab.core.types import Provenance, Regime, Session, Side, SymbolSpec, Tick  # noqa: E402
from tradinglab.market_data.feed import MarketSnapshot  # noqa: E402
from tradinglab.market_data.indicators import enrich  # noqa: E402
from tradinglab.market_data.regime import RegimeResult  # noqa: E402

TF_MIN = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
NUM_COLS = ("open", "high", "low", "close", "tick_volume", "spread", "ema20", "ema50", "ema200", "rsi14", "macd",
            "macd_signal", "macd_hist", "atr14", "bb_mid", "bb_up", "bb_low", "bb_width", "adx14", "vol_pct",
            "mom10", "ret1")


def _spec_of(asset_class, name, digits, point):
    return SymbolSpec(name=name, digits=digits, point=point, tick_size=point, tick_value=1.0,
                      contract_size=100000.0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                      stops_level_points=0, trade_allowed=True, currency_base="EUR", currency_profit="USD",
                      currency_margin="EUR", asset_class=asset_class, root=name)


def _mk(rows, start, tf, vols=None):
    """Frame enrichie à partir de barres (open, high, low, close) ; la DERNIÈRE est la barre en formation."""
    n = len(rows)
    t = pd.date_range(start, periods=n, freq=f"{TF_MIN[tf]}min", tz="UTC")
    return enrich(pd.DataFrame({"time": t, "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                                "low": [r[2] for r in rows], "close": [r[3] for r in rows],
                                "tick_volume": list(vols) if vols is not None else [1000.0] * n,
                                "spread": [10] * n, "real_volume": [0] * n}))


def _ramp(n, start, tf, base, slope):
    """Série monotone : `_trend_of` vaut UP (slope > 0) ou DOWN (slope < 0) sur la dernière barre clôturée."""
    rows = []
    for i in range(n):
        o, c = base + slope * i, base + slope * (i + 1)
        w = abs(slope) * 0.4 + 1e-9
        rows.append((o, max(o, c) + w, min(o, c) - w, c))
    return _mk(rows, start, tf)


def _synth_snap(symbol, spec, frames, atr_h1, session, regime=Regime.TRENDING, spread_points=1):
    return MarketSnapshot(symbol=symbol, spec=spec,
                          tick=Tick(symbol, datetime(2026, 1, 20, tzinfo=timezone.utc), 1.0, 1.0), frames=frames,
                          regime=RegimeResult(regime, 0.8, {}, Provenance.CALCULATED), session=session,
                          spread_points=spread_points, atr_h1=atr_h1, data_fresh=True, data_quality="OK",
                          fetched_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
                          bar_times={tf: str(df["time"].iloc[-2]) for tf, df in frames.items()},
                          bar_counts={tf: len(df) for tf, df in frames.items()})


# ---------------------------------------------------------------- un scénario par agent
def _s_l01():
    """Or : séance de Londres, repli au contact de la VWAP ancrée à 07:00 UTC puis reprise (barre 10:00)."""
    rows = []
    for i in range(52):
        o = 1999.0 + (0.3 if i % 2 else -0.3)
        c = 1999.0 + (-0.3 if i % 2 else 0.3)
        rows.append((o, max(o, c) + 0.35, min(o, c) - 0.35, c))
    for i in range(12):
        mid = 2000.0 + (0.6 if i % 2 else -0.6)
        rows.append((mid - 0.1, mid + 0.5, mid - 0.5, mid + 0.1))
    rows[-1] = (2000.0, 2000.5, 1999.5, 2000.0)
    rows.append((2000.0, 2000.4, 1999.8, 2000.3))
    rows.append((2000.3, 2010.0, 2000.0, 2009.0))
    frames = {"M15": _mk(rows, "2026-01-19 18:00", "M15"), "H1": _ramp(300, "2026-01-05 00:00", "H1", 1980.0, 0.1)}
    return _synth_snap("XAUUSD", _spec_of("metals", "XAUUSD", 2, 0.01), frames, 2.8, Session.LONDON)


def _s_l02():
    """Or : plage asiatique 1999,0-2001,0 tenue par des mèches, fausse sortie par le haut rejetée en clôture."""
    rows = [(1999.9, 2000.4, 1999.4, 2000.1) for _ in range(48)]
    for i in range(20):
        rows.append((2000.2, 2001.0, 2000.0, 2000.4) if i % 2 else (2000.4, 2000.6, 1999.0, 2000.2))
    rows.append((2000.6, 2001.3, 2000.4, 2000.7))
    rows.append((2000.7, 2005.0, 2000.0, 2004.0))
    frames = {"M15": _mk(rows, "2026-01-19 12:00", "M15"), "H1": _ramp(300, "2026-01-05 00:00", "H1", 1990.0, 0.05)}
    return _synth_snap("XAUUSD", _spec_of("metals", "XAUUSD", 2, 0.01), frames, 2.5, Session.ASIA, Regime.RANGING)


def _s_l03():
    """Indice : canal de régression haussier très linéaire, repli à ~-0,6 sigma puis reprise (barre 20:00)."""
    n, rows = 100, []
    for i in range(n):
        c = 4500.0 + 0.5 * i + math.sin(i * 0.7)
        rows.append((c - 0.3, max(c - 0.3, c) + 0.6, min(c - 0.3, c) - 0.6, c))
    sig = n - 2
    ls = 4500.0 + 0.5 * sig
    rows[sig - 1] = (ls - 1.5, ls - 0.3, ls - 2.3, ls - 1.7)
    rows[sig] = (ls - 1.2, ls - 0.2, ls - 1.5, ls - 0.55)
    rows[n - 1] = (ls, ls + 8, ls - 8, ls + 7)
    start = pd.Timestamp("2026-01-20 20:00", tz="UTC") - pd.Timedelta(minutes=15 * sig)
    frames = {"M15": _mk(rows, start, "M15"), "H1": _ramp(300, "2026-01-05 00:00", "H1", 4400.0, 0.4)}
    return _synth_snap("US500", _spec_of("indices", "US500", 1, 0.1), frames, 4.0, Session.NEWYORK)


def _s_l04():
    """Indice : POC bâti par 74 barres à fort volume, repli dans la zone de valeur défendu au volume."""
    rows, vols = [], []
    for i in range(74):
        mid = 4500.0 + (1.2 if i % 2 else -1.2)
        rows.append((mid - 0.2, mid + 0.6, mid - 0.6, mid + 0.2))
        vols.append(3000.0)
    for c in (4502.2, 4502.6, 4503.0):
        rows.append((c - 0.4, c + 0.3, c - 0.6, c))
        vols.append(800.0)
    rows.append((4502.2, 4502.6, 4499.5, 4502.4))
    vols.append(6000.0)
    rows.append((4502.4, 4512.0, 4495.0, 4510.0))
    vols.append(9000.0)
    frames = {"M15": _mk(rows, "2026-01-20 04:00", "M15", vols=vols),
              "H1": _ramp(300, "2026-01-05 00:00", "H1", 4400.0, 0.4)}
    return _synth_snap("US500", _spec_of("indices", "US500", 1, 0.1), frames, 3.0, Session.NEWYORK)


def _s_l05():
    """Indice : plus haut de la veille (4505) balayé puis réintégré, retour vers la médiane du range D1."""
    d1 = [(4480.0 + i * 2, 4480.0 + i * 2 + 12, 4480.0 + i * 2 - 12, 4480.0 + i * 2 + 5) for i in range(6)]
    d1[-2] = (4490.0, 4505.0, 4480.0, 4495.0)
    rows = []
    for i in range(74):
        mid = 4496.0 + (0.5 if i % 2 else -0.5)
        rows.append((mid - 0.2, mid + 1.0, mid - 1.0, mid + 0.2))
    rows += [(4497.0, 4500.0, 4496.5, 4499.5), (4499.5, 4506.5, 4499.0, 4503.0),
             (4503.0, 4504.0, 4500.5, 4501.0), (4501.0, 4502.0, 4499.0, 4499.5),
             (4499.5, 4520.0, 4490.0, 4518.0)]
    start = pd.Timestamp("2026-01-20 14:00", tz="UTC") - pd.Timedelta(minutes=15 * (len(rows) - 2))
    frames = {"M15": _mk(rows, start, "M15"), "H1": _ramp(300, "2026-01-05 00:00", "H1", 4400.0, 0.4),
              "D1": _mk(d1, "2026-01-15 00:00", "D1")}
    return _synth_snap("US500", _spec_of("indices", "US500", 1, 0.1), frames, 6.0, Session.NEWYORK, Regime.RANGING)


def _s_l06():
    """Énergie : EMA50 M15 en déplacement régulier (~0,08 ATR/barre), clôture proche de l'EMA20."""
    rows = []
    for i in range(140):
        c = 75.0 + 0.012 * i
        rows.append((c - 0.012, c + 0.07, c - 0.082, c))
    rows[-1] = (76.7, 78.0, 76.6, 77.8)
    frames = {"M15": _mk(rows, "2026-01-20 06:00", "M15"), "H1": _ramp(300, "2026-01-05 00:00", "H1", 70.0, 0.02)}
    return _synth_snap("USOIL", _spec_of("energies", "USOIL", 2, 0.01), frames, 0.25, Session.LONDON)


def _s_l07():
    """Crypto : première clôture H4 au-dessus du canal de Donchian 20 après consolidation haussière."""
    rows = []
    for i in range(70):
        c = 60.0 + 0.5 * i
        rows.append((c - 0.5, c + 0.5, c - 1.0, c))
    for k in range(38):
        c = 95.0 + 0.35 * k + (1.0 if k % 2 else -1.0)
        o = c - (0.5 if k % 2 else -0.5)
        rows.append((min(o, c) - 0.2, max(o, c) + 0.5, min(o, c) - 0.5, c))
    hi = max(r[1] for r in rows[-20:])
    rows.append((hi - 0.5, hi + 3.0, hi - 1.0, hi + 2.5))
    rows.append((hi + 2.5, hi + 9, hi + 1, hi + 8))
    e = _mk(rows, "2026-01-01 00:00", "H4")
    frames = {"H4": e, "D1": _ramp(120, "2025-10-01 00:00", "D1", 40.0, 0.6)}
    return _synth_snap("BTCUSD", _spec_of("crypto", "BTCUSD", 2, 0.01), frames,
                       float(e["atr14"].iloc[-2]) / 2, Session.LONDON)


def _s_l08():
    """Crypto : choc de rendement (cascade de liquidations) puis barre de stabilisation sans nouvel extrême."""
    rng = np.random.default_rng(7)
    rows, p = [], 50000.0
    for _ in range(117):
        o, p = p, p + rng.normal(0, 20.0)
        rows.append((o, max(o, p) + 15, min(o, p) - 15, p))
    sc = p - 700.0
    rows.append((p, p + 10, sc - 30, sc))
    rows.append((sc, sc + 470, sc - 25, sc + 450))
    rows.append((sc + 450, sc + 900, sc + 400, sc + 800))
    e = _mk(rows, "2026-01-20 00:00", "M15")
    frames = {"M15": e, "H1": _ramp(300, "2026-01-05 00:00", "H1", 49000.0, 5.0)}
    return _synth_snap("BTCUSD", _spec_of("crypto", "BTCUSD", 2, 0.01), frames,
                       float(e["atr14"].iloc[-2]) * 2, Session.LONDON, Regime.RANGING)


def _s_l09():
    """Croisé JPY : reconquête du chiffre rond 160,00 tenue sur la barre, en tendance H1 haussière."""
    n, rows = 90, []
    for i in range(n - 2):
        c = 159.95 - 0.025 * (n - 3 - i)
        rows.append((c - 0.025, c + 0.09, c - 0.115, c))
    rows.append((160.00, 160.10, 159.995, 160.06))
    rows.append((160.06, 160.9, 160.0, 160.8))
    start = pd.Timestamp("2026-01-20 06:00", tz="UTC") - pd.Timedelta(minutes=15 * (n - 2))
    e = _mk(rows, start, "M15")
    frames = {"M15": e, "H1": _ramp(300, "2026-01-05 00:00", "H1", 150.0, 0.03)}
    return _synth_snap("EURJPY", _spec_of("forex", "EURJPY", 3, 0.001), frames,
                       float(e["atr14"].iloc[-2]) * 2, Session.ASIA)


def _s_l10():
    """Croisé EUR : journée étirée à la baisse (97 % d'un seul côté), RSI épuisé puis reprise vers l'ouverture."""
    d1 = [(0.8500 + i * 0.0005, 0.8500 + i * 0.0005 + 0.0030, 0.8500 + i * 0.0005 - 0.0030,
           0.8500 + i * 0.0005 + 0.0010) for i in range(7)]
    rows = [(0.85598, 0.85608, 0.85592, 0.85602) for _ in range(24)]
    px = 0.85600
    for _ in range(40):
        o = px
        px = px - 0.00015
        rows.append((o, o + 0.00005, px - 0.00005, px))
    for _ in range(3):
        rows.append((px, px + 0.00010, px - 0.00005, px + 0.00005))
        px = px + 0.00005
    prev = rows[-1][3]
    rows.append((prev, prev + 0.00030, prev - 0.00003, prev + 0.00025))
    rows.append((prev + 0.00025, prev + 0.0012, prev + 0.0002, prev + 0.0010))
    e = _mk(rows, "2026-01-19 18:00", "M15")
    frames = {"M15": e, "H1": _mk([(0.8550, 0.8553, 0.8547, 0.8550)] * 300, "2026-01-05 00:00", "H1"),
              "D1": _mk(d1, "2026-01-14 00:00", "D1")}
    return _synth_snap("EURGBP", _spec_of("forex", "EURGBP", 5, 1e-5), frames,
                       float(e["atr14"].iloc[-2]) * 2, Session.LONDON, Regime.RANGING)


def _s_l11():
    """Majeure : escalier de Londres — 6 barres M15 à plus bas strictement croissants, en tendance H1."""
    rows = []
    for i in range(82):
        c = 1.09600 + 0.00005 * i
        rows.append((c - 0.00005, c + 0.00012, c - 0.00017, c))
    px = rows[-1][3]
    for k in range(6):
        o, c = px, px + 0.00018
        rows.append((o, c + 0.00004, o - 0.00002 + k * 0.00001, c))
        px = c
    rows.append((px, px + 0.0006, px - 0.0002, px + 0.0005))
    start = pd.Timestamp("2026-01-20 12:00", tz="UTC") - pd.Timedelta(minutes=15 * (len(rows) - 2))
    e = _mk(rows, start, "M15")
    frames = {"M15": e, "H1": _ramp(300, "2026-01-05 00:00", "H1", 1.0900, 0.00005)}
    return _synth_snap("EURUSD", _spec_of("forex", "EURUSD", 5, 1e-5), frames,
                       float(e["atr14"].iloc[-2]) * 2, Session.LONDON)


def _s_l12():
    """Mineure : jambe D1 haussière puis compression de l'ATR H4, reprise sur plus haute clôture 5 barres."""
    rows = []
    for i in range(70):
        c = 150.0 + 0.30 * i
        rows.append((c - 0.30, c + 0.30, c - 0.60, c))
    px = rows[-1][3]
    for k in range(48):
        c = px + 0.20 * math.sin(k * 0.65)
        rows.append((c - 0.01, max(c - 0.01, c) + 0.05, min(c - 0.01, c) - 0.05, c))
    top = max(r[3] for r in rows[-5:])
    rows.append((top - 0.02, top + 0.10, top - 0.06, top + 0.08))
    rows.append((top + 0.08, top + 0.98, top - 0.02, top + 0.88))
    e = _mk(rows, "2026-01-01 00:00", "H4")
    frames = {"H4": e, "D1": _ramp(150, "2025-09-01 00:00", "D1", 120.0, 0.25)}
    return _synth_snap("EURJPY", _spec_of("forex", "EURJPY", 3, 0.001), frames,
                       float(e["atr14"].iloc[-2]), Session.LONDON)


SCENARIOS = {"L01": _s_l01, "L02": _s_l02, "L03": _s_l03, "L04": _s_l04, "L05": _s_l05, "L06": _s_l06,
             "L07": _s_l07, "L08": _s_l08, "L09": _s_l09, "L10": _s_l10, "L11": _s_l11, "L12": _s_l12}


@pytest.fixture(scope="module")
def scenario_snaps():
    """Un snapshot synthétique par agent (reconstruit à la demande : les tests ne partagent aucun état)."""
    return {agent_id: build() for agent_id, build in SCENARIOS.items()}


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_chaque_agent_produit_un_candidat(agent_id, specs, scenario_snaps):
    """Aucun agent du module n'est mort : chacun produit un candidat VALIDE sur son scénario nominal.

    Régression : L02 ne pouvait produire aucun candidat, la plage asiatique étant mesurée sur une fenêtre qui
    INCLUAIT la barre de signal (`up_poke`/`dn_poke` négatifs ou nuls par construction). `test_module_alive`
    (total >= 1) ne pouvait pas le détecter.
    """
    spec = specs[agent_id]
    snap = scenario_snaps[agent_id]
    c = screeners.SCREENERS[agent_id](spec, snap)
    assert c is not None, f"{agent_id} ne produit aucun candidat sur son scénario nominal"
    _check_candidate(c, spec, snap)


def test_l02_plage_mesuree_hors_barre_de_signal(specs):
    """L02 : la plage asiatique exclut la barre de signal, sinon aucun dépassement ne serait mesurable.

    On vérifie la propriété directement : les bornes retenues par l'agent doivent être celles des barres qui
    PRÉCÈDENT la barre de signal, et la barre de signal doit les dépasser (c'est la fausse sortie).
    """
    snap = _s_l02()
    c = screeners.SCREENERS["L02"](specs["L02"], snap)
    assert c is not None
    closed = snap.frames["M15"].iloc[:-1]
    asia = L._bars_between(closed, 0.0, 8.0)
    prior = asia.iloc[:-1]
    le = closed.iloc[-1]
    assert len(prior) >= 8
    # la barre de signal dépasse strictement la borne haute des barres qui la précèdent
    assert float(le["high"]) > float(prior["high"].max())
    # ... mais clôture à l'intérieur : c'est bien une fausse sortie vendue
    assert c.side is Side.SELL and float(le["close"]) < float(prior["high"].max())
    # si la plage incluait la barre de signal, le dépassement mesuré serait nul ou négatif
    assert float(le["high"]) - float(asia["high"].max()) <= 0


def test_day_bars_horodatage_illisible():
    """`_day_bars` renvoie une fenêtre vide (jamais une exception) sur des horodatages NaT ou absents.

    Régression : `t.iloc[-1].normalize()` levait `AttributeError: 'NaTType' object has no attribute 'normalize'`,
    et L10 (seul agent sans filtre horaire préalable) remontait l'exception au lieu de refuser proprement.
    """
    df = pd.DataFrame({"time": [pd.NaT] * 5, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    assert L._day_bars(df).empty
    assert L._bars_between(df, 0.0, 8.0).empty
    assert L._day_bars(pd.DataFrame({"open": [1.0]})).empty
    assert L._day_bars(pd.DataFrame()).empty
    ok = pd.DataFrame({"time": pd.date_range("2026-01-19 22:00", periods=8, freq="1h", tz="UTC")})
    assert len(L._day_bars(ok)) == 6                       # seules les barres du jour de la dernière barre


def _copy_snap(snap, **over):
    kw = dict(symbol=snap.symbol, spec=snap.spec, tick=snap.tick,
              frames={tf: df.copy() for tf, df in snap.frames.items()}, regime=snap.regime, session=snap.session,
              spread_points=snap.spread_points, atr_h1=snap.atr_h1, data_fresh=snap.data_fresh,
              data_quality=snap.data_quality, fetched_at=snap.fetched_at, bar_times=dict(snap.bar_times),
              bar_counts=dict(snap.bar_counts))
    kw.update(over)
    return MarketSnapshot(**kw)


def _corrupt_forming_bar(snap):
    """Corrompt TOUTES les colonnes (et l'horodatage) de la barre en formation de chaque frame."""
    s = _copy_snap(snap)
    for df in s.frames.values():
        if len(df) < 2:
            continue
        last = df.index[-1]
        for col in NUM_COLS:
            if col in df.columns:
                df.loc[last, col] = float(df[col].iloc[-2]) * 7.0 + 13.0
        df.loc[last, "time"] = pd.Timestamp(df["time"].iloc[-2]) + pd.Timedelta(days=3)
    return s


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_lookahead_toutes_colonnes(agent_id, specs, snapshots, scenario_snaps):
    """Version renforcée du test d'origine : toutes les colonnes de la barre en formation sont corrompues.

    Le test d'origine ne modifiait que close/high/low/tick_volume, et uniquement sur des snapshots où 8 agents
    sur 12 renvoyaient `None` avant toute lecture de frame : une lecture de `atr14`, `rsi14`, `bb_*`, `mom10`,
    `ret1`, `ema50` ou `time` sur la dernière ligne serait passée inaperçue. On le rejoue ici sur les snapshots
    du broker simulé ET sur le scénario nominal de l'agent, où il produit réellement un candidat.
    """
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    # 6 des 36 groupes de snapshots suffisent (corrompre toutes les colonnes de 5 frames x 16 symboles x 36
    # groupes coûte plusieurs minutes) ; le scénario nominal, lui, est TOUJOURS inclus : c'est le seul cas où
    # l'agent va jusqu'au bout de sa logique.
    pool = [snap for snaps in snapshots[:6] for snap in snaps.values()] + [scenario_snaps[agent_id]]
    for snap in pool:
        before = _signature(fn(spec, snap))
        after = _signature(fn(spec, _corrupt_forming_bar(snap)))
        assert after == before, f"{agent_id}/{snap.symbol} : la barre en formation est lue quelque part"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_deterministe(agent_id, specs, scenario_snaps, snapshots):
    """Deux appels sur le même snapshot donnent exactement la même décision (aucune source d'aléa)."""
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    for snap in [scenario_snaps[agent_id]] + [s for snaps in snapshots[:2] for s in snaps.values()]:
        assert _signature(fn(spec, snap)) == _signature(fn(spec, snap))


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_scenario_reproductible(agent_id, specs):
    """Le scénario lui-même est déterministe : deux constructions donnent le même candidat."""
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    assert _signature(fn(spec, SCENARIOS[agent_id]())) == _signature(fn(spec, SCENARIOS[agent_id]()))


def _degrade(snap, kind):
    s = _copy_snap(snap)
    if kind == "tout_nan":
        for tf, df in list(s.frames.items()):
            d2 = df.copy()
            for c in d2.columns:
                if c != "time":
                    d2[c] = np.nan
            s.frames[tf] = d2
    elif kind == "prix_plats":
        for df in s.frames.values():
            for c in ("open", "high", "low", "close"):
                df[c] = 1.0
    elif kind == "sans_volume":
        for tf, df in list(s.frames.items()):
            s.frames[tf] = df.drop(columns=["tick_volume"])
    elif kind == "volume_nul":
        for df in s.frames.values():
            df["tick_volume"] = 0.0
    elif kind == "frame_61":
        for tf, df in list(s.frames.items()):
            s.frames[tf] = df.tail(61).reset_index(drop=True)
    elif kind == "frame_2":
        for tf, df in list(s.frames.items()):
            s.frames[tf] = df.tail(2).reset_index(drop=True)
    elif kind == "frame_vide":
        for tf, df in list(s.frames.items()):
            s.frames[tf] = df.iloc[0:0]
    elif kind == "temps_naif":
        for df in s.frames.values():
            df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
    elif kind == "temps_nat":
        for df in s.frames.values():
            df["time"] = pd.NaT
    elif kind == "temps_identiques":
        for df in s.frames.values():
            df["time"] = df["time"].iloc[0]
    elif kind == "atr14_nan":
        for df in s.frames.values():
            df["atr14"] = np.nan
    elif kind == "high_low_inverses":
        for df in s.frames.values():
            df["high"], df["low"] = df["low"].copy(), df["high"].copy()
    elif kind == "atr_h1_zero":
        s.atr_h1 = 0.0
    elif kind == "atr_h1_nan":
        s.atr_h1 = float("nan")
    elif kind == "atr_h1_enorme":
        s.atr_h1 = 1e6
    elif kind == "sans_spec":
        s.spec = None
    elif kind == "sans_d1":
        s.frames.pop("D1", None)
    elif kind == "d1_court":
        if "D1" in s.frames:
            s.frames["D1"] = s.frames["D1"].tail(3).reset_index(drop=True)
    elif kind == "colonnes_absentes":
        drop = ("mom10", "bb_mid", "bb_up", "bb_low", "vol_pct", "ret1")
        for tf, df in list(s.frames.items()):
            s.frames[tf] = df.drop(columns=[c for c in drop if c in df.columns])
    return s


DEGRADATIONS = ["tout_nan", "prix_plats", "sans_volume", "volume_nul", "frame_61", "frame_2", "frame_vide",
                "temps_naif", "temps_nat", "temps_identiques", "atr14_nan", "high_low_inverses", "atr_h1_zero",
                "atr_h1_nan", "atr_h1_enorme", "sans_spec", "sans_d1", "d1_court", "colonnes_absentes"]


@pytest.mark.parametrize("kind", DEGRADATIONS)
def test_frames_degradees(kind, specs, snapshots, scenario_snaps):
    """NaN, frames courtes/vides, colonnes absentes, division par zéro, horodatages illisibles → None ou candidat
    valide, jamais d'exception. Les scénarios nominaux sont inclus : ce sont les seuls cas où les 12 agents vont
    au bout de leur logique, donc les seuls où une dégradation peut réellement les faire échouer."""
    pool = list(scenario_snaps.values()) + list(snapshots[0].values())[:4]
    for snap in pool:
        s = _degrade(snap, kind)
        for agent_id in AGENT_IDS:
            c = screeners.SCREENERS[agent_id](specs[agent_id], s)
            if c is not None:      # un candidat reste possible (dégradation inoffensive) mais doit rester valide
                assert c.side.sign * (c.entry - c.sl) > 0
                assert np.isfinite(c.sl) and c.sl_distance > 0
                assert c.rr >= 1.5
                assert 0 <= c.setup_score <= 100


# ---------------------------------------------------------------- honnêteté du SL réellement proposé
def test_sl_note_helper():
    """`_sl_note` dit explicitement que le SL proposé n'est plus le niveau décrit par la thèse."""
    n = L._sl_note(Side.BUY, 100.0, 90.0, 97.0, 1.0)        # SL resserré au plafond de l'agent
    assert n and n.startswith("SL ramené")
    n = L._sl_note(Side.BUY, 100.0, 99.9, 99.0, 1.0)        # SL élargi au plancher de distance
    assert n and n.startswith("SL élargi")
    assert L._sl_note(Side.BUY, 100.0, 99.0, 99.0, 1.0) is None
    assert L._sl_note(Side.BUY, 100.0, float("nan"), 99.0, 1.0) is None
    assert L._sl_note(Side.BUY, 100.0, 99.0, 99.0, 0.0) is None


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_sl_deplace_est_signale(agent_id, specs, scenario_snaps):
    """Quand `_bound_sl` déplace le SL par rapport au niveau de la thèse, le candidat le DIT.

    Sans cette mention, `arguments_for` et `invalidation` continuaient d'annoncer un niveau (borne de zone de
    valeur, extrême du balayage, canal de sortie, départ d'escalier…) qui n'est plus celui qui est protégé : la
    sortie au stop peut alors précéder l'invalidation décrite. On force ici le plancher de distance (0,3 ATR H1)
    au-delà du SL voulu et on vérifie que la mention apparaît et que la distance vaut bien ce plancher.
    """
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    snap = scenario_snaps[agent_id]
    base = fn(spec, snap)
    assert base is not None
    atr_entry = float(snap.frames[spec.timeframes["entry"]].iloc[-2]["atr14"])
    plafond = min(float(spec.params.get("sl_atr", 1.5)), L.SL_MAX_ATR) * atr_entry
    if snap.atr_h1 and snap.atr_h1 > 0:                      # le plafond en ATR H1 peut être le plus contraignant
        plafond = min(plafond, L.SL_MAX_H1_ATR * float(snap.atr_h1))
    cible = min(base.sl_distance * 1.2, plafond)            # un peu plus large, sans dépasser le plafond
    if cible <= base.sl_distance * (1.0 + 1e-9):
        return      # l'agent est déjà à son plafond de distance : impossible d'élargir (cas couvert par le test
        #             `test_sl_note_present_quand_le_plafond_mord`, qui vérifie la mention « SL ramené »)
    forced = _copy_snap(snap, atr_h1=cible / L.SL_MIN_H1_ATR)   # plancher de distance = `cible`
    c = fn(spec, forced)
    if c is None:                                           # refus déterministe du gate de SL : acceptable
        return
    assert c.sl_distance == pytest.approx(cible, rel=1e-9)
    assert any(x.startswith("SL élargi") for x in c.arguments_against), c.arguments_against


def test_sl_note_present_quand_le_plafond_mord(specs, scenario_snaps):
    """Au moins un scénario nominal a son SL ramené au plafond : la mention doit alors être présente."""
    vus = 0
    for agent_id in AGENT_IDS:
        c = screeners.SCREENERS[agent_id](specs[agent_id], scenario_snaps[agent_id])
        assert c is not None
        if any(x.startswith("SL ramené") for x in c.arguments_against):
            vus += 1
    assert vus >= 1, "aucun scénario ne déclenche la mention de SL ramené : le test ne prouve rien"


# ---------------------------------------------------------------- distinction réelle entre moteurs
@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_distinct_du_screener_generique(agent_id, specs, scenario_snaps):
    """La stratégie propre ne redonne jamais la décision du screener générique de repli (`base_strategy`)."""
    spec = specs[agent_id]
    snap = scenario_snaps[agent_id]

    def sig(c):
        return None if c is None else (c.side, round(c.entry, 10), round(c.sl, 10),
                                       tuple(round(x, 10) for x in c.tp_plan))

    own = sig(screeners.SCREENERS[agent_id](spec, snap))
    base = sig(screeners.SCREENERS[spec.base_strategy](spec, snap))
    assert own is not None
    assert own != base, f"{agent_id} reproduit son screener de repli {spec.base_strategy}"


def test_agents_non_redondants(specs, scenario_snaps):
    """Sur un même snapshot, deux agents du module ne proposent jamais exactement le même trade."""
    for agent_id, snap in scenario_snaps.items():
        vus = {}
        for other in AGENT_IDS:
            c = screeners.SCREENERS[other](specs[other], snap)
            if c is None:
                continue
            key = (c.side, round(c.entry, 10), round(c.sl, 10), tuple(round(x, 10) for x in c.tp_plan))
            vus.setdefault(key, []).append(other)
        for key, agents in vus.items():
            assert len(agents) == 1, f"{agents} produisent un trade identique sur le scénario {agent_id}"


# ---------------------------------------------------------------- utilitaires locaux
def test_bound_sl_cote_et_bornes():
    """`_bound_sl` ne change jamais le côté du SL, respecte les bornes et refuse une configuration incohérente."""
    class _Fake:
        def __init__(self, atr_h1):
            self.atr_h1 = atr_h1

    snap = _Fake(0.0)
    assert L._bound_sl(snap, Side.BUY, 100.0, 80.0, 1.0, 0.5, 3.0) == pytest.approx(97.0)     # plafond 3 ATR
    assert L._bound_sl(snap, Side.SELL, 100.0, 100.05, 1.0, 0.5, 3.0) == pytest.approx(100.5)  # plancher 0,5 ATR
    assert L._bound_sl(_Fake(50.0), Side.BUY, 100.0, 99.0, 1.0, 0.5, 3.0) is None              # 0,3 ATR H1 > 3 ATR
    assert L._bound_sl(snap, Side.BUY, 100.0, float("nan"), 1.0, 0.5, 3.0) is None
    assert L._bound_sl(snap, Side.BUY, 100.0, 99.0, 0.0, 0.5, 3.0) is None


def _fake_candidate(side, entry, sl):
    return TradeCandidate(symbol="XAUUSD", side=side, entry=entry, sl=sl, tp_plan=[], timeframes=["M15", "H1"],
                          regime=Regime.TRENDING, agent_id="L01")


def test_set_tp_plan():
    """Plan de TP : rr >= 1,5, au plus 3 niveaux croissants dans le sens du trade, SL nul refusé, cible plafonnée."""
    c = L._set_tp_plan(_fake_candidate(Side.BUY, 100.0, 99.0), Side.BUY, 100.0,
                       [103.0, 104.0, float("nan"), 99.5], 2.0)
    assert c is not None and len(c.tp_plan) <= 3 and c.tp_plan == sorted(c.tp_plan)
    assert all(x > 100.0 for x in c.tp_plan) and c.rr == pytest.approx(4.0)
    # cible structurelle délirante → plafonnée à MAX_STRUCT_RR
    c = L._set_tp_plan(_fake_candidate(Side.BUY, 100.0, 99.0), Side.BUY, 100.0, [1e9], 2.0)
    assert c is not None and c.rr == pytest.approx(L.MAX_STRUCT_RR)
    s = L._set_tp_plan(_fake_candidate(Side.SELL, 100.0, 101.0), Side.SELL, 100.0, [], 1.0)
    assert s is not None and s.rr >= 1.5 and all(x < 100.0 for x in s.tp_plan)
    assert s.tp_plan == sorted(s.tp_plan, reverse=True)
    assert L._set_tp_plan(_fake_candidate(Side.BUY, 100.0, 100.0), Side.BUY, 100.0, [101.0], 2.0) is None
    assert L._set_tp_plan(None, Side.BUY, 100.0, [101.0], 2.0) is None


def test_utilitaires_locaux():
    """Utilitaires : aucune division par zéro, aucune valeur inventée sur des données inexploitables."""
    plat = pd.Series({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    assert L._range(plat) == 0.0
    assert L._body_ratio(plat) == 0.0
    assert L._close_pos(plat, Side.BUY) == 0.5
    assert L._wick_against(plat, Side.SELL) == 0.0
    assert L._bar_hour(pd.Series({"time": pd.NaT})) is None
    assert L._bar_hour(pd.Series({"close": 1.0})) is None
    assert L._bar_hour(pd.Series({"time": pd.Timestamp("2026-01-20 07:30", tz="UTC")})) == pytest.approx(7.5)
    assert L._figure_step(0.001) == pytest.approx(0.5)
    assert L._figure_step(0.0) is None and L._figure_step(float("nan")) is None
    vide = pd.DataFrame({"open": [], "high": [], "low": [], "close": []})
    assert L._ohlc_ok(vide) is False and L._ohlc_ok(None) is False
    assert L._vwap_sigma(vide) is None
    assert L._volume_profile(vide) is None
    assert L._reg_channel(np.arange(5, dtype=float)) is None          # moins de 10 points
    assert L._reg_channel(np.ones(30)) is None                        # dispersion nulle
    reg = L._reg_channel(np.arange(30, dtype=float) + np.sin(np.arange(30)))
    assert reg is not None and reg[0] > 0 and reg[2] > 0 and 0.0 <= reg[3] <= 1.0
    bars = pd.DataFrame({"high": [2.0, 3.0, 4.0], "low": [1.0, 2.0, 3.0], "close": [1.5, 2.5, 3.5],
                         "open": [1.2, 2.2, 3.2], "tick_volume": [0.0, 0.0, 0.0]})
    assert L._volumes(bars) is None                                   # volume entièrement nul
    assert L._vwap_sigma(bars) is not None                            # repli sur des poids égaux
    assert L._volumes(bars.drop(columns=["tick_volume"])) is None


def test_streak_et_market_ok():
    """`_streak` compte l'escalier qui se termine sur la barre de signal ; `_market_ok` respecte `spec.markets`."""
    up = pd.DataFrame({"low": [1.0, 2.0, 3.0, 4.0, 5.0], "high": [9.0, 8.0, 7.0, 6.0, 5.0]})
    assert L._streak(up, Side.BUY) == 4
    assert L._streak(up, Side.SELL) == 4
    rompu = pd.DataFrame({"low": [1.0, 2.0, 3.0, 4.0, 3.5], "high": [9.0, 8.0, 7.0, 6.0, 6.5]})
    assert L._streak(rompu, Side.BUY) == 0
    assert L._streak(pd.DataFrame({"low": [np.nan, 1.0], "high": [1.0, np.nan]}), Side.BUY) == 0

    class _S:
        def __init__(self, sym, cls):
            self.symbol, self.spec = sym, type("sp", (), {"asset_class": cls})()

    from tradinglab.agents.registry import AgentSpec as _A
    metals = [a for a in default_agents() if a.agent_id == "L01"][0]
    assert L._market_ok(metals, _S("XAUUSD.m", "metals")) is True
    assert L._market_ok(metals, _S("EURUSD", "forex")) is False
    majors = [a for a in default_agents() if a.agent_id == "L11"][0]
    assert L._market_ok(majors, _S("EURUSD.m", "forex")) is True      # groupe forex_majors
    assert L._market_ok(majors, _S("EURGBP", "forex")) is False
    assert isinstance(majors, _A)
    snap_sans_spec = type("x", (), {"spec": None, "symbol": "EURUSD"})()
    assert L._market_ok(majors, snap_sans_spec) is False


def test_bound_sl_plafond_atr_h1():
    """`_bound_sl` plafonne aussi la distance à 3,9 ATR H1 : sans cela, `_finalize` refuserait en silence.

    Régression : sur un tf d'entrée H4 (L07, L12), l'ATR du tf vaut ~2 x l'ATR H1. Un stop de 2 ATR H4 dépasse
    donc le plafond du gate (4 x ATR H1) dès que la volatilité H4 est un peu plus du double de celle de H1,
    c'est-à-dire exactement en tendance. Le candidat était construit puis jeté par `validate_stop_loss`.
    """
    class _Fake:
        def __init__(self, atr_h1):
            self.atr_h1 = atr_h1

    # ATR tf d'entrée = 2, ATR H1 = 0,8 → plafond du gate = 3,2 ; sans plafond H1 on proposerait 2 x 2 = 4
    sl = L._bound_sl(_Fake(0.8), Side.BUY, 100.0, 90.0, 2.0, 0.8, 2.0)
    assert sl is not None
    assert 100.0 - sl == pytest.approx(L.SL_MAX_H1_ATR * 0.8)
    from tradinglab.risk.stop_loss import validate_stop_loss as _v
    spec = _spec_of("crypto", "BTCUSD", 2, 0.01)
    assert _v(Side.BUY, 100.0, sl, spec, atr=0.8).ok        # accepté par le gate (référence = ATR H1)
    # ATR H1 inconnu (0) : le plafond H1 ne s'applique pas, la borne du tf d'entrée reste seule
    assert L._bound_sl(_Fake(0.0), Side.BUY, 100.0, 90.0, 2.0, 0.8, 2.0) == pytest.approx(96.0)
    # ATR H1 NaN : traité comme inconnu, jamais propagé en NaN dans le SL
    sl_nan = L._bound_sl(_Fake(float("nan")), Side.BUY, 100.0, 90.0, 2.0, 0.8, 2.0)
    assert sl_nan is not None and np.isfinite(sl_nan)


@pytest.mark.parametrize("agent_id", ["L07", "L12"])
def test_agents_h4_survivent_a_un_atr_h1_bas(agent_id, specs, scenario_snaps):
    """L07 / L12 (tf d'entrée H4) produisent encore un candidat quand l'ATR H1 vaut 1/3 de l'ATR H4.

    C'est la configuration normale d'un marché en tendance ; avant le plafond `SL_MAX_H1_ATR`, ces deux agents
    y étaient systématiquement muets (refus silencieux de `_finalize`).
    """
    spec = specs[agent_id]
    snap = scenario_snaps[agent_id]
    atr_entry = float(snap.frames[spec.timeframes["entry"]].iloc[-2]["atr14"])
    for diviseur in (2.2, 2.5, 3.0):
        forced = _copy_snap(snap, atr_h1=atr_entry / diviseur)
        c = screeners.SCREENERS[agent_id](spec, forced)
        assert c is not None, f"{agent_id} muet avec atr_h1 = ATR tf / {diviseur}"
        _check_candidate(c, spec, forced)
        assert any(x.startswith("SL ramené") for x in c.arguments_against), c.arguments_against


def test_spread_ratio_atr_inconnu_refuse():
    """Un ATR inconnu (NaN) rend le ratio de spread infini : le filtre REFUSE, il ne se désactive pas.

    Régression : `_spread_ratio_h1` renvoyait `NaN` quand `snap.atr_h1` valait NaN ; `NaN > 0.15` étant faux, le
    filtre de spread de L01 était silencieusement neutralisé alors que la donnée manquait.
    """
    class _Snap:
        def __init__(self, atr_h1, spread=10):
            self.atr_h1 = atr_h1
            self.spread_points = spread
            self.spec = _spec_of("metals", "XAUUSD", 2, 0.01)

    assert L._spread_ratio_h1(_Snap(float("nan"))) == float("inf")
    assert L._spread_ratio_h1(_Snap(0.0)) == float("inf")
    assert L._spread_ratio_h1(_Snap(-1.0)) == float("inf")
    assert L._spread_ratio_h1(_Snap(1.0)) == pytest.approx(0.1)
    sans_spec = _Snap(1.0)
    sans_spec.spec = None
    assert L._spread_ratio_h1(sans_spec) == float("inf")
    assert L._spread_ratio(_Snap(1.0), float("nan")) == float("inf")
    assert L._spread_ratio(_Snap(1.0), 0.0) == float("inf")
    assert L._spread_ratio(_Snap(1.0), 1.0) == pytest.approx(0.1)
