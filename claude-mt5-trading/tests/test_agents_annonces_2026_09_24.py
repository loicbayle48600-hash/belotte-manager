"""Agents de trading d'annonces (famille K, demande utilisateur 2026-09-24 : « mets plusieurs agents dessus au lieu de 2 »).

Les deux agents K historiques réutilisaient des screeners génériques ; cinq vraies stratégies d'annonces sont ajoutées
(K03-K07), déclenchées par l'annonce RÉELLE du calendrier (`snap.news_events`), en SHADOW uniquement. Le routeur
écartait tout symbole en NEWS_SHOCK : en routage SHADOW, seuls les agents d'annonces y tournent désormais ; en
routage LIVE rien ne change (jamais de trade réel en choc de news).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.agents.news_strategies import central_bank_drift, news_follow, news_range_break  # noqa: E402
from tradinglab.agents.registry import AgentRegistry  # noqa: E402
from tradinglab.core.types import AgentStatus, Regime, Side  # noqa: E402
from tradinglab.market_data.feed import MarketDataFeed  # noqa: E402
from tradinglab.market_data.indicators import last_closed  # noqa: E402


@pytest.fixture
def snap(broker):
    return MarketDataFeed(broker, ["M5", "M15", "H1"], bars=300).snapshot("EURUSD", now=broker.now())


def _impulsion(snap, atr_mult: float, event_bars_ago: int = 3):
    """Annonce il y a `event_bars_ago` barres M5 ; depuis, le prix monte régulièrement jusqu'à `atr_mult` ATR."""
    e = snap.frames["M5"].copy()
    atr = float(last_closed(e)["atr14"])
    ref = float(e["close"].iloc[-event_bars_ago - 1])
    n = event_bars_ago
    # la bougie de l'annonce part du prix de référence, les suivantes montent jusqu'à `atr_mult` ATR
    for k, idx in enumerate(range(len(e) - n, len(e)), start=0):
        px = ref + atr * atr_mult * min(1.0, k / max(1, n - 2))
        e.loc[e.index[idx], ["open", "high", "low", "close"]] = [px - 0.1 * atr, px + 0.1 * atr, px - 0.2 * atr, px]
    snap.frames["M5"] = e
    t_ev = pd.Timestamp(e["time"].iloc[-n])
    snap.news_events = [{"ts": t_ev.isoformat(), "minutes_ago": 5.0 * (n - 1), "title": "Non-Farm Payrolls",
                         "currency": "USD", "central_bank": False, "surprise": 120.0}]
    return ref, atr


def test_sept_agents_d_annonces_en_shadow():
    """2026-09-26 (décision utilisateur, option news FOXX) : K03-K07 passent LIVE et tradent les annonces, K08-K12
    (spécialistes crypto) s'y ajoutent ; K01-K02 restent en SHADOW."""
    reg = AgentRegistry()
    k = {a.agent_id: a for a in reg.agents.values() if a.agent_id.startswith("K")}
    assert sorted(k) == [f"K{i:02d}" for i in range(1, 13)]
    assert all(a.news_sensitive for a in k.values())
    assert all(k[i].status == AgentStatus.SHADOW.value for i in ("K01", "K02"))
    assert all(k[f"K{i:02d}"].status == AgentStatus.LIVE.value and k[f"K{i:02d}"].news_trader for i in range(3, 13))
    assert all(Regime.NEWS_SHOCK.value in k[f"K{i:02d}"].regimes for i in range(3, 13))


def test_news_follow_suit_l_impulsion_apres_l_annonce(snap):
    spec = AgentRegistry().get("K03")
    ref, atr = _impulsion(snap, 2.0)
    c = news_follow(spec, snap)
    assert c is not None and c.side is Side.BUY and c.agent_id == "K03"
    assert c.sl < ref and "Non-Farm Payrolls" in c.arguments_for[0]


def test_sans_annonce_ou_hors_fenetre_aucun_signal(snap):
    spec = AgentRegistry().get("K03")
    _impulsion(snap, 2.0)
    ev = snap.news_events
    snap.news_events = []
    assert news_follow(spec, snap) is None, "aucune annonce : jamais de signal (rien d'inventé)"
    snap.news_events = [dict(ev[0], minutes_ago=90.0)]
    assert news_follow(spec, snap) is None, "hors fenêtre 2-15 min"
    snap.news_events = ev
    _impulsion(snap, 0.3)
    assert news_follow(spec, snap) is None, "impulsion trop faible"


def test_banque_centrale_seulement_pour_central_bank_drift(snap):
    spec = AgentRegistry().get("K07")
    _impulsion(snap, 2.0)
    snap.news_events = [dict(snap.news_events[0], minutes_ago=90.0)]
    assert central_bank_drift(spec, snap) is None, "annonce qui n'est pas une banque centrale"


def test_range_break_casse_du_range_pre_annonce(snap):
    spec = AgentRegistry().get("K05")
    ref, atr = _impulsion(snap, 3.0)
    snap.news_events = [dict(snap.news_events[0], minutes_ago=20.0)]
    e = snap.frames["M5"]
    pre = e.iloc[-3 - 12:-3]                                          # les 12 barres M5 avant l'annonce
    e.loc[pre.index, ["high", "low"]] = [ref + 0.3 * atr, ref - 0.3 * atr]      # range serré avant l'annonce
    c = news_range_break(spec, snap)
    assert c is not None and c.side is Side.BUY and abs(c.sl - ref) < 0.05 * atr


def test_routeur_news_shock_seulement_agents_d_annonces_en_shadow(snap, monkeypatch):
    from tradinglab.orchestration import market_router as mr

    reg = AgentRegistry()
    snap.regime.regime = Regime.NEWS_SHOCK
    vus: list[str] = []
    monkeypatch.setattr(mr, "run_screener", lambda a, s: vus.append(a.agent_id) or None)
    router = mr.MarketRouter(reg)
    # 2026-09-26 (décision utilisateur) : en LIVE, seuls les agents qui tradent les annonces tournent en choc de news
    _, rep = router.route({"EURUSD": snap}, (AgentStatus.LIVE.value,))
    assert vus and set(vus) <= {"K03", "K04", "K05", "K06", "K07"}, "forex : les spécialistes crypto K08-K12 n'y vont pas"
    assert all(reg.get(a).news_trader for a in vus)
