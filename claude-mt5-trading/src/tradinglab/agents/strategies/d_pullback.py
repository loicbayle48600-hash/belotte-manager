"""Famille D — replis dans la tendance : une stratégie propre par agent (D01 … D07).

Thème commun : la tendance du timeframe supérieur est établie, le prix revient vers une zone de valeur (EMA20/50,
retracement Fibonacci, ancien niveau S/R, liquidité sous un swing) et une barre clôturée signale la reprise. Chaque
agent a sa propre définition de la zone de valeur, sa propre confirmation, ses filtres, sa logique de SL, son plan
de TP et sa règle d'invalidation : une simple différence de paramètres ne suffit pas.

Conventions communes (voir `agents/screeners.py`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est la
  barre en formation et n'est JAMAIS lue ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14 du tf
  d'entrée, horodatage de la barre clôturée) ;
- `_build` construit le `TradeCandidate` (pénalité de spread) et refuse tout SL du mauvais côté ; les agents de ce
  module remplacent ensuite le plan de TP générique par leur propre plan (`_set_tp_plan`, `rr >= 1.5` garanti) ;
- le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (ATR H1, stops_level du symbole) : un SL refusé
  donne `None`, jamais une valeur corrigée à la volée ;
- `setup_score` = somme documentée de composantes (structure, tendance, alignement MTF, momentum, volatilité, qualité
  d'exécution), bornée 0-100 : ce n'est PAS une probabilité de gain ;
- données insuffisantes ou indicateur NaN → `None`, jamais une valeur inventée.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import structure_label, support_resistance, swing_points
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# Bornes de distance entrée→SL en ATR du tf d'entrée (le gate impose 0,25-4 ATR H1 ; on reste plus strict)
SL_MIN_ATR = 0.3
SL_MAX_ATR = 3.0
# Pour les tf d'entrée courts (M15), l'ATR du tf d'entrée est bien plus petit que l'ATR H1 : la distance minimale
# tient aussi compte de l'ATR H1 (fraction `SL_MIN_H1_ATR`) afin de ne jamais proposer un stop que le gate refuserait
SL_MIN_H1_ATR = 0.3

# D01 : tolérance (en ATR du tf d'entrée) accordée aux clôtures du repli sous l'EMA50 ; une clôture qui pique
# sous l'EMA50 sans s'y installer reste une respiration, une égalité stricte rendait la condition intenable
HOLD_ATR = 0.6
# D03 : nombre de barres de course exigées avant le premier contact, et nombre de barres tolérées hors norme
RUN_BARS = 6
RUN_TOLERANCE = 1
# D03 : tolérance (en ATR du tf d'entrée) sur le contact de l'EMA par la mèche de la barre de signal
CONTACT_ATR = 0.3

EMA_KEYS = ("ema20", "ema50", "ema200")
NEXT_EMA = {"ema20": "ema50", "ema50": "ema200", "ema200": "ema200"}


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
        ts = pd.Timestamp(row["time"])
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC")
        return int(ts.hour)
    except (TypeError, ValueError, AttributeError, KeyError):
        return None


def _range(row: pd.Series) -> float:
    return float(row["high"] - row["low"])


def _body_ratio(row: pd.Series) -> float:
    """Corps / amplitude de la bougie (0 si amplitude nulle)."""
    rng = _range(row)
    return float(abs(row["close"] - row["open"]) / rng) if rng > 0 else 0.0


def _close_pos(row: pd.Series, side: Side) -> float:
    """Position de la clôture dans l'amplitude, orientée : 1 = clôture à l'extrême favorable au trade."""
    rng = _range(row)
    if rng <= 0:
        return 0.5
    pos = float((row["close"] - row["low"]) / rng)
    return pos if side is Side.BUY else 1.0 - pos


def _wick_against(row: pd.Series, side: Side) -> float:
    """Mèche opposée au trade (mèche basse pour un BUY) rapportée à l'amplitude : mesure du rejet."""
    rng = _range(row)
    if rng <= 0:
        return 0.0
    if side is Side.BUY:
        return float((min(row["open"], row["close"]) - row["low"]) / rng)
    return float((row["high"] - max(row["open"], row["close"])) / rng)


def _spread_ratio(snap, atr: float) -> float:
    """Spread courant exprimé en fraction de l'ATR du tf d'entrée."""
    if atr <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr)


def _spread_ratio_h1(snap) -> float:
    """Spread courant en fraction de l'ATR H1 (référence du gate : <= 0,15 ATR H1) ; inf si l'ATR H1 est inconnu."""
    atr_h1 = float(snap.atr_h1 or 0.0)
    if atr_h1 <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr_h1)


def _bound_sl(snap, side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> Optional[float]:
    """Ramène la distance entrée→SL dans [max(lo·ATR, 0,3·ATR H1) ; hi·ATR] sans jamais changer de côté.

    Renvoie None si la borne basse dépasse la borne haute (configuration incohérente : on refuse plutôt que d'inventer).
    """
    if not np.isfinite(sl) or atr <= 0:
        return None
    atr_h1 = float(snap.atr_h1 or 0.0)
    lo_dist = max(lo * atr, SL_MIN_ATR * atr, SL_MIN_H1_ATR * atr_h1)
    hi_dist = min(hi, SL_MAX_ATR) * atr
    if lo_dist > hi_dist:
        return None
    dist = min(max(abs(entry - sl), lo_dist), hi_dist)
    return entry - side.sign * dist


def _set_tp_plan(c: Optional[TradeCandidate], side: Side, entry: float, targets: list[float], rr_min: float,
                 first_r: float = 1.5) -> Optional[TradeCandidate]:
    """Remplace le plan de TP générique de `_build` par un plan propre à l'agent.

    - `targets` : cibles structurelles (niveaux, projections) ; seules celles situées à >= 0,5 R dans le sens du
      trade sont retenues ;
    - premier TP à `first_r` R (prise partielle), cible finale = max(`rr_min` R, cible structurelle la plus
      lointaine) → `rr >= max(1.5, rr_min)` garanti ;
    - plan limité à 3 niveaux croissants dans le sens du trade.
    """
    if c is None:
        return None
    dist = abs(entry - c.sl)
    if dist <= 0:
        return None
    rr_min = max(1.5, float(rr_min))
    sign = side.sign
    pts = [float(x) for x in targets if x is not None and np.isfinite(x) and sign * (x - entry) >= 0.5 * dist]
    final = entry + sign * dist * rr_min
    if pts:
        far = max(pts, key=lambda x: sign * (x - entry))
        if sign * (far - entry) > sign * (final - entry):
            final = far
    # arrondi AVANT la comparaison : arrondir après ferait sortir la cible finale de sa propre borne
    final = round(final, 10)
    first = round(entry + sign * dist * first_r, 10)
    plan = sorted({first, *[round(x, 10) for x in pts], final}, key=lambda x: sign * (x - entry))
    plan = [x for x in plan if sign * (x - entry) <= sign * (final - entry) + 1e-9]
    if len(plan) > 3:
        plan = [plan[0], plan[len(plan) // 2], plan[-1]]
    c.tp_plan = [float(x) for x in plan]
    c.rr = round(abs(c.tp_plan[-1] - entry) / dist, 2)
    return c


def _finalize(c: Optional[TradeCandidate], snap) -> Optional[TradeCandidate]:
    """Revalidation déterministe du SL (côté, stops_level, 0,25-4 ATR H1) : refus → None, jamais de correction."""
    if c is None:
        return None
    chk = validate_stop_loss(c.side, c.entry, c.sl, snap.spec, atr=float(c.atr or 0.0))
    return c if chk.ok else None


def _last_impulse(closed: pd.DataFrame, side: Side) -> Optional[tuple[int, float, int, float]]:
    """Dernière impulsion confirmée dans le sens du trade : (idx_origine, prix_origine, idx_fin, prix_fin).

    BUY : dernier swing bas confirmé puis le swing haut le plus élevé confirmé après lui ; SELL symétrique.
    Les indices sont positionnels dans `closed`. None si aucune impulsion complète.
    """
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    if side is Side.BUY:
        lo_i, lo = sl_[-1]
        later = [x for x in sh if x[0] > lo_i]
        if not later:
            return None
        hi_i, hi = max(later, key=lambda x: x[1])
        return lo_i, float(lo), hi_i, float(hi)
    hi_i, hi = sh[-1]
    later = [x for x in sl_ if x[0] > hi_i]
    if not later:
        return None
    lo_i, lo = min(later, key=lambda x: x[1])
    return hi_i, float(hi), lo_i, float(lo)


def _rsi_band(p: dict, side: Side, lo_default: float, hi_default: float) -> tuple[float, float]:
    """Bande RSI acceptée pour un repli : [rsi_lo, rsi_hi] pour un BUY, miroir [100-rsi_hi, 100-rsi_lo] pour un SELL."""
    lo, hi = float(p.get("rsi_lo", lo_default)), float(p.get("rsi_hi", hi_default))
    return (lo, hi) if side is Side.BUY else (100.0 - hi, 100.0 - lo)


def _ema_key(p: dict, default: str) -> str:
    k = str(p.get("ema", default))
    return k if k in EMA_KEYS else default


# --------------------------------------------------------------------------------------------------------------
# D01 — h1_trend_m15_pullback (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("D01")
def strategy_d01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D01 — Séquence de repli M15 (2 à 6 barres contraires) vers l'EMA20, reprise par cassure du haut de la barre précédente.

    Thèse : en tendance H1, un repli ordonné de quelques bougies contraires consécutives qui vient chercher l'EMA20
    M15 sans s'installer au-delà de l'EMA50 est une simple respiration ; la reprise est actée quand une bougie
    clôture dans le sens de la tendance AU-DELÀ du plus haut (bas) de la bougie précédente, avec un RSI qui repart.

    Entrée : `_trend_of` H1 UP/DOWN et ADX14 H1 >= `adx_min` (défaut 18) ; sur M15, 2 à 6 barres clôturées de
    repli consécutives juste avant la barre de signal (barre de repli pour un BUY : clôture < ouverture, OU clôture
    < clôture précédente, OU plus haut < plus haut précédent — une bougie qui clôture en hausse mais fait un plus
    haut plus bas fait partie du repli) ; au moins une d'elles entre dans la zone de valeur EMA20
    (low <= EMA20 + 0,5 ATR pour un BUY) et aucune ne clôture au-delà de l'EMA50 − 0,6 ATR (tolérance en ATR : un
    repli qui pique sous l'EMA50 sans s'y installer reste une respiration) ; barre de signal dans le sens du trade
    dont la clôture dépasse le plus haut (bas) de la barre précédente.
    Confirmation : RSI14 M15 dans [`rsi_lo`, `rsi_hi`] (miroir pour un SELL) ET RSI en hausse (baisse) par rapport
    à la barre précédente.
    Filtres : heure de la barre hors 21h-23h UTC (rollover, liquidité faible) ; spread <= 12 % de l'ATR H1 ;
    repli de plus de 1,2 ATR = pénalité.
    SL : sous (au-dessus) l'extrême de la séquence de repli − 0,25 ATR, distance bornée à [0,4 ATR ; min(3, 2·`sl_atr`) ATR].
    TP : multiples de R (premier TP à 1 R, prise partielle rapide) ; cible finale = max(`rr` R, dernier swing haut
    (bas) M15 confirmé avant le repli).
    Invalidation : clôture M15 au-delà de l'extrême de la séquence de repli.
    Score : structure 20 + tendance H1 15 + ADX H1 0-10 + MTF 10 (EMA20/50 M15 alignées) + momentum RSI 0-10
    + volatilité 10 (repli <= 1,2 ATR) + exécution 0-10 (corps de la barre de signal).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 40 or not _valid(lt, "adx14"):
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None or lt["adx14"] < float(p.get("adx_min", 18)):
        return None
    sgn = side.sign
    # séquence de barres de repli consécutives se terminant juste avant la barre de signal (indice n-1).
    # Barre de repli = corps contraire à la tendance, OU clôture qui recule, OU extrême qui recule (plus haut plus
    # bas pour un BUY). Le troisième critère est indispensable : sur des barres agrégées, l'ouverture d'une barre
    # vaut la clôture de la précédente, si bien que les deux premiers critères sont redondants — sans lui, seules
    # les séquences de clôtures strictement décroissantes étaient vues et le repli ordonné n'était jamais détecté.
    k = 0
    j = n - 2
    while j >= 1 and k < 7:
        r, r0 = closed.iloc[j], closed.iloc[j - 1]
        ext = (r["high"] - r0["high"]) if side is Side.BUY else (r["low"] - r0["low"])
        if (sgn * (r["close"] - r["open"]) < 0 or sgn * (r["close"] - r0["close"]) < 0
                or sgn * ext < 0):
            k += 1
            j -= 1
        else:
            break
    if k < 2 or k > 6:
        return None
    pull = closed.iloc[n - 1 - k:n - 1]
    if pull[["ema20", "ema50", "rsi14"]].isna().any().any():
        return None
    if side is Side.BUY:
        touched = bool((pull["low"] <= pull["ema20"] + 0.5 * atr).any())
        held = bool((pull["close"] >= pull["ema50"] - HOLD_ATR * atr).all())
        extreme = float(pull["low"].min())
    else:
        touched = bool((pull["high"] >= pull["ema20"] - 0.5 * atr).any())
        held = bool((pull["close"] <= pull["ema50"] + HOLD_ATR * atr).all())
        extreme = float(pull["high"].max())
    if not touched or not held:
        return None
    prev = closed.iloc[-2]
    entry = float(le["close"])
    if sgn * (le["close"] - le["open"]) <= 0:
        return None
    if (side is Side.BUY and entry <= float(prev["high"])) or (side is Side.SELL and entry >= float(prev["low"])):
        return None
    lo, hi = _rsi_band(p, side, 35, 60)
    if not _valid(prev, "rsi14") or not (lo <= le["rsi14"] <= hi) or sgn * (le["rsi14"] - prev["rsi14"]) <= 0:
        return None
    hour = _bar_hour(le)
    if hour is None or hour >= 21:
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    sl_atr = float(p.get("sl_atr", 1.2))
    sl = _bound_sl(snap, side, entry, extreme - sgn * 0.25 * atr, atr, 0.4, min(3.0, 2.0 * sl_atr))
    if sl is None:
        return None
    depth = abs(entry - extreme) / atr
    score = 20.0 + 15.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']} (ADX {lt['adx14']:.0f})",
            f"repli ordonné de {k} barre(s) sur l'EMA20 M15, EMA50 tenue",
            "reprise : clôture au-delà de l'extrême de la barre précédente", f"RSI {le['rsi14']:.0f} qui repart"]
    cons: list[str] = []
    score += _clamp((float(lt["adx14"]) - 20.0) * 0.5, 0, 10)
    if sgn * (le["ema20"] - le["ema50"]) > 0:
        score += 10
        pros.append("EMA20/50 M15 alignées")
    else:
        cons.append("EMA20/50 M15 non alignées")
    score += _clamp(sgn * (float(le["rsi14"]) - float(prev["rsi14"])) * 2.0, 0, 10)
    if depth <= 1.2:
        score += 10
    else:
        cons.append(f"repli profond ({depth:.1f} ATR)")
    score += _clamp(_body_ratio(le) * 10, 0, 10)
    sh, sl_ = swing_points(closed.iloc[:n - 1 - k])
    targets = [sh[-1][1]] if (side is Side.BUY and sh) else [sl_[-1][1]] if (side is Side.SELL and sl_) else []
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la séquence de repli", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0)), first_r=1.0), snap)


# --------------------------------------------------------------------------------------------------------------
# D02 — h4_trend_h1_pullback (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("D02")
def strategy_d02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D02 — Retracement 30-70 % de la dernière impulsion H1 en tendance H4, reprise de l'EMA20 H1 avec MACD qui repart.

    Thèse : en tendance H4, une impulsion H1 (swing bas → swing haut confirmés) suivie d'un repli qui en efface
    entre 30 et 70 % garde son élan ; on attend que le prix, passé sous l'EMA20 H1 pendant le repli, la reprenne
    par une clôture, avec un histogramme MACD qui se redresse (momentum). Cible = mouvement mesuré.

    Entrée : `_trend_of` H4 UP/DOWN, ADX14 H4 >= `adx_min` (défaut 20), RSI H4 non étiré (< 75 pour un BUY) ;
    dernière impulsion H1 confirmée (`_last_impulse`) de hauteur >= 1,5 ATR ; profondeur du repli dans [0,3 ; 0,7]
    de l'impulsion ; au moins une des 3 barres clôturées précédentes a clôturé sous (au-dessus de) l'EMA20 H1 et
    la barre de signal clôture au-dessus (sous) avec une bougie dans le sens du trade.
    Confirmation : histogramme MACD H1 en hausse (baisse) par rapport à la barre précédente.
    Filtres : ATR H1 <= 2 × sa moyenne 20 barres (pas de choc) ; spread <= 10 % de l'ATR H1.
    SL : sous (au-dessus) l'extrême du repli − 0,3 ATR, distance bornée à [0,5 ATR ; min(3, `sl_atr` + 1) ATR].
    TP : mouvement mesuré — cibles = fin de l'impulsion (retour au swing) puis extrême du repli + hauteur de
    l'impulsion ; cible finale = max(`rr` R, mouvement mesuré) ; premier TP à 1,5 R.
    Invalidation : clôture H1 au-delà de l'extrême du repli (retracement > 70 % = impulsion niée).
    Score : structure 20 + tendance H4 15 + ADX H4 0-10 + MTF 10 (EMA20/50 H1 alignées) + momentum MACD 10
    + volatilité 0-10 (ATR stable) + exécution 0-10 (clôture près de l'extrême favorable).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or not _valid(lt, "adx14", "rsi14") or not _valid(le, "macd_hist"):
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None or lt["adx14"] < float(p.get("adx_min", 20)):
        return None
    sgn = side.sign
    if (side is Side.BUY and lt["rsi14"] > 75) or (side is Side.SELL and lt["rsi14"] < 25):
        return None
    imp = _last_impulse(closed, side)
    if imp is None:
        return None
    o_i, o_px, f_i, f_px = imp
    height = abs(f_px - o_px)
    if height < 1.5 * atr:
        return None
    post = closed.iloc[f_i + 1:]
    if len(post) < 1:
        return None
    extreme = float(post["low"].min()) if side is Side.BUY else float(post["high"].max())
    depth = sgn * (f_px - extreme) / height
    if not (0.3 <= depth <= 0.7):
        return None
    prev3 = closed.iloc[n - 4:n - 1]
    if prev3[["close", "ema20"]].isna().any().any():
        return None
    was_below = bool((sgn * (prev3["close"] - prev3["ema20"]) < 0).any())
    entry = float(le["close"])
    if not was_below or sgn * (entry - le["ema20"]) <= 0 or sgn * (le["close"] - le["open"]) <= 0:
        return None
    prev = closed.iloc[-2]
    if not _valid(prev, "macd_hist") or sgn * (le["macd_hist"] - prev["macd_hist"]) <= 0:
        return None
    atr_ma = float(closed["atr14"].iloc[-21:-1].mean())
    if not np.isfinite(atr_ma) or atr_ma <= 0 or atr > 2.0 * atr_ma:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    sl_atr = float(p.get("sl_atr", 1.5))
    sl = _bound_sl(snap, side, entry, extreme - sgn * 0.3 * atr, atr, 0.5, min(3.0, sl_atr + 1.0))
    if sl is None:
        return None
    score = 20.0 + 15.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']} (ADX {lt['adx14']:.0f})",
            f"repli de {depth * 100:.0f} % d'une impulsion de {height / atr:.1f} ATR",
            "reprise de l'EMA20 H1 par une clôture", "histogramme MACD qui se redresse"]
    cons: list[str] = []
    score += _clamp((float(lt["adx14"]) - 20.0) * 0.5, 0, 10)
    if sgn * (le["ema20"] - le["ema50"]) > 0:
        score += 10
        pros.append("EMA20/50 H1 alignées")
    else:
        cons.append("EMA20/50 H1 non alignées : repli avancé")
    score += 10
    ratio = atr / atr_ma
    if ratio <= 1.3:
        score += 10
    else:
        score += 5
        cons.append(f"ATR H1 en expansion (x{ratio:.2f})")
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    targets = [f_px, extreme + sgn * height]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "clôture H1 au-delà de l'extrême du repli (retracement > 70 %)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.5)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# D03 — ema20_pullback (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("D03")
def strategy_d03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D03 — Premier contact de l'EMA (`ema`, défaut EMA20) M15 après une course sans contact, avec volume.

    Thèse : quand le prix a couru au-dessus de l'EMA20 pendant 6 bougies sans la toucher, le PREMIER retour sur
    l'EMA est le repli le plus souvent acheté (les retardataires attendent ce niveau) ; on exige que la bougie de
    contact clôture dans la moitié favorable de son amplitude et avec un volume supérieur à la médiane récente
    (participation).

    Entrée : `_trend_of` H1 UP/DOWN ; sur les 6 barres M15 clôturées précédant la barre de signal, au moins 5 ont
    clôturé au-dessus (sous) de l'EMA ET au moins 5 ont gardé leur low (high) au-delà de l'EMA (aucun contact) —
    une seule barre hors norme est tolérée, une course parfaite sur 6 barres étant une exigence de laboratoire ;
    la barre de signal touche l'EMA (low <= EMA + 0,3 ATR et clôture >= EMA pour un BUY : le contact se mesure avec
    une tolérance en ATR, pas au tick près).
    Confirmation : clôture dans la moitié favorable de l'amplitude (`_close_pos` >= 0,5) ; volume de la barre
    >= médiane des 20 barres précédentes (sinon pénalité).
    Filtres : pente de l'EMA favorable (EMA > EMA 3 barres avant pour un BUY) ; RSI14 M15 < 70 (BUY) / > 30 (SELL) ;
    spread <= 15 % de l'ATR H1 ; amplitude de la barre <= 2 ATR (pas de bougie de choc).
    SL : sous (au-dessus) du plus éloigné entre le low (high) de la barre de contact et l'EMA suivante (EMA50 pour
    l'EMA20) − 0,15 ATR, distance bornée à [0,4 ATR ; min(3, 2·`sl_atr`) ATR].
    TP : bande de Bollinger opposée du tf d'entrée (bb_up pour un BUY) comme cible structurelle ; cible finale =
    max(`rr` R, bande) ; premier TP à 1,5 R.
    Invalidation : clôture M15 au-delà de l'EMA suivante (EMA50).
    Score : structure 20 (course + premier contact, −5 si une barre de course est hors norme) + tendance H1 15
    + pente EMA 10 + MTF 10 (EMA20/50 M15 alignées) + volume 10 + volatilité 10 (amplitude <= 1,5 ATR)
    + exécution 0-10 (position de la clôture). Somme bornée 0-100 par `_build` : ce n'est PAS une probabilité.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 40:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    sgn = side.sign
    key = _ema_key(p, "ema20")
    nxt = NEXT_EMA[key]
    if not _valid(le, key, nxt, "bb_up", "bb_low"):
        return None
    prior = closed.iloc[n - 1 - RUN_BARS:n - 1]
    if len(prior) < RUN_BARS or prior[[key, "close", "low", "high", "tick_volume"]].isna().any().any():
        return None
    need = RUN_BARS - RUN_TOLERANCE
    # « aucun contact » se mesure du bon côté de l'EMA : le low (high) doit rester AU-DELÀ de l'EMA. La version
    # précédente tolérait EMA − 0,1 ATR pour un BUY, c'est-à-dire des barres déjà passées sous l'EMA : ce qui était
    # compté comme une course sans contact en contenait souvent, et le « premier contact » n'en était pas un.
    if side is Side.BUY:
        above = int((prior["close"] > prior[key]).sum())
        clear = int((prior["low"] > prior[key]).sum())
        contact = bool(le["low"] <= le[key] + CONTACT_ATR * atr and le["close"] >= le[key])
    else:
        above = int((prior["close"] < prior[key]).sum())
        clear = int((prior["high"] < prior[key]).sum())
        contact = bool(le["high"] >= le[key] - CONTACT_ATR * atr and le["close"] <= le[key])
    ran = above >= need and clear >= need
    if not ran or not contact:
        return None
    perfect = above == RUN_BARS and clear == RUN_BARS
    if _close_pos(le, side) < 0.5:
        return None
    ema_prev = closed[key].iloc[-4]
    if pd.isna(ema_prev) or sgn * (le[key] - ema_prev) <= 0:
        return None
    if (side is Side.BUY and le["rsi14"] >= 70) or (side is Side.SELL and le["rsi14"] <= 30):
        return None
    if _spread_ratio_h1(snap) > 0.15 or _range(le) > 2.0 * atr:
        return None
    entry = float(le["close"])
    if side is Side.BUY:
        raw_sl = min(float(le["low"]), float(le[nxt])) - 0.15 * atr
    else:
        raw_sl = max(float(le["high"]), float(le[nxt])) + 0.15 * atr
    sl_atr = float(p.get("sl_atr", 1.0))
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.4, min(3.0, 2.0 * sl_atr))
    if sl is None:
        return None
    vol_med = float(closed["tick_volume"].iloc[n - 21:n - 1].median())
    score = 20.0 + 15.0 + 10.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}",
            f"course de {RUN_BARS} barres sans contact puis premier contact de l'{key.upper()}",
            f"clôture dans la moitié favorable ({_close_pos(le, side) * 100:.0f} %)", f"pente {key.upper()} favorable"]
    cons: list[str] = []
    if not perfect:
        score -= 5
        cons.append("une barre de la course a déjà effleuré l'EMA : contact moins « premier »")
    if sgn * (le["ema20"] - le["ema50"]) > 0:
        score += 10
        pros.append("EMA20/50 M15 alignées")
    else:
        cons.append("EMA20/50 M15 non alignées")
    if np.isfinite(vol_med) and float(le["tick_volume"]) >= vol_med:
        score += 10
        pros.append("volume >= médiane 20 barres")
    else:
        cons.append("volume sous la médiane : participation faible")
    if _range(le) <= 1.5 * atr:
        score += 10
    else:
        cons.append("bougie de contact ample")
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    band = float(le["bb_up"]) if side is Side.BUY else float(le["bb_low"])
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  f"clôture M15 au-delà de l'{nxt.upper()}", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [band], float(p.get("rr", 2.0)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# D04 — ema50_pullback (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("D04")
def strategy_d04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D04 — Double test de l'EMA (`ema`, défaut EMA50) H1 tenu, puis cassure du haut des barres de test.

    Thèse : en tendance H4, deux bougies H1 consécutives dont les mèches viennent tester l'EMA50 sans clôturer
    sous elle montrent une défense du support dynamique ; on n'achète pas la défense mais sa validation, quand une
    bougie clôture au-dessus du plus haut des deux barres de test, avec un RSI qui remonte depuis la zone < 50.

    Entrée : `_trend_of` H4 UP/DOWN ET clôture H4 au-dessus (sous) de son EMA20 (le H4 n'est pas lui-même en repli
    profond) ; sur H1, parmi les 4 barres clôturées précédant la barre de signal, au moins 2 barres de test dont le
    low (high) entre dans la zone EMA + 0,4 ATR (− 0,4 ATR) et toutes clôturent au-dessus (sous) de l'EMA − 0,15 ATR
    (+ 0,15 ATR) ; barre de signal dans le sens du trade qui clôture au-delà du plus haut (bas) des barres de test.
    Confirmation : RSI14 H1 minimal des barres de test < 50 (> 50 pour un SELL) et RSI de signal > RSI précédent.
    Filtres : ADX14 H1 >= `adx_min` (défaut 18) ; écart EMA20−EMA50 H1 > 0,2 ATR dans le bon sens (tendance non
    plate) ; spread <= 10 % de l'ATR H1.
    SL : sous (au-dessus) du plus bas (haut) des barres de test − 0,3 ATR, distance bornée à
    [0,5 ATR ; min(3, 2·`sl_atr`) ATR].
    TP : dernier swing haut (bas) H1 confirmé avant le test, puis son extension 0,618 de la jambe (swing − plus bas
    des tests) ; cible finale = max(`rr` R, extension) ; premier TP à 1,5 R.
    Invalidation : clôture H1 au-delà du plus bas (haut) des barres de test.
    Score : structure 20 (double test tenu + cassure) + tendance H4 15 + H4 au-dessus EMA20 5 + écart EMA 0-10
    + ADX H1 0-10 + momentum RSI 10 + volatilité 10 (barre de signal <= 1,5 ATR) + exécution 0-10 (corps).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 40:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    sgn = side.sign
    if sgn * (lt["close"] - lt["ema20"]) <= 0:
        return None
    key = _ema_key(p, "ema50")
    window = closed.iloc[n - 5:n - 1]
    if len(window) < 4 or window[[key, "close", "low", "high", "rsi14"]].isna().any().any():
        return None
    if side is Side.BUY:
        touch = window["low"] <= window[key] + 0.4 * atr
        held = bool((window["close"] >= window[key] - 0.15 * atr).all())
    else:
        touch = window["high"] >= window[key] - 0.4 * atr
        held = bool((window["close"] <= window[key] + 0.15 * atr).all())
    test = window[touch]
    if len(test) < 2 or not held:
        return None
    if side is Side.BUY:
        test_extreme = float(test["low"].min())
        breakout = float(le["close"]) > float(test["high"].max())
        rsi_turn = float(test["rsi14"].min()) < 50
    else:
        test_extreme = float(test["high"].max())
        breakout = float(le["close"]) < float(test["low"].min())
        rsi_turn = float(test["rsi14"].max()) > 50
    if not breakout or sgn * (le["close"] - le["open"]) <= 0:
        return None
    prev = closed.iloc[-2]
    if not rsi_turn or sgn * (le["rsi14"] - prev["rsi14"]) <= 0:
        return None
    if le["adx14"] < float(p.get("adx_min", 18)):
        return None
    gap = sgn * (le["ema20"] - le["ema50"]) / atr
    if gap <= 0.2:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    entry = float(le["close"])
    sl_atr = float(p.get("sl_atr", 1.2))
    sl = _bound_sl(snap, side, entry, test_extreme - sgn * 0.3 * atr, atr, 0.5, min(3.0, 2.0 * sl_atr))
    if sl is None:
        return None
    score = 20.0 + 15.0 + 5.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}, H4 au-dessus de son EMA20" if side is Side.BUY else
            f"tendance {tr} sur {spec.timeframes['trend']}, H4 sous son EMA20",
            f"double test de l'{key.upper()} H1 tenu puis cassure du haut des tests" if side is Side.BUY else
            f"double test de l'{key.upper()} H1 tenu puis cassure du bas des tests",
            f"RSI H1 repart ({prev['rsi14']:.0f} → {le['rsi14']:.0f})"]
    cons: list[str] = []
    score += _clamp(gap * 10, 0, 10)
    score += _clamp((float(le["adx14"]) - 18.0) * 0.5, 0, 10)
    score += 10
    if _range(le) <= 1.5 * atr:
        score += 10
    else:
        cons.append("barre de signal ample : entrée tardive possible")
    score += _clamp(_body_ratio(le) * 10, 0, 10)
    sh, sl_ = swing_points(closed.iloc[:n - 5])
    targets: list[float] = []
    if side is Side.BUY and sh:
        swing = sh[-1][1]
        targets = [swing, swing + 0.618 * (swing - test_extreme)]
    elif side is Side.SELL and sl_:
        swing = sl_[-1][1]
        targets = [swing, swing - 0.618 * (test_extreme - swing)]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  f"clôture H1 au-delà de l'extrême des barres de test de l'{key.upper()}", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# D05 — fibonacci_confluence (H1 / H4)
# --------------------------------------------------------------------------------------------------------------
@register("D05")
def strategy_d05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D05 — Retracement Fibonacci (`levels`, défaut 50 / 61,8 %) de la dernière impulsion H1, confluence EMA, extensions en cible.

    Thèse : en tendance H4, le creux d'un repli qui s'arrête sur un niveau de Fibonacci de la dernière impulsion H1
    (swing → swing confirmés) est une zone de valeur objective ; la confluence avec l'EMA50 H1 ou l'EMA20 H4 la
    renforce ; le niveau 78,6 % sert de stop logique (au-delà, la thèse de retracement est fausse), les cibles sont
    le retour au swing (100 %) puis l'extension 161,8 %.

    Entrée : `_trend_of` H4 UP/DOWN ; impulsion H1 confirmée (`_last_impulse`) de hauteur entre 2 et 12 ATR ;
    l'extrême du repli (plus bas depuis le swing haut, barres clôturées) est à moins de `tol_atr` ATR d'un niveau
    `levels` et le retracement n'a pas dépassé 78,6 % ; l'extrême date d'au plus 3 barres ; barre de signal dans
    le sens du trade dont la clôture est au-dessus (sous) du niveau touché.
    Confirmation : RSI14 H1 à l'extrême du repli >= 35 (<= 65 pour un SELL : pas de rupture de momentum) ;
    amplitude de la barre de signal >= 0,5 ATR.
    Filtres : spread <= 12 % de l'ATR H1 ; confluence EMA (EMA50 H1 ou EMA20 H4 à moins de `tol_atr` ATR du
    niveau) = bonus, absence = argument contre.
    SL : niveau 78,6 % de l'impulsion − 0,1 ATR (stop de niveau, pas de swing), distance bornée à [0,5 ATR ; 3 ATR] ;
    si ce niveau est à plus de 3 ATR, repli sur l'extrême du repli − 0,2 ATR (même bornes).
    TP : retour au swing de fin d'impulsion (100 %) puis extension 161,8 % (swing + 0,618 × hauteur) ; cible
    finale = max(`rr` R, extension) ; premier TP à 1,5 R.
    Invalidation : clôture H1 au-delà du niveau 78,6 % de l'impulsion.
    Score : structure 20 (impulsion + retracement sur niveau) + tendance H4 15 + confluence 0-15 + MTF 10
    (EMA20/50 H1 alignées) + momentum RSI 0-10 + volatilité 10 (impulsion 2-6 ATR) + exécution 0-10 (amplitude).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60:
        return None
    tr = _trend_of(lt)
    side = _side_from(tr)
    if side is None:
        return None
    sgn = side.sign
    levels = [float(x) for x in p.get("levels", [0.5, 0.618]) if 0 < float(x) < 1]
    if not levels:
        return None
    tol = float(p.get("tol_atr", 0.3)) * atr
    imp = _last_impulse(closed, side)
    if imp is None:
        return None
    o_i, o_px, f_i, f_px = imp
    height = abs(f_px - o_px)
    if not (2.0 * atr <= height <= 12.0 * atr):
        return None
    post = closed.iloc[f_i + 1:]
    if len(post) < 1:
        return None
    lows = post["low"].to_numpy(dtype=float)
    highs = post["high"].to_numpy(dtype=float)
    if side is Side.BUY:
        k = int(np.argmin(lows))
        extreme = float(lows[k])
    else:
        k = int(np.argmax(highs))
        extreme = float(highs[k])
    ext_pos = f_i + 1 + k
    if (n - 1) - ext_pos > 3:
        return None
    depth = sgn * (f_px - extreme) / height
    if depth > 0.786 or depth <= 0:
        return None
    fibs = {lv: f_px - sgn * height * lv for lv in levels}
    near = [lv for lv, px in fibs.items() if abs(extreme - px) <= tol]
    if not near:
        return None
    lv = near[0]
    level_px = fibs[lv]
    entry = float(le["close"])
    if sgn * (le["close"] - le["open"]) <= 0 or sgn * (entry - level_px) <= 0:
        return None
    ext_row = closed.iloc[ext_pos]
    if not _valid(ext_row, "rsi14"):
        return None
    if (side is Side.BUY and ext_row["rsi14"] < 35) or (side is Side.SELL and ext_row["rsi14"] > 65):
        return None
    if _range(le) < 0.5 * atr:
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    lvl_786 = f_px - sgn * height * 0.786
    raw_sl = lvl_786 - sgn * 0.1 * atr
    if abs(entry - raw_sl) > 3.0 * atr:
        raw_sl = extreme - sgn * 0.2 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, 3.0)
    if sl is None:
        return None
    score = 20.0 + 15.0
    pros = [f"tendance {tr} sur {spec.timeframes['trend']}",
            f"retracement {depth * 100:.0f} % d'une impulsion H1 de {height / atr:.1f} ATR, arrêt sur le niveau {lv * 100:.1f} %",
            "reprise : clôture au-delà du niveau"]
    cons: list[str] = []
    conf = []
    if _valid(le, "ema50") and abs(float(le["ema50"]) - level_px) <= tol:
        conf.append("EMA50 H1")
    if _valid(lt, "ema20") and abs(float(lt["ema20"]) - level_px) <= tol:
        conf.append("EMA20 H4")
    if conf:
        score += 15 if len(conf) > 1 else 10
        pros.append("confluence " + " + ".join(conf))
    else:
        cons.append("pas de confluence EMA sur le niveau")
    if sgn * (le["ema20"] - le["ema50"]) > 0:
        score += 10
        pros.append("EMA20/50 H1 alignées")
    else:
        cons.append("EMA20/50 H1 non alignées")
    score += _clamp((sgn * (float(ext_row["rsi14"]) - 50.0) + 15.0) * (10.0 / 30.0), 0, 10)
    if height <= 6.0 * atr:
        score += 10
    else:
        cons.append("impulsion très ample : repli potentiellement plus long")
    score += _clamp(_range(le) / atr * 5, 0, 10)
    targets = [f_px, f_px + sgn * 0.618 * height]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "clôture H1 au-delà du niveau 78,6 % de l'impulsion", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.5)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# D06 — sr_pullback (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("D06")
def strategy_d06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D06 — Retest d'un ancien niveau de résistance devenu support (bascule S/R) dans la tendance H1, en session active.

    Thèse : un niveau S/R ancien (regroupement de swings, `support_resistance`) qui a été cassé récemment change de
    rôle ; le premier retest du niveau depuis l'autre côté, rejeté par une mèche avec participation, offre une
    entrée dans le sens de la tendance avec un stop derrière un niveau objectif et une cible = niveau suivant.

    Entrée : niveaux calculés sur les barres clôturées hors les 12 dernières (le niveau pré-date la cassure) ; si
    `with_trend` (défaut vrai), sens = `_trend_of` H1 (FLAT → rien), sinon les deux sens sont examinés (BUY d'abord)
    avec pénalité contre-tendance ; le low (high) de la barre de signal est à moins de `tol_atr` ATR d'un niveau,
    sa clôture est au-delà du niveau + 0,1 ATR ; parmi les 20 barres précédentes, une clôture a franchi le niveau
    (+ 0,2 ATR) et, parmi les 40 barres précédentes, une clôture se trouvait de l'autre côté (bascule de rôle).
    Confirmation : mèche de rejet >= 30 % de l'amplitude OU clôture dans les 2/3 favorables ; volume de la barre
    >= moyenne des 10 précédentes (bonus, sinon pénalité).
    Filtres : heure de la barre entre 7h et 20h UTC (Londres / New York) ; ADX14 H1 >= 18 ; spread <= 12 % de l'ATR H1.
    SL : niveau − 0,5·`sl_atr` ATR (stop derrière le niveau, indépendant de la bougie), distance bornée à
    [0,4 ATR ; 3 ATR].
    TP : niveaux S/R suivants au-delà de l'entrée (jusqu'à 2), cible finale = max(`rr` R, niveau le plus lointain
    retenu) ; premier TP à 1,5 R.
    Invalidation : clôture M15 au-delà du niveau retesté.
    Score : structure 25 (bascule S/R + retest) + tendance H1 15 (0 et pénalité −10 si contre-tendance)
    + ADX H1 0-10 + rejet 0-15 (mèche) + volume 10 + volatilité 5 (amplitude <= 1,5 ATR) + exécution 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or not _valid(lt, "adx14") or lt["adx14"] < 18:
        return None
    hour = _bar_hour(le)
    if hour is None or not (7 <= hour <= 20):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    tr = _trend_of(lt)
    with_trend = bool(p.get("with_trend", True))
    if with_trend:
        s0 = _side_from(tr)
        if s0 is None:
            return None
        sides = [s0]
    else:
        sides = [Side.BUY, Side.SELL]
    levels = support_resistance(closed.iloc[:n - 12])
    if not levels:
        return None
    tol = float(p.get("tol_atr", 0.3)) * atr
    entry = float(le["close"])
    recent = closed.iloc[n - 21:n - 1]
    older = closed.iloc[n - 41:n - 1]
    for side in sides:
        sgn = side.sign
        if sgn * (le["close"] - le["open"]) <= 0:
            continue
        touch = float(le["low"]) if side is Side.BUY else float(le["high"])
        cands = [lv for lv in levels if abs(touch - lv) <= tol and sgn * (entry - lv) > 0.1 * atr]
        level = None
        for lv in cands:
            broke = bool((sgn * (recent["close"] - lv) > 0.2 * atr).any())
            other_side = bool((sgn * (older["close"] - lv) < -0.1 * atr).any())
            if broke and other_side:
                level = lv
                break
        if level is None:
            continue
        wick = _wick_against(le, side)
        cpos = _close_pos(le, side)
        if wick < 0.3 and cpos < 2.0 / 3.0:
            continue
        sl_atr = float(p.get("sl_atr", 1.0))
        sl = _bound_sl(snap, side, entry, level - sgn * 0.5 * sl_atr * atr, atr, 0.4, 3.0)
        if sl is None:
            continue
        score = 25.0
        pros = ["bascule S/R : niveau cassé puis retesté depuis l'autre côté", f"rejet du niveau (mèche {wick * 100:.0f} %)"]
        cons: list[str] = []
        aligned = (side is Side.BUY and tr == "UP") or (side is Side.SELL and tr == "DOWN")
        if aligned:
            score += 15
            pros.append(f"tendance {tr} sur {spec.timeframes['trend']}")
        else:
            score -= 10
            cons.append("contre-tendance H1")
        score += _clamp((float(lt["adx14"]) - 18.0) * 0.5, 0, 10)
        score += _clamp(wick * 30, 0, 15)
        vol_ma = float(closed["tick_volume"].iloc[n - 11:n - 1].mean())
        if np.isfinite(vol_ma) and float(le["tick_volume"]) >= vol_ma:
            score += 10
            pros.append("volume >= moyenne 10 barres")
        else:
            cons.append("volume sous la moyenne")
        if _range(le) <= 1.5 * atr:
            score += 5
        score += _clamp(cpos * 10, 0, 10)
        above = sorted([lv for lv in levels if sgn * (lv - entry) > 0], key=lambda x: sgn * (x - entry))[:2]
        cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                      "clôture M15 au-delà du niveau retesté", bt)
        return _finalize(_set_tp_plan(cand, side, entry, above, float(p.get("rr", 2.0)), first_r=1.5), snap)
    return None


# --------------------------------------------------------------------------------------------------------------
# D07 — liquidity_retest (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("D07")
def strategy_d07(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """D07 — Balayage de la liquidité sous un swing M15 en tendance H1, puis retest tenu et déplacement.

    Thèse : en tendance H1 haussière, les stops sous le dernier swing bas M15 sont une poche de liquidité ; une
    bougie qui pique sous ce swing mais clôture au-dessus (balayage) suivie d'une bougie qui tient au-dessus du
    swing ET clôture au-delà du plus haut de la bougie de balayage (déplacement) signale une absorption. On entre
    sur la bougie de confirmation, pas sur le balayage lui-même. Cible = liquidité opposée (plus haut récent).

    Entrée : si `with_trend` (défaut vrai), sens = `_trend_of` H1 (FLAT → rien), sinon les deux sens (BUY d'abord)
    avec pénalité ; poche de liquidité = un des 3 derniers swings bas (hauts) M15 confirmés hors des 5 dernières
    barres clôturées (le plus récent balayé est retenu) ; bougie de balayage parmi les 4 barres précédant la barre
    de signal : low < swing − 0,1 ATR et clôture > swing ; profondeur du balayage <= 1 ATR (sinon c'est une
    cassure) ; barres intermédiaires et barre de signal avec low >= swing − 0,1 ATR (retest tenu) ; barre de signal
    dans le sens du trade qui clôture au-delà du high (low) de la bougie de balayage.
    Confirmation : déplacement = (clôture − high du balayage) / ATR (graduée dans le score) ; structure M15 HH_HL
    (LH_LL) = bonus.
    Filtres : EMA20 H1 du bon côté de l'EMA50 H1 ; percentile de volatilité M15 <= 90 (pas de choc) ; spread
    <= 15 % de l'ATR H1.
    SL : sous (au-dessus) l'extrême de la mèche de balayage − 0,5·`sl_atr` ATR, distance bornée à [0,4 ATR ; 3 ATR].
    TP : plus haut (bas) des 40 barres clôturées précédant le signal = liquidité opposée ; cible finale =
    max(`rr` R, ce niveau) ; premier TP à 1,5 R.
    Invalidation : clôture M15 au-delà de l'extrême de la mèche de balayage.
    Score : structure 25 (balayage + retest tenu + déplacement) + tendance H1 15 (−10 contre-tendance) + MTF 10
    (structure M15 alignée) + momentum 0-10 (déplacement) + volatilité 10 (balayage <= 0,5 ATR) + exécution 0-10
    (corps) + fraîcheur 5 (balayage immédiatement avant le signal).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60:
        return None
    if _spread_ratio_h1(snap) > 0.15:
        return None
    if _valid(le, "vol_pct") and le["vol_pct"] > 90:
        return None
    tr = _trend_of(lt)
    with_trend = bool(p.get("with_trend", True))
    if with_trend:
        s0 = _side_from(tr)
        if s0 is None:
            return None
        sides = [s0]
    else:
        sides = [Side.BUY, Side.SELL]
    sh, sl_ = swing_points(closed.iloc[:n - 5])
    entry = float(le["close"])
    label = structure_label(closed)
    for side in sides:
        sgn = side.sign
        if sgn * (lt["ema20"] - lt["ema50"]) <= 0:
            continue
        if sgn * (le["close"] - le["open"]) <= 0:
            continue
        pool = sl_ if side is Side.BUY else sh
        if not pool:
            continue
        ref = None
        sweep_pos = None
        for _, px in reversed(pool[-3:]):  # du swing le plus récent au plus ancien
            for j in (n - 2, n - 3, n - 4, n - 5):
                r = closed.iloc[j]
                pierced = (r["low"] < px - 0.1 * atr) if side is Side.BUY else (r["high"] > px + 0.1 * atr)
                reclaimed = (r["close"] > px) if side is Side.BUY else (r["close"] < px)
                if pierced and reclaimed:
                    ref, sweep_pos = float(px), j
                    break
            if sweep_pos is not None:
                break
        if sweep_pos is None or ref is None:
            continue
        sweep = closed.iloc[sweep_pos]
        wick_ext = float(sweep["low"]) if side is Side.BUY else float(sweep["high"])
        depth = abs(ref - wick_ext)
        if depth > 1.0 * atr:
            continue
        between = closed.iloc[sweep_pos + 1:n - 1]
        if side is Side.BUY:
            held = bool((between["low"] >= ref - 0.1 * atr).all()) and float(le["low"]) >= ref - 0.1 * atr
            displaced = entry > float(sweep["high"])
            disp = (entry - float(sweep["high"])) / atr
        else:
            held = bool((between["high"] <= ref + 0.1 * atr).all()) and float(le["high"]) <= ref + 0.1 * atr
            displaced = entry < float(sweep["low"])
            disp = (float(sweep["low"]) - entry) / atr
        if not held or not displaced:
            continue
        sl_atr = float(p.get("sl_atr", 0.8))
        sl = _bound_sl(snap, side, entry, wick_ext - sgn * 0.5 * sl_atr * atr, atr, 0.4, 3.0)
        if sl is None:
            continue
        score = 25.0
        pros = [f"balayage du swing ({depth / atr:.2f} ATR sous le niveau)" if side is Side.BUY else
                f"balayage du swing ({depth / atr:.2f} ATR au-dessus du niveau)",
                "retest tenu puis déplacement au-delà de la bougie de balayage"]
        cons: list[str] = []
        aligned = (side is Side.BUY and tr == "UP") or (side is Side.SELL and tr == "DOWN")
        if aligned:
            score += 15
            pros.append(f"tendance {tr} sur {spec.timeframes['trend']}")
        else:
            score -= 10
            cons.append("contre-tendance H1")
        if label == ("HH_HL" if side is Side.BUY else "LH_LL"):
            score += 10
            pros.append("structure M15 alignée")
        else:
            cons.append(f"structure M15 {label}")
        score += _clamp(disp * 20, 0, 10)
        if depth <= 0.5 * atr:
            score += 10
        else:
            cons.append("balayage profond")
        score += _clamp(_body_ratio(le) * 10, 0, 10)
        if sweep_pos == n - 2:
            score += 5
        else:
            cons.append("balayage vieux de plusieurs barres")
        window = closed.iloc[n - 41:n - 1]
        target = float(window["high"].max()) if side is Side.BUY else float(window["low"].min())
        cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                      "clôture M15 au-delà de l'extrême de la mèche de balayage", bt)
        return _finalize(_set_tp_plan(cand, side, entry, [target], float(p.get("rr", 2.5)), first_r=1.5), snap)
    return None
