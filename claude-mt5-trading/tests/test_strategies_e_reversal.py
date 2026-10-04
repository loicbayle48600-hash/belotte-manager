"""Tests des stratégies propres de la famille E (retour à la moyenne et retournements) : E01 … E07.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou None),
absence de lookahead (la barre en formation n'influence pas la décision) et vitalité du module (au moins un
candidat sur l'ensemble des snapshots).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import e_reversal  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"E{i:02d}" for i in range(1, 8)]
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
    for step in range(6):
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
    # distance entrée→SL entre 0,3 et 3 ATR du tf d'entrée (la barre -2 est la dernière barre clôturée)
    atr_entry = float(snap.frames[spec.timeframes["entry"]].iloc[-2]["atr14"])
    assert 0.3 * atr_entry <= c.sl_distance <= 3.0 * atr_entry + 1e-12
    # les TP sont du bon côté et croissants dans le sens du trade
    for tp in c.tp_plan:
        assert c.side.sign * (tp - c.entry) > 0
    assert list(c.tp_plan) == sorted(c.tp_plan, key=lambda x: c.side.sign * (x - c.entry))
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
    assert total >= 1, f"aucun candidat produit par la famille E : {per_agent}"


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "e_reversal" in strategies.LOADED
    assert "e_reversal" not in strategies.FAILED


# ------------------------------------------------------------------------------------------------------------
# Régressions de relecture : scénarios synthétiques déterministes
#
# Le marché simulé ne sollicite qu'une partie de la famille E (E01, E02, E05). Les agents restants — et surtout
# les points corrigés en relecture — sont exercés ici sur des frames construites à la main, enrichies par les
# mêmes indicateurs que le feed. Chaque test échoue sur le code d'avant correction.
# ------------------------------------------------------------------------------------------------------------
from datetime import datetime, timezone

import pandas as pd

from tradinglab.agents.strategies import e_reversal
from tradinglab.core.types import Regime, Session, Side
from tradinglab.market_data.feed import MarketSnapshot
from tradinglab.market_data.indicators import enrich
from tradinglab.market_data.regime import RegimeResult

BAR_START = datetime(2026, 1, 20, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def eurusd_spec():
    from tradinglab.mt5.mock_adapter import MockBroker

    broker = MockBroker(seed=7)
    broker.connect()
    return broker.symbol_info("EURUSD")


def _frame(bars, minutes=15):
    """Frame OHLCV enrichie : `bars` = liste de (open, high, low, close, tick_volume), la DERNIÈRE est la barre
    en formation (convention du feed)."""
    rows = [{"time": BAR_START + timedelta(minutes=minutes * k), "open": o, "high": h, "low": lo, "close": c,
             "tick_volume": v, "real_volume": 0, "spread": 1} for k, (o, h, lo, c, v) in enumerate(bars)]
    return enrich(pd.DataFrame(rows))


def _flat_h1(price=1.1000, n=120):
    """Frame H1 sans tendance : `_trend_of` renvoie FLAT, donc aucun veto de timeframe supérieur."""
    return _frame([(price, price + 0.0008, price - 0.0008, price, 900)] * n, minutes=60)


def _snapshot(spec, m15, h1, atr_h1=0.0016, spread_points=1):
    return MarketSnapshot(
        symbol="EURUSD", spec=spec, tick=None, frames={"M15": m15, "H1": h1},
        regime=RegimeResult(regime=Regime.RANGING, confidence=0.6), session=Session.LONDON,
        spread_points=spread_points, atr_h1=atr_h1, data_fresh=True, data_quality="OK",
        fetched_at=datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc),
        bar_times={"M15": str(m15["time"].iloc[-2]), "H1": str(h1["time"].iloc[-2])},
        bar_counts={"M15": len(m15), "H1": len(h1)})


# ----------------------------------------------------------------- E01 : traversée de la borne du range
def test_e01_refuse_la_traversee_de_la_borne(specs, snapshots):
    """E01 fade la borne d'un couloir : la barre de signal doit ENTRER dans la zone de borne, pas la traverser.

    Sans cette limite, une barre qui perce le couloir de 1,5 ATR avant de refermer dedans était traitée comme un
    simple repli sur la borne — c'est un faux breakout (E05) ou un balayage (E06), et le SL de E01 (borne −
    0,35 ATR) se retrouvait au milieu de l'excursion déjà parcourue par la barre.
    """
    spec = specs["E01"]
    fn = screeners.SCREENERS["E01"]
    testes = 0
    for snaps in snapshots:
        for snap in snaps.values():
            ref = fn(spec, snap)
            if ref is None:
                continue
            testes += 1
            df = snap.frames[spec.timeframes["entry"]]
            closed = df.iloc[:-1]
            win = closed.iloc[-41:-1]
            atr = float(closed.iloc[-1]["atr14"])
            i = len(df) - 2                      # ligne de la dernière barre CLÔTURÉE
            perce = df.copy()
            if ref.side is Side.BUY:
                perce.loc[i, "low"] = float(win["low"].min()) - 1.5 * atr
            else:
                perce.loc[i, "high"] = float(win["high"].max()) + 1.5 * atr
            sauf = snap.frames[spec.timeframes["entry"]]
            snap.frames[spec.timeframes["entry"]] = perce
            try:
                assert fn(spec, snap) is None, "E01 a accepté une barre qui traverse la borne du range"
            finally:
                snap.frames[spec.timeframes["entry"]] = sauf
    assert testes >= 1, "aucun candidat E01 de référence : le test ne prouve rien"


# ----------------------------------------------------------------- E05 : piège de cassure
E05_LO, E05_HI = 1.09800, 1.10200


def _e05_bars(signal):
    """Consolidation [1,0980 ; 1,1020], cassure en clôture sous la borne basse puis réintégration (`signal`)."""
    bars = []
    for i in range(94):                                   # couloir : base = barres [64:94] des barres clôturées
        if i % 5 == 0:
            bars.append((E05_LO + 0.0002, E05_LO + 0.0010, E05_LO, E05_LO + 0.0008, 1000))   # touche la borne basse
        elif i % 5 == 2:
            bars.append((E05_HI - 0.0008, E05_HI, E05_HI - 0.0010, E05_HI - 0.0002, 1000))   # touche la borne haute
        else:
            bars.append((1.10000, 1.10070, 1.09930, 1.09990, 1000))
    bars.append((1.09850, 1.09900, 1.09790, 1.09820, 1000))    # brk[0] : pas encore de cassure
    bars.append((1.09820, 1.09830, 1.09740, 1.09750, 1200))    # brk[1] : CLÔTURE sous la borne basse
    bars.append((1.09750, 1.09800, 1.09740, 1.09780, 1100))
    bars.append((1.09780, 1.09810, 1.09750, 1.09770, 1100))
    bars.append((1.09770, 1.09820, 1.09760, 1.09790, 1100))
    bars.append(signal)                                        # barre de signal : réintégration du couloir
    bars.append((signal[3], signal[3] + 0.0005, signal[3] - 0.0005, signal[3] + 0.0002, 900))  # barre en formation
    return bars


def _e05_candidate(spec, eurusd_spec, signal):
    snap = _snapshot(eurusd_spec, _frame(_e05_bars(signal)), _flat_h1())
    return screeners.SCREENERS["E05"](spec, snap), snap


def test_e05_sl_derriere_l_extreme_du_piege_barre_de_signal_comprise(specs, eurusd_spec):
    """Le SL de E05 doit être derrière l'extrême du piège EN INCLUANT la barre de signal.

    C'est très souvent la barre de réintégration elle-même qui pousse le plus loin au-delà du niveau avant de
    refermer : la calculer sur les seules barres antérieures plaçait le SL à l'intérieur de l'excursion déjà
    parcourue (ici SL ≈ 1,09688 alors que la barre de signal était descendue à 1,09620).
    """
    # signal : mèche à 1,09620 (nouvel extrême du piège), clôture à 1,09880 dans le couloir
    c, _ = _e05_candidate(specs["E05"], eurusd_spec, (1.09760, 1.09890, 1.09620, 1.09880, 1500))
    assert c is not None, "le scénario de faux breakout doit produire un candidat E05"
    assert c.side is Side.BUY
    assert c.sl <= 1.09620, f"SL {c.sl} à l'intérieur de la mèche du piège (plus bas de la barre de signal)"


def test_e05_score_recompense_la_profondeur_de_reintegration(specs, eurusd_spec):
    """La composante « profondeur de la réintégration » (0-10 documentée) doit réellement varier.

    Les deux scénarios ne diffèrent QUE par la clôture de la barre de signal (même mèche, même amplitude, même
    corps, même volume, même consolidation) : une réintégration profonde doit marquer nettement plus qu'une
    réintégration au ras du niveau. Avec le signe inversé la composante valait 0 dans les deux cas et les deux
    scores étaient identiques.
    """
    profond, _ = _e05_candidate(specs["E05"], eurusd_spec, (1.09760, 1.09890, 1.09620, 1.09880, 1500))
    rase, _ = _e05_candidate(specs["E05"], eurusd_spec, (1.09700, 1.09890, 1.09620, 1.09820, 1500))
    assert profond is not None and rase is not None
    assert profond.setup_score - rase.setup_score > 5.0, (
        f"profondeur de réintégration non prise en compte : {profond.setup_score} vs {rase.setup_score}")


# ----------------------------------------------------------------- E06 : balayage de liquidité
def _e06_bars(signal, sommet_lointain=None):
    """Oscillation régulière + deux creux confirmés proches (1,09900 et 1,09920) formant l'amas de liquidité."""
    bars = []
    for i in range(100):
        base = 1.10000 + (0.00060 if i % 6 < 3 else -0.00060)
        o = base
        c = base + (0.0001 if i % 2 else -0.0001)
        bars.append((o, max(o, c) + 0.00025, min(o, c) - 0.00025, c, 900))
    bars[60] = (1.10000, 1.10010, 1.09900, 1.09980, 1000)      # plancher de l'amas
    bars[76] = (1.10000, 1.10010, 1.09920, 1.09980, 1000)      # plafond de l'amas
    if sommet_lointain is not None:
        bars[50] = (1.10000, sommet_lointain, 1.09990, 1.10050, 1000)   # pivot haut très lointain, mais ancien
    bars.append(signal)
    bars.append((signal[3], signal[3] + 0.0004, signal[3] - 0.0004, signal[3] + 0.0002, 800))
    return bars


def _e06_candidate(spec, eurusd_spec, signal, sommet_lointain=None):
    snap = _snapshot(eurusd_spec, _frame(_e06_bars(signal, sommet_lointain)), _flat_h1())
    return screeners.SCREENERS["E06"](spec, snap)


def test_e06_le_balayage_doit_depasser_le_plancher_de_l_amas(specs, eurusd_spec):
    """Les stops sont sous le PLUS BAS de l'amas, pas sous sa moyenne.

    Une mèche qui s'arrête entre le plancher (1,09900) et la moyenne de l'amas ne déclenche aucun stop : il n'y a
    pas de flux de couverture à exploiter, donc pas de signal. Le test compare deux mèches identiques par ailleurs.
    """
    sous_le_plancher = _e06_candidate(specs["E06"], eurusd_spec, (1.09980, 1.10010, 1.09850, 1.10000, 1500))
    assert sous_le_plancher is not None, "un balayage sous le plancher de l'amas doit produire un candidat E06"
    assert sous_le_plancher.side is Side.BUY

    au_dessus_du_plancher = _e06_candidate(specs["E06"], eurusd_spec, (1.09980, 1.10010, 1.09898, 1.10000, 1500))
    assert au_dessus_du_plancher is None, "aucun stop n'est pris entre le plancher et la moyenne de l'amas"


def test_e06_cible_le_dernier_pivot_oppose_et_non_l_extreme_de_la_fenetre(specs, eurusd_spec):
    """La cible de E06 est le dernier pivot opposé confirmé, pas l'extrême des 60 barres.

    Un sommet isolé très lointain dans la fenêtre ne doit pas gonfler le `rr` annoncé : il n'est pas l'amas de
    liquidité que le prix ira chercher en premier.
    """
    sans = _e06_candidate(specs["E06"], eurusd_spec, (1.09980, 1.10010, 1.09850, 1.10000, 1500))
    avec = _e06_candidate(specs["E06"], eurusd_spec, (1.09980, 1.10010, 1.09850, 1.10000, 1500),
                          sommet_lointain=1.10600)
    assert sans is not None and avec is not None
    rr_defaut = float(specs["E06"].params.get("rr", 2.0))
    assert avec.rr == pytest.approx(rr_defaut, abs=0.05), (
        f"rr {avec.rr} gonflé par un sommet lointain au lieu du dernier pivot opposé")
    assert avec.tp_plan[-1] < 1.10600


# ----------------------------------------------------------------- E04 : excès mesuré en écarts-types
def _e04_bars(drop=0.009, nfall=3, rebond=0.003):
    """Longue phase plate, puis chute de `drop` en `nfall` barres (clôtures hors bande) et barre de réintégration."""
    bars = []
    px = 1.10000
    for i in range(160):
        o = px + (0.0002 if i % 2 else -0.0002)
        c = px - (0.0002 if i % 2 else -0.0002)
        bars.append((o, max(o, c) + 0.0003, min(o, c) - 0.0003, c, 900))
    for k in range(nfall):
        c = px - drop * (k + 1) / nfall
        o = c + drop / nfall * 0.8
        bars.append((o, o + 0.0002, c - 0.0004, c, 1400))
    last = bars[-1][3]
    bars.append((last, last + rebond + 0.0002, last - 0.0002, last + rebond, 1300))       # barre de signal
    bars.append((last + rebond, last + rebond + 0.0004, last + rebond - 0.0004, last + rebond + 0.0002, 900))
    return bars


def test_e04_ecart_type_mesure_sur_chaque_barre_d_excursion(specs, eurusd_spec):
    """L'excès de chaque barre d'excursion est mesuré avec SES PROPRES bandes.

    On élargit uniquement les bandes de la barre de signal (les barres d'excursion sont inchangées) : l'excès déjà
    constaté ne bouge pas, donc la décision ne doit pas bouger. Mesurer les trois barres avec l'écart-type de la
    seule barre de signal faisait disparaître le signal dès que les bandes s'élargissaient.
    """
    spec = specs["E04"]
    base = _frame(_e04_bars())
    reference = screeners.SCREENERS["E04"](spec, _snapshot(eurusd_spec, base, _flat_h1()))
    assert reference is not None, "le scénario de réintégration doit produire un candidat E04"

    elargi = base.copy()
    i = len(elargi) - 2                                   # ligne de la dernière barre CLÔTURÉE
    mid = float(elargi.loc[i, "bb_mid"])
    elargi.loc[i, "bb_up"] = mid + (float(elargi.loc[i, "bb_up"]) - mid) * 3.0
    elargi.loc[i, "bb_low"] = mid - (mid - float(elargi.loc[i, "bb_low"])) * 3.0
    apres = screeners.SCREENERS["E04"](spec, _snapshot(eurusd_spec, elargi, _flat_h1()))
    assert apres is not None, "l'excès mesuré sur les barres d'excursion ne dépend pas des bandes de la barre de signal"
    assert apres.side is reference.side
    assert apres.entry == pytest.approx(reference.entry)


# ----------------------------------------------------------------- Robustesse des utilitaires
def test_spread_ratio_h1_refuse_un_atr_h1_non_fini(eurusd_spec):
    """Un ATR H1 absent, nul ou NaN doit donner `inf` (filtre de spread bloquant), jamais NaN.

    `float(nan or 0.0)` vaut NaN : toute comparaison `ratio > seuil` devenait fausse et désactivait silencieusement
    le filtre de spread de TOUS les agents du module.
    """
    import math
    import types

    for valeur in (float("nan"), None, 0.0, -1.0, float("inf")):
        faux_snap = types.SimpleNamespace(atr_h1=valeur, spec=eurusd_spec, spread_points=5)
        ratio = e_reversal._spread_ratio_h1(faux_snap)
        assert not math.isnan(ratio), f"ATR H1 {valeur} : ratio de spread NaN (filtre désactivé)"
        assert ratio == float("inf"), f"ATR H1 {valeur} : le filtre de spread doit refuser"
    bon = types.SimpleNamespace(atr_h1=0.0016, spec=eurusd_spec, spread_points=5)
    assert 0 < e_reversal._spread_ratio_h1(bon) < 1.0


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_robustesse_frames_degradees(agent_id, specs, eurusd_spec):
    """Frames trop courtes, colonnes NaN ou volume absent : `None`, jamais d'exception ni de valeur inventée."""
    spec = specs[agent_id]
    fn = screeners.SCREENERS[agent_id]
    complet = _frame(_e05_bars((1.09760, 1.09890, 1.09620, 1.09880, 1500)))

    courte = complet.iloc[-5:].reset_index(drop=True)
    assert fn(spec, _snapshot(eurusd_spec, courte, _flat_h1())) is None

    nan_frame = complet.copy()
    for col in ("atr14", "rsi14", "adx14", "ema20"):
        nan_frame.loc[len(nan_frame) - 2, col] = float("nan")
    assert fn(spec, _snapshot(eurusd_spec, nan_frame, _flat_h1())) is None

    sans_volume = complet.drop(columns=["tick_volume"])
    sortie = fn(spec, _snapshot(eurusd_spec, sans_volume, _flat_h1()))              # aucune exception attendue
    assert sortie is None or isinstance(sortie, TradeCandidate)

    atr_nul = complet.copy()
    atr_nul.loc[len(atr_nul) - 2, "atr14"] = 0.0
    assert fn(spec, _snapshot(eurusd_spec, atr_nul, _flat_h1())) is None

    assert fn(spec, _snapshot(eurusd_spec, complet, _flat_h1(), atr_h1=float("nan"))) is None
