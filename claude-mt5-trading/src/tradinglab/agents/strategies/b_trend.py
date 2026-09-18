"""Famille B — suivi de tendance : une stratégie propre par agent (B01 … B10).

Conventions communes (voir `agents/screeners.py`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est la
  barre en formation et n'est JAMAIS lue ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14 du tf
  d'entrée, horodatage de la barre clôturée) ;
- `_build` construit le `TradeCandidate` (plan de TP = 1.5R / 2.5R / max(rr, 2.5)R, pénalité de spread) et refuse
  tout SL du mauvais côté ;
- `setup_score` = somme documentée de composantes (structure, tendance, alignement MTF, volatilité, qualité
  d'exécution), bornée 0-100 : ce n'est PAS une probabilité de gain ;
- données insuffisantes ou indicateur NaN → `None`, jamais une valeur inventée.

Chaque agent a sa propre confirmation, ses propres filtres, sa propre logique de SL, son propre plan de TP et sa
propre règle d'invalidation (une simple différence de paramètres ne suffit pas).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import daily_high_low, structure_label, swing_points
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------


def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


def _side_from(tr: str) -> Optional[Side]:
    return Side.BUY if tr == "UP" else Side.SELL if tr == "DOWN" else None


def _bar_hour(row: pd.Series) -> Optional[int]:
    """Heure UTC de la barre clôturée (None si l'horodatage est illisible)."""
    try:
        return int(pd.Timestamp(row["time"]).tz_convert("UTC").hour)
    except (TypeError, ValueError, AttributeError):
        try:
            return int(pd.Timestamp(row["time"]).hour)
        except (TypeError, ValueError):
            return None


def _range(row: pd.Series) -> float:
    return float(row["high"] - row["low"])


def _body_ratio(row: pd.Series) -> float:
    """Corps / amplitude de la bougie (0 si amplitude nulle)."""
    rng = _range(row)
    return float(abs(row["close"] - row["open"]) / rng) if rng > 0 else 0.0


def _close_position(row: pd.Series) -> float:
    """Position de la clôture dans l'amplitude : 1 = clôture au plus haut, 0 = au plus bas."""
    rng = _range(row)
    return float((row["close"] - row["low"]) / rng) if rng > 0 else 0.5


def _spread_ratio(snap, atr: float) -> float:
    """Spread courant exprimé en fraction de l'ATR du tf d'entrée."""
    if atr <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr)


def _bound_sl(side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> float:
    """Ramène la distance entrée→SL dans [lo·ATR, hi·ATR] sans jamais changer de côté."""
    dist = abs(entry - sl)
    dist = min(max(dist, lo * atr), hi * atr)
    return entry - side.sign * dist


def _di(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series]:
    """+DI / -DI de Wilder (0-100) calculés sur les barres fournies — `adx14` d'`enrich` ne donne pas la direction."""
    h = df["high"].astype(float)
    lo = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    up = h.diff()
    down = -lo.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([h - lo, (h - prev_close).abs(), (lo - prev_close).abs()], axis=1).max(axis=1).fillna(h - lo)
    atr_w = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean() / atr_w
        minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean() / atr_w
    return plus_di.replace([np.inf, -np.inf], np.nan).fillna(0.0), minus_di.replace([np.inf, -np.inf], np.nan).fillna(0.0)


# --------------------------------------------------------------------------------------------------------------
# B01 — ema_trend_m15 (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("B01")
def strategy_b01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B01 — Ré-accélération d'une tendance EMA établie sur M15.

    Thèse : une tendance M15 déjà en place (EMA20 > EMA50 > EMA200) dont l'ADX repart à la hausse et dont le prix
    reste collé au-dessus de l'EMA20 offre une continuation à faible risque, à condition d'entrer sur une bougie
    directionnelle et non sur un doji.

    Entrée : tendance EMA du tf d'entrée non FLAT ; clôture au-dessus (sous) de l'EMA20 sur les 3 dernières barres
    clôturées ; ADX14 >= `adx_min` ET ADX en hausse par rapport à 3 barres plus tôt.
    Confirmation : bougie clôturée dans le sens du trade avec corps >= 40 % de son amplitude.
    Filtres : tendance H1 non opposée ; spread <= 10 % de l'ATR M15 ; RSI extrême (> 75 / < 25) = pénalité.
    SL : sous (au-dessus) du plus bas (haut) des 5 dernières barres clôturées − 0,25 ATR, distance bornée à
    [0,5 ATR ; `sl_atr` ATR] (`sl_atr` sert de plafond : on n'accepte pas un stop plus large que le paramètre).
    TP : multiples de R via `rr` (plan 1.5R / 2.5R / rr·R).
    Invalidation : clôture M15 au-delà de l'EMA50.
    Score : structure 25 (EMA + 3 clôtures) + tendance 15 + ADX 0-15 + MTF 0-15 + volatilité 0-10 + exécution 0-15.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 30:
        return None
    tr = _trend_of(le)
    side = _side_from(tr)
    if side is None:
        return None
    adx_min = float(p.get("adx_min", 25))
    adx_prev = closed["adx14"].iloc[-4]
    if pd.isna(adx_prev) or le["adx14"] < adx_min or le["adx14"] <= adx_prev:
        return None
    last3 = closed.iloc[-3:]
    if side is Side.BUY and not (last3["close"] > last3["ema20"]).all():
        return None
    if side is Side.SELL and not (last3["close"] < last3["ema20"]).all():
        return None
    body = _body_ratio(le)
    if body < 0.4 or side.sign * (le["close"] - le["open"]) <= 0:
        return None
    if _trend_of(lt) not in (tr, "FLAT"):
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    entry = float(le["close"])
    last5 = closed.iloc[-5:]
    raw_sl = float(last5["low"].min()) - 0.25 * atr if side is Side.BUY else float(last5["high"].max()) + 0.25 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 1.5)))
    # score : composantes documentées
    score = 25.0 + 15.0 + _clamp((le["adx14"] - adx_min) * 1.5, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    atr_ma = closed["atr14"].iloc[-30:].mean()
    if atr_ma > 0 and 0.8 <= atr / atr_ma <= 1.6:
        score += 10
        pros.append("volatilité normale (ATR proche de sa moyenne 30 barres)")
    score += _clamp(body * 15, 0, 15)
    pros += [f"EMA alignées {tr} sur {spec.timeframes['entry']}", "3 clôtures consécutives du bon côté de l'EMA20",
             f"ADX {le['adx14']:.0f} en hausse", f"bougie directionnelle (corps {body:.0%})"]
    cons = []
    if (side is Side.BUY and le["rsi14"] > 75) or (side is Side.SELL and le["rsi14"] < 25):
        cons.append(f"RSI {le['rsi14']:.0f} extrême : entrée tardive possible")
        score -= 10
    if abs(entry - raw_sl) > float(p.get("sl_atr", 1.5)) * atr:
        cons.append("stop structurel plus large que le plafond ATR : SL ramené au plafond")
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà de l'EMA50", bt)


# --------------------------------------------------------------------------------------------------------------
# B02 — ema_trend_h1 (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("B02")
def strategy_b02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B02 — Reprise de tendance H1 après compression des EMA20/EMA50, dans le sens de la tendance H4.

    Thèse : en tendance H4, une phase où l'EMA20 et l'EMA50 H1 se rapprochent (consolidation) puis s'écartent à
    nouveau dans le sens de la tendance marque la fin de la pause ; l'entrée se fait sur une clôture qui dépasse
    les clôtures récentes (cassure de clôture, pas de mèche).

    Entrée : tendance H4 (`_trend_of` de la barre H4 clôturée) UP/DOWN ; sur H1, écart |EMA20−EMA50| passé
    sous 0,4 ATR dans les 12 barres précédentes, écart courant >= 0,2 ATR dans le sens de la tendance et en
    augmentation ; clôture > max (BUY) / < min (SELL) des 5 clôtures précédentes.
    Confirmation : ADX14 H1 >= `adx_min`.
    Filtres : spread <= 12 % de l'ATR H1 ; largeur des bandes de Bollinger en expansion = bonus.
    SL : derrière l'EMA50 H1 (− 0,3 ATR), distance bornée à [0,6 ATR ; 3 ATR] ; `sl_atr` si l'EMA50 est illisible.
    TP : multiples de R via `rr`.
    Invalidation : clôture H1 au-delà de l'EMA50 (la reprise a échoué).
    Score : structure 25 + expansion 0-10 + tendance 10 + ADX 0-10 + MTF 15 + volatilité 0-10 + exécution 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    gap = (closed["ema20"] - closed["ema50"]) / atr
    window = gap.iloc[-13:-1]
    if window.isna().any() or pd.isna(gap.iloc[-1]) or pd.isna(gap.iloc[-2]):
        return None
    if window.abs().min() > 0.4:
        return None  # pas de compression récente
    g_now, g_prev = float(gap.iloc[-1]), float(gap.iloc[-2])
    if side.sign * g_now < 0.2 or side.sign * (g_now - g_prev) <= 0:
        return None
    prev_closes = closed["close"].iloc[-6:-1]
    entry = float(le["close"])
    if side is Side.BUY and entry <= float(prev_closes.max()):
        return None
    if side is Side.SELL and entry >= float(prev_closes.min()):
        return None
    adx_min = float(p.get("adx_min", 22))
    if le["adx14"] < adx_min:
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    ema50 = float(le["ema50"])
    raw_sl = ema50 - side.sign * 0.3 * atr
    if side.sign * (entry - raw_sl) <= 0:
        raw_sl = entry - side.sign * float(p.get("sl_atr", 1.5)) * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.6, 3.0)
    score = 25.0 + _clamp((abs(g_now) - 0.2) * 40, 0, 10) + 10.0 + _clamp((le["adx14"] - adx_min) * 1.0, 0, 10) + 15.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}", f"compression EMA20/50 (min {window.abs().min():.2f} ATR) puis expansion",
            "cassure des 5 dernières clôtures", f"ADX {le['adx14']:.0f}"]
    cons = []
    bbw_prev = closed["bb_width"].iloc[-4]
    if _valid(le, "bb_width") and pd.notna(bbw_prev) and le["bb_width"] > bbw_prev:
        score += 10
        pros.append("largeur de Bollinger en expansion")
    else:
        cons.append("pas d'expansion de volatilité mesurable")
    cp = _close_position(le) if side is Side.BUY else 1 - _close_position(le)
    score += _clamp((cp - 0.5) * 20, 0, 10)
    if cp < 0.5:
        cons.append("clôture dans la moitié défavorable de la bougie")
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture H1 au-delà de l'EMA50 H1", bt)


# --------------------------------------------------------------------------------------------------------------
# B03 — mtf_trend_m5_h1 (M5 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("B03")
def strategy_b03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B03 — Repli M5 sur l'EMA20 en tendance H1, entrée sur cassure du haut (bas) du repli.

    Thèse : en tendance H1, un repli M5 qui touche l'EMA20 sans clôturer sous l'EMA50 est une pause ; la reprise
    est validée seulement quand une clôture dépasse l'extrême du repli (on n'achète pas le contact lui-même).

    Entrée : tendance H1 UP/DOWN ; parmi les 6 barres clôturées précédant la barre de signal, une barre de contact
    (low <= EMA20 et close >= EMA50 pour un BUY) avec RSI14 dans [`rsi_lo`, `rsi_hi`] ; la barre de signal clôture
    au-dessus du plus haut des barres du repli (contact → signal exclu) et au-dessus de son EMA20.
    Confirmation : corps de la barre de signal >= 30 % de son amplitude.
    Filtres : spread <= 25 % de l'ATR M5 (le M5 est sensible aux coûts) ; repli de plus de 4 barres = pénalité.
    SL : sous (au-dessus) l'extrême du repli − 0,2 ATR, distance bornée à [`sl_atr`·0,5 ATR ; 3 ATR].
    TP : multiples de R via `rr`.
    Invalidation : clôture M5 au-delà de l'extrême du repli.
    Score : structure 20 + tendance 15 (+10 si EMA20>EMA50 M5) + MTF 15 (+5 structure HH_HL) + volatilité 0-10
    + exécution 0-10 − 5 si repli long.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 30:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    lo, hi = float(p.get("rsi_lo", 40)), float(p.get("rsi_hi", 60))
    n = len(closed)
    contact = None
    for j in range(n - 2, max(n - 8, 0) - 1, -1):  # 6 barres avant la barre de signal (indice n-1)
        r = closed.iloc[j]
        if not _valid(r, "ema20", "ema50", "rsi14"):
            continue
        touched = (r["low"] <= r["ema20"] and r["close"] >= r["ema50"]) if side is Side.BUY else \
            (r["high"] >= r["ema20"] and r["close"] <= r["ema50"])
        if touched and lo <= r["rsi14"] <= hi:
            contact = j
            break
    if contact is None:
        return None
    pull = closed.iloc[contact:n - 1]
    entry = float(le["close"])
    if side is Side.BUY:
        if entry <= float(pull["high"].max()) or entry <= le["ema20"]:
            return None
        extreme = min(float(pull["low"].min()), float(le["low"]))
    else:
        if entry >= float(pull["low"].min()) or entry >= le["ema20"]:
            return None
        extreme = max(float(pull["high"].max()), float(le["high"]))
    if _body_ratio(le) < 0.3 or side.sign * (le["close"] - le["open"]) <= 0:
        return None
    if _spread_ratio(snap, atr) > 0.25:
        return None
    raw_sl = extreme - side.sign * 0.2 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5 * float(p.get("sl_atr", 1.2)), 3.0)
    depth = abs(entry - extreme) / atr
    score = 20.0 + 15.0 + 15.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}", f"repli sur EMA20 M5 ({len(pull)} barre(s)), RSI {closed.iloc[contact]['rsi14']:.0f}",
            "cassure de l'extrême du repli par une clôture"]
    cons = []
    if (side is Side.BUY and le["ema20"] > le["ema50"]) or (side is Side.SELL and le["ema20"] < le["ema50"]):
        score += 10
        pros.append("EMA20/50 M5 alignées")
    if structure_label(closed) == ("HH_HL" if side is Side.BUY else "LH_LL"):
        score += 5
        pros.append("structure M5 alignée")
    if depth <= 1.5:
        score += 10
    else:
        cons.append(f"repli profond ({depth:.1f} ATR)")
    score += _clamp(_body_ratio(le) * 10, 0, 10)
    if len(pull) > 4:
        score -= 5
        cons.append("repli long : élan affaibli")
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M5 au-delà de l'extrême du repli", bt)


# --------------------------------------------------------------------------------------------------------------
# B04 — h4h1_trend_continuation (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("B04")
def strategy_b04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B04 — Continuation H4/H1 : impulsion H4 puis micro-repli H1 englobé.

    Thèse : une bougie H4 d'impulsion (corps >= 0,6 ATR H4) dans le sens de la tendance H4 est rarement suivie d'un
    retournement immédiat ; on attend une barre H1 de repli puis une barre H1 qui l'englobe (clôture au-delà de son
    extrême) pour rejoindre le mouvement.

    Entrée : `_trend_of` H4 UP/DOWN et dernière H4 clôturée impulsive dans ce sens ; sur H1, barre précédente
    contraire (repli) et barre de signal qui clôture au-dessus (sous) du plus haut (bas) de cette barre de repli,
    au-dessus (sous) de son EMA50 H1.
    Confirmation : RSI14 H1 dans [`rsi_lo`, `rsi_hi`] (ni épuisé, ni survendu).
    Filtres : ATR H1 <= 1,8 × sa moyenne 20 barres (pas de choc) ; spread <= 12 % ATR H1.
    SL : sous (au-dessus) du plus bas (haut) des 3 dernières barres H1 clôturées − 0,3 ATR, borné à
    [0,6 ATR ; 3 ATR] puis plafonné à `sl_atr` ATR.
    TP : multiples de R via `rr` (2,5 par défaut : continuation ample attendue).
    Invalidation : clôture H1 au-delà du plus bas (haut) du repli.
    Score : structure 20 + impulsion 0-10 + tendance 15 + MTF 15 (+5 EMA H1 alignées) + volatilité 10 + exécution 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 25 or not _valid(lt, "atr14", "open", "close"):
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    atr_h4 = float(lt["atr14"])
    if atr_h4 <= 0:
        return None
    impulse = side.sign * (lt["close"] - lt["open"]) / atr_h4
    if impulse < 0.6:
        return None
    prev = closed.iloc[-2]
    if side.sign * (prev["close"] - prev["open"]) >= 0:
        return None  # pas de barre de repli
    entry = float(le["close"])
    if side is Side.BUY and not (entry > prev["high"] and entry > le["ema50"]):
        return None
    if side is Side.SELL and not (entry < prev["low"] and entry < le["ema50"]):
        return None
    lo, hi = float(p.get("rsi_lo", 40)), float(p.get("rsi_hi", 65))
    rsi = float(le["rsi14"])
    if side is Side.BUY and not (lo <= rsi <= hi):
        return None
    if side is Side.SELL and not (100 - hi <= rsi <= 100 - lo):
        return None
    atr_ma = closed["atr14"].iloc[-20:].mean()
    if atr_ma > 0 and atr > 1.8 * atr_ma:
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    last3 = closed.iloc[-3:]
    pull_ext = float(last3["low"].min()) if side is Side.BUY else float(last3["high"].max())
    raw_sl = pull_ext - side.sign * 0.3 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.6, min(3.0, float(p.get("sl_atr", 1.5))))
    score = 20.0 + _clamp((impulse - 0.6) * 10, 0, 10) + 15.0 + 15.0 + 10.0
    pros = [f"impulsion H4 {tr} de {impulse:.1f} ATR H4", "barre H1 de repli englobée par la barre de signal",
            f"RSI H1 {rsi:.0f} dans la zone de continuation"]
    cons = []
    if _trend_of(le) == tr:
        score += 5
        pros.append("EMA H1 alignées")
    else:
        cons.append("EMA H1 pas encore alignées")
    cp = _close_position(le) if side is Side.BUY else 1 - _close_position(le)
    score += _clamp(cp * 10, 0, 10)
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "clôture H1 au-delà de l'extrême du repli", bt)


# --------------------------------------------------------------------------------------------------------------
# B05 — trend_pullback_m15 (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("B05")
def strategy_b05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B05 — Rejet par mèche dans la zone EMA20-EMA50 M15, en tendance H1.

    Thèse : en tendance H1, la zone entre l'EMA20 et l'EMA50 M15 attire les acheteurs ; une bougie dont la mèche
    traverse la zone mais qui clôture au-dessus de l'EMA20 signale l'absorption de l'offre.

    Entrée : tendance H1 UP/DOWN ; barre M15 clôturée dont le plus bas (haut) entre dans [EMA50 − 0,3 ATR ;
    EMA20 + 0,2 ATR] et dont la clôture est au-dessus (sous) de l'EMA20 ; RSI14 dans [`rsi_lo`, `rsi_hi`].
    Confirmation : mèche de rejet >= 40 % de l'amplitude et clôture dans le sens du trade ; ADX14 M15 >= 18.
    Filtres : amplitude de la bougie >= 0,6 ATR (bonus) ; espace jusqu'au dernier swing haut >= 1 R (sinon
    pénalité) ; spread <= 10 % ATR.
    SL : sous (au-dessus) de la mèche de rejet − 0,3 ATR, distance bornée à [`sl_atr`·0,5 ATR ; 3 ATR].
    TP : multiples de R via `rr`.
    Invalidation : clôture M15 au-delà de l'extrême de la mèche de rejet.
    Score : structure 20 + tendance 10 (+10 EMA M15 alignées) + MTF 15 + volatilité 0-10 + exécution 0-20 (mèche)
    − 10 si l'espace jusqu'au swing est insuffisant.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 30:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    lo, hi = float(p.get("rsi_lo", 38)), float(p.get("rsi_hi", 62))
    rsi = float(le["rsi14"])
    if not (lo <= rsi <= hi):
        return None
    if le["adx14"] < 18:
        return None
    rng = _range(le)
    if rng <= 0:
        return None
    if side is Side.BUY:
        in_zone = (le["ema50"] - 0.3 * atr) <= le["low"] <= (le["ema20"] + 0.2 * atr) and le["close"] > le["ema20"]
        wick = (min(le["open"], le["close"]) - le["low"]) / rng
        up_bar = le["close"] > le["open"]
    else:
        in_zone = (le["ema20"] - 0.2 * atr) <= le["high"] <= (le["ema50"] + 0.3 * atr) and le["close"] < le["ema20"]
        wick = (le["high"] - max(le["open"], le["close"])) / rng
        up_bar = le["close"] < le["open"]
    if not in_zone or wick < 0.4 or not up_bar:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    entry = float(le["close"])
    raw_sl = float(le["low"]) - 0.3 * atr if side is Side.BUY else float(le["high"]) + 0.3 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5 * float(p.get("sl_atr", 1.3)), 3.0)
    dist = abs(entry - sl)
    score = 20.0 + 10.0 + 15.0 + _clamp(wick * 20, 0, 20)
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}", f"mèche de rejet ({wick:.0%}) dans la zone EMA20-EMA50",
            f"RSI {rsi:.0f} neutre", f"ADX {le['adx14']:.0f}"]
    cons = []
    if _trend_of(le) == tr:
        score += 10
        pros.append("EMA M15 alignées")
    if rng >= 0.6 * atr:
        score += 10
    else:
        cons.append("bougie de rejet étroite (< 0,6 ATR)")
    sh, sl_ = swing_points(closed)
    target = sh[-1][1] if (side is Side.BUY and sh) else sl_[-1][1] if (side is Side.SELL and sl_) else None
    if target is not None and side.sign * (target - entry) < dist:
        score -= 10
        cons.append("swing opposé à moins de 1 R : espace limité")
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la mèche de rejet", bt)


# --------------------------------------------------------------------------------------------------------------
# B06 — ma_structure_trend (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("B06")
def strategy_b06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B06 — Cassure de structure (BOS) H1 confirmée par les EMA et le volume, cible = projection du swing.

    Thèse : en structure HH/HL (LH/LL), la cassure du dernier swing haut (bas) par une clôture, avec volume
    supérieur à la moyenne et EMA alignées, prolonge la tendance d'au moins la hauteur du dernier swing.

    Entrée : `structure_label` H1 = HH_HL (BUY) / LH_LL (SELL) ; clôture au-delà du dernier swing haut (bas) alors
    que la clôture précédente ne l'était pas (cassure fraîche).
    Confirmation : `require_ema` → `_trend_of` H1 aligné ; tick_volume de la barre >= moyenne 20 barres
    (>= 1,2 × moyenne = bonus volatilité/volume).
    Filtres : tendance H4 non opposée ; clôture à plus de 0,5 ATR au-delà du niveau = pénalité (poursuite).
    SL : sous (au-dessus) le dernier swing bas (haut) − 0,2 ATR ; si > 3 ATR → `sl_atr` ATR ; plancher 0,5 ATR.
    TP : cible structurelle = niveau cassé + hauteur du swing (swing haut − swing bas) ; rr = max(`rr`,
    min(cible/R, 4)) ; refus si la cible est à moins de 1,5 R.
    Invalidation : clôture H1 au-delà du swing opposé.
    Score : structure 25 + tendance 15 + MTF 0-15 + volatilité/volume 0-10 + exécution 10 (pas de poursuite).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 30 or "tick_volume" not in closed.columns:
        return None
    label = structure_label(closed)
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    entry = float(le["close"])
    prev_close = float(closed["close"].iloc[-2])
    if label == "HH_HL" and entry > sh[-1][1] >= prev_close:
        side, level, opp = Side.BUY, sh[-1][1], sl_[-1][1]
    elif label == "LH_LL" and entry < sl_[-1][1] <= prev_close:
        side, level, opp = Side.SELL, sl_[-1][1], sh[-1][1]
    else:
        return None
    tr = "UP" if side is Side.BUY else "DOWN"
    if p.get("require_ema", True) and _trend_of(le) != tr:
        return None
    if _trend_of(lt) not in (tr, "FLAT"):
        return None
    vol_ma = float(closed["tick_volume"].iloc[-21:-1].mean())
    if not (vol_ma > 0) or float(le["tick_volume"]) < vol_ma:
        return None
    raw_sl = opp - side.sign * 0.2 * atr
    if abs(entry - raw_sl) > 3 * atr:
        raw_sl = entry - side.sign * float(p.get("sl_atr", 1.5)) * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5, 3.0)
    dist = abs(entry - sl)
    height = abs(level - opp)
    target = level + side.sign * height
    rr_struct = side.sign * (target - entry) / dist
    if rr_struct < 1.5:
        return None
    rr = max(float(p.get("rr", 2.0)), min(rr_struct, 4.0))
    score = 25.0 + 15.0
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"structure {label} + cassure de swing par clôture", f"volume {le['tick_volume'] / vol_ma:.1f}× la moyenne",
             f"cible structurelle à {rr_struct:.1f} R (projection du swing)"]
    cons = []
    if float(le["tick_volume"]) >= 1.2 * vol_ma:
        score += 10
    else:
        cons.append("volume seulement au niveau de la moyenne")
    overshoot = side.sign * (entry - level) / atr
    if overshoot <= 0.5:
        score += 10
    else:
        cons.append(f"clôture {overshoot:.1f} ATR au-delà du niveau : poursuite")
    return _build(spec, snap, side, entry, sl, rr, score, pros, cons, "clôture H1 au-delà du swing opposé", bt)


# --------------------------------------------------------------------------------------------------------------
# B07 — adx_trend_h1 (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("B07")
def strategy_b07(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B07 — Tendance forte ADX/DI sur H1 avec stop « chandelier ».

    Thèse : un ADX élevé et croissant avec un +DI nettement dominant (ou −DI) décrit une tendance directionnelle
    que l'on suit avec un stop suiveur ancré sur l'extrême récent (chandelier), pas sur un swing.

    Entrée : ADX14 H1 >= `adx_min` et supérieur à sa valeur 5 barres plus tôt ; +DI − (−DI) >= 5 pour un BUY
    (inverse pour un SELL) ; clôture au-dessus (sous) de l'EMA20 H1.
    Confirmation : clôture dans les 30 % supérieurs (inférieurs) de l'amplitude de la barre.
    Filtres : tendance H4 alignée obligatoire ; percentile de volatilité (`vol_pct`) hors [20 ; 90] → refus.
    SL : chandelier = plus haut (bas) des 10 dernières barres clôturées − `sl_atr` × ATR, distance bornée à
    [0,5 ATR ; 3 ATR].
    TP : multiples de R via `rr`.
    Invalidation : ADX < 20 ou croisement inverse des DI.
    Score : structure 15 + tendance 20 + ADX 0-15 + DI 0-10 + MTF 15 + volatilité 10 + exécution 5.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 30:
        return None
    adx_min = float(p.get("adx_min", 30))
    adx_old = closed["adx14"].iloc[-6]
    if pd.isna(adx_old) or le["adx14"] < adx_min or le["adx14"] <= adx_old:
        return None
    plus_di, minus_di = _di(closed)
    pdi, mdi = float(plus_di.iloc[-1]), float(minus_di.iloc[-1])
    if not (np.isfinite(pdi) and np.isfinite(mdi)):
        return None
    if pdi - mdi >= 5 and le["close"] > le["ema20"]:
        side = Side.BUY
    elif mdi - pdi >= 5 and le["close"] < le["ema20"]:
        side = Side.SELL
    else:
        return None
    if _trend_of(lt) != ("UP" if side is Side.BUY else "DOWN"):
        return None
    cp = _close_position(le) if side is Side.BUY else 1 - _close_position(le)
    if cp < 0.7:
        return None
    if _valid(le, "vol_pct") and not (20 <= le["vol_pct"] <= 90):
        return None
    entry = float(le["close"])
    last10 = closed.iloc[-10:]
    sl_atr = float(p.get("sl_atr", 2.0))
    raw_sl = float(last10["high"].max()) - sl_atr * atr if side is Side.BUY else float(last10["low"].min()) + sl_atr * atr
    if side.sign * (entry - raw_sl) <= 0:
        return None  # l'extrême est trop loin : le chandelier serait du mauvais côté
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5, 3.0)
    di_spread = abs(pdi - mdi)
    score = 15.0 + 20.0 + _clamp((le["adx14"] - adx_min) * 1.0, 0, 15) + _clamp(di_spread * 0.5, 0, 10) + 15.0 + 5.0
    pros = [f"ADX {le['adx14']:.0f} en hausse", f"+DI {pdi:.0f} / -DI {mdi:.0f}", f"tendance H4 alignée",
            f"clôture dans les {cp:.0%} favorables de la barre"]
    cons = []
    if _valid(le, "vol_pct"):
        score += 10
        pros.append(f"volatilité au {le['vol_pct']:.0f}e percentile")
    else:
        cons.append("percentile de volatilité indisponible")
    if abs(entry - raw_sl) < 0.5 * atr:
        cons.append("stop chandelier ramené au plancher 0,5 ATR")
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "ADX H1 < 20 ou croisement inverse des DI", bt)


# --------------------------------------------------------------------------------------------------------------
# B08 — vol_adjusted_trend (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("B08")
def strategy_b08(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B08 — Tendance H1 ajustée à la volatilité : entrée dans la bande médiane, cible modulée par le percentile ATR.

    Thèse : une tendance se trade mieux quand la volatilité n'est ni morte ni extrême ; on entre entre la médiane
    de Bollinger et la bande externe (jamais au-delà, pas de surextension) et on adapte l'objectif : volatilité
    basse → objectif plus ambitieux, volatilité haute → objectif rapproché.

    Entrée : `_trend_of` H1 UP/DOWN ; ADX14 >= `adx_min` ; position de la clôture entre bb_mid et bb_up (BUY)
    à 10-90 % de la demi-largeur ; bougie dans le sens du trade.
    Confirmation : `vol_pct` (percentile ATR sur 200 barres) dans [15 ; `vol_pct_max`].
    Filtres : tendance H4 non opposée ; spread <= 12 % ATR.
    SL : derrière la médiane de Bollinger − 0,5 ATR (le retour sous la médiane nie la tendance), distance bornée à
    [0,5 ATR ; `sl_atr` ATR].
    TP : rr_eff = `rr` × (1 + (50 − vol_pct)/200), borné à [1,5 ; 3,5].
    Invalidation : clôture H1 au-delà de la médiane de Bollinger.
    Score : structure 15 + tendance 15 + ADX 0-10 + MTF 15 + volatilité 0-20 (décroît avec vol_pct) + exécution 10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    if not _valid(le, "bb_mid", "bb_up", "bb_low", "vol_pct"):
        return None
    tr = _trend_of(le)
    side = _side_from(tr)
    if side is None:
        return None
    adx_min = float(p.get("adx_min", 25))
    if le["adx14"] < adx_min:
        return None
    vol_pct = float(le["vol_pct"])
    if not (15 <= vol_pct <= float(p.get("vol_pct_max", 80))):
        return None
    if _trend_of(lt) not in (tr, "FLAT"):
        return None
    half = float(le["bb_up"] - le["bb_mid"]) if side is Side.BUY else float(le["bb_mid"] - le["bb_low"])
    if half <= 0:
        return None
    pos = side.sign * (le["close"] - le["bb_mid"]) / half
    if not (0.1 <= pos <= 0.9):
        return None
    if side.sign * (le["close"] - le["open"]) <= 0:
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    entry = float(le["close"])
    raw_sl = float(le["bb_mid"]) - side.sign * 0.5 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 2.0)))
    rr_eff = _clamp(float(p.get("rr", 2.0)) * (1 + (50 - vol_pct) / 200), 1.5, 3.5)
    score = 15.0 + 15.0 + _clamp((le["adx14"] - adx_min) * 1.0, 0, 10) + _clamp(20 * (1 - vol_pct / 100), 0, 20) + 10.0
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"EMA alignées {tr} sur H1", f"clôture à {pos:.0%} de la demi-bande (pas de surextension)",
             f"volatilité au {vol_pct:.0f}e percentile → objectif {rr_eff:.2f} R", f"ADX {le['adx14']:.0f}"]
    cons = []
    if vol_pct > 65:
        cons.append("volatilité élevée : objectif rapproché, slippage possible")
    if abs(entry - raw_sl) > float(p.get("sl_atr", 2.0)) * atr:
        cons.append("médiane éloignée : SL plafonné à sl_atr")
    return _build(spec, snap, side, entry, sl, rr_eff, score, pros, cons, "clôture H1 au-delà de la médiane de Bollinger", bt)


# --------------------------------------------------------------------------------------------------------------
# B09 — macd_momentum_trend (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("B09")
def strategy_b09(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B09 — Retournement de l'histogramme MACD au-dessus (sous) de zéro : reprise de momentum en tendance.

    Thèse : en tendance H1, quand la ligne MACD M15 reste au-dessus de zéro, un creux de l'histogramme suivi de
    deux barres de remontée signale la fin du repli de momentum (à la différence du croisement MACD/signal, qui
    arrive plus tard).

    Entrée : tendance H1 UP/DOWN ; ligne MACD > 0 (BUY) / < 0 (SELL) ; histogramme : creux négatif (positif)
    h[-3] < h[-4], puis h[-2] > h[-3] et h[-1] > h[-2] (deux barres de remontée) ; clôture > EMA20 M15.
    Confirmation : la barre de signal clôture dans le sens du trade.
    Filtres : profondeur du repli (clôture − plus bas depuis le creux) <= 2 ATR ; spread <= 10 % ATR.
    SL : sous (au-dessus) le plus bas (haut) des barres depuis le creux de l'histogramme − 0,25 ATR, borné à
    [0,5 ATR ; `sl_atr` ATR].
    TP : multiples de R via `rr`.
    Invalidation : histogramme MACD sous son creux ou ligne MACD sous zéro.
    Score : structure 20 + tendance 15 (+5 MACD du bon côté de zéro depuis 10 barres) + MTF 15 + volatilité 0-10
    + exécution 0-15 (vigueur de la remontée).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 30 or "macd_hist" not in closed.columns:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    h = closed["macd_hist"].iloc[-4:].to_numpy(dtype=float)
    if np.isnan(h).any() or not _valid(le, "macd"):
        return None
    s = side.sign
    trough = s * h[1] < s * h[0] and s * h[1] < 0 and s * h[2] > s * h[1] and s * h[3] > s * h[2]
    if not trough or s * le["macd"] <= 0:
        return None
    if s * (le["close"] - le["ema20"]) <= 0 or s * (le["close"] - le["open"]) <= 0:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    entry = float(le["close"])
    since = closed.iloc[-3:]
    ext = float(since["low"].min()) if side is Side.BUY else float(since["high"].max())
    depth = abs(entry - ext) / atr
    if depth > 2.0:
        return None
    raw_sl = ext - s * 0.25 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 1.5)))
    rise = abs(h[3] - h[1]) / atr
    score = 20.0 + 15.0 + 15.0 + _clamp(rise * 30, 0, 15)
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}", "creux de l'histogramme MACD puis 2 barres de remontée",
            f"ligne MACD {'>' if s > 0 else '<'} 0 (repli de momentum, pas retournement)"]
    cons = []
    macd10 = closed["macd"].iloc[-10:]
    if (s * macd10 > 0).all():
        score += 5
        pros.append("MACD du bon côté de zéro depuis 10 barres")
    if depth <= 1.0:
        score += 10
    else:
        cons.append(f"repli de {depth:.1f} ATR : stop plus large")
    return _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "histogramme MACD sous son creux ou ligne MACD de retour sous zéro", bt)


# --------------------------------------------------------------------------------------------------------------
# B10 — ema_trend_gold (M15 / H1, métaux)
# --------------------------------------------------------------------------------------------------------------
@register("B10")
def strategy_b10(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """B10 — Or : reconquête de l'EMA20 M15 en tendance, pendant les heures liquides, cible = extrême de la veille.

    Thèse : sur l'or, les tendances intrajournalières reprennent souvent après une incursion sous l'EMA20 ; deux
    clôtures consécutives de retour au-dessus, pendant Londres/New York et avec une volatilité suffisante,
    offrent une entrée avec un objectif naturel : le plus haut (bas) de la veille.

    Entrée : symbole de classe `metals` uniquement ; heure UTC de la barre clôturée dans [7 ; 20) ; `_trend_of`
    M15 UP/DOWN et H1 aligné ; au moins une clôture sous (au-dessus de) l'EMA20 dans les 6 barres précédant les
    deux dernières ; les deux dernières clôtures au-dessus (sous) de l'EMA20 avec progression de la clôture.
    Confirmation : ATR14 M15 >= 0,8 × sa moyenne 50 barres (l'or doit bouger) ; corps >= 40 % (bonus).
    Filtres : spread <= 8 % ATR (l'or a un spread élevé) ; RSI > 75 / < 25 = pénalité.
    SL : sous (au-dessus) le plus bas (haut) des 8 dernières barres clôturées − 0,2 ATR, borné à
    [0,6 ATR ; `sl_atr` ATR].
    TP : extrême de la veille (D1) si à >= 1,5 R (rr = min(distance/R, 4)), sinon multiples de R via `rr`.
    Invalidation : clôture M15 au-delà de l'EMA50.
    Score : structure 20 (reconquête) + tendance 15 + MTF 15 + volatilité 10 (+5 si ATR >= moyenne)
    + exécution 0-10 (corps) + 5 si cible journalière − 10 si RSI extrême.
    """
    if snap.spec is None or getattr(snap.spec, "asset_class", "") != "metals":
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60:
        return None
    hour = _bar_hour(le)
    if hour is None or not (7 <= hour < 20):
        return None
    tr = _trend_of(le)
    side = _side_from(tr)
    if side is None or _trend_of(lt) != tr:
        return None
    s = side.sign
    prev = closed.iloc[-2]
    before = closed.iloc[-8:-2]
    if not (s * (before["close"] - before["ema20"]) < 0).any():
        return None  # pas d'incursion à reconquérir
    if s * (prev["close"] - prev["ema20"]) <= 0 or s * (le["close"] - le["ema20"]) <= 0 or s * (le["close"] - prev["close"]) <= 0:
        return None
    atr_ma = closed["atr14"].iloc[-50:].mean()
    if not (atr_ma > 0) or atr < 0.8 * atr_ma:
        return None
    if _spread_ratio(snap, atr) > 0.08:
        return None
    entry = float(le["close"])
    last8 = closed.iloc[-8:]
    raw_sl = float(last8["low"].min()) - 0.2 * atr if side is Side.BUY else float(last8["high"].max()) + 0.2 * atr
    sl = _bound_sl(side, entry, raw_sl, atr, 0.6, float(p.get("sl_atr", 1.8)))
    dist = abs(entry - sl)
    rr = float(p.get("rr", 2.0))
    score = 20.0 + 15.0 + 15.0 + 10.0
    pros = [f"reconquête de l'EMA20 M15 en tendance {tr}", "H1 aligné", f"heure de barre {hour:02d}h UTC (Londres/NY)",
            f"ATR M15 = {atr / atr_ma:.2f}× sa moyenne 50"]
    cons = []
    if atr >= atr_ma:
        score += 5
    d1 = snap.frames.get("D1")
    if d1 is not None and len(d1) >= 3:
        ph, pl = daily_high_low(d1)
        target = ph if side is Side.BUY else pl
        if np.isfinite(target) and s * (target - entry) / dist >= 1.5:
            rr = min(s * (target - entry) / dist, 4.0)
            score += 5
            pros.append(f"cible = extrême de la veille à {rr:.1f} R")
        else:
            cons.append("extrême de la veille trop proche ou dépassé : cible en multiples de R")
    body = _body_ratio(le)
    score += _clamp(body * 10, 0, 10) if s * (le["close"] - le["open"]) > 0 else 0.0
    if (side is Side.BUY and le["rsi14"] > 75) or (side is Side.SELL and le["rsi14"] < 25):
        score -= 10
        cons.append(f"RSI {le['rsi14']:.0f} extrême")
    return _build(spec, snap, side, entry, sl, rr, score, pros, cons, "clôture M15 au-delà de l'EMA50 M15", bt)
