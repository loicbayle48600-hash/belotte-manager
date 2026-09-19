"""Famille G — volatilité : une stratégie propre par agent générateur (G01 … G04).

Thème commun : la volatilité elle-même est le signal. Chaque agent mesure un état de volatilité différent —
changement de régime de l'ATR (G01), compression des bandes dans le canal de Keltner (G02), amplitude de
l'opening range comparée à son propre historique (G03), budget d'amplitude encore disponible dans la session
en cours (G04) — puis en déduit une direction, un stop calibré sur CETTE mesure de volatilité et un objectif
projeté à partir d'elle. Aucune des quatre n'est une variante paramétrique d'une autre, ni des modules déjà
écrits (B tendance, C cassure, D repli), ni du screener générique de repli (`AgentSpec.base_strategy`).

L'agent G05 (`abnormal_volatility_filter`) n'est pas un générateur : il n'a ni `strategy` ni `base_strategy`
dans le registre et reste donc volontairement sans fonction ici.

Conventions communes (voir `agents/screeners.py`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est
  la barre en formation et n'est JAMAIS lue ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14
  du tf d'entrée, horodatage de la barre clôturée) ;
- `_build` construit le `TradeCandidate` (pénalité de spread) et refuse tout SL du mauvais côté ; le plan de TP
  générique est ensuite remplacé par le plan propre à l'agent (`_set_tp_plan`, `rr >= 1.5` garanti) ;
- le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (côté, stops_level, 0,25-4 ATR H1) : un SL
  refusé donne `None`, jamais une valeur corrigée à la volée ;
- `setup_score` 0-100 = somme documentée de composantes (mesure de volatilité, confirmation, alignement MTF,
  qualité d'exécution) : ce n'est PAS une probabilité de gain ;
- données insuffisantes ou indicateur NaN → `None`, jamais une valeur inventée ;
- les heures sont celles des barres clôturées (UTC), jamais l'horloge système : le Market Router filtre déjà la
  session, les agents n'affinent que leur fenêtre d'exploitation.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# Bornes de distance entrée→SL en ATR du tf d'entrée (le gate impose 0,25-4 ATR H1 ; on reste plus strict)
SL_MIN_ATR = 0.3
SL_MAX_ATR = 3.0
# Sur un tf d'entrée court (M15), l'ATR du tf d'entrée est bien plus petit que l'ATR H1 servant de référence au
# gate : la distance minimale tient aussi compte de l'ATR H1 pour ne jamais proposer un stop que le gate refuserait.
SL_MIN_H1_ATR = 0.3
# Une cible située au-delà de 6 R n'est pas un objectif de trade réaliste : elle est ignorée (le plan reste borné
# par `rr` × R). Même convention que la famille C.
MAX_TARGET_RR = 6.0

# Fenêtres de session UTC utilisées par G04 (déterminées par l'heure de la barre clôturée, pas par l'horloge système)
SESSION_WINDOWS: tuple[tuple[float, float, str], ...] = (
    (0.0, 7.0, "asiatique"),
    (7.0, 12.0, "de Londres"),
    (12.0, 17.0, "de New York"),
    (17.0, 24.0, "de fin de journée"),
)


# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------
def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


def _range(row: pd.Series) -> float:
    return float(row["high"] - row["low"])


def _close_pos(row: pd.Series, side: Side) -> float:
    """Position de la clôture dans l'amplitude, orientée : 1 = clôture à l'extrême favorable au trade."""
    rng = _range(row)
    if rng <= 0:
        return 0.5
    pos = float((row["close"] - row["low"]) / rng)
    return pos if side is Side.BUY else 1.0 - pos


def _hhmm(s: str, default: float) -> float:
    """Heure « HH:MM » → heure décimale UTC ; `default` si le format est invalide."""
    try:
        hh, mm = str(s).strip().split(":")[:2]
        return int(hh) + int(mm) / 60.0
    except (ValueError, AttributeError, TypeError):
        return default


def _hours(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(horodatages UTC, heures décimales d'ouverture) des barres fournies."""
    t = pd.to_datetime(df["time"], utc=True)
    return t, t.dt.hour + t.dt.minute / 60.0


def _bar_hour(row: pd.Series) -> Optional[float]:
    """Heure décimale UTC d'ouverture d'une barre (None si l'horodatage est illisible)."""
    try:
        ts = pd.Timestamp(row["time"])
    except (KeyError, TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return float(ts.hour + ts.minute / 60.0)


def _today_between(closed: pd.DataFrame, start_h: float, end_h: float) -> pd.DataFrame:
    """Barres clôturées du jour UTC de la dernière barre clôturée dont l'heure d'ouverture est dans [start_h, end_h)."""
    if len(closed) == 0:
        return closed
    t, h = _hours(closed)
    day = t.dt.normalize()
    return closed[(day == day.iloc[-1]) & (h >= start_h) & (h < end_h)]


def _session_history(h1_closed: pd.DataFrame, start_h: float, end_h: float, days: int = 10) -> list[float]:
    """Amplitudes (haut − bas) de la fenêtre [start_h, end_h) pour les jours UTC ANTÉRIEURS au jour courant.

    Calculée sur les barres H1 CLÔTURÉES (300 barres H1 ≈ 12 jours, contre ≈ 3 jours en M15) : c'est la mesure
    de « volatilité habituelle de cette session » utilisée par G03 et G04. Liste triée par jour croissant,
    limitée aux `days` derniers jours. Liste vide si l'historique est inexploitable.
    """
    if len(h1_closed) == 0:
        return []
    t, h = _hours(h1_closed)
    day = t.dt.normalize()
    mask = (h >= start_h) & (h < end_h) & (day < day.iloc[-1])
    if not bool(mask.any()):
        return []
    sub = pd.DataFrame({
        "day": day[mask].to_numpy(),
        "high": h1_closed["high"][mask].to_numpy(dtype=float),
        "low": h1_closed["low"][mask].to_numpy(dtype=float),
    })
    g = sub.groupby("day")
    rng = (g["high"].max() - g["low"].min()).sort_index()
    out = [float(x) for x in rng.tolist() if np.isfinite(x) and x > 0]
    return out[-days:]


def _efficiency_ratio(closes: pd.Series) -> tuple[float, float]:
    """Ratio d'efficience de Kaufman sur une série de clôtures : (déplacement net signé, ER dans [0, 1]).

    ER = |déplacement net| / somme des variations absolues : 1 = ligne droite, proche de 0 = aller-retour.
    (nan, 0) si le chemin parcouru est nul (série plate) — aucune direction exploitable.
    """
    s = closes.astype(float)
    disp = float(s.iloc[-1] - s.iloc[0])
    path = float(s.diff().abs().iloc[1:].sum())
    if not np.isfinite(path) or path <= 0 or not np.isfinite(disp):
        return float("nan"), 0.0
    return disp, abs(disp) / path


def _spread_ratio(snap, atr: float) -> float:
    """Spread courant exprimé en fraction de l'ATR du tf d'entrée (inf si inconnu : le filtre refuse alors)."""
    if atr <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr)


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
    """Remplace le plan de TP générique de `_build` par le plan propre à l'agent.

    - `targets` : cibles issues de la mesure de volatilité de l'agent (projection d'amplitude, couloir, budget
      de session) ; seules celles situées entre 0,5 R et `MAX_TARGET_RR` R dans le sens du trade sont retenues ;
    - premier TP à `first_r` R (prise partielle), cible finale = la plus lointaine entre `rr_min` R et la cible
      retenue la plus éloignée → `rr >= max(1.5, rr_min)` garanti ;
    - plan limité à 3 niveaux croissants dans le sens du trade, sans doublon.
    """
    if c is None:
        return None
    dist = abs(entry - c.sl)
    if dist <= 0:
        return None
    rr_min = max(1.5, float(rr_min))
    sign = side.sign
    pts = [float(x) for x in targets if x is not None and np.isfinite(x)
           and 0.5 * dist <= sign * (x - entry) <= MAX_TARGET_RR * dist]
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


def _squeeze_box(closed: pd.DataFrame, dur: int, n_sq: int) -> tuple[float, float, int]:
    """Couloir de compression BORNÉ : les `min(dur, n_sq)` dernières barres comprimées + la barre de libération.

    Renvoie (bas, haut, nombre de barres comprimées retenues). Mesurer l'amplitude sur TOUT le squeeze serait
    incohérent : un squeeze peut durer des dizaines de barres et son amplitude cumulée croît mécaniquement avec
    sa durée. Un filtre d'amplitude posé dessus refuserait justement les compressions les plus mûres — celles
    que le score récompense — et le SL posé à l'autre bout sortirait systématiquement des bornes de distance.
    (nan, nan, 0) si la fenêtre est vide ou inexploitable (colonnes NaN).
    """
    k = max(0, min(int(dur), int(n_sq)))
    box = closed.iloc[-(k + 1):]
    if len(box) == 0:
        return float("nan"), float("nan"), 0
    lo, hi = float(box["low"].min()), float(box["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi < lo:
        return float("nan"), float("nan"), 0
    return lo, hi, k


def _sl_note(side: Side, entry: float, raw_sl: float, sl: float, atr: float) -> Optional[str]:
    """Argument CONTRE honnête quand `_bound_sl` a dû déplacer le SL brut voulu par la thèse.

    Le niveau décrit par la thèse (stop chandelier, borne opposée du couloir de compression ou de l'opening
    range, structure de session) n'est alors plus celui du SL réellement proposé : on le dit explicitement au
    lieu de laisser croire qu'il est protégé. `None` si le SL proposé est bien le niveau voulu.
    """
    if not np.isfinite(raw_sl) or not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
        return None
    raw_d, d = abs(entry - raw_sl), abs(entry - sl)
    if d < raw_d - 1e-12:
        return (f"SL ramené à {d / atr:.2f} ATR (plafond {SL_MAX_ATR} ATR) : le niveau visé par la thèse "
                f"({raw_sl:.5g}) est plus loin et n'est donc plus protégé")
    if d > raw_d + 1e-12:
        return (f"SL élargi à {d / atr:.2f} ATR (distance minimale) : plus loin que le niveau visé par la thèse "
                f"({raw_sl:.5g}), le risque par trade porte sur cette distance élargie")
    return None


def _opposed(lt: pd.Series, side: Side) -> bool:
    """Vrai si la tendance EMA du tf supérieur est franchement opposée au sens du trade."""
    tr = _trend_of(lt)
    return (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP")


# --------------------------------------------------------------------------------------------------------------
# G01 — expansion d'ATR directionnelle, stop chandelier (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("G01")
def strategy_g01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """G01 — atr_expansion : changement de régime de volatilité exploité seulement s'il est DIRECTIONNEL.

    Thèse : une hausse durable de l'ATR n'est pas un signal en soi — elle peut n'être que du bruit à large
    amplitude. Elle ne devient exploitable que si le chemin parcouru pendant l'expansion est efficient : le
    ratio d'efficience de Kaufman (déplacement net / somme des variations) sépare l'expansion directionnelle
    de l'expansion en aller-retour. On suit alors le déplacement net, avec un stop chandelier ancré sur
    l'extrême de la fenêtre d'expansion.

    Règles d'entrée : ATR14 de la barre clôturée >= `atr_ratio` × médiane des ATR14 des 50 barres ANTÉRIEURES à
    la fenêtre d'expansion (comparaison à un régime de référence non contaminé par l'expansion elle-même) ;
    ATR14 encore en hausse (> ATR14 de 3 barres plus tôt) ; ratio d'efficience sur `win` barres >= `er_min` ;
    sens = signe du déplacement net.
    Confirmation : clôture du bon côté de l'EMA20 du tf d'entrée ET `mom10` de même signe que le déplacement.
    Filtres : `vol_pct` <= `vol_pct_max` (au-delà, c'est un choc anormal : domaine de G05/E03, pas de suivi) ;
    tendance H1 non opposée ; RSI14 non extrême (<= 85 / >= 15) ; spread <= 12 % de l'ATR du tf d'entrée ;
    entrée située à moins de `sl_atr` ATR de l'extrême de la fenêtre (sinon le chandelier serait du mauvais côté,
    c'est-à-dire que le mouvement est déjà retracé).
    Logique de SL : stop chandelier = extrême (plus haut / plus bas) des `win` barres clôturées − `sl_atr` × ATR,
    distance ramenée dans [0,5 ATR ; 3 ATR] — le stop suit la volatilité du mouvement, pas un swing.
    Plan de TP : mouvement mesuré — 1,5 R (partiel), puis le déplacement net projeté depuis l'entrée (×1 puis
    ×1,6), cible finale = la plus lointaine entre `rr` × R et ces projections.
    Invalidation : clôture du tf d'entrée sous (au-dessus de) le SL proposé, ou ATR14 repassant sous la
    médiane de référence (le régime de volatilité s'est refermé). Si le chandelier sort des bornes de distance,
    le SL est ramené dans les bornes et l'écart est signalé dans `arguments_against` (`_sl_note`).
    Score : 30 (régime de volatilité élargi + efficience) + 0-12 ampleur de l'expansion + 0-13 efficience
    au-delà du seuil + 0-15 alignement MTF + 10 clôture à l'extrême de la bougie + 10 spread <= 6 % ATR.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    win = int(p.get("win", 10))
    if len(closed) < 60 + win + 2 or win < 4:
        return None
    base = closed["atr14"].iloc[-(50 + win):-win].dropna()
    if len(base) < 30:
        return None
    med = float(base.median())
    atr_prev = float(closed["atr14"].iloc[-4])
    if not np.isfinite(med) or med <= 0 or not np.isfinite(atr_prev):
        return None
    ratio = atr / med
    if ratio < float(p.get("atr_ratio", 1.4)) or atr <= atr_prev:
        return None
    disp, er = _efficiency_ratio(closed["close"].iloc[-(win + 1):])
    er_min = float(p.get("er_min", 0.45))
    if not np.isfinite(disp) or disp == 0.0 or er < er_min:
        return None
    side = Side.BUY if disp > 0 else Side.SELL
    if not _valid(le, "mom10", "ema20", "rsi14"):
        return None
    if side.sign * float(le["close"] - le["ema20"]) <= 0 or side.sign * float(le["mom10"]) <= 0:
        return None
    if _valid(le, "vol_pct") and float(le["vol_pct"]) > float(p.get("vol_pct_max", 97)):
        return None
    if _opposed(lt, side):
        return None
    if (side is Side.BUY and le["rsi14"] > 85) or (side is Side.SELL and le["rsi14"] < 15):
        return None
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return None
    entry = float(le["close"])
    sl_atr = float(p.get("sl_atr", 1.2))
    extreme = float(closed["high"].iloc[-win:].max()) if side is Side.BUY else float(closed["low"].iloc[-win:].min())
    raw_sl = extreme - side.sign * sl_atr * atr
    if side.sign * (entry - raw_sl) <= 0:
        return None  # entrée trop éloignée de l'extrême : le mouvement est déjà retracé
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, SL_MAX_ATR)
    if sl is None:
        return None
    score = 30.0 + _clamp((ratio - float(p.get("atr_ratio", 1.4))) * 25.0, 0, 12) + _clamp((er - er_min) * 60.0, 0, 13)
    pros = [f"ATR14 ×{ratio:.2f} par rapport à sa médiane 50 barres de référence, encore en hausse",
            f"expansion directionnelle : efficience de Kaufman {er:.2f} sur {win} barres",
            f"clôture du bon côté de l'EMA20 avec momentum {('positif' if side is Side.BUY else 'négatif')}"]
    cons = ["volatilité élargie : slippage et retours rapides possibles"]
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if _close_pos(le, side) >= 0.7:
        score += 10
        pros.append("clôture dans les 30 % favorables de la bougie")
    if spread <= 0.06:
        score += 10
    else:
        cons.append(f"spread {spread:.2f} ATR sur une volatilité déjà élargie")
    if _valid(le, "vol_pct") and float(le["vol_pct"]) >= 90:
        cons.append(f"percentile de volatilité {le['vol_pct']:.0f} : expansion déjà mûre")
    note = _sl_note(side, entry, raw_sl, sl, atr)
    if note:
        cons.append(note)
    inv = (f"clôture {spec.timeframes.get('entry', 'M15')} au-delà du SL ({sl:.5g}, stop chandelier = extrême "
           f"{win} barres ∓ {sl_atr} ATR) ou ATR14 sous sa médiane de référence ({med:.5g})")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons, inv, bt)
    targets = [entry + disp, entry + 1.6 * disp]
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# --------------------------------------------------------------------------------------------------------------
# G02 — compression de volatilité : squeeze Bollinger dans Keltner puis libération (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("G02")
def strategy_g02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """G02 — volatility_contraction : sortie d'un squeeze (bandes de Bollinger enfermées dans le canal de Keltner).

    Thèse : la contraction se mesure par un RAPPORT entre deux enveloppes, pas par un percentile de largeur —
    tant que les bandes de Bollinger (2σ) restent à l'intérieur du canal de Keltner (EMA20 ± `kc_mult` × ATR),
    le marché stocke de l'énergie. La barre où les bandes ressortent du canal libère la compression ; la
    direction n'est PAS donnée par une cassure de niveau (le prix peut rester dans son couloir) mais par le
    momentum déjà mesurable, c'est-à-dire le signe et la progression de l'histogramme MACD.

    Règles d'entrée : les `squeeze_min` barres clôturées qui PRÉCÈDENT la dernière ne sont pas sorties du
    squeeze, et la dernière barre clôturée en sort (au moins une bande hors du canal).
    Confirmation : histogramme MACD non nul, de signe constant avec le trade et en progression sur la barre ;
    clôture du bon côté de la médiane de Bollinger (`bb_mid`).
    Filtres : contraction réellement basse — `vol_pct` de la barre précédente <= `vol_pct_max` et ADX14 du tf
    d'entrée < `adx_max` (on ne veut pas d'une tendance déjà lancée) ; tendance H1 non opposée ; spread <= 12 %
    de l'ATR ; couloir de compression (fenêtre bornée ci-dessous) d'amplitude <= 2,5 ATR. L'amplitude est
    mesurée sur une fenêtre BORNÉE et non sur tout le squeeze : l'amplitude cumulée d'un squeeze long croît
    mécaniquement avec sa durée, un filtre posé dessus contredirait le bonus de durée du score et refuserait
    les compressions les mieux notées.
    Logique de SL : extrême OPPOSÉ du couloir de compression — les `squeeze_min` DERNIÈRES barres du squeeze
    plus la barre de libération, jamais tout le squeeze — avec une marge de 0,3 × `sl_atr` × ATR : un retour à
    l'autre bout de ce couloir signe l'échec de la libération. Distance ramenée dans [0,4 ATR ; 3 ATR], écart
    éventuel signalé dans `arguments_against` (`_sl_note`).
    Plan de TP : hauteur du canal de Keltner au moment du squeeze (2 × `kc_mult` × ATR) projetée ×1 puis ×2
    depuis l'entrée ; premier TP à 1,5 R ; cible finale = la plus lointaine entre `rr` × R et ces projections.
    Invalidation : retour des bandes de Bollinger à l'intérieur du canal de Keltner (compression non résolue)
    ou clôture du tf d'entrée au-delà de l'extrême opposé du couloir.
    Score : 30 (squeeze confirmé puis libéré) + 0-10 durée de la compression + 0-10 momentum MACD en
    progression + 0-15 alignement MTF + 10 `vol_pct` <= 25 + 5 spread <= 6 % ATR.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    n_sq = int(p.get("squeeze_min", 6))
    if len(closed) < 60 or n_sq < 3:
        return None
    if not _valid(le, "bb_up", "bb_low", "bb_mid", "macd_hist"):
        return None
    span = min(len(closed), 40)
    sub = closed.iloc[-span:]
    cols = ["bb_up", "bb_low", "ema20", "atr14", "macd_hist"]
    if sub[cols].isna().to_numpy().any():
        return None
    kc = float(p.get("kc_mult", 1.5))
    upper_k = sub["ema20"] + kc * sub["atr14"]
    lower_k = sub["ema20"] - kc * sub["atr14"]
    squeeze = ((sub["bb_up"] < upper_k) & (sub["bb_low"] > lower_k)).to_numpy()
    if squeeze[-1] or not bool(squeeze[-(n_sq + 1):-1].all()):
        return None  # il faut une compression continue PUIS une libération sur la dernière barre clôturée
    dur = 0
    for flag in squeeze[-2::-1]:
        if not flag:
            break
        dur += 1
    hist = float(le["macd_hist"])
    hist_prev = float(sub["macd_hist"].iloc[-2])
    if hist == 0.0:
        return None
    side = Side.BUY if hist > 0 else Side.SELL
    if side.sign * (hist - hist_prev) <= 0:
        return None  # momentum qui ne progresse pas : la libération n'a pas de porteur
    if side.sign * float(le["close"] - le["bb_mid"]) <= 0:
        return None
    prev = closed.iloc[-2]
    if _valid(prev, "vol_pct") and float(prev["vol_pct"]) > float(p.get("vol_pct_max", 50)):
        return None
    if float(le["adx14"]) >= float(p.get("adx_max", 30)):
        return None
    if _opposed(lt, side):
        return None
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return None
    # Couloir de référence (SL + filtre d'amplitude) : borné aux n_sq dernières barres comprimées + la barre de
    # libération. `dur` (durée totale du squeeze) ne sert qu'au score et aux arguments.
    box_lo, box_hi, box_len = _squeeze_box(closed, dur, n_sq)
    if not np.isfinite(box_hi) or not np.isfinite(box_lo) or box_hi - box_lo > 2.5 * atr:
        return None
    entry = float(le["close"])
    margin = 0.3 * float(p.get("sl_atr", 1.0)) * atr
    raw_sl = box_lo - margin if side is Side.BUY else box_hi + margin
    if side.sign * (entry - raw_sl) <= 0:
        return None
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.4, SL_MAX_ATR)
    if sl is None:
        return None
    score = 30.0 + _clamp((dur - n_sq) * 2.0, 0, 10) + _clamp(abs(hist - hist_prev) / atr * 100.0, 0, 10)
    pros = [f"compression : bandes de Bollinger enfermées dans le canal de Keltner sur {dur} barres",
            f"libération sur la dernière barre clôturée (couloir des {box_len} dernières barres comprimées : "
            f"{box_lo:.5g}-{box_hi:.5g})",
            "histogramme MACD de signe constant avec le trade et en progression"]
    cons = ["direction donnée par le momentum, pas par une cassure de niveau : faux départ possible"]
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if _valid(prev, "vol_pct") and float(prev["vol_pct"]) <= 25:
        score += 10
        pros.append(f"percentile de volatilité {prev['vol_pct']:.0f} avant libération")
    if spread <= 0.06:
        score += 5
    else:
        cons.append(f"spread {spread:.2f} ATR sur un marché encore étroit")
    if float(le["adx14"]) < 15:
        cons.append(f"ADX {le['adx14']:.0f} très bas : le marché peut retourner en compression")
    note = _sl_note(side, entry, raw_sl, sl, atr)
    if note:
        cons.append(note)
    kh = 2.0 * kc * atr
    inv = (f"retour des bandes de Bollinger dans le canal de Keltner (EMA20 ± {kc} ATR) ou clôture "
           f"{spec.timeframes.get('entry', 'M15')} au-delà du SL ({sl:.5g} ; borne opposée du couloir "
           f"{box_lo if side is Side.BUY else box_hi:.5g})")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), _clamp(score), pros, cons, inv, bt)
    targets = [entry + side.sign * kh, entry + side.sign * 2.0 * kh]
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.5))), snap)


# --------------------------------------------------------------------------------------------------------------
# G03 — opening range comprimé par rapport à son propre historique (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("G03")
def strategy_g03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """G03 — opening_range : cassure de l'opening range, mais SEULEMENT quand cet opening range est comprimé.

    Thèse : l'amplitude d'un opening range n'a de sens que comparée à celle des MÊMES heures les jours
    précédents. Un opening range nettement plus étroit que sa médiane historique signale une journée dont
    l'amplitude reste à faire ; sa cassure a alors une réserve de mouvement. Un opening range déjà large a au
    contraire consommé la volatilité du jour et sa cassure est un piège.

    Règles d'entrée : amplitude de l'opening range du jour (barres du tf d'entrée entre `start` et `end`)
    <= `or_ratio` × médiane des amplitudes de la MÊME fenêtre horaire les jours précédents (mesurées sur les
    barres H1 clôturées, qui couvrent ≈ 12 jours) ; PREMIÈRE clôture au-delà de la borne augmentée d'un tampon
    de `buffer_ratio` × l'amplitude du range (tampon proportionnel au range, pas à l'ATR) ; barre précédente
    encore en deçà de ce seuil.
    Confirmation : clôture dans les 35 % favorables de la bougie de cassure et volume (`tick_volume`) >= la
    moyenne des barres de l'opening range.
    Filtres : heure de la barre dans [`end`, `end` + `window_h`) ; au moins `min_days` jours d'historique ;
    amplitude du range entre 0,25 et 2 ATR H1 ; aucune cassure antérieure du jour dans un sens ou dans l'autre ;
    tendance H1 non opposée ; spread <= 12 % de l'ATR du tf d'entrée.
    Logique de SL : borne OPPOSÉE de l'opening range (moins 0,1 ATR de marge) — un range comprimé autorise un
    stop de range complet, et le retour du prix à l'autre bout invalide totalement l'idée. Distance ramenée
    dans [0,4 ATR ; 3 ATR] ; tout écart avec la borne opposée est signalé dans `arguments_against` (`_sl_note`).
    Plan de TP : projections classiques d'opening range — borne cassée + 1 × amplitude puis + 2 × amplitude ;
    premier TP à 1,5 R ; cible finale = la plus lointaine entre `rr` × R et ces projections.
    Invalidation : clôture du tf d'entrée de retour à l'intérieur de l'opening range.
    Score : 30 (cassure d'un opening range comprimé) + 0-15 degré de compression vs historique + 0-15
    alignement MTF + 10 clôture extrême + 10 volume >= 1,3 × moyenne du range + 5 spread <= 6 % ATR.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40 or "tick_volume" not in closed.columns:
        return None
    start_h = _hhmm(p.get("start", "07:00"), 7.0)
    end_h = _hhmm(p.get("end", "08:00"), 8.0)
    if end_h <= start_h:
        return None
    h = _bar_hour(le)
    if h is None or not (end_h <= h < end_h + float(p.get("window_h", 4.0))):
        return None
    or_bars = _today_between(closed, start_h, end_h)
    if len(or_bars) < 2:
        return None
    hi, lo = float(or_bars["high"].max()), float(or_bars["low"].min())
    rng = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not np.isfinite(rng) or rng <= 0 or atr_h1 <= 0 or not (0.25 * atr_h1 <= rng <= 2.0 * atr_h1):
        return None
    h1 = _frame(snap, "H1")
    if h1 is None:
        return None
    hist = _session_history(_closed(h1), start_h, end_h)
    if len(hist) < int(p.get("min_days", 3)):
        return None
    med = float(np.median(hist))
    if not np.isfinite(med) or med <= 0 or rng > float(p.get("or_ratio", 0.9)) * med:
        return None
    buf = float(p.get("buffer_ratio", 0.25)) * rng
    up_trig, dn_trig = hi + buf, lo - buf
    entry = float(le["close"])
    prev = closed.iloc[-2]
    after = _today_between(closed.iloc[:-1], end_h, 24.0)
    if entry > up_trig and float(prev["close"]) <= up_trig:
        side, boundary = Side.BUY, hi
    elif entry < dn_trig and float(prev["close"]) >= dn_trig:
        side, boundary = Side.SELL, lo
    else:
        return None
    if len(after) and bool(((after["close"] > up_trig) | (after["close"] < dn_trig)).any()):
        return None  # ce n'est plus la première cassure du jour
    if _close_pos(le, side) < 0.65:
        return None
    vol = float(le["tick_volume"]) if pd.notna(le.get("tick_volume")) else float("nan")
    vol_ref = float(or_bars["tick_volume"].mean())
    if not np.isfinite(vol) or not np.isfinite(vol_ref) or vol_ref <= 0 or vol < vol_ref:
        return None
    if _opposed(lt, side):
        return None
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return None
    raw_sl = (lo - 0.1 * atr) if side is Side.BUY else (hi + 0.1 * atr)
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.4, SL_MAX_ATR)
    if sl is None:
        return None
    score = 30.0 + _clamp((1.0 - rng / med) * 60.0, 0, 15)
    pros = [f"opening range {lo:.5g}-{hi:.5g} comprimé : {rng / med:.0%} de son amplitude médiane ({len(hist)} jours)",
            f"première cassure du jour au-delà de la borne + {float(p.get('buffer_ratio', 0.25)):.0%} du range",
            f"volume de cassure ×{vol / vol_ref:.1f} par rapport aux barres du range"]
    cons = ["cassure d'opening range : un retour dans le range invalide immédiatement l'idée"]
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if _close_pos(le, side) >= 0.8:
        score += 10
    if vol >= 1.3 * vol_ref:
        score += 10
    if spread <= 0.06:
        score += 5
    else:
        cons.append(f"spread {spread:.2f} ATR au moment de la cassure")
    note = _sl_note(side, entry, raw_sl, sl, atr)
    if note:
        cons.append(note)
    inv = f"clôture {spec.timeframes.get('entry', 'M15')} de retour à l'intérieur de l'opening range ({lo:.5g}-{hi:.5g})"
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons, inv, bt)
    targets = [boundary + side.sign * rng, boundary + side.sign * 2.0 * rng]
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# --------------------------------------------------------------------------------------------------------------
# G04 — budget d'amplitude de la session en cours (M15 / H1)
# --------------------------------------------------------------------------------------------------------------
@register("G04")
def strategy_g04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """G04 — session_volatility : suivre une session directionnelle tant qu'il lui reste du budget d'amplitude.

    Thèse : chaque session (asiatique, Londres, New York, fin de journée) a une amplitude habituelle. Quand la
    session en cours s'est déjà déplacée dans une seule direction mais n'a consommé qu'une PART de cette
    amplitude habituelle, il reste statistiquement de la place pour prolonger le mouvement ; quand le budget
    est consommé, le même signal n'a plus de réserve et on ne prend pas le trade. La volatilité de la session
    sert à trois choses : filtrer (budget restant), calibrer le stop (amplitude moyenne des barres de la
    session, pas l'ATR14) et fixer l'objectif (amplitude médiane projetée depuis l'ouverture de la session).

    Règles d'entrée : la barre clôturée appartient à une fenêtre de session, au moins 1 h après son début et au
    moins 30 min avant sa fin ; la session compte >= 4 barres clôturées ; la dernière barre clôturée INSCRIT le
    nouvel extrême de la session dans le sens du trade ; déplacement net de la session (clôture − ouverture de
    la session) >= `disp_ratio` × amplitude réalisée de la session (session réellement directionnelle).
    Confirmation : clôture dans les 35 % favorables de la bougie ; tendance H1 non opposée.
    Filtres : >= `min_days` jours d'historique pour la même fenêtre horaire ; amplitude réalisée <= `used_max`
    × amplitude médiane de la session ; budget restant (médiane − réalisé) >= `atr_ratio` × distance de SL ;
    amplitude de la barre <= 2 ATR (on n'entre pas sur une barre de choc) ; spread <= 12 % de l'ATR.
    Logique de SL : `sl_atr` × amplitude MOYENNE des barres de la session en cours, élargi si nécessaire pour
    passer derrière l'extrême des 3 dernières barres de la session ; distance ramenée dans [0,4 ATR ; 3 ATR],
    écart éventuel signalé dans `arguments_against` (`_sl_note`).
    Plan de TP : 1,5 R (partiel), puis ouverture de la session + 0,75 × amplitude médiane, puis + 1 × amplitude
    médiane (le budget d'amplitude de la session est la cible naturelle) ; cible finale = la plus lointaine
    entre `rr` × R et ces projections.
    Invalidation : clôture du tf d'entrée au-delà de la moyenne des clôtures de la session en sens inverse, ou
    amplitude de session dépassant son amplitude médiane historique (budget consommé).
    Score : 30 (session directionnelle avec budget) + 0-15 budget restant + 0-15 alignement MTF + 0-10 netteté
    du déplacement + 10 clôture extrême + 5 spread <= 6 % ATR.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    h = _bar_hour(le)
    if h is None:
        return None
    window = next(((s, en, lab) for s, en, lab in SESSION_WINDOWS if s <= h < en), None)
    if window is None:
        return None
    start_h, end_h, label = window
    if h < start_h + 1.0 or h >= end_h - 0.5:
        return None  # trop tôt (session non formée) ou trop tard (plus le temps de prolonger)
    ses = _today_between(closed, start_h, end_h)
    if len(ses) < 4:
        return None
    s_hi, s_lo = float(ses["high"].max()), float(ses["low"].min())
    realized = s_hi - s_lo
    open_price = float(ses["open"].iloc[0])
    if not np.isfinite(realized) or realized <= 0 or not np.isfinite(open_price):
        return None
    h1 = _frame(snap, "H1")
    if h1 is None:
        return None
    hist = _session_history(_closed(h1), start_h, end_h)
    if len(hist) < int(p.get("min_days", 3)):
        return None
    med = float(np.median(hist))
    if not np.isfinite(med) or med <= 0 or realized > float(p.get("used_max", 0.75)) * med:
        return None
    entry = float(le["close"])
    disp = entry - open_price
    if abs(disp) < float(p.get("disp_ratio", 0.5)) * realized or disp == 0.0:
        return None
    side = Side.BUY if disp > 0 else Side.SELL
    extreme_now = float(le["high"]) >= s_hi if side is Side.BUY else float(le["low"]) <= s_lo
    if not extreme_now:
        return None  # l'extrême de session n'est pas sur la dernière barre clôturée : le mouvement n'est pas en cours
    if _close_pos(le, side) < 0.65 or _range(le) > 2.0 * atr:
        return None
    if _opposed(lt, side):
        return None
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return None
    bar_vol = float((ses["high"] - ses["low"]).mean())
    if not np.isfinite(bar_vol) or bar_vol <= 0:
        return None
    last3 = ses.iloc[-3:]
    struct = float(last3["low"].min()) if side is Side.BUY else float(last3["high"].max())
    raw_sl = entry - side.sign * float(p.get("sl_atr", 1.5)) * bar_vol
    raw_sl = min(raw_sl, struct - 0.1 * bar_vol) if side is Side.BUY else max(raw_sl, struct + 0.1 * bar_vol)
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.4, SL_MAX_ATR)
    if sl is None:
        return None
    dist = abs(entry - sl)
    remaining = med - realized
    if remaining < float(p.get("atr_ratio", 1.2)) * dist:
        return None  # le budget d'amplitude restant ne couvre même pas la distance de stop
    score = 30.0 + _clamp(remaining / med * 30.0, 0, 15)
    pros = [f"session {label} : amplitude réalisée {realized / med:.0%} de sa médiane ({len(hist)} jours), "
            f"budget restant {remaining / med:.0%}",
            f"déplacement net {abs(disp) / realized:.0%} de l'amplitude de session (session directionnelle)",
            "nouvel extrême de session sur la dernière barre clôturée"]
    cons = ["statistique de session : une amplitude médiane passée ne garantit aucune amplitude future"]
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    score += _clamp((abs(disp) / realized - float(p.get("disp_ratio", 0.5))) * 30.0, 0, 10)
    if _close_pos(le, side) >= 0.8:
        score += 10
    if spread <= 0.06:
        score += 5
    else:
        cons.append(f"spread {spread:.2f} ATR en cours de session")
    if _trend_of(lt) == "FLAT":
        cons.append("tendance H1 neutre : le mouvement de session n'est pas soutenu par le tf supérieur")
    note = _sl_note(side, entry, raw_sl, sl, atr)
    if note:
        cons.append(note)
    ses_mean = float(ses["close"].mean())
    inv = (f"clôture {spec.timeframes.get('entry', 'M15')} au-delà de la moyenne des clôtures de la session "
           f"({ses_mean:.5g}) en sens inverse, ou amplitude de session supérieure à {med:.5g}")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons, inv, bt)
    targets = [open_price + side.sign * 0.75 * med, open_price + side.sign * med]
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)
