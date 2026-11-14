"""Famille C — stratégies de cassure (breakout) propres aux agents C01 … C09.

Chaque fonction `strategy_cXX` est enregistrée sous la clé `agent_id` (via `screeners.register`) et
remplace le screener générique de repli de l'agent. Règles communes à tout le module :

- Déterministe, pandas/numpy uniquement ; aucune donnée inventée (données insuffisantes → `None`).
- Décision sur la dernière barre CLÔTURÉE (`last_closed` / `iloc[:-1]`) ; la dernière ligne de chaque
  frame est la barre en formation et n'est JAMAIS lue.
- SL obligatoire, du bon côté, distance entre 0,3 et 3 ATR du tf d'entrée et au-dessus du `stops_level`
  broker (`_build` refuse déjà un SL du mauvais côté ; `_dist_ok` borne la distance).
- Largeur des ranges de session mesurée en ATR H1 (`snap.atr_h1`) : un range de 1 à 7 heures se compare à
  la volatilité horaire, pas à l'ATR M15.
- Plan de TP propre à chaque agent (niveaux structurels, projections de range, multiples de R) avec
  `rr >= max(1.5, spec.params["rr"])` garanti par `_with_tp_plan`.
- `setup_score` 0-100 = somme documentée de composantes (structure, confirmation/momentum, alignement MTF,
  volatilité, qualité d'exécution) ; ce n'est PAS une probabilité de gain. `_build` retranche ensuite une
  pénalité de spread (0-15 points).
- Les heures sont celles de la barre clôturée (UTC), pas l'horloge système : le Market Router filtre déjà
  la session, ici on affine la fenêtre horaire d'exploitation de chaque cassure.
- `REJECTIONS` compte les motifs de refus par agent (diagnostic « pourquoi pas de signal ») ; il n'influence
  jamais une décision.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import daily_high_low, last_closed, session_range, structure_label, support_resistance, swing_points
from ..registry import AgentSpec
from ..screeners import register, _build, _ctx, _structure_sl, _mtf_bonus, _trend_of, _frame, _valid, _clamp

SL_MIN_ATR = 0.3            # distance de SL minimale acceptée (en ATR du tf d'entrée ET en ATR H1)
SL_MAX_ATR = 3.0            # distance de SL maximale acceptée (en ATR du tf d'entrée)
SL_MAX_ATR_H1 = 3.0         # distance de SL maximale en ATR H1 (même invariant que SL_MAX_ATR)
STOPS_LEVEL_MARGIN = 1.5    # marge sur le stops_level broker (normalisation de prix, spread)
LONDON_CLOSE_H = 16.0       # fin de la session de Londres (UTC), cf. core.clock.current_session
MAX_STRUCT_RR = 6.0         # une cible structurelle au-delà de 6 R n'est pas un objectif de trade réaliste

REJECTIONS: Counter = Counter()   # (agent_id, motif) -> nombre de refus (diagnostic uniquement)


# ---------------------------------------------------------------- utilitaires du module
def _rej(agent_id: str, reason: str) -> None:
    """Enregistre un motif de refus et renvoie None (valeur de retour des stratégies)."""
    REJECTIONS[(agent_id, reason)] += 1
    return None


def _hhmm(s: str, default: float) -> float:
    """"HH:MM" → heure décimale (UTC). Valeur par défaut si le format est invalide."""
    try:
        hh, mm = str(s).strip().split(":")[:2]
        return int(hh) + int(mm) / 60.0
    except (ValueError, AttributeError):
        return default


def _bar_hour(row: pd.Series) -> Optional[float]:
    """Heure décimale UTC d'ouverture de la barre (None si horodatage absent)."""
    try:
        t = pd.Timestamp(row["time"])
    except (KeyError, TypeError, ValueError):
        return None
    if pd.isna(t):
        return None
    return t.hour + t.minute / 60.0


def _today(closed: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées appartenant au jour UTC de la dernière barre clôturée."""
    t = pd.to_datetime(closed["time"], utc=True)
    return closed[t.dt.normalize() == t.iloc[-1].normalize()]


def _bars_between(closed: pd.DataFrame, start_h: float, end_h: float) -> pd.DataFrame:
    """Barres clôturées du jour courant dont l'heure d'ouverture est dans [start_h, end_h)."""
    today = _today(closed)
    if today.empty:
        return today
    t = pd.to_datetime(today["time"], utc=True)
    h = t.dt.hour + t.dt.minute / 60.0
    return today[(h >= start_h) & (h < end_h)]


def _spread_ratio(snap, atr: float) -> float:
    """Spread courant exprimé en fraction de l'ATR du tf d'entrée (0 si indisponible)."""
    if atr <= 0 or snap.spec is None:
        return 0.0
    return float(snap.spread_points) * float(snap.spec.point) / atr


def _dist_ok(dist: float, atr: float, snap) -> bool:
    """Distance de SL acceptable : finie, dans [0,3 ; 3] ATR du tf d'entrée, dans [0,3 ; 3] ATR H1 (référence
    du `TradeCandidate.atr` validé par le gate) et au-dessus du stops_level broker (avec marge)."""
    if not np.isfinite(dist) or not (SL_MIN_ATR * atr <= dist <= SL_MAX_ATR * atr):
        return False
    atr_h1 = float(snap.atr_h1 or 0.0)
    if atr_h1 > 0 and not (SL_MIN_ATR * atr_h1 <= dist <= SL_MAX_ATR_H1 * atr_h1):
        return False
    min_broker = float(getattr(snap.spec, "min_stop_distance", 0.0) or 0.0) if snap.spec is not None else 0.0
    return dist >= STOPS_LEVEL_MARGIN * min_broker


def _range_ok(rng: float, atr_h1: float, lo_mult: float, hi_mult: float) -> bool:
    """Largeur de range de session dans [lo_mult, hi_mult] × ATR H1 (ATR H1 indisponible → refus)."""
    return atr_h1 > 0 and np.isfinite(rng) and lo_mult * atr_h1 <= rng <= hi_mult * atr_h1


def _with_tp_plan(c: Optional[TradeCandidate], side: Side, entry: float, dist: float, targets: list[float],
                  rr_min: float, first_r: float = 1.5) -> Optional[TradeCandidate]:
    """Remplace le plan de TP générique de `_build` par un plan propre.

    - `targets` : cibles structurelles (niveaux, projections) ; seules celles situées au-delà du premier TP
      partiel (`first_r` R) et à moins de `MAX_STRUCT_RR` R dans le sens du trade sont retenues.
    - Le premier TP est toujours à `first_r` R (prise partielle) ; la cible finale est la plus lointaine entre
      `rr_min` R et la cible structurelle la plus éloignée → `rr >= rr_min` garanti.
    - Au plus 3 niveaux (premier, intermédiaire, final), triés dans le sens du trade, sans doublon.
    """
    if c is None or dist <= 0:
        return c
    rr_min = max(1.5, float(rr_min))
    sign = side.sign
    tol = 1e-9 * max(1.0, abs(entry))
    pts = [float(x) for x in targets if x is not None and np.isfinite(x)
           and first_r * dist + tol < sign * (x - entry) <= MAX_STRUCT_RR * dist]
    final = entry + sign * dist * rr_min
    if pts:
        far = max(pts, key=lambda x: sign * (x - entry))
        if sign * (far - entry) > sign * (final - entry):
            final = far
    first = entry + sign * dist * first_r
    plan: list[float] = []
    for x in sorted([first, *pts, final], key=lambda v: sign * (v - entry)):
        if sign * (x - entry) > sign * (final - entry) + tol:
            continue  # jamais au-delà de la cible finale
        if plan and abs(x - plan[-1]) <= tol:
            continue  # doublon
        plan.append(float(x))
    if len(plan) > 3:
        plan = [plan[0], plan[len(plan) // 2], plan[-1]]
    c.tp_plan = plan
    c.rr = round(abs(plan[-1] - entry) / dist, 2)
    return c


def _h4_last(snap) -> Optional[pd.Series]:
    """Dernière barre H4 clôturée (None si le frame est absent/trop court ou EMA non calculées)."""
    h4 = snap.frames.get("H4")
    if h4 is None or len(h4) < 30:
        return None
    row = last_closed(h4)
    return row if _valid(row, "ema20", "ema50", "ema200", "close") else None


def _nearest_beyond(values: list[float], side: Side, entry: float, min_dist: float) -> list[float]:
    """Niveau le plus proche au-delà de l'entrée (à ≥ min_dist) dans le sens du trade, sous forme de liste."""
    lv = [v for v in values if side.sign * (v - entry) >= min_dist]
    return [min(lv, key=lambda v: abs(v - entry))] if lv else []


def _first_break(closed: pd.DataFrame, hi: float, lo: float, start_h: float, margin: float = 0.0):
    """Première cassure du jour d'un range [lo, hi], cherchée sur les barres CLÔTURÉES du jour courant dont
    l'heure d'ouverture est ≥ `start_h` (heure UTC de fin du range).

    Renvoie `(côté, borne cassée, âge en barres, barres du jour)` ou None si aucune clôture ne dépasse une
    borne de plus de `margin`. L'âge est le nombre de barres clôturées écoulées depuis la barre de cassure
    (0 = la cassure EST la dernière barre clôturée). Il permet d'exiger une cassure FRAÎCHE sans imposer
    qu'elle tombe exactement sur la barre évaluée : le scan ne tourne pas à chaque barre M15, et exiger la
    barre de cassure exacte revient en pratique à ne jamais signaler. Aucune barre en formation n'est lue.
    """
    day = _bars_between(closed, start_h, 24.0)
    if day.empty:
        return None
    c = day["close"].to_numpy(dtype=float)
    up, dn = c > hi + margin, c < lo - margin
    idx = np.flatnonzero(up | dn)
    if idx.size == 0:
        return None
    k = int(idx[0])
    side = Side.BUY if bool(up[k]) else Side.SELL
    return side, (hi if side is Side.BUY else lo), len(day) - 1 - k, day


# ================================================================ C01 — cassure du range asiatique
@register("C01")
def strategy_c01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C01 — asian_range_breakout (M15 / H1, sessions LONDON & OVERLAP).

    Thèse : le range construit pendant la session asiatique (00:00-07:00 UTC) concentre la liquidité ; la
    PREMIÈRE cassure du jour hors de ce range, pendant la matinée londonienne, tend à se prolonger tant que
    le prix reste accroché à la borne cassée.

    Entrée : première barre M15 clôturée hors du range après la fin de la session asiatique (cassure du jour) ;
    la décision est prise sur cette barre ou sur l'une des 16 barres clôturées suivantes (fenêtre
    d'exploitation), tant que la dernière barre clôturée reste hors du range (cassure TENUE). Le scan ne
    tourne pas à chaque barre M15 : exiger que la cassure tombe exactement sur la barre évaluée reviendrait
    à ne jamais signaler.
    Confirmation : bougie de cassure d'impulsion — corps ≥ 0,35 ATR OU clôture dans les 35 % extrêmes de la
    bougie (les deux ⇒ bonus de score).
    Filtres : range asiatique entre 0,8 et 4 ATR H1 (ni bruit, ni journée déjà consommée) ; heure de la barre
    dans [fin de session, fin + 5 h) ; extension au-delà de la borne ≤ 1,2 ATR (on ne court pas après le prix).
    SL : milieu du range asiatique (− 0,1 ATR de marge), plafonné à sl_atr × ATR derrière la borne cassée
    quand le range est large — une cassure valide ne doit pas revenir au centre du range.
    TP : 1,5 R (partiel), borne + 1 × hauteur du range (mouvement mesuré), puis max(rr × R, borne + 1,5 × range).
    Invalidation : clôture M15 de retour à l'intérieur du range asiatique.
    Score (0-100, somme de composantes documentées, PAS une probabilité de gain) = 35 (cassure nette d'un
    range valide) + 10 impulsion complète (corps ET clôture extrême) + 10 range compact (≤ 2 ATR H1)
    + 15 tendance H1 alignée (_mtf_bonus) + 5 volatilité médiane (vol_pct 20-80) + 10 extension ≤ 0,3 ATR
    + 5 cassure sur la barre courante − 10 tendance H1 opposée ; `_build` retranche la pénalité de spread.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 40:
        return _rej(aid, "historique court")
    hi, lo = session_range(closed, p.get("start", "00:00"), p.get("end", "07:00"))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return _rej(aid, "range asiatique indisponible")
    rng = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not _range_ok(rng, atr_h1, 0.8, 4.0):
        return _rej(aid, "largeur du range hors bornes")
    end_h = _hhmm(p.get("end", "07:00"), 7.0)
    h = _bar_hour(le)
    if h is None or not (end_h <= h < end_h + 5.0):
        return _rej(aid, "hors fenêtre horaire")
    brk = _first_break(closed, hi, lo, end_h)
    if brk is None:
        return _rej(aid, "aucune cassure du range aujourd'hui")
    side, boundary, age, day = brk
    entry = float(le["close"])
    ext = side.sign * (entry - boundary)
    if ext <= 0:
        return _rej(aid, "cassure non tenue (retour dans le range)")
    if age > 16:
        return _rej(aid, "cassure trop ancienne")
    bk = day.iloc[len(day) - 1 - age]          # bougie de cassure (barre clôturée)
    bar_rng = float(bk["high"] - bk["low"])
    if bar_rng <= 0:
        return _rej(aid, "bougie sans range")
    body = abs(float(bk["close"] - bk["open"]))
    close_pos = (float(bk["close"] - bk["low"]) if side is Side.BUY else float(bk["high"] - bk["close"])) / bar_rng
    strong_close, strong_body = close_pos >= 0.65, body >= 0.35 * atr
    if not (strong_close or strong_body):
        return _rej(aid, "bougie sans impulsion")
    if ext > 1.2 * atr:
        return _rej(aid, "extension trop grande")
    sl_atr = float(p.get("sl_atr", 1.0))
    if side is Side.BUY:
        sl = max((hi + lo) / 2.0 - 0.1 * atr, hi - sl_atr * atr)
    else:
        sl = min((hi + lo) / 2.0 + 0.1 * atr, lo + sl_atr * atr)
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 35.0
    pros = [f"première cassure du jour hors du range asiatique {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1)",
            f"bougie de cassure d'impulsion (corps {body / atr:.2f} ATR, clôture à {close_pos * 100:.0f} % de la bougie)"]
    cons: list[str] = []
    if strong_close and strong_body:
        score += 10
        pros.append("impulsion complète (corps et clôture extrême)")
    if rng <= 2.0 * atr_h1:
        score += 10
        pros.append("range asiatique compact")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    tr = _trend_of(lt)
    if (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP"):
        score -= 10
        cons.append("tendance H1 opposée à la cassure")
    if _valid(le, "vol_pct") and 20 <= le["vol_pct"] <= 80:
        score += 5
    if ext <= 0.3 * atr:
        score += 10
        pros.append("entrée proche de la borne")
    else:
        cons.append(f"extension {ext / atr:.2f} ATR au-delà de la borne")
    if age == 0:
        score += 5
    else:
        cons.append(f"cassure vieille de {age} barre(s) M15 : une partie du mouvement est faite")
    cons.append("faux breakout possible : SL à l'intérieur du range")
    inv = f"clôture M15 de retour à l'intérieur du range asiatique ({lo:.5g}-{hi:.5g})"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    targets = [boundary + side.sign * rng, boundary + side.sign * 1.5 * rng]
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.0))


# ================================================================ C02 — cassure de l'ouverture de Londres
@register("C02")
def strategy_c02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C02 — london_breakout (M15 / H1, sessions LONDON & OVERLAP).

    Thèse : les deux premières heures de Londres (07:00-09:00 UTC) fixent le range d'ouverture ; sa cassure
    accompagnée d'un momentum MACD orienté donne la direction de la matinée.

    Entrée : première barre M15 clôturée au-delà d'une borne d'au moins 0,15 ATR après la fin du range ;
    la décision est prise sur cette barre ou sur l'une des 8 barres clôturées suivantes, à condition que la
    dernière barre clôturée soit toujours au moins 0,15 ATR au-delà de la borne (cassure tenue).
    Confirmation : histogramme MACD (tf d'entrée) du signe de la cassure ET en progression sur l'une des deux
    dernières barres clôturées (le momentum peut respirer une barre sans invalider la cassure).
    Filtres : range 0,5-3 ATR H1 ; heure de la barre dans [fin du range, fin + 3 h) ; extension ≤ 1 ATR.
    SL : extrême de la JAMBE de cassure (de la bougie de cassure à la dernière barre clôturée) − 0,2 ATR,
    et au moins 0,5 ATR sous/au-dessus de l'entrée.
    TP : 1,5 R partiel, puis swing H1 confirmé le plus proche au-delà de l'entrée (structure du tf de tendance),
    cible finale max(rr × R, swing H1).
    Invalidation : clôture M15 de retour dans le range OU histogramme MACD qui change de signe.
    Score (0-100, somme documentée, PAS une probabilité) = 30 (cassure du range d'ouverture) + 15 momentum
    MACD orienté + 10 range ≤ 1 ATR H1 (compression pré-Londres) + 15 tendance H1 alignée + 10 ADX H1 ≥ 25
    + 10 extension ≤ 0,3 ATR + 5 cassure sur la barre courante ; `_build` retranche la pénalité de spread.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 40 or not _valid(le, "macd_hist"):
        return _rej(aid, "historique court")
    prev, pre = closed.iloc[-2], closed.iloc[-3]
    if not _valid(prev, "macd_hist", "close") or not _valid(pre, "macd_hist"):
        return _rej(aid, "MACD indisponible")
    hi, lo = session_range(closed, p.get("start", "07:00"), p.get("end", "09:00"))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return _rej(aid, "range d'ouverture indisponible")
    rng = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not _range_ok(rng, atr_h1, 0.5, 3.0):
        return _rej(aid, "largeur du range hors bornes")
    end_h = _hhmm(p.get("end", "09:00"), 9.0)
    h = _bar_hour(le)
    if h is None or not (end_h <= h < end_h + 3.0):
        return _rej(aid, "hors fenêtre horaire")
    brk = _first_break(closed, hi, lo, end_h, margin=0.15 * atr)
    if brk is None:
        return _rej(aid, "pas de cassure du range d'ouverture")
    side, boundary, age, day = brk
    if age > 8:
        return _rej(aid, "cassure trop ancienne")
    entry = float(le["close"])
    sgn = side.sign
    ext = sgn * (entry - boundary)
    if ext < 0.15 * atr:
        return _rej(aid, "cassure non tenue")
    hist, hist_prev, hist_pre = float(le["macd_hist"]), float(prev["macd_hist"]), float(pre["macd_hist"])
    if not (sgn * hist > 0 and (sgn * (hist - hist_prev) > 0 or sgn * (hist_prev - hist_pre) > 0)):
        return _rej(aid, "MACD non confirmé")
    if ext > 1.0 * atr:
        return _rej(aid, "extension trop grande")
    leg = day.iloc[len(day) - 1 - age:]            # jambe de cassure (barres clôturées uniquement)
    if side is Side.BUY:
        sl = min(float(leg["low"].min()) - 0.2 * atr, entry - 0.5 * atr)
    else:
        sl = max(float(leg["high"].max()) + 0.2 * atr, entry + 0.5 * atr)
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0 + 15.0
    pros = [f"cassure du range d'ouverture Londres {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1), tenue sur {age + 1} barre(s)",
            "histogramme MACD orienté dans le sens de la cassure"]
    cons: list[str] = []
    if rng <= 1.0 * atr_h1:
        score += 10
        pros.append("range d'ouverture étroit (compression pré-Londres)")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if _valid(lt, "adx14") and lt["adx14"] >= 25:
        score += 10
        pros.append(f"ADX H1 {lt['adx14']:.0f}")
    else:
        cons.append("tendance H1 peu affirmée (ADX < 25)")
    if ext <= 0.3 * atr:
        score += 10
    else:
        cons.append(f"extension {ext / atr:.2f} ATR : entrée moins favorable")
    if age == 0:
        score += 5
    sh, sl_pts = swing_points(t.iloc[:-1])
    swings = [v for _, v in (sh if side is Side.BUY else sl_pts)]
    targets = _nearest_beyond(swings, side, entry, 0.5 * dist)
    if not targets:
        cons.append("aucun swing H1 comme cible : multiples de R")
    cons.append("jambe de cassure comme référence de SL : un retest profond invalide le trade")
    inv = f"clôture M15 de retour dans le range ({lo:.5g}-{hi:.5g}) ou histogramme MACD de signe inverse"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.0))


# ================================================================ C03 — cassure de l'ouverture de New York
@register("C03")
def strategy_c03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C03 — newyork_breakout (M15 / H1, sessions NEWYORK & OVERLAP).

    Thèse : le range 12:00-14:00 UTC (pré-ouverture US) est souvent un piège ; on n'entre qu'après DEUX
    clôtures M15 consécutives hors du range, sans réintégration depuis la cassure, dans le sens de la
    tendance (ou de la structure) H1, pendant le cœur de la session US.

    Entrée : première cassure du jour après la fin du range (`_first_break`), au plus 14 barres clôturées plus
    tôt (fenêtre d'exploitation) ; la dernière barre clôturée ET la précédente sont hors du range (double
    clôture) et aucune clôture n'est revenue dans le range depuis la cassure ; la dernière barre n'a pas
    replongé dans le range de plus de 0,3 ATR (mèche).
    Confirmation : au moins UNE des trois lectures H1 alignées (tendance EMA complète, structure HH_HL / LH_LL,
    ou simple EMA20 vs EMA50) ; refus si la tendance H1 est franchement opposée.
    Filtres : range 0,5-3 ATR H1 ; heure de la barre dans [fin du range, fin + 4,5 h) — cœur de la séance US ;
    spread ≤ 0,12 ATR ; extension ≤ min(3 ATR ; max(1,2 ATR ; 1 × hauteur du range)).
    SL : borne cassée − 0,3 ATR, borné entre 0,7 et 2,2 ATR de l'entrée — une double clôture éloigne l'entrée
    de la borne : sans plancher le SL passerait sous le stops_level (et sous 0,3 ATR H1), sans plafond il
    sortirait des bornes de SL du module ; au-delà, le SL devient un stop ATR assumé.
    TP : 1,5 R partiel, plus haut/bas de la veille (D1) comme cible structurelle, final max(rr × R, PDH/PDL).
    Invalidation : clôture M15 de retour sous/au-dessus de la borne du range NY.
    Score (0-100, somme documentée, PAS une probabilité) = 30 (cassure NY) + 15 double clôture tenue
    + 10 structure H1 alignée + 15 tendance H1 alignée + 5 volatilité médiane + 10 spread ≤ 0,05 ATR ;
    `_build` retranche la pénalité de spread.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 40:
        return _rej(aid, "historique court")
    hi, lo = session_range(closed, p.get("start", "12:00"), p.get("end", "14:00"))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return _rej(aid, "range NY indisponible")
    rng = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not _range_ok(rng, atr_h1, 0.5, 3.0):
        return _rej(aid, "largeur du range hors bornes")
    end_h = _hhmm(p.get("end", "14:00"), 14.0)
    h = _bar_hour(le)
    if h is None or not (end_h <= h < end_h + 4.5):
        return _rej(aid, "hors fenêtre horaire")
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return _rej(aid, "spread trop élevé")
    brk = _first_break(closed, hi, lo, end_h)
    if brk is None:
        return _rej(aid, "pas de cassure du range NY")
    side, boundary, age, day = brk
    if age > 14:
        return _rej(aid, "cassure trop ancienne")
    entry = float(le["close"])
    sgn = side.sign
    prev = closed.iloc[-2]
    if sgn * (entry - boundary) <= 0 or sgn * (float(prev["close"]) - boundary) <= 0:
        return _rej(aid, "pas de double clôture hors du range")
    held = day.iloc[len(day) - 1 - age:]
    if bool((sgn * (held["close"] - boundary) <= 0).any()):
        return _rej(aid, "réintégration du range depuis la cassure")
    dip = (boundary - float(le["low"])) if side is Side.BUY else (float(le["high"]) - boundary)
    if dip > 0.3 * atr:
        return _rej(aid, "dernière barre replongée dans le range")
    tr = _trend_of(lt)
    st = structure_label(t.iloc[:-1])
    aligned = st == ("HH_HL" if side is Side.BUY else "LH_LL")
    ema_ok = sgn * (float(lt["ema20"]) - float(lt["ema50"])) > 0
    if (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP"):
        return _rej(aid, "tendance H1 opposée")
    if not (tr != "FLAT" or aligned or ema_ok):
        return _rej(aid, "ni tendance, ni structure, ni EMA H1 alignées")
    ext = sgn * (entry - boundary)
    if ext > min(3.0 * atr, max(1.2 * atr, 1.0 * rng)):
        return _rej(aid, "extension trop grande")
    # SL derrière la borne cassée, plancher 0,7 ATR (stops_level / borne ATR H1) et plafond 2,2 ATR
    sl = boundary - sgn * 0.3 * atr
    sl = min(sl, entry - 0.7 * atr) if side is Side.BUY else max(sl, entry + 0.7 * atr)
    sl = max(sl, entry - 2.2 * atr) if side is Side.BUY else min(sl, entry + 2.2 * atr)
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0 + 15.0
    pros = [f"deux clôtures M15 consécutives hors du range NY {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1)",
            f"cassure tenue depuis {age + 1} barre(s) (aucune réintégration du range)"]
    cons: list[str] = []
    if aligned:
        score += 10
        pros.append(f"structure H1 {st} alignée")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if _valid(le, "vol_pct") and 20 <= le["vol_pct"] <= 80:
        score += 5
    if spread <= 0.05:
        score += 10
    targets: list[float] = []
    d1 = snap.frames.get("D1")
    if d1 is not None and len(d1) >= 3:
        ph, pl = daily_high_low(d1)
        lvl = ph if side is Side.BUY else pl
        if np.isfinite(lvl):
            targets.append(float(lvl))
    if not targets:
        cons.append("pas de niveau D1 exploitable comme cible : multiples de R")
    cons.append("entrée sur la seconde clôture : une partie du mouvement est déjà faite")
    inv = f"clôture M15 de retour {'sous' if side is Side.BUY else 'au-dessus de'} la borne {boundary:.5g} du range NY"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.0))


# ================================================================ C04 — cassure du plus haut / plus bas de la veille
@register("C04")
def strategy_c04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C04 — daily_high_low_breakout (M15 / H1, toutes sessions).

    Thèse : le plus haut / plus bas de la veille (D1) est un niveau de liquidité observé par tous ; sa première
    clôture M15 au-delà, avec un momentum RSI cohérent et un contexte H4 non opposé, ouvre une extension de
    la journée.

    Entrée : première clôture M15 au-delà du PDH/PDL (barre précédente clôturée en deçà), extension ≤ 0,5 ATR.
    Confirmation : RSI14 ≥ 55 (BUY) / ≤ 45 (SELL) ; tendance H4 (EMA) non opposée.
    Filtres : range de la veille ≥ 1,5 × ATR H1 (journée précédente réellement directionnelle) ; ≥ 3 barres D1.
    SL : niveau cassé − 0,5 ATR, plafonné à sl_atr × ATR sous l'entrée (hybride niveau / ATR).
    TP : 1,5 R partiel, projection 61,8 % du range de la veille depuis le niveau, final max(rr × R, projection).
    Invalidation : clôture M15 sous/au-dessus du niveau de la veille.
    Score = 30 (cassure PDH/PDL) + 15 première clôture + 10 RSI directionnel + 15 tendance H1 alignée
            + 10 tendance H4 alignée + 5 extension ≤ 0,25 ATR − 5 session ASIA.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    d1 = snap.frames.get("D1")
    if not c or d1 is None or len(d1) < 3:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 3:
        return _rej(aid, "historique court")
    ph, pl = daily_high_low(d1)
    if not (np.isfinite(ph) and np.isfinite(pl)) or ph <= pl:
        return _rej(aid, "niveaux de la veille indisponibles")
    pd_range = ph - pl
    atr_h1 = float(snap.atr_h1 or 0.0)
    if atr_h1 > 0 and pd_range < 1.5 * atr_h1:
        return _rej(aid, "range de la veille trop étroit")
    prev = closed.iloc[-2]
    entry = float(le["close"])
    h4 = _h4_last(snap)
    tr4 = _trend_of(h4) if h4 is not None else "FLAT"
    sl_atr = float(p.get("sl_atr", 1.2))
    if entry > ph and prev["close"] <= ph:
        if le["rsi14"] < 55 or tr4 == "DOWN":
            return _rej(aid, "RSI ou H4 non confirmé")
        side, level, ext = Side.BUY, ph, entry - ph
        sl = max(ph - 0.5 * atr, entry - sl_atr * atr)
    elif entry < pl and prev["close"] >= pl:
        if le["rsi14"] > 45 or tr4 == "UP":
            return _rej(aid, "RSI ou H4 non confirmé")
        side, level, ext = Side.SELL, pl, pl - entry
        sl = min(pl + 0.5 * atr, entry + sl_atr * atr)
    else:
        return _rej(aid, "pas de première clôture hors des niveaux de la veille")
    if ext > 0.5 * atr:
        return _rej(aid, "extension trop grande")
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0 + 15.0 + 10.0
    pros = [f"première clôture M15 au-delà du {'plus haut' if side is Side.BUY else 'plus bas'} de la veille ({level:.5g})",
            f"RSI14 {le['rsi14']:.0f} dans le sens de la cassure"]
    cons: list[str] = []
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if (side is Side.BUY and tr4 == "UP") or (side is Side.SELL and tr4 == "DOWN"):
        score += 10
        pros.append("tendance H4 alignée")
    elif h4 is None:
        cons.append("contexte H4 indisponible")
    if ext <= 0.25 * atr:
        score += 5
    if str(getattr(snap.session, "value", snap.session)) == "ASIA":
        score -= 5
        cons.append("session asiatique : participation réduite")
    cons.append("niveau de la veille : chasse de stops fréquente avant continuation")
    target = level + side.sign * 0.618 * pd_range
    inv = f"clôture M15 {'sous' if side is Side.BUY else 'au-dessus du'} niveau de la veille {level:.5g}"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    return _with_tp_plan(cand, side, entry, dist, [target], p.get("rr", 2.0))


# ================================================================ C05 — compression puis expansion
@register("C05")
def strategy_c05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C05 — compression_expansion (M15 / H1, toutes sessions).

    Thèse : une compression conjointe de la largeur des bandes de Bollinger ET de l'ATR (énergie stockée)
    se résout par une barre d'expansion qui sort de la « boîte » des 12 dernières barres ; on suit cette
    expansion.

    Entrée : la dernière barre clôturée clôture hors de la boîte (max/min des 12 barres précédentes) dans le
    sens de sa propre bougie (clôture > ouverture pour un BUY).
    Confirmation : range de la barre ≥ 1,2 × range moyen de la boîte (barre d'expansion) ; tick_volume ≥ 0,8 ×
    volume moyen de la boîte (pas de cassure sur volume anémique).
    Filtres (état de compression à la barre PRÉCÉDENTE) : largeur BB ≤ 20e percentile des 100 barres
    antérieures ET ATR14 < moyenne ATR14 des 50 barres antérieures ; extension hors boîte ≤ 0,8 ATR.
    SL : côté opposé de la boîte − 0,1 ATR ; refus si la boîte est plus large que 2,5 ATR (pas une compression).
    TP : 1,5 R partiel, projections de la hauteur de la boîte (× 1 et × 2 depuis la borne), final
    max(rr × R, borne + 2 × hauteur).
    Invalidation : clôture M15 de retour à l'intérieur de la boîte.
    Score = 30 (compression validée) + (20 − percentile) × 0,5 (0-10) + 10 range ≥ 1,8 × boîte + 10 volume
            ≥ 1,3 × + 15 tendance H1 alignée + 5 ADX H1 < 20 (énergie non encore libérée) + 5 extension ≤ 0,3 ATR.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 120 or "tick_volume" not in closed.columns:
        return _rej(aid, "historique court ou volume absent")
    box_len = int(p.get("box_len", 12))
    box = closed.iloc[-1 - box_len:-1]
    prev = closed.iloc[-2]
    if len(box) < box_len or not _valid(prev, "bb_width", "atr14"):
        return _rej(aid, "boîte ou indicateurs indisponibles")
    window = closed["bb_width"].iloc[-102:-2].dropna()
    if len(window) < 80:
        return _rej(aid, "percentile BB non calculable")
    pct = float((window <= prev["bb_width"]).mean() * 100.0)
    atr_ref = closed["atr14"].iloc[-52:-2].mean()
    if pct > 20.0 or not np.isfinite(atr_ref) or prev["atr14"] >= atr_ref:
        return _rej(aid, "pas de compression")
    box_hi, box_lo = float(box["high"].max()), float(box["low"].min())
    box_h = box_hi - box_lo
    box_avg_rng = float((box["high"] - box["low"]).mean())
    box_avg_vol = float(box["tick_volume"].mean())
    bar_rng = float(le["high"] - le["low"])
    vol = float(le["tick_volume"]) if pd.notna(le.get("tick_volume")) else math.nan
    if box_h <= 0 or box_avg_rng <= 0 or not np.isfinite(vol) or box_avg_vol <= 0:
        return _rej(aid, "boîte dégénérée")
    entry = float(le["close"])
    if entry > box_hi and le["close"] > le["open"]:
        side, boundary, ext = Side.BUY, box_hi, entry - box_hi
        sl = box_lo - 0.1 * atr
    elif entry < box_lo and le["close"] < le["open"]:
        side, boundary, ext = Side.SELL, box_lo, box_lo - entry
        sl = box_hi + 0.1 * atr
    else:
        return _rej(aid, "pas de clôture hors de la boîte")
    if bar_rng < 1.2 * box_avg_rng:
        return _rej(aid, "pas de barre d'expansion")
    if vol < 0.8 * box_avg_vol:
        return _rej(aid, "volume anémique")
    if ext > 0.8 * atr or box_h > 2.5 * atr:
        return _rej(aid, "extension ou boîte trop grande")
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0 + _clamp((20.0 - pct) * 0.5, 0, 10)
    pros = [f"compression : largeur BB au {pct:.0f}e percentile, ATR sous sa moyenne 50",
            f"barre d'expansion ({bar_rng / box_avg_rng:.1f} × range moyen) hors de la boîte {box_lo:.5g}-{box_hi:.5g}"]
    cons: list[str] = []
    if bar_rng >= 1.8 * box_avg_rng:
        score += 10
    if vol >= 1.3 * box_avg_vol:
        score += 10
        pros.append(f"volume {vol / box_avg_vol:.1f} × moyenne de la boîte")
    elif vol < 1.0 * box_avg_vol:
        cons.append(f"volume {vol / box_avg_vol:.1f} × moyenne de la boîte : participation faible")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if _valid(lt, "adx14") and lt["adx14"] < 20:
        score += 5
        pros.append("ADX H1 faible : mouvement non encore engagé")
    if ext <= 0.3 * atr:
        score += 5
    else:
        cons.append(f"extension {ext / atr:.2f} ATR hors de la boîte")
    cons.append("première barre d'expansion : peut être un simple test de la boîte")
    inv = f"clôture M15 de retour dans la boîte de compression ({box_lo:.5g}-{box_hi:.5g})"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.5), _clamp(score), pros, cons, inv, bt)
    targets = [boundary + side.sign * box_h, boundary + side.sign * 2.0 * box_h]
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.5))


# ================================================================ C06 — cassure de volatilité (canal de Keltner)
@register("C06")
def strategy_c06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C06 — volatility_breakout (M15 / H1, toutes sessions).

    Thèse : quand la volatilité s'expanse brutalement (range moyen des 3 dernières barres ≥ atr_ratio × l'ATR14
    d'AVANT ces barres) alors que le percentile de volatilité est déjà élevé, une barre à large range qui
    clôture hors du canal de Keltner (EMA20 ± kc_mult × ATR) signale un déplacement directionnel que la
    volatilité devrait prolonger.

    Entrée : clôture M15 au-delà de EMA20 + kc_mult × ATR (BUY) ou en deçà de EMA20 − kc_mult × ATR (SELL).
    Confirmation : range de la barre ≥ 1,2 ATR (large range) ; clôture dans les 25 % extrêmes ; mom10 du même signe.
    Filtres : expansion fraîche (range moyen 3 barres ≥ atr_ratio × ATR14 de la 4e barre précédente) ;
    vol_pct ≥ 50 ; RSI14 non extrême (≤ 80 BUY / ≥ 20 SELL) ; spread ≤ 0,12 ATR.
    SL : purement ATR — entrée − sl_atr × ATR (distance calibrée sur la volatilité courante, sans niveau).
    TP : 1 R (prise rapide, la volatilité coupe dans les deux sens), 2 R, final max(rr, 2) × R.
    Invalidation : clôture M15 de retour sous/au-dessus de l'EMA20 (milieu du canal).
    Score = 30 (sortie de canal) + (ratio d'expansion − 1) × 40 (0-15) + 10 large range + 10 clôture extrême
            + 15 tendance H1 alignée + 5 spread ≤ 0,06 ATR − 10 tendance H1 opposée.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 40 or not _valid(le, "mom10", "vol_pct"):
        return _rej(aid, "historique court ou vol_pct indisponible")
    atr_before = float(closed["atr14"].iloc[-4])
    tr3 = float((closed["high"] - closed["low"]).iloc[-3:].mean())
    if not np.isfinite(atr_before) or atr_before <= 0:
        return _rej(aid, "ATR de référence indisponible")
    ratio = tr3 / atr_before
    if ratio < float(p.get("atr_ratio", 1.3)):
        return _rej(aid, "pas d'expansion de volatilité")
    if le["vol_pct"] < 50:
        return _rej(aid, "percentile de volatilité bas")
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return _rej(aid, "spread trop élevé")
    kc = float(p.get("kc_mult", 1.2))
    entry = float(le["close"])
    bar_rng = float(le["high"] - le["low"])
    if bar_rng < 1.2 * atr:
        return _rej(aid, "barre trop étroite")
    upper, lower = float(le["ema20"]) + kc * atr, float(le["ema20"]) - kc * atr
    sl_atr = float(p.get("sl_atr", 1.2))
    if entry > upper and le["mom10"] > 0 and le["rsi14"] <= 80 and (le["close"] - le["low"]) / bar_rng >= 0.75:
        side = Side.BUY
        sl = entry - sl_atr * atr
    elif entry < lower and le["mom10"] < 0 and le["rsi14"] >= 20 and (le["high"] - le["close"]) / bar_rng >= 0.75:
        side = Side.SELL
        sl = entry + sl_atr * atr
    else:
        return _rej(aid, "pas de sortie de canal confirmée")
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0 + _clamp((ratio - 1.0) * 40.0, 0, 15) + 10.0 + 10.0
    pros = [f"expansion de volatilité ×{ratio:.2f} (vol_pct {le['vol_pct']:.0f})", f"clôture hors du canal Keltner EMA20 ± {kc} ATR",
            f"barre large ({bar_rng / atr:.1f} ATR) à clôture extrême, momentum aligné"]
    cons = ["volatilité élevée : slippage et retours violents possibles"]
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    tr = _trend_of(lt)
    if (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP"):
        score -= 10
        cons.append("tendance H1 opposée : expansion possiblement corrective")
    if spread <= 0.06:
        score += 5
    inv = f"clôture M15 de retour {'sous' if side is Side.BUY else 'au-dessus de'} l'EMA20 ({le['ema20']:.5g})"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    targets = [entry + side.sign * dist, entry + side.sign * 2.0 * dist]
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.0), first_r=1.0)


# ================================================================ C07 — cassure puis retest d'un niveau S/R
@register("C07")
def strategy_c07(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C07 — breakout_retest (M15 / H1, toutes sessions).

    Thèse : un niveau support/résistance (regroupement de swings sur 100 barres) cassé puis RETESTÉ avec une
    mèche de rejet devient un point d'entrée à risque défini : le niveau a changé de polarité.

    Entrée : parmi les niveaux S/R calculés sur les barres antérieures, un niveau L tel que, dans la fenêtre
    des 16 barres précédant les 2 dernières, le prix clôturait d'abord sous L puis a clôturé au-dessus de
    L + 0,2 ATR (cassure) ; la barre précédente a clôturé au-dessus de L (niveau tenu) ; la dernière barre
    clôturée est revenue toucher L (low ≤ L + tol_atr × ATR) et a clôturé au-dessus de L, haussière.
    Confirmation : mèche de rejet ≥ 40 % du range de la bougie de retest ; tendance H1 non opposée.
    Filtres : extension au-dessus de L ≤ 1 ATR ; bougie de retest ≤ 2,5 ATR.
    SL : extrême de la bougie de retest − 0,3 ATR.
    TP : 1,5 R partiel, prochain niveau S/R au-delà de l'entrée, final max(rr × R, prochain niveau).
    Invalidation : clôture M15 sous/au-dessus du niveau retesté.
    Score = 35 (cassure + retest tenu) + 10 mèche ≥ 60 % + 10 clôture ≥ L + 0,2 ATR + 15 tendance H1 alignée
            + 5 retest précis (profondeur ≤ tol/2) − 10 aucun niveau cible.
    """
    aid = spec.agent_id
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 120:
        return _rej(aid, "historique court")
    levels = support_resistance(closed.iloc[:-2])
    if not levels:
        return _rej(aid, "aucun niveau S/R")
    tol = float(p.get("tol_atr", 0.3)) * atr
    prev = closed.iloc[-2]
    win = closed.iloc[-18:-2]
    if len(win) < 10:
        return _rej(aid, "fenêtre de cassure trop courte")
    entry = float(le["close"])
    bar_rng = float(le["high"] - le["low"])
    if bar_rng <= 0 or bar_rng > 2.5 * atr:
        return _rej(aid, "bougie de retest dégénérée")
    tr = _trend_of(lt)
    lower_wick = float(min(le["open"], le["close"]) - le["low"])
    upper_wick = float(le["high"] - max(le["open"], le["close"]))
    side: Optional[Side] = None
    level = math.nan
    if le["close"] > le["open"] and tr != "DOWN" and lower_wick / bar_rng >= 0.4:
        for lv in sorted(levels, reverse=True):
            if not (lv < entry <= lv + 1.0 * atr) or le["low"] > lv + tol or prev["close"] <= lv:
                continue
            if win["close"].iloc[0] < lv and bool((win["close"] > lv + 0.2 * atr).any()):
                side, level = Side.BUY, float(lv)
                break
    if side is None and le["close"] < le["open"] and tr != "UP" and upper_wick / bar_rng >= 0.4:
        for lv in sorted(levels):
            if not (lv - 1.0 * atr <= entry < lv) or le["high"] < lv - tol or prev["close"] >= lv:
                continue
            if win["close"].iloc[0] > lv and bool((win["close"] < lv - 0.2 * atr).any()):
                side, level = Side.SELL, float(lv)
                break
    if side is None:
        return _rej(aid, "pas de retest de niveau cassé")
    sl = float(le["low"]) - 0.3 * atr if side is Side.BUY else float(le["high"]) + 0.3 * atr
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    wick = lower_wick if side is Side.BUY else upper_wick
    depth = abs(float(le["low"] if side is Side.BUY else le["high"]) - level)
    score = 35.0
    pros = [f"niveau S/R {level:.5g} cassé puis retesté et tenu", f"mèche de rejet {wick / bar_rng * 100:.0f} % de la bougie"]
    cons: list[str] = []
    if wick / bar_rng >= 0.6:
        score += 10
    if side.sign * (entry - level) >= 0.2 * atr:
        score += 10
        pros.append("clôture nette au-delà du niveau retesté")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if depth <= tol / 2:
        score += 5
        pros.append("retest précis du niveau")
    targets = _nearest_beyond(levels, side, entry, 0.5 * dist)
    if not targets:
        score -= 10
        cons.append("aucun niveau S/R au-delà comme cible : multiples de R")
    cons.append("le retest peut se transformer en réintégration (faux breakout)")
    inv = f"clôture M15 {'sous' if side is Side.BUY else 'au-dessus du'} niveau retesté {level:.5g}"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.5), _clamp(score), pros, cons, inv, bt)
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.5))


# ================================================================ C08 — cassure du range asiatique sur l'or
@register("C08")
def strategy_c08(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C08 — asian_range_breakout_gold (M15 / H1, métaux, session LONDON).

    Thèse : sur l'or, la sortie du range asiatique pendant la séance de Londres n'est exploitable que si elle
    est portée par une impulsion ET par une tendance H1 déjà active (ADX) ; les cassures sans élan sur l'or
    sont typiquement des chasses de stops.

    Entrée : première clôture M15 du jour au-delà de la borne du range asiatique d'au moins 0,1 ATR, au plus
    32 barres clôturées plus tôt (fenêtre d'exploitation), la dernière barre clôturée restant au-delà de la
    borne (cassure tenue).
    Confirmation : au moins DEUX des trois confluences suivantes (exiger les trois simultanément ne se
    produit quasiment jamais) — impulsion (clôture progressée de ≥ 0,3 ATR sur 3 barres dans le sens),
    RSI14 > 50 (BUY) / < 50 (SELL), extrême du jour touché à 0,5 ATR près.
    Filtres : classe d'actif « metals » ; range 0,6-4,5 ATR H1 ; heure de la barre entre la fin de la session
    asiatique et la clôture de Londres (16:00 UTC) ; spread ≤ 0,08 ATR (le spread de l'or pèse) ; tendance H1
    non opposée et (ADX H1 ≥ 18 ou tendance H1 alignée) ; extension ≤ min(3 ATR ; max(1,5 ATR ; 1 × hauteur
    du range)).
    SL : structure — derrière le dernier swing M15 confirmé via `_structure_sl` (sl_atr = 1,2), plafonné à
    2,8 ATR de l'entrée (un swing trop lointain sortirait des bornes de SL du module).
    TP : 1,5 R partiel, swing H4 confirmé le plus proche au-delà de l'entrée, final max(rr × R, swing H4).
    Invalidation : clôture H1 de retour sous/au-dessus de la borne du range asiatique.
    Score (0-100, somme documentée, PAS une probabilité) = 30 (cassure asiatique sur l'or) + 8 par confluence
    validée (16 ou 24) + 10 ADX H1 ≥ 25 + 15 tendance H1 alignée + 10 exécution (spread ≤ 0,04 ATR et
    extension ≤ 0,4 ATR) − 10 aucun swing H4 cible ; `_build` retranche la pénalité de spread.
    """
    aid = spec.agent_id
    if snap.spec is None or getattr(snap.spec, "asset_class", "") != "metals":
        return _rej(aid, "classe d'actif non métaux")
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 40:
        return _rej(aid, "historique court")
    hi, lo = session_range(closed, p.get("start", "00:00"), p.get("end", "07:00"))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return _rej(aid, "range asiatique indisponible")
    rng = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not _range_ok(rng, atr_h1, 0.6, 4.5):
        return _rej(aid, "largeur du range hors bornes")
    end_h = _hhmm(p.get("end", "07:00"), 7.0)
    h = _bar_hour(le)
    if h is None or not (end_h <= h < LONDON_CLOSE_H):
        return _rej(aid, "hors fenêtre horaire")
    spread = _spread_ratio(snap, atr)
    if spread > 0.08:
        return _rej(aid, "spread trop élevé")
    brk = _first_break(closed, hi, lo, end_h, margin=0.1 * atr)
    if brk is None:
        return _rej(aid, "pas de cassure du range")
    side, boundary, age, day = brk
    entry = float(le["close"])
    sgn = side.sign
    ext = sgn * (entry - boundary)
    if ext <= 0:
        return _rej(aid, "cassure non tenue")
    if age > 32:
        return _rej(aid, "cassure trop ancienne")
    today = _today(closed)
    c4 = closed["close"].iloc[-4:].to_numpy(dtype=float)
    impulse = bool(sgn * (c4[-1] - c4[0]) >= 0.3 * atr)
    rsi_ok = bool(le["rsi14"] > 50) if side is Side.BUY else bool(le["rsi14"] < 50)
    extreme = bool(float(le["high"]) >= float(today["high"].max()) - 0.5 * atr) if side is Side.BUY \
        else bool(float(le["low"]) <= float(today["low"].min()) + 0.5 * atr)
    conf = int(impulse) + int(rsi_ok) + int(extreme)
    if conf < 2:
        return _rej(aid, "moins de deux confluences sur trois")
    tr = _trend_of(lt)
    adx_ok = bool(_valid(lt, "adx14") and lt["adx14"] >= 18)
    if (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP"):
        return _rej(aid, "tendance H1 opposée")
    if not (adx_ok or tr == ("UP" if side is Side.BUY else "DOWN")):
        return _rej(aid, "tendance H1 non porteuse")
    if ext > min(3.0 * atr, max(1.5 * atr, 1.0 * rng)):
        return _rej(aid, "extension trop grande")
    sl = _structure_sl(e, side, entry, atr, float(p.get("sl_atr", 1.2)))
    # le swing M15 peut être très loin : plafond ATR explicite, sinon le SL sort des bornes du module
    sl = max(sl, entry - 2.8 * atr) if side is Side.BUY else min(sl, entry + 2.8 * atr)
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0 + 8.0 * conf
    pros = [f"cassure du range asiatique {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1) sur l'or, tenue depuis {age + 1} barre(s)",
            f"{conf}/3 confluences (impulsion {impulse}, RSI {le['rsi14']:.0f}, extrême du jour {extreme})"]
    cons: list[str] = []
    if _valid(lt, "adx14") and lt["adx14"] >= 25:
        score += 10
        pros.append(f"ADX H1 {lt['adx14']:.0f}")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if spread <= 0.04 and ext <= 0.4 * atr:
        score += 10
        pros.append("exécution favorable (spread faible, entrée proche de la borne)")
    elif ext > 0.6 * atr:
        cons.append(f"extension {ext / atr:.2f} ATR : entrée tardive")
    targets: list[float] = []
    h4 = snap.frames.get("H4")
    if h4 is not None and len(h4) >= 20:
        sh4, sl4 = swing_points(h4.iloc[:-1])
        swings = [v for _, v in (sh4 if side is Side.BUY else sl4)]
        targets = _nearest_beyond(swings, side, entry, 0.5 * dist)
    if not targets:
        score -= 10
        cons.append("aucun swing H4 comme cible : multiples de R")
    cons.append("or : chasses de stops fréquentes autour des bornes asiatiques")
    inv = f"clôture H1 de retour {'sous' if side is Side.BUY else 'au-dessus de'} la borne {boundary:.5g} du range asiatique"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.0))


# ================================================================ C09 — opening range breakout sur indices
@register("C09")
def strategy_c09(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """C09 — index_open_breakout (M15 / H1, indices, sessions NEWYORK & OVERLAP).

    Thèse : sur les indices, l'opening range de la première heure du cash US (13:30-14:30 UTC) est le
    référentiel de la journée ; sa cassure sur volume, dans le sens du gap d'ouverture, tend à se prolonger
    (« gap and go »).

    Entrée : première clôture M15 du jour au-delà de l'opening range d'au moins 0,1 ATR, au plus 14 barres
    clôturées plus tôt ; la dernière barre clôturée est toujours au moins 0,1 ATR au-delà de la borne
    (cassure tenue) — une seconde clôture consécutive au-delà de la borne vaut un bonus de score.
    Confirmation : tick_volume ≥ 0,9 × volume moyen des barres de l'opening range, sur la bougie de cassure OU
    sur la bougie d'entrée (la participation peut arriver sur l'une ou l'autre).
    Filtres : classe d'actif « indices » ; largeur de l'OR entre 0,25 et 2,5 ATR H1 ; heure de la barre dans
    [fin de l'OR, fin + 3,5 h) ; extension ≤ min(3 ATR ; max(1,5 ATR ; 1 × hauteur de l'OR)) — jamais au-delà du mouvement
    mesuré (et jamais plus de 3 ATR).
    SL : cascade — borne opposée de l'opening range − 0,1 ATR ; si cela dépasse 2,2 ATR, milieu de l'OR − 0,1 ATR ;
    si c'est encore trop loin, juste derrière la borne cassée (− 0,4 ATR), le tout plafonné à 2,5 ATR de l'entrée.
    TP : 1,5 R partiel, projections de la hauteur de l'OR (× 1 et × 2 depuis la borne cassée), final
    max(rr × R, borne + 2 × OR).
    Invalidation : clôture M15 de retour dans l'opening range.
    Score (0-100, somme documentée, PAS une probabilité) = 30 (ORB) + 10 seconde clôture consécutive au-delà
    de la borne + 10 volume ≥ 1,5 × + 10 gap d'ouverture (D1) aligné − 5 cassure contre le gap + 15 tendance
    H1 alignée + 5 OR compact (≤ 1 ATR H1) + 5 extension ≤ 0,3 ATR + 5 bougie d'entrée dans le sens de la
    cassure ; `_build` retranche la pénalité de spread.
    """
    aid = spec.agent_id
    if snap.spec is None or getattr(snap.spec, "asset_class", "") != "indices":
        return _rej(aid, "classe d'actif non indices")
    c = _ctx(spec, snap)
    if not c:
        return _rej(aid, "contexte insuffisant")
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    if len(closed) < 40 or "tick_volume" not in closed.columns:
        return _rej(aid, "historique court ou volume absent")
    start_h, end_h = _hhmm(p.get("start", "13:30"), 13.5), _hhmm(p.get("end", "14:30"), 14.5)
    hi, lo = session_range(closed, p.get("start", "13:30"), p.get("end", "14:30"))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return _rej(aid, "opening range indisponible")
    rng = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not _range_ok(rng, atr_h1, 0.25, 2.5):
        return _rej(aid, "largeur de l'OR hors bornes")
    h = _bar_hour(le)
    if h is None or not (end_h <= h < end_h + 3.5):
        return _rej(aid, "hors fenêtre horaire")
    or_bars = _bars_between(closed, start_h, end_h)
    if or_bars.empty:
        return _rej(aid, "barres de l'OR absentes")
    or_vol = float(or_bars["tick_volume"].mean())
    brk = _first_break(closed, hi, lo, end_h, margin=0.1 * atr)
    if brk is None:
        return _rej(aid, "pas de cassure de l'OR")
    side, boundary, age, day = brk
    if age > 14:
        return _rej(aid, "cassure trop ancienne")
    entry = float(le["close"])
    sgn = side.sign
    prev = closed.iloc[-2]
    ext = sgn * (entry - boundary)
    if ext < 0.1 * atr:
        return _rej(aid, "cassure non tenue")
    second = bool(sgn * (float(prev["close"]) - boundary) > 0)   # seconde clôture au-delà de la borne
    bk = day.iloc[len(day) - 1 - age]
    vols = [float(x) for x in (bk.get("tick_volume"), le.get("tick_volume")) if pd.notna(x)]
    if not vols or or_vol <= 0:
        return _rej(aid, "volume indisponible")
    vol = max(vols)
    if vol < 0.9 * or_vol:
        return _rej(aid, "volume insuffisant")
    if ext > min(3.0 * atr, max(1.5 * atr, 1.0 * rng)):
        return _rej(aid, "extension trop grande")
    mid = (hi + lo) / 2.0
    # SL en cascade : borne opposée de l'OR, sinon milieu de l'OR, sinon juste derrière la borne cassée
    sl = (lo - 0.1 * atr) if side is Side.BUY else (hi + 0.1 * atr)
    if abs(entry - sl) > 2.2 * atr:
        sl = (mid - 0.1 * atr) if side is Side.BUY else (mid + 0.1 * atr)
    if abs(entry - sl) > 2.2 * atr:
        sl = (boundary - 0.4 * atr) if side is Side.BUY else (boundary + 0.4 * atr)
    sl = max(sl, entry - 2.5 * atr) if side is Side.BUY else min(sl, entry + 2.5 * atr)
    dist = abs(entry - sl)
    if not _dist_ok(dist, atr, snap):
        return _rej(aid, "distance SL hors bornes")
    score = 30.0
    pros = [f"cassure de l'opening range {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1) sur volume ×{vol / or_vol:.1f}",
            f"clôture M15 au-delà de la borne, cassure tenue depuis {age + 1} barre(s)"]
    cons: list[str] = []
    if second:
        score += 10
        pros.append("deux clôtures consécutives au-delà de la borne")
    else:
        cons.append("une seule clôture au-delà de la borne pour l'instant")
    if vol >= 1.5 * or_vol:
        score += 10
    d1 = snap.frames.get("D1")
    if d1 is not None and len(d1) >= 2:
        prev_close = float(last_closed(d1)["close"])
        if np.isfinite(prev_close):
            gap_up, gap_down = lo > prev_close, hi < prev_close
            if (side is Side.BUY and gap_up) or (side is Side.SELL and gap_down):
                score += 10
                pros.append("gap d'ouverture dans le sens de la cassure (gap and go)")
            elif (side is Side.BUY and gap_down) or (side is Side.SELL and gap_up):
                score -= 5
                cons.append("cassure contre le gap d'ouverture (comblement possible)")
    else:
        cons.append("gap d'ouverture inconnu (D1 indisponible)")
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if rng <= 1.0 * atr_h1:
        score += 5
        pros.append("opening range compact")
    if ext <= 0.3 * atr:
        score += 5
    else:
        cons.append(f"extension {ext / atr:.2f} ATR hors de l'OR")
    if sgn * (entry - float(le["open"])) > 0:
        score += 5
    else:
        cons.append("bougie d'entrée contre le sens de la cassure")
    cons.append("ORB : les faux départs de la première heure sont fréquents")
    inv = f"clôture M15 de retour dans l'opening range ({lo:.5g}-{hi:.5g})"
    cand = _build(spec, snap, side, entry, sl, p.get("rr", 2.0), _clamp(score), pros, cons, inv, bt)
    targets = [boundary + side.sign * rng, boundary + side.sign * 2.0 * rng]
    return _with_tp_plan(cand, side, entry, dist, targets, p.get("rr", 2.0))
