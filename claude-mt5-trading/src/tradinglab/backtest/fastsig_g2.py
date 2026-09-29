"""Groupe 2 des signaux vectorisés : stratégies à pivots / structure (2026-09-29).

Jumeaux EXACTS des screeners breakout_retest, liquidity_sweep, structure_bos, choch, failed_breakout, fib_pullback
(agents/screeners.py). Pivots : `swing_points` (3/3) calculé une fois sur tout l'historique ; pour un préfixe de m lignes,
les pivots confirmés sont ceux d'index j <= m - 4 (identiques à ceux du préfixe : la condition d'un pivot ne lit que
les 3 barres avant et après lui). À la bougie i, `closed` = lignes 0..i (m = i + 1).
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ..market_data.indicators import swing_points
from .fastsig import FastCtx, build, register_fast, trend_of


def _pivots(ctx: FastCtx, which: str = "e"):
    """((idx_hauts, prix_hauts), (idx_bas, prix_bas)) de swing_points(3, 3) sur le cadre entier (entrée ou tendance)."""
    key = ("piv", which)
    if key not in ctx._sw:
        sh, sl = swing_points(ctx.e if which == "e" else ctx.t, 3, 3)
        ctx._sw[key] = tuple((np.array([p[0] for p in piv], dtype=np.int64), np.array([p[1] for p in piv], dtype=float))
                             for piv in (sh, sl))
    return ctx._sw[key]


def _last(piv, limit: np.ndarray, m: int = 0):
    """Prix / index du (m+1)-ième pivot le plus récent d'index <= limit (NaN / -1 si absent)."""
    idx, prix = piv
    pos = np.searchsorted(idx, limit, side="right") - 1 - m
    ok = pos >= 0
    px = np.full(len(limit), np.nan)
    ix = np.full(len(limit), -1, dtype=np.int64)
    px[ok] = prix[pos[ok]]
    ix[ok] = idx[pos[ok]]
    return px, ix


def _label(sh, sl, limit):
    """structure_label vectorisé pour le préfixe dont les pivots sont d'index <= limit : +1 HH_HL, -1 LH_LL, 0 sinon."""
    h0, _ = _last(sh, limit, 0)
    h1, _ = _last(sh, limit, 1)
    l0, _ = _last(sl, limit, 0)
    l1, _ = _last(sl, limit, 1)
    ok = ~np.isnan(h1) & ~np.isnan(l1)
    hh, hl = h0 > h1, l0 > l1
    return np.where(ok & hh & hl, 1, np.where(ok & ~hh & ~hl, -1, 0)).astype(np.int8)


def _roll(x: np.ndarray, back: int, width: int, fn) -> np.ndarray:
    """fn (max / min) des lignes i-back .. i-back+width-1, pour chaque i (NaN si hors historique)."""
    n = len(x)
    out = np.full(n, np.nan)
    if n >= width:
        w = fn(sliding_window_view(x, width), axis=1)          # w[j] = fn(x[j : j + width])
        i = np.arange(back, n - width + back + 1)
        i = i[(i >= 0) & (i < n)]
        out[i] = w[i - back]
    return out


def _prev(x: np.ndarray) -> np.ndarray:
    out = np.full(len(x), np.nan)
    out[1:] = x[:-1]
    return out


# ------------------------------------------------------------------------------------------------ stratégies
@register_fast("breakout_retest", [{}, {"sl_atr": 1.5, "rr": 3.0}])
def breakout_retest(ctx: FastCtx, p: dict):
    n = ctx.n
    sh, sl = _pivots(ctx)
    lim = np.arange(n) - 9                                    # swing_points(closed.iloc[:-6]) : m = i - 5
    lh, _ = _last(sh, lim)
    ll, _ = _last(sl, lim)
    close, low, high, atr = ctx.col("close"), ctx.col("low"), ctx.col("high"), ctx.col("atr14")
    rmax = _roll(close, 5, 5, np.max)                         # recent = closed.iloc[-6:-1] = lignes i-5 .. i-1
    rmin = _roll(close, 5, 5, np.min)
    ok = ctx.base & ~np.isnan(lh) & ~np.isnan(ll)
    buy = ok & (rmax > lh) & (low <= lh + 0.2 * atr) & (close > lh)
    sell = ok & ~buy & (rmin < ll) & (high >= ll - 0.2 * atr) & (close < ll)
    side = np.where(buy, 1, np.where(sell, -1, 0))
    k = p.get("sl_atr", 1.0)
    stop = np.where(buy, lh - k * atr, ll + k * atr)
    return build(side, close, stop, p.get("rr", 2.5))


@register_fast("failed_breakout", [{}, {"sl_atr": 1.2, "rr": 3.0}])
def failed_breakout(ctx: FastCtx, p: dict):
    n = ctx.n
    sh, sl = _pivots(ctx)
    lim = np.arange(n) - 6                                    # swing_points(closed.iloc[:-3]) : m = i - 2
    lh, _ = _last(sh, lim)
    ll, _ = _last(sl, lim)
    close, low, high, atr = ctx.col("close"), ctx.col("low"), ctx.col("high"), ctx.col("atr14")
    pc, ph, pl = _prev(close), _prev(high), _prev(low)        # prev = e.iloc[-3] = ligne i-1
    ok = ctx.base & ~np.isnan(lh) & ~np.isnan(ll)
    sell = ok & (pc > lh) & (close < lh)
    buy = ok & ~sell & (pc < ll) & (close > ll)
    k = p.get("sl_atr", 0.8) * atr * 0.5
    # max(a, b) Python = a sauf si b > a (mêmes valeurs qu'np.maximum hors NaN)
    stop = np.where(sell, np.where(high > ph, high, ph) + k, np.where(low < pl, low, pl) - k)
    side = np.where(sell, -1, np.where(buy, 1, 0))
    return build(side, close, stop, p.get("rr", 2.0))


@register_fast("liquidity_sweep", [{}, {"with_trend": True, "sl_atr": 1.0}])
def liquidity_sweep(ctx: FastCtx, p: dict):
    n = ctx.n
    sh, sl = _pivots(ctx)
    lim = np.arange(n) - 4                                    # swing_points(closed.iloc[:-1]) : m = i
    lh, _ = _last(sh, lim)
    ll, _ = _last(sl, lim)
    close, low, high, atr = ctx.col("close"), ctx.col("low"), ctx.col("high"), ctx.col("atr14")
    tr = trend_of(ctx.tcol("close"), ctx.tcol("ema20"), ctx.tcol("ema50"), ctx.tcol("ema200"))
    wt = bool(p.get("with_trend"))
    ok = ctx.base & ~np.isnan(lh) & ~np.isnan(ll)
    buy = ok & (low < ll) & (close > ll) & ((not wt) | (tr == 1))
    sell = ok & ~buy & (high > lh) & (close < lh) & ((not wt) | (tr == -1))
    k = p.get("sl_atr", 0.8) * atr * 0.5
    stop = np.where(buy, low - k, high + k)
    side = np.where(buy, 1, np.where(sell, -1, 0))
    return build(side, close, stop, p.get("rr", 2.0))


@register_fast("structure_bos", [{}, {"direction": "UP", "require_ema": True}, {"require_mtf": True, "sl_atr": 1.5}])
def structure_bos(ctx: FastCtx, p: dict):
    n = ctx.n
    sh, sl = _pivots(ctx)
    lim = np.arange(n) - 3                                    # closed : m = i + 1
    lab = _label(sh, sl, lim)
    lh, _ = _last(sh, lim)
    ll, _ = _last(sl, lim)
    close, atr = ctx.col("close"), ctx.col("atr14")
    want = p.get("direction")
    ok = ctx.base & ~np.isnan(lh) & ~np.isnan(ll)
    buy = ok & (lab == 1) & (close > lh) & (want != "DOWN")
    sell = ok & ~buy & (lab == -1) & (close < ll) & (want != "UP")
    side = np.where(buy, 1, np.where(sell, -1, 0))
    stop = np.where(buy, ll - 0.2 * atr, lh + 0.2 * atr)
    trop = np.abs(close - stop) > 3 * atr
    stop = np.where(trop, close - side * p.get("sl_atr", 1.2) * atr, stop)
    if p.get("require_ema"):
        side = np.where(trend_of(close, ctx.col("ema20"), ctx.col("ema50"), ctx.col("ema200")) == 0, 0, side)
    if p.get("require_mtf"):
        tsh, tsl = _pivots(ctx, "t")
        tlab = _label(tsh, tsl, ctx.lt_idx - 3)             # structure_label(t.iloc[:-1]) = cadre de tendance clôturé
        side = np.where(tlab != lab, 0, side)
    return build(side, close, stop, p.get("rr", 2.0))


@register_fast("choch", [{}, {"sl_atr": 2.0, "rr": 3.0}])
def choch(ctx: FastCtx, p: dict):
    n = ctx.n
    sh, sl = _pivots(ctx)
    lab = _label(sh, sl, np.arange(n) - 4)                    # structure_label(closed.iloc[:-1]) : m = i
    lim = np.arange(n) - 3
    lh, _ = _last(sh, lim)
    ll, _ = _last(sl, lim)
    close, atr = ctx.col("close"), ctx.col("atr14")
    ok = ctx.base & ~np.isnan(lh) & ~np.isnan(ll)
    buy = ok & (lab == -1) & (close > lh)
    sell = ok & ~buy & (lab == 1) & (close < ll)
    side = np.where(buy, 1, np.where(sell, -1, 0))
    stop = np.where(buy, ll - 0.2 * atr, lh + 0.2 * atr)
    trop = np.abs(close - stop) > 3 * atr
    stop = np.where(trop, close - side * p.get("sl_atr", 1.0) * atr, stop)
    return build(side, close, stop, p.get("rr", 2.0))


@register_fast("fib_pullback", [{}, {"tol_atr": 0.5, "levels": [0.382, 0.5, 0.618], "sl_atr": 1.5}])
def fib_pullback(ctx: FastCtx, p: dict):
    n = ctx.n
    sh, sl = _pivots(ctx)
    lim = np.arange(n) - 3
    hi, hix = _last(sh, lim)
    lo, lix = _last(sl, lim)
    close, opn, low, high, atr = ctx.col("close"), ctx.col("open"), ctx.col("low"), ctx.col("high"), ctx.col("atr14")
    tr = trend_of(ctx.tcol("close"), ctx.tcol("ema20"), ctx.tcol("ema50"), ctx.tcol("ema200"))
    tol = p.get("tol_atr", 0.3) * atr
    levels = p.get("levels", [0.5, 0.618])
    ok = ctx.base & ~np.isnan(hi) & ~np.isnan(lo)
    up = ok & (tr == 1) & (hix > lix)
    dn = ok & (tr == -1) & (lix > hix)
    touch_up = np.zeros(n, dtype=bool)
    touch_dn = np.zeros(n, dtype=bool)
    for f in levels:
        touch_up |= np.abs(low - (hi - (hi - lo) * f)) <= tol
        touch_dn |= np.abs(high - (lo + (hi - lo) * f)) <= tol
    buy = up & touch_up & (close > opn)
    sell = dn & touch_dn & (close < opn)
    k = p.get("sl_atr", 1.0)
    stop_b = np.where(close - lo < 3 * atr, lo - 0.2 * atr, close - k * atr)
    stop_s = np.where(hi - close < 3 * atr, hi + 0.2 * atr, close + k * atr)
    side = np.where(buy, 1, np.where(sell, -1, 0))
    return build(side, close, np.where(buy, stop_b, stop_s), p.get("rr", 2.5))
