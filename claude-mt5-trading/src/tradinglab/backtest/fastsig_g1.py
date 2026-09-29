"""Groupe 1 des signaux vectorisés (2026-09-29) : tendance / momentum / bandes.

Jumeaux exacts de mtf_trend_pullback, ema_pullback, macd_momentum, atr_expansion, compression_expansion et bollinger_mr
(src/tradinglab/agents/screeners.py). Le score et les textes n'influencent pas le Signal (aucun de ces screeners ne
filtre sur le score) : seules les conditions qui décident sens / entrée / stop / rr sont reproduites.
Équivalence bougie par bougie : tests/test_fastsig_2026_09_29.py.

Portage 3090 (2026-09-29) : calculs dans `ctx.xp` (numpy ou cupy) ; `sl_atr` / `rr` peuvent être des colonnes (P, 1)
(paquets de configurations). Les parties qui ne dépendent que des données (décalages, moyennes glissantes, percentiles)
sont calculées une fois en numpy, mises en cache dans ctx._sw puis copiées sur la carte (tests/test_fastsig_gpu_*).
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .fastsig import FastCtx, build, register_fast, structure_sl, trend_of


def _lt_trend(ctx: FastCtx):
    return trend_of(ctx.tcol("close"), ctx.tcol("ema20"), ctx.tcol("ema50"), ctx.tcol("ema200"))


def _prev_np(a: np.ndarray) -> np.ndarray:
    """Valeur de la ligne i-1 (e.iloc[-3] dans le screener)."""
    out = np.full(len(a), np.nan)
    out[1:] = a[:-1]
    return out


def _prev(ctx: FastCtx, c: str):
    """Colonne c décalée d'une ligne (données seules : numpy, cache, puis xp)."""
    key = ("g1_prev", c)
    if key not in ctx._sw:
        ctx._sw[key] = _prev_np(ctx.ncol(c))
    return ctx.dev(ctx._sw[key], key)


def _py_min(xp, a, b):
    """min(a, b) de Python : b seulement si b < a (NaN compris comme en Python)."""
    return xp.where(b < a, b, a)


def _py_max(xp, a, b):
    return xp.where(b > a, b, a)


# ------------------------------------------------------------------------------------------------ pullback de tendance
def _pullback(ctx: FastCtx, p: dict, lo: float, hi: float):
    xp = ctx.xp
    tt = _lt_trend(ctx)
    low, high, close = ctx.col("low"), ctx.col("high"), ctx.col("close")
    e20, rsi = ctx.col("ema20"), ctx.col("rsi14")
    buy_t = (low <= e20) & (e20 <= close)
    sell_t = (high >= e20) & (e20 >= close)
    touched = xp.where(tt > 0, buy_t, sell_t)
    ok = ctx.base_x & (tt != 0) & touched & (lo <= rsi) & (rsi <= hi)
    side = xp.where(ok, tt, 0)
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
    xp = ctx.xp
    m, s = ctx.col("macd"), ctx.col("macd_signal")
    pm, ps = _prev(ctx, "macd"), _prev(ctx, "macd_signal")
    valid = ~xp.isnan(m) & ~xp.isnan(s) & ~xp.isnan(pm) & ~xp.isnan(ps)
    tt = _lt_trend(ctx)
    buy = (pm <= ps) & (m > s) & (tt == 1)
    sell = ~buy & (pm >= ps) & (m < s) & (tt == -1)
    ok = ctx.base_x & valid
    side = xp.where(ok & buy, 1, xp.where(ok & sell, -1, 0))
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


def _atr_ma(ctx: FastCtx):
    key = ("g1_atr_ma29",)
    if key not in ctx._sw:
        ctx._sw[key] = _mean_prev29(ctx.ncol("atr14"))
    return ctx.dev(ctx._sw[key], key)


@register_fast("atr_expansion", [{}, {"atr_ratio": 1.1, "sl_atr": 1.6, "rr": 3.0}, {"atr_ratio": 1.5}])
def atr_expansion(ctx: FastCtx, p: dict):
    xp = ctx.xp
    atr = ctx.col("atr14")
    atr_ma = _atr_ma(ctx)
    with np.errstate(invalid="ignore"):
        refus_ma = (atr_ma == 0) | (atr < p.get("atr_ratio", 1.3) * atr_ma)
        mom = ctx.col("mom10")
        refus_mom = xp.isnan(mom) | (xp.abs(mom) < 0.5 * atr)
    side0 = xp.where(mom > 0, 1, -1)
    tt = _lt_trend(ctx)
    tr_ok = xp.where(side0 > 0, tt != -1, tt != 1)
    ok = ctx.base_x & ~refus_ma & ~refus_mom & tr_ok
    side = xp.where(ok, side0, 0)
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


def _compression_ok(ctx: FastCtx):
    """assez de données ET pas (pct > 25) — ne dépend que des données."""
    key = ("g1_bw_ok",)
    if key not in ctx._sw:
        enough, pct = _bw_percentile(ctx.ncol("bb_width"))
        with np.errstate(invalid="ignore"):
            ctx._sw[key] = enough & ~(pct > 25)
    return ctx.dev(ctx._sw[key], key)


@register_fast("compression_expansion", [{}, {"sl_atr": 2.0, "rr": 2.0}])
def compression_expansion(ctx: FastCtx, p: dict):
    xp = ctx.xp
    close = ctx.col("close")
    up, lo = ctx.col("bb_up"), ctx.col("bb_low")
    ok = ctx.base_x & _compression_ok(ctx)
    buy = close > up
    sell = ~buy & (close < lo)
    side = xp.where(ok & buy, 1, xp.where(ok & sell, -1, 0))
    sl = structure_sl(ctx, side, close, ctx.col("atr14"), p.get("sl_atr", 1.0))
    return build(side, close, sl, p.get("rr", 2.5))


# ------------------------------------------------------------------------------------------------ retour dans les bandes
@register_fast("bollinger_mr", [{}, {"rsi_lo": 20, "rsi_hi": 80, "sl_atr": 1.2, "rr": 2.0}, {"rsi_lo": 40, "rsi_hi": 60}])
def bollinger_mr(ctx: FastCtx, p: dict):
    xp = ctx.xp
    close, rsi, atr = ctx.col("close"), ctx.col("rsi14"), ctx.col("atr14")
    low, high = ctx.col("low"), ctx.col("high")
    up, bl = ctx.col("bb_up"), ctx.col("bb_low")
    pc, pbl, pbu = _prev(ctx, "close"), _prev(ctx, "bb_low"), _prev(ctx, "bb_up")
    pl, ph = _prev(ctx, "low"), _prev(ctx, "high")
    ok = ctx.base_x & ~(ctx.col("adx14") > 25)
    buy = (pc < pbl) & (close > bl) & (rsi <= p.get("rsi_lo", 30) + 10)
    sell = ~buy & (pc > pbu) & (close < up) & (rsi >= p.get("rsi_hi", 70) - 10)
    side = xp.where(ok & buy, 1, xp.where(ok & sell, -1, 0))
    sla = p.get("sl_atr", 0.8)
    sl = xp.where(side > 0, _py_min(xp, pl, low) - sla * atr, _py_max(xp, ph, high) + sla * atr)
    return build(side, close, sl, p.get("rr", 1.5))
