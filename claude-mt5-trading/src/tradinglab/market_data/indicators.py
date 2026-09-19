"""Indicateurs techniques — fonctions pures pandas/numpy, sans état ni dépendance broker.

Conventions :
- Les DataFrames suivent le format `BrokerAdapter.rates` : colonnes time/open/high/low/close/tick_volume/spread,
  triées par temps croissant, `time` en UTC.
- La DERNIÈRE ligne d'un DataFrame est considérée comme la barre EN FORMATION (non clôturée). Toute décision
  de trading doit se baser sur `last_closed(df)` (avant-dernière ligne) pour éviter le lookahead.
- Les lissages EMA/Wilder utilisent `ewm(adjust=False)` : ils restent définis sur des séries courtes (biais de
  démarrage accepté) ; les indicateurs à fenêtre glissante (Bollinger, percentile de volatilité) renvoient NaN
  tant que la fenêtre n'est pas remplie.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

ENRICHED_COLUMNS = [
    "ema20", "ema50", "ema200", "rsi14", "macd", "macd_signal", "macd_hist", "atr14",
    "bb_mid", "bb_up", "bb_low", "bb_width", "adx14", "vol_pct", "mom10", "ret1",
]


# ----------------------------------------------------------------------------------------------------------
# Moyennes et oscillateurs
# ----------------------------------------------------------------------------------------------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    """Moyenne mobile exponentielle (span = n, adjust=False, comme MT5)."""
    return s.astype(float).ewm(span=n, adjust=False, min_periods=1).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    """Moyenne mobile simple sur n barres (NaN tant que la fenêtre n'est pas pleine)."""
    return s.astype(float).rolling(n, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Lissage de Wilder (RMA) : alpha = 1/n."""
    return s.astype(float).ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """RSI de Wilder, borné dans [0, 100]. Renvoie 100 si aucune perte, 0 si aucun gain."""
    s = s.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0.0).fillna(0.0)
    loss = (-delta).clip(lower=0.0).fillna(0.0)
    avg_gain = _wilder(gain, n)
    avg_loss = _wilder(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss > 0, 100.0)
    out = out.where(~((avg_loss <= 0) & (avg_gain <= 0)), 50.0)  # série plate : neutre
    return out.clip(0.0, 100.0)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD classique : (ligne macd, ligne signal, histogramme)."""
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range : max(h-l, |h-close_prev|, |l-close_prev|). Première barre : h-l."""
    h = df["high"].astype(float)
    lo = df["low"].astype(float)
    prev = df["close"].astype(float).shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    return tr.fillna(h - lo)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR de Wilder sur le true range."""
    if len(df) == 0:
        return pd.Series(dtype=float, index=df.index)
    return _wilder(true_range(df), n)


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bandes de Bollinger : (médiane SMA n, bande haute, bande basse) avec écart-type population (ddof=0)."""
    s = s.astype(float)
    mid = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ADX de Wilder (0-100) : force de tendance, indépendante de la direction."""
    if len(df) == 0:
        return pd.Series(dtype=float, index=df.index)
    h = df["high"].astype(float)
    lo = df["low"].astype(float)
    up = h.diff()
    down = -lo.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * _wilder(plus_dm, n) / tr
        minus_di = 100.0 * _wilder(minus_dm, n) / tr
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    dx = dx.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return _wilder(dx, n).clip(0.0, 100.0)


def momentum(s: pd.Series, n: int = 10) -> pd.Series:
    """Momentum simple : close - close(n barres avant)."""
    s = s.astype(float)
    return s - s.shift(n)


def volatility_percentile(atr_series: pd.Series, lookback: int = 200) -> pd.Series:
    """Rang percentile (0-100) de l'ATR courant dans la fenêtre glissante `lookback`.

    NaN tant que la fenêtre n'est pas remplie (pas de percentile fiable sur un petit échantillon).
    """
    s = atr_series.astype(float)
    return s.rolling(lookback, min_periods=lookback).rank(pct=True) * 100.0


# ----------------------------------------------------------------------------------------------------------
# Structure de marché (swings)
# ----------------------------------------------------------------------------------------------------------
def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Points pivots confirmés, sans lookahead.

    Un swing haut à l'indice i est confirmé si high[i] est strictement supérieur aux `left` highs précédents
    et supérieur ou égal aux `right` highs suivants (en cas de plateau, le premier point est le pivot) ;
    symétrique pour les swings bas. Un pivot n'est émis qu'une fois `right` barres écoulées après lui :
    ajouter des barres futures ne modifie jamais un pivot déjà confirmé.
    Renvoie (swing_highs, swing_lows) sous forme de listes de (index positionnel, prix).
    """
    n = len(df)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    sh: list[tuple[int, float]] = []
    sl: list[tuple[int, float]] = []
    for i in range(left, n - right):
        h = highs[i]
        if h > highs[i - left:i].max() and h >= highs[i + 1:i + right + 1].max():
            sh.append((i, float(h)))
        lo = lows[i]
        if lo < lows[i - left:i].min() and lo <= lows[i + 1:i + right + 1].min():
            sl.append((i, float(lo)))
    return sh, sl


def structure_label(df: pd.DataFrame, left: int = 3, right: int = 3) -> str:
    """Étiquette de structure d'après les 2 derniers swings hauts et bas confirmés.

    - "HH_HL" : plus haut plus haut ET plus bas plus haut (tendance haussière)
    - "LH_LL" : plus haut plus bas ET plus bas plus bas (tendance baissière)
    - "MIXED" : configuration contradictoire
    - "UNKNOWN" : moins de 2 swings d'un côté
    """
    sh, sl = swing_points(df, left, right)
    if len(sh) < 2 or len(sl) < 2:
        return "UNKNOWN"
    hh = sh[-1][1] > sh[-2][1]
    hl = sl[-1][1] > sl[-2][1]
    if hh and hl:
        return "HH_HL"
    if (not hh) and (not hl):
        return "LH_LL"
    return "MIXED"


def support_resistance(df: pd.DataFrame, lookback: int = 100, tolerance_atr: float = 0.5) -> list[float]:
    """Niveaux de support/résistance : swings des `lookback` dernières barres regroupés par proximité.

    Deux niveaux distants de moins de `tolerance_atr * ATR14` sont fusionnés (moyenne). Résultat trié croissant.
    """
    if len(df) < 3:
        return []
    sub = df.tail(lookback).reset_index(drop=True)
    sh, sl = swing_points(sub)
    levels = sorted([p for _, p in sh] + [p for _, p in sl])
    if not levels:
        return []
    a = atr(sub, 14)
    ref_atr = float(a.iloc[-1]) if len(a) and not math.isnan(float(a.iloc[-1])) else 0.0
    tol = tolerance_atr * ref_atr
    clusters: list[list[float]] = [[levels[0]]]
    for lv in levels[1:]:
        if lv - clusters[-1][-1] <= tol:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    return [float(np.mean(c)) for c in clusters]


# ----------------------------------------------------------------------------------------------------------
# Plages de session / journalières
# ----------------------------------------------------------------------------------------------------------
def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.strip().split(":")[:2]
    return int(hh), int(mm)


def session_range(df: pd.DataFrame, start_utc: str, end_utc: str) -> tuple[float, float]:
    """(plus haut, plus bas) des barres de la session [start_utc, end_utc) du jour de la dernière barre.

    `df.time` doit être en UTC ; les heures sont au format "HH:MM". Si la session chevauche minuit
    (end < start), elle est prise du jour précédent au jour courant. (nan, nan) si aucune barre.
    """
    if len(df) == 0:
        return (math.nan, math.nan)
    t = pd.to_datetime(df["time"], utc=True)
    day = t.iloc[-1].normalize()
    sh, sm = _parse_hhmm(start_utc)
    eh, em = _parse_hhmm(end_utc)
    start = day + pd.Timedelta(hours=sh, minutes=sm)
    end = day + pd.Timedelta(hours=eh, minutes=em)
    if end <= start:
        start = start - pd.Timedelta(days=1)
    mask = (t >= start) & (t < end)
    if not mask.any():
        return (math.nan, math.nan)
    return float(df.loc[mask, "high"].max()), float(df.loc[mask, "low"].min())


def daily_high_low(df_d1: pd.DataFrame) -> tuple[float, float]:
    """(plus haut, plus bas) de la VEILLE : avant-dernière barre D1 (la dernière est le jour en cours)."""
    if len(df_d1) < 2:
        return (math.nan, math.nan)
    row = last_closed(df_d1)
    return float(row["high"]), float(row["low"])


# ----------------------------------------------------------------------------------------------------------
# Enrichissement
# ----------------------------------------------------------------------------------------------------------
def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute les colonnes d'indicateurs standard à une copie du DataFrame de barres.

    Colonnes : ema20, ema50, ema200, rsi14, macd, macd_signal, macd_hist, atr14, bb_mid, bb_up, bb_low,
    bb_width (=(up-low)/mid), adx14, vol_pct, mom10, ret1 (close/close_prev - 1).
    Ne lève jamais sur un df court ou vide : les colonnes non calculables sont NaN.
    """
    out = df.copy()
    if len(out) == 0:
        for c in ENRICHED_COLUMNS:
            out[c] = pd.Series(dtype=float)
        return out
    close = out["close"].astype(float)
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["ema200"] = ema(close, 200)
    out["rsi14"] = rsi(close, 14)
    m, sg, h = macd(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = m, sg, h
    out["atr14"] = atr(out, 14)
    mid, up, low = bollinger(close, 20, 2.0)
    out["bb_mid"], out["bb_up"], out["bb_low"] = mid, up, low
    with np.errstate(divide="ignore", invalid="ignore"):
        out["bb_width"] = ((up - low) / mid).replace([np.inf, -np.inf], np.nan)
    out["adx14"] = adx(out, 14)
    out["vol_pct"] = volatility_percentile(out["atr14"], 200)
    out["mom10"] = momentum(close, 10)
    out["ret1"] = close.pct_change()
    return out


def last_closed(df: pd.DataFrame) -> pd.Series:
    """Dernière barre CLÔTURÉE = avant-dernière ligne.

    Par convention, la dernière ligne renvoyée par le broker est la barre en cours de formation (son close
    bouge encore) ; l'utiliser pour un signal introduirait un lookahead. Lève ValueError si moins de 2 barres.
    """
    if len(df) < 2:
        raise ValueError("last_closed : au moins 2 barres nécessaires (la dernière est en formation)")
    return df.iloc[-2]
