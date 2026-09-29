"""Groupe 1 des signaux vectorisés (2026-09-29) : tendance / momentum / bandes.

Jumeaux exacts de mtf_trend_pullback, ema_pullback, macd_momentum, atr_expansion, compression_expansion et bollinger_mr
(src/tradinglab/agents/screeners.py). Le score et les textes n'influencent pas le Signal (aucun de ces screeners ne
filtre sur le score) : seules les conditions qui décident sens / entrée / stop / rr sont reproduites.
Équivalence bougie par bougie : tests/test_fastsig_2026_09_29.py.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .fastsig import FastCtx, build, register_fast, structure_sl, trend_of


def _lt_trend(ctx: FastCtx) -> np.ndarray:
    return trend_of(ctx.tcol("close"), ctx.tcol("ema20"), ctx.tcol("ema50"), ctx.tcol("ema200"))


def _prev(a: np.ndarray) -> np.ndarray:
    """Valeur de la ligne i-1 (e.iloc[-3] dans le screener)."""
    out = np.full(len(a), np.nan)
    out[1:] = a[:-1]
    return out


def _py_min(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """min(a, b) de Python : b seulement si b < a (NaN compris comme en Python)."""
    return np.where(b < a, b, a)


def _py_max(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.where(b > a, b, a)


# ------------------------------------------------------------------------------------------------ pullback de tendance
def _pullback(ctx: FastCtx, p: dict, lo: float, hi: float):
    tt = _lt_trend(ctx)
    low, high, close = ctx.col("low"), ctx.col("high"), ctx.col("close")
    e20, rsi = ctx.col("ema20"), ctx.col("rsi14")
    buy_t = (low <= e20) & (e20 <= close)
    sell_t = (high >= e20) & (e20 >= close)
    touched = np.where(tt > 0, buy_t, sell_t)
    ok = ctx.base & (tt != 0) & touched & (lo <= rsi) & (rsi <= hi)
    side = np.where(ok, tt, 0)
    sl = structure_sl(ctx, side, close, ctx.col("atr14"), p.get("sl_atr", 1.2))
    return build(side, close, sl, p.get("rr", 2.0))


@register_fast("mtf_trend_pullback", [{}, {"rsi_lo": 38, "rsi_hi": 62, "sl_atr": 1.3, "rr": 3.0}, {"rsi_lo": 20, "rsi_hi": 80}])
def mtf_trend_pullback(ctx: FastCtx, p: dict):
    return _pullback(ctx, p, p.get("rsi_lo", 40), p.get("rsi_hi", 60))


@register_fast("ema_pullback", [{}, {"sl_atr": 2.0, "rr": 1.5}])
def ema_pullback(ctx: FastCtx, p: dict):
    # le screener force rsi_lo=30 / rsi_hi=70 quels que soient les paramètres
    return _pullback(ctx, {**p, "rsi_lo": 30, "rsi_hi": 70}, 30, 70)


# ------------------------------------------------------------------------------------------------ MACD
@register_fast("macd_momentum", [{}, {"sl_atr": 1.0, "rr": 3.0}])
def macd_momentum(ctx: FastCtx, p: dict):
    m, s = ctx.col("macd"), ctx.col("macd_signal")
    pm, ps = _prev(m), _prev(s)
    valid = ~np.isnan(m) & ~np.isnan(s) & ~np.isnan(pm) & ~np.isnan(ps)
    tt = _lt_trend(ctx)
    buy = (pm <= ps) & (m > s) & (tt == 1)
    sell = ~buy & (pm >= ps) & (m < s) & (tt == -1)
    ok = ctx.base & valid
    side = np.where(ok & buy, 1, np.where(ok & sell, -1, 0))
    close = ctx.col("close")
    sl = structure_sl(ctx, side, close, ctx.col("atr14"), p.get("sl_atr", 1.5))
    return build(side, close, sl, p.get("rr", 2.0))


# ------------------------------------------------------------------------------------------------ expansion d'ATR
def _mean_prev29(a: np.ndarray) -> np.ndarray:
    """closed["atr14"].iloc[-30:-1].mean() : moyenne (NaN ignorés, comme pandas) des lignes i-29..i-1."""
    n = len(a)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    z = np.where(np.isnan(a), 0.0, a)
    cnt = (~np.isnan(a)).astype(np.int64)
    # fenêtre des lignes max(0, i-29)..i-1 ; pour i >= 30, fenêtre pleine de 29 valeurs
    for i in range(1, min(n, 30)):
        c = cnt[max(0, i - 29):i].sum()
        if c:
            out[i] = z[max(0, i - 29):i].sum() / c
    if n > 30:
        wz = sliding_window_view(z, 29)            # wz[s] = z[s:s+29] ; pour i : s = i-29
        wc = sliding_window_view(cnt, 29)
        s_idx = np.arange(30, n) - 29
        sums = wz[s_idx].sum(axis=1)
        cs = wc[s_idx].sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[30:] = np.where(cs > 0, sums / np.where(cs > 0, cs, 1), np.nan)
    return out


@register_fast("atr_expansion", [{}, {"atr_ratio": 1.1, "sl_atr": 1.6, "rr": 3.0}, {"atr_ratio": 1.5}])
def atr_expansion(ctx: FastCtx, p: dict):
    atr = ctx.col("atr14")
    atr_ma = _mean_prev29(atr)
    with np.errstate(invalid="ignore"):
        refus_ma = (atr_ma == 0) | (atr < p.get("atr_ratio", 1.3) * atr_ma)
        mom = ctx.col("mom10")
        refus_mom = np.isnan(mom) | (np.abs(mom) < 0.5 * atr)
    side0 = np.where(mom > 0, 1, -1)
    tt = _lt_trend(ctx)
    tr_ok = np.where(side0 > 0, tt != -1, tt != 1)
    ok = ctx.base & ~refus_ma & ~refus_mom & tr_ok
    side = np.where(ok, side0, 0)
    close = ctx.col("close")
    sl = structure_sl(ctx, side, close, atr, p.get("sl_atr", 1.2))
    return build(side, close, sl, p.get("rr", 2.0))


# ------------------------------------------------------------------------------------------------ compression puis expansion
def _bw_percentile(bw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pour chaque bougie i : (assez_de_données, pct) du screener.
    bw_ok = valeurs non NaN des lignes 0..i ; exige >= 100 ; pct = part des 99 valeurs précédant la dernière qui sont
    <= bb_width de la ligne i-1, en % (même arithmétique que pandas : somme / 99 × 100)."""
    n = len(bw)
    valid = ~np.isnan(bw)
    pos = np.flatnonzero(valid)
    vals = bw[pos]
    m = np.searchsorted(pos, np.arange(n), side="right")          # nombre de valeurs valides dans 0..i
    enough = m >= 100
    prev = np.full(n, np.nan)
    prev[1:] = bw[:-1]
    pct = np.full(n, np.nan)
    if len(vals) >= 100:
        win = sliding_window_view(vals, 99)                         # win[s] = vals[s:s+99]
        idx = np.flatnonzero(enough)
        s = m[idx] - 100
        with np.errstate(invalid="ignore"):
            cnt = (win[s] <= prev[idx][:, None]).sum(axis=1)
        pct[idx] = (cnt.astype(float) / 99.0) * 100.0
    return enough, pct


@register_fast("compression_expansion", [{}, {"sl_atr": 2.0, "rr": 2.0}])
def compression_expansion(ctx: FastCtx, p: dict):
    enough, pct = _bw_percentile(ctx.col("bb_width"))
    close = ctx.col("close")
    up, lo = ctx.col("bb_up"), ctx.col("bb_low")
    with np.errstate(invalid="ignore"):
        ok = ctx.base & enough & ~(pct > 25)
    buy = close > up
    sell = ~buy & (close < lo)
    side = np.where(ok & buy, 1, np.where(ok & sell, -1, 0))
    sl = structure_sl(ctx, side, close, ctx.col("atr14"), p.get("sl_atr", 1.0))
    return build(side, close, sl, p.get("rr", 2.5))


# ------------------------------------------------------------------------------------------------ retour dans les bandes
@register_fast("bollinger_mr", [{}, {"rsi_lo": 20, "rsi_hi": 80, "sl_atr": 1.2, "rr": 2.0}, {"rsi_lo": 40, "rsi_hi": 60}])
def bollinger_mr(ctx: FastCtx, p: dict):
    close, rsi, atr = ctx.col("close"), ctx.col("rsi14"), ctx.col("atr14")
    low, high = ctx.col("low"), ctx.col("high")
    up, bl = ctx.col("bb_up"), ctx.col("bb_low")
    pc, pbl, pbu = _prev(close), _prev(bl), _prev(up)
    pl, ph = _prev(low), _prev(high)
    ok = ctx.base & ~(ctx.col("adx14") > 25)
    buy = (pc < pbl) & (close > bl) & (rsi <= p.get("rsi_lo", 30) + 10)
    sell = ~buy & (pc > pbu) & (close < up) & (rsi >= p.get("rsi_hi", 70) - 10)
    side = np.where(ok & buy, 1, np.where(ok & sell, -1, 0))
    sla = p.get("sl_atr", 0.8)
    sl = np.where(side > 0, _py_min(pl, low) - sla * atr, _py_max(ph, high) + sla * atr)
    return build(side, close, sl, p.get("rr", 1.5))
