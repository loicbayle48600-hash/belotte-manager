"""Tests des stratégies propres de la famille G (volatilité) : G01 … G04.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou None),
absence de lookahead (la barre en formation n'influence pas la décision) et vitalité du module (au moins un
candidat sur l'ensemble des snapshots).

G05 (`abnormal_volatility_filter`) n'est pas un générateur (ni `strategy` ni `base_strategy` dans le registre) :
il n'a pas de stratégie propre et n'est donc pas testé ici.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import g_volatility  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"G{i:02d}" for i in range(1, 5)]
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
    """Snapshots de tous les symboles à 10 instants (le broker avance de 48 barres M5 entre chaque).

    Les agents de la famille G dépendent de l'heure de la barre clôturée (opening range, budget de session) :
    les 10 instants balaient les heures 21:00, 01:00, 05:00, 09:00, 13:00 et 17:00 UTC sur deux journées, donc
    les quatre fenêtres de session utilisées par le module.
    """
    broker = broker_module
    out = []
    for step in range(10):
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
    assert c.tp_plan == sorted(c.tp_plan, key=lambda x: c.side.sign * x)
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
    assert total >= 1, f"aucun candidat produit par la famille G : {per_agent}"


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "g_volatility" in strategies.LOADED
    assert "g_volatility" not in strategies.FAILED


# ==============================================================================================================
# Tests ajoutés par la relecture contradictoire (lookahead complet, déterminisme, robustesse, couloir borné,
# honnêteté du SL réellement proposé)
# ==============================================================================================================
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tradinglab.core.types import Regime, Side  # noqa: E402
from tradinglab.market_data.feed import MarketSnapshot  # noqa: E402

NUM_COLS = ("open", "high", "low", "close", "tick_volume", "spread", "ema20", "ema50", "ema200", "rsi14", "macd",
            "macd_signal", "macd_hist", "atr14", "bb_mid", "bb_up", "bb_low", "bb_width", "adx14", "vol_pct",
            "mom10", "ret1")


def _copy_snap(snap, **over):
    """Copie d'un snapshot (frames copiées) : les tests ne modifient jamais la fixture partagée."""
    kw = dict(symbol=snap.symbol, spec=snap.spec, tick=snap.tick,
              frames={tf: df.copy() for tf, df in snap.frames.items()}, regime=snap.regime, session=snap.session,
              spread_points=snap.spread_points, atr_h1=snap.atr_h1, data_fresh=snap.data_fresh,
              data_quality=snap.data_quality, fetched_at=snap.fetched_at, bar_times=dict(snap.bar_times),
              bar_counts=dict(snap.bar_counts))
    kw.update(over)
    return MarketSnapshot(**kw)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_no_lookahead_toutes_colonnes(agent_id, specs, snapshots):
    """Version renforcée : TOUTES les colonnes de la barre en formation sont corrompues, pas seulement l'OHLC.

    Le test d'origine ne modifiait que close/high/low/tick_volume : une lecture de `atr14`, `macd_hist`,
    `vol_pct`, `bb_*` ou `time` sur la dernière ligne serait passée inaperçue.
    """
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    for snaps in snapshots:
        for sym, snap in snaps.items():
            before = _signature(fn(spec, snap))
            s = _copy_snap(snap)
            for df in s.frames.values():
                if len(df) < 2:
                    continue
                last = df.index[-1]
                for col in NUM_COLS:
                    if col in df.columns:
                        df.loc[last, col] = float(df[col].iloc[-2]) * 7.0 + 13.0
                df.loc[last, "time"] = pd.Timestamp(df["time"].iloc[-2]) + pd.Timedelta(days=3)
            assert _signature(fn(spec, s)) == before, f"{agent_id}/{sym} : barre en formation lue quelque part"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_deterministe(agent_id, specs, snapshots):
    """Deux appels sur le même snapshot donnent exactement la même décision (aucune source d'aléa)."""
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    for snaps in snapshots[:3]:
        for snap in snaps.values():
            assert _signature(fn(spec, snap)) == _signature(fn(spec, snap))


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
    return s


DEGRADATIONS = ["tout_nan", "prix_plats", "sans_volume", "volume_nul", "frame_61", "frame_2", "frame_vide",
                "temps_naif", "temps_nat", "temps_identiques", "atr14_nan", "high_low_inverses", "atr_h1_zero",
                "atr_h1_nan", "atr_h1_enorme", "sans_spec"]


@pytest.mark.parametrize("kind", DEGRADATIONS)
def test_frames_degradees(kind, specs, snapshots):
    """NaN, frames courtes/vides, division par zéro, index vides, horodatages illisibles → None, jamais d'exception."""
    snaps = snapshots[0]
    for sym in list(snaps)[:4]:
        s = _degrade(snaps[sym], kind)
        for agent_id in AGENT_IDS:
            c = screeners.SCREENERS[agent_id](specs[agent_id], s)
            if c is not None:  # un candidat reste possible (dégradation inoffensive) mais doit rester valide
                assert c.side.sign * (c.entry - c.sl) > 0
                assert np.isfinite(c.sl) and c.sl_distance > 0
                assert c.rr >= 1.5


def test_squeeze_box_borne():
    """G02 : le couloir de compression est borné aux `squeeze_min` dernières barres, pas à tout le squeeze.

    Régression : mesuré sur le couloir complet, l'amplitude croît avec la durée du squeeze — le filtre
    « couloir <= 2,5 ATR » refusait 94 % des libérations (médiane 3,4 ATR sur 276 libérations) alors que le
    score récompense la durée, et le SL posé à l'autre bout sortait des bornes de distance.
    """
    n = 40
    closed = pd.DataFrame({
        "time": pd.date_range("2026-01-20", periods=n, freq="15min", tz="UTC"),
        "high": np.linspace(100.0, 130.0, n) + 1.0,
        "low": np.linspace(100.0, 130.0, n) - 1.0,
    })
    lo6, hi6, k6 = g_volatility._squeeze_box(closed, dur=30, n_sq=6)
    lo_all, hi_all, _ = g_volatility._squeeze_box(closed, dur=30, n_sq=99)
    assert k6 == 6
    assert hi6 - lo6 < hi_all - lo_all           # le couloir borné est strictement plus étroit
    assert lo6 == pytest.approx(float(closed["low"].iloc[-7]))
    assert hi6 == pytest.approx(float(closed["high"].iloc[-1]))
    # dur plus court que n_sq : on ne remonte pas au-delà du squeeze réel
    assert g_volatility._squeeze_box(closed, dur=2, n_sq=6)[2] == 2
    # fenêtre inexploitable → (nan, nan, 0), jamais une valeur inventée
    nanbox = closed.copy()
    nanbox["low"] = np.nan
    assert g_volatility._squeeze_box(nanbox, dur=6, n_sq=6)[2] == 0
    assert g_volatility._squeeze_box(closed.iloc[0:0], dur=6, n_sq=6)[2] == 0


def test_sl_note_helper():
    """`_sl_note` dit explicitement que le SL proposé n'est plus le niveau décrit par la thèse."""
    n = g_volatility._sl_note(Side.BUY, 100.0, 90.0, 97.0, 1.0)      # SL resserré au plafond
    assert n and n.startswith("SL ramené")
    n = g_volatility._sl_note(Side.BUY, 100.0, 99.9, 99.0, 1.0)      # SL élargi au plancher
    assert n and n.startswith("SL élargi")
    assert g_volatility._sl_note(Side.BUY, 100.0, 99.0, 99.0, 1.0) is None
    assert g_volatility._sl_note(Side.BUY, 100.0, float("nan"), 99.0, 1.0) is None
    assert g_volatility._sl_note(Side.BUY, 100.0, 99.0, 99.0, 0.0) is None


def test_sl_deplace_est_signale(specs, snapshots):
    """Quand la distance minimale (0,3 ATR H1) éloigne le SL du niveau voulu, le candidat le dit.

    On force `atr_h1` pour que le plancher de distance dépasse le SL de la thèse : le candidat doit rester
    valide, sa distance doit valoir le plancher, et `arguments_against` doit porter la mention.
    """
    teste = 0
    for snaps in snapshots:
        for snap in snaps.values():
            atr_m15 = float(snap.frames["M15"].iloc[-2]["atr14"])
            if not np.isfinite(atr_m15) or atr_m15 <= 0:
                continue
            for agent_id in AGENT_IDS:
                spec = specs[agent_id]
                base = screeners.SCREENERS[agent_id](spec, snap)
                if base is None or base.sl_distance >= 2.0 * atr_m15:
                    continue
                forced = _copy_snap(snap, atr_h1=2.0 * atr_m15 / 0.3)   # plancher = 2 ATR M15
                c = screeners.SCREENERS[agent_id](spec, forced)
                if c is None:      # le gate de SL peut refuser : c'est un refus déterministe, pas un mensonge
                    continue
                teste += 1
                assert c.sl_distance == pytest.approx(2.0 * atr_m15, rel=1e-9)
                assert any(x.startswith("SL élargi") for x in c.arguments_against), c.arguments_against
    assert teste >= 1, "aucun candidat n'a permis de vérifier la mention du SL déplacé"


class _FakeSnap:
    def __init__(self, atr_h1):
        self.atr_h1 = atr_h1


def test_bound_sl_cote_et_bornes():
    """`_bound_sl` ne change jamais le côté du SL, respecte les bornes et refuse une configuration incohérente."""
    snap = _FakeSnap(0.0)
    sl = g_volatility._bound_sl(snap, Side.BUY, 100.0, 80.0, 1.0, 0.5, 3.0)
    assert sl == pytest.approx(97.0)                      # ramené au plafond 3 ATR, toujours sous l'entrée
    sl = g_volatility._bound_sl(snap, Side.SELL, 100.0, 100.05, 1.0, 0.5, 3.0)
    assert sl == pytest.approx(100.5)                     # élargi au plancher 0,5 ATR, toujours au-dessus
    assert g_volatility._bound_sl(_FakeSnap(50.0), Side.BUY, 100.0, 99.0, 1.0, 0.5, 3.0) is None  # 0,3 ATR H1 > 3 ATR
    assert g_volatility._bound_sl(snap, Side.BUY, 100.0, float("nan"), 1.0, 0.5, 3.0) is None
    assert g_volatility._bound_sl(snap, Side.BUY, 100.0, 99.0, 0.0, 0.5, 3.0) is None


def _fake_candidate(side, entry, sl):
    return TradeCandidate(symbol="EURUSD", side=side, entry=entry, sl=sl, tp_plan=[], timeframes=["M15", "H1"],
                          regime=Regime.TRENDING, agent_id="G01")


def test_set_tp_plan():
    """Plan de TP : rr >= 1,5, au plus 3 niveaux croissants dans le sens du trade, SL nul refusé."""
    c = g_volatility._set_tp_plan(_fake_candidate(Side.BUY, 100.0, 99.0), Side.BUY, 100.0,
                                  [103.0, 104.0, 1e9, float("nan"), 99.5], 2.0)
    assert c is not None
    assert len(c.tp_plan) <= 3
    assert c.tp_plan == sorted(c.tp_plan)
    assert all(x > 100.0 for x in c.tp_plan)
    assert c.rr >= 1.5 and c.rr == pytest.approx(4.0)      # cible la plus lointaine retenue : 104
    s = g_volatility._set_tp_plan(_fake_candidate(Side.SELL, 100.0, 101.0), Side.SELL, 100.0, [], 1.0)
    assert s is not None and s.rr >= 1.5
    assert all(x < 100.0 for x in s.tp_plan)
    assert g_volatility._set_tp_plan(_fake_candidate(Side.BUY, 100.0, 100.0), Side.BUY, 100.0, [101.0], 2.0) is None
    assert g_volatility._set_tp_plan(None, Side.BUY, 100.0, [101.0], 2.0) is None


def test_utilitaires_locaux():
    """Utilitaires : aucune division par zéro, aucune valeur inventée sur des données inexploitables."""
    plate = pd.Series([1.0] * 10)
    disp, er = g_volatility._efficiency_ratio(plate)
    assert np.isnan(disp) and er == 0.0
    disp, er = g_volatility._efficiency_ratio(pd.Series([1.0, 2.0, 3.0]))
    assert disp == pytest.approx(2.0) and er == pytest.approx(1.0)
    assert g_volatility._hhmm("07:30", 9.0) == pytest.approx(7.5)
    assert g_volatility._hhmm("n'importe quoi", 9.0) == pytest.approx(9.0)
    assert g_volatility._hhmm(None, 9.0) == pytest.approx(9.0)
    vide = pd.DataFrame({"time": pd.to_datetime([], utc=True), "high": [], "low": []})
    assert g_volatility._session_history(vide, 7.0, 8.0) == []
    un_jour = pd.DataFrame({"time": pd.date_range("2026-01-20 07:00", periods=2, freq="1h", tz="UTC"),
                            "high": [1.0, 1.0], "low": [0.0, 0.0]})
    assert g_volatility._session_history(un_jour, 7.0, 8.0) == []   # aucun jour antérieur : pas d'historique
    assert g_volatility._bar_hour(pd.Series({"time": pd.NaT})) is None
    assert g_volatility._bar_hour(pd.Series({"close": 1.0})) is None
    assert g_volatility._close_pos(pd.Series({"high": 1.0, "low": 1.0, "close": 1.0}), Side.BUY) == 0.5
