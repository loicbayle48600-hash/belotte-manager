"""Tests des stratégies propres de la famille M (spécialistes par symbole) : M01 … M14.

Vérifie pour chaque agent : enregistrement dans SCREENERS, robustesse (aucune exception, candidat valide ou None),
absence de lookahead (la barre en formation n'influence pas la décision) et vitalité du module (au moins un
candidat sur l'ensemble des snapshots).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tradinglab.agents import screeners
from tradinglab.agents.registry import default_agents
from tradinglab.agents.strategies import m_symbols  # noqa: F401 - l'import enregistre les stratégies
from tradinglab.core.types import TradeCandidate
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.risk.stop_loss import validate_stop_loss

AGENT_IDS = [f"M{i:02d}" for i in range(1, 15)]
TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]
# Nombre de barres M5 ajoutées entre deux instants : les décalages (4 h, 3 h 30, 2 h 30…) font balayer aux barres
# clôturées toutes les fenêtres horaires exploitées par la famille (Asie, ouverture de Londres, ouverture de NY).
ADVANCES = [48, 42, 48, 30, 48, 36, 48]


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
    """Snapshots de tous les symboles simulés à 7 instants différents (barres clôturées de 21h à 18h30 UTC)."""
    broker = broker_module
    out = []
    for step, bars in enumerate(ADVANCES):
        if step:
            broker.advance_bars(bars)
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
    prev = None
    for tp in c.tp_plan:
        assert c.side.sign * (tp - c.entry) > 0
        if prev is not None:
            assert c.side.sign * (tp - prev) > 0
        prev = tp
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
    assert total >= 1, f"aucun candidat produit par la famille M : {per_agent}"


def test_module_loaded():
    from tradinglab.agents import strategies

    assert "m_symbols" in strategies.LOADED
    assert "m_symbols" not in strategies.FAILED


# ------------------------------------------------------------------------------------------------------------
# Régressions de relecture contradictoire : scénarios synthétiques déterministes
#
# Le marché simulé ne déclenche qu'une petite partie de la famille M (M02, M09) : les autres agents — et surtout
# les points corrigés en relecture — ne seraient couverts par AUCUNE assertion. Les frames ci-dessous sont
# construites à la main puis enrichies par les mêmes indicateurs que le feed. Chaque test échoue sur le code
# d'avant correction.
# ------------------------------------------------------------------------------------------------------------
from datetime import datetime, timezone

import pandas as pd

from tradinglab.agents.strategies import m_symbols
from tradinglab.core.types import Regime, Session, Side
from tradinglab.market_data.feed import MarketSnapshot
from tradinglab.market_data.indicators import enrich
from tradinglab.market_data.regime import RegimeResult

# classe d'actif exigée par chaque agent (`_class_ok`) : sans elle, un test « toutes familles » ne prouverait
# rien (l'agent sortirait sur le filtre de classe avant d'atteindre le point contrôlé)
CLASSE = {"M01": "forex", "M02": "forex", "M03": "forex", "M04": "forex", "M05": "forex", "M06": "forex",
          "M07": "metals", "M08": "metals", "M09": "indices", "M10": "indices", "M11": "indices",
          "M12": "forex", "M13": "forex", "M14": "energies"}
SYMBOLE = {"forex": "EURUSD", "metals": "XAUUSD", "indices": "NAS100", "energies": "USOIL"}
ECHELLE = {"forex": 1.0, "metals": 1800.0, "indices": 16000.0, "energies": 70.0}


@pytest.fixture(scope="module")
def broker_specs(broker_module):
    """Spécifications de symboles réelles (digits, point, stops_level) issues du broker simulé."""
    return {c: broker_module.symbol_info(s) for c, s in SYMBOLE.items()}


def _frame(bars, minutes, end):
    """Frame OHLCV enrichie se terminant à `end` pour la DERNIÈRE BARRE CLÔTURÉE.

    `bars` = liste de (open, high, low, close, tick_volume) ; la dernière est la barre EN FORMATION (convention
    du feed), elle est donc datée `end + minutes`.
    """
    start = end - timedelta(minutes=minutes * (len(bars) - 2))
    rows = [{"time": start + timedelta(minutes=minutes * k), "open": o, "high": h, "low": lo, "close": c,
             "tick_volume": v, "real_volume": 0, "spread": 1} for k, (o, h, lo, c, v) in enumerate(bars)]
    return enrich(pd.DataFrame(rows))


def _frame_aux_dates(bars, times):
    """Frame enrichie sur des horodatages EXPLICITES (utile pour sauter un week-end)."""
    rows = [{"time": t, "open": o, "high": h, "low": lo, "close": c, "tick_volume": v, "real_volume": 0,
             "spread": 1} for t, (o, h, lo, c, v) in zip(times, bars)]
    return enrich(pd.DataFrame(rows))


def _h1_tendance(px, end, n=240, pas=None, hausse=True):
    """Frame H1 en tendance nette : `_trend_of` = UP/DOWN et ADX élevé (aucun veto de timeframe supérieur)."""
    pas = pas if pas is not None else px * 0.00035
    s = 1 if hausse else -1
    bars, p = [], px
    for _ in range(n):
        o, c = p, p + s * pas
        bars.append((o, max(o, c) + pas * 0.1, min(o, c) - pas * 0.1, c, 1000))
        p = c
    return _frame(bars, 60, end)


def _h1_plat(px, end, n=240):
    """Frame H1 sans direction : `_trend_of` = FLAT, donc aucun veto ni bonus de timeframe supérieur."""
    a = px * 0.0002
    bars = [((px + a, px + 2 * a, px - 2 * a, px - a, 1000) if i % 2 else (px - a, px + 2 * a, px - 2 * a, px + a, 1000))
            for i in range(n)]
    return _frame(bars, 60, end)


def _d1(px, end, n=10, haut=None, bas=None):
    """Frame D1 minimale (>= 3 barres) ; la dernière ligne est le jour EN COURS, `iloc[-2]` la veille."""
    haut = haut if haut is not None else px * 1.004
    bas = bas if bas is not None else px * 0.996
    return _frame([((haut + bas) / 2, haut, bas, (haut + bas) / 2, 5000)] * n, 1440, end)


def _snap(sym_spec, m15, h1, d1=None, atr_h1=0.0016, spread_points=1, symbole="EURUSD"):
    frames = {"M15": m15, "H1": h1}
    if d1 is not None:
        frames["D1"] = d1
    return MarketSnapshot(
        symbol=symbole, spec=sym_spec, tick=None, frames=frames,
        regime=RegimeResult(regime=Regime.TRENDING, confidence=0.6), session=Session.LONDON,
        spread_points=spread_points, atr_h1=atr_h1, data_fresh=True, data_quality="OK",
        fetched_at=datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc),
        bar_times={"M15": str(m15["time"].iloc[-2]), "H1": str(h1["time"].iloc[-2])},
        bar_counts={"M15": len(m15), "H1": len(h1)})


def _frame_neutre(px, n=130):
    """M15 oscillante quelconque : sert de support aux tests transverses (robustesse, NaN, lookahead)."""
    a = px * 0.0004
    bars = []
    for i in range(n):
        o = px + (a if i % 2 else -a)
        c = px - (a if i % 2 else -a)
        bars.append((o, max(o, c) + a, min(o, c) - a, c, 1000))
    return bars


def _snap_neutre(broker_specs, agent_id, atr_h1=None, **kw):
    """Snapshot « quelconque » mais de la BONNE classe d'actif pour l'agent visé."""
    cls = CLASSE[agent_id]
    px = ECHELLE[cls]
    end = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)
    m15 = _frame(_frame_neutre(px), 15, end)
    h1 = _h1_plat(px, end)
    atr_h1 = atr_h1 if atr_h1 is not None else px * 0.0015
    return _snap(broker_specs[cls], m15, h1, _d1(px, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)),
                 atr_h1=atr_h1, symbole=SYMBOLE[cls], **kw)


# ----------------------------------------------------------------- utilitaires partagés du module
def test_spread_ratio_h1_refuse_un_atr_h1_non_exploitable(broker_specs):
    """Un ATR H1 absent, nul, négatif, infini ou NaN doit donner `inf`, jamais NaN.

    `float(nan or 0.0)` vaut NaN (NaN est « vrai ») : la comparaison `ratio > seuil` devenait fausse et
    DÉSACTIVAIT silencieusement le filtre de spread des 14 agents du module.
    """
    import math
    import types

    for valeur in (float("nan"), None, 0.0, -1.0, float("inf")):
        faux = types.SimpleNamespace(atr_h1=valeur, spec=broker_specs["forex"], spread_points=5)
        ratio = m_symbols._spread_ratio_h1(faux)
        assert not math.isnan(ratio), f"ATR H1 {valeur} : ratio NaN (filtre de spread neutralisé)"
        assert ratio == float("inf"), f"ATR H1 {valeur} : le filtre de spread doit refuser"
    bon = types.SimpleNamespace(atr_h1=0.0016, spec=broker_specs["forex"], spread_points=5)
    assert 0 < m_symbols._spread_ratio_h1(bon) < 1.0


def test_bound_sl_refuse_un_stop_du_mauvais_cote(broker_specs):
    """Un SL brut du mauvais côté de l'entrée doit donner `None`, pas un stop « retourné ».

    `_bound_sl` ne travaillait que sur `abs(entry - sl)` : un niveau structurel erroné (au-dessus de l'entrée
    pour un achat) était silencieusement recopié SOUS l'entrée à la même distance — un stop que l'analyse de
    l'agent ne justifie pas, exactement la « valeur inventée » que le projet interdit.
    """
    import types

    snap = types.SimpleNamespace(atr_h1=0.0016, spec=broker_specs["forex"], spread_points=1)
    atr = 0.0010
    assert m_symbols._bound_sl(snap, Side.BUY, 1.1000, 1.1020, atr, 0.5, 2.0) is None
    assert m_symbols._bound_sl(snap, Side.SELL, 1.1000, 1.0980, atr, 0.5, 2.0) is None
    assert m_symbols._bound_sl(snap, Side.BUY, 1.1000, 1.1000, atr, 0.5, 2.0) is None
    bon = m_symbols._bound_sl(snap, Side.BUY, 1.1000, 1.0985, atr, 0.5, 2.0)
    assert bon is not None and bon < 1.1000
    assert m_symbols._bound_sl(snap, Side.BUY, 1.1000, float("nan"), atr, 0.5, 2.0) is None


def test_prev_day_prend_la_derniere_journee_cotee():
    """`_prev_day` doit renvoyer la dernière journée RÉELLEMENT cotée, pas « jour courant − 1 ».

    Un lundi (ou un lendemain de férié), la veille calendaire ne contient aucune barre : l'agent qui s'appuie
    sur le profil de la veille (M11) était silencieusement mort une séance sur cinq.
    """
    ven = datetime(2026, 1, 16, 20, 0, tzinfo=timezone.utc)
    lun = datetime(2026, 1, 19, 0, 0, tzinfo=timezone.utc)
    times = [ven + timedelta(minutes=15 * k) for k in range(8)] + [lun + timedelta(minutes=15 * k) for k in range(8)]
    bars = [(1.1, 1.1005, 1.0995, 1.1, 900)] * len(times)
    closed = _frame_aux_dates(bars, times)
    veille = m_symbols._prev_day(closed)
    assert len(veille) == 8
    assert str(veille["time"].iloc[0])[:10] == "2026-01-16"
    # une frame qui ne contient qu'une seule journée n'invente rien : elle renvoie une fenêtre vide
    seule = _frame_aux_dates(bars[:8], times[8:])
    assert m_symbols._prev_day(seule).empty


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_atr_h1_non_exploitable_refuse_le_signal(agent_id, specs, broker_specs):
    """ATR H1 NaN / nul : aucun agent ne doit produire de candidat (et aucun ne doit lever).

    Avec un ATR H1 NaN, `validate_stop_loss` saute ses bornes en ATR (toute comparaison avec NaN est fausse) et
    le `TradeCandidate.atr` publié — qui sert ensuite au dimensionnement du risque — serait NaN.
    """
    fn = screeners.SCREENERS[agent_id]
    for valeur in (float("nan"), 0.0):
        assert fn(specs[agent_id], _snap_neutre(broker_specs, agent_id, atr_h1=valeur)) is None


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_barre_de_signal_sans_prix_exploitables(agent_id, specs, broker_specs):
    """Prix OHLC NaN sur la barre CLÔTURÉE de décision : `None`, jamais un signal non confirmé.

    `_ctx` ne valide que les colonnes d'indicateurs. Sans `_bar_ok`, les filtres d'anatomie de bougie
    (`_close_pos`, `_wick_against`, `close <= open`…) comparaient des NaN — donc ne rejetaient RIEN — et la
    « confirmation » annoncée dans la docstring n'était jamais vérifiée.
    """
    fn = screeners.SCREENERS[agent_id]
    snap = _snap_neutre(broker_specs, agent_id)
    for tf in ("M15", "H1"):
        abime = snap.frames[tf].copy()
        i = len(abime) - 2
        abime.loc[i, "high"] = float("nan")
        abime.loc[i, "low"] = float("nan")
        sauf = snap.frames[tf]
        snap.frames[tf] = abime
        try:
            assert fn(specs[agent_id], snap) is None, f"{agent_id} : barre {tf} sans prix acceptée"
        finally:
            snap.frames[tf] = sauf


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_robustesse_frames_degradees(agent_id, specs, broker_specs):
    """Frames courtes, indicateurs NaN, volume absent, ATR nul : `None` ou candidat valide, jamais d'exception."""
    fn = screeners.SCREENERS[agent_id]
    spec = specs[agent_id]
    snap = _snap_neutre(broker_specs, agent_id)
    complet = snap.frames["M15"]

    courte = complet.iloc[-5:].reset_index(drop=True)
    snap.frames["M15"] = courte
    assert fn(spec, snap) is None
    snap.frames["M15"] = complet

    nan_frame = complet.copy()
    for col in ("atr14", "rsi14", "adx14", "ema20"):
        nan_frame.loc[len(nan_frame) - 2, col] = float("nan")
    snap.frames["M15"] = nan_frame
    assert fn(spec, snap) is None
    snap.frames["M15"] = complet

    snap.frames["M15"] = complet.drop(columns=["tick_volume"])
    sortie = fn(spec, snap)                                    # aucune exception attendue
    assert sortie is None or isinstance(sortie, TradeCandidate)
    snap.frames["M15"] = complet

    atr_nul = complet.copy()
    atr_nul.loc[len(atr_nul) - 2, "atr14"] = 0.0
    snap.frames["M15"] = atr_nul
    assert fn(spec, snap) is None
    snap.frames["M15"] = complet

    vide = snap.frames["D1"]
    snap.frames.pop("D1")
    assert fn(spec, snap) is None or isinstance(fn(spec, snap), TradeCandidate)
    snap.frames["D1"] = vide


# ------------------------------------------------------------------------------------------------------------
# Scénarios de référence : chacun DÉCLENCHE réellement son agent. Sans eux, les tests ci-dessus seraient vides
# de sens (un agent qui ne signale jamais passe tous les tests « doit renvoyer None »).
# ------------------------------------------------------------------------------------------------------------
PDH, PDL = 1.10500, 1.09800


def _bruit(px, n, amp_mult=4.0):
    a = px * 0.0001
    return [((px + a, px + amp_mult * a, px - amp_mult * a, px - a, 900) if i % 2 else
             (px - a, px + amp_mult * a, px - amp_mult * a, px + a, 900)) for i in range(n)]


# ---------------------------------------------------------------------------------- M04 : balayage de la veille
def _bars_m04(balayage_sur_precedente=True):
    """Journée plate sous le plus haut de la veille, puis balayage de ce niveau et réintégration."""
    bars = _bruit(1.10200, 70)
    if balayage_sur_precedente:
        bars.append((1.10300, PDH + 0.0012, 1.10280, 1.10330, 1500))   # balayage : longue mèche haute
        bars.append((1.10330, 1.10335, 1.10290, 1.10300, 1400))        # signal : mèche propre quasi nulle
    else:
        bars.append((1.10300, 1.10380, 1.10280, 1.10350, 1000))
        bars.append((1.10350, PDH + 0.0012, 1.10250, 1.10300, 1500))   # balayage SUR la barre de signal
    c = bars[-1][3]
    bars.append((c, c + 0.0004, c - 0.0004, c - 0.0001, 900))
    return bars


def _snap_m04(broker_specs, **kw):
    end = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)
    return _snap(broker_specs["forex"], _frame(_bars_m04(**kw), 15, end), _h1_plat(1.10000, end),
                 _d1(1.10150, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc), haut=PDH, bas=PDL))


def test_m04_mesure_la_meche_de_rejet_sur_la_barre_de_balayage(specs, broker_specs):
    """La mèche de rejet se mesure sur la barre QUI A BALAYÉ, pas systématiquement sur la barre de signal.

    Quand le dépassement du plus haut de la veille a lieu sur la barre précédente, c'est elle qui porte le rejet ;
    la barre de signal n'est que la confirmation et n'a souvent aucune mèche. En mesurant `_wick_against(le)`,
    l'agent rejetait le cas le plus caractéristique du turtle soup — celui que sa propre docstring décrit.
    """
    spec, fn = specs["M04"], screeners.SCREENERS["M04"]
    snap = _snap_m04(broker_specs, balayage_sur_precedente=True)
    le = snap.frames["M15"].iloc[-2]
    assert m_symbols._wick_against(le, Side.SELL) < 0.35, "le scénario doit avoir une barre de signal SANS mèche"
    c = fn(spec, snap)
    assert c is not None, "balayage porté par la barre précédente : le rejet doit être reconnu"
    assert c.side is Side.SELL
    _check_candidate(c, spec, snap)
    assert c.sl > c.entry
    # le cas « balayage sur la barre de signal » reste évidemment valide
    autre = _snap_m04(broker_specs, balayage_sur_precedente=False)
    c2 = fn(spec, autre)
    assert c2 is not None and c2.side is Side.SELL
    _check_candidate(c2, spec, autre)


# ---------------------------------------------------------------------------------- M06 : escalier de marches
def _bars_m06(marches=8, casser=False, zigzag=False):
    """Bruit plat puis une série de barres dont les plus bas montent (escalier), enfin la marche suivante."""
    bars = _bruit(1.09000, 70, amp_mult=2.0)
    p = 1.09000
    for k in range(marches):
        lo = p + 0.0003 * k
        bars.append((lo + 0.00005, lo + 0.00060, lo, lo + 0.00055, 1000))
    if casser:                       # la dernière marche repasse sous la précédente : série rompue
        o, h, l, c, v = bars[-1]
        bars[-1] = (o, h, bars[-2][2] - 0.0010, c, v)
    if zigzag:                       # même pente moyenne, mais plus bas NON monotones : un canal, pas un escalier
        for j in range(marches):
            i = len(bars) - marches + j
            o, h, l, c, v = bars[i]
            if j % 2 == 0:
                bars[i] = (o, h, l - 0.0009, c, v)
    prev = bars[-1]
    lo = prev[2] + 0.0002
    c = prev[1] + 0.0004
    bars.append((lo + 0.0001, c + 0.00003, lo, c, 1200))          # barre de signal : une marche de plus
    bars.append((c, c + 0.0003, c - 0.0003, c + 0.0001, 900))     # barre en formation
    return bars


def _snap_m06(broker_specs, **kw):
    end = datetime(2026, 1, 20, 9, 0, tzinfo=timezone.utc)
    return _snap(broker_specs["forex"], _frame(_bars_m06(**kw), 15, end), _h1_tendance(1.08000, end),
                 _d1(1.09300, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)))


def test_m06_escalier_stop_sous_la_marche_precedente(specs, broker_specs):
    """M06 signale un escalier et ancre son stop SOUS le plus bas des deux dernières marches.

    La thèse (« l'escalier meurt à sa première marche cassée ») n'a de sens que si le stop est réellement
    derrière ce niveau : un plafond de distance trop serré le plaçait à l'intérieur de l'escalier.
    """
    spec, fn = specs["M06"], screeners.SCREENERS["M06"]
    snap = _snap_m06(broker_specs)
    c = fn(spec, snap)
    assert c is not None, "une série de 8 plus bas croissants suivie d'une nouvelle marche doit signaler"
    assert c.side is Side.BUY
    _check_candidate(c, spec, snap)
    closed = snap.frames["M15"].iloc[:-1]
    marche_precedente = float(closed["low"].iloc[-2])
    assert c.sl < marche_precedente, f"SL {c.sl} à l'intérieur de l'escalier (marche à {marche_precedente})"


def test_m06_refuse_ce_qui_n_est_pas_un_escalier(specs, broker_specs):
    """Série rompue, escalier trop court, ou canal linéaire à plus bas NON monotones → aucun signal.

    Le dernier cas est le discriminant de la relecture : un canal de régression de même pente (moteur de L03)
    ne doit PAS déclencher M06, sinon les deux agents décideraient sur le même objet.
    """
    spec, fn = specs["M06"], screeners.SCREENERS["M06"]
    for libelle, kw in (("série rompue", dict(casser=True)), ("escalier trop court", dict(marches=3)),
                        ("canal linéaire non monotone", dict(zigzag=True))):
        assert fn(spec, _snap_m06(broker_specs, **kw)) is None, f"M06 a accepté : {libelle}"


def test_m06_efficience_calculee_sans_valeur_de_repli():
    """Le ratio d'efficience refuse une série plate (chemin nul) au lieu de renvoyer 0 ou 1 par défaut."""
    import numpy as np

    assert m_symbols._efficiency(np.array([1.0, 1.0, 1.0, 1.0])) is None
    assert m_symbols._efficiency(np.array([1.0, float("nan"), 3.0, 4.0])) is None
    assert m_symbols._efficiency(np.array([1.0, 2.0, 3.0, 4.0])) == pytest.approx(1.0)
    assert m_symbols._efficiency(np.array([1.0, 2.0, 1.0, 2.0])) == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------------- M08 : balayage + climax
def _bars_m08():
    """Veille plate, journée avec un plancher net, puis balayage sous ce plancher au volume x4 et réintégration."""
    bars = _bruit(2000.0, 48, amp_mult=6.0)
    for i in range(20):
        o = 2000.0 + (0.2 if i % 2 else -0.2)
        c = 2000.0 - (0.2 if i % 2 else -0.2)
        bars.append((o, max(o, c) + 0.6, min(o, c) - 2.0, c, 1000))
    bars.append((1999.0, 1999.4, 1994.0, 1999.2, 4000))      # balayage : mèche basse + climax de volume
    bars.append((1999.2, 2000.6, 1999.0, 2000.4, 1500))      # signal : clôture au-dessus du milieu du balayage
    bars.append((2000.4, 2001.0, 2000.0, 2000.6, 900))
    return bars


def _snap_m08(broker_specs):
    end = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)
    return _snap(broker_specs["metals"], _frame(_bars_m08(), 15, end), _h1_plat(2000.0, end),
                 _d1(2000.0, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)), atr_h1=6.0, symbole="XAUUSD")


def test_m08_argument_oriente_selon_le_sens(specs, broker_specs):
    """L'argument de réintégration doit décrire le sens RÉEL du trade.

    Le texte « clôture sous le milieu de la bougie de balayage » était écrit en dur : sur un achat (balayage du
    plus BAS du jour) il affirmait le contraire de ce que l'agent venait de vérifier — un argument faux publié
    au réviseur adversarial puis au journal.
    """
    spec, fn = specs["M08"], screeners.SCREENERS["M08"]
    snap = _snap_m08(broker_specs)
    c = fn(spec, snap)
    assert c is not None and c.side is Side.BUY
    _check_candidate(c, spec, snap)
    milieu = [a for a in c.arguments_for if "milieu de la bougie de balayage" in a]
    assert milieu and "au-dessus" in milieu[0], f"argument contradictoire pour un achat : {milieu}"


# ---------------------------------------------------------------------------------- M09 : opening range + gap
def _bars_m09(gap_contraire=False):
    bars = _bruit(18000.0, 64, amp_mult=3.0)
    bars.append((17960.0 if gap_contraire else 18005.0, 18020.0, 18000.0, 18015.0, 2000))   # 13:30
    bars.append((18015.0, 18022.0, 18008.0, 18012.0, 2000))                                  # 13:45
    bars.append((18013.0, 18031.0, 18012.0, 18030.0, 3000))                                  # 14:00 : cassure
    bars.append((18030.0, 18035.0, 18025.0, 18032.0, 900))
    return bars


def _snap_m09(broker_specs, **kw):
    end = datetime(2026, 1, 20, 14, 0, tzinfo=timezone.utc)
    return _snap(broker_specs["indices"], _frame(_bars_m09(**kw), 15, end), _h1_plat(18000.0, end),
                 _d1(18000.0, datetime(2026, 1, 19, 0, 0, tzinfo=timezone.utc)), atr_h1=30.0, symbole="NAS100")


def test_m09_accepte_la_premiere_barre_apres_l_opening_range(specs, broker_specs):
    """La toute PREMIÈRE clôture postérieure à l'opening range doit pouvoir signaler.

    L'agent exigeait au moins deux barres après l'opening range (`len(after) < 2`), ce qui excluait exactement
    le signal décrit par sa thèse — la cassure initiale — et le repoussait d'une barre entière.
    """
    spec, fn = specs["M09"], screeners.SCREENERS["M09"]
    snap = _snap_m09(broker_specs)
    closed = snap.frames["M15"].iloc[:-1]
    apres = m_symbols._bars_between(closed, 14.0, 24.0)
    assert len(apres) == 1, "le scénario doit tester la PREMIÈRE barre postérieure à l'opening range"
    c = fn(spec, snap)
    assert c is not None and c.side is Side.BUY
    _check_candidate(c, spec, snap)


def test_m09_refuse_une_cassure_contre_un_gap_significatif(specs, broker_specs):
    """Le filtre propre de M09 (cohérence gap / cassure) reste actif : cassure haussière
    après un gap baissier significatif → aucun signal."""
    assert screeners.SCREENERS["M09"](specs["M09"], _snap_m09(broker_specs, gap_contraire=True)) is None


# ---------------------------------------------------------------------------------- M11 : profil de la veille
def _snap_m11(broker_specs):
    """Vendredi (profil de volume concentré autour de 18 000) puis lundi : le week-end est réellement absent."""
    ven = datetime(2026, 1, 16, 16, 0, tzinfo=timezone.utc)
    lun = datetime(2026, 1, 19, 0, 0, tzinfo=timezone.utc)
    times = [ven + timedelta(minutes=15 * k) for k in range(30)] + [lun + timedelta(minutes=15 * k) for k in range(37)]
    bars = []
    for i in range(30):
        o = 18000.0 + (2.0 if i % 2 else -2.0)
        c = 18000.0 - (2.0 if i % 2 else -2.0)
        bars.append((o, max(o, c) + 5.0, min(o, c) - 5.0, c, 3000 if 8 <= i <= 20 else 600))
    for i in range(34):
        o = 18040.0 + (2.0 if i % 2 else -2.0)
        c = 18040.0 - (2.0 if i % 2 else -2.0)
        bars.append((o, max(o, c) + 5.0, min(o, c) - 5.0, c, 1000))
    bars[-1] = (18040.0, 18046.0, 18034.0, 18038.0, 1000)
    bars.append((18000.0, 18014.0, 17996.0, 18011.0, 2500))      # signal : revient tester la zone de valeur
    bars.append((18011.0, 18016.0, 18008.0, 18013.0, 900))
    m15 = _frame_aux_dates(bars, times)
    h1 = _h1_tendance(17900.0, datetime(2026, 1, 19, 9, 0, tzinfo=timezone.utc), pas=1.0)
    return _snap(broker_specs["indices"], m15, h1, _d1(18000.0, datetime(2026, 1, 19, 0, 0, tzinfo=timezone.utc)),
                 atr_h1=25.0, symbole="GER40")


def test_m11_utilise_le_profil_de_la_derniere_journee_cotee(specs, broker_specs):
    """Un lundi, M11 doit travailler sur le profil du VENDREDI, pas sur un dimanche vide.

    Avec « jour courant − 1 jour », `_volume_profile` recevait une fenêtre vide et l'agent renvoyait `None`
    chaque lundi : une stratégie « ouverture de Londres » silencieusement absente un jour d'ouverture sur cinq.
    """
    spec, fn = specs["M11"], screeners.SCREENERS["M11"]
    snap = _snap_m11(broker_specs)
    closed = snap.frames["M15"].iloc[:-1]
    assert str(closed["time"].iloc[-1])[:10] == "2026-01-19"
    veille = m_symbols._prev_day(closed)
    assert len(veille) == 30 and str(veille["time"].iloc[0])[:10] == "2026-01-16"
    c = fn(spec, snap)
    assert c is not None, "le profil de la dernière journée cotée doit permettre un signal le lundi"
    _check_candidate(c, spec, snap)


# ---------------------------------------------------------------------------------- M12 : range asiatique + OBV
def _bars_m12():
    bars = [((0.65005, 0.65025, 0.64975, 0.64995, 800) if i % 2 else (0.64995, 0.65025, 0.64975, 0.65005, 800))
            for i in range(52)]
    for i in range(16):                                   # 22:00 → 01:45 : range étroit, accumulation
        o = 0.65000 + (0.00005 if i % 2 else -0.00005)
        c = 0.65000 + (0.00010 if i % 2 else -0.00002)
        bars.append((o, max(o, c) + 0.00025, min(o, c) - 0.00025, c, 900))
    bars.append((0.65010, 0.65090, 0.65005, 0.65085, 3000))    # 02:00 : première clôture hors du range
    bars.append((0.65085, 0.65110, 0.65070, 0.65095, 900))
    return bars


def _snap_m12(broker_specs):
    end = datetime(2026, 1, 20, 2, 0, tzinfo=timezone.utc)
    return _snap(broker_specs["forex"], _frame(_bars_m12(), 15, end), _h1_plat(0.65000, end),
                 _d1(0.65000, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)), atr_h1=0.00060, symbole="AUDUSD")


def test_m12_accepte_la_premiere_barre_apres_le_range(specs, broker_specs):
    """Comme M09 : la première clôture postérieure au range de nuit est le signal, pas la suivante."""
    spec, fn = specs["M12"], screeners.SCREENERS["M12"]
    snap = _snap_m12(broker_specs)
    closed = snap.frames["M15"].iloc[:-1]
    assert len(m_symbols._bars_between(closed, 2.0, 24.0)) == 1
    c = fn(spec, snap)
    assert c is not None and c.side is Side.BUY
    _check_candidate(c, spec, snap)


# ---------------------------------------------------------------------------------- M13 : pression de mèches
def _bars_m13(inverse=False, signal_sans_meche=False, deja_converti=False):
    """Fenêtre d'absorption : mèches basses longues, corps courts, dérive quasi nulle, puis conversion."""
    bars = _bruit(1.10000, 50, amp_mult=3.0)
    p = 1.10000
    for i in range(19):
        o = p
        c = p + (0.00015 if i % 2 else -0.00005)
        lo, hi = min(o, c) - 0.0012, max(o, c) + 0.00010
        if inverse:
            lo, hi = min(o, c) - 0.00010, max(o, c) + 0.0012
        bars.append((o, hi, lo, c, 1000))
        p = c + (0.0008 if deja_converti else 0.0)
    o, c = p - 0.0002, p + 0.0006
    lo, hi = (o - 0.00005 if signal_sans_meche else o - 0.0009), c + 0.00005
    if inverse:
        o, c = p + 0.0002, p - 0.0006
        hi, lo = o + 0.0009, c - 0.00005
    bars.append((o, hi, lo, c, 1200))
    bars.append((c, c + 0.0004, c - 0.0004, c + 0.0001, 900))
    return bars


def _snap_m13(broker_specs, **kw):
    end = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)
    return _snap(broker_specs["forex"], _frame(_bars_m13(**kw), 15, end), _h1_tendance(1.09000, end, pas=0.00015),
                 _d1(1.10000, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)))


def test_m13_signale_sur_la_pression_de_meches(specs, broker_specs):
    """M13 décide sur la géométrie interne des bougies et ancre son stop sur la barre d'absorption.

    Le moteur précédent (canal de Donchian 20 / stop sur le Donchian 10 opposé) était celui de L07 à la
    virgule près : mêmes bornes, même stop de canal, même projection de largeur. Ici aucune borne n'est
    franchie — la fenêtre est une consolidation — et le signal vient des mèches seules.
    """
    spec, fn = specs["M13"], screeners.SCREENERS["M13"]
    snap = _snap_m13(broker_specs)
    closed = snap.frames["M15"].iloc[:-1]
    fenetre = closed.iloc[-20:]
    pression = m_symbols._wick_pressure(fenetre)
    assert pression is not None and pression[0] >= 0.25, "le scénario doit reposer sur un déséquilibre de mèches"
    c = fn(spec, snap)
    assert c is not None and c.side is Side.BUY
    _check_candidate(c, spec, snap)
    assert c.sl < float(fenetre["low"].iloc[-6:].min()) + 1e-9


def test_m13_refuse_une_pression_absente_ou_inverse(specs, broker_specs):
    """Mèches inversées, barre de signal sans mèche, absorption déjà convertie en déplacement → aucun signal."""
    spec, fn = specs["M13"], screeners.SCREENERS["M13"]
    for libelle, kw in (("mèches inversées", dict(inverse=True)),
                        ("signal sans mèche d'absorption", dict(signal_sans_meche=True)),
                        ("absorption déjà convertie", dict(deja_converti=True))):
        assert fn(spec, _snap_m13(broker_specs, **kw)) is None, f"M13 a accepté : {libelle}"


def test_wick_pressure_refuse_une_fenetre_sans_meche():
    """`_wick_pressure` renvoie None (division par zéro) au lieu d'un déséquilibre inventé."""
    plate = pd.DataFrame({"open": [1.0] * 12, "high": [1.0] * 12, "low": [1.0] * 12, "close": [1.0] * 12})
    assert m_symbols._wick_pressure(plate) is None
    assert m_symbols._wick_pressure(plate.iloc[:3]) is None


def _bars_m13_donchian():
    """Canal plat à mèches SYMÉTRIQUES puis cassure franche du plus haut 20 barres, clôture sur ses hauts.

    C'est exactement la configuration que prenait le moteur précédent de M13 (canal de Donchian 20, stop sur le
    Donchian 10 opposé) — et celui de L07. Le moteur actuel n'y voit aucune absorption : il doit refuser.
    """
    bars = [((1.10011, 1.10035, 1.09985, 1.09989, 900) if i % 2 else (1.09989, 1.10035, 1.09985, 1.10011, 900))
            for i in range(69)]
    bars.append((1.10010, 1.10140, 1.10008, 1.10135, 3000))     # cassure nette, sans mèche d'absorption
    bars.append((1.10135, 1.10160, 1.10120, 1.10145, 900))
    return bars


def test_m13_ne_reprend_pas_le_moteur_de_canal_de_donchian(specs, broker_specs):
    """Une cassure de canal de Donchian sans pression de mèches ne doit PAS déclencher M13.

    Garde-fou de distinction : deux agents du projet (ici et L07) ne peuvent pas décider sur le même objet.
    """
    spec, fn = specs["M13"], screeners.SCREENERS["M13"]
    end = datetime(2026, 1, 20, 15, 0, tzinfo=timezone.utc)
    m15 = _frame(_bars_m13_donchian(), 15, end)
    closed = m15.iloc[:-1]
    entry = float(closed["close"].iloc[-1])
    assert entry > float(closed["high"].iloc[-21:-1].max()), "le scénario doit être une vraie cassure de Donchian 20"
    snap = _snap(broker_specs["forex"], m15, _h1_tendance(1.09000, end, pas=0.00015),
                 _d1(1.10000, datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)))
    assert fn(spec, snap) is None, "M13 a signalé une simple cassure de canal (moteur de L07)"


# ------------------------------------------------------------------------------------------------------------
# Les scénarios ci-dessus servent de socle aux contrôles transverses : un agent qui SIGNALE prouve que les
# filtres testés plus bas rejettent vraiment, au lieu de constater un `None` déjà acquis.
# ------------------------------------------------------------------------------------------------------------
SCENARIOS = {"M04": _snap_m04, "M06": _snap_m06, "M08": _snap_m08, "M09": _snap_m09, "M11": _snap_m11,
             "M12": _snap_m12, "M13": _snap_m13}


@pytest.mark.parametrize("agent_id", sorted(SCENARIOS))
def test_scenarios_de_reference_declenchent_bien(agent_id, specs, broker_specs):
    """Chaque scénario de référence produit un candidat COMPLET et valide (socle des tests transverses)."""
    snap = SCENARIOS[agent_id](broker_specs)
    c = screeners.SCREENERS[agent_id](specs[agent_id], snap)
    assert c is not None, f"{agent_id} : scénario de référence muet, les tests transverses ne prouveraient rien"
    _check_candidate(c, specs[agent_id], snap)


@pytest.mark.parametrize("agent_id", sorted(SCENARIOS))
def test_scenario_atr_h1_non_exploitable_annule_le_signal(agent_id, specs, broker_specs):
    """Sur un scénario qui SIGNALE, un ATR H1 NaN ou nul doit annuler le candidat.

    Sans la normalisation de `_atr_h1`, le filtre de spread comparait avec NaN (toujours faux), `_build`
    publiait `atr=NaN` et `validate_stop_loss` sautait ses bornes en ATR : le candidat passait entièrement.
    """
    snap = SCENARIOS[agent_id](broker_specs)
    assert screeners.SCREENERS[agent_id](specs[agent_id], snap) is not None
    for valeur in (float("nan"), 0.0):
        casse = SCENARIOS[agent_id](broker_specs)
        casse.atr_h1 = valeur
        assert screeners.SCREENERS[agent_id](specs[agent_id], casse) is None, f"{agent_id} : ATR H1 {valeur} accepté"


@pytest.mark.parametrize("agent_id", sorted(SCENARIOS))
def test_scenario_barre_de_signal_sans_prix(agent_id, specs, broker_specs):
    """Sur un scénario qui SIGNALE, des prix NaN sur la barre de décision doivent annuler le candidat."""
    snap = SCENARIOS[agent_id](broker_specs)
    assert screeners.SCREENERS[agent_id](specs[agent_id], snap) is not None
    for tf in ("M15", "H1"):
        casse = SCENARIOS[agent_id](broker_specs)
        abime = casse.frames[tf].copy()
        i = len(abime) - 2
        abime.loc[i, "high"] = float("nan")
        abime.loc[i, "low"] = float("nan")
        casse.frames[tf] = abime
        assert screeners.SCREENERS[agent_id](specs[agent_id], casse) is None, f"{agent_id}/{tf} : barre sans prix acceptée"


@pytest.mark.parametrize("agent_id", sorted(SCENARIOS))
def test_scenario_sans_lookahead(agent_id, specs, broker_specs):
    """Sur un scénario qui SIGNALE, écraser la barre EN FORMATION ne doit rien changer à la décision."""
    spec, fn = specs[agent_id], screeners.SCREENERS[agent_id]
    avant = _signature(fn(spec, SCENARIOS[agent_id](broker_specs)))
    assert avant is not None
    apres_snap = SCENARIOS[agent_id](broker_specs)
    for tf, df in apres_snap.frames.items():
        if len(df) < 2:
            continue
        modifie = df.copy()
        i = len(modifie) - 1
        bond = float(modifie["atr14"].iloc[-2]) * 5
        modifie.loc[i, "close"] = float(modifie["close"].iloc[-1]) + bond
        modifie.loc[i, "high"] = float(modifie["close"].iloc[i]) + bond
        modifie.loc[i, "low"] = float(modifie["close"].iloc[i]) - 3 * bond
        modifie.loc[i, "tick_volume"] = float(modifie["tick_volume"].iloc[-1]) * 20
        apres_snap.frames[tf] = modifie
    assert _signature(fn(spec, apres_snap)) == avant, f"{agent_id} : la barre en formation a changé la décision"


def test_agents_du_module_ne_sont_pas_des_copies(specs, broker_specs):
    """Aucun scénario ne doit déclencher deux agents du module avec la MÊME décision.

    Deux agents qui produisent le même couple (sens, entrée, SL, plan de TP) sur la même donnée sont des copies :
    ils occuperaient deux places du registre pour une seule idée, et le dédoublonnage du Market Router leur
    accorderait un bonus de concordance imméritée.
    """
    vus = {}
    for nom, builder in SCENARIOS.items():
        snap = builder(broker_specs)
        for agent_id in AGENT_IDS:
            c = screeners.SCREENERS[agent_id](specs[agent_id], snap)
            if c is None:
                continue
            cle = (nom, c.side, round(c.entry, 8), round(c.sl, 8), tuple(round(x, 8) for x in c.tp_plan))
            assert cle not in vus, f"{agent_id} et {vus[cle]} produisent la même décision sur le scénario {nom}"
            vus[cle] = agent_id


def test_finalize_refuse_un_candidat_dont_le_risque_n_est_pas_mesurable(specs, broker_specs):
    """`_finalize` refuse aussi un candidat dont l'ATR H1 publié n'est pas exploitable (défense en profondeur).

    `validate_stop_loss` ignore silencieusement ses bornes en ATR quand `atr` vaut NaN (toute comparaison avec
    NaN est fausse) ; le `TradeCandidate.atr` sert ensuite au dimensionnement du risque et au trailing. Même si
    le filtre de spread attrape déjà le cas en amont, le dernier verrou du module doit tenir seul.
    """
    import types

    snap = _snap_m06(broker_specs)
    c = screeners.SCREENERS["M06"](specs["M06"], snap)
    assert c is not None
    for valeur in (float("nan"), 0.0, None):
        faux = types.SimpleNamespace(atr_h1=valeur, spec=snap.spec)
        c.atr = 0.0 if valeur in (0.0, None) else float("nan")
        assert m_symbols._finalize(c, faux) is None, f"ATR H1 {valeur} : candidat accepté sans risque mesurable"
    c.atr = float(snap.atr_h1)
    assert m_symbols._finalize(c, snap) is c
