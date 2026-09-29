"""Jumeaux vectorisés — groupe 3 : rsi_divergence, exhaustion, sr_rejection (agents/screeners.py), donchian_breakout,
rsi2_reversion (agents/crypto_strategies.py). Équivalence exacte bougie par bougie : tests/test_fastsig_2026_09_29.py.

daily_hl_breakout n'est PAS converti : dans le backtest, son cadre D1 n'existe que si l'unité de tendance est D1
(sinon il ne produit jamais rien) ; la stratégie garde le chemin lent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view as _win

from ..market_data.indicators import true_range
from .fastsig import FastCtx, build, register_fast, trend_of


def _lt_trend(ctx: FastCtx) -> np.ndarray:
    return trend_of(ctx.tcol("close"), ctx.tcol("ema20"), ctx.tcol("ema50"), ctx.tcol("ema200"))


@register_fast("rsi_divergence", [{}, {"sl_atr": 2.0, "rr": 3.0}])
def rsi_divergence(ctx: FastCtx, p: dict):
    close, atr, rsi = ctx.col("close"), ctx.col("atr14"), ctx.col("rsi14")
    sh_p, sh_i, sl_p, sl_i = ctx.swings(3, 3, 2)
    k = p.get("sl_atr", 1.0)

    def at(arr, idx):
        out = np.full(ctx.n, np.nan)
        ok = idx >= 0
        out[ok] = arr[idx[ok]]
        return out

    with np.errstate(invalid="ignore"):
        buy = ctx.base & (sl_i[1] >= 0) & (sl_p[0] < sl_p[1]) & (at(rsi, sl_i[0]) > at(rsi, sl_i[1]) + 3) \
            & (close > at(close, sl_i[0]))
        sell = ctx.base & ~buy & (sh_i[1] >= 0) & (sh_p[0] > sh_p[1]) & (at(rsi, sh_i[0]) < at(rsi, sh_i[1]) - 3) \
            & (close < at(close, sh_i[0]))
    side = np.where(buy, 1, np.where(sell, -1, 0))
    sl = np.where(buy, sl_p[0] - k * atr * 0.5, sh_p[0] + k * atr * 0.5)
    return build(side, close, sl, p.get("rr", 2.0))


@register_fast("exhaustion", [{"rsi_ext": 30}, {}, {"rsi_ext": 35, "sl_atr": 1.5, "rr": 3.0}])
def exhaustion(ctx: FastCtx, p: dict):
    close, opn, atr = ctx.col("close"), ctx.col("open"), ctx.col("atr14")
    low, high, rsi = ctx.col("low"), ctx.col("high"), ctx.col("rsi14")
    prev = lambda a: np.r_[np.nan, a[:-1]]                                     # noqa: E731 - ligne i-1
    p_rsi, p_low, p_high = prev(rsi), prev(low), prev(high)
    ext = p.get("rsi_ext", 20)
    k = p.get("sl_atr", 0.8)
    body = np.abs(close - opn)
    with np.errstate(invalid="ignore"):
        buy = ctx.base & (p_rsi <= ext) & (close > opn) & (body > 0.5 * atr)
        sell = ctx.base & ~buy & (p_rsi >= 100 - ext) & (close < opn) & (body > 0.5 * atr)
    side = np.where(buy, 1, np.where(sell, -1, 0))
    sl = np.where(buy, np.minimum(p_low, low) - k * atr * 0.5, np.maximum(p_high, high) + k * atr * 0.5)
    return build(side, close, sl, p.get("rr", 1.8))


def _window_atr(ctx: FastCtx, lookback: int = 100, n: int = 14) -> np.ndarray:
    """ATR(14) recalculé sur la seule fenêtre des `lookback` dernières barres clôturées (comme support_resistance)."""
    if "sr_atr" in ctx._sw:
        return ctx._sw["sr_atr"]
    tr = true_range(ctx.e).to_numpy(dtype=float)
    hl = (ctx.e["high"].astype(float) - ctx.e["low"].astype(float)).to_numpy()
    out = np.full(ctx.n, np.nan)
    if ctx.n >= lookback:
        m = _win(tr, lookback).T.copy()                  # colonne j = fenêtre des lignes j..j+lookback-1
        m[0, :] = hl[: ctx.n - lookback + 1]            # 1re barre de la fenêtre : h - l (pas de clôture précédente)
        w = pd.DataFrame(m).ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean().iloc[-1].to_numpy()
        out[lookback - 1:] = w
    ctx._sw["sr_atr"] = out
    return out


@register_fast("sr_rejection", [{}, {"with_trend": True}, {"tol_atr": 0.6, "sl_atr": 1.5, "rr": 3.0}])
def sr_rejection(ctx: FastCtx, p: dict):
    close, opn, atr = ctx.col("close"), ctx.col("open"), ctx.col("atr14")
    low, high = ctx.col("low"), ctx.col("high")
    tr = _lt_trend(ctx)
    from ..market_data.indicators import swing_points
    sh, sl = swing_points(ctx.e)
    piv_i = np.array([q[0] for q in sh] + [q[0] for q in sl], dtype=np.int64)
    piv_p = np.array([q[1] for q in sh] + [q[1] for q in sl], dtype=float)
    ref = _window_atr(ctx)
    tol_atr, k, wt = p.get("tol_atr", 0.3), p.get("sl_atr", 1.0), p.get("with_trend")
    side = np.zeros(ctx.n, dtype=np.int8)
    slv = np.full(ctx.n, np.nan)
    for i in np.nonzero(ctx.base)[0]:
        lb = i - 99 if i >= 99 else 0
        sel = (piv_i >= lb + 3) & (piv_i <= i - 3)
        levels = sorted(piv_p[sel].tolist())
        if not levels:
            continue
        ra = float(ref[i]) if i + 1 >= 100 else float("nan")
        if i + 1 < 100:                                     # fenêtre plus courte : repli exact sur l'indicateur
            from ..market_data.indicators import atr as _atr
            ra = float(_atr(ctx.e.iloc[: i + 1].tail(100).reset_index(drop=True), 14).iloc[-1])
        tol = 0.5 * (ra if not np.isnan(ra) else 0.0)
        clusters = [[levels[0]]]
        for lv in levels[1:]:
            if lv - clusters[-1][-1] <= tol:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        lv_ = [float(np.mean(c)) for c in clusters]
        t = tol_atr * atr[i]
        near_low = [x for x in lv_ if abs(low[i] - x) <= t]
        near_high = [x for x in lv_ if abs(high[i] - x) <= t]
        if near_low and close[i] > opn[i] and close[i] > near_low[0] and (not wt or tr[i] == 1):
            side[i], slv[i] = 1, float(low[i]) - k * atr[i] * 0.5
        elif near_high and close[i] < opn[i] and close[i] < near_high[0] and (not wt or tr[i] == -1):
            side[i], slv[i] = -1, float(high[i]) + k * atr[i] * 0.5
    return build(side, close, slv, p.get("rr", 2.0))


@register_fast("donchian_breakout", [{}, {"lookback": 55, "sl_atr": 2.0, "rr": 3.0}, {"lookback": 10}])
def donchian_breakout(ctx: FastCtx, p: dict):
    close, atr = ctx.col("close"), ctx.col("atr14")
    high, low = ctx.col("high"), ctx.col("low")
    n = int(p.get("lookback", 20))
    i = np.arange(ctx.n)
    ok = ctx.base & (i + 1 >= n + 3)
    haut = np.full(ctx.n, np.nan); bas = np.full(ctx.n, np.nan)
    haut_p = np.full(ctx.n, np.nan); bas_p = np.full(ctx.n, np.nan)
    if ctx.n > n + 1:
        wh, wl = _win(high, n).max(axis=1), _win(low, n).min(axis=1)       # w[j] = lignes j..j+n-1
        haut[n:] = wh[: ctx.n - n]; bas[n:] = wl[: ctx.n - n]              # lignes i-n..i-1
        haut_p[n + 1:] = wh[: ctx.n - n - 1]; bas_p[n + 1:] = wl[: ctx.n - n - 1]   # lignes i-n-1..i-2
    prev_close = np.r_[np.nan, close[:-1]]
    tr = _lt_trend(ctx)
    with np.errstate(invalid="ignore"):
        buy = ok & (close > haut) & (prev_close <= haut_p)
        sell = ok & ~buy & (close < bas) & (prev_close >= bas_p)
    buy &= tr != -1
    sell &= tr != 1
    side = np.where(buy, 1, np.where(sell, -1, 0))
    sl = close - side * float(p.get("sl_atr", 1.5)) * atr
    return build(side, close, sl, float(p.get("rr", 2.5)))


@register_fast("rsi2_reversion", [{}, {"rsi_lo": 20, "rsi_hi": 80, "sl_atr": 1.0}])
def rsi2_reversion(ctx: FastCtx, p: dict):
    close, atr, e200 = ctx.col("close"), ctx.col("atr14"), ctx.col("ema200")
    v = ctx.col("rsi2")
    tr = _lt_trend(ctx)
    with np.errstate(invalid="ignore"):
        ok = ctx.base & ~np.isnan(v)
        buy = ok & (v < float(p.get("rsi_lo", 10))) & (close > e200) & (tr != -1)
        sell = ok & ~buy & (v > float(p.get("rsi_hi", 90))) & (close < e200) & (tr != 1)
    side = np.where(buy, 1, np.where(sell, -1, 0))
    sl = close - side * float(p.get("sl_atr", 1.5)) * atr
    return build(side, close, sl, float(p.get("rr", 1.5)))
