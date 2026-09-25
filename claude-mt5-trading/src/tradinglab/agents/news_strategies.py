"""Stratégies de trading d'annonces économiques (famille K, demande utilisateur 2026-09-24).

Toutes sont déclenchées par une annonce RÉELLE du calendrier (importance HIGH, devise du symbole), fournie par
l'orchestrateur dans ``snap.news_events`` : jamais d'annonce supposée. Elles tournent en **SHADOW** (positions
virtuelles, aucun ordre) : le but est de mesurer, sur au moins 40 trades, si l'une d'elles est rentable avant
toute décision de l'utilisateur (option FOXX « News Trading » +25 %).

Cinq approches distinctes, pour comparer :
- ``news_follow``        : suivre la première impulsion (2 à 15 min après) quand elle dépasse 1 ATR M5 ;
- ``news_fade``          : parier contre un pic excessif (≥ 2,5 ATR) qui se retourne, 10 à 60 min après ;
- ``news_range_break``   : cassure du range de l'heure précédant l'annonce, 5 à 45 min après ;
- ``news_trend_resume``  : reprise de la tendance H1 dans le sens de l'annonce, sur repli à l'EMA20 M15 (30-120 min) ;
- ``central_bank_drift`` : dérive après une décision de banque centrale, 60 à 360 min après, tendance H1 alignée.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core.types import Side, TradeCandidate
from ..market_data.indicators import last_closed
from .registry import AgentSpec
from .screeners import _build, _frame, _trend_of, _valid, register


def _ts(v) -> Optional[pd.Timestamp]:
    try:
        t = pd.Timestamp(v)
    except (TypeError, ValueError):
        return None
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _news_ctx(spec: AgentSpec, snap, min_after: float, max_after: float, central_bank_only: bool = False):
    """(annonce, minutes écoulées, frame d'entrée, frame de tendance, dernière barre close, atr, bar_time, barres
    depuis l'annonce) ou None. L'annonce retenue est la plus récente dans la fenêtre [min_after, max_after]."""
    events = [ev for ev in (getattr(snap, "news_events", None) or [])
              if min_after <= float(ev.get("minutes_ago", -1)) <= max_after and (ev.get("central_bank") or not central_bank_only)]
    if not events:
        return None
    ev = min(events, key=lambda x: float(x["minutes_ago"]))
    e = _frame(snap, spec.timeframes.get("entry", "M5"))
    t = _frame(snap, spec.timeframes.get("trend", "H1"))
    if e is None or t is None:
        return None
    le, lt = last_closed(e), last_closed(t)
    if not _valid(le, "atr14", "ema20", "rsi14") or not _valid(lt, "ema20", "ema50", "ema200"):
        return None
    atr = float(le["atr14"])
    t0 = _ts(ev.get("ts"))
    if atr <= 0 or t0 is None:
        return None
    closed = e.iloc[:-1]
    times = pd.to_datetime(closed["time"], utc=True)
    since = closed[times >= t0]
    before = closed[times < t0]
    if since.empty or before.empty:
        return None
    return ev, float(ev["minutes_ago"]), e, t, le, lt, atr, str(le["time"]), since, before


def _cons(ev: dict) -> list[str]:
    return [f"trade d'annonce ({ev.get('title', '?')}) : spread et slippage élevés possibles"]


@register("news_follow")
def news_follow(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _news_ctx(spec, snap, spec.params.get("min_after", 2), spec.params.get("max_after", 15))
    if not c:
        return None
    ev, mins, e, t, le, lt, atr, bt, since, before = c
    ref = float(before["close"].iloc[-1])
    move = float(le["close"]) - ref
    if abs(move) < spec.params.get("impulse_atr", 1.0) * atr:
        return None
    side = Side.BUY if move > 0 else Side.SELL
    entry = float(le["close"])
    # stop derrière l'extrême opposé depuis l'annonce (le point de départ de l'impulsion)
    sl = float(since["low"].min()) - 0.2 * atr if side is Side.BUY else float(since["high"].max()) + 0.2 * atr
    if abs(entry - sl) > 3 * atr:
        sl = entry - side.sign * 3 * atr
    score = 60 + min(15.0, (abs(move) / atr - 1) * 10)
    return _build(spec, snap, side, entry, sl, spec.params.get("rr", 2.0), score,
                  [f"impulsion {abs(move) / atr:.1f} ATR {mins:.0f} min après « {ev.get('title', '?')} »"], _cons(ev),
                  "retour sous le prix d'avant l'annonce", bt)


@register("news_fade")
def news_fade(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _news_ctx(spec, snap, spec.params.get("min_after", 10), spec.params.get("max_after", 60))
    if not c:
        return None
    ev, mins, e, t, le, lt, atr, bt, since, before = c
    ref = float(before["close"].iloc[-1])
    hi, lo = float(since["high"].max()), float(since["low"].min())
    up, down = hi - ref, ref - lo
    spike = spec.params.get("spike_atr", 2.5) * atr
    if max(up, down) < spike:
        return None
    side = Side.SELL if up >= down else Side.BUY          # contre le pic
    close, open_ = float(le["close"]), float(le["open"])
    reversal = (close < open_) if side is Side.SELL else (close > open_)
    rsi_ok = (le["rsi14"] >= 65) if side is Side.SELL else (le["rsi14"] <= 35)
    if not reversal or not rsi_ok:
        return None
    entry = close
    sl = hi + 0.3 * atr if side is Side.SELL else lo - 0.3 * atr
    if abs(entry - sl) > 3 * atr:
        return None
    score = 58 + min(12.0, (max(up, down) / atr - 2.5) * 6)
    return _build(spec, snap, side, entry, sl, spec.params.get("rr", 1.5), score,
                  [f"pic de {max(up, down) / atr:.1f} ATR après l'annonce, bougie de retournement, RSI {le['rsi14']:.0f}"],
                  _cons(ev), "nouveau plus haut/bas au-delà du pic", bt)


@register("news_range_break")
def news_range_break(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _news_ctx(spec, snap, spec.params.get("min_after", 5), spec.params.get("max_after", 45))
    if not c:
        return None
    ev, mins, e, t, le, lt, atr, bt, since, before = c
    pre = before.iloc[-int(spec.params.get("range_bars", 12)):]      # ~1 h de M5 avant l'annonce
    r_hi, r_lo = float(pre["high"].max()), float(pre["low"].min())
    if r_hi - r_lo <= 0 or r_hi - r_lo > 4 * atr:
        return None
    close = float(le["close"])
    marge = 0.2 * atr
    if close > r_hi + marge:
        side = Side.BUY
    elif close < r_lo - marge:
        side = Side.SELL
    else:
        return None
    mid = (r_hi + r_lo) / 2
    return _build(spec, snap, side, close, mid, spec.params.get("rr", 2.0), 62,
                  [f"cassure du range pré-annonce ({(r_hi - r_lo) / atr:.1f} ATR) {mins:.0f} min après"], _cons(ev),
                  "réintégration du range pré-annonce", bt)


@register("news_trend_resume")
def news_trend_resume(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _news_ctx(spec, snap, spec.params.get("min_after", 30), spec.params.get("max_after", 120))
    if not c:
        return None
    ev, mins, e, t, le, lt, atr, bt, since, before = c
    ref = float(before["close"].iloc[-1])
    move = float(le["close"]) - ref
    if abs(move) < 0.8 * atr:
        return None
    side = Side.BUY if move > 0 else Side.SELL
    tr = _trend_of(lt)
    if (side is Side.BUY and tr != "UP") or (side is Side.SELL and tr != "DOWN"):
        return None
    # repli sur l'EMA20 du tf d'entrée : la barre close a touché l'EMA et a clôturé du bon côté
    ema = float(le["ema20"])
    touche = float(le["low"]) <= ema <= float(le["close"]) if side is Side.BUY else float(le["close"]) <= ema <= float(le["high"])
    if not touche:
        return None
    entry = float(le["close"])
    sl = entry - side.sign * spec.params.get("sl_atr", 1.5) * atr
    return _build(spec, snap, side, entry, sl, spec.params.get("rr", 2.0), 64,
                  [f"annonce dans le sens de la tendance H1, repli EMA20 {mins:.0f} min après"], _cons(ev),
                  "clôture de l'autre côté de l'EMA50", bt)


@register("central_bank_drift")
def central_bank_drift(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _news_ctx(spec, snap, spec.params.get("min_after", 60), spec.params.get("max_after", 360), central_bank_only=True)
    if not c:
        return None
    ev, mins, e, t, le, lt, atr, bt, since, before = c
    ref = float(before["close"].iloc[-1])
    move = float(le["close"]) - ref
    if abs(move) < spec.params.get("drift_atr", 1.5) * atr:
        return None
    side = Side.BUY if move > 0 else Side.SELL
    tr = _trend_of(lt)
    if (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP"):
        return None
    entry = float(le["close"])
    sl = entry - side.sign * spec.params.get("sl_atr", 2.0) * atr
    return _build(spec, snap, side, entry, sl, spec.params.get("rr", 2.5), 63,
                  [f"dérive {abs(move) / atr:.1f} ATR {mins:.0f} min après « {ev.get('title', '?')} » (banque centrale)"],
                  _cons(ev), "retour au prix d'avant la décision", bt)


# convention du dépôt (agents/strategies/*) : la stratégie propre d'un agent est enregistrée sous son agent_id
for _aid, _fn in (("K03", news_follow), ("K04", news_fade), ("K05", news_range_break),
                  ("K06", news_trend_resume), ("K07", central_bank_drift)):
    register(_aid)(_fn)
