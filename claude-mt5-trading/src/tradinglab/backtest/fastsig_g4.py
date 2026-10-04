"""Jumeau vectorisé — groupe 4 : session_breakout (agents/screeners.py), cassure du range d'une fenêtre horaire.

2026-10-01, décision utilisateur (« 6 ok » : réorienter la recherche vers de nouvelles familles) : sans jumeau, la recherche
en masse ne pouvait pas explorer les cassures de range d'ouverture (Asie, Londres, New York, Sydney). Mêmes règles, dans
le même ordre, que le screener ; équivalence bougie par bougie vérifiée par tests/test_fastsig_2026_09_29.py.

Règles du screener reproduites :
- range = plus haut / plus bas des bougies de la fenêtre [start, end) du jour de la dernière bougie clôturée, bougies
  jusqu'à celle-ci incluse (fenêtre de la veille au jour si end <= start) ; aucune bougie → pas de signal ;
- range entre 0,3 et 3 ATR ;
- achat : clôture au-dessus du haut, ouverture en dessous ou égale (première cassure) ; stop = max(bas, entrée − sl_atr ×
  ATR) puis au moins 0,5 ATR sous l'entrée ; vente symétrique ;
- option `trend_only` : seulement dans le sens de la tendance du cadre supérieur.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .fastsig import FastCtx, build, register_fast
from .fastsig_g3 import _cached, _lt_trend


def _hhmm(s: str) -> pd.Timedelta:
    h, m = (int(x) for x in str(s).split(":"))
    return pd.Timedelta(hours=h, minutes=m)


def _range_de_session(ctx: FastCtx, start: str, end: str) -> tuple[np.ndarray, np.ndarray]:
    """(haut, bas) du range de la fenêtre pour chaque bougie i, sur les bougies 0..i (NaN si aucune bougie).
    Temps en nanosecondes explicites : selon la source, les dates sont en microsecondes ou en nanosecondes."""
    t = pd.DatetimeIndex(pd.to_datetime(ctx.e["time"], utc=True)).tz_convert("UTC").tz_localize(None)
    tv = t.values.astype("datetime64[ns]").astype(np.int64)
    high, low = ctx.ncol("high"), ctx.ncol("low")
    n = ctx.n
    haut, bas = np.full(n, np.nan), np.full(n, np.nan)
    if n == 0:
        return haut, bas
    jour_ns = 86_400_000_000_000
    jours = tv - np.mod(tv, jour_ns)
    uniques, debut_j = np.unique(jours, return_index=True)
    fin_j = np.r_[debut_j[1:], n]
    ds, de = int(_hhmm(start).value), int(_hhmm(end).value)
    veille = de <= ds
    for jour, a_i, b_i in zip(uniques, debut_j, fin_j):
        ws = int(jour) + ds - (jour_ns if veille else 0)
        we = int(jour) + de
        a = int(np.searchsorted(tv, ws, side="left"))
        b = int(np.searchsorted(tv, we, side="left"))
        if b <= a:
            continue
        cmax = np.maximum.accumulate(high[a:b])
        cmin = np.minimum.accumulate(low[a:b])
        idx = np.arange(a_i, b_i)
        ok = idx >= a
        k = np.minimum(idx[ok], b - 1) - a
        haut[idx[ok]], bas[idx[ok]] = cmax[k], cmin[k]
    return haut, bas


@register_fast("session_breakout", [{}, {"start": "13:30", "end": "14:00", "trend_only": True, "sl_atr": 3.0},
                                    {"start": "22:00", "end": "02:00", "rr": 3.0}, {"start": "07:00", "end": "08:00"}])
def session_breakout(ctx: FastCtx, p: dict):
    start, end = str(p.get("start", "00:00")), str(p.get("end", "07:00"))
    trend_only = bool(p.get("trend_only"))

    def donnees():
        haut, bas = _range_de_session(ctx, start, end)
        close, open_, atr = ctx.ncol("close"), ctx.ncol("open"), ctx.ncol("atr14")
        rng = haut - bas
        with np.errstate(invalid="ignore"):
            ok = ctx.base & np.isfinite(haut) & np.isfinite(bas) & (haut > bas) & ~(rng > 3 * atr) & ~(rng < 0.3 * atr)
            buy = ok & (close > haut) & (open_ <= haut)
            sell = ok & ~buy & (close < bas) & (open_ >= bas)
        if trend_only:
            tr = _lt_trend(ctx)
            buy &= tr == 1
            sell &= tr == -1
        side = np.where(buy, 1, np.where(sell, -1, 0)).astype(np.int8)
        return side, haut, bas
    key = ("g4", "sess", start, end, trend_only)
    side_np, haut_np, bas_np = _cached(ctx, key, donnees)
    xp = ctx.xp
    side = ctx.dev(side_np, key + ("side",))
    haut, bas = ctx.dev(haut_np, key + ("haut",)), ctx.dev(bas_np, key + ("bas",))
    close, atr = ctx.col("close"), ctx.col("atr14")
    k = p.get("sl_atr", 1.0)                         # nombre ou colonne (P, 1)
    sl_buy = xp.minimum(xp.maximum(bas, close - k * atr), close - 0.5 * atr)
    sl_sell = xp.maximum(xp.minimum(haut, close + k * atr), close + 0.5 * atr)
    sl = xp.where(side > 0, sl_buy, sl_sell)
    return build(side, close, sl, p.get("rr", 2.0))
