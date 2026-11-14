"""Classification déterministe du régime de marché à partir d'un DataFrame enrichi (`indicators.enrich`).

Aucune interprétation LLM ici : règles fixes, traçables, `Provenance.CALCULATED`.
La barre de référence est la dernière barre CLÔTURÉE (`indicators.last_closed`), jamais la barre en formation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ..core.types import Provenance, Regime
from .indicators import last_closed

# Seuils (documentés, modifiables en un seul endroit)
MIN_BARS = 60                  # en dessous : UNCERTAIN
VOL_HIGH_PCT = 85.0            # vol_pct >= -> HIGH_VOLATILITY
VOL_LOW_PCT = 15.0             # vol_pct <= -> LOW_VOLATILITY
ADX_TREND = 25.0               # adx14 >= -> tendance possible
ADX_RANGE = 20.0               # adx14 <  -> range possible
BB_COMPRESSION_PCT = 20.0      # percentile de bb_width (sur 100 barres) <= -> compression
BB_WIDTH_LOOKBACK = 100
RANGE_WINDOW = 20              # barres inspectées pour l'oscillation autour de bb_mid
RANGE_MIN_CROSSES = 3          # nombre minimal de traversées de bb_mid


@dataclass
class RegimeResult:
    regime: Regime
    confidence: float                       # 0-1, heuristique (pas une probabilité calibrée)
    features: dict = field(default_factory=dict)
    provenance: Provenance = Provenance.CALCULATED

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "confidence": round(float(self.confidence), 3),
            "features": self.features,
            "provenance": self.provenance.value,
        }


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v


def _isnan(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _ema_order(row: pd.Series) -> str:
    """"UP" si ema20 > ema50 > ema200, "DOWN" si ema20 < ema50 < ema200, sinon "FLAT"."""
    e20, e50, e200 = _f(row.get("ema20")), _f(row.get("ema50")), _f(row.get("ema200"))
    if any(_isnan(v) for v in (e20, e50, e200)):
        return "FLAT"
    if e20 > e50 > e200:
        return "UP"
    if e20 < e50 < e200:
        return "DOWN"
    return "FLAT"


def trend_direction(df_enriched: pd.DataFrame) -> str:
    """Direction de tendance sur la dernière barre clôturée.

    "UP" : ema20 > ema50 > ema200 et close > ema20 ; "DOWN" : symétrique ; sinon "FLAT".
    """
    if len(df_enriched) < 2:
        return "FLAT"
    row = last_closed(df_enriched)
    order = _ema_order(row)
    close, e20 = _f(row.get("close")), _f(row.get("ema20"))
    if order == "UP" and close > e20:
        return "UP"
    if order == "DOWN" and close < e20:
        return "DOWN"
    return "FLAT"


def _bb_width_percentile(df: pd.DataFrame, end_pos: int, lookback: int = BB_WIDTH_LOOKBACK) -> float:
    """Rang percentile (0-100) de bb_width à la position `end_pos` parmi les `lookback` barres finissant là."""
    w = df["bb_width"].iloc[max(0, end_pos - lookback + 1): end_pos + 1].dropna()
    if len(w) < 20:
        return math.nan
    cur = float(w.iloc[-1])
    return float((w <= cur).mean() * 100.0)


def _mid_crosses(df: pd.DataFrame, end_pos: int, window: int = RANGE_WINDOW) -> int:
    """Nombre de changements de signe de (close - bb_mid) sur les `window` barres finissant à end_pos."""
    sub = df.iloc[max(0, end_pos - window + 1): end_pos + 1]
    diff = (sub["close"].astype(float) - sub["bb_mid"].astype(float)).dropna()
    if len(diff) < 3:
        return 0
    sign = np.sign(diff.to_numpy())
    sign = sign[sign != 0]
    return int((sign[1:] != sign[:-1]).sum()) if len(sign) > 1 else 0


def classify_regime(df_enriched: pd.DataFrame, spread_points: int = 0, point: float = 0.0,
                    news_shock: bool = False, risk_sentiment: Optional[str] = None) -> RegimeResult:
    """Classe le régime de marché selon des règles déterministes, dans cet ordre de priorité :

    1. `news_shock`                                          -> NEWS_SHOCK
    2. < 60 barres, ou atr14/adx14 NaN sur la barre de référence -> UNCERTAIN
    3. vol_pct >= 85                                         -> HIGH_VOLATILITY
    4. vol_pct <= 15                                         -> LOW_VOLATILITY
    5. adx14 >= 25 et EMA ordonnées (20/50/200) et close du bon côté de l'ema20 -> TRENDING
    6. compression de bb_width (percentile <= 20 sur 100 barres, mesuré sur la barre précédente)
       puis clôture de la barre de référence hors des bandes -> BREAKOUT
    7. adx14 < 20 et prix oscillant autour de bb_mid (>= 3 traversées sur 20 barres) -> RANGING
    8. sinon                                                 -> UNCERTAIN

    `spread_points`/`point` et `risk_sentiment` ("RISK_ON"/"RISK_OFF") sont reportés dans `features` pour
    la traçabilité (le spread relatif à l'ATR est calculé si `point` > 0) mais ne changent pas le régime
    technique : le sentiment macro est un axe distinct, interprété en aval.
    La barre de référence est la dernière barre CLÔTURÉE (avant-dernière ligne).
    """
    features: dict = {
        "bars": int(len(df_enriched)), "adx": math.nan, "vol_pct": math.nan, "ema_order": "FLAT",
        "bb_width_pct": math.nan, "direction": "FLAT", "rsi": math.nan, "atr": math.nan,
        "spread_points": int(spread_points), "spread_atr_ratio": math.nan,
        "risk_sentiment": risk_sentiment, "news_shock": bool(news_shock),
    }
    if news_shock:
        return RegimeResult(Regime.NEWS_SHOCK, 1.0, features)

    n = len(df_enriched)
    if n < max(MIN_BARS, 2):
        features["reason"] = f"données insuffisantes ({n} barres < {MIN_BARS})"
        return RegimeResult(Regime.UNCERTAIN, 0.0, features)

    ref_pos = n - 2
    row = df_enriched.iloc[ref_pos]
    adx_v, atr_v, vol_pct = _f(row.get("adx14")), _f(row.get("atr14")), _f(row.get("vol_pct"))
    close = _f(row.get("close"))
    order = _ema_order(row)
    direction = trend_direction(df_enriched)
    bb_pct_prev = _bb_width_percentile(df_enriched, ref_pos - 1)
    features.update({
        "adx": round(adx_v, 2) if not _isnan(adx_v) else math.nan,
        "atr": atr_v, "vol_pct": round(vol_pct, 1) if not _isnan(vol_pct) else math.nan,
        "ema_order": order, "direction": direction,
        "bb_width_pct": round(bb_pct_prev, 1) if not _isnan(bb_pct_prev) else math.nan,
        "rsi": _f(row.get("rsi14")),
    })
    if point > 0 and not _isnan(atr_v) and atr_v > 0:
        features["spread_atr_ratio"] = round(spread_points * point / atr_v, 4)

    if _isnan(adx_v) or _isnan(atr_v):
        features["reason"] = "atr14/adx14 indisponibles"
        return RegimeResult(Regime.UNCERTAIN, 0.0, features)

    # 3-4. Volatilité extrême (nécessite vol_pct calculé : 200 barres d'ATR)
    if not _isnan(vol_pct):
        if vol_pct >= VOL_HIGH_PCT:
            features["reason"] = f"vol_pct {vol_pct:.0f} >= {VOL_HIGH_PCT:.0f}"
            return RegimeResult(Regime.HIGH_VOLATILITY, _clamp(0.6 + (vol_pct - VOL_HIGH_PCT) / 37.5), features)
        if vol_pct <= VOL_LOW_PCT:
            features["reason"] = f"vol_pct {vol_pct:.0f} <= {VOL_LOW_PCT:.0f}"
            return RegimeResult(Regime.LOW_VOLATILITY, _clamp(0.6 + (VOL_LOW_PCT - vol_pct) / 37.5), features)

    # 5. Tendance
    if adx_v >= ADX_TREND and order != "FLAT" and direction == order:
        features["reason"] = f"adx {adx_v:.1f} >= {ADX_TREND:.0f}, EMA {order}"
        return RegimeResult(Regime.TRENDING, _clamp(0.5 + (adx_v - ADX_TREND) / 50.0), features)

    # 6. Breakout après compression
    bb_up, bb_low = _f(row.get("bb_up")), _f(row.get("bb_low"))
    if (not _isnan(bb_pct_prev) and bb_pct_prev <= BB_COMPRESSION_PCT
            and not _isnan(bb_up) and (close > bb_up or close < bb_low)):
        features["reason"] = f"compression bb_width pct {bb_pct_prev:.0f} puis clôture hors bandes"
        features["direction"] = "UP" if close > bb_up else "DOWN"
        return RegimeResult(Regime.BREAKOUT, _clamp(0.5 + (BB_COMPRESSION_PCT - bb_pct_prev) / 40.0), features)

    # 7. Range
    crosses = _mid_crosses(df_enriched, ref_pos)
    features["mid_crosses"] = crosses
    if adx_v < ADX_RANGE and crosses >= RANGE_MIN_CROSSES:
        features["reason"] = f"adx {adx_v:.1f} < {ADX_RANGE:.0f}, {crosses} traversées de bb_mid"
        return RegimeResult(Regime.RANGING, _clamp(0.5 + (ADX_RANGE - adx_v) / 40.0 + crosses / 40.0), features)

    features["reason"] = "aucune règle satisfaite"
    return RegimeResult(Regime.UNCERTAIN, 0.2, features)


def regime_series(df_enriched: pd.DataFrame) -> np.ndarray:
    """Régime de TOUTES les bougies en une passe (2026-09-30, accélération de la recherche en masse).

    `out[j]` = `classify_regime(prefixe).regime.value` quand la dernière barre clôturée du préfixe est la ligne j
    (préfixe = lignes 0..j + une barre en formation). Mêmes règles, dans le même ordre, que `classify_regime` ;
    équivalence stricte vérifiée par tests/test_regime_serie_2026_09_30.py."""
    n = len(df_enriched)
    col = lambda c: df_enriched[c].to_numpy(dtype=float) if c in df_enriched.columns else np.full(n, np.nan)  # noqa: E731
    adx, atr, vol, close = col("adx14"), col("atr14"), col("vol_pct"), col("close")
    e20, e50, e200 = col("ema20"), col("ema50"), col("ema200")
    bb_up, bb_low, bb_mid, bw = col("bb_up"), col("bb_low"), col("bb_mid"), col("bb_width")
    out = np.full(n, Regime.UNCERTAIN.value, dtype=object)
    j = np.arange(n)
    with np.errstate(invalid="ignore"):
        order = np.where((e20 > e50) & (e50 > e200), 1, np.where((e20 < e50) & (e50 < e200), -1, 0))
        direction = np.where((order == 1) & (close > e20), 1, np.where((order == -1) & (close < e20), -1, 0))
        # percentile de bb_width à la bougie j-1 (fenêtre de 100 bougies, NaN ignorés, au moins 20 valeurs)
        pct = np.full(n, np.nan)
        pad = np.concatenate([np.full(BB_WIDTH_LOOKBACK - 1, np.nan), bw])
        W = np.lib.stride_tricks.sliding_window_view(pad, BB_WIDTH_LOOKBACK)      # W[e] = bw[e-99 .. e]
        valid = ~np.isnan(W)
        cnt = valid.sum(axis=1)
        last = BB_WIDTH_LOOKBACK - 1 - np.argmax(valid[:, ::-1], axis=1)
        cur = W[np.arange(len(W)), last]
        le = (valid & (W <= cur[:, None])).sum(axis=1)
        p = np.where(cnt >= 20, le / np.where(cnt > 0, cnt, 1) * 100.0, np.nan)
        pct[1:] = p[:-1]                                                             # mesuré sur la bougie précédente
        base = (j + 2 >= max(MIN_BARS, 2)) & ~np.isnan(adx) & ~np.isnan(atr)
        reste = base.copy()
        hv = reste & ~np.isnan(vol) & (vol >= VOL_HIGH_PCT)
        out[hv] = Regime.HIGH_VOLATILITY.value
        reste &= ~hv
        lv = reste & ~np.isnan(vol) & (vol <= VOL_LOW_PCT)
        out[lv] = Regime.LOW_VOLATILITY.value
        reste &= ~lv
        tr = reste & (adx >= ADX_TREND) & (order != 0) & (direction == order)
        out[tr] = Regime.TRENDING.value
        reste &= ~tr
        bo = reste & ~np.isnan(pct) & (pct <= BB_COMPRESSION_PCT) & ~np.isnan(bb_up) & ((close > bb_up) | (close < bb_low))
        out[bo] = Regime.BREAKOUT.value
        reste &= ~bo
    diff_all = close - bb_mid
    for k in np.nonzero(reste & (adx < ADX_RANGE))[0]:                               # traversées de bb_mid (20 bougies)
        d = diff_all[max(0, k - RANGE_WINDOW + 1): k + 1]
        d = d[~np.isnan(d)]
        if len(d) < 3:
            continue
        s = np.sign(d)
        s = s[s != 0]
        if len(s) > 1 and int((s[1:] != s[:-1]).sum()) >= RANGE_MIN_CROSSES:
            out[k] = Regime.RANGING.value
    return out
