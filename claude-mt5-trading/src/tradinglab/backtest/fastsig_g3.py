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
    """Tendance de `lt`, toujours en numpy (partie « données » des stratégies)."""
    return trend_of(ctx.ntcol("close"), ctx.ntcol("ema20"), ctx.ntcol("ema50"), ctx.ntcol("ema200"))


# 2026-09-29 (portage 3090) : chaque stratégie calcule en numpy, une fois par jeu de paramètres NON groupés (mis en cache
# dans ctx._sw), le sens et le prix de référence du stop ; seuls le stop et la cible, qui dépendent de sl_atr / rr
# (nombres ou colonnes (P, 1) pour un paquet de P configurations), se calculent dans ctx.xp (numpy ou cupy).
def _cached(ctx: FastCtx, key, fn):
    if key not in ctx._sw:
        ctx._sw[key] = fn()
    return ctx._sw[key]


@register_fast("rsi_divergence", [{}, {"sl_atr": 2.0, "rr": 3.0}])
def rsi_divergence(ctx: FastCtx, p: dict):
    def donnees():
        close, rsi = ctx.ncol("close"), ctx.ncol("rsi14")
        sh_p, sh_i, sl_p, sl_i = ctx.swings(3, 3, 2)

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
        return buy, np.where(buy, 1, np.where(sell, -1, 0)), sl_p[0], sh_p[0]
    key = ("g3", "rsid")
    buy, side, lo_ref, hi_ref = _cached(ctx, key, donnees)
    xp = ctx.xp
    k = p.get("sl_atr", 1.0)
    atr, close = ctx.col("atr14"), ctx.col("close")
    sl = xp.where(ctx.dev(buy, key + ("buy",)), ctx.dev(lo_ref, key + ("lo",)) - k * atr * 0.5,
                  ctx.dev(hi_ref, key + ("hi",)) + k * atr * 0.5)
    return build(ctx.dev(side, key + ("side",)), close, sl, p.get("rr", 2.0))


@register_fast("exhaustion", [{"rsi_ext": 30}, {}, {"rsi_ext": 35, "sl_atr": 1.5, "rr": 3.0}])
def exhaustion(ctx: FastCtx, p: dict):
    ext = p.get("rsi_ext", 20)

    def donnees():
        close, opn, atr = ctx.ncol("close"), ctx.ncol("open"), ctx.ncol("atr14")
        low, high, rsi = ctx.ncol("low"), ctx.ncol("high"), ctx.ncol("rsi14")
        prev = lambda a: np.r_[np.nan, a[:-1]]                                     # noqa: E731 - ligne i-1
        p_rsi, p_low, p_high = prev(rsi), prev(low), prev(high)
        body = np.abs(close - opn)
        with np.errstate(invalid="ignore"):
            buy = ctx.base & (p_rsi <= ext) & (close > opn) & (body > 0.5 * atr)
            sell = ctx.base & ~buy & (p_rsi >= 100 - ext) & (close < opn) & (body > 0.5 * atr)
        return buy, np.where(buy, 1, np.where(sell, -1, 0)), np.minimum(p_low, low), np.maximum(p_high, high)
    key = ("g3", "exh", ext)
    buy, side, lo_ref, hi_ref = _cached(ctx, key, donnees)
    xp = ctx.xp
    k = p.get("sl_atr", 0.8)
    atr, close = ctx.col("atr14"), ctx.col("close")
    sl = xp.where(ctx.dev(buy, key + ("buy",)), ctx.dev(lo_ref, key + ("lo",)) - k * atr * 0.5,
                  ctx.dev(hi_ref, key + ("hi",)) + k * atr * 0.5)
    return build(ctx.dev(side, key + ("side",)), close, sl, p.get("rr", 1.8))


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


def _sr_levels(ctx: FastCtx) -> dict:
    """Niveaux de support_resistance (100 barres, tolérance 0,5 ATR) pour chaque bougie active, calculés une fois par
    contexte (2026-09-29 : 0,8 s par configuration sinon). Même arithmétique que l'indicateur."""
    if "sr_levels" in ctx._sw:
        return ctx._sw["sr_levels"]
    from ..market_data.indicators import swing_points
    sh, sl = swing_points(ctx.e)
    piv_i = np.array([q[0] for q in sh] + [q[0] for q in sl], dtype=np.int64)
    piv_p = np.array([q[1] for q in sh] + [q[1] for q in sl], dtype=float)
    ref = _window_atr(ctx)
    res: dict = {}
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
        res[int(i)] = [float(np.mean(c)) for c in clusters]
    ctx._sw["sr_levels"] = res
    return res


@register_fast("sr_rejection", [{}, {"with_trend": True}, {"tol_atr": 0.6, "sl_atr": 1.5, "rr": 3.0}])
def sr_rejection(ctx: FastCtx, p: dict):
    tol_atr, wt = p.get("tol_atr", 0.3), p.get("with_trend")

    def donnees():
        close, opn, atr = ctx.ncol("close"), ctx.ncol("open"), ctx.ncol("atr14")
        low, high = ctx.ncol("low"), ctx.ncol("high")
        tr = _lt_trend(ctx)
        niveaux = _sr_levels(ctx)                          # ne dépend que des données : partagé entre configurations
        side = np.zeros(ctx.n, dtype=np.int8)
        ref = np.full(ctx.n, np.nan)                        # prix de référence du stop (bas si achat, haut si vente)
        for i in np.nonzero(ctx.base)[0]:
            lv_ = niveaux.get(int(i))
            if not lv_:
                continue
            t = tol_atr * atr[i]
            near_low = [x for x in lv_ if abs(low[i] - x) <= t]
            near_high = [x for x in lv_ if abs(high[i] - x) <= t]
            if near_low and close[i] > opn[i] and close[i] > near_low[0] and (not wt or tr[i] == 1):
                side[i], ref[i] = 1, float(low[i])
            elif near_high and close[i] < opn[i] and close[i] < near_high[0] and (not wt or tr[i] == -1):
                side[i], ref[i] = -1, float(high[i])
        return side, ref
    key = ("g3", "sr", tol_atr, bool(wt))
    side, ref = _cached(ctx, key, donnees)
    xp = ctx.xp
    k = p.get("sl_atr", 1.0)
    atr, close = ctx.col("atr14"), ctx.col("close")
    side_x, ref_x = ctx.dev(side, key + ("side",)), ctx.dev(ref, key + ("ref",))
    slv = xp.where(side_x > 0, ref_x - k * atr * 0.5, xp.where(side_x < 0, ref_x + k * atr * 0.5, xp.nan))
    return build(side_x, close, slv, p.get("rr", 2.0))


@register_fast("donchian_breakout", [{}, {"lookback": 55, "sl_atr": 2.0, "rr": 3.0}, {"lookback": 10}])
def donchian_breakout(ctx: FastCtx, p: dict):
    n = int(p.get("lookback", 20))

    def donnees():
        close = ctx.ncol("close")
        high, low = ctx.ncol("high"), ctx.ncol("low")
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
        return np.where(buy, 1, np.where(sell, -1, 0))
    key = ("g3", "don", n)
    side_x = ctx.dev(_cached(ctx, key, donnees), key)
    k = p.get("sl_atr", 1.5)
    atr, close = ctx.col("atr14"), ctx.col("close")
    sl = close - side_x * k * atr
    return build(side_x, close, sl, p.get("rr", 2.5))


@register_fast("rsi2_reversion", [{}, {"rsi_lo": 20, "rsi_hi": 80, "sl_atr": 1.0}])
def rsi2_reversion(ctx: FastCtx, p: dict):
    lo, hi = float(p.get("rsi_lo", 10)), float(p.get("rsi_hi", 90))

    def donnees():
        close, e200 = ctx.ncol("close"), ctx.ncol("ema200")
        v = ctx.ncol("rsi2")
        tr = _lt_trend(ctx)
        with np.errstate(invalid="ignore"):
            ok = ctx.base & ~np.isnan(v)
            buy = ok & (v < lo) & (close > e200) & (tr != -1)
            sell = ok & ~buy & (v > hi) & (close < e200) & (tr != 1)
        return np.where(buy, 1, np.where(sell, -1, 0))
    key = ("g3", "rsi2", lo, hi)
    side_x = ctx.dev(_cached(ctx, key, donnees), key)
    k = p.get("sl_atr", 1.5)
    atr, close = ctx.col("atr14"), ctx.col("close")
    sl = close - side_x * k * atr
    return build(side_x, close, sl, p.get("rr", 1.5))
