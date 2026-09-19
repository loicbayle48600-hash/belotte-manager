"""Famille M — spécialistes par symbole : une stratégie propre par agent (M01 … M14).

Chaque agent de cette famille est CALIBRÉ pour un symbole précis (`AgentSpec.markets` : EURUSD, GBPUSD, USDJPY,
XAUUSD, NAS100, US500, GER40, AUDUSD, USDCAD, USOIL) et exploite une particularité de ce marché (heure de
respiration, profil de liquidité, comportement d'ouverture). Le Market Router filtre déjà le symbole, le régime
et la session ; ce module affine avec un filtre de CLASSE d'actif (la mécanique reste valable sur les symboles
voisins du même groupe) et une fenêtre horaire propre à chaque agent.

Conventions communes (voir `agents/screeners.py`, identiques aux familles B, C et D) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est la
  barre en formation et n'est JAMAIS lue (ni close, ni high/low, ni volume) ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14 du
  tf d'entrée, horodatage de la barre clôturée) ;
- `_build` construit le `TradeCandidate` (pénalité de spread 0-15) et refuse tout SL du mauvais côté ; les agents de
  ce module remplacent ensuite le plan de TP générique par leur propre plan (`_set_tp_plan`, `rr >= 1.5` garanti) ;
- le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (côté, stops_level du symbole, 0,25-4 ATR H1) :
  un SL refusé donne `None`, jamais une valeur corrigée à la volée ;
- distance entrée→SL toujours ramenée dans [0,3 ; 3] ATR du tf d'entrée ET au-dessus de 0,3 ATR H1 (`_bound_sl`) ;
  ces bornes PRIMENT sur la lecture graphique : quand le niveau structurel visé par un agent est plus éloigné que
  son plafond (`sl_atr`), le stop réellement placé est plus serré que ce niveau — c'est un choix de risque assumé
  (même convention que la famille B), et le champ `invalidation` décrit l'invalidation ANALYTIQUE du scénario,
  pas la position du stop ; un SL brut du MAUVAIS côté de l'entrée fait en revanche refuser le signal, jamais
  « retourner » le stop ;
- `setup_score` 0-100 = somme documentée de composantes (structure, confirmation, alignement MTF, volatilité,
  qualité d'exécution) : ce n'est PAS une probabilité de gain ;
- données insuffisantes, indicateur NaN, volume absent → `None`, jamais une valeur inventée.

Chaque agent a une mécanique réellement distincte des autres agents du module, des familles déjà écrites (B suivi
de tendance, C cassures, D replis, E retournements, F structure, G volatilité, K news) et du screener générique de
repli (`AgentSpec.base_strategy`) :

| Agent | Symbole | Mécanique propre |
|-------|---------|------------------|
| M01 | EURUSD | cassure de structure laissant une INEFFICIENCE (FVG), entrée sur son retest |
| M02 | EURUSD | VWAP de séance ancré au jour UTC + bandes d'écart-type pondéré |
| M03 | GBPUSD | RETEST tenu de la borne du range asiatique déjà cassée |
| M04 | GBPUSD | balayage de l'extrême de la VEILLE puis réintégration (turtle soup) |
| M05 | USDJPY | score z des clôtures dans la séance asiatique (retour à la moyenne de séance) |
| M06 | USDJPY | ESCALIER de marches monotones + ratio d'efficience de Kaufman |
| M07 | XAUUSD | points PIVOTS journaliers classiques (P, S1/R1, S2/R2) |
| M08 | XAUUSD | balayage de l'extrême du JOUR avec climax de volume puis réintégration |
| M09 | NAS100 | opening range 13:30-14:00 aligné avec le GAP d'ouverture |
| M10 | US500 | RSI(2) survendu/suracheté dans la tendance H1 (repli statistique) |
| M11 | GER40 | PROFIL DE VOLUME de la veille (POC + zone de valeur) |
| M12 | AUDUSD | cassure du range asiatique confirmée par l'OBV (accumulation) |
| M13 | USDCAD | PRESSION DE MÈCHES cumulée (absorption) sur 20 barres |
| M14 | USOIL  | momentum statistiquement extrême (percentile du déplacement 10 barres) |
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import daily_high_low, rsi, session_range, structure_label
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# Bornes de distance entrée→SL (le gate impose 0,25-4 ATR H1 ; on reste plus strict)
SL_MIN_ATR = 0.3        # en ATR du tf d'entrée
SL_MAX_ATR = 3.0        # en ATR du tf d'entrée
SL_MIN_H1_ATR = 0.3     # plancher exprimé en ATR H1 (référence du `TradeCandidate.atr`)
SL_MAX_H1_ATR = 3.5     # plafond en ATR H1 (marge sous les 4 ATR H1 du gate)
STOPS_LEVEL_MARGIN = 1.5  # marge sur le stops_level broker


# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------
def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


def _side_from(tr: str) -> Optional[Side]:
    return Side.BUY if tr == "UP" else Side.SELL if tr == "DOWN" else None


def _hhmm(s: str, default: float) -> float:
    """« HH:MM » → heure décimale UTC ; `default` si le format est invalide."""
    try:
        hh, mm = str(s).strip().split(":")[:2]
        return int(hh) + int(mm) / 60.0
    except (ValueError, AttributeError, TypeError):
        return default


def _bar_hour(row: pd.Series) -> Optional[float]:
    """Heure décimale UTC d'ouverture de la barre (None si l'horodatage est illisible)."""
    try:
        ts = pd.Timestamp(row["time"])
    except (KeyError, TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return float(ts.hour + ts.minute / 60.0)


def _in_window(row: pd.Series, start_h: float, end_h: float) -> bool:
    """Heure de la barre clôturée dans [start_h, end_h) (fenêtre qui peut franchir minuit)."""
    h = _bar_hour(row)
    if h is None:
        return False
    return start_h <= h < end_h if start_h <= end_h else (h >= start_h or h < end_h)


def _today(closed: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées appartenant au jour UTC de la dernière barre clôturée."""
    t = pd.to_datetime(closed["time"], utc=True)
    return closed[t.dt.normalize() == t.iloc[-1].normalize()]


def _prev_day(closed: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées du DERNIER jour UTC antérieur PRÉSENT dans la frame (vide s'il n'y en a pas).

    On ne prend pas « jour courant − 1 jour » : un lundi (ou un lendemain de férié), la veille calendaire ne
    contient aucune barre et l'agent qui s'appuie dessus serait silencieusement mort une séance sur cinq. La
    dernière journée réellement cotée est la référence correcte — et elle reste entièrement CLÔTURÉE.
    """
    t = pd.to_datetime(closed["time"], utc=True)
    days = t.dt.normalize()
    earlier = days[days < days.iloc[-1]]
    if earlier.empty:
        return closed.iloc[:0]
    return closed[days == earlier.iloc[-1]]


def _bars_between(closed: pd.DataFrame, start_h: float, end_h: float) -> pd.DataFrame:
    """Barres clôturées du jour courant dont l'heure d'ouverture est dans [start_h, end_h)."""
    today = _today(closed)
    if today.empty:
        return today
    t = pd.to_datetime(today["time"], utc=True)
    h = t.dt.hour + t.dt.minute / 60.0
    return today[(h >= start_h) & (h < end_h)]


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


def _ohlc_ok(df: pd.DataFrame) -> bool:
    """Vrai si les colonnes OHLC de la fenêtre sont toutes exploitables (aucun NaN, fenêtre non vide)."""
    if df is None or df.empty:
        return False
    return not bool(df[["open", "high", "low", "close"]].isna().to_numpy().any())


def _bar_ok(row: pd.Series) -> bool:
    """Vrai si la barre porte des prix OHLC exploitables (présents, finis, high >= low).

    À vérifier AVANT tout filtre d'anatomie de bougie : en pandas/numpy toute comparaison avec NaN est fausse,
    donc un test du type `close <= open`, `_close_pos(...) < 0.5` ou `_wick_against(...) < 0.35` sur une barre
    incomplète ne rejette RIEN et laisse passer un signal dont la confirmation n'a jamais été vérifiée.
    `_ctx` ne contrôle que les colonnes d'indicateurs, pas les prix.
    """
    for col in ("open", "high", "low", "close"):
        if col not in row:
            return False
        try:
            v = float(row[col])
        except (TypeError, ValueError):
            return False
        if not np.isfinite(v):
            return False
    return float(row["high"]) >= float(row["low"])


def _volumes(df: pd.DataFrame) -> Optional[np.ndarray]:
    """Volumes de tick de la fenêtre (None si la colonne est absente, NaN ou nulle : aucune valeur inventée)."""
    if "tick_volume" not in df.columns or df.empty:
        return None
    v = df["tick_volume"].to_numpy(dtype=float)
    if not np.isfinite(v).all() or v.sum() <= 0:
        return None
    return v


def _atr_h1(snap) -> float:
    """ATR H1 du snapshot, ramené à 0.0 s'il est absent, nul, négatif ou non fini.

    `float(snap.atr_h1 or 0.0)` vaut NaN quand `atr_h1` est NaN (NaN est « vrai ») : la valeur se propageait
    alors dans les filtres, où toute comparaison avec NaN est fausse — le filtre concerné était donc
    silencieusement désactivé. On normalise ici une bonne fois pour toutes.
    """
    try:
        v = float(snap.atr_h1)
    except (TypeError, ValueError):
        return 0.0
    return v if np.isfinite(v) and v > 0 else 0.0


def _spread_ratio_h1(snap) -> float:
    """Spread courant en fraction de l'ATR H1 (référence du gate : <= 0,15 ATR H1) ; inf si l'ATR H1 est inconnu.

    Un ATR H1 nul, absent, infini ou NaN renvoie `inf` : le filtre de spread REFUSE le signal au lieu de laisser
    passer une comparaison avec NaN (toujours fausse) qui le désactiverait pour TOUS les agents du module.
    """
    atr_h1 = _atr_h1(snap)
    if atr_h1 <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points) * float(snap.spec.point) / atr_h1


def _class_ok(snap, *classes: str) -> bool:
    """Classe d'actif du symbole compatible avec l'agent (le routeur filtre déjà le symbole exact)."""
    if snap.spec is None:
        return False
    return str(getattr(snap.spec, "asset_class", "")) in classes


def _bias(row: pd.Series) -> str:
    """Biais du timeframe supérieur, volontairement plus souple que `_trend_of` (qui exige EMA20>EMA50>EMA200).

    "UP" : clôture au-dessus de l'EMA50 ET EMA20 au-dessus de l'EMA50 ; "DOWN" : miroir ; "FLAT" sinon.
    Utilisé par les agents dont le niveau d'entrée vient d'une grille externe (pivots, profil de volume) : exiger
    l'empilement complet des trois EMA rejetterait la quasi-totalité des tests de niveau.
    """
    if not _valid(row, "ema20", "ema50", "close"):
        return "FLAT"
    if row["close"] > row["ema50"] and row["ema20"] > row["ema50"]:
        return "UP"
    if row["close"] < row["ema50"] and row["ema20"] < row["ema50"]:
        return "DOWN"
    return "FLAT"


def _hard_opposed(lt: pd.Series, side: Side, adx_max: float = 30.0) -> bool:
    """Veto du timeframe supérieur : tendance EMA franchement opposée ET ADX >= `adx_max` (contre-courant trop cher)."""
    tr = _trend_of(lt)
    opposed = (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP")
    if not opposed:
        return False
    return not _valid(lt, "adx14") or float(lt["adx14"]) >= adx_max


def _bound_sl(snap, side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> Optional[float]:
    """Ramène la distance entrée→SL dans [max(lo·ATR, 0,3·ATR, 0,3·ATR H1) ; min(hi, 3)·ATR] sans changer de côté.

    Renvoie None si la borne basse dépasse la borne haute (bornes incohérentes) ET si le SL brut est du MAUVAIS
    côté de l'entrée : dans ce dernier cas le niveau structurel proposé par l'agent est faux, et le « ramener »
    du bon côté reviendrait à inventer un stop que l'analyse ne justifie pas. On refuse le signal.
    """
    if sl is None or not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
        return None
    if side.sign * (entry - sl) <= 0:
        return None
    atr_h1 = _atr_h1(snap)
    min_broker = float(getattr(snap.spec, "min_stop_distance", 0.0) or 0.0) if snap.spec is not None else 0.0
    lo_dist = max(lo * atr, SL_MIN_ATR * atr, SL_MIN_H1_ATR * atr_h1, STOPS_LEVEL_MARGIN * min_broker)
    hi_dist = min(min(hi, SL_MAX_ATR) * atr, SL_MAX_H1_ATR * atr_h1 if atr_h1 > 0 else float("inf"))
    if lo_dist > hi_dist:
        return None
    dist = min(max(abs(entry - sl), lo_dist), hi_dist)
    return entry - side.sign * dist


def _set_tp_plan(c: Optional[TradeCandidate], side: Side, entry: float, targets: list[float], rr_min: float,
                 first_r: float = 1.5) -> Optional[TradeCandidate]:
    """Remplace le plan de TP générique de `_build` par un plan propre à l'agent.

    - `targets` : cibles structurelles (niveaux, projections) ; seules celles situées à >= 0,5 R et <= 6 R dans le
      sens du trade sont retenues ;
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
    pts = [float(x) for x in targets
           if x is not None and np.isfinite(x) and 0.5 * dist <= sign * (x - entry) <= 6.0 * dist]
    final = entry + sign * dist * rr_min
    if pts:
        far = max(pts, key=lambda x: sign * (x - entry))
        if sign * (far - entry) > sign * (final - entry):
            final = far
    final = round(final, 10)
    first = round(entry + sign * dist * first_r, 10)
    plan = sorted({first, *[round(x, 10) for x in pts], final}, key=lambda x: sign * (x - entry))
    plan = [x for x in plan if sign * (x - entry) <= sign * (final - entry) + 1e-9]
    if not plan:
        return None
    if len(plan) > 3:
        plan = [plan[0], plan[len(plan) // 2], plan[-1]]
    c.tp_plan = [float(x) for x in plan]
    c.rr = round(abs(c.tp_plan[-1] - entry) / dist, 2)
    return c


def _finalize(c: Optional[TradeCandidate], snap) -> Optional[TradeCandidate]:
    """Revalidation déterministe du SL (côté, stops_level, 0,25-4 ATR H1) : refus → None, jamais de correction.

    Un ATR H1 non exploitable (absent, nul, NaN) fait AUSSI refuser le candidat : `validate_stop_loss` ignore
    silencieusement ses bornes en ATR quand l'ATR vaut NaN, et le `TradeCandidate.atr` publié servirait ensuite
    au dimensionnement du risque. Mieux vaut aucun signal qu'un signal dont le risque n'est pas mesurable.
    """
    if c is None:
        return None
    atr_h1 = _atr_h1(snap)
    if atr_h1 <= 0 or not np.isfinite(float(c.atr or 0.0)) or float(c.atr or 0.0) <= 0:
        return None
    chk = validate_stop_loss(c.side, c.entry, c.sl, snap.spec, atr=float(c.atr))
    return c if chk.ok else None


def _session_vwap(bars: pd.DataFrame) -> Optional[tuple[float, float]]:
    """VWAP de séance et écart-type pondéré par le volume sur les barres fournies.

    Prix typique = (haut + bas + clôture)/3 pondéré par `tick_volume`. None si le volume est absent/nul ou si la
    dispersion n'est pas calculable (aucune valeur de repli inventée).
    """
    v = _volumes(bars)
    if v is None or len(bars) < 6 or not _ohlc_ok(bars):
        return None
    tp = (bars["high"].to_numpy(dtype=float) + bars["low"].to_numpy(dtype=float) + bars["close"].to_numpy(dtype=float)) / 3.0
    total = float(v.sum())
    vwap = float((tp * v).sum() / total)
    var = float((v * (tp - vwap) ** 2).sum() / total)
    if not np.isfinite(vwap) or not np.isfinite(var) or var <= 0:
        return None
    return vwap, float(np.sqrt(var))


def _obv(bars: pd.DataFrame) -> Optional[np.ndarray]:
    """On-Balance Volume sur les barres fournies (0 au départ de la fenêtre). None si le volume est indisponible."""
    v = _volumes(bars)
    if v is None or len(bars) < 10:
        return None
    close = bars["close"].to_numpy(dtype=float)
    if not np.isfinite(close).all():
        return None
    step = np.sign(np.diff(close, prepend=close[0])) * v
    step[0] = 0.0
    return np.cumsum(step)


def _volume_profile(bars: pd.DataFrame, bins: int = 24, value_area: float = 0.70) -> Optional[tuple[float, float, float]]:
    """Profil de volume d'une journée : (POC, borne haute de la zone de valeur, borne basse).

    Chaque barre verse son `tick_volume` dans le casier de son prix typique ; la zone de valeur est le plus petit
    ensemble de casiers CONTIGUS autour du POC contenant `value_area` du volume. None si données insuffisantes.
    """
    v = _volumes(bars)
    if v is None or len(bars) < 20 or not _ohlc_ok(bars):
        return None
    lo, hi = float(bars["low"].min()), float(bars["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None
    tp = (bars["high"].to_numpy(dtype=float) + bars["low"].to_numpy(dtype=float) + bars["close"].to_numpy(dtype=float)) / 3.0
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.searchsorted(edges, tp, side="right") - 1, 0, bins - 1)
    hist = np.zeros(bins, dtype=float)
    np.add.at(hist, idx, v)
    total = float(hist.sum())
    if total <= 0:
        return None
    centers = (edges[:-1] + edges[1:]) / 2.0
    poc_i = int(np.argmax(hist))
    lo_i = hi_i = poc_i
    acc = float(hist[poc_i])
    while acc < value_area * total and (lo_i > 0 or hi_i < bins - 1):
        down = float(hist[lo_i - 1]) if lo_i > 0 else -1.0
        up = float(hist[hi_i + 1]) if hi_i < bins - 1 else -1.0
        if up >= down:
            hi_i += 1
            acc += float(hist[hi_i])
        else:
            lo_i -= 1
            acc += float(hist[lo_i])
    return float(centers[poc_i]), float(edges[hi_i + 1]), float(edges[lo_i])


def _stair_run(v: np.ndarray, tol: float) -> int:
    """Longueur, en barres, de la série FINALE de valeurs non décroissantes à `tol` près (>= 1).

    `_stair_run(lows, tol)` compte les « marches » d'un escalier haussier (chaque plus bas reste au-dessus du
    précédent, à `tol` près) ; `_stair_run(-highs, tol)` fait la même chose pour un escalier baissier.
    Une valeur NaN casse la série (aucune marche supposée).
    """
    k = 1
    i = len(v) - 1
    while i > 0 and np.isfinite(v[i]) and np.isfinite(v[i - 1]) and v[i] >= v[i - 1] - tol:
        k += 1
        i -= 1
    return k


def _efficiency(closes: np.ndarray) -> Optional[float]:
    """Ratio d'efficience de Kaufman : |déplacement net| / longueur du chemin parcouru, dans [0, 1].

    1 = ligne droite (chaque barre avance dans le même sens), 0 = aller-retour stérile. None si le chemin est
    nul ou non fini (aucune valeur de repli inventée).
    """
    if len(closes) < 3 or not np.isfinite(closes).all():
        return None
    path = float(np.abs(np.diff(closes)).sum())
    if path <= 0:
        return None
    return float(abs(closes[-1] - closes[0]) / path)


def _wick_pressure(bars: pd.DataFrame) -> Optional[tuple[float, np.ndarray, np.ndarray]]:
    """Pression de mèches cumulée d'une fenêtre : (déséquilibre normalisé, mèches basses, mèches hautes).

    Mèche basse = min(open, close) − bas (absorption acheteuse), mèche haute = haut − max(open, close). Le
    déséquilibre vaut (Σ basses − Σ hautes) / (Σ basses + Σ hautes) ∈ [−1, +1]. None si la fenêtre est vide,
    incomplète ou entièrement sans mèche (division par zéro).
    """
    if bars is None or len(bars) < 10 or not _ohlc_ok(bars):
        return None
    o = bars["open"].to_numpy(dtype=float)
    h = bars["high"].to_numpy(dtype=float)
    lo = bars["low"].to_numpy(dtype=float)
    c = bars["close"].to_numpy(dtype=float)
    lower = np.maximum(np.minimum(o, c) - lo, 0.0)
    upper = np.maximum(h - np.maximum(o, c), 0.0)
    total = float(lower.sum() + upper.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    return float((lower.sum() - upper.sum()) / total), lower, upper


# ==============================================================================================================
# M01 — eurusd_london_bos (M15 / H1, LONDON & OVERLAP)
# ==============================================================================================================
@register("M01")
def strategy_m01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M01 — EURUSD : cassure de structure qui laisse une INEFFICIENCE (FVG), entrée sur son retest.

    Thèse : sur EURUSD le matin, une cassure de structure n'est fiable que si elle est produite par un
    déplacement assez violent pour laisser un trou de cotation (fair value gap : le bas de la barre suivante
    reste au-dessus du haut de la barre précédente). Ce trou est une zone d'ordres non exécutés ; le marché y
    revient souvent une fois, et c'est ce RETOUR — pas la cassure elle-même — qui offre un stop court.

    Entrée : parmi les barres clôturées n−10 … n−3, la plus récente barre de déplacement qui (a) clôture au-delà
    du plus haut (bas) des 10 barres qui la précèdent — cassure de structure —, (b) a un corps >= 0,5 ATR et
    (c) laisse une inefficience de >= 0,15 ATR entre la barre d'avant et la barre d'après. Aucune barre depuis
    n'a clôturé au-delà de la base de l'inefficience. La barre de signal entre dans l'inefficience (son extrême
    la touche) et clôture au-dessus (sous) de son milieu, dans le sens du déplacement.
    Confirmation : bougie de signal dans le sens du trade et clôture toujours du bon côté du niveau cassé.
    Filtres : classe forex ; heure de la barre dans [7h ; 16h) UTC ; tendance H1 non franchement opposée ;
    spread <= 12 % de l'ATR H1.
    SL : sous (au-dessus) la BASE de l'inefficience − 0,25 ATR — si le trou est intégralement comblé, la lecture
    est fausse ; distance bornée à [0,4 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), projection du déplacement (inefficience + hauteur du déplacement), extrême de la veille ;
    cible finale = max(`rr` R, cible structurelle la plus lointaine).
    Invalidation : clôture M15 au-delà de la base de l'inefficience.
    Score : 25 (cassure + inefficience valide) + 0-10 corps du déplacement + 0-10 fraîcheur du trou
    + 15 MTF (`_mtf_bonus`) + 5 structure M15 alignée + 0-10 qualité de la clôture de signal.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or not _ohlc_ok(closed.iloc[-30:]):
        return None
    if not _in_window(le, 7.0, 16.0):
        return None
    highs = closed["high"].to_numpy(dtype=float)
    lows = closed["low"].to_numpy(dtype=float)
    opens = closed["open"].to_numpy(dtype=float)
    closes = closed["close"].to_numpy(dtype=float)
    found = None  # (side, indice du déplacement, base du trou, sommet du trou, niveau cassé)
    for i in range(n - 3, n - 11, -1):
        if i - 10 < 0:
            break
        body = abs(closes[i] - opens[i])
        if body < 0.5 * atr:
            continue
        up_level = float(highs[i - 10:i].max())
        dn_level = float(lows[i - 10:i].min())
        if closes[i] > opens[i] and closes[i] > up_level and lows[i + 1] - highs[i - 1] >= 0.15 * atr:
            found = (Side.BUY, i, float(highs[i - 1]), float(lows[i + 1]), up_level)
            break
        if closes[i] < opens[i] and closes[i] < dn_level and lows[i - 1] - highs[i + 1] >= 0.15 * atr:
            found = (Side.SELL, i, float(lows[i - 1]), float(highs[i + 1]), dn_level)
            break
    if found is None:
        return None
    side, i, gap_base, gap_top, level = found
    s = side.sign
    after = closed.iloc[i + 2:]  # barres postérieures à l'inefficience, barre de signal comprise
    if len(after) < 1 or (s * (after["close"] - gap_base) <= 0).any():
        return None  # trou intégralement comblé en clôture : la lecture est invalidée
    touched = float(le["low"]) <= gap_top if side is Side.BUY else float(le["high"]) >= gap_top
    mid = (gap_base + gap_top) / 2.0
    entry = float(le["close"])
    if not touched or s * (entry - mid) <= 0 or s * (entry - float(le["open"])) <= 0 or s * (entry - level) <= 0:
        return None
    if _hard_opposed(lt, side, 30.0) or _spread_ratio_h1(snap) > 0.12:
        return None
    sl = _bound_sl(snap, side, entry, gap_base - s * 0.25 * atr, atr, 0.4, 2.0 * float(p.get("sl_atr", 1.0)))
    if sl is None:
        return None
    age = n - 1 - i
    score = 25.0 + _clamp((abs(closes[i] - opens[i]) / atr - 0.5) * 20, 0, 10) + _clamp((10 - age) * 1.5, 0, 10)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"cassure de structure par une barre de déplacement (corps {abs(closes[i] - opens[i]) / atr:.1f} ATR)",
             f"inefficience non comblée entre {min(gap_base, gap_top):.5g} et {max(gap_base, gap_top):.5g}",
             f"retour dans l'inefficience {age} barre(s) après le déplacement"]
    cons: list[str] = []
    if structure_label(closed) == ("HH_HL" if side is Side.BUY else "LH_LL"):
        score += 5
        pros.append("structure M15 alignée")
    else:
        cons.append("structure M15 non confirmée par les swings")
    cp = _close_pos(le, side)
    score += _clamp(cp * 10, 0, 10)
    if cp < 0.5:
        cons.append("clôture dans la moitié défavorable de la barre de retest")
    cons.append("un retour dans une inefficience peut se poursuivre jusqu'à son comblement total")
    d1 = snap.frames.get("D1")
    targets = [gap_top + s * abs(closes[i] - gap_base)]
    if d1 is not None and len(d1) >= 3:
        ph, pl = daily_high_low(d1)
        targets.append(ph if side is Side.BUY else pl)
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "clôture M15 au-delà de la base de l'inefficience (FVG comblée)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.5))), snap)


# ==============================================================================================================
# M02 — eurusd_ny_pullback (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M02")
def strategy_m02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M02 — EURUSD : repli sur le VWAP de séance ancré au jour UTC, avec bandes d'écart-type pondéré.

    Thèse : l'après-midi, le prix moyen pondéré par le volume depuis 00:00 UTC est la référence des exécutions
    institutionnelles. En tendance H1, un retour du prix sur ce VWAP qui est immédiatement racheté (clôture de
    nouveau au-dessus) est un repli sur la valeur, pas un retournement — à la différence d'un repli sur une EMA,
    le niveau est ici pondéré par le VOLUME réellement traité dans la journée.

    Entrée : tendance H1 UP/DOWN ; au moins 12 barres clôturées dans le jour UTC courant ; VWAP de séance et
    écart-type pondéré calculables (volume disponible) ; la barre de signal touche la bande [VWAP − 0,25 σ ;
    VWAP + 0,25 σ] par son extrême et clôture du bon côté du VWAP, dans le sens du trade.
    Confirmation : RSI14 M15 dans [`rsi_lo`, `rsi_hi`] (bande miroir pour une vente) ; la barre précédente était
    déjà du bon côté du VWAP (repli, pas cassure).
    Filtres : classe forex ; heure de la barre dans [12h ; 21h) UTC ; clôture à moins de 1,5 σ du VWAP (pas
    d'entrée déjà étendue) ; spread <= 12 % de l'ATR H1.
    SL : au-delà de la bande opposée VWAP − 1,2 σ (le VWAP perdu invalide la lecture de séance), au minimum
    derrière l'extrême de la barre de signal ; distance bornée à [0,5 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), VWAP + 2 σ, extrême du jour ; cible finale = max(`rr` R, cible la plus lointaine).
    Invalidation : clôture M15 au-delà de VWAP − 1 σ (côté opposé au trade).
    Score : 20 (repli sur VWAP racheté) + 0-10 proximité du VWAP + 0-10 momentum RSI + 15 MTF + 0-10 volume de
    la barre de signal + 0-10 qualité de la clôture.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _in_window(le, 12.0, 21.0):
        return None
    side = _side_from(_trend_of(lt))
    if side is None:
        return None
    day = _today(closed)
    if len(day) < 12:
        return None
    vw = _session_vwap(day)
    if vw is None:
        return None
    vwap, sd = vw
    s = side.sign
    entry = float(le["close"])
    prev = closed.iloc[-2]
    touch = float(le["low"]) <= vwap + 0.25 * sd if side is Side.BUY else float(le["high"]) >= vwap - 0.25 * sd
    if not touch or s * (entry - vwap) <= 0 or s * (entry - float(le["open"])) <= 0:
        return None
    if s * (float(prev["close"]) - vwap) <= 0:
        return None  # la barre précédente était déjà du mauvais côté : ce n'est pas un repli
    if s * (entry - vwap) > 1.5 * sd:
        return None  # entrée déjà étendue par rapport au prix moyen de la séance
    lo_r, hi_r = float(p.get("rsi_lo", 38)), float(p.get("rsi_hi", 62))
    rsi_v = float(le["rsi14"])
    band = (lo_r, hi_r) if side is Side.BUY else (100.0 - hi_r, 100.0 - lo_r)
    if not (band[0] <= rsi_v <= band[1]):
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    raw = min(vwap - 1.2 * sd, float(le["low"])) if side is Side.BUY else max(vwap + 1.2 * sd, float(le["high"]))
    sl = _bound_sl(snap, side, entry, raw, atr, 0.5, 2.0 * float(p.get("sl_atr", 1.2)))
    if sl is None:
        return None
    dev = abs(entry - vwap) / sd
    score = 20.0 + _clamp((1.5 - dev) * 8, 0, 10)
    score += _clamp(10 - abs(rsi_v - 50) * 0.5, 0, 10)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"repli sur le VWAP de séance ({vwap:.5g}) racheté en clôture",
             f"clôture à {dev:.2f} σ du VWAP (σ pondéré volume = {sd:.5g})", f"RSI {rsi_v:.0f} dans la bande de repli"]
    cons: list[str] = []
    vols = _volumes(day)
    if vols is not None and len(vols) >= 6:
        ratio = float(vols[-1] / np.mean(vols[:-1])) if float(np.mean(vols[:-1])) > 0 else 0.0
        score += _clamp((ratio - 0.8) * 12, 0, 10)
        if ratio < 0.8:
            cons.append(f"volume de la barre de signal faible ({ratio:.1f}× la moyenne de séance)")
        else:
            pros.append(f"volume de signal {ratio:.1f}× la moyenne de séance")
    cp = _close_pos(le, side)
    score += _clamp(cp * 10, 0, 10)
    if cp < 0.5:
        cons.append("clôture dans la moitié défavorable de la barre")
    cons.append("le VWAP de séance perd sa signification en fin de journée (volume déjà consommé)")
    targets = [vwap + s * 2.0 * sd, float(day["high"].max()) if side is Side.BUY else float(day["low"].min())]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de VWAP ∓ 1 σ (côté opposé au trade)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M03 — gbpusd_london_breakout (M15 / H1, LONDON)
# ==============================================================================================================
@register("M03")
def strategy_m03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M03 — GBPUSD : RETEST tenu de la borne du range asiatique déjà cassée (jamais la cassure elle-même).

    Thèse : GBPUSD produit beaucoup de fausses cassures à l'ouverture de Londres. On laisse donc passer la
    cassure du range asiatique et on n'entre que si la borne cassée est revenue se faire tester et l'a TENUE en
    clôture : la polarité s'est inversée (ancienne résistance devenue support) et le stop tient sous la borne.

    Entrée : range de la session `session` (`start`-`end`, 00:00-07:00 par défaut) valide ; une barre clôturée du
    jour, postérieure à la fin de session et antérieure à la barre de signal, a clôturé au-delà de la borne
    (cassure) ; depuis, AUCUNE clôture n'est revenue à l'intérieur du range, et l'extrême d'au moins une barre est
    redescendu à moins de 0,35 ATR de la borne (le retest a bien eu lieu).
    Confirmation : la barre de signal clôture au-delà de la borne ET au-delà du plus haut (bas) de la barre
    précédente, dans le sens de la cassure.
    Filtres : classe forex ; heure de la barre dans [fin de session ; fin + 6 h) ; largeur du range entre 0,5 et
    4 ATR H1 ; entrée à moins de 1,2 ATR de la borne (pas de poursuite) ; spread <= 12 % de l'ATR H1.
    SL : sous (au-dessus) le plus bas (haut) du retest − 0,25 ATR, jamais au-delà de la borne du range ;
    distance bornée à [0,4 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), projection de la hauteur du range depuis la borne, extrême de la veille.
    Invalidation : clôture M15 de retour à l'intérieur du range asiatique.
    Score : 25 (cassure + retest tenu) + 0-10 profondeur du retest + 0-10 range compact + 15 MTF
    + 0-10 corps de la barre de signal + 5 entrée proche de la borne.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60:
        return None
    hi, lo = session_range(closed, str(p.get("start", "00:00")), str(p.get("end", "07:00")))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return None
    atr_h1 = float(snap.atr_h1 or 0.0)
    rng = float(hi - lo)
    if atr_h1 <= 0 or not (0.5 * atr_h1 <= rng <= 4.0 * atr_h1):
        return None
    end_h = _hhmm(p.get("end", "07:00"), 7.0)
    if not _in_window(le, end_h, min(end_h + 6.0, 23.99)):
        return None
    after = _bars_between(closed, end_h, 24.0)
    if len(after) < 3 or not _ohlc_ok(after):
        return None
    prior = after.iloc[:-1]  # barres postérieures à la session, barre de signal exclue
    entry = float(le["close"])
    prev = closed.iloc[-2]
    if entry > hi and bool((prior["close"] > hi).any()):
        side, boundary = Side.BUY, float(hi)
    elif entry < lo and bool((prior["close"] < lo).any()):
        side, boundary = Side.SELL, float(lo)
    else:
        return None
    s = side.sign
    brk = int(np.argmax((prior["close"].to_numpy(dtype=float) * s > boundary * s)))
    since = prior.iloc[brk + 1:]
    if len(since) < 1:
        return None  # la cassure vient d'avoir lieu : pas encore de retest
    if (s * (since["close"] - boundary) <= 0).any():
        return None  # une clôture est revenue dans le range : la borne n'a pas tenu
    retest = float(since["low"].min()) if side is Side.BUY else float(since["high"].max())
    if s * (retest - boundary) > 0.35 * atr:
        return None  # le prix n'est jamais revenu tester la borne
    if s * (entry - float(prev["high" if side is Side.BUY else "low"])) <= 0:
        return None
    if s * (entry - float(le["open"])) <= 0 or s * (entry - boundary) > 1.2 * atr:
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    raw = min(retest, boundary) - 0.25 * atr if side is Side.BUY else max(retest, boundary) + 0.25 * atr
    sl = _bound_sl(snap, side, entry, raw, atr, 0.4, 2.0 * float(p.get("sl_atr", 1.0)))
    if sl is None:
        return None
    depth = abs(retest - boundary) / atr
    score = 25.0 + _clamp((0.35 - depth) * 25, 0, 10)
    if rng <= 2.0 * atr_h1:
        score += 10
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"range de session {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1) cassé puis retesté",
             f"borne tenue en clôture sur {len(since)} barre(s), retest à {depth:.2f} ATR",
             "clôture au-delà de l'extrême de la barre précédente"]
    cons: list[str] = []
    if rng > 2.0 * atr_h1:
        cons.append("range asiatique large : objectif de projection éloigné")
    body = _body_ratio(le)
    score += _clamp(body * 10, 0, 10)
    if body < 0.4:
        cons.append(f"corps de la barre de signal étroit ({body:.0%} de l'amplitude)")
    ext = s * (entry - boundary) / atr
    if ext <= 0.5:
        score += 5
        pros.append("entrée proche de la borne cassée")
    else:
        cons.append(f"entrée {ext:.1f} ATR au-delà de la borne")
    d1 = snap.frames.get("D1")
    targets = [boundary + s * rng]
    if d1 is not None and len(d1) >= 3:
        ph, pl = daily_high_low(d1)
        targets.append(ph if side is Side.BUY else pl)
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 de retour à l'intérieur du range asiatique", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M04 — gbpusd_ny_reversal (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M04")
def strategy_m04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M04 — GBPUSD : balayage de l'extrême de la VEILLE puis réintégration (turtle soup).

    Thèse : le plus haut (bas) de la veille est le stop évident des positions intrajournalières. Quand le prix
    le dépasse en séance américaine mais que la barre clôture À L'INTÉRIEUR de la journée précédente, la sortie
    est un balayage de liquidité et non une continuation : le déséquilibre est déjà résorbé.

    Entrée : plus haut / plus bas de la VEILLE disponibles (frame D1) ; aucune barre du jour n'avait clôturé
    au-delà du niveau avant le balayage (niveau encore intact) ; la barre de signal ou la précédente dépasse le
    niveau par son extrême, et la barre de signal clôture en deçà, contre le balayage.
    Confirmation : mèche de la BARRE DE BALAYAGE (celle qui est allée le plus loin au-delà du niveau, signal ou
    précédente) >= 35 % de son amplitude, et clôture de la barre de signal dans sa moitié favorable ;
    RSI14 M15 >= 50 pour une vente (<= 50 pour un achat) — le mouvement balayé doit avoir été acheté (vendu).
    Filtres : classe forex ; heure de la barre dans [12h ; 21h) UTC ; veto si la tendance H1 est franchement
    opposée avec ADX >= 32 (contre-courant trop cher) ; spread <= 12 % de l'ATR H1.
    SL : au-delà de l'extrême du balayage + 0,3 ATR (si le niveau est repris, la lecture est fausse) ; distance
    bornée à [0,4 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), milieu de la journée de la veille, extrême opposé de la veille.
    Invalidation : clôture M15 au-delà de l'extrême de la veille balayé.
    Score : 25 (balayage + réintégration) + 0-10 mèche de rejet + 0-10 excès du balayage + 0-10 RSI
    + 10 tendance H1 non opposée + 0-10 qualité de la clôture.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    d1 = snap.frames.get("D1")
    if len(closed) < 60 or d1 is None or len(d1) < 3 or not _in_window(le, 12.0, 21.0):
        return None
    pdh, pdl = daily_high_low(d1)
    if not (np.isfinite(pdh) and np.isfinite(pdl)) or pdh <= pdl:
        return None
    prev = closed.iloc[-2]
    entry = float(le["close"])
    day = _today(closed)
    if len(day) < 4 or not _ohlc_ok(day):
        return None
    earlier = day.iloc[:-2] if len(day) > 2 else day.iloc[:0]
    if not _bar_ok(prev):
        return None
    if max(float(le["high"]), float(prev["high"])) > pdh and entry < pdh:
        side, level = Side.SELL, float(pdh)
        # la barre de balayage est celle qui est allée le plus loin au-delà du niveau : c'est SA mèche qui
        # mesure le rejet (mesurer celle de la barre de signal laissait passer un balayage sans rejet quand
        # le dépassement avait eu lieu sur la barre précédente)
        bar = le if float(le["high"]) >= float(prev["high"]) else prev
        sweep = float(bar["high"])
        if len(earlier) and bool((earlier["close"] > pdh).any()):
            return None  # le niveau était déjà dépassé en clôture : ce n'est plus un balayage
    elif min(float(le["low"]), float(prev["low"])) < pdl and entry > pdl:
        side, level = Side.BUY, float(pdl)
        bar = le if float(le["low"]) <= float(prev["low"]) else prev
        sweep = float(bar["low"])
        if len(earlier) and bool((earlier["close"] < pdl).any()):
            return None
    else:
        return None
    s = side.sign
    if s * (entry - float(le["open"])) <= 0:
        return None
    wick = _wick_against(bar, side)
    if wick < 0.35 or _close_pos(le, side) < 0.5:
        return None
    rsi_v = float(le["rsi14"])
    if (side is Side.SELL and rsi_v < 50) or (side is Side.BUY and rsi_v > 50):
        return None
    if _hard_opposed(lt, side, 32.0) or _spread_ratio_h1(snap) > 0.12:
        return None
    sl = _bound_sl(snap, side, entry, sweep - s * 0.3 * atr, atr, 0.4, 2.0 * float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    excess = abs(sweep - level) / atr
    score = 25.0 + _clamp(wick * 20, 0, 10) + _clamp((0.8 - excess) * 12, 0, 10)
    score += _clamp(abs(rsi_v - 50) * 0.4, 0, 10)
    pros = [f"balayage de l'extrême de la veille ({level:.5g}) puis clôture à l'intérieur",
            f"mèche de rejet {wick:.0%} de l'amplitude", f"excès du balayage {excess:.2f} ATR",
            f"RSI {rsi_v:.0f} cohérent avec un épuisement"]
    cons = ["trade à contre-courant du mouvement qui vient de casser le niveau"]
    if not _hard_opposed(lt, side, 0.0):
        score += 10
        pros.append("tendance H1 non opposée au retournement")
    else:
        cons.append("tendance H1 opposée : retournement contre le timeframe supérieur")
    cp = _close_pos(le, side)
    score += _clamp((cp - 0.5) * 20, 0, 10)
    if excess > 0.8:
        cons.append("balayage profond : le niveau peut être réellement cassé")
    targets = [(pdh + pdl) / 2.0, pdl if side is Side.SELL else pdh]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la veille balayé", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M05 — usdjpy_asia_range (M15 / H1, ASIA)
# ==============================================================================================================
@register("M05")
def strategy_m05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M05 — USDJPY : score z des clôtures DANS la séance asiatique (retour à la moyenne de séance).

    Thèse : de 00:00 à 08:00 UTC, USDJPY oscille le plus souvent autour d'un prix d'équilibre propre à la séance.
    L'écart à ce prix se mesure mieux avec la dispersion OBSERVÉE DEPUIS LE DÉBUT DE SÉANCE qu'avec des bandes de
    Bollinger calculées sur 20 barres glissantes : la référence est la journée elle-même, pas la fenêtre.

    Entrée : au moins 10 barres clôturées depuis 00:00 UTC ; score z de la clôture (moyenne et écart-type des
    clôtures de la séance) : la barre PRÉCÉDENTE est en excès (z <= −`z_in` pour un achat, +`z_in` pour une
    vente, `z_in` = 1,8) et la barre de signal revient vers la moyenne (z en amélioration d'au moins 0,2 et
    clôture dans le sens du trade).
    Confirmation : RSI14 M15 dans la moitié basse (<= 50) pour un achat, haute (>= 50) pour une vente — le score z
    mesure déjà l'excès, le RSI ne sert que de garde-fou (on n'achète pas un excès HAUSSIER) et rapporte d'autant
    plus de points qu'il approche `rsi_lo` / `rsi_hi` ; mèche de rejet >= 25 % de l'amplitude.
    Filtres : classe forex ; heure de la barre dans [1h ; 9h) UTC ; ADX14 M15 <= 30 (aucun retour à la moyenne
    dans une tendance forte) ; tendance H1 non franchement opposée (ADX H1 >= 30) ; spread <= 12 % de l'ATR H1.
    SL : au-delà de l'extrême de séance (plus bas pour un achat) − 0,25 ATR : si la séance fait un nouvel extrême,
    l'équilibre est rompu ; distance bornée à [0,4 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), moyenne de séance (cible naturelle), bande opposée à 1,5 σ.
    Invalidation : clôture M15 au-delà de l'extrême de la séance asiatique.
    Score : 20 (excès mesuré + retour) + 0-15 amplitude de l'excès + 0-10 RSI (10 points à `rsi_lo`/`rsi_hi`)
    + 0-10 mèche de rejet
    + 10 absence de tendance M15 + 0-10 distance à la moyenne (espace de cible).
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _in_window(le, 1.0, 9.0):
        return None
    day = _bars_between(closed, 0.0, 24.0)
    if len(day) < 10 or not _ohlc_ok(day):
        return None
    cl = day["close"].to_numpy(dtype=float)
    mean, sd = float(cl.mean()), float(cl.std(ddof=0))
    if not np.isfinite(mean) or sd <= 0:
        return None
    z_now, z_prev = (cl[-1] - mean) / sd, (cl[-2] - mean) / sd
    z_in = 1.8
    if z_prev <= -z_in and z_now > z_prev + 0.2:
        side = Side.BUY
    elif z_prev >= z_in and z_now < z_prev - 0.2:
        side = Side.SELL
    else:
        return None
    s = side.sign
    entry = float(le["close"])
    if s * (entry - float(le["open"])) <= 0:
        return None
    rsi_v = float(le["rsi14"])
    lo_r, hi_r = float(p.get("rsi_lo", 30)), float(p.get("rsi_hi", 70))
    if (side is Side.BUY and rsi_v > 50.0) or (side is Side.SELL and rsi_v < 50.0):
        return None
    wick = _wick_against(le, side)
    if wick < 0.25:
        return None
    if float(le["adx14"]) > 30 or _hard_opposed(lt, side, 30.0) or _spread_ratio_h1(snap) > 0.12:
        return None
    ext = float(day["low"].min()) if side is Side.BUY else float(day["high"].max())
    sl = _bound_sl(snap, side, entry, ext - s * 0.25 * atr, atr, 0.4, 2.0 * float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    # composante RSI : 0 à 50 (neutre), 10 à `rsi_lo` (30) ou au-delà pour un achat, miroir pour une vente
    rsi_gap = (50.0 - rsi_v) / max(50.0 - lo_r, 1e-9) if side is Side.BUY else (rsi_v - 50.0) / max(hi_r - 50.0, 1e-9)
    score = 20.0 + _clamp((abs(z_prev) - z_in) * 12 + 5, 0, 15) + _clamp(rsi_gap * 10, 0, 10)
    score += _clamp(wick * 20, 0, 10)
    pros = [f"clôture précédente à {z_prev:+.1f} σ de la moyenne de séance ({mean:.5g})",
            f"retour vers la moyenne ({z_now:+.1f} σ) confirmé par une clôture dans le sens du trade",
            f"RSI {rsi_v:.0f}", f"mèche de rejet {wick:.0%}"]
    cons = ["retour à la moyenne : aucune protection si la séance part en tendance"]
    if float(le["adx14"]) <= 22:
        score += 10
        pros.append(f"ADX M15 {le['adx14']:.0f} : séance sans tendance")
    else:
        cons.append(f"ADX M15 {le['adx14']:.0f} : tendance naissante possible")
    score += _clamp(abs(mean - entry) / atr * 8, 0, 10)
    targets = [mean, mean + s * 1.5 * sd]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.5)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la séance asiatique", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 1.5))), snap)


# ==============================================================================================================
# M06 — usdjpy_tokyo_trend (M15 / H1, ASIA & LONDON)
# ==============================================================================================================
@register("M06")
def strategy_m06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M06 — USDJPY : ESCALIER de séance — série de plus bas non décroissants, entrée sur la marche suivante.

    Thèse : en séance de Tokyo puis à l'ouverture de Londres, USDJPY ne monte pas en ligne droite mais par
    MARCHES : chaque barre laisse son plus bas au-dessus du plus bas précédent, sans jamais rendre le terrain
    gagné. L'objet de décision de cet agent n'est donc ni une moyenne, ni un canal, ni une structure de swings :
    c'est la LONGUEUR de la série monotone des extrêmes, doublée du ratio d'efficience de Kaufman (déplacement
    net / chemin parcouru) qui mesure si cette montée est une ligne ou un aller-retour déguisé. Un escalier meurt
    à sa première marche cassée : c'est exactement là que se place le stop, et nulle part ailleurs.

    Entrée : au moins `min_steps` barres clôturées consécutives (paramètre optionnel de `spec.params`,
    défaut 6, plancher 3) dont les plus bas sont non décroissants à
    0,05 ATR près (miroir sur les plus hauts pour une vente), et l'escalier opposé plus court d'au moins
    2 marches (sinon la série est ambiguë : simple plage) ; déplacement net des clôtures de l'escalier dans le
    sens du trade ; ratio d'efficience de l'escalier >= 0,35.
    Confirmation : la barre de signal grimpe une marche de plus — sa clôture dépasse le PLUS HAUT (bas) de la
    barre précédente — clôture dans le sens de la bougie et dans la moitié favorable de son amplitude.
    Filtres : classe forex ; heure de la barre dans [0h ; 12h) UTC (Tokyo puis ouverture de Londres) ; escalier
    plafonné à 30 marches (au-delà, la série est un artefact de marché plat) ; veto si la tendance H1 est
    franchement opposée (`_hard_opposed`, ADX >= 30) ; ADX14 H1 >= `adx_min` − 8 ; spread <= 12 % de l'ATR H1.
    SL : sous (au-dessus) le plus bas des DEUX dernières marches − 0,25 ATR : une clôture au-delà casse la série
    monotone, donc la thèse entière ; distance bornée à [0,5 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), prolongement de l'escalier (pas médian × nombre de marches déjà gravies : « il dure
    autant qu'il a déjà duré »), extrême de la veille ; cible finale = max(`rr` R, cible la plus lointaine).
    Invalidation : clôture M15 au-delà du plus bas (haut) de la marche précédente — l'escalier est rompu.
    Score : 20 (escalier >= 6 marches) + 0-15 marches supplémentaires + 0-15 ratio d'efficience + 15 MTF
    + 0-10 régularité des marches + 0-10 qualité de la clôture. Somme documentée, pas une probabilité.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    n = len(closed)
    if n < 60 or not _in_window(le, 0.0, 12.0) or not _ohlc_ok(closed.iloc[-40:]):
        return None
    lows = closed["low"].to_numpy(dtype=float)
    highs = closed["high"].to_numpy(dtype=float)
    cl = closed["close"].to_numpy(dtype=float)
    tol = 0.05 * atr
    up, dn = _stair_run(lows, tol), _stair_run(-highs, tol)
    min_steps = max(3, int(p.get("min_steps", 6)))   # paramétrable, jamais en dessous de 3 marches
    if up >= min_steps and up >= dn + 2:
        side, run = Side.BUY, min(up, 30)
    elif dn >= min_steps and dn >= up + 2:
        side, run = Side.SELL, min(dn, 30)
    else:
        return None
    s = side.sign
    seg_close = cl[-run:]
    er = _efficiency(seg_close)
    if er is None or er < 0.35 or s * (seg_close[-1] - seg_close[0]) <= 0:
        return None
    entry = float(le["close"])
    prev = closed.iloc[-2]
    step_trigger = float(prev["high"]) if side is Side.BUY else float(prev["low"])
    if s * (entry - step_trigger) <= 0 or s * (entry - float(le["open"])) <= 0 or _close_pos(le, side) < 0.5:
        return None
    if _hard_opposed(lt, side, 30.0):
        return None
    if not _valid(lt, "adx14") or float(lt["adx14"]) < float(p.get("adx_min", 25)) - 8.0:
        return None
    if _spread_ratio_h1(snap) > 0.12:
        return None
    rail = lows[-run:] if side is Side.BUY else highs[-run:]      # la rampe de l'escalier
    base = float(min(rail[-2], rail[-1])) if side is Side.BUY else float(max(rail[-2], rail[-1]))
    raw = base - s * 0.25 * atr            # derrière le plus bas (haut) des DEUX dernières marches
    sl = _bound_sl(snap, side, entry, raw, atr, 0.5, 2.0 * float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    steps = np.diff(rail) * s                                     # hauteur de chaque marche, orientée
    med_step = float(np.median(steps)) if len(steps) else 0.0
    spread_step = float(np.std(steps)) if len(steps) else 0.0
    score = 20.0 + _clamp((run - min_steps) * 2.0, 0, 15) + _clamp((er - 0.35) * 40, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"escalier de {run} marches (plus bas/hauts monotones à 0,05 ATR près)",
             f"ratio d'efficience {er:.2f} (déplacement net / chemin parcouru)",
             "la barre de signal grimpe une marche de plus (clôture au-delà de l'extrême précédent)"]
    cons: list[str] = []
    if med_step > 0:
        regularity = _clamp(10.0 * (1.0 - spread_step / med_step), 0, 10)
        score += regularity
        if spread_step > med_step:
            cons.append("marches irrégulières : l'escalier tient surtout à quelques barres")
    else:
        cons.append("marches de hauteur quasi nulle : escalier plat, l'efficience fait tout le travail")
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    if er < 0.5:
        cons.append(f"efficience {er:.2f} : le chemin parcouru dépasse largement le déplacement net")
    cons.append("un escalier est une lecture du passé : la première marche cassée annule tout le raisonnement")
    targets = [entry + s * max(med_step, 0.0) * run]
    d1 = snap.frames.get("D1")
    if d1 is not None and len(d1) >= 3:
        ph, pl = daily_high_low(d1)
        targets.append(ph if side is Side.BUY else pl)
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà du plus bas (haut) de la marche précédente : escalier rompu", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M07 — xauusd_ny_trend (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M07")
def strategy_m07(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M07 — XAUUSD : points PIVOTS journaliers classiques (P, S1/R1, S2/R2) comme grille de repli et de cible.

    Thèse : l'or est massivement suivi au pivot journalier (P = (H+B+C)/3 de la veille). Dans une tendance H1,
    le premier pivot situé sous le prix (P ou S1/S2) est le repli naturel : on l'achète quand il est DÉFENDU en
    clôture, et on vise le pivot suivant — une grille fixe, connue à l'avance, indépendante des moyennes mobiles.

    Entrée : biais H1 UP/DOWN (`_bias` : clôture et EMA20 du même côté de l'EMA50 H1) ; grille de pivots de la
    veille calculable (frame D1) ; le pivot le plus proche
    dans le dos du trade (sous le prix pour un achat) est touché par l'extrême de la barre de signal (à 0,25 ATR
    près) et la barre clôture au-dessus (sous) de lui, dans le sens du trade.
    Confirmation : le pivot suivant dans le sens du trade est à au moins 1,2 × la distance du stop (l'objectif
    doit payer le risque) ; corps de la barre de signal >= 30 % de son amplitude.
    Filtres : classe metals ; heure de la barre dans [12h ; 21h) UTC ; spread <= 10 % de l'ATR H1 (l'or est cher
    à traiter) ; RSI14 extrême (> 78 / < 22) = pénalité.
    SL : sous (au-dessus) le pivot utilisé − 0,35 ATR (un pivot perdu en clôture n'est plus un support) ;
    distance bornée à [0,5 ATR ; `sl_atr` ATR].
    TP : 1,5 R (partiel), pivot suivant, pivot d'après ; cible finale = max(`rr` R, pivot le plus lointain retenu).
    Invalidation : clôture M15 au-delà du pivot utilisé.
    Score : 20 (pivot défendu) + 0-15 espace jusqu'au pivot cible + 15 MTF + 0-10 corps + 0-10 proximité du pivot
    + 10 tendance M15 alignée − 10 RSI extrême.
    """
    if not _class_ok(snap, "metals"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    d1 = snap.frames.get("D1")
    if len(closed) < 60 or d1 is None or len(d1) < 3 or not _in_window(le, 12.0, 21.0):
        return None
    side = _side_from(_bias(lt))
    if side is None:
        return None
    prev_day = d1.iloc[-2]
    if not _valid(prev_day, "high", "low", "close"):
        return None
    hi, lo, cl = float(prev_day["high"]), float(prev_day["low"]), float(prev_day["close"])
    if not (np.isfinite(hi) and np.isfinite(lo) and np.isfinite(cl)) or hi <= lo:
        return None
    piv = (hi + lo + cl) / 3.0
    grid = sorted([piv - (hi - lo), 2 * piv - hi, piv, 2 * piv - lo, piv + (hi - lo)])  # S2 S1 P R1 R2
    s = side.sign
    entry = float(le["close"])
    behind = [x for x in grid if s * (entry - x) > 0]
    ahead = [x for x in grid if s * (x - entry) > 0]
    if not behind or not ahead:
        return None
    level = min(behind, key=lambda x: abs(entry - x))          # pivot le plus proche dans le dos du trade
    ahead.sort(key=lambda x: s * (x - entry))
    touched = float(le["low"]) <= level + 0.25 * atr if side is Side.BUY else float(le["high"]) >= level - 0.25 * atr
    if not touched or s * (entry - float(le["open"])) <= 0:
        return None
    sl = _bound_sl(snap, side, entry, level - s * 0.35 * atr, atr, 0.5, float(p.get("sl_atr", 1.8)))
    if sl is None:
        return None
    dist = abs(entry - sl)
    if abs(ahead[0] - entry) < 1.2 * dist:
        return None  # le pivot cible ne paie pas le risque
    body = _body_ratio(le)
    if body < 0.30 or _spread_ratio_h1(snap) > 0.10:
        return None
    room = abs(ahead[0] - entry) / dist
    score = 20.0 + _clamp((room - 1.2) * 10, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    names = {0: "S2", 1: "S1", 2: "P", 3: "R1", 4: "R2"}
    pros += [f"pivot {names.get(grid.index(level), '?')} de la veille ({level:.5g}) défendu en clôture",
             f"pivot cible à {room:.1f} R", f"corps de signal {body:.0%}"]
    cons: list[str] = []
    score += _clamp(body * 10, 0, 10)
    prox = abs(entry - level) / atr
    score += _clamp((0.6 - prox) * 20, 0, 10)
    if prox > 0.6:
        cons.append(f"entrée à {prox:.1f} ATR du pivot : stop plus large")
    if _trend_of(le) == _trend_of(lt):
        score += 10
        pros.append("tendance M15 alignée sur H1")
    else:
        cons.append("EMA M15 pas encore alignées sur la tendance H1")
    rsi_v = float(le["rsi14"])
    if (side is Side.BUY and rsi_v > 78) or (side is Side.SELL and rsi_v < 22):
        score -= 10
        cons.append(f"RSI {rsi_v:.0f} extrême : entrée tardive possible")
    cons.append("les pivots ne sont qu'une grille statistique : un communiqué les traverse sans réagir")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà du pivot journalier utilisé", bt)
    return _finalize(_set_tp_plan(cand, side, entry, ahead[:2], float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M08 — xauusd_sweep_reversal (M15 / H1, LONDON, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M08")
def strategy_m08(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M08 — XAUUSD : balayage de l'extrême du JOUR avec CLIMAX DE VOLUME puis réintégration.

    Thèse : sur l'or, l'extrême de la journée en cours concentre les stops des positions intrajournalières. Un
    dépassement accompagné d'un pic de volume de tick, immédiatement suivi d'une clôture sous l'extrême et sous
    la moitié de la bougie de balayage, signale une distribution : le volume a servi à SORTIR, pas à poursuivre.
    C'est le volume — et non la seule mèche — qui distingue ce cas d'une cassure ordinaire.

    Entrée : au moins 12 barres clôturées dans le jour ; extrême de référence = extrême du jour établi hors des
    2 dernières barres ; la barre de balayage (signal ou précédente) dépasse cet extrême, avec un volume de tick
    >= 1,4 × la moyenne des 20 barres précédentes et une mèche >= 40 % de son amplitude ; la barre de signal
    clôture en deçà de l'extrême ET en deçà du milieu de la barre de balayage.
    Confirmation : clôture de la barre de signal dans la moitié favorable de sa propre amplitude.
    Filtres : classe metals ; heure de la barre dans [7h ; 21h) UTC ; veto si la tendance H1 est franchement
    opposée avec ADX >= 32 ; spread <= 10 % de l'ATR H1.
    SL : au-delà de l'extrême du balayage + 0,3 ATR ; distance bornée à [0,4 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), milieu de la journée, extrême opposé de la journée.
    Invalidation : clôture M15 au-delà de l'extrême balayé.
    Score : 20 (balayage + réintégration) + 0-15 climax de volume + 0-10 mèche + 0-10 réintégration sous le
    milieu + 10 tendance H1 non opposée + 0-10 clôture.
    """
    if not _class_ok(snap, "metals"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _in_window(le, 7.0, 21.0):
        return None
    day = _today(closed)
    if len(day) < 12 or not _ohlc_ok(day):
        return None
    ref_win = day.iloc[:-2]
    if len(ref_win) < 6:
        return None
    day_hi, day_lo = float(ref_win["high"].max()), float(ref_win["low"].min())
    prev = closed.iloc[-2]
    entry = float(le["close"])
    if float(le["high"]) > day_hi or float(prev["high"]) > day_hi:
        side = Side.SELL
        bar = le if float(le["high"]) >= float(prev["high"]) else prev
        level, sweep = day_hi, float(bar["high"])
        if entry >= day_hi:
            return None
    elif float(le["low"]) < day_lo or float(prev["low"]) < day_lo:
        side = Side.BUY
        bar = le if float(le["low"]) <= float(prev["low"]) else prev
        level, sweep = day_lo, float(bar["low"])
        if entry <= day_lo:
            return None
    else:
        return None
    s = side.sign
    mid_bar = (float(bar["high"]) + float(bar["low"])) / 2.0
    if s * (entry - mid_bar) <= 0 or s * (entry - float(le["open"])) <= 0:
        return None
    wick = _wick_against(bar, side)
    if wick < 0.40 or _close_pos(le, side) < 0.5:
        return None
    vols = _volumes(closed.iloc[-22:])
    if vols is None or len(vols) < 22:
        return None
    base = float(np.mean(vols[:-2]))
    v_bar = float(vols[-1] if bar is le else vols[-2])
    if base <= 0 or v_bar < 1.4 * base:
        return None
    if _hard_opposed(lt, side, 32.0) or _spread_ratio_h1(snap) > 0.10:
        return None
    sl = _bound_sl(snap, side, entry, sweep - s * 0.3 * atr, atr, 0.4, 2.0 * float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    ratio = v_bar / base
    reintegration = abs(entry - mid_bar) / max(abs(float(bar["high"]) - float(bar["low"])), 1e-12)
    score = 20.0 + _clamp((ratio - 1.4) * 15 + 5, 0, 15) + _clamp(wick * 20, 0, 10)
    score += _clamp(reintegration * 20, 0, 10)
    pros = [f"balayage de l'extrême du jour ({level:.5g}) puis réintégration",
            f"climax de volume {ratio:.1f}× la moyenne 20 barres", f"mèche de balayage {wick:.0%}",
            f"clôture {'sous' if side is Side.SELL else 'au-dessus'} du milieu de la bougie de balayage"]
    cons = ["contre-tendance intrajournalière : l'extrême peut être repris dans la séance suivante"]
    if not _hard_opposed(lt, side, 0.0):
        score += 10
        pros.append("tendance H1 non opposée")
    else:
        cons.append("tendance H1 opposée au retournement")
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    targets = [(day_hi + day_lo) / 2.0, day_lo if side is Side.SELL else day_hi]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême du jour balayé", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M09 — nas100_open_range (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M09")
def strategy_m09(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M09 — NAS100 : cassure de l'opening range 13:30-14:00 alignée avec le GAP d'ouverture.

    Thèse : sur le NASDAQ, la première demi-heure de cotation américaine fixe le champ de bataille du jour. Le
    sens du GAP (ouverture du cash contre clôture de la veille) dit qui a déjà pris la main pendant la nuit :
    une cassure de l'opening range CONTRE le gap est le plus souvent un comblement, une cassure DANS le sens du
    gap est une continuation. Le filtre n'est donc pas la compression du range mais la cohérence gap / cassure.

    Entrée : opening range (`start`-`end`, 13:30-14:00 par défaut) disponible ; gap = ouverture de la première
    barre du jour à partir de `start` moins la clôture D1 de la veille ; gap non opposé au sens de la cassure
    (ou négligeable, < 0,2 ATR H1) ; première clôture du jour au-delà d'une borne de l'opening range, après la
    fin de celui-ci.
    Confirmation : corps >= 0,35 ATR et clôture dans les 30 % extrêmes de la bougie (poussée, pas mèche).
    Filtres : classe indices ; heure de la barre dans [fin de l'opening range ; fin + 5 h) ; hauteur du range
    entre 0,3 et 2,5 ATR H1 ; extension au-delà de la borne <= 1 ATR ; spread <= 12 % de l'ATR H1.
    SL : milieu de l'opening range (± 0,1 ATR) — revenir au centre du range annule la cassure ; distance bornée
    à [0,5 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), borne + 1 hauteur de range, borne + 2 hauteurs (mouvement mesuré).
    Invalidation : clôture M15 de retour au-delà du milieu de l'opening range.
    Score : 25 (première cassure d'un opening range valide) + 0-10 alignement du gap + 0-10 corps
    + 15 MTF + 0-10 extension faible + 0-10 volume de la barre de cassure.
    """
    if not _class_ok(snap, "indices"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    d1 = snap.frames.get("D1")
    if len(closed) < 60 or d1 is None or len(d1) < 3:
        return None
    start_h = _hhmm(p.get("start", "13:30"), 13.5)
    end_h = _hhmm(p.get("end", "14:00"), 14.0)
    if end_h <= start_h or not _in_window(le, end_h, min(end_h + 5.0, 23.99)):
        return None
    or_hi, or_lo = session_range(closed, str(p.get("start", "13:30")), str(p.get("end", "14:00")))
    if not (np.isfinite(or_hi) and np.isfinite(or_lo)) or or_hi <= or_lo:
        return None
    atr_h1 = float(snap.atr_h1 or 0.0)
    height = float(or_hi - or_lo)
    if atr_h1 <= 0 or not (0.3 * atr_h1 <= height <= 2.5 * atr_h1):
        return None
    first = _bars_between(closed, start_h, end_h)
    if first.empty or not _ohlc_ok(first):
        return None
    prev_close = float(d1.iloc[-2]["close"])
    if not np.isfinite(prev_close):
        return None
    gap = float(first.iloc[0]["open"]) - prev_close
    entry = float(le["close"])
    after = _bars_between(closed, end_h, 24.0)
    # une seule barre suffit : c'est la PREMIÈRE clôture postérieure à l'opening range qui intéresse l'agent.
    # Exiger deux barres (`< 2`) excluait justement ce premier signal, pourtant décrit par la thèse.
    if len(after) < 1 or not _ohlc_ok(after):
        return None
    prior = after.iloc[:-1]
    if entry > or_hi and not bool((prior["close"] > or_hi).any()):
        side, boundary = Side.BUY, float(or_hi)
    elif entry < or_lo and not bool((prior["close"] < or_lo).any()):
        side, boundary = Side.SELL, float(or_lo)
    else:
        return None
    s = side.sign
    if s * gap < 0 and abs(gap) > 0.2 * atr_h1:
        return None  # cassure contre un gap significatif : configuration de comblement, pas de continuation
    body = abs(entry - float(le["open"]))
    if body < 0.35 * atr or _close_pos(le, side) < 0.7 or s * (entry - float(le["open"])) <= 0:
        return None
    ext = s * (entry - boundary)
    if ext > 1.0 * atr or _spread_ratio_h1(snap) > 0.12:
        return None
    mid = (or_hi + or_lo) / 2.0
    sl = _bound_sl(snap, side, entry, mid - s * 0.1 * atr, atr, 0.5, 2.0 * float(p.get("sl_atr", 1.0)))
    if sl is None:
        return None
    score = 25.0 + _clamp(abs(gap) / atr_h1 * 15 if s * gap > 0 else 0.0, 0, 10) + _clamp((body / atr - 0.35) * 20, 0, 10)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"première clôture hors de l'opening range {or_lo:.5g}-{or_hi:.5g} ({height / atr_h1:.1f} ATR H1)",
             f"gap d'ouverture {gap:+.5g} {'aligné' if s * gap > 0 else 'négligeable'}",
             f"bougie de poussée (corps {body / atr:.1f} ATR)"]
    cons: list[str] = []
    if s * gap <= 0:
        cons.append("aucun soutien du gap d'ouverture")
    score += _clamp((1.0 - ext / atr) * 10, 0, 10)
    if ext > 0.5 * atr:
        cons.append(f"entrée {ext / atr:.1f} ATR au-delà de la borne")
    vols = _volumes(closed.iloc[-21:])
    if vols is not None and len(vols) >= 21:
        base = float(np.mean(vols[:-1]))
        ratio = float(vols[-1] / base) if base > 0 else 0.0
        score += _clamp((ratio - 1.0) * 20, 0, 10)
        if ratio < 1.0:
            cons.append(f"volume de cassure sous la moyenne ({ratio:.1f}×)")
        else:
            pros.append(f"volume de cassure {ratio:.1f}× la moyenne 20 barres")
    cons.append("fausse cassure possible : le stop est au centre de l'opening range")
    targets = [boundary + s * height, boundary + s * 2.0 * height]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 de retour au-delà du milieu de l'opening range", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M10 — us500_trend_pullback (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M10")
def strategy_m10(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M10 — US500 : RSI(2) en excès dans la tendance H1 — le repli STATISTIQUE, pas le repli graphique.

    Thèse : un indice large ne fait pas de replis profonds tant que la tendance de fond tient ; les creux durent
    deux ou trois barres. Un RSI à 2 périodes (beaucoup plus réactif que le RSI14 utilisé par les autres agents)
    mesure cet excès court ; on ne se positionne qu'après un signal de reprise (clôture au-delà du plus haut de
    la barre précédente), jamais sur le seul excès.

    Entrée : tendance de fond H1 (clôture au-dessus/sous l'EMA200 H1 ET EMA50 du bon côté de l'EMA200) ;
    RSI(2) M15 de la barre précédente <= 10 (achat) / >= 90 (vente) ; la barre de signal clôture au-delà du plus
    haut (bas) de la barre précédente, dans le sens de la tendance.
    Confirmation : clôture dans la moitié favorable de la bougie de signal.
    Filtres : classe indices ; heure de la barre dans [12h ; 21h) UTC ; profondeur du repli (extrême des 8
    dernières barres clôturées → creux) <= 3 ATR — au-delà, ce n'est plus une respiration mais une correction ;
    spread <= 12 % de l'ATR H1.
    SL : sous (au-dessus) l'extrême des 4 dernières barres clôturées − 0,3 ATR (le creux du repli) ; distance
    bornée à [0,5 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), extrême des 20 dernières barres (retour au sommet), puis `rr` R.
    Invalidation : clôture M15 au-delà de l'extrême du repli.
    Score : 20 (excès RSI(2) + reprise) + 0-15 profondeur de l'excès + 15 MTF + 0-10 repli peu profond
    + 0-10 clôture + 0-10 ADX H1 (tendance de fond).
    """
    if not _class_ok(snap, "indices"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _in_window(le, 12.0, 21.0):
        return None
    if not _valid(lt, "ema50", "ema200", "close"):
        return None
    if float(lt["close"]) > float(lt["ema200"]) and float(lt["ema50"]) > float(lt["ema200"]):
        side = Side.BUY
    elif float(lt["close"]) < float(lt["ema200"]) and float(lt["ema50"]) < float(lt["ema200"]):
        side = Side.SELL
    else:
        return None
    s = side.sign
    r2 = rsi(closed["close"], 2)
    if len(r2) < 3 or bool(r2.iloc[-3:].isna().any()):
        return None
    r_prev = float(r2.iloc[-2])
    if (side is Side.BUY and r_prev > 10.0) or (side is Side.SELL and r_prev < 90.0):
        return None
    prev = closed.iloc[-2]
    entry = float(le["close"])
    trigger = float(prev["high"]) if side is Side.BUY else float(prev["low"])
    if s * (entry - trigger) <= 0 or s * (entry - float(le["open"])) <= 0 or _close_pos(le, side) < 0.5:
        return None
    win20 = closed.iloc[-21:-1]
    peak = float(win20["high"].max()) if side is Side.BUY else float(win20["low"].min())   # cible de retour
    low4 = float(closed["low"].iloc[-4:].min()) if side is Side.BUY else float(closed["high"].iloc[-4:].max())
    # profondeur mesurée depuis l'extrême LOCAL d'où part le repli (8 barres), pas depuis le sommet de 20 barres
    local = float(closed["high"].iloc[-8:].max()) if side is Side.BUY else float(closed["low"].iloc[-8:].min())
    depth = abs(local - low4) / atr
    if depth > 3.0 or _spread_ratio_h1(snap) > 0.12:
        return None
    sl = _bound_sl(snap, side, entry, low4 - s * 0.3 * atr, atr, 0.5, 2.0 * float(p.get("sl_atr", 1.2)))
    if sl is None:
        return None
    excess = (10.0 - r_prev) if side is Side.BUY else (r_prev - 90.0)
    score = 20.0 + _clamp(excess * 1.5, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"RSI(2) M15 à {r_prev:.0f} sur la barre précédente (excès court)",
             "reprise confirmée par une clôture au-delà de l'extrême de la barre précédente",
             f"tendance de fond H1 (clôture du bon côté de l'EMA200), repli local de {depth:.1f} ATR"]
    cons: list[str] = []
    score += _clamp((3.0 - depth) * 5, 0, 10)
    if depth > 2.0:
        cons.append(f"repli de {depth:.1f} ATR : la respiration devient une correction")
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    if _valid(lt, "adx14"):
        score += _clamp((float(lt["adx14"]) - 15) * 0.6, 0, 10)
        if float(lt["adx14"]) < 15:
            cons.append(f"ADX H1 {lt['adx14']:.0f} : tendance de fond molle")
    cons.append("acheter un excès reste un pari sur la persistance de la tendance de fond")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême du repli (4 dernières barres)", bt)
    return _finalize(_set_tp_plan(cand, side, entry, [peak], float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M11 — ger40_london_open (M15 / H1, LONDON)
# ==============================================================================================================
@register("M11")
def strategy_m11(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M11 — GER40 : PROFIL DE VOLUME de la veille — test du POC ou de la zone de valeur à l'ouverture de Londres.

    Thèse : le DAX ouvre en réaction à la journée précédente. Le prix qui a concentré le plus de volume de tick
    (POC) et les bornes de la zone de valeur (70 % du volume) sont les vrais niveaux de référence de l'ouverture,
    bien plus que les EMA : le marché y revient pour vérifier l'acceptation. Un test qui TIENT en clôture donne
    une entrée avec un stop court sous une zone où beaucoup d'échanges ont déjà eu lieu.

    Entrée : profil de volume de la journée UTC précédente calculable (>= 20 barres M15 et volume disponible) ;
    biais H1 UP/DOWN (`_bias` : clôture et EMA20 du même côté de l'EMA50 H1 — un test de niveau n'exige pas
    l'empilement complet des EMA) ; le niveau du profil le plus proche dans le dos du trade (POC, borne haute ou basse de
    la zone de valeur) est touché par l'extrême de la barre de signal (à 0,3 ATR près) et la barre clôture du bon
    côté de ce niveau, dans le sens du trade.
    Confirmation : corps >= 30 % de l'amplitude et clôture dans la moitié favorable de la bougie.
    Filtres : classe indices ; heure de la barre dans [7h ; 12h) UTC ; spread <= 12 % de l'ATR H1. L'absence de
    niveau de profil en cible devant le trade n'est PAS bloquante (l'objectif retombe alors sur des multiples de
    R), mais elle est signalée dans `arguments_against` et ne rapporte aucun point de score.
    SL : sous (au-dessus) le niveau testé et l'extrême de la barre de signal − 0,3 ATR ; distance bornée à
    [0,5 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), niveaux du profil situés devant (POC, bornes de la zone de valeur), extrême de la veille.
    Invalidation : clôture M15 au-delà du niveau de profil utilisé.
    Score : 20 (niveau du profil défendu) + 10 si le niveau est le POC + 15 MTF + 0-10 proximité du niveau
    + 0-10 corps + 0-10 espace jusqu'au niveau suivant.
    """
    if not _class_ok(snap, "indices"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _in_window(le, 7.0, 12.0):
        return None
    side = _side_from(_bias(lt))
    if side is None:
        return None
    prof = _volume_profile(_prev_day(closed))
    if prof is None:
        return None
    poc, va_hi, va_lo = prof
    s = side.sign
    entry = float(le["close"])
    levels = [poc, va_hi, va_lo]
    behind = [x for x in levels if s * (entry - x) > 0]
    ahead = sorted([x for x in levels if s * (x - entry) > 0], key=lambda x: s * (x - entry))
    if not behind:
        return None
    level = min(behind, key=lambda x: abs(entry - x))
    touched = float(le["low"]) <= level + 0.3 * atr if side is Side.BUY else float(le["high"]) >= level - 0.3 * atr
    if not touched or s * (entry - float(le["open"])) <= 0:
        return None
    body = _body_ratio(le)
    if body < 0.30 or _close_pos(le, side) < 0.5 or _spread_ratio_h1(snap) > 0.12:
        return None
    anchor = min(level, float(le["low"])) if side is Side.BUY else max(level, float(le["high"]))
    sl = _bound_sl(snap, side, entry, anchor - s * 0.3 * atr, atr, 0.5, 2.0 * float(p.get("sl_atr", 1.0)))
    if sl is None:
        return None
    if level == poc:
        name = "POC"
    else:
        name = "borne haute de la zone de valeur" if level == va_hi else "borne basse de la zone de valeur"
    score = 20.0 + (10.0 if level == poc else 0.0)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"{name} de la veille ({level:.5g}) testé puis tenu en clôture",
             f"profil de volume de la veille : POC {poc:.5g}, zone de valeur {va_lo:.5g}-{va_hi:.5g}",
             f"corps de signal {body:.0%}"]
    cons: list[str] = []
    prox = abs(entry - level) / atr
    score += _clamp((0.8 - prox) * 12, 0, 10)
    score += _clamp(body * 10, 0, 10)
    dist = abs(entry - sl)
    d1 = snap.frames.get("D1")
    targets = list(ahead)
    if d1 is not None and len(d1) >= 3:
        ph, pl = daily_high_low(d1)
        targets.append(ph if side is Side.BUY else pl)
    room = [x for x in targets if np.isfinite(x) and s * (x - entry) >= dist]
    if room:
        score += _clamp(min(s * (x - entry) for x in room) / dist * 5, 0, 10)
        pros.append(f"niveau de profil en cible à {min(s * (x - entry) for x in room) / dist:.1f} R")
    else:
        cons.append("aucun niveau de profil en cible : objectif en multiples de R uniquement")
    if level != poc:
        cons.append("niveau de bordure : le POC reste l'aimant principal de la séance")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà du niveau de profil de volume utilisé", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M12 — audusd_asia_breakout (M15 / H1, ASIA & LONDON)
# ==============================================================================================================
@register("M12")
def strategy_m12(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M12 — AUDUSD : cassure du range asiatique CONFIRMÉE PAR L'OBV (accumulation avant la sortie).

    Thèse : AUDUSD casse souvent son range de nuit sur un simple pic de spread, sans flux derrière. L'OBV
    (volume de tick cumulé, signé par le sens de la clôture) distingue les deux cas : si l'OBV inscrit son plus
    haut des 20 dernières barres AVANT ou AVEC la cassure, le range a été accumulé et la sortie est portée ;
    sinon c'est une mèche technique. C'est un filtre de FLUX, pas de volatilité.

    Entrée : range de la session `session` (`start`-`end`, 22:00-02:00 par défaut) valide ; première clôture du
    jour au-delà d'une borne après la fin de la session ; OBV de la barre de signal = maximum (minimum) des 20
    dernières valeurs ET en progression sur 3 barres.
    Confirmation : corps >= 0,3 ATR et clôture dans les 35 % extrêmes de la bougie.
    Filtres : classe forex ; heure de la barre dans [fin de session ; fin + 6 h) ; largeur du range entre 0,4 et
    4,5 ATR H1 (un range de nuit de 4 heures dépasse largement l'ATR horaire) ; extension au-delà de la borne
    <= 1 ATR ; spread <= 12 % de l'ATR H1.
    SL : le plus proche entre le milieu du range et la borne − `sl_atr` ATR, moins 0,1 ATR de marge ; distance
    bornée à [0,4 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), borne + 1 hauteur de range, borne + 1,5 hauteur.
    Invalidation : clôture M15 de retour à l'intérieur du range asiatique.
    Score : 25 (cassure + accumulation OBV) + 0-10 pente de l'OBV + 0-10 range compact + 15 MTF
    + 0-10 corps + 0-10 extension faible.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60:
        return None
    end_h = _hhmm(p.get("end", "02:00"), 2.0)
    if not _in_window(le, end_h, min(end_h + 6.0, 23.99)):
        return None
    hi, lo = session_range(closed, str(p.get("start", "22:00")), str(p.get("end", "02:00")))
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi <= lo:
        return None
    atr_h1 = float(snap.atr_h1 or 0.0)
    rng = float(hi - lo)
    if atr_h1 <= 0 or not (0.4 * atr_h1 <= rng <= 4.5 * atr_h1):
        return None
    after = _bars_between(closed, end_h, 24.0)
    # idem M09 : la première clôture postérieure au range est un signal légitime, ne pas l'exclure (`< 2`)
    if len(after) < 1 or not _ohlc_ok(after):
        return None
    prior = after.iloc[:-1]
    entry = float(le["close"])
    if entry > hi and not bool((prior["close"] > hi).any()):
        side, boundary = Side.BUY, float(hi)
    elif entry < lo and not bool((prior["close"] < lo).any()):
        side, boundary = Side.SELL, float(lo)
    else:
        return None
    s = side.sign
    obv = _obv(closed.iloc[-60:])
    if obv is None or len(obv) < 24:
        return None
    window = obv[-20:]
    extreme = float(window.max()) if side is Side.BUY else float(window.min())
    if s * (float(obv[-1]) - extreme) < 0 or s * (float(obv[-1]) - float(obv[-4])) <= 0:
        return None
    body = abs(entry - float(le["open"]))
    if body < 0.3 * atr or _close_pos(le, side) < 0.65 or s * (entry - float(le["open"])) <= 0:
        return None
    ext = s * (entry - boundary)
    if ext > 1.0 * atr or _spread_ratio_h1(snap) > 0.12:
        return None
    sl_atr = float(p.get("sl_atr", 1.0))
    mid = (hi + lo) / 2.0
    raw = max(mid, boundary - sl_atr * atr) if side is Side.BUY else min(mid, boundary + sl_atr * atr)
    sl = _bound_sl(snap, side, entry, raw - s * 0.1 * atr, atr, 0.4, 2.0 * sl_atr)
    if sl is None:
        return None
    slope_obv = (float(obv[-1]) - float(obv[-4])) / max(abs(float(np.mean(np.abs(np.diff(obv[-20:]))))), 1e-9)
    score = 25.0 + _clamp(abs(slope_obv) * 2, 0, 10)
    if rng <= 1.5 * atr_h1:
        score += 10
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"range asiatique {lo:.5g}-{hi:.5g} ({rng / atr_h1:.1f} ATR H1) cassé pour la première fois",
             f"OBV à son extrême 20 barres et en progression ({slope_obv:+.1f} écarts moyens sur 3 barres)",
             f"corps de cassure {body / atr:.1f} ATR"]
    cons: list[str] = []
    if rng > 1.5 * atr_h1:
        cons.append("range de nuit large : la projection est éloignée")
    score += _clamp((body / atr - 0.3) * 20, 0, 10)
    score += _clamp((1.0 - ext / atr) * 10, 0, 10)
    if ext > 0.5 * atr:
        cons.append(f"entrée {ext / atr:.1f} ATR au-delà de la borne")
    cons.append("le volume de tick n'est pas un volume échangé : l'OBV n'est qu'un proxy de flux")
    targets = [boundary + s * rng, boundary + s * 1.5 * rng]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 de retour à l'intérieur du range asiatique", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M13 — usdcad_ny_trend (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M13")
def strategy_m13(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M13 — USDCAD : PRESSION DE MÈCHES cumulée — l'absorption se lit dans les mèches, pas dans les clôtures.

    Thèse : USDCAD est une paire lente, tenue par des flux commerciaux (pétrole) et des teneurs de marché. En
    séance américaine, ce qui annonce le prochain déplacement n'est pas la suite des clôtures — elles piétinent —
    mais la répartition des MÈCHES : sur 20 barres, la somme des mèches basses (le prix est allé chercher des
    vendeurs plus bas et a été racheté) contre la somme des mèches hautes dit quel côté ABSORBE. Quand ce
    déséquilibre est net et que l'absorption n'a pas encore produit de déplacement, la conversion est devant.
    Aucun autre agent ne décide sur la géométrie interne des bougies : ni moyenne, ni canal, ni niveau.

    Entrée : fenêtre de 20 barres clôturées complète ; déséquilibre de pression (Σ mèches basses − Σ mèches
    hautes) / (Σ des deux) >= +0,25 pour un achat, <= −0,25 pour une vente ; la barre de signal porte elle-même
    une mèche d'absorption >= 30 % de son amplitude et clôture au-delà de la CLÔTURE précédente, dans le sens
    de la pression.
    Confirmation : clôture dans le sens de la bougie et dans la moitié favorable de son amplitude ;
    ADX14 M15 >= `adx_min` − 10 (l'absorption doit déjà produire un minimum de direction).
    Filtres : classe forex ; heure de la barre dans [12h ; 21h) UTC ; déplacement net des clôtures de la fenêtre
    <= 2,5 ATR — au-delà, l'absorption est DÉJÀ convertie et il ne reste rien à jouer ; veto si la tendance H1
    est franchement opposée (ADX >= 30) ; spread <= 12 % de l'ATR H1.
    SL : sous (au-dessus) le plus bas (haut) de la BARRE D'ABSORPTION de référence — celle des 6 dernières
    barres dont la mèche favorable est la plus longue — moins 0,25 ATR : si ce niveau tombe, l'acheteur qui
    absorbait n'est plus là ; distance bornée à [0,5 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), plafond de la fenêtre d'absorption (le niveau que l'absorption doit franchir), puis ce
    plafond prolongé d'une demi-hauteur de fenêtre ; cible finale = max(`rr` R, cible la plus lointaine).
    Invalidation : clôture M15 au-delà de l'extrême de la barre d'absorption de référence.
    Score : 20 (pression nette) + 0-15 amplitude du déséquilibre + 15 MTF + 0-10 mèche de la barre de signal
    + 0-10 absorption non encore convertie + 0-10 qualité de la clôture. Somme documentée, pas une probabilité.
    """
    if not _class_ok(snap, "forex"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _in_window(le, 12.0, 21.0):
        return None
    win = closed.iloc[-20:]
    wp = _wick_pressure(win)
    if wp is None:
        return None
    press, lower, upper = wp
    if press >= 0.25:
        side = Side.BUY
    elif press <= -0.25:
        side = Side.SELL
    else:
        return None
    s = side.sign
    entry = float(le["close"])
    prev_close = float(closed["close"].iloc[-2])
    if s * (entry - prev_close) <= 0 or s * (entry - float(le["open"])) <= 0 or _close_pos(le, side) < 0.5:
        return None
    wick = _wick_against(le, side)
    if wick < 0.30:
        return None
    if float(le["adx14"]) < float(p.get("adx_min", 25)) - 10.0:
        return None
    wc = win["close"].to_numpy(dtype=float)
    travelled = abs(float(wc[-1] - wc[0])) / atr
    if travelled > 2.5:
        return None                        # absorption déjà convertie en déplacement : plus de prime à prendre
    if _hard_opposed(lt, side, 30.0) or _spread_ratio_h1(snap) > 0.12:
        return None
    marks = (lower if side is Side.BUY else upper)[-6:]
    tail = win.iloc[-6:]
    j = int(np.argmax(marks))
    ref = tail.iloc[j]
    anchor = float(ref["low"]) if side is Side.BUY else float(ref["high"])
    sl = _bound_sl(snap, side, entry, anchor - s * 0.25 * atr, atr, 0.5, 2.0 * float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    score = 20.0 + _clamp((abs(press) - 0.25) * 40, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    quel = "basses" if side is Side.BUY else "hautes"
    pros += [f"pression de mèches {press:+.2f} sur 20 barres (mèches {quel} dominantes)",
             f"barre de signal absorbée à son tour (mèche {wick:.0%} de l'amplitude)",
             f"absorption pas encore convertie : {travelled:.1f} ATR parcourus en 20 barres"]
    cons: list[str] = []
    score += _clamp(wick * 20, 0, 10)
    score += _clamp((2.5 - travelled) * 4, 0, 10)
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    if abs(press) < 0.4:
        cons.append(f"déséquilibre modéré ({press:+.2f}) : l'absorption n'est pas franche")
    if travelled > 1.5:
        cons.append(f"{travelled:.1f} ATR déjà parcourus : une partie de la conversion est faite")
    cons.append("une mèche n'est pas un carnet d'ordres : l'absorption reste une inférence, pas une mesure")
    hi_w, lo_w = float(win["high"].max()), float(win["low"].min())
    ceiling = hi_w if side is Side.BUY else lo_w
    targets = [ceiling, ceiling + s * 0.5 * (hi_w - lo_w)]
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la barre d'absorption de référence", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# ==============================================================================================================
# M14 — usoil_ny_momentum (M15 / H1, NEWYORK & OVERLAP)
# ==============================================================================================================
@register("M14")
def strategy_m14(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """M14 — USOIL : momentum STATISTIQUEMENT extrême — le déplacement 10 barres dépasse son propre percentile 95.

    Thèse : le pétrole bouge par à-coups (stocks hebdomadaires, OPEP). Plutôt que de comparer l'ATR à sa moyenne
    (expansion de volatilité, déjà couverte ailleurs), on mesure le DÉPLACEMENT NET sur 10 barres et on le compare
    à la distribution de ses propres 100 dernières valeurs : un déplacement au-delà du 95ᵉ percentile est un
    événement de flux, pas une oscillation — et il se poursuit souvent jusqu'à la fin de la séance américaine.

    Entrée : |clôture − clôture 10 barres avant| >= percentile 95 des 100 dernières valeurs du même indicateur
    ET >= `atr_ratio` × ATR ; clôture de la barre de signal = extrême des 10 dernières clôtures dans le sens du
    déplacement.
    Confirmation : clôture dans les 25 % extrêmes de la bougie (la barre finit sur ses plus hauts/bas).
    Filtres : classe energies ; heure de la barre dans [12h ; 21h) UTC ; veto si la tendance H1 est franchement
    opposée avec ADX >= 32 ; spread <= 50 % de l'ATR H1 (le brut est structurellement large — au-delà de 15 %,
    un argument contre est ajouté car le gate d'exécution refusera).
    SL : extrême de la DERNIÈRE barre contraire des 6 précédentes (le dernier point de repos du flux) − 0,3 ATR ;
    à défaut, extrême des 5 dernières barres ; distance bornée à [0,6 ATR ; 2 × `sl_atr` ATR].
    TP : 1,5 R (partiel), extrême du jour + 0,5 amplitude du jour (projection de séance), puis `rr` R.
    Invalidation : clôture M15 au-delà de l'extrême de la dernière barre contraire (le flux s'est retourné).
    Score : 20 (momentum extrême) + 0-15 dépassement du percentile + 0-10 ATR relatif + 15 MTF
    + 0-10 clôture + 0-10 position dans l'amplitude du jour.
    """
    if not _class_ok(snap, "energies"):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    if not _bar_ok(le) or not _bar_ok(lt):
        return None
    p = spec.params
    closed = _closed(e)
    if len(closed) < 120 or not _in_window(le, 12.0, 21.0):
        return None
    cl = closed["close"].to_numpy(dtype=float)
    if not np.isfinite(cl[-120:]).all():
        return None
    disp = cl[10:] - cl[:-10]                 # déplacement net sur 10 barres, pour chaque barre clôturée
    hist = np.abs(disp[-100:])
    if len(hist) < 100:
        return None
    thr = float(np.quantile(hist, 0.95))
    m = float(disp[-1])
    ratio_atr = float(p.get("atr_ratio", 1.3))
    if abs(m) < thr or abs(m) < ratio_atr * atr or thr <= 0:
        return None
    side = Side.BUY if m > 0 else Side.SELL
    s = side.sign
    entry = float(le["close"])
    last10 = cl[-10:]
    if (side is Side.BUY and entry < float(last10.max())) or (side is Side.SELL and entry > float(last10.min())):
        return None
    if _close_pos(le, side) < 0.75 or s * (entry - float(le["open"])) <= 0:
        return None
    if _hard_opposed(lt, side, 32.0) or _spread_ratio_h1(snap) > 0.50:
        return None
    anchor = None
    for j in range(len(closed) - 2, max(len(closed) - 7, 0) - 1, -1):   # les 6 barres précédant le signal
        row = closed.iloc[j]
        if not _bar_ok(row):
            continue
        if s * (float(row["close"]) - float(row["open"])) < 0:
            anchor = float(row["low"]) if side is Side.BUY else float(row["high"])
            break
    if anchor is None:
        anchor = float(closed["low"].iloc[-5:].min()) if side is Side.BUY else float(closed["high"].iloc[-5:].max())
    sl = _bound_sl(snap, side, entry, anchor - s * 0.3 * atr, atr, 0.6, 2.0 * float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    score = 20.0 + _clamp((abs(m) / thr - 1.0) * 60, 0, 15) + _clamp((abs(m) / atr - ratio_atr) * 10, 0, 10)
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    pros += [f"déplacement 10 barres de {m:+.5g} ({abs(m) / thr:.2f}× le percentile 95 de sa propre distribution)",
             f"soit {abs(m) / atr:.1f} ATR", "clôture extrême des 10 dernières clôtures"]
    cons: list[str] = []
    score += _clamp(_close_pos(le, side) * 10, 0, 10)
    day = _today(closed)
    if len(day) >= 6 and _ohlc_ok(day):
        d_hi, d_lo = float(day["high"].max()), float(day["low"].min())
        span = d_hi - d_lo
        if span > 0:
            pos = (entry - d_lo) / span if side is Side.BUY else (d_hi - entry) / span
            score += _clamp(pos * 10, 0, 10)
            if pos < 0.7:
                cons.append("l'entrée n'est pas à l'extrémité de l'amplitude du jour")
        targets = [(d_hi if side is Side.BUY else d_lo) + s * 0.5 * span]
    else:
        targets = []
    sr = _spread_ratio_h1(snap)
    if sr > 0.15:
        cons.append(f"spread {sr:.2f} ATR H1 : au-delà du seuil d'exécution, le gate refusera probablement")
    cons.append("entrée après un mouvement déjà accompli : le retour à la moyenne est le risque principal")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la dernière barre contraire", bt)
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)
