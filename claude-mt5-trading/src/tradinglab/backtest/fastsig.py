"""Signaux de backtest VECTORISÉS (2026-09-29, accélération demandée par l'utilisateur).

Le backtest classique appelle la stratégie (screener) à chaque bougie sur un préfixe de DataFrame : correct mais lent
(coût de pandas à chaque appel). Ici, chaque stratégie générique a un jumeau vectorisé qui calcule en une fois, sur tout
l'historique, le signal que le screener aurait donné à chaque bougie. La simulation (`run_backtest`) ne change pas.

Règle absolue : le jumeau doit donner EXACTEMENT le même signal que le screener, bougie par bougie. `compare_signals`
le vérifie ; une stratégie n'entre dans `FAST` qu'avec un test d'équivalence (tests/test_fastsig_*.py).

Correspondances avec le screener, pour la bougie i (préfixe df.iloc[:i+1]) :
- `le` (dernière barre d'entrée clôturée) = ligne i du cadre d'entrée enrichi ;
- `lt` (dernière barre de tendance clôturée) = ligne `lt_idx[i]` du cadre de tendance enrichi ;
- `closed` (= e.iloc[:-1] dans le screener) = lignes 0..i ; un pivot j n'y est confirmé que si j <= i - right.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..market_data.indicators import swing_points

FastFn = Callable[["FastCtx", dict], tuple]
FAST: dict[str, FastFn] = {}
#: jeux de paramètres testés par le test d'équivalence (tests/test_fastsig_2026_09_29.py), déclarés avec la stratégie
TEST_PARAMS: dict[str, list[dict]] = {}

LE_COLS = ("ema20", "ema50", "ema200", "atr14", "rsi14", "adx14")      # _ctx : colonnes obligatoires de `le`
LT_COLS = ("ema20", "ema50", "ema200")                                  # _ctx : colonnes obligatoires de `lt`


def register_fast(name: str, test_params: Optional[list] = None):
    def deco(fn):
        FAST[name] = fn
        TEST_PARAMS[name] = list(test_params or [{}])
        return fn
    return deco


@dataclass
class FastCtx:
    """Tableaux alignés sur les bougies d'entrée. `E[c][i]` = colonne c de `le` ; `T[c][i]` = colonne c de `lt`."""
    e: pd.DataFrame                    # cadre d'entrée enrichi complet
    t: pd.DataFrame                    # cadre de tendance enrichi complet
    lt_idx: np.ndarray                 # index de `lt` dans t (-1 si aucune barre de tendance clôturée)
    base: np.ndarray                   # bougies où le screener est appelé ET où _ctx réussit
    E: dict = field(default_factory=dict)
    T: dict = field(default_factory=dict)
    _sw: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.e)

    def col(self, c: str) -> np.ndarray:
        if c not in self.E:
            self.E[c] = self.e[c].to_numpy(dtype=float) if c in self.e.columns else np.full(self.n, np.nan)
        return self.E[c]

    def tcol(self, c: str) -> np.ndarray:
        if c not in self.T:
            src = self.t[c].to_numpy(dtype=float) if c in self.t.columns else np.full(len(self.t), np.nan)
            out = np.full(self.n, np.nan)
            ok = self.lt_idx >= 0
            out[ok] = src[self.lt_idx[ok]]
            self.T[c] = out
        return self.T[c]

    def swings(self, left: int = 3, right: int = 3, k: int = 1):
        """Pour chaque bougie i : prix (et index) des k derniers pivots hauts / bas confirmés dans les lignes 0..i.
        Renvoie (sh_prix[k][i], sh_idx[k][i], sl_prix[k][i], sl_idx[k][i]) ; k=0 = le plus récent. NaN / -1 si absent."""
        cle = (left, right, k)
        if cle in self._sw:
            return self._sw[cle]
        sh, sl = swing_points(self.e, left, right)
        res = []
        for piv in (sh, sl):
            idx = np.array([p[0] for p in piv], dtype=np.int64)
            prix = np.array([p[1] for p in piv], dtype=float)
            # nombre de pivots confirmés à la bougie i : pivots j <= i - right
            cnt = np.searchsorted(idx, np.arange(self.n) - right, side="right")
            px_k, ix_k = [], []
            for m in range(k):
                pos = cnt - 1 - m
                ok = pos >= 0
                px = np.full(self.n, np.nan)
                ix = np.full(self.n, -1, dtype=np.int64)
                px[ok] = prix[pos[ok]]
                ix[ok] = idx[pos[ok]]
                px_k.append(px)
                ix_k.append(ix)
            res += [px_k, ix_k]
        self._sw[cle] = tuple(res)
        return self._sw[cle]


def trend_of(close, e20, e50, e200) -> np.ndarray:
    """_trend_of vectorisé : +1 UP, -1 DOWN, 0 FLAT (comparaisons avec NaN → FLAT, comme le scalaire)."""
    up = (e20 > e50) & (e50 > e200) & (close > e20)
    dn = (e20 < e50) & (e50 < e200) & (close < e20)
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def structure_sl(ctx: FastCtx, side: np.ndarray, entry: np.ndarray, atr: np.ndarray, sl_atr: float) -> np.ndarray:
    """_structure_sl vectorisé (swings 3/3 sur les lignes 0..i)."""
    sh_p, _, sl_p, _ = ctx.swings(3, 3, 1)
    low, high = sl_p[0], sh_p[0]
    buy = entry - sl_atr * atr
    buy = np.where(np.isnan(low), buy, np.minimum(buy, low - 0.2 * atr))
    buy = np.maximum(buy, entry - 3 * atr)
    sell = entry + sl_atr * atr
    sell = np.where(np.isnan(high), sell, np.maximum(sell, high + 0.2 * atr))
    sell = np.minimum(sell, entry + 3 * atr)
    return np.where(side > 0, buy, sell)


def build(side: np.ndarray, entry: np.ndarray, sl: np.ndarray, rr) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """_build vectorisé : refus si distance non finie / nulle / stop du mauvais côté ; cible finale = max(rr, 2,5) R."""
    side = side.astype(np.int8)
    with np.errstate(invalid="ignore"):
        dist = np.abs(entry - sl)
        ok = (side != 0) & np.isfinite(dist) & (dist > 0) & (side * (entry - sl) > 0)
        rr = np.maximum(np.asarray(rr, dtype=float), 2.5)
        tp = entry + side * dist * rr
    side = np.where(ok, side, 0).astype(np.int8)
    return side, np.where(ok, sl, np.nan), np.where(ok, tp, np.nan)


# ------------------------------------------------------------------------------------------------ stratégies
@register_fast("ema_trend", [{}, {"adx_min": 15, "sl_atr": 1.0, "rr": 3.0}, {"adx_min": 30, "vol_pct_max": 70}])
def ema_trend(ctx: FastCtx, p: dict):
    close, adx = ctx.col("close"), ctx.col("adx14")
    tr = trend_of(close, ctx.col("ema20"), ctx.col("ema50"), ctx.col("ema200"))
    ok = ctx.base & (tr != 0) & ~(adx < p.get("adx_min", 25))
    if p.get("vol_pct_max"):
        vp = ctx.col("vol_pct")
        ok &= ~(~np.isnan(vp) & (vp > p["vol_pct_max"]))
    tt = trend_of(ctx.tcol("close"), ctx.tcol("ema20"), ctx.tcol("ema50"), ctx.tcol("ema200"))
    ok &= (tt == tr) | (tt == 0)
    side = np.where(ok, tr, 0)
    sl = structure_sl(ctx, side, close, ctx.col("atr14"), p.get("sl_atr", 1.5))
    return build(side, close, sl, p.get("rr", 2.0))


# ------------------------------------------------------------------------------------------------ vérification
def compare_signals(slow_fn, fast_fn, df: pd.DataFrame, start: int = 0, tol: float = 1e-9) -> list[tuple]:
    """Compare, bougie par bougie, le Signal du screener (slow_fn) et celui du jumeau vectorisé (fast_fn).
    Renvoie la liste des écarts (i, lent, rapide). Les deux fonctions doivent avoir été préparées sur `df`."""
    ecarts = []
    for i in range(start, len(df)):
        pre = df.iloc[: i + 1]
        a, b = slow_fn(pre), fast_fn(pre)
        if (a is None) != (b is None):
            ecarts.append((i, a, b))
            continue
        if a is None:
            continue
        same = a.side == b.side and abs(a.sl - b.sl) <= tol * max(1.0, abs(a.sl)) and (
            (a.tp is None and b.tp is None) or (a.tp is not None and b.tp is not None and abs(a.tp - b.tp) <= tol * max(1.0, abs(a.tp))))
        if not same:
            ecarts.append((i, a, b))
    return ecarts


# stratégies converties par groupes (un module par groupe) ; absentes = repli sur le screener d'origine
for _m in ("fastsig_g1", "fastsig_g2", "fastsig_g3"):
    try:
        __import__(f"{__package__}.{_m}")
    except ImportError as _e:
        if _m not in str(_e):
            raise
