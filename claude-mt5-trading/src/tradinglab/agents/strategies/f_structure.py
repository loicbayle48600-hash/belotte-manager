"""Famille F — structure de marché et price action : une stratégie propre par agent (F01 … F06).

Thème commun : la décision naît de la SÉQUENCE DES SWINGS confirmés (HH-HL / LH-LL), de la cassure de cette
séquence (BOS), de son changement de caractère (CHoCH), du rejet d'une zone de swing testée plusieurs fois et de
l'alignement des structures M15 / H1. Aucun agent de ce module ne décide à partir d'un simple croisement d'EMA
(famille B), d'un range de session ou d'un canal de volatilité (famille C), ni d'un repli vers une zone de valeur
EMA / Fibonacci (famille D) : le référentiel est la structure elle-même.

Conventions communes (voir `agents/screeners.py`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est la
  barre en formation et n'est JAMAIS lue (ni son close, ni son high/low, ni son volume) ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14 du
  tf d'entrée, horodatage de la barre clôturée) ;
- `swing_points` ne publie un pivot qu'une fois `right` barres écoulées après lui : un pivot déjà confirmé ne
  change jamais quand de nouvelles barres arrivent (pas de lookahead structurel) ;
- `_build` construit le `TradeCandidate` (pénalité de spread) et refuse tout SL du mauvais côté ; les agents de ce
  module remplacent ensuite le plan de TP générique par leur propre plan (`_set_tp_plan`, `rr >= 1.5` garanti) ;
- le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (ATR H1, stops_level du symbole) : un SL refusé
  donne `None`, jamais une valeur corrigée à la volée ;
- `setup_score` = somme documentée de composantes (structure, fraîcheur du signal, alignement MTF, anatomie de la
  bougie, place jusqu'à la cible), bornée 0-100 : ce n'est PAS une probabilité de gain ;
- données insuffisantes, prix OHLC ou indicateur NaN → `None`, jamais une valeur inventée : `_ctx` ne contrôle
  que les indicateurs, `_bar_ok` contrôle les prix de la barre décisive (une comparaison avec NaN est fausse,
  donc un filtre de bougie ne rejetterait rien sur une barre incomplète).

Chaque agent a sa propre confirmation, ses propres filtres, sa propre logique de SL, son propre plan de TP et sa
propre règle d'invalidation (une simple différence de paramètres ne suffit pas).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import structure_label, swing_points
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# Bornes de distance entrée→SL en ATR du tf d'entrée (le gate impose 0,25-4 ATR H1 ; on reste plus strict)
SL_MIN_ATR = 0.3
SL_MAX_ATR = 3.0
# Pour un tf d'entrée court (M15), l'ATR du tf d'entrée est bien plus petit que l'ATR H1 : la distance minimale
# tient aussi compte de l'ATR H1 afin de ne jamais proposer un stop que le gate d'exécution refuserait
SL_MIN_H1_ATR = 0.3
# Au-delà de ce multiple de R, la cible finale d'un plan est une projection structurelle lointaine :
# le `rr` reste exact mais il est signalé dans `arguments_against` (il n'est pas une promesse de gain)
FAR_TARGET_R = 4.0


# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------
def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


def _side_from_label(label: str) -> Optional[Side]:
    """Côté impliqué par une étiquette de structure : HH_HL → achat, LH_LL → vente."""
    return Side.BUY if label == "HH_HL" else Side.SELL if label == "LH_LL" else None


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


def _body_beyond(row: pd.Series, level: float, side: Side) -> float:
    """Part du CORPS de la bougie située au-delà de `level` dans le sens du trade (0 = rien, 1 = corps entier).

    Sert à distinguer une cassure portée par le corps d'une cassure portée par la seule mèche. On ne peut pas
    exiger le corps ENTIER au-delà du niveau : sur des données sans gap, la première bougie qui clôture au-delà
    d'un pivot ouvre forcément en deçà (son ouverture ≈ la clôture précédente, qui n'avait pas dépassé le pivot).
    """
    lo, hi = float(min(row["open"], row["close"])), float(max(row["open"], row["close"]))
    height = hi - lo
    if height <= 0:                                        # doji : le corps est réduit à un point
        return 1.0 if side.sign * (float(row["close"]) - level) > 0 else 0.0
    beyond = (hi - max(lo, level)) if side is Side.BUY else (min(hi, level) - lo)
    return float(_clamp(beyond / height, 0.0, 1.0))


def _spread_ratio_h1(snap) -> float:
    """Spread courant en fraction de l'ATR H1 (référence du gate) ; inf si l'ATR H1 ou la spec est inconnu."""
    atr_h1 = float(snap.atr_h1 or 0.0)
    if atr_h1 <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr_h1)


def _ohlc_ok(df: pd.DataFrame) -> bool:
    """Vrai si les colonnes OHLC de la fenêtre sont toutes exploitables (aucun NaN)."""
    if df.empty:
        return False
    return not bool(df[["open", "high", "low", "close"]].isna().any().any())


def _bar_ok(row: pd.Series) -> bool:
    """Vrai si la barre porte des prix OHLC exploitables (présents, finis, high >= low).

    À vérifier AVANT tout filtre d'anatomie de bougie : en pandas/numpy toute comparaison avec NaN est
    fausse, donc un test du type `close <= open` sur une barre incomplète ne rejette rien et laisse passer
    un signal dont la confirmation n'a jamais été vérifiée (règle du projet : NaN → None, jamais de valeur
    supposée). `_ctx` ne contrôle que les colonnes d'indicateurs, pas les prix.
    """
    try:
        o, h, lo, c = (float(row[col]) for col in ("open", "high", "low", "close"))
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return all(np.isfinite(v) for v in (o, h, lo, c)) and h >= lo


def _bound_sl(snap, side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> Optional[float]:
    """Ramène la distance entrée→SL dans [max(lo·ATR, 0,3·ATR, 0,3·ATR H1) ; min(hi, 3)·ATR] sans changer de côté.

    Renvoie None si la borne basse dépasse la borne haute (configuration incohérente : on refuse plutôt que
    d'inventer un stop) ou si le SL proposé n'est pas fini.
    """
    if sl is None or not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
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
    """Remplace le plan de TP générique de `_build` par un plan structurel propre à l'agent.

    - `targets` : cibles structurelles (swings, projections de mouvement mesuré) ; seules celles situées à
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
    # honnêteté : au-delà de FAR_TARGET_R, le `rr` annoncé (lu par le gate, la revue et le classement) ne repose
    # plus sur une cible proche mais sur une projection structurelle entière ; il faut le dire, pas l'afficher seul
    if c.rr > FAR_TARGET_R:
        c.arguments_against.append(f"rr {c.rr:.1f} porté par une cible structurelle lointaine : il suppose que la "
                                   "projection se réalise entièrement, les prises partielles décident du résultat")
    return c


def _finalize(c: Optional[TradeCandidate], snap) -> Optional[TradeCandidate]:
    """Revalidation déterministe du SL (côté, stops_level, 0,25-4 ATR H1) : refus → None, jamais de correction."""
    if c is None:
        return None
    chk = validate_stop_loss(c.side, c.entry, c.sl, snap.spec, atr=float(c.atr or 0.0))
    return c if chk.ok else None


def _no_close_beyond(closed: pd.DataFrame, level: float, side: Side, lookback: int) -> bool:
    """Vrai si AUCUNE des `lookback` barres clôturées précédant la barre de signal n'a clôturé au-delà de `level`.

    Sert à n'accepter qu'une cassure NEUVE (une cassure déjà exploitée n'est plus un événement structurel).
    """
    win = closed.iloc[max(0, len(closed) - 1 - lookback):len(closed) - 1]
    if win.empty:
        return False
    if side is Side.BUY:
        return not bool((win["close"] > level).any())
    return not bool((win["close"] < level).any())


def _nearest_beyond(levels: list[float], entry: float, side: Side, min_dist: float) -> Optional[float]:
    """Niveau le plus proche au-delà de l'entrée (dans le sens du trade), à au moins `min_dist` : None si aucun."""
    cand = [float(x) for x in levels if np.isfinite(x) and side.sign * (x - entry) >= min_dist]
    if not cand:
        return None
    return min(cand, key=lambda x: side.sign * (x - entry))


def _cluster_zone(points: list[tuple[int, float]], atr: float, width: float) -> Optional[tuple[float, float, int, int]]:
    """Zone de swings répétés : plus récent groupe d'au moins 2 pivots contenus dans `width` × ATR.

    Renvoie (bas de zone, haut de zone, nombre de touches, index positionnel de la touche la plus récente).
    None si aucun groupe d'au moins deux pivots n'est assez serré.
    """
    if len(points) < 2 or atr <= 0:
        return None
    tol = width * atr
    last_i, last_p = points[-1]
    members = [(i, p) for i, p in points if abs(p - last_p) <= tol]
    if len(members) < 2:
        return None
    prices = [p for _, p in members]
    return float(min(prices)), float(max(prices)), len(members), int(max(i for i, _ in members))


# --------------------------------------------------------------------------------------------------------------
# F01 — hh_hl_continuation (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("F01")
def strategy_f01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """F01 — Continuation HH-HL : entrée sur cassure du repli APRÈS un nouveau plus bas plus haut, sous le sommet.

    Thèse : dans une structure HH-HL confirmée, l'information exploitable n'est pas la cassure du sommet (c'est le
    travail de F03) mais la FORMATION D'UN NOUVEAU PLUS BAS PLUS HAUT peu profond : tant que le repli reste au-dessus
    de la moitié de l'impulsion précédente et que le prix reprend le haut du repli, la séquence est intacte et on
    entre AVANT la cassure du sommet, avec le sommet comme première cible (ratio favorable).

    Entrée : `structure_label` du tf d'entrée = HH_HL (BUY, `direction` = UP) ou LH_LL (SELL, `direction` = DOWN) ;
    le dernier pivot confirmé est le plus bas plus haut (il est POSTÉRIEUR au dernier sommet) ; impulsion
    précédente (HL précédent → HH) >= 0,8 ATR ; retracement du repli compris entre 15 % et 65 % de cette impulsion ;
    la barre clôturée dépasse le plus haut de toutes les barres du repli (du pivot HL jusqu'à la barre précédente)
    tout en restant SOUS le sommet (au moins 0,3 ATR de marge) : on achète la reprise, pas la cassure.
    Confirmation : bougie dans le sens du trade avec clôture dans les 60 % favorables de son amplitude.
    Filtres : tendance du tf supérieur non opposée ; spread <= 15 % de l'ATR H1 ; place jusqu'au sommet >= 0,3 ATR.
    SL : derrière le pivot HL (plus bas plus haut) − 0,3 ATR — un retour sous ce pivot détruit la séquence ;
    distance bornée à [0,4 ATR ; min(3 ; `sl_atr` + 0,8) ATR].
    TP : cibles structurelles = sommet de la structure puis mouvement mesuré (pivot HL + hauteur de l'impulsion) ;
    premier TP à 1 R (prise partielle avant le sommet), cible finale = max(`rr` R, mouvement mesuré).
    Invalidation : clôture du tf d'entrée au-delà du pivot HL (le plus bas plus haut cède : structure niée).
    Score : structure 25 + repli peu profond 0-15 + impulsion 0-10 + MTF 0-15 + anatomie de bougie 0-15
    + place jusqu'au sommet 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    # prix OHLC manquants/NaN sur une barre décisive → refus : `_ctx` ne valide que les indicateurs et
    # toute comparaison avec NaN étant fausse, les filtres d'anatomie de bougie ne rejetteraient rien
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60:
        return None
    want = str(p.get("direction", "UP")).upper()
    side = Side.BUY if want == "UP" else Side.SELL if want == "DOWN" else None
    if side is None:
        return None
    sgn = side.sign
    label = "HH_HL" if side is Side.BUY else "LH_LL"
    if structure_label(closed) != label:
        return None
    sh, sl_ = swing_points(closed)
    if len(sh) < 2 or len(sl_) < 2:
        return None
    # pivot « sommet de la structure » et pivot « repli » (le plus récent doit être celui du repli)
    top_i, top = (sh[-1] if side is Side.BUY else sl_[-1])
    pull_i, pull_px = (sl_[-1] if side is Side.BUY else sh[-1])
    prev_pull = float(sl_[-2][1] if side is Side.BUY else sh[-2][1])
    if pull_i <= top_i or pull_i >= n - 1:
        return None
    leg = sgn * (float(top) - prev_pull)
    if not np.isfinite(leg) or leg < 0.8 * atr:
        return None
    retr = sgn * (float(top) - float(pull_px)) / leg
    if not (0.15 <= retr <= 0.65):
        return None
    pull = closed.iloc[pull_i:n - 1]
    if len(pull) < 2 or not _ohlc_ok(pull):
        return None
    entry = float(le["close"])
    trigger = float(pull["high"].max()) if side is Side.BUY else float(pull["low"].min())
    if sgn * (entry - trigger) <= 0:                       # le repli n'est pas repris
        return None
    room = sgn * (float(top) - entry)
    if room < 0.3 * atr:                                   # trop près du sommet : c'est une cassure, pas une reprise
        return None
    if sgn * (le["close"] - le["open"]) <= 0 or _close_pos(le, side) < 0.6:
        return None
    if _trend_of(lt) == ("DOWN" if side is Side.BUY else "UP"):
        return None
    if _spread_ratio_h1(snap) > 0.15:
        return None
    sl_atr = float(p.get("sl_atr", 1.2))
    sl = _bound_sl(snap, side, entry, float(pull_px) - sgn * 0.3 * atr, atr, 0.4, min(3.0, sl_atr + 0.8))
    if sl is None:
        return None
    # score : composantes documentées
    score = 25.0
    score += _clamp((0.65 - retr) * 30.0, 0, 15)           # repli peu profond = structure solide
    score += _clamp((leg / atr - 0.8) * 8.0, 0, 10)        # impulsion précédente ample
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    score += _clamp(_close_pos(le, side) * 15.0, 0, 15)
    score += _clamp(room / atr * 8.0, 0, 10)
    pros += [f"structure {label} confirmée sur {spec.timeframes['entry']}",
             f"nouveau pivot de repli frais ({n - 1 - pull_i} barre(s)) retraçant {retr * 100:.0f} % de l'impulsion",
             f"reprise du haut du repli ({len(pull)} barres) sans cassure du sommet",
             f"impulsion précédente de {leg / atr:.1f} ATR"]
    cons: list[str] = []
    if retr > 0.5:
        cons.append(f"repli profond ({retr * 100:.0f} % de l'impulsion) : élan entamé")
    if room < 0.8 * atr:
        cons.append(f"peu de place jusqu'au sommet ({room / atr:.1f} ATR) : premier TP proche")
    if _trend_of(lt) == "FLAT":
        cons.append(f"tendance {spec.timeframes['trend']} neutre : continuation moins soutenue")
    targets = [float(top), float(pull_px) + sgn * leg]
    rr = float(p.get("rr", 2.0))
    cand = _build(spec, snap, side, entry, sl, rr, score, pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà du pivot de repli ({pull_px:.5f})", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, rr, first_r=1.0), snap)


# --------------------------------------------------------------------------------------------------------------
# F02 — lh_ll_continuation (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("F02")
def strategy_f02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """F02 — Continuation LH-LL par inversion de polarité : l'ancien support cassé rejette le prix.

    Thèse : dans une structure LH-LL, la continuation la plus lisible n'est pas la cassure suivante mais le RETOUR
    DU PRIX SUR LE SUPPORT QU'IL VIENT DE CASSER. Si ce niveau, devenu résistance, repousse le prix (mèche haute)
    sans que le dernier plus haut plus bas soit repris, la distribution continue et le stop se place juste au-dessus
    du sommet du retour — le risque est défini par le retour lui-même, pas par un pivot lointain.

    Entrée : `structure_label` du tf d'entrée = LH_LL (SELL, `direction` = DOWN) ou HH_HL (BUY, `direction` = UP) ;
    le creux confirmé le plus récent AYANT ÉTÉ CASSÉ EN CLÔTURE (au-delà de 0,1 ATR) dans les `break_lookback`
    (défaut 15) dernières barres clôturées sert de niveau — ce n'est pas forcément le dernier pivot de la liste,
    celui-ci devenant le nouvel extrême creusé par la cassure ; depuis cette cassure, le prix est revenu dans la
    zone de polarité [niveau − 0,15 ATR ; niveau + 0,8 ATR] sans jamais reprendre le dernier plus haut plus bas ;
    la barre clôturée repart au-delà du niveau (clôture < niveau − 0,05 ATR pour une vente).
    Confirmation : bougie dans le sens du trade, clôture dans les 55 % favorables de son amplitude et mèche
    opposée >= 25 % de l'amplitude (trace du rejet du niveau).
    Filtres : tendance du tf supérieur non opposée ; spread <= 15 % de l'ATR H1 ; retour d'au moins une barre.
    SL : au-dessus (sous) l'extrême du retour + 0,25 ATR — si le retour est dépassé, la polarité n'a pas tenu ;
    distance bornée à [0,4 ATR ; min(3 ; `sl_atr` + 0,6) ATR].
    TP : cibles = extrême atteint depuis la cassure (relance du mouvement) puis projection de la hauteur
    dernier LH → niveau cassé, reportée sous le niveau ; premier TP à 1,25 R.
    Invalidation : clôture du tf d'entrée au-dessus (sous) l'extrême du retour : la polarité du niveau est perdue.
    Score : structure 20 + fraîcheur de la cassure 0-15 + qualité du retour 0-15 + MTF 0-15 + rejet (mèche) 0-15
    + profondeur du mouvement déjà acquis 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    # prix OHLC manquants/NaN sur une barre décisive → refus : `_ctx` ne valide que les indicateurs et
    # toute comparaison avec NaN étant fausse, les filtres d'anatomie de bougie ne rejetteraient rien
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60:
        return None
    want = str(p.get("direction", "DOWN")).upper()
    side = Side.SELL if want == "DOWN" else Side.BUY if want == "UP" else None
    if side is None:
        return None
    sgn = side.sign
    label = "LH_LL" if side is Side.SELL else "HH_HL"
    if structure_label(closed) != label:
        return None
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    cap = float(sh[-1][1] if side is Side.SELL else sl_[-1][1])       # dernier LH (HL) : borne du retour
    lb = int(p.get("break_lookback", 15))
    start = max(0, n - 1 - lb)
    win = closed.iloc[start:n - 1]
    if win.empty or not _ohlc_ok(win):
        return None
    wc = win["close"].to_numpy(dtype=float)
    # Niveau de polarité = creux (sommet) confirmé le PLUS RÉCENT réellement cassé en clôture dans la fenêtre.
    # Imposer `sl_[-1]` rendait `break_lookback` inopérant : dès que le nouvel extrême creusé par la cassure est
    # confirmé (3 barres après lui), le niveau cassé n'est plus le dernier pivot de la liste et l'agent cessait
    # de voir la cassure au moment même où le retour sur le niveau devient observable.
    # Positions calculées sur la fenêtre (numpy) et non sur les étiquettes d'index : le screener ne suppose rien
    # de l'index du frame reçu (un frame issu d'une tranche non réindexée donnait un `bi` faux).
    pivots = sl_ if side is Side.SELL else sh
    level: Optional[float] = None
    bi = 0
    for pi, px in reversed(pivots):
        hits = np.flatnonzero(sgn * (wc - float(px)) > 0.1 * atr)
        if hits.size == 0:
            continue
        first = start + int(hits[0])
        if pi >= first:                                               # le pivot doit précéder sa propre cassure
            continue
        level, bi = float(px), first                                  # première barre de cassure de la fenêtre
        break
    if level is None:
        return None
    post = closed.iloc[bi + 1:n - 1]
    if len(post) < 1 or not _ohlc_ok(post):
        return None
    back = float(post["high"].max()) if side is Side.SELL else float(post["low"].min())
    if sgn * (level - back) < -0.15 * atr:                            # le prix n'est jamais revenu sur le niveau
        return None
    if sgn * (level - back) > 0.8 * atr:                              # retour trop profond : niveau repris
        return None
    if sgn * (cap - back) >= 0:                                       # le dernier LH/HL a été repris : plus de structure
        return None
    entry = float(le["close"])
    if sgn * (entry - level) <= 0.05 * atr:                           # la barre de signal ne repart pas au-delà du niveau
        return None
    if sgn * (le["close"] - le["open"]) <= 0 or _close_pos(le, side) < 0.55:
        return None
    if _wick_against(le, side) < 0.25:
        return None
    if _trend_of(lt) == ("UP" if side is Side.SELL else "DOWN"):
        return None
    if _spread_ratio_h1(snap) > 0.15:
        return None
    sl_atr = float(p.get("sl_atr", 1.2))
    sl = _bound_sl(snap, side, entry, back - sgn * 0.25 * atr, atr, 0.4, min(3.0, sl_atr + 0.6))
    if sl is None:
        return None
    seg = closed.iloc[bi:n]
    extreme = float(seg["low"].min()) if side is Side.SELL else float(seg["high"].max())
    height = abs(cap - level)
    age = n - 1 - bi
    # score : composantes documentées
    score = 20.0
    score += _clamp((lb - age) * 15.0 / max(lb, 1), 0, 15)            # cassure récente = polarité encore vive
    score += _clamp((1.0 - abs(level - back) / max(0.8 * atr, 1e-12)) * 15.0, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    score += _clamp(_wick_against(le, side) * 30.0, 0, 15)
    score += _clamp(abs(extreme - level) / atr * 6.0, 0, 10)
    pros += [f"structure {label} confirmée sur {spec.timeframes['entry']}",
             f"niveau structurel {level:.5f} cassé en clôture il y a {age} barre(s)",
             f"retour sur le niveau ({abs(level - back) / atr:.2f} ATR) sans reprise du dernier pivot opposé",
             f"rejet marqué (mèche {_wick_against(le, side):.0%} de l'amplitude)"]
    cons = ["inversion de polarité : un second retour plus ample reste possible avant la reprise"]
    if age >= lb - 2:
        cons.append("cassure ancienne : la mémoire du niveau s'estompe")
    if height < 0.8 * atr:
        cons.append(f"structure étroite ({height / atr:.1f} ATR) : objectif mesuré limité")
    targets = [extreme, level + sgn * height]
    rr = float(p.get("rr", 2.0))
    cand = _build(spec, snap, side, entry, sl, rr, score, pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà de l'extrême du retour ({back:.5f})", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, rr, first_r=1.25), snap)


# --------------------------------------------------------------------------------------------------------------
# F03 — break_of_structure (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("F03")
def strategy_f03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """F03 — Cassure de structure (BOS) impulsive au sortir d'une base contractée.

    Thèse : toutes les cassures de swing ne se valent pas. Une cassure exploitable réunit trois conditions :
    le CORPS de la bougie (pas seulement la mèche) dépasse le pivot, la bougie est plus ample que les précédentes
    (impulsion réelle) et elle part d'une base contractée où le prix s'est comprimé. Le stop va sous la base,
    la cible est la projection de la hauteur de cette base au-delà du pivot (mouvement mesuré).

    Entrée : la barre clôturée dépasse le dernier pivot confirmé (swing haut pour un achat, swing bas pour une
    vente) d'au moins 0,15 ATR EN CLÔTURE et au moins `body_beyond_min` (défaut 50 %) du CORPS est au-delà du
    pivot (une cassure portée par la seule mèche est refusée) ; aucune des 10 barres clôturées précédentes n'avait
    clôturé au-delà de ce pivot (cassure neuve, pas déjà exploitée). Exiger le corps ENTIER au-delà du pivot serait
    inatteignable : sans gap, la première bougie qui clôture au-delà ouvre toujours en deçà.
    Confirmation : amplitude de la barre de cassure >= 1,2 × l'amplitude moyenne des 10 barres précédentes.
    Filtres : base des 10 barres précédentes contractée (hauteur <= `base_max_atr`, défaut 2,5 ATR) ; ADX14 du tf
    d'entrée >= `adx_min` (défaut 15) ; spread <= 15 % de l'ATR H1.
    SL : à l'opposé de la base (plus bas / plus haut des 10 barres précédant la cassure) ∓ 0,2 ATR : la cassure
    n'a de sens que tant que la base tient ; distance bornée à [0,5 ATR ; min(3 ; `sl_atr` + 1) ATR].
    TP : mouvement mesuré = pivot + hauteur de la base, et pivot + 1,5 × cette hauteur ; premier TP à 1,5 R,
    cible finale = max(`rr` R, mouvement mesuré).
    Invalidation : clôture du tf d'entrée de retour à l'intérieur de la base (au-delà du pivot cassé).
    Score : cassure 25 + impulsion 0-15 + contraction de la base 0-15 + MTF 0-15 + corps de bougie 0-15
    + ADX 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    # prix OHLC manquants/NaN sur une barre décisive → refus : `_ctx` ne valide que les indicateurs et
    # toute comparaison avec NaN étant fausse, les filtres d'anatomie de bougie ne rejetteraient rien
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60:
        return None
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    base = closed.iloc[n - 11:n - 1]
    if len(base) < 10 or not _ohlc_ok(base):
        return None
    entry = float(le["close"])
    up_lvl, dn_lvl = float(sh[-1][1]), float(sl_[-1][1])
    body_min = float(p.get("body_beyond_min", 0.5))
    if (entry > up_lvl + 0.15 * atr and _body_beyond(le, up_lvl, Side.BUY) >= body_min
            and _no_close_beyond(closed, up_lvl, Side.BUY, 10)):
        side, level = Side.BUY, up_lvl
    elif (entry < dn_lvl - 0.15 * atr and _body_beyond(le, dn_lvl, Side.SELL) >= body_min
            and _no_close_beyond(closed, dn_lvl, Side.SELL, 10)):
        side, level = Side.SELL, dn_lvl
    else:
        return None
    sgn = side.sign
    rng_mean = float((base["high"] - base["low"]).mean())
    if not np.isfinite(rng_mean) or rng_mean <= 0 or _range(le) < 1.2 * rng_mean:
        return None
    base_hi, base_lo = float(base["high"].max()), float(base["low"].min())
    height = base_hi - base_lo
    if not np.isfinite(height) or height <= 0 or height > float(p.get("base_max_atr", 2.5)) * atr:
        return None
    if le["adx14"] < float(p.get("adx_min", 15)):
        return None
    if _spread_ratio_h1(snap) > 0.15:
        return None
    sl_atr = float(p.get("sl_atr", 1.0))
    anchor = base_lo - 0.2 * atr if side is Side.BUY else base_hi + 0.2 * atr
    sl = _bound_sl(snap, side, entry, anchor, atr, 0.5, min(3.0, sl_atr + 1.0))
    if sl is None:
        return None
    # score : composantes documentées
    score = 25.0
    score += _clamp((_range(le) / rng_mean - 1.2) * 20.0, 0, 15)                 # impulsion de la barre de cassure
    score += _clamp((2.5 - height / atr) * 8.0, 0, 15)                           # base d'autant plus serrée
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    score += _clamp(_body_ratio(le) * 15.0, 0, 15)
    score += _clamp((float(le["adx14"]) - 15.0) * 0.6, 0, 10)
    pros += [f"cassure en clôture du pivot {level:.5f} ({_body_beyond(le, level, side):.0%} du corps au-delà)",
             f"barre {_range(le) / rng_mean:.1f}× plus ample que les 10 précédentes",
             f"base contractée de {height / atr:.1f} ATR avant la cassure",
             f"aucune clôture au-delà du pivot sur les 10 barres précédentes (cassure neuve)"]
    cons = ["cassure sans retest : un retour sur le niveau reste le scénario contraire le plus fréquent"]
    if structure_label(closed) == ("LH_LL" if side is Side.BUY else "HH_HL"):
        cons.append("cassure à contre-structure : premier signal de retournement, pas une continuation")
        score -= 8
    if _trend_of(lt) == ("DOWN" if side is Side.BUY else "UP"):
        cons.append(f"tendance {spec.timeframes['trend']} opposée")
        score -= 8
    targets = [level + sgn * height, level + sgn * 1.5 * height]
    rr = float(p.get("rr", 2.5))
    cand = _build(spec, snap, side, entry, sl, rr, score, pros, cons,
                  f"clôture {spec.timeframes['entry']} de retour dans la base (au-delà de {level:.5f})", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, rr, first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# F04 — change_of_character (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("F04")
def strategy_f04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """F04 — Changement de caractère (CHoCH) après ESSOUFFLEMENT mesuré des jambes.

    Thèse : un CHoCH n'a de valeur que si la tendance qu'il conteste montrait déjà des signes d'épuisement. On
    exige donc que la DERNIÈRE JAMBE de la tendance soit nettement plus courte que la précédente (perte d'amplitude
    mesurée sur les pivots confirmés, pas sur un oscillateur), puis que le prix casse le dernier pivot contraire
    peu de temps après l'extrême. Le risque est limité au dernier creux (sommet) mineur : inutile de risquer
    jusqu'à l'extrême de la tendance, le scénario est faux bien avant.

    Entrée : `structure_label` du tf d'entrée = LH_LL (achat de retournement) ou HH_HL (vente de retournement) ;
    les deux dernières jambes contraires mesurées sur pivots confirmés vérifient jambe_récente <= 0,85 × jambe
    précédente (essoufflement) ; l'extrême de la tendance date de moins de `max_age` (défaut 12) barres clôturées ;
    la barre clôturée casse le dernier pivot contraire de plus de 0,1 ATR en clôture, sans qu'aucune des 10 barres
    précédentes ne l'ait fait.
    Confirmation : bougie dans le sens du trade avec corps >= 40 % de son amplitude.
    Filtres : refus si la tendance du tf supérieur est OPPOSÉE au retournement ET que son ADX14 >= `adx_block`
    (défaut 30) : on ne se met pas en travers d'une tendance supérieure forte ; spread <= 15 % de l'ATR H1.
    SL : sous (au-dessus) le dernier creux (sommet) mineur — extrême des 6 dernières barres clôturées, barre de
    signal comprise — ∓ 0,25 ATR ;
    distance bornée à [0,5 ATR ; min(3 ; `sl_atr` + 1) ATR].
    TP : retracement de la dernière jambe entière : 61,8 % puis pivot contraire précédent (LH précédent) ;
    premier TP à 1,5 R.
    Invalidation : clôture du tf d'entrée au-delà du creux (sommet) mineur ayant servi de base : la tendance initiale
    reprend la main.
    Score : CHoCH 20 + essoufflement 0-20 + fraîcheur 0-15 + corps de bougie 0-15 + place jusqu'au retracement 0-15
    + contexte MTF 0-15.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    # prix OHLC manquants/NaN sur une barre décisive → refus : `_ctx` ne valide que les indicateurs et
    # toute comparaison avec NaN étant fausse, les filtres d'anatomie de bougie ne rejetteraient rien
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or not _valid(lt, "adx14"):
        return None
    label = structure_label(closed)
    side = Side.BUY if label == "LH_LL" else Side.SELL if label == "HH_HL" else None
    if side is None:
        return None
    sgn = side.sign
    sh, sl_ = swing_points(closed)
    if len(sh) < 2 or len(sl_) < 2:
        return None
    # pivots : `opp` = pivots contraires au retournement (sommets pour un achat), `ext` = extrêmes de la tendance
    opp, ext = (sh, sl_) if side is Side.BUY else (sl_, sh)
    lvl_i, lvl = opp[-1]
    ext_i, ext_px = ext[-1]
    prev_opp = float(opp[-2][1])
    prev_ext = float(ext[-2][1])
    if ext_i <= lvl_i:                                     # l'extrême doit suivre le dernier pivot contraire
        return None
    leg_now = abs(float(lvl) - float(ext_px))
    leg_prev = abs(prev_opp - prev_ext)
    if leg_prev <= 0 or leg_now <= 0 or leg_now > 0.85 * leg_prev:
        return None
    if n - 1 - ext_i > int(p.get("max_age", 12)):
        return None
    entry = float(le["close"])
    if sgn * (entry - float(lvl)) < 0.1 * atr or not _no_close_beyond(closed, float(lvl), side, 10):
        return None
    if sgn * (le["close"] - le["open"]) <= 0 or _body_ratio(le) < 0.4:
        return None
    tr_h = _trend_of(lt)
    if tr_h == ("DOWN" if side is Side.BUY else "UP") and float(lt["adx14"]) >= float(p.get("adx_block", 30)):
        return None
    if _spread_ratio_h1(snap) > 0.15:
        return None
    minor = closed.iloc[n - 6:n]
    if len(minor) < 5 or not _ohlc_ok(minor):
        return None
    base = float(minor["low"].min()) if side is Side.BUY else float(minor["high"].max())
    sl_atr = float(p.get("sl_atr", 1.0))
    sl = _bound_sl(snap, side, entry, base - sgn * 0.25 * atr, atr, 0.5, min(3.0, sl_atr + 1.0))
    if sl is None:
        return None
    span = abs(prev_opp - float(ext_px))                   # dernière jambe entière de la tendance contestée
    fib = float(ext_px) + sgn * 0.618 * span
    room = sgn * (fib - entry)
    # score : composantes documentées
    score = 20.0
    score += _clamp((1.0 - leg_now / leg_prev) * 40.0, 0, 20)          # essoufflement des jambes
    score += _clamp((12 - (n - 1 - ext_i)) * 1.5, 0, 15)               # signal proche de l'extrême
    score += _clamp(_body_ratio(le) * 15.0, 0, 15)
    score += _clamp(room / atr * 7.0, 0, 15)
    if tr_h == ("UP" if side is Side.BUY else "DOWN"):
        score += 15
        pros_mtf = [f"tendance {spec.timeframes['trend']} déjà retournée dans le sens du CHoCH"]
    elif tr_h == "FLAT":
        score += 7
        pros_mtf = [f"tendance {spec.timeframes['trend']} neutre : pas d'opposition"]
    else:
        pros_mtf = []
    pros = [f"structure {label} cassée : changement de caractère sur {spec.timeframes['entry']}",
            f"jambe finale {leg_now / max(leg_prev, 1e-12):.0%} de la précédente : essoufflement mesuré",
            f"extrême de tendance vieux de {n - 1 - ext_i} barre(s) : signal frais",
            f"clôture au-delà du pivot {lvl:.5f} (aucune des 10 barres précédentes ne l'avait fait)"] + pros_mtf
    cons = ["retournement : premier signal contre la tendance en place, taux de réussite historiquement variable"]
    if tr_h == ("DOWN" if side is Side.BUY else "UP"):
        cons.append(f"tendance {spec.timeframes['trend']} encore opposée (ADX {float(lt['adx14']):.0f})")
    if room <= 0:
        cons.append("retracement 61,8 % déjà atteint : cible structurelle limitée")
    targets = [fib, prev_opp]
    rr = float(p.get("rr", 2.0))
    cand = _build(spec, snap, side, entry, sl, rr, score, pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà du creux/sommet mineur de base ({base:.5f})", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, rr, first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# F05 — swing_rejection (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("F05")
def strategy_f05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """F05 — Rejet d'une ZONE de swings testée plusieurs fois, confirmé par la barre suivante.

    Thèse : un niveau touché une seule fois n'est qu'un prix ; une zone où plusieurs pivots confirmés se
    superposent (à moins de `zone_atr` ATR les uns des autres) est une zone où des ordres dorment. Le signal exige
    DEUX barres : une barre qui pénètre la zone et la rejette par une longue mèche, puis une barre de confirmation
    qui clôture au-delà du plus haut (plus bas) de la barre de rejet. Le stop tient dans la mèche : si la zone cède,
    la perte est petite.

    Entrée : zone construite sur les pivots confirmés du tf d'entrée (au moins `min_touches`, défaut 2, pivots
    contenus dans `zone_atr` ATR, défaut 0,5) ; la barre clôturée PRÉCÉDENTE pénètre la zone (son extrême entre
    dans [zone − `tol_atr` ATR ; zone]) sans clôturer au-delà du bord opposé de la zone, avec une mèche de rejet
    >= 40 % de son amplitude ; la barre clôturée de signal clôture au-delà du plus haut (plus bas) de la barre de
    rejet ET au-delà de la zone.
    Confirmation : la barre de signal va dans le sens du trade (clôture > ouverture pour un achat).
    Filtres : `with_trend` (défaut vrai) impose la tendance du tf supérieur dans le sens du trade ; zone testée
    pour la dernière fois il y a moins de `zone_age` (défaut 80) barres ; spread <= 15 % de l'ATR H1.
    SL : sous (au-dessus) l'extrême des deux barres du rejet − 0,25 ATR — la mèche est la borne du risque ;
    distance bornée à [0,4 ATR ; min(3 ; `sl_atr` + 0,8) ATR].
    TP : les deux pivots confirmés opposés les plus proches au-delà de l'entrée (jamais l'extrême de toute la
    fenêtre, qui n'est pas une cible de ce trade) ; premier TP à 1,5 R.
    Invalidation : clôture du tf d'entrée au-delà du bord lointain de la zone (la zone a cédé).
    Score : zone 15 + nombre de touches 0-15 + rejet (mèche) 0-20 + confirmation 0-15 + MTF 0-15 + fraîcheur 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    # prix OHLC manquants/NaN sur une barre décisive → refus : `_ctx` ne valide que les indicateurs et
    # toute comparaison avec NaN étant fausse, les filtres d'anatomie de bougie ne rejetteraient rien
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60:
        return None
    sh, sl_ = swing_points(closed)
    tr = _trend_of(lt)
    with_trend = bool(p.get("with_trend", True))
    zone_w = float(p.get("zone_atr", 0.5))
    tol = float(p.get("tol_atr", 0.25)) * atr
    prev = closed.iloc[-2]
    if not _ohlc_ok(closed.iloc[-2:]):
        return None
    entry = float(le["close"])
    side: Optional[Side] = None
    for cand_side, points in ((Side.BUY, sl_), (Side.SELL, sh)):
        if with_trend and tr != ("UP" if cand_side is Side.BUY else "DOWN"):
            continue
        z = _cluster_zone(points, atr, zone_w)
        if z is None:
            continue
        z_lo, z_hi, touches, last_touch = z
        if touches < int(p.get("min_touches", 2)) or n - 1 - last_touch > int(p.get("zone_age", 80)):
            continue
        if cand_side is Side.BUY:
            entered = z_lo - tol <= float(prev["low"]) <= z_hi + tol
            held = float(prev["close"]) > z_lo
            confirmed = entry > float(prev["high"]) and entry > z_hi
        else:
            entered = z_lo - tol <= float(prev["high"]) <= z_hi + tol
            held = float(prev["close"]) < z_hi
            confirmed = entry < float(prev["low"]) and entry < z_lo
        if entered and held and confirmed and _wick_against(prev, cand_side) >= 0.4:
            side = cand_side
            zone = (z_lo, z_hi, touches, last_touch)
            break
    if side is None:
        return None
    sgn = side.sign
    z_lo, z_hi, touches, last_touch = zone
    if sgn * (le["close"] - le["open"]) <= 0:
        return None
    if _spread_ratio_h1(snap) > 0.15:
        return None
    wick_ext = min(float(prev["low"]), float(le["low"])) if side is Side.BUY else max(float(prev["high"]), float(le["high"]))
    sl_atr = float(p.get("sl_atr", 0.9))
    sl = _bound_sl(snap, side, entry, wick_ext - sgn * 0.25 * atr, atr, 0.4, min(3.0, sl_atr + 0.8))
    if sl is None:
        return None
    # Cibles = les DEUX pivots opposés confirmés les PLUS PROCHES au-delà de l'entrée. La seconde cible était
    # auparavant `max(pivots)`, c'est-à-dire l'extrême de toute la fenêtre (plusieurs centaines de barres) : ce
    # n'est pas une cible de ce trade et elle fixait à elle seule le `rr` annoncé au gate, à la revue et au
    # classement des candidats (rr de 10 R et plus sur une simple mèche de rejet).
    opp_all = [float(px) for _, px in (sh if side is Side.BUY else sl_)]
    beyond = sorted([x for x in opp_all if np.isfinite(x) and sgn * (x - entry) >= 0.5 * atr],
                    key=lambda x: sgn * (x - entry))
    near = beyond[0] if beyond else None
    second = beyond[1] if len(beyond) > 1 else None
    # score : composantes documentées
    score = 15.0
    score += _clamp((touches - 1) * 7.5, 0, 15)
    score += _clamp(_wick_against(prev, side) * 25.0, 0, 20)
    score += _clamp(_close_pos(le, side) * 15.0, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    score += _clamp((80 - (n - 1 - last_touch)) * 0.125, 0, 10)
    pros += [f"zone de {touches} pivots confirmés ({z_lo:.5f}-{z_hi:.5f}), large de {(z_hi - z_lo) / atr:.2f} ATR",
             f"barre de rejet : mèche {_wick_against(prev, side):.0%} de l'amplitude, clôture dans la zone tenue",
             "confirmation : la barre suivante clôture au-delà de l'extrême de la barre de rejet",
             f"dernier test de la zone il y a {n - 1 - last_touch} barre(s)"]
    cons: list[str] = []
    if not with_trend and tr not in ("FLAT", "UP" if side is Side.BUY else "DOWN"):
        cons.append(f"tendance {spec.timeframes['trend']} opposée à ce rejet")
        score -= 10
    if near is None:
        cons.append("aucun pivot opposé au-delà de l'entrée : objectif purement en multiples de R")
    if (z_hi - z_lo) > 0.8 * atr:
        cons.append(f"zone large ({(z_hi - z_lo) / atr:.2f} ATR) : point d'entrée moins précis")
    targets = [x for x in (near, second) if x is not None]
    rr = float(p.get("rr", 2.0))
    edge = z_lo if side is Side.BUY else z_hi
    cand = _build(spec, snap, side, entry, sl, rr, score, pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà du bord lointain de la zone ({edge:.5f})", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, rr, first_r=1.5), snap)


# --------------------------------------------------------------------------------------------------------------
# F06 — mtf_structure_alignment (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("F06")
def strategy_f06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """F06 — Alignement des structures M15 et H1, entrée sur plus haute clôture de 20 barres, cible = swing H1.

    Thèse : quand la structure du tf d'entrée ET celle du tf supérieur racontent la même histoire (HH-HL des deux
    côtés), le bon déclencheur n'est pas un extrême de mèche mais une CLÔTURE plus haute que toutes les clôtures
    des 20 dernières barres : c'est la preuve que l'acceptation du prix progresse. Le trade n'est pris que s'il
    reste assez de place jusqu'au dernier pivot confirmé du tf supérieur pour payer au moins 1,5 fois le risque —
    le filtre porte sur la GÉOMÉTRIE du trade, pas sur un indicateur.

    Entrée : `structure_label` identique sur le tf d'entrée et le tf de tendance (HH_HL → achat, LH_LL → vente ;
    `require_mtf` = faux relâche l'exigence sur le tf supérieur) ; clôture de la barre du tf d'entrée strictement
    supérieure (inférieure) à toutes les clôtures des `donchian` (défaut 20) barres clôturées précédentes.
    Confirmation : ADX14 du tf d'entrée >= `adx_min` (défaut 18) et tendance EMA du tf supérieur non opposée.
    Filtres : un pivot confirmé du tf supérieur doit exister au-delà de l'entrée et laisser une place >= 1,5 × la
    distance au SL (sinon le trade est refusé : pas de cible, pas de trade) ; spread <= 12 % de l'ATR H1 ;
    structures H4 / D1 (si disponibles) utilisées en bonus/malus, jamais en donnée inventée.
    SL : sous (au-dessus) le dernier pivot confirmé du tf d'entrée situé du bon côté de l'entrée − 0,2 ATR ;
    distance bornée à [0,5 ATR ; min(3 ; `sl_atr` + 0,6) ATR].
    TP : premier TP à 1,5 R, cible intermédiaire = milieu du chemin vers le pivot du tf supérieur, cible finale =
    ce pivot (ou `rr` R s'il est plus lointain).
    Invalidation : clôture du tf d'entrée sous (au-dessus) le pivot ayant servi de SL : l'alignement M15/H1 est rompu.
    Score : alignement M15/H1 25 + place jusqu'à la cible 0-20 + ADX 0-15 + MTF EMA 0-15 + anatomie de bougie 0-10
    + alignement H4/D1 0-10.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    # prix OHLC manquants/NaN sur une barre décisive → refus : `_ctx` ne valide que les indicateurs et
    # toute comparaison avec NaN étant fausse, les filtres d'anatomie de bougie ne rejetteraient rien
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    tclosed = _closed(t)
    n = len(closed)
    if n < 60 or len(tclosed) < 40:
        return None
    label_e = structure_label(closed)
    side = _side_from_label(label_e)
    if side is None:
        return None
    sgn = side.sign
    label_t = structure_label(tclosed)
    require_mtf = bool(p.get("require_mtf", True))
    if require_mtf and label_t != label_e:
        return None
    dc = int(p.get("donchian", 20))
    win = closed["close"].iloc[max(0, n - 1 - dc):n - 1]
    if len(win) < dc or win.isna().any():
        return None
    entry = float(le["close"])
    if sgn * (entry - (float(win.max()) if side is Side.BUY else float(win.min()))) <= 0:
        return None
    if le["adx14"] < float(p.get("adx_min", 18)):
        return None
    if _trend_of(lt) == ("DOWN" if side is Side.BUY else "UP"):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    # SL : dernier pivot confirmé du tf d'entrée du bon côté de l'entrée
    sh, sl_ = swing_points(closed)
    own = [px for _, px in (sl_ if side is Side.BUY else sh) if sgn * (entry - px) > 0]
    if not own:
        return None
    pivot = float(own[-1])
    sl_atr = float(p.get("sl_atr", 1.2))
    sl = _bound_sl(snap, side, entry, pivot - sgn * 0.2 * atr, atr, 0.5, min(3.0, sl_atr + 0.6))
    if sl is None:
        return None
    dist = abs(entry - sl)
    # cible structurelle du tf supérieur : place >= 1,5 R exigée
    sh_t, sl_t = swing_points(tclosed)
    tgt = _nearest_beyond([px for _, px in (sh_t if side is Side.BUY else sl_t)], entry, side, 1.5 * dist)
    if tgt is None:
        return None
    room_r = sgn * (tgt - entry) / dist
    # score : composantes documentées
    score = 25.0
    score += _clamp((room_r - 1.5) * 10.0, 0, 20)
    score += _clamp((float(le["adx14"]) - 18.0) * 1.0, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros += [f"structures alignées : {label_e} sur {spec.timeframes['entry']} et {label_t} sur {spec.timeframes['trend']}",
             f"plus haute/basse clôture des {dc} dernières barres (acceptation du prix)",
             f"cible structurelle {spec.timeframes['trend']} à {room_r:.1f} R de l'entrée",
             f"ADX {float(le['adx14']):.0f} sur {spec.timeframes['entry']}"]
    cons: list[str] = []
    if not require_mtf and label_t != label_e:
        cons.append(f"structure {spec.timeframes['trend']} ({label_t}) non alignée : exigence MTF désactivée")
        score -= 10
    # contexte H4 / D1 : bonus/malus seulement si le frame existe réellement (jamais de valeur inventée)
    for tf_name in ("H4", "D1"):
        df = snap.frames.get(tf_name)
        if df is None or len(df) < 40:
            continue
        lbl = structure_label(_closed(df))
        if lbl == label_e:
            score += 5
            pros.append(f"structure {tf_name} également {lbl}")
        elif lbl in ("HH_HL", "LH_LL"):
            score -= 5
            cons.append(f"structure {tf_name} opposée ({lbl})")
    if room_r < 2.0:
        cons.append(f"cible {spec.timeframes['trend']} peu éloignée ({room_r:.1f} R) : marge d'erreur réduite")
    targets = [entry + sgn * (tgt - entry) * 0.5, float(tgt)]
    rr = float(p.get("rr", 2.5))
    cand = _build(spec, snap, side, entry, sl, rr, score, pros, cons,
                  f"clôture {spec.timeframes['entry']} au-delà du pivot {pivot:.5f} (alignement rompu)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, rr, first_r=1.5), snap)
