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


def _scenario_post_news(snap, sgn: int = 1, decoy=None):
    """Injecte dans le frame M15 un motif post-événement : barre de choc, base de digestion, sortie de base.

    `sgn` = +1 pour un motif haussier, -1 pour son miroir baissier (couverture du chemin VENTE).
    `decoy` = (offset, amplitude, corps) optionnel : une barre PARASITE plus ample que le choc, placée `offset`
    barres avant la barre de signal, pour vérifier que la sélection du choc ne se laisse pas piéger.

    Seules des barres DÉJÀ CLÔTURÉES sont réécrites (la dernière ligne, barre en formation, n'est pas touchée)
    et les horodatages sont conservés : le scénario reste cohérent avec le reste du snapshot.
    """
    from tradinglab.market_data.indicators import enrich

    df = snap.frames["M15"][["time", "open", "high", "low", "close", "tick_volume", "spread"]].copy().reset_index(drop=True)
    n = len(df)
    ref = float((df["high"] - df["low"]).iloc[-80:-10].median())
    if decoy is not None:                       # barre parasite AVANT le choc (elle reste une barre clôturée)
        off, amp, body = decoy
        d = n - 2 - off
        d0 = float(df["close"].iloc[d - 1])
        df.loc[d, ["open", "high", "low", "close"]] = [d0, d0 + amp * ref, d0 - 0.05 * ref, d0 + body * ref]
    i = n - 7                                   # barre de choc : 5 barres avant la barre de signal
    p0 = float(df["close"].iloc[i - 1])
    hi, lo = (2.4, 0.1) if sgn > 0 else (0.1, 2.4)
    df.loc[i, ["open", "high", "low", "close"]] = [p0, p0 + hi * ref, p0 - lo * ref, p0 + sgn * 2.0 * ref]
    base = float(df["close"].iloc[i])
    for k in range(1, 5):                       # digestion : 4 barres étroites qui conservent le déplacement
        cc = base - sgn * 0.05 * ref
        up, dn = (0.25, 0.30) if sgn > 0 else (0.30, 0.25)
        df.loc[i + k, ["open", "high", "low", "close"]] = [base, base + up * ref, base - dn * ref, cc]
        base = cc
    ext = float(df["high"].iloc[i + 1:i + 5].max()) if sgn > 0 else float(df["low"].iloc[i + 1:i + 5].min())
    sig = i + 5                                 # barre de signal = dernière barre clôturée (indice n-2)
    up, dn = (0.6, 0.15) if sgn > 0 else (0.15, 0.6)
    df.loc[sig, ["open", "high", "low", "close"]] = [base, ext + up * ref, ext - dn * ref, ext + sgn * 0.45 * ref]
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


# --------------------------------------------------------------------------------------------------------------
# Tests de non-régression des corrections de relecture
# --------------------------------------------------------------------------------------------------------------
def _with_params(spec, **params):
    """Copie du spec avec des paramètres surchargés (même motif que `ema_pullback` dans screeners.py)."""
    from tradinglab.agents.registry import AgentSpec

    return AgentSpec(**{**spec.to_dict(), "params": {**spec.params, **params}})


def test_bound_sl_refuse_un_sl_du_mauvais_cote():
    """Un SL brut du mauvais côté doit être REFUSÉ, jamais retourné du bon côté (ce serait un stop inventé)."""
    import types

    from tradinglab.agents.strategies import k_news

    snap = types.SimpleNamespace(atr_h1=0.0)
    # BUY : un SL brut au-dessus de l'entrée ne doit pas devenir un SL sous l'entrée
    assert k_news._bound_sl(snap, Side.BUY, 100.0, 101.0, 1.0, 0.5, 1.5) is None
    assert k_news._bound_sl(snap, Side.BUY, 100.0, 100.0, 1.0, 0.5, 1.5) is None
    assert k_news._bound_sl(snap, Side.SELL, 100.0, 99.0, 1.0, 0.5, 1.5) is None
    # cas normaux : côté conservé et distance ramenée dans les bornes
    sl = k_news._bound_sl(snap, Side.BUY, 100.0, 99.9, 1.0, 0.5, 1.5)
    assert sl is not None and 100.0 - sl == pytest.approx(0.5)
    sl = k_news._bound_sl(snap, Side.SELL, 100.0, 110.0, 1.0, 0.5, 1.5)
    assert sl is not None and sl - 100.0 == pytest.approx(1.5)
    # bornes incohérentes (ATR H1 énorme devant l'ATR du tf d'entrée) → refus, pas de stop bricolé
    assert k_news._bound_sl(types.SimpleNamespace(atr_h1=100.0), Side.BUY, 100.0, 99.0, 1.0, 0.5, 1.5) is None


def test_shock_bar_ignore_le_doji_le_plus_ample():
    """`_shock_bar` doit retenir la barre de choc DIRECTIONNELLE, pas seulement la plus ample de la fenêtre."""
    import pandas as pd

    from tradinglab.agents.strategies import k_news

    n, ref = 200, 60
    rows = []
    for i in range(n):
        rows.append({"open": 100.0, "high": 100.4, "low": 99.6, "close": 100.0})   # amplitude de référence 0,8
    df = pd.DataFrame(rows)
    # fenêtre examinée (oldest=8, newest=3) : indices n-8 .. n-4
    df.loc[n - 8, ["open", "high", "low", "close"]] = [100.0, 104.0, 96.0, 100.1]   # doji très ample (corps 1 %)
    df.loc[n - 6, ["open", "high", "low", "close"]] = [100.0, 103.0, 99.8, 102.8]   # vrai choc directionnel
    out = k_news._shock_bar(df, 8, 3, ref, 1.8, 0.45)
    assert out is not None, "le vrai choc directionnel n'est pas retenu"
    j, rng, ratio = out
    assert j == n - 6 and rng == pytest.approx(3.2) and ratio > 1.8
    # sans barre directionnelle qualifiante, aucune barre n'est inventée
    df2 = df.copy()
    df2.loc[n - 6, ["open", "high", "low", "close"]] = [100.0, 103.0, 99.8, 100.1]
    assert k_news._shock_bar(df2, 8, 3, ref, 1.8, 0.45) is None
    # fenêtre incohérente ou historique trop court → None, jamais d'exception
    assert k_news._shock_bar(df, 3, 3, ref, 1.8, 0.45) is None
    assert k_news._shock_bar(df.iloc[:40], 8, 3, ref, 1.8, 0.45) is None


@pytest.mark.parametrize("decoy,label", [
    ((8, 3.0, 2.8), "barre plus ample hors de la fenêtre utile (base trop longue)"),
    ((7, 3.0, 0.2), "doji plus ample à l'intérieur de la fenêtre"),
])
def test_k01_choc_selectionne_malgre_une_barre_parasite(specs, snapshots, decoy, label):
    """Une barre plus ample mais inutilisable ne doit pas faire disparaître un motif post-événement valide."""
    spec, fn = specs["K01"], screeners.SCREENERS["K01"]
    found = 0
    for snap0 in snapshots[0].values():
        snap = _scenario_post_news(copy.deepcopy(snap0), decoy=decoy)
        c = fn(spec, snap)
        if c is not None:
            found += 1
            assert c.side is Side.BUY
            _check_candidate(c, spec, snap)
    assert found >= 1, f"K01 aveuglé par : {label}"


def test_k01_scenario_post_news_vente(specs, snapshots):
    """Miroir baissier du motif post-événement : le chemin VENTE de K01 doit être exercé et valide."""
    spec, fn = specs["K01"], screeners.SCREENERS["K01"]
    found = 0
    for snap0 in snapshots[0].values():
        snap = _scenario_post_news(copy.deepcopy(snap0), sgn=-1)
        c = fn(spec, snap)
        if c is not None:
            found += 1
            assert c.side is Side.SELL
            assert c.sl > c.entry
            _check_candidate(c, spec, snap)
    assert found >= 1, "K01 ne reconnaît pas le motif post-événement baissier"


def test_k01_score_mesure_les_seuils_reellement_utilises(specs, snapshots):
    """Les composantes de score de K01 doivent se référer aux seuils du FILTRE, pas à d'autres valeurs par défaut."""
    spec, fn = specs["K01"], screeners.SCREENERS["K01"]
    snap = _scenario_post_news(copy.deepcopy(next(iter(snapshots[0].values()))))
    base = fn(spec, snap)
    assert base is not None
    # seuils explicitement fixés à leur valeur par défaut → score inchangé (aucun second défaut caché)
    same = fn(_with_params(spec, shock_mult=1.8, contract_max=0.65, shock_body=0.45), snap)
    assert same is not None and same.setup_score == base.setup_score
    # seuils abaissés → le setup est plus loin du seuil, donc mieux noté sur ces composantes
    looser = fn(_with_params(spec, shock_mult=1.2, contract_max=0.95), snap)
    assert looser is not None and looser.setup_score > base.setup_score


def test_k02_score_mesure_les_seuils_reellement_utilises(specs, snapshots):
    """Idem pour K02 : la composante « part des corps » doit suivre `body_share`, pas une constante figée."""
    spec, fn = specs["K02"], screeners.SCREENERS["K02"]
    found = None
    for snaps in snapshots:
        for snap in snaps.values():
            if fn(spec, snap) is not None:
                found = snap
                break
        if found is not None:
            break
    assert found is not None, "aucun candidat K02 sur les snapshots : le test de score ne peut pas être exercé"
    base = fn(spec, found)
    assert fn(_with_params(spec, body_share=0.55, giveback_max=0.45, atr_ratio=1.5), found).setup_score == base.setup_score
    looser = fn(_with_params(spec, body_share=0.20, giveback_max=0.90, atr_ratio=0.5), found)
    assert looser is not None and looser.setup_score > base.setup_score


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_deterministe(agent_id, specs, snapshots):
    """Deux évaluations du même snapshot donnent exactement le même résultat (aucune source d'aléa)."""
    spec, fn = specs[agent_id], screeners.SCREENERS[agent_id]
    for snaps in snapshots:
        for snap in snaps.values():
            assert _signature(fn(spec, snap)) == _signature(fn(spec, snap))


@pytest.mark.parametrize("agent_id", AGENT_IDS)
@pytest.mark.parametrize("degrade", ["nan_all", "nan_tail", "court", "une_barre", "vide", "plat"])
def test_donnees_degradees_donnent_none(agent_id, specs, snapshots, degrade):
    """NaN, frames courtes/vides, prix constant : `None` sans exception, jamais une valeur inventée."""
    import numpy as np

    spec, fn = specs[agent_id], screeners.SCREENERS[agent_id]
    for snap0 in snapshots[0].values():
        snap = copy.deepcopy(snap0)
        for tf, df in list(snap.frames.items()):
            if degrade == "nan_all":
                for col in ("open", "high", "low", "close", "atr14", "rsi14"):
                    if col in df:
                        df[col] = np.nan
            elif degrade == "nan_tail":
                for col in ("open", "high", "low", "close"):
                    if col in df:
                        df.loc[df.index[-15:], col] = np.nan
            elif degrade == "court":
                snap.frames[tf] = df.iloc[:3].copy()
            elif degrade == "une_barre":
                snap.frames[tf] = df.iloc[:1].copy()
            elif degrade == "vide":
                snap.frames[tf] = df.iloc[:0].copy()
            elif degrade == "plat":
                for col in ("open", "high", "low", "close"):
                    if col in df:
                        df[col] = 1.0
                if "atr14" in df:
                    df["atr14"] = 0.0
        assert fn(spec, snap) is None


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_snapshot_sans_spec_ni_atr_h1(agent_id, specs, snapshots):
    """Sans `spec` symbole ni ATR H1, le spread est inconnu : refus déterministe, aucune exception."""
    spec, fn = specs[agent_id], screeners.SCREENERS[agent_id]
    for snap0 in snapshots[0].values():
        for attr, value in (("spec", None), ("atr_h1", 0.0), ("spread_points", 10 ** 6)):
            snap = copy.deepcopy(snap0)
            setattr(snap, attr, value)
            c = fn(spec, snap)
            if c is not None:
                _check_candidate(c, spec, snap)


def test_k02_arguments_contre_disent_que_le_z_score_nest_pas_une_probabilite(specs, snapshots):
    """Honnêteté : K02 doit signaler que son z-score est descriptif (fenêtres chevauchantes, queues épaisses)."""
    spec, fn = specs["K02"], screeners.SCREENERS["K02"]
    seen = 0
    for snaps in snapshots:
        for snap in snaps.values():
            c = fn(spec, snap)
            if c is None:
                continue
            seen += 1
            texte = " ".join(c.arguments_against).lower()
            assert "probabilité" in texte and "chevauchantes" in texte
            assert 0 <= c.setup_score <= 100
    assert seen >= 1, "aucun candidat K02 : l'honnêteté des arguments n'a pas pu être vérifiée"
