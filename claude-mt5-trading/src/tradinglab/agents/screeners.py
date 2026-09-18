"""Screeners déterministes (Python) : transforment un MarketSnapshot en TradeCandidate.

Chaque screener travaille sur la dernière barre CLÔTURÉE (jamais la barre en
formation), calcule entrée / SL (structure + ATR) / plan de TP, un score interne
0-100 (qui n'est PAS une probabilité de gain) et des arguments pour/contre.
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..core.types import Regime, Side, TradeCandidate
from ..market_data.indicators import daily_high_low, last_closed, session_range, structure_label, support_resistance, swing_points
from ..market_data.regime import trend_direction
from .registry import AgentSpec

ScreenerFn = Callable[[AgentSpec, "MarketSnapshot"], Optional[TradeCandidate]]  # noqa: F821
SCREENERS: dict[str, ScreenerFn] = {}


def register(name: str):
    def deco(fn):
        SCREENERS[name] = fn
        return fn
    return deco


# ---------------------------------------------------------------- utilitaires
def _frame(snap, tf: str) -> Optional[pd.DataFrame]:
    df = snap.frames.get(tf)
    if df is None or len(df) < 60:
        return None
    return df


def _valid(row: pd.Series, *cols: str) -> bool:
    return all(col in row and pd.notna(row[col]) for col in cols)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


def _build(spec: AgentSpec, snap, side: Side, entry: float, sl: float, rr: float, score: float, pros: list[str],
           cons: list[str], invalidation: str, bar_time: str) -> Optional[TradeCandidate]:
    dist = abs(entry - sl)
    if dist <= 0 or not np.isfinite(dist):
        return None
    tp1 = entry + side.sign * dist * 1.5
    tp2 = entry + side.sign * dist * 2.5
    tp_final = entry + side.sign * dist * max(rr, 2.5)
    atr = float(snap.atr_h1 or 0.0)
    spread_pen = 0.0
    if atr > 0 and snap.spec:
        ratio = snap.spread_points * snap.spec.point / atr
        spread_pen = _clamp(ratio * 100, 0, 15)
        if ratio > 0.1:
            cons.append(f"spread élevé ({ratio:.2f} ATR)")
    score = _clamp(score - spread_pen)
    return TradeCandidate(
        symbol=snap.symbol, side=side, entry=float(entry), sl=float(sl), tp_plan=[tp1, tp2, tp_final],
        timeframes=[spec.timeframes.get("entry", "M15"), spec.timeframes.get("trend", "H1")], regime=snap.regime.regime,
        agent_id=spec.agent_id, agent_version=spec.version, setup_score=round(score, 1), data_quality=snap.data_quality,
        spread_points=snap.spread_points, rr=round(abs(tp_final - entry) / dist, 2), invalidation=invalidation,
        arguments_for=pros, arguments_against=cons, atr=atr, session=snap.session.value, bar_time=bar_time,
    )


def _ctx(spec: AgentSpec, snap):
    e = _frame(snap, spec.timeframes.get("entry", "M15"))
    t = _frame(snap, spec.timeframes.get("trend", "H1"))
    if e is None or t is None:
        return None
    le, lt = last_closed(e), last_closed(t)
    if not _valid(le, "ema20", "ema50", "ema200", "atr14", "rsi14", "adx14") or not _valid(lt, "ema20", "ema50", "ema200"):
        return None
    atr = float(le["atr14"])
    if atr <= 0:
        return None
    return e, t, le, lt, atr, str(le["time"])


def _trend_of(row: pd.Series) -> str:
    if row["ema20"] > row["ema50"] > row["ema200"] and row["close"] > row["ema20"]:
        return "UP"
    if row["ema20"] < row["ema50"] < row["ema200"] and row["close"] < row["ema20"]:
        return "DOWN"
    return "FLAT"


def _structure_sl(e: pd.DataFrame, side: Side, entry: float, atr: float, sl_atr: float) -> float:
    """SL derrière le dernier swing confirmé, au moins à sl_atr x ATR, au plus à 3 x ATR."""
    closed = e.iloc[:-1]
    sh, sl_ = swing_points(closed)
    if side is Side.BUY:
        cand = entry - sl_atr * atr
        if sl_:
            cand = min(cand, sl_[-1][1] - 0.2 * atr)
        return max(cand, entry - 3 * atr)
    cand = entry + sl_atr * atr
    if sh:
        cand = max(cand, sh[-1][1] + 0.2 * atr)
    return min(cand, entry + 3 * atr)


def _mtf_bonus(le: pd.Series, lt: pd.Series, side: Side) -> tuple[float, list[str]]:
    pros = []
    b = 0.0
    tr = _trend_of(lt)
    if (side is Side.BUY and tr == "UP") or (side is Side.SELL and tr == "DOWN"):
        b += 15
        pros.append("tendance tf supérieur alignée")
    st = "HH_HL" if side is Side.BUY else "LH_LL"
    return b, pros


# ---------------------------------------------------------------- stratégies
@register("ema_trend")
def ema_trend(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    tr = _trend_of(le)
    if tr == "FLAT" or le["adx14"] < p.get("adx_min", 25):
        return None
    if p.get("vol_pct_max") and pd.notna(le.get("vol_pct")) and le["vol_pct"] > p["vol_pct_max"]:
        return None
    side = Side.BUY if tr == "UP" else Side.SELL
    if _trend_of(lt) not in (tr, "FLAT"):
        return None
    entry = float(le["close"])
    sl = _structure_sl(e, side, entry, atr, p.get("sl_atr", 1.5))
    score = 50 + _clamp((le["adx14"] - 20) * 1.0, 0, 20)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"EMA alignées {tr}", f"ADX {le['adx14']:.0f}"]
    cons = []
    if (side is Side.BUY and le["rsi14"] > 75) or (side is Side.SELL and le["rsi14"] < 25):
        cons.append("RSI extrême : risque d'entrée tardive")
        score -= 10
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score, pros, cons, "clôture au-delà de l'EMA50 du tf d'entrée", bt)


@register("mtf_trend_pullback")
def mtf_trend_pullback(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    tr = _trend_of(lt)
    if tr == "FLAT":
        return None
    side = Side.BUY if tr == "UP" else Side.SELL
    lo, hi = p.get("rsi_lo", 40), p.get("rsi_hi", 60)
    touched = (le["low"] <= le["ema20"] <= le["close"]) if side is Side.BUY else (le["high"] >= le["ema20"] >= le["close"])
    if not touched or not (lo <= le["rsi14"] <= hi):
        return None
    entry = float(le["close"])
    sl = _structure_sl(e, side, entry, atr, p.get("sl_atr", 1.2))
    score = 60
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}", "repli sur EMA20 rejeté", f"RSI {le['rsi14']:.0f} neutre"]
    cons = []
    if structure_label(e.iloc[:-1]) not in (("HH_HL",) if side is Side.BUY else ("LH_LL",)):
        cons.append("structure du tf d'entrée non confirmée")
    else:
        score += 10
        pros.append("structure alignée")
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score, pros, cons, "clôture sous/au-dessus du swing du repli", bt)


@register("ema_pullback")
def ema_pullback(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    spec2 = AgentSpec(**{**spec.to_dict(), "params": {**spec.params, "rsi_lo": 30, "rsi_hi": 70}})
    return mtf_trend_pullback(spec2, snap)


@register("session_breakout")
def session_breakout(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    hi, lo = session_range(e.iloc[:-1], p.get("start", "00:00"), p.get("end", "07:00"))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return None
    rng = hi - lo
    if rng > 3 * atr or rng < 0.3 * atr:
        return None
    entry = float(le["close"])
    if entry > hi and le["open"] <= hi:
        side, sl = Side.BUY, max(lo, entry - p.get("sl_atr", 1.0) * atr - 0.0)
        sl = min(sl, entry - 0.5 * atr)
    elif entry < lo and le["open"] >= lo:
        side, sl = Side.SELL, min(hi, entry + p.get("sl_atr", 1.0) * atr)
        sl = max(sl, entry + 0.5 * atr)
    else:
        return None
    score = 58
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"cassure du range {p.get('session', 'session')} ({rng / atr:.1f} ATR)"]
    cons = ["risque de faux breakout : attendre retest recommandé"]
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score, pros, cons, "retour et clôture dans le range", bt)


@register("daily_hl_breakout")
def daily_hl_breakout(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    d1 = snap.frames.get("D1")
    if not c or d1 is None or len(d1) < 3:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    ph, pl = daily_high_low(d1)
    if not (np.isfinite(ph) and np.isfinite(pl)):
        return None
    entry = float(le["close"])
    if entry > ph and le["open"] <= ph:
        side = Side.BUY
    elif entry < pl and le["open"] >= pl:
        side = Side.SELL
    else:
        return None
    sl = _structure_sl(e, side, entry, atr, p.get("sl_atr", 1.2))
    score = 55
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros.append("cassure du plus haut/bas de la veille")
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score, pros, [], "clôture de retour sous/au-dessus du niveau", bt)


@register("compression_expansion")
def compression_expansion(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    bw = closed["bb_width"].dropna()
    if len(bw) < 100:
        return None
    prev = closed.iloc[-2]
    pct = (bw.iloc[-100:-1] <= prev["bb_width"]).mean() * 100
    if pct > 25:
        return None
    entry = float(le["close"])
    if entry > le["bb_up"]:
        side = Side.BUY
    elif entry < le["bb_low"]:
        side = Side.SELL
    else:
        return None
    sl = _structure_sl(e, side, entry, atr, p.get("sl_atr", 1.0))
    score = 60 + (25 - pct) * 0.6
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros.append(f"compression (largeur BB au {pct:.0f}e percentile) puis expansion")
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.5), score, pros, [], "clôture de retour dans les bandes", bt)


@register("atr_expansion")
def atr_expansion(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    atr_ma = closed["atr14"].iloc[-30:-1].mean()
    if not atr_ma or atr < p.get("atr_ratio", 1.3) * atr_ma:
        return None
    if not _valid(le, "mom10") or abs(le["mom10"]) < 0.5 * atr:
        return None
    side = Side.BUY if le["mom10"] > 0 else Side.SELL
    if _trend_of(lt) not in (("UP", "FLAT") if side is Side.BUY else ("DOWN", "FLAT")):
        return None
    entry = float(le["close"])
    sl = _structure_sl(e, side, entry, atr, p.get("sl_atr", 1.2))
    score = 52 + _clamp((atr / atr_ma - 1) * 40, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    pros.append(f"expansion ATR x{atr / atr_ma:.2f} avec momentum")
    cons = ["volatilité élevée : slippage possible"]
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score + b, pros, cons, "momentum inversé sur 3 barres", bt)


@register("breakout_retest")
def breakout_retest(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    sh, sl_ = swing_points(closed.iloc[:-6])
    if not sh or not sl_:
        return None
    entry = float(le["close"])
    lvl_h, lvl_l = sh[-1][1], sl_[-1][1]
    recent = closed.iloc[-6:-1]
    if (recent["close"] > lvl_h).any() and le["low"] <= lvl_h + 0.2 * atr and entry > lvl_h:
        side, level = Side.BUY, lvl_h
    elif (recent["close"] < lvl_l).any() and le["high"] >= lvl_l - 0.2 * atr and entry < lvl_l:
        side, level = Side.SELL, lvl_l
    else:
        return None
    sl = level - p.get("sl_atr", 1.0) * atr if side is Side.BUY else level + p.get("sl_atr", 1.0) * atr
    score = 62
    b, pros = _mtf_bonus(le, lt, side)
    pros.append("cassure puis retest du niveau tenu")
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.5), score + b, pros, [], "clôture de retour au-delà du niveau retesté", bt)


@register("bollinger_mr")
def bollinger_mr(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    if le["adx14"] > 25:
        return None
    prev = e.iloc[-3]
    entry = float(le["close"])
    if prev["close"] < prev["bb_low"] and entry > le["bb_low"] and le["rsi14"] <= p.get("rsi_lo", 30) + 10:
        side = Side.BUY
        sl = min(float(prev["low"]), float(le["low"])) - p.get("sl_atr", 0.8) * atr
    elif prev["close"] > prev["bb_up"] and entry < le["bb_up"] and le["rsi14"] >= p.get("rsi_hi", 70) - 10:
        side = Side.SELL
        sl = max(float(prev["high"]), float(le["high"])) + p.get("sl_atr", 0.8) * atr
    else:
        return None
    score = 55 + _clamp((25 - le["adx14"]) * 0.8, 0, 12)
    pros = ["excès hors bande puis retour", f"ADX {le['adx14']:.0f} (range)"]
    cons = ["contre-tendance possible sur tf supérieur"] if _trend_of(lt) != "FLAT" else []
    if cons:
        score -= 8
    return _build(spec, snap, side, entry, sl, p.get("rr", 1.5), score, pros, cons, "clôture au-delà de l'extrême de rejet", bt)


@register("rsi_divergence")
def rsi_divergence(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    sh, sl_ = swing_points(closed)
    entry = float(le["close"])
    if len(sl_) >= 2 and sl_[-1][1] < sl_[-2][1] and closed["rsi14"].iloc[sl_[-1][0]] > closed["rsi14"].iloc[sl_[-2][0]] + 3 \
            and entry > closed["close"].iloc[sl_[-1][0]]:
        side, sl = Side.BUY, sl_[-1][1] - p.get("sl_atr", 1.0) * atr * 0.5
    elif len(sh) >= 2 and sh[-1][1] > sh[-2][1] and closed["rsi14"].iloc[sh[-1][0]] < closed["rsi14"].iloc[sh[-2][0]] - 3 \
            and entry < closed["close"].iloc[sh[-1][0]]:
        side, sl = Side.SELL, sh[-1][1] + p.get("sl_atr", 1.0) * atr * 0.5
    else:
        return None
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), 56, ["divergence RSI confirmée sur swings"],
                  ["retournement : taux de réussite historiquement variable"], "nouveau plus bas/haut sous/au-dessus du swing", bt)


@register("exhaustion")
def exhaustion(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    ext = p.get("rsi_ext", 20)
    prev = e.iloc[-3]
    entry = float(le["close"])
    body = abs(le["close"] - le["open"])
    if prev["rsi14"] <= ext and le["close"] > le["open"] and body > 0.5 * atr:
        side, sl = Side.BUY, min(float(prev["low"]), float(le["low"])) - p.get("sl_atr", 0.8) * atr * 0.5
    elif prev["rsi14"] >= 100 - ext and le["close"] < le["open"] and body > 0.5 * atr:
        side, sl = Side.SELL, max(float(prev["high"]), float(le["high"])) + p.get("sl_atr", 0.8) * atr * 0.5
    else:
        return None
    return _build(spec, snap, side, entry, sl, p.get("rr", 1.8), 54, ["RSI extrême + bougie de rejet"], ["contre-tendance"],
                  "nouvel extrême au-delà de la bougie de rejet", bt)


@register("failed_breakout")
def failed_breakout(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    sh, sl_ = swing_points(closed.iloc[:-3])
    if not sh or not sl_:
        return None
    prev = e.iloc[-3]
    entry = float(le["close"])
    if prev["close"] > sh[-1][1] and entry < sh[-1][1]:
        side, sl = Side.SELL, max(float(prev["high"]), float(le["high"])) + p.get("sl_atr", 0.8) * atr * 0.5
    elif prev["close"] < sl_[-1][1] and entry > sl_[-1][1]:
        side, sl = Side.BUY, min(float(prev["low"]), float(le["low"])) - p.get("sl_atr", 0.8) * atr * 0.5
    else:
        return None
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), 58, ["faux breakout : réintégration du range"], [],
                  "nouvelle clôture au-delà du niveau cassé", bt)


@register("liquidity_sweep")
def liquidity_sweep(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    sh, sl_ = swing_points(closed.iloc[:-1])
    if not sh or not sl_:
        return None
    entry = float(le["close"])
    tr = _trend_of(lt)
    if le["low"] < sl_[-1][1] and entry > sl_[-1][1] and (not p.get("with_trend") or tr == "UP"):
        side, sl = Side.BUY, float(le["low"]) - p.get("sl_atr", 0.8) * atr * 0.5
    elif le["high"] > sh[-1][1] and entry < sh[-1][1] and (not p.get("with_trend") or tr == "DOWN"):
        side, sl = Side.SELL, float(le["high"]) + p.get("sl_atr", 0.8) * atr * 0.5
    else:
        return None
    score = 58 + (10 if p.get("with_trend") else 0)
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score, ["balayage de liquidité puis reprise"], [],
                  "clôture au-delà de la mèche du balayage", bt)


@register("sr_rejection")
def sr_rejection(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    levels = support_resistance(e.iloc[:-1])
    if not levels:
        return None
    tol = p.get("tol_atr", 0.3) * atr
    entry = float(le["close"])
    tr = _trend_of(lt)
    near_low = [l for l in levels if abs(le["low"] - l) <= tol]
    near_high = [l for l in levels if abs(le["high"] - l) <= tol]
    if near_low and le["close"] > le["open"] and entry > near_low[0] and (not p.get("with_trend") or tr == "UP"):
        side, sl = Side.BUY, float(le["low"]) - p.get("sl_atr", 1.0) * atr * 0.5
    elif near_high and le["close"] < le["open"] and entry < near_high[0] and (not p.get("with_trend") or tr == "DOWN"):
        side, sl = Side.SELL, float(le["high"]) + p.get("sl_atr", 1.0) * atr * 0.5
    else:
        return None
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), 57 + (8 if p.get("with_trend") else 0),
                  ["rejet d'un niveau S/R"], [], "clôture au-delà du niveau", bt)


@register("structure_bos")
def structure_bos(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    label = structure_label(closed)
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    entry = float(le["close"])
    want = p.get("direction")
    if label == "HH_HL" and entry > sh[-1][1] and want != "DOWN":
        side, sl = Side.BUY, sl_[-1][1] - 0.2 * atr
    elif label == "LH_LL" and entry < sl_[-1][1] and want != "UP":
        side, sl = Side.SELL, sh[-1][1] + 0.2 * atr
    else:
        return None
    if abs(entry - sl) > 3 * atr:
        sl = entry - side.sign * p.get("sl_atr", 1.2) * atr
    if p.get("require_ema") and _trend_of(le) == "FLAT":
        return None
    if p.get("require_mtf") and structure_label(t.iloc[:-1]) != label:
        return None
    score = 60
    b, pros = _mtf_bonus(le, lt, side)
    pros.append(f"structure {label} + cassure de swing")
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), score + b, pros, [], "cassure du swing opposé", bt)


@register("choch")
def choch(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    label = structure_label(closed.iloc[:-1])
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    entry = float(le["close"])
    if label == "LH_LL" and entry > sh[-1][1]:
        side, sl = Side.BUY, sl_[-1][1] - 0.2 * atr
    elif label == "HH_HL" and entry < sl_[-1][1]:
        side, sl = Side.SELL, sh[-1][1] + 0.2 * atr
    else:
        return None
    if abs(entry - sl) > 3 * atr:
        sl = entry - side.sign * p.get("sl_atr", 1.0) * atr
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), 55, ["changement de caractère (CHoCH)"],
                  ["premier signal de retournement : faible confirmation"], "retour sous/au-dessus du swing cassé", bt)


@register("macd_momentum")
def macd_momentum(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    prev = e.iloc[-3]
    if not _valid(le, "macd", "macd_signal") or not _valid(prev, "macd", "macd_signal"):
        return None
    tr = _trend_of(lt)
    entry = float(le["close"])
    if prev["macd"] <= prev["macd_signal"] and le["macd"] > le["macd_signal"] and tr == "UP":
        side = Side.BUY
    elif prev["macd"] >= prev["macd_signal"] and le["macd"] < le["macd_signal"] and tr == "DOWN":
        side = Side.SELL
    else:
        return None
    sl = _structure_sl(e, side, entry, atr, p.get("sl_atr", 1.5))
    return _build(spec, snap, side, entry, sl, p.get("rr", 2.0), 58, ["croisement MACD dans le sens de la tendance"], [],
                  "croisement MACD inverse", bt)


@register("fib_pullback")
def fib_pullback(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    tr = _trend_of(lt)
    entry = float(le["close"])
    tol = p.get("tol_atr", 0.3) * atr
    if tr == "UP" and sh[-1][0] > sl_[-1][0]:
        lo, hi = sl_[-1][1], sh[-1][1]
        fibs = [hi - (hi - lo) * f for f in p.get("levels", [0.5, 0.618])]
        if any(abs(le["low"] - f) <= tol for f in fibs) and le["close"] > le["open"]:
            return _build(spec, snap, Side.BUY, entry, lo - 0.2 * atr if entry - lo < 3 * atr else entry - p.get("sl_atr", 1.0) * atr,
                          p.get("rr", 2.5), 62, ["confluence Fibonacci 50/61.8 + tendance"], [], "clôture sous le swing bas", bt)
    if tr == "DOWN" and sl_[-1][0] > sh[-1][0]:
        hi, lo = sh[-1][1], sl_[-1][1]
        fibs = [lo + (hi - lo) * f for f in p.get("levels", [0.5, 0.618])]
        if any(abs(le["high"] - f) <= tol for f in fibs) and le["close"] < le["open"]:
            return _build(spec, snap, Side.SELL, entry, hi + 0.2 * atr if hi - entry < 3 * atr else entry + p.get("sl_atr", 1.0) * atr,
                          p.get("rr", 2.5), 62, ["confluence Fibonacci 50/61.8 + tendance"], [], "clôture au-dessus du swing haut", bt)
    return None


def run_screener(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    fn = SCREENERS.get(spec.strategy or "")
    if fn is None:
        return None
    try:
        return fn(spec, snap)
    except (KeyError, IndexError, ValueError, TypeError):
        return None
