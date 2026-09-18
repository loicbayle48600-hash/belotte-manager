"""Famille E — retour à la moyenne et retournements : une stratégie propre par agent (E01 … E07).

Thème commun : le prix s'est éloigné d'une référence (bornes d'un range, moyenne de Bollinger, EMA50) ou a piégé
des participants (faux breakout, balayage de liquidité, rejet d'un niveau testé) ; on cherche le retour vers la
référence, jamais la poursuite du mouvement. Chaque agent a sa propre définition de l'excès, sa propre
confirmation, ses filtres, sa logique de SL, son plan de TP et sa règle d'invalidation : une simple différence de
paramètres ne suffit pas (et aucun agent ne reproduit le screener générique de repli de `AgentSpec.base_strategy`).

Conventions communes (voir `agents/screeners.py`, modules `b_trend`, `c_breakout`, `d_pullback`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est la
  barre en formation et n'est JAMAIS lue ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14 du
  tf d'entrée, horodatage de la barre clôturée) ;
- `_build` construit le `TradeCandidate` (pénalité de spread) et refuse tout SL du mauvais côté ; les agents de ce
  module remplacent ensuite le plan de TP générique par leur propre plan (`_set_tp_plan`, `rr >= 1.5` garanti) ;
- le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (ATR H1, stops_level du symbole) : un SL refusé
  donne `None`, jamais une valeur corrigée à la volée ;
- `setup_score` = somme documentée de composantes (excès mesuré, confirmation, contexte, volatilité, qualité
  d'exécution), bornée 0-100 : ce n'est PAS une probabilité de gain ;
- données insuffisantes ou indicateur NaN → `None`, jamais une valeur inventée.

Avertissement de famille : le contre-tendance a un taux de réussite historiquement variable ; chaque agent porte
l'argument contre correspondant et refuse d'entrer quand le timeframe supérieur est franchement opposé.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import support_resistance, swing_points
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# Bornes de distance entrée→SL en ATR du tf d'entrée (le gate impose 0,25-4 ATR H1 ; on reste plus strict)
SL_MIN_ATR = 0.3
SL_MAX_ATR = 3.0
# Pour un tf d'entrée court (M15), l'ATR du tf d'entrée est bien plus petit que l'ATR H1 : la distance minimale
# tient aussi compte de l'ATR H1 afin de ne jamais proposer un stop que le gate d'exécution refuserait
SL_MIN_H1_ATR = 0.3


# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------
def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


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


def _spread_ratio_h1(snap) -> float:
    """Spread courant en fraction de l'ATR H1 (référence du gate : <= 0,15 ATR H1) ; inf si l'ATR H1 est inconnu."""
    atr_h1 = float(snap.atr_h1 or 0.0)
    if atr_h1 <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr_h1)


def _opposed(lt: pd.Series, side: Side) -> bool:
    """Vrai si la tendance EMA du timeframe supérieur est franchement opposée au trade envisagé."""
    tr = _trend_of(lt)
    return (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP")


def _htf_veto(lt: pd.Series, side: Side, adx_max: float) -> bool:
    """Veto du timeframe supérieur : tendance opposée ET ADX >= `adx_max` (retournement trop coûteux)."""
    if not _opposed(lt, side):
        return False
    return (not _valid(lt, "adx14")) or float(lt["adx14"]) >= adx_max


def _bound_sl(snap, side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> Optional[float]:
    """Ramène la distance entrée→SL dans [max(lo·ATR, 0,3·ATR, 0,3·ATR H1) ; min(hi, 3)·ATR] sans changer de côté.

    Renvoie None si la borne basse dépasse la borne haute (configuration incohérente : on refuse plutôt que
    d'inventer un stop).
    """
    if not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
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

    - `targets` : cibles structurelles (bornes de range, moyenne mobile, niveau opposé) ; seules celles situées à
      >= 0,5 R dans le sens du trade sont retenues ;
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


def _touch_count(df: pd.DataFrame, level: float, tol: float, side: Side) -> int:
    """Nombre de barres dont l'extrême pertinent (bas pour un support, haut pour une résistance) entre dans
    [niveau − tol, niveau + tol] : mesure de l'intérêt porté au niveau."""
    if tol <= 0 or not np.isfinite(level) or df.empty:
        return 0
    col = "low" if side is Side.BUY else "high"
    return int(((df[col] - level).abs() <= tol).sum())


def _mean_range(df: pd.DataFrame) -> float:
    """Amplitude moyenne (high − low) des barres fournies ; 0 si indisponible."""
    if df.empty:
        return 0.0
    v = float((df["high"] - df["low"]).mean())
    return v if np.isfinite(v) and v > 0 else 0.0


def _vol_ratio(df: pd.DataFrame, row: pd.Series) -> Optional[float]:
    """Volume de la barre rapporté au volume moyen des barres de référence ; None si le volume est indisponible."""
    if "tick_volume" not in df.columns or "tick_volume" not in row:
        return None
    ref = float(df["tick_volume"].mean()) if not df.empty else 0.0
    v = float(row["tick_volume"]) if pd.notna(row["tick_volume"]) else float("nan")
    if not np.isfinite(ref) or ref <= 0 or not np.isfinite(v):
        return None
    return v / ref


# --------------------------------------------------------------------------------------------------------------
# E01 — range_mean_reversion (M15 / H1) — repli sur la borne d'un range horizontal identifié
# --------------------------------------------------------------------------------------------------------------
@register("E01")
def strategy_e01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E01 — Achat (vente) de la borne basse (haute) d'un range horizontal VALIDÉ par ses touches.

    Thèse : un range n'est tradable que s'il a déjà été respecté. On mesure un couloir horizontal sur 40 barres
    M15 clôturées, on exige au moins deux touches de CHAQUE borne (le couloir contient réellement le prix), puis
    on achète la borne basse (vend la borne haute) quand une barre y entre et clôture en revenant dans le range.
    Le référentiel est le couloir lui-même, pas une bande statistique (E04) ni un niveau isolé (E07).

    Entrée : sur les 40 barres clôturées précédant la barre de signal, couloir [lo, hi] de largeur comprise entre
    1 et 6 ATR M15, avec >= 2 barres touchant chaque borne (zone de 15 % de la largeur) ; la barre de signal entre
    dans la zone de borne (low <= lo + 20 % de largeur pour un achat), clôture au-dessus de lo + 10 % de largeur,
    dans la moitié basse du range, et dans le sens du trade (clôture > ouverture).
    Confirmation : RSI14 M15 <= `rsi_lo` + 15 (miroir en vente) et clôture dans les 50 % supérieurs de sa propre
    amplitude (le vendeur n'a pas gardé la main sur la barre).
    Filtres : ADX14 M15 <= 28 (pas de range en train de se rompre) ; veto si la tendance H1 est opposée avec
    ADX H1 >= 28 ; spread <= 12 % de l'ATR H1 ; borne opposée à moins de 1,5 R → refus (le range ne paie pas).
    SL : sous (au-dessus) la borne du range − 0,35 ATR, distance bornée à [0,4 ATR ; min(3 ; 2,5 × `sl_atr`) ATR].
    TP : premier TP à 1 R (prise partielle), médiane du range, puis borne opposée (à 90 % de la largeur) ;
    cible finale = max(`rr` R, borne opposée).
    Invalidation : clôture M15 au-delà de la borne du range (le couloir est cassé).
    Score : range validé 25 + touches 0-10 + position dans le range 0-10 + RSI 0-10 + rejet de la borne 0-10
    + ADX bas 0-10 + H1 non opposée 10 − 10 si H1 opposée.
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
    win = closed.iloc[-41:-1]                      # 40 barres clôturées AVANT la barre de signal
    if len(win) < 40 or win[["high", "low"]].isna().any().any():
        return None
    hi, lo = float(win["high"].max()), float(win["low"].min())
    width = hi - lo
    if not np.isfinite(width) or not (1.0 * atr <= width <= 6.0 * atr):
        return None
    edge = 0.15 * width
    n_top = int((win["high"] >= hi - edge).sum())
    n_bot = int((win["low"] <= lo + edge).sum())
    if n_top < 2 or n_bot < 2:
        return None
    mid = (hi + lo) / 2.0
    entry = float(le["close"])
    zone = 0.20 * width
    rsi_lo, rsi_hi = float(p.get("rsi_lo", 30)), float(p.get("rsi_hi", 70))
    if float(le["low"]) <= lo + zone and entry > lo + 0.10 * width and entry < mid and le["close"] > le["open"]:
        side, boundary, opposite = Side.BUY, lo, hi - 0.10 * width
        rsi_ok = float(le["rsi14"]) <= rsi_lo + 15.0
    elif float(le["high"]) >= hi - zone and entry < hi - 0.10 * width and entry > mid and le["close"] < le["open"]:
        side, boundary, opposite = Side.SELL, hi, lo + 0.10 * width
        rsi_ok = float(le["rsi14"]) >= rsi_hi - 15.0
    else:
        return None
    if not rsi_ok or _close_pos(le, side) < 0.5:
        return None
    if float(le["adx14"]) > 28.0:
        return None
    if _htf_veto(lt, side, 28.0):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    sl_atr = float(p.get("sl_atr", 0.8))
    sl = _bound_sl(snap, side, entry, boundary - side.sign * 0.35 * atr, atr, 0.4, min(3.0, 2.5 * sl_atr))
    if sl is None:
        return None
    dist = abs(entry - sl)
    if side.sign * (opposite - entry) < 1.5 * dist:
        return None                                # la traversée du range ne paie pas le risque
    depth = side.sign * (entry - boundary) / width  # position dans le range depuis la borne travaillée
    score = 25.0
    score += _clamp((min(n_top, n_bot) - 2) * 3.0, 0, 10)
    score += _clamp((0.25 - depth) * 60, 0, 10)     # entrée collée à la borne = meilleure
    score += _clamp(abs(50.0 - float(le["rsi14"])) - 10.0, 0, 10)
    score += _clamp(_wick_against(le, side) * 25, 0, 10)
    score += _clamp((28.0 - float(le["adx14"])) * 0.8, 0, 10)
    pros = [f"range horizontal {lo:.5g}-{hi:.5g} ({width / atr:.1f} ATR) validé par {n_bot}/{n_top} touches",
            f"entrée sur la borne {'basse' if side is Side.BUY else 'haute'} avec clôture de retour dans le range",
            f"RSI {le['rsi14']:.0f}", f"ADX M15 {le['adx14']:.0f} (pas de rupture en cours)"]
    cons = ["mean reversion : un range finit toujours par se rompre"]
    if _opposed(lt, side):
        score -= 10
        cons.append(f"tendance {spec.timeframes['trend']} opposée : le range peut être en train de se rompre")
    else:
        score += 10
        pros.append(f"tendance {spec.timeframes['trend']} non opposée")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.5)), _clamp(score), pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà de la borne {boundary:.5g} du range", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [mid, opposite], float(p.get("rr", 1.5)), first_r=1.0), snap)


# --------------------------------------------------------------------------------------------------------------
# E02 — rsi_divergence (M15 / H1) — divergence entre deux swings confirmés + cassure du sommet intermédiaire
# --------------------------------------------------------------------------------------------------------------
@register("E02")
def strategy_e02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E02 — Divergence RSI classique entre deux swings confirmés, déclenchée par une reprise, cible = sommet intermédiaire.

    Thèse : un nouveau plus bas (haut) accompagné d'un RSI qui ne confirme pas signale un essoufflement du
    mouvement. La divergence seule ne suffit pas : on n'entre qu'après une barre de reprise qui dépasse le plus
    haut (bas) de la barre précédente, et l'objectif naturel est le sommet (creux) intermédiaire entre les deux
    swings, pas un multiple de R arbitraire.

    Entrée : deux swings bas (hauts) confirmés par `swing_points`, écartés de 5 à 30 barres, le second au moins
    0,15 ATR plus bas (haut) que le premier ; RSI14 du second swing supérieur d'au moins 5 points à celui du
    premier (miroir en vente) ; RSI du premier swing <= 38 (>= 62 en vente) — la divergence doit partir d'une
    zone d'excès ; second swing confirmé depuis 3 à 12 barres.
    Confirmation : histogramme MACD M15 en amélioration sur les deux dernières barres clôturées ; barre de signal
    dans le sens du trade qui clôture au-delà du plus haut (bas) de la barre précédente ET au-delà de la clôture
    du second swing.
    Filtres : ADX14 M15 <= 32 ; veto si la tendance H1 est opposée avec ADX H1 >= 30 ; spread <= 12 % de l'ATR H1 ;
    aucune clôture sous (au-dessus de) l'extrême du second swing depuis sa confirmation.
    SL : sous (au-dessus) l'extrême atteint depuis le second swing − 0,3 ATR, borné à
    [0,4 ATR ; min(3 ; 2 × `sl_atr`) ATR].
    TP : premier TP à 1,5 R, cible structurelle = sommet (creux) intermédiaire entre les deux swings, cible finale
    = max(`rr` R, sommet intermédiaire).
    Invalidation : nouveau plus bas (haut) sous (au-dessus de) l'extrême du second swing : la divergence est niée.
    Score : divergence 25 + écart RSI 0-15 + excès du premier swing 0-10 + MACD 10 + fraîcheur du swing 0-10
    + exécution 0-10 + H1 non opposée 10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or "macd_hist" not in closed.columns:
        return None
    sh, sl_ = swing_points(closed)
    entry = float(le["close"])
    prev = closed.iloc[-2]
    if not _valid(prev, "high", "low", "close"):
        return None
    pivots = None
    if len(sl_) >= 2 and le["close"] > le["open"] and entry > float(prev["high"]):
        (i1, p1), (i2, p2) = sl_[-2], sl_[-1]
        if p2 <= p1 - 0.15 * atr:
            pivots, side = (i1, p1, i2, p2), Side.BUY
    if pivots is None and len(sh) >= 2 and le["close"] < le["open"] and entry < float(prev["low"]):
        (i1, p1), (i2, p2) = sh[-2], sh[-1]
        if p2 >= p1 + 0.15 * atr:
            pivots, side = (i1, p1, i2, p2), Side.SELL
    if pivots is None:
        return None
    i1, p1, i2, p2 = pivots
    s = side.sign
    if not (5 <= i2 - i1 <= 30) or not (3 <= (n - 1) - i2 <= 12):
        return None
    r1, r2 = closed["rsi14"].iloc[i1], closed["rsi14"].iloc[i2]
    if pd.isna(r1) or pd.isna(r2) or s * (float(r2) - float(r1)) < 5.0:
        return None
    if (side is Side.BUY and float(r1) > 38.0) or (side is Side.SELL and float(r1) < 62.0):
        return None
    since = closed.iloc[i2:n - 1]                   # barres entre le second swing et la barre de signal (exclue)
    if since.empty:
        return None
    if (s * (since["close"] - p2) < 0).any():
        return None                                  # le swing a déjà été enfoncé en clôture : pas de divergence tenue
    if s * (entry - float(closed["close"].iloc[i2])) <= 0:
        return None            # la barre de signal doit dépasser la clôture du second swing
    h = closed["macd_hist"].iloc[-3:].to_numpy(dtype=float)
    if np.isnan(h).any() or s * (h[2] - h[1]) <= 0 or s * (h[1] - h[0]) <= 0:
        return None
    if float(le["adx14"]) > 32.0:
        return None
    if _htf_veto(lt, side, 30.0):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    ext = float(since["low"].min()) if side is Side.BUY else float(since["high"].max())
    ext = min(ext, float(le["low"])) if side is Side.BUY else max(ext, float(le["high"]))
    sl_atr = float(p.get("sl_atr", 1.0))
    sl = _bound_sl(snap, side, entry, ext - s * 0.3 * atr, atr, 0.4, min(3.0, 2.0 * sl_atr))
    if sl is None:
        return None
    between = closed.iloc[i1:i2 + 1]
    target = float(between["high"].max()) if side is Side.BUY else float(between["low"].min())
    score = 25.0
    score += _clamp(abs(float(r2) - float(r1)) * 1.5, 0, 15)
    score += _clamp(abs(50.0 - float(r1)) - 5.0, 0, 10)
    score += 10.0
    score += _clamp(12.0 - ((n - 1) - i2), 0, 10)
    score += _clamp(_body_ratio(le) * 10, 0, 10)
    pros = [f"divergence RSI sur 2 swings confirmés ({float(r1):.0f} → {float(r2):.0f})",
            f"prix {'plus bas' if side is Side.BUY else 'plus haut'} de {abs(p2 - p1) / atr:.2f} ATR sans confirmation du RSI",
            "histogramme MACD en amélioration sur 2 barres",
            "barre de reprise au-delà de l'extrême de la barre précédente"]
    cons = ["retournement : la divergence peut se prolonger plusieurs fois avant de payer"]
    if _opposed(lt, side):
        cons.append(f"tendance {spec.timeframes['trend']} encore opposée")
    else:
        score += 10
        pros.append(f"tendance {spec.timeframes['trend']} non opposée")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons,
                  f"nouveau {'plus bas' if side is Side.BUY else 'plus haut'} au-delà de {p2:.5g} (extrême du second swing)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [target], float(p.get("rr", 2.0)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# E03 — exhaustion_reversal (M15 / H1) — climax de volume et d'extension, bougie de rejet
# --------------------------------------------------------------------------------------------------------------
@register("E03")
def strategy_e03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E03 — Épuisement : course étirée loin de l'EMA50, climax de volume et d'amplitude, puis bougie de rejet.

    Thèse : un mouvement qui s'accélère sur une amplitude et un volume anormaux, très loin de sa moyenne, n'est
    pas une tendance saine mais une capitulation ; la barre qui fait un nouvel extrême et clôture à l'opposé de son
    amplitude marque la fin de la course. On vise le retour vers la moyenne (EMA20 / médiane de Bollinger), pas un
    retournement de tendance.

    Entrée : la barre de signal fait le nouvel extrême des 20 dernières barres clôturées ; la course des 6
    dernières barres atteint >= 2 ATR ; extension de l'extrême par rapport à l'EMA50 M15 >= 2 ATR ; RSI14 M15
    >= 100 − `rsi_ext` (<= `rsi_ext` en vente) sur la barre de signal ou la précédente.
    Confirmation : amplitude de la barre >= 1,3 × l'amplitude moyenne des 20 barres précédentes, mèche de rejet
    >= 40 % de l'amplitude, clôture dans les 45 % favorables au retournement, et volume >= 1,4 × le volume moyen
    des 20 barres précédentes (climax : sans volume exploitable, pas de signal).
    Filtres : veto si la tendance H1 est opposée avec ADX H1 >= 35 ; spread <= 12 % de l'ATR H1.
    SL : au-delà de l'extrême du climax + 0,25 ATR, borné à [0,5 ATR ; min(3 ; 2,5 × `sl_atr`) ATR] — le stop est
    derrière la mèche de capitulation, pas derrière une structure.
    TP : premier TP à 1 R, cibles de retour à la moyenne (EMA20 puis médiane de Bollinger M15) ; cible finale
    = max(`rr` R, moyenne la plus lointaine).
    Invalidation : nouvel extrême au-delà de la mèche du climax.
    Score : climax 20 + extension 0-15 + volume 0-15 + rejet 0-15 + RSI 0-10 + amplitude 0-10 − 10 si H1 opposée.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or "tick_volume" not in closed.columns or not _valid(le, "bb_mid", "ema20", "ema50"):
        return None
    ref = closed.iloc[-21:-1]                       # 20 barres clôturées avant la barre de signal
    if len(ref) < 20:
        return None
    ext_hi, ext_lo = float(ref["high"].max()), float(ref["low"].min())
    run = float(closed["close"].iloc[-1] - closed["close"].iloc[-6])
    rsi_ext = float(p.get("rsi_ext", 20))
    rsi_now, rsi_prev = float(le["rsi14"]), float(closed["rsi14"].iloc[-2])
    if pd.isna(rsi_prev):
        return None
    if float(le["high"]) > ext_hi and run >= 2.0 * atr and max(rsi_now, rsi_prev) >= 100.0 - rsi_ext:
        side, extreme = Side.SELL, float(le["high"])
    elif float(le["low"]) < ext_lo and run <= -2.0 * atr and min(rsi_now, rsi_prev) <= rsi_ext:
        side, extreme = Side.BUY, float(le["low"])
    else:
        return None
    s = side.sign
    stretch = -s * (extreme - float(le["ema50"])) / atr
    if stretch < 2.0:
        return None
    rng = _range(le)
    mean_rng = _mean_range(ref)
    if mean_rng <= 0 or rng < 1.3 * mean_rng:
        return None
    wick = _wick_against(le, side)
    if wick < 0.40 or _close_pos(le, side) < 0.45:
        return None
    vr = _vol_ratio(ref, le)
    if vr is None or vr < 1.4:
        return None
    if _htf_veto(lt, side, 35.0):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    entry = float(le["close"])
    sl_atr = float(p.get("sl_atr", 0.8))
    sl = _bound_sl(snap, side, entry, extreme - s * 0.25 * atr, atr, 0.5, min(3.0, 2.5 * sl_atr))
    if sl is None:
        return None
    score = 20.0
    score += _clamp((stretch - 2.0) * 10, 0, 15)
    score += _clamp((vr - 1.4) * 20, 0, 15)
    score += _clamp((wick - 0.40) * 50, 0, 15)
    score += _clamp(abs(50.0 - rsi_now) - 15.0, 0, 10)
    score += _clamp((rng / mean_rng - 1.3) * 20, 0, 10)
    pros = [f"climax : nouvel extrême sur 20 barres après une course de {abs(run) / atr:.1f} ATR",
            f"extension de {stretch:.1f} ATR au-delà de l'EMA50 M15", f"volume ×{vr:.1f} (capitulation)",
            f"bougie de rejet (mèche {wick:.0%}, amplitude {rng / mean_rng:.1f}× la moyenne)",
            f"RSI {rsi_now:.0f} en zone d'excès"]
    cons = ["épuisement : un climax peut être suivi d'un second climax dans le même sens"]
    if _opposed(lt, side):
        score -= 10
        cons.append(f"tendance {spec.timeframes['trend']} opposée : retour à la moyenne uniquement, pas de retournement")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.8)), _clamp(score), pros, cons,
                  f"nouvel extrême au-delà de {extreme:.5g} (mèche du climax)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [float(le["ema20"]), float(le["bb_mid"])],
                                  float(p.get("rr", 1.8)), first_r=1.0), snap)


# --------------------------------------------------------------------------------------------------------------
# E04 — bollinger_mean_reversion (M15 / H1) — excursion hors bande mesurée en écarts-types, réintégration
# --------------------------------------------------------------------------------------------------------------
@register("E04")
def strategy_e04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E04 — Réintégration des bandes de Bollinger après une excursion mesurée en écarts-types.

    Thèse : une clôture hors bande est un excès statistique ; il ne devient exploitable qu'une fois le prix
    REVENU à l'intérieur des bandes, avec des bandes suffisamment larges pour que le trajet vers la médiane paie
    le risque. Contrairement à E01 (couloir horizontal) et E03 (climax de volume), la référence ici est la
    distribution locale des clôtures : écart à la médiane en écarts-types et largeur de bande en percentile.

    Entrée : parmi les 3 barres clôturées précédant la barre de signal, au moins une clôture hors bande
    (< bb_low / > bb_up) avec un écart à la médiane >= `z_min` (2,2) écarts-types ; la barre de signal clôture de
    nouveau à l'intérieur de la bande, dans le sens du trade, au-dessus (sous) de la clôture précédente, avec
    RSI14 <= `rsi_lo` + 10 (>= `rsi_hi` − 10 en vente).
    Confirmation : histogramme MACD M15 en amélioration sur la dernière barre clôturée.
    Filtres : ADX14 M15 <= 25 (régime de retour à la moyenne) ; largeur de bande au-dessus du 30e percentile des
    100 dernières barres (une bande écrasée ne laisse aucune amplitude) ; médiane à >= 1 R du prix d'entrée ;
    veto si la tendance H1 est opposée avec ADX H1 >= 28 ; spread <= 12 % de l'ATR H1.
    SL : au-delà de l'extrême de l'excursion (mèches comprises) − 0,4 ATR, borné à
    [0,4 ATR ; min(3 ; 3 × `sl_atr`) ATR].
    TP : premier TP à 1 R, médiane de Bollinger, puis bande opposée ; cible finale = max(`rr` R, médiane).
    Invalidation : clôture M15 au-delà de l'extrême de l'excursion (la réintégration a échoué).
    Score : excursion 20 + écart-type 0-15 + largeur de bande 0-10 + RSI 0-10 + MACD 10 + ADX bas 0-10
    + exécution 0-10 + H1 non opposée 10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 120 or "macd_hist" not in closed.columns or not _valid(le, "bb_mid", "bb_up", "bb_low", "bb_width"):
        return None
    sd = (float(le["bb_up"]) - float(le["bb_low"])) / 4.0     # bandes à ±2 écarts-types
    if not np.isfinite(sd) or sd <= 0:
        return None
    exc = closed.iloc[-4:-1]                                   # 3 barres clôturées avant la barre de signal
    if len(exc) < 3 or exc[["bb_low", "bb_up", "bb_mid", "close"]].isna().any().any():
        return None
    z_min = float(p.get("z_min", 2.2))
    entry = float(le["close"])
    prev = closed.iloc[-2]
    z_dn = float(((exc["bb_mid"] - exc["close"]) / sd).max())
    z_up = float(((exc["close"] - exc["bb_mid"]) / sd).max())
    rsi_lo, rsi_hi = float(p.get("rsi_lo", 25)), float(p.get("rsi_hi", 75))
    if (exc["close"] < exc["bb_low"]).any() and z_dn >= z_min and entry > float(le["bb_low"]) \
            and le["close"] > le["open"] and entry > float(prev["close"]) and float(le["rsi14"]) <= rsi_lo + 10.0:
        side, z = Side.BUY, z_dn
        extreme = min(float(exc["low"].min()), float(le["low"]))
    elif (exc["close"] > exc["bb_up"]).any() and z_up >= z_min and entry < float(le["bb_up"]) \
            and le["close"] < le["open"] and entry < float(prev["close"]) and float(le["rsi14"]) >= rsi_hi - 10.0:
        side, z = Side.SELL, z_up
        extreme = max(float(exc["high"].max()), float(le["high"]))
    else:
        return None
    s = side.sign
    h = closed["macd_hist"].iloc[-2:].to_numpy(dtype=float)
    if np.isnan(h).any() or s * (h[1] - h[0]) <= 0:
        return None
    if float(le["adx14"]) > 25.0:
        return None
    bw = closed["bb_width"].iloc[-101:-1].dropna()
    if len(bw) < 60:
        return None
    bw_pct = float((bw <= float(le["bb_width"])).mean() * 100.0)
    if bw_pct < 30.0:
        return None
    if _htf_veto(lt, side, 28.0):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    sl_atr = float(p.get("sl_atr", 0.7))
    sl = _bound_sl(snap, side, entry, extreme - s * 0.4 * atr, atr, 0.4, min(3.0, 3.0 * sl_atr))
    if sl is None:
        return None
    dist = abs(entry - sl)
    mid = float(le["bb_mid"])
    if s * (mid - entry) < 1.0 * dist:
        return None                                            # la médiane est déjà atteinte : plus d'excès à jouer
    opposite = float(le["bb_up"]) if side is Side.BUY else float(le["bb_low"])
    score = 20.0
    score += _clamp((z - z_min) * 12, 0, 15)
    score += _clamp((bw_pct - 30.0) * 0.2, 0, 10)
    score += _clamp(abs(50.0 - float(le["rsi14"])) - 15.0, 0, 10)
    score += 10.0
    score += _clamp((25.0 - float(le["adx14"])) * 0.8, 0, 10)
    score += _clamp(_body_ratio(le) * 10, 0, 10)
    pros = [f"excursion à {z:.1f} écarts-types hors bande puis réintégration en clôture",
            f"largeur de bande au {bw_pct:.0f}e percentile (amplitude disponible)",
            f"RSI {le['rsi14']:.0f}", f"ADX M15 {le['adx14']:.0f} (retour à la moyenne)",
            "histogramme MACD en amélioration"]
    cons = ["une réintégration peut échouer : les bandes suivent le mouvement si la tendance reprend"]
    if _opposed(lt, side):
        cons.append(f"tendance {spec.timeframes['trend']} opposée : viser la médiane, pas la bande opposée")
    else:
        score += 10
        pros.append(f"tendance {spec.timeframes['trend']} non opposée")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.5)), _clamp(score), pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà de {extreme:.5g} (extrême de l'excursion)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [mid, opposite], float(p.get("rr", 1.5)), first_r=1.0), snap)


# --------------------------------------------------------------------------------------------------------------
# E05 — failed_breakout (M15 / H1) — piège de cassure d'une consolidation, réintégration en clôture
# --------------------------------------------------------------------------------------------------------------
@register("E05")
def strategy_e05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E05 — Faux breakout d'une consolidation : cassure en clôture puis réintégration rapide du couloir.

    Thèse : une cassure qui ne tient pas quelques barres piège les participants entrés dans le sens de la
    cassure ; leur sortie alimente le mouvement inverse. Le signal n'est pas la mèche (E06) mais bien une
    CLÔTURE au-delà du niveau suivie d'une CLÔTURE de retour à l'intérieur : le marché a validé puis renié la
    cassure.

    Entrée : consolidation mesurée sur les barres clôturées −36 à −7 (30 barres) : [lvl_lo, lvl_hi] de largeur
    entre 0,8 et 6 ATR, borne cassée testée au moins 2 fois ; parmi les 5 barres clôturées suivantes, au moins une
    clôture au-delà de la borne (cassure validée) ; la barre de signal clôture de nouveau à l'intérieur, au-delà de
    0,1 ATR sous (au-dessus de) la borne, et dans le sens du retournement.
    Confirmation : extension maximale au-delà de la borne <= 1,2 ATR (piège, pas départ en tendance) et retour
    en <= 4 barres après la cassure.
    Filtres : ADX14 M15 <= 30 ; veto si la tendance H1 est opposée avec ADX H1 >= 32 ; spread <= 12 % de l'ATR H1.
    SL : au-delà de l'extrême atteint pendant le piège + 0,3 ATR, borné à [0,4 ATR ; min(3 ; 2,5 × `sl_atr`) ATR].
    TP : premier TP à 1,5 R, médiane de la consolidation, puis borne opposée ; cible finale = max(`rr` R, borne
    opposée).
    Invalidation : nouvelle clôture M15 au-delà de la borne cassée (le piège se referme sur nous).
    Score : piège 25 + rapidité du retour 0-10 + extension contenue 0-10 + touches de la borne 0-10
    + volume de la réintégration 0-10 + profondeur de la réintégration 0-10 + exécution 0-10 + H1 non opposée 10.
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
    base = closed.iloc[-36:-6]                      # consolidation de référence
    brk = closed.iloc[-6:-1]                        # 5 barres clôturées où la cassure a pu avoir lieu
    if len(base) < 30 or len(brk) < 5 or base[["high", "low"]].isna().any().any():
        return None
    lvl_hi, lvl_lo = float(base["high"].max()), float(base["low"].min())
    width = lvl_hi - lvl_lo
    if not np.isfinite(width) or not (0.8 * atr <= width <= 6.0 * atr):
        return None
    entry = float(le["close"])
    up_break = [i for i in range(len(brk)) if float(brk["close"].iloc[i]) > lvl_hi]
    dn_break = [i for i in range(len(brk)) if float(brk["close"].iloc[i]) < lvl_lo]
    if up_break and entry < lvl_hi - 0.1 * atr and le["close"] < le["open"]:
        side, level, opposite, k = Side.SELL, lvl_hi, lvl_lo + 0.1 * width, up_break[0]
        after = brk.iloc[k:]
        trap_ext = float(after["high"].max())
        over = (trap_ext - lvl_hi) / atr
    elif dn_break and entry > lvl_lo + 0.1 * atr and le["close"] > le["open"]:
        side, level, opposite, k = Side.BUY, lvl_lo, lvl_hi - 0.1 * width, dn_break[0]
        after = brk.iloc[k:]
        trap_ext = float(after["low"].min())
        over = (lvl_lo - trap_ext) / atr
    else:
        return None
    s = side.sign
    bars_since = len(brk) - k                        # barres écoulées entre la cassure et la barre de signal
    if bars_since > 4 or over > 1.2 or not np.isfinite(over):
        return None
    touches = _touch_count(base, level, 0.25 * atr, side)
    if touches < 2:
        return None
    if float(le["adx14"]) > 30.0:
        return None
    if _htf_veto(lt, side, 32.0):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    sl_atr = float(p.get("sl_atr", 0.8))
    sl = _bound_sl(snap, side, entry, trap_ext - s * 0.3 * atr, atr, 0.4, min(3.0, 2.5 * sl_atr))
    if sl is None:
        return None
    reentry = s * (level - entry) / atr              # profondeur de la réintégration sous/au-dessus du niveau
    score = 25.0
    score += _clamp((5 - bars_since) * 3.0, 0, 10)
    score += _clamp((1.2 - over) * 10, 0, 10)
    score += _clamp((touches - 2) * 3.0, 0, 10)
    score += _clamp(reentry * 20, 0, 10)
    score += _clamp(_body_ratio(le) * 10, 0, 10)
    pros = [f"faux breakout de {level:.5g} : clôture au-delà puis réintégration en {bars_since} barre(s)",
            f"extension du piège limitée à {over:.2f} ATR", f"borne testée {touches} fois dans la consolidation",
            f"consolidation {lvl_lo:.5g}-{lvl_hi:.5g} ({width / atr:.1f} ATR)"]
    cons = ["un faux breakout peut se transformer en vraie cassure au second essai"]
    vr = _vol_ratio(base, le)
    if vr is None:
        cons.append("volume indisponible : conviction du retour non mesurée")
    elif vr >= 1.1:
        score += 10
        pros.append(f"volume de réintégration ×{vr:.1f}")
    else:
        cons.append(f"volume de réintégration faible (×{vr:.1f})")
    if _opposed(lt, side):
        cons.append(f"tendance {spec.timeframes['trend']} opposée")
    else:
        score += 10
        pros.append(f"tendance {spec.timeframes['trend']} non opposée")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons,
                  f"nouvelle clôture {spec.timeframes['entry']} au-delà de {level:.5g} (niveau cassé)", bt)
    mid = (lvl_hi + lvl_lo) / 2.0
    return _finalize(_set_tp_plan(cand, side, entry, [mid, opposite], float(p.get("rr", 2.0)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# E06 — liquidity_sweep_reversal (M15 / H1) — balayage d'un amas d'extrêmes égaux par une seule mèche
# --------------------------------------------------------------------------------------------------------------
@register("E06")
def strategy_e06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E06 — Balayage de liquidité : une mèche prend les stops sous un amas de plus bas (hauts) égaux, puis rejet.

    Thèse : les stops s'accumulent sous des plus bas quasi identiques ; leur déclenchement par une seule bougie
    qui referme au-dessus de l'amas est une prise de liquidité, pas une cassure. Le signal est purement
    intra-barre (mèche + clôture), là où E05 exige une clôture au-delà du niveau puis un retour.

    Entrée : au moins deux plus bas (hauts) confirmés par `swing_points` dans les 60 dernières barres clôturées,
    distants de moins de 0,3 ATR entre eux (amas de liquidité) et non balayés depuis leur formation ; la barre de
    signal descend sous l'amas d'au moins 0,05 ATR et clôture au-dessus de l'amas + 0,05 ATR.
    Confirmation : mèche de balayage >= 45 % de l'amplitude de la barre, clôture dans les 55 % favorables,
    amplitude de la barre <= 2,5 ATR (balayage, pas barre de panique) et >= 0,5 ATR (mouvement réel).
    Filtres : heure de la barre dans [6 ; 21) UTC (hors rollover et Asie creuse) ; ADX14 M15 <= 32 ; veto si la
    tendance H1 est opposée avec ADX H1 >= 30 ; spread <= 10 % de l'ATR H1 (stop serré : le coût compte double).
    SL : sous (au-dessus) l'extrême de la mèche de balayage − 0,15 ATR (stop serré collé au balayage), borné à
    [0,35 ATR ; min(3 ; 2 × `sl_atr`) ATR].
    TP : premier TP à 1,5 R, cible = amas de liquidité opposé (dernier swing haut (bas) confirmé), cible finale
    = max(`rr` R, amas opposé).
    Invalidation : clôture M15 sous (au-dessus de) l'extrême de la mèche de balayage.
    Score : balayage 25 + qualité de l'amas 0-10 + mèche 0-15 + clôture 0-10 + fraîcheur de l'amas 0-10
    + amplitude maîtrisée 0-10 + H1 non opposée 10.
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
    sh, sl_ = swing_points(closed)
    lows = [(i, v) for i, v in sl_ if i >= n - 60]
    highs = [(i, v) for i, v in sh if i >= n - 60]
    entry = float(le["close"])
    tol = 0.30 * atr
    pool = None
    if lows:
        base = min(v for _, v in lows)
        grp = [(i, v) for i, v in lows if v - base <= tol]
        if len(grp) >= 2:
            lvl = float(np.mean([v for _, v in grp]))
            if float(le["low"]) < lvl - 0.05 * atr and entry > lvl + 0.05 * atr:
                pool = (Side.BUY, lvl, max(i for i, _ in grp), len(grp))
    if pool is None and highs:
        base = max(v for _, v in highs)
        grp = [(i, v) for i, v in highs if base - v <= tol]
        if len(grp) >= 2:
            lvl = float(np.mean([v for _, v in grp]))
            if float(le["high"]) > lvl + 0.05 * atr and entry < lvl - 0.05 * atr:
                pool = (Side.SELL, lvl, max(i for i, _ in grp), len(grp))
    if pool is None:
        return None
    side, lvl, last_i, count = pool
    s = side.sign
    mid = closed.iloc[last_i + 1:n - 1]              # barres entre le dernier pivot de l'amas et la barre de signal
    if not mid.empty and (s * (mid["low" if side is Side.BUY else "high"] - lvl) < -0.05 * atr).any():
        return None                                  # l'amas a déjà été balayé : la liquidité n'est plus là
    rng = _range(le)
    if rng < 0.5 * atr or rng > 2.5 * atr:
        return None
    wick = _wick_against(le, side)
    if wick < 0.45 or _close_pos(le, side) < 0.55:
        return None
    hour = _bar_hour(le)
    if hour is None or not (6 <= hour < 21):
        return None
    if float(le["adx14"]) > 32.0:
        return None
    if _htf_veto(lt, side, 30.0):
        return None
    if _spread_ratio_h1(snap) > 0.10:
        return None
    sweep = float(le["low"]) if side is Side.BUY else float(le["high"])
    sl_atr = float(p.get("sl_atr", 0.8))
    sl = _bound_sl(snap, side, entry, sweep - s * 0.15 * atr, atr, 0.35, min(3.0, 2.0 * sl_atr))
    if sl is None:
        return None
    opp = [v for i, v in (highs if side is Side.BUY else lows)]
    target = max(opp) if (side is Side.BUY and opp) else min(opp) if (side is Side.SELL and opp) else float("nan")
    age = (n - 1) - last_i
    score = 25.0
    score += _clamp((count - 2) * 5.0, 0, 10)
    score += _clamp((wick - 0.45) * 40, 0, 15)
    score += _clamp((_close_pos(le, side) - 0.55) * 30, 0, 10)
    score += _clamp(10.0 - max(0.0, age - 20) * 0.5, 0, 10)
    score += _clamp((2.5 - rng / atr) * 6, 0, 10)
    pros = [f"amas de {count} extrêmes égaux vers {lvl:.5g} balayé par une seule mèche",
            f"mèche de balayage {wick:.0%} de l'amplitude, clôture rendue à l'intérieur",
            f"barre de {rng / atr:.1f} ATR à {hour:02d}h UTC (heures liquides)"]
    cons = ["balayage : la reprise doit être immédiate, sinon le niveau devient résistance/support"]
    if _opposed(lt, side):
        cons.append(f"tendance {spec.timeframes['trend']} opposée")
    else:
        score += 10
        pros.append(f"tendance {spec.timeframes['trend']} non opposée")
    if not np.isfinite(target):
        cons.append("amas opposé introuvable : cible en multiples de R")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà de {sweep:.5g} (extrême du balayage)", bt)
    targets = [target] if np.isfinite(target) else []
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0)), first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# E07 — sr_rejection_reversal (M15 / H1) — rejet d'un niveau S/R historisé, avec espace jusqu'au niveau suivant
# --------------------------------------------------------------------------------------------------------------
@register("E07")
def strategy_e07(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """E07 — Rejet d'un niveau S/R déjà testé plusieurs fois, avec confluence H1 et espace jusqu'au niveau suivant.

    Thèse : tous les niveaux ne se valent pas. Un niveau issu de `support_resistance` qui a déjà été touché
    plusieurs fois et qui coïncide avec un niveau H1 est défendu ; son rejet par une mèche se trade à condition
    que le niveau suivant dans le sens du trade laisse de la place (sinon le rendement ne paie pas le risque).

    Entrée : niveau S/R M15 (`support_resistance` sur les barres clôturées) distant de moins de `tol_atr` ATR de
    l'extrême de la barre de signal, du bon côté de la clôture ; niveau touché >= 3 fois sur les 100 dernières
    barres clôturées (barre de signal comprise).
    Confirmation : mèche de rejet >= 40 % de l'amplitude, clôture dans le sens du trade et au-delà du niveau de
    0,1 ATR au moins, RSI14 M15 <= 45 (>= 55 en vente).
    Filtres : niveau S/R H1 à moins de 0,5 ATR H1 = bonus de confluence ; ADX14 M15 <= 30 ; veto si la tendance
    H1 est opposée avec ADX H1 >= 30 ; spread <= 12 % de l'ATR H1 ; niveau suivant dans le sens du trade à moins
    de 1,5 R → refus (pas d'espace).
    SL : au-delà du plus extrême entre le niveau et la mèche de rejet − 0,35 ATR, borné à
    [0,4 ATR ; min(3 ; 3 × `sl_atr`) ATR].
    TP : premier TP à 1,5 R, cible = niveau S/R suivant dans le sens du trade (moins 0,1 ATR de marge), cible
    finale = max(`rr` R, niveau suivant).
    Invalidation : clôture M15 au-delà du niveau de plus de 0,3 ATR (le niveau a cédé).
    Score : niveau historisé 20 + touches 0-15 + confluence H1 0-10 + mèche 0-15 + RSI 0-10 + espace 0-10
    + H1 non opposée 10.
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
    levels = support_resistance(closed)
    if not levels:
        return None
    tol = float(p.get("tol_atr", 0.3)) * atr
    if tol <= 0:
        return None
    entry = float(le["close"])
    rsi = float(le["rsi14"])
    if le["close"] > le["open"] and rsi <= 45.0:
        side = Side.BUY
        near = [lv for lv in levels if abs(float(le["low"]) - lv) <= tol and lv <= entry - 0.1 * atr]
        lvl = max(near) if near else None
    elif le["close"] < le["open"] and rsi >= 55.0:
        side = Side.SELL
        near = [lv for lv in levels if abs(float(le["high"]) - lv) <= tol and lv >= entry + 0.1 * atr]
        lvl = min(near) if near else None
    else:
        return None
    if lvl is None:
        return None
    s = side.sign
    hist = closed.iloc[-100:]
    touches = _touch_count(hist, lvl, tol, side)
    if touches < 3:
        return None
    wick = _wick_against(le, side)
    if wick < 0.40:
        return None
    if float(le["adx14"]) > 30.0:
        return None
    if _htf_veto(lt, side, 30.0):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    extreme = float(le["low"]) if side is Side.BUY else float(le["high"])
    anchor = min(extreme, lvl) if side is Side.BUY else max(extreme, lvl)
    sl_atr = float(p.get("sl_atr", 0.8))
    sl = _bound_sl(snap, side, entry, anchor - s * 0.35 * atr, atr, 0.4, min(3.0, 3.0 * sl_atr))
    if sl is None:
        return None
    dist = abs(entry - sl)
    ahead = [lv for lv in levels if s * (lv - entry) >= 0.3 * atr]
    target = (min(ahead) if side is Side.BUY else max(ahead)) - s * 0.1 * atr if ahead else float("nan")
    if not np.isfinite(target) or s * (target - entry) < 1.5 * dist:
        return None                                  # pas d'espace jusqu'au niveau suivant : le trade ne paie pas
    atr_h1 = float(snap.atr_h1 or 0.0)
    h1_levels = support_resistance(_closed(t)) if t is not None else []
    confluence = bool(atr_h1 > 0 and any(abs(lv - lvl) <= 0.5 * atr_h1 for lv in h1_levels))
    score = 20.0
    score += _clamp((touches - 3) * 4.0, 0, 15)
    score += _clamp((wick - 0.40) * 40, 0, 15)
    score += _clamp(abs(50.0 - rsi) - 5.0, 0, 10)
    score += _clamp((s * (target - entry) / dist - 1.5) * 8, 0, 10)
    pros = [f"niveau {'support' if side is Side.BUY else 'résistance'} {lvl:.5g} touché {touches} fois sur 100 barres",
            f"mèche de rejet {wick:.0%} de l'amplitude", f"RSI {rsi:.0f}",
            f"niveau suivant à {s * (target - entry) / dist:.1f} R ({target:.5g})"]
    cons = ["un niveau testé de nombreuses fois finit souvent par céder"]
    if confluence:
        score += 10
        pros.append(f"confluence avec un niveau {spec.timeframes['trend']} (< 0,5 ATR H1)")
    else:
        cons.append(f"aucune confluence {spec.timeframes['trend']} : niveau purement intrajournalier")
    if _opposed(lt, side):
        cons.append(f"tendance {spec.timeframes['trend']} opposée")
    else:
        score += 10
        pros.append(f"tendance {spec.timeframes['trend']} non opposée")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.8)), _clamp(score), pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà de {lvl:.5g} de plus de 0,3 ATR", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [target], float(p.get("rr", 1.8)), first_r=1.5), snap)
