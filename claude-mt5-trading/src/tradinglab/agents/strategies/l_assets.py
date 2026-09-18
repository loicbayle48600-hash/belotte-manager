"""Famille L — spécialistes par classe d'actif : une stratégie propre par agent (L01 … L12).

Thème commun : chaque agent exploite une propriété du marché qu'il suit (or, indices, énergie, crypto, croisés JPY,
croisés EUR, majeures, mineures) avec un MOTEUR DE DÉCISION qui lui est propre — VWAP de session, plage asiatique,
canal de régression, profil de volume, extrême de la veille, cinématique d'EMA, canal de Donchian, choc de rendement,
grille des chiffres ronds, ancrage sur l'ouverture du jour, escalier de plus bas croissants, compression de
volatilité. Une simple différence de paramètres ne suffit pas : confirmations, filtres, logique de SL, plan de TP et
règle d'invalidation diffèrent d'un agent à l'autre, des familles déjà écrites (B, C, D…) et des screeners
génériques de repli (`AgentSpec.base_strategy`).

Conventions communes (voir `agents/screeners.py`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est la
  barre en formation et n'est JAMAIS lue (ni son close, ni son volume, ni ses extrêmes) ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14 du tf
  d'entrée, horodatage de la barre clôturée) et respecte `spec.timeframes` ;
- `_build` construit le `TradeCandidate` (pénalité de spread) et refuse tout SL du mauvais côté ; les agents de ce
  module remplacent ensuite le plan de TP générique par leur propre plan (`_set_tp_plan`, `rr >= max(1.5, rr)`) ;
- la distance entrée→SL est bornée par `_bound_sl` dans [0,3 ATR ; 3 ATR] du tf d'entrée, ET dans
  [0,3 ATR H1 ; 3,9 ATR H1] pour rester dans les bornes du gate (0,25-4 ATR H1) quel que soit le tf d'entrée ;
  puis le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (`_finalize`) : un SL refusé donne `None`,
  jamais une valeur corrigée à la volée ;
- quand cette borne déplace le SL par rapport au niveau décrit par la thèse, l'écart est DIT dans
  `arguments_against` (`_sl_note`) : le niveau d'invalidation annoncé n'est alors plus celui qui est protégé, et
  la sortie au stop peut précéder l'invalidation décrite ;
- `setup_score` = somme documentée de composantes, bornée 0-100 : ce n'est PAS une probabilité de gain ;
- l'univers de l'agent (`spec.markets`) est revérifié localement (`_market_ok`) : le Market Router filtre déjà
  régime / session / marché, on affine seulement ;
- les heures utilisées sont celles de la BARRE CLÔTURÉE (UTC), jamais l'horloge système ;
- données insuffisantes, colonne absente ou indicateur NaN → `None`, jamais une valeur inventée.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...market_data.indicators import daily_high_low, swing_points
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _frame, _mtf_bonus, _structure_sl, _trend_of, _valid, register  # noqa: F401

# Bornes de distance entrée→SL en ATR du tf d'entrée (le gate impose 0,25-4 ATR H1 ; on reste plus strict)
SL_MIN_ATR = 0.3
SL_MAX_ATR = 3.0
# Sur un tf d'entrée court (M15) l'ATR du tf est bien plus petit que l'ATR H1 : la distance minimale tient aussi
# compte de l'ATR H1 pour ne jamais proposer un stop que le gate refuserait.
SL_MIN_H1_ATR = 0.3
# Symétrique du plancher ci-dessus, et tout aussi nécessaire : sur un tf d'entrée LONG (H4 pour L07 et L12) l'ATR
# du tf vaut environ 2 x l'ATR H1, si bien qu'un stop de 2 ATR H4 dépasse le plafond du gate
# (`validate_stop_loss`, 4 x ATR H1) dès que la volatilité H4 est un peu plus de deux fois celle de H1 — c'est-à-
# dire précisément en tendance, quand ces agents déclenchent. Sans ce plafond, le candidat est construit puis
# refusé en silence par `_finalize` et l'agent paraît mort. Marge de 0,1 ATR H1 sous la limite du gate.
SL_MAX_H1_ATR = 3.9
# Une cible structurelle au-delà de 6 R n'est pas un objectif de trade réaliste : la cible finale y est plafonnée.
MAX_STRUCT_RR = 6.0

# Groupes de symboles de `config/markets.yaml` (racines) : un agent peut déclarer un groupe ou des racines explicites.
MARKET_GROUPS: dict[str, frozenset[str]] = {
    "forex_majors": frozenset({"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"}),
    "forex_minors": frozenset({"EURGBP", "EURJPY", "GBPJPY", "EURAUD", "EURCHF", "AUDJPY", "CADJPY", "GBPAUD",
                               "AUDNZD", "EURCAD"}),
}
# Classes d'actif renvoyées par `SymbolSpec.asset_class`
ASSET_CLASSES = frozenset({"metals", "indices", "energies", "crypto", "forex", "other"})


# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------
def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


def _side_from(tr: str) -> Optional[Side]:
    return Side.BUY if tr == "UP" else Side.SELL if tr == "DOWN" else None


def _root(symbol: str) -> str:
    """Racine du symbole broker (« EURUSD.m » → « EURUSD »), sans dépendance au package broker."""
    s = str(symbol).strip().upper()
    for sep in (".", "_", "-", "#", "!"):
        if sep in s:
            s = s.split(sep)[0]
    return s


def _market_ok(spec: AgentSpec, snap) -> bool:
    """Le symbole du snapshot appartient-il à l'univers déclaré par l'agent (classe d'actif ou racines) ?"""
    if snap.spec is None:
        return False
    root = _root(snap.symbol)
    cls = str(getattr(snap.spec, "asset_class", "") or "")
    for m in spec.markets:
        key = str(m)
        if key in ASSET_CLASSES:
            if cls == key:
                return True
        elif key in MARKET_GROUPS:
            if root in MARKET_GROUPS[key]:
                return True
        elif root == _root(key):
            return True
    return False


def _bar_hour(row: pd.Series) -> Optional[float]:
    """Heure décimale UTC d'ouverture de la barre clôturée (None si l'horodatage est illisible)."""
    try:
        ts = pd.Timestamp(row["time"])
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC")
        if pd.isna(ts):
            return None
        return float(ts.hour) + float(ts.minute) / 60.0
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
    """Spread courant en fraction de l'ATR du tf d'entrée (inf si inconnu : le filtre refuse alors)."""
    if not np.isfinite(atr) or atr <= 0 or snap.spec is None:
        return float("inf")
    ratio = float(snap.spread_points * snap.spec.point / atr)
    return ratio if np.isfinite(ratio) else float("inf")


def _spread_ratio_h1(snap) -> float:
    """Spread courant en fraction de l'ATR H1 (référence du gate : <= 0,15 ATR H1) ; inf si l'ATR H1 est inconnu.

    `NaN` doit renvoyer `inf`, pas `NaN` : une comparaison `NaN > seuil` est fausse, donc un ATR H1 manquant
    DÉSACTIVERAIT silencieusement le filtre de spread au lieu de le faire refuser.
    """
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not np.isfinite(atr_h1) or atr_h1 <= 0 or snap.spec is None:
        return float("inf")
    ratio = float(snap.spread_points * snap.spec.point / atr_h1)
    return ratio if np.isfinite(ratio) else float("inf")


def _bound_sl(snap, side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> Optional[float]:
    """Ramène la distance entrée→SL dans [max(lo·ATR, 0,3·ATR, 0,3·ATR H1) ; min(min(hi, 3)·ATR, 3,9·ATR H1)]
    sans changer de côté.

    Les deux bornes en ATR H1 encadrent le plafond et le plancher du gate (`validate_stop_loss` : 0,25 et 4 ATR
    H1) : sans elles, un stop parfaitement valide au regard du tf d'entrée serait refusé en silence par
    `_finalize` (cas d'un tf d'entrée H4, dont l'ATR vaut ~2 x l'ATR H1).
    Renvoie None si la borne basse dépasse la borne haute (configuration incohérente : on refuse plutôt que d'inventer).
    """
    if not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
        return None
    atr_h1 = float(snap.atr_h1 or 0.0)
    if not np.isfinite(atr_h1) or atr_h1 < 0:
        atr_h1 = 0.0
    lo_dist = max(lo * atr, SL_MIN_ATR * atr, SL_MIN_H1_ATR * atr_h1)
    hi_dist = min(hi, SL_MAX_ATR) * atr
    if atr_h1 > 0:
        hi_dist = min(hi_dist, SL_MAX_H1_ATR * atr_h1)
    if lo_dist > hi_dist:
        return None
    dist = min(max(abs(entry - sl), lo_dist), hi_dist)
    return entry - side.sign * dist


def _sl_note(side: Side, entry: float, raw_sl: float, sl: float, atr: float) -> Optional[str]:
    """Argument CONTRE honnête quand `_bound_sl` a dû déplacer le SL voulu par la thèse.

    Le niveau décrit par la thèse et par l'invalidation (bande VWAP, borne de la zone de valeur, extrême du
    balayage, canal de sortie, chiffre rond, départ de l'escalier…) n'est alors PLUS celui du SL réellement
    proposé : on le dit explicitement au lieu de laisser croire qu'il est protégé. `None` si le SL proposé est
    bien le niveau voulu. Ce n'est pas une correction : le SL reste celui que `_bound_sl` a calculé.
    """
    if not np.isfinite(raw_sl) or not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
        return None
    raw_d, d = abs(entry - raw_sl), abs(entry - sl)
    if d < raw_d - 1e-12:
        return (f"SL ramené à {d / atr:.2f} ATR (plafond de distance de l'agent) : le niveau visé par la thèse "
                f"({raw_sl:.5g}, soit {raw_d / atr:.2f} ATR) est plus loin et n'est donc plus protégé — "
                f"l'invalidation décrite peut survenir après la sortie au stop")
    if d > raw_d + 1e-12:
        return (f"SL élargi à {d / atr:.2f} ATR (distance minimale) : plus loin que le niveau visé par la thèse "
                f"({raw_sl:.5g}), le risque par trade porte sur cette distance élargie")
    return None


def _set_tp_plan(c: Optional[TradeCandidate], side: Side, entry: float, targets: list[float], rr_min: float,
                 first_r: float = 1.5) -> Optional[TradeCandidate]:
    """Remplace le plan de TP générique de `_build` par un plan propre à l'agent.

    - `targets` : cibles structurelles (niveaux, projections) ; seules celles situées à >= 0,5 R dans le sens du
      trade sont retenues ;
    - premier TP à `first_r` R (prise partielle), cible finale = max(`rr_min` R, cible structurelle la plus
      lointaine) → `rr >= max(1.5, rr_min)` garanti ;
    - la cible finale est plafonnée à `MAX_STRUCT_RR` R : au-delà, un objectif structurel n'est plus un objectif
      de trade réaliste (la gestion adaptative gère le reste) ;
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
    cap = entry + sign * dist * max(rr_min, MAX_STRUCT_RR)
    if sign * (final - entry) > sign * (cap - entry):
        final = cap
    # arrondi AVANT la comparaison : arrondir après ferait sortir la cible finale de sa propre borne
    final = round(final, 10)
    first = round(entry + sign * dist * max(0.5, first_r), 10)
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


def _rsi_band(p: dict, side: Side, lo_default: float, hi_default: float) -> tuple[float, float]:
    """Bande RSI acceptée : [rsi_lo, rsi_hi] pour un BUY, miroir [100-rsi_hi, 100-rsi_lo] pour un SELL."""
    lo, hi = float(p.get("rsi_lo", lo_default)), float(p.get("rsi_hi", hi_default))
    return (lo, hi) if side is Side.BUY else (100.0 - hi, 100.0 - lo)


def _opposed(lt: pd.Series, side: Side) -> bool:
    """Vrai si la tendance EMA du tf supérieur est franchement opposée au sens du trade."""
    tr = _trend_of(lt)
    return (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP")


def _day_bars(closed: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées appartenant au jour UTC de la dernière barre clôturée.

    Renvoie une fenêtre VIDE (jamais une exception) si la colonne `time` est absente, vide ou illisible : un
    horodatage NaT/non convertible est une donnée manquante, l'agent doit alors refuser, pas planter.
    """
    if closed is None or len(closed) == 0 or "time" not in closed.columns:
        return closed.iloc[0:0] if closed is not None else pd.DataFrame()
    t = pd.to_datetime(closed["time"], utc=True, errors="coerce")
    last = t.iloc[-1]
    if pd.isna(last):
        return closed.iloc[0:0]
    return closed[t.dt.normalize() == last.normalize()]


def _bars_between(closed: pd.DataFrame, start_h: float, end_h: float) -> pd.DataFrame:
    """Barres clôturées du jour UTC courant dont l'heure d'ouverture est dans [start_h, end_h)."""
    day = _day_bars(closed)
    if day.empty:
        return day
    t = pd.to_datetime(day["time"], utc=True, errors="coerce")
    h = t.dt.hour + t.dt.minute / 60.0
    return day[(h >= start_h) & (h < end_h)]


def _ohlc_ok(bars: pd.DataFrame) -> bool:
    """Vrai si les colonnes OHLC de la fenêtre sont exploitables (non vides, aucun NaN)."""
    if bars is None or bars.empty:
        return False
    return not bars[["open", "high", "low", "close"]].isna().to_numpy().any()


def _volumes(bars: pd.DataFrame) -> Optional[np.ndarray]:
    """Volumes tick de la fenêtre (None si la colonne est absente, NaN ou entièrement nulle)."""
    if "tick_volume" not in bars.columns:
        return None
    v = bars["tick_volume"].to_numpy(dtype=float)
    if v.size == 0 or not np.isfinite(v).all() or v.sum() <= 0:
        return None
    return v


def _vwap_sigma(bars: pd.DataFrame) -> Optional[tuple[float, float]]:
    """(VWAP, écart-type pondéré) des barres fournies, sur le prix typique (h+l+c)/3.

    Les poids sont les volumes tick ; si le volume est indisponible, les poids sont égaux (documenté dans les
    arguments du candidat). None si la fenêtre est vide, non exploitable ou de dispersion nulle.
    """
    if not _ohlc_ok(bars):
        return None
    tp = ((bars["high"] + bars["low"] + bars["close"]).astype(float) / 3.0).to_numpy()
    w = _volumes(bars)
    if w is None:
        w = np.ones(len(tp), dtype=float)
    total = float(w.sum())
    if total <= 0 or tp.size < 3:
        return None
    vwap = float((tp * w).sum() / total)
    var = float((w * (tp - vwap) ** 2).sum() / total)
    if not np.isfinite(vwap) or not np.isfinite(var) or var <= 0:
        return None
    return vwap, math.sqrt(var)


def _reg_channel(closes: np.ndarray) -> Optional[tuple[float, float, float, float]]:
    """Régression linéaire des clôtures : (pente par barre, valeur de la droite sur la dernière barre, écart-type
    des résidus, R²). None si moins de 10 points, données non finies ou dispersion nulle."""
    n = closes.size
    if n < 10 or not np.isfinite(closes).all():
        return None
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), closes.mean()
    var_x = float(((x - xm) ** 2).sum())
    ss_tot = float(((closes - ym) ** 2).sum())
    if var_x <= 0 or ss_tot <= 0:
        return None
    slope = float(((x - xm) * (closes - ym)).sum() / var_x)
    intercept = float(ym - slope * xm)
    resid = closes - (slope * x + intercept)
    ss_res = float((resid ** 2).sum())
    sd = math.sqrt(ss_res / (n - 2)) if n > 2 else 0.0
    if sd <= 0 or not np.isfinite(sd):
        return None
    r2 = float(1.0 - ss_res / ss_tot)
    return slope, float(slope * (n - 1) + intercept), sd, r2


def _volume_profile(bars: pd.DataFrame, bins: int = 24) -> Optional[tuple[float, float, float]]:
    """Profil de volume simplifié : (POC, borne basse, borne haute de la zone de valeur 70 %).

    Le volume de chaque barre est affecté au casier de son prix typique (approximation assumée : on ne connaît pas
    la répartition intrabar). None si le volume ou les prix sont indisponibles.
    """
    if len(bars) < 20 or not _ohlc_ok(bars):
        return None
    w = _volumes(bars)
    if w is None:
        return None
    lo, hi = float(bars["low"].min()), float(bars["high"].max())
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return None
    tp = ((bars["high"] + bars["low"] + bars["close"]).astype(float) / 3.0).to_numpy()
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    hist = np.zeros(bins, dtype=float)
    np.add.at(hist, idx, w)
    total = float(hist.sum())
    if total <= 0:
        return None
    centers = (edges[:-1] + edges[1:]) / 2.0
    k = int(np.argmax(hist))
    lo_i = hi_i = k
    acc = float(hist[k])
    while acc < 0.7 * total and (lo_i > 0 or hi_i < bins - 1):
        down = float(hist[lo_i - 1]) if lo_i > 0 else -1.0
        up = float(hist[hi_i + 1]) if hi_i < bins - 1 else -1.0
        if up >= down:
            hi_i += 1
            acc += float(hist[hi_i])
        else:
            lo_i -= 1
            acc += float(hist[lo_i])
    return float(centers[k]), float(edges[lo_i]), float(edges[hi_i + 1])


def _streak(closed: pd.DataFrame, side: Side, max_len: int = 12) -> int:
    """Longueur de l'escalier terminant sur la barre de signal : plus bas strictement croissants (BUY) ou plus
    hauts strictement décroissants (SELL). 0 si la dernière barre rompt déjà la séquence."""
    lows = closed["low"].to_numpy(dtype=float)
    highs = closed["high"].to_numpy(dtype=float)
    n = len(closed)
    k = 0
    for j in range(n - 1, max(n - 1 - max_len, 0), -1):
        prev, cur = (lows[j - 1], lows[j]) if side is Side.BUY else (highs[j - 1], highs[j])
        if not (np.isfinite(prev) and np.isfinite(cur)):
            break
        if (cur > prev) if side is Side.BUY else (cur < prev):
            k += 1
        else:
            break
    return k


def _figure_step(point: float) -> Optional[float]:
    """Pas de la grille des « chiffres ronds » : 50 pips (0,50 sur une paire JPY à 3 décimales)."""
    if not np.isfinite(point) or point <= 0:
        return None
    return 500.0 * float(point)


# --------------------------------------------------------------------------------------------------------------
# L01 — gold_london_pullback (M15 / H1, métaux, sessions LONDON & OVERLAP)
# --------------------------------------------------------------------------------------------------------------
@register("L01")
def strategy_l01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L01 — Or : repli sur la VWAP ancrée à l'ouverture de Londres, dans la tendance H1.

    Thèse : sur l'or, la VWAP ancrée à 07:00 UTC est le prix moyen payé par les intervenants de la séance
    européenne. Tant que la tendance H1 est en place, un retour du prix sur cette VWAP est un repli « au prix de
    revient » ; s'il est acheté (vendu) immédiatement, la séance repart dans le sens de la tendance. Aucun autre
    agent du dépôt ne raisonne sur une moyenne pondérée par le volume ancrée à une heure de session.

    Règles d'entrée : symbole de classe `metals` ; barre clôturée entre 07:00 et 17:00 UTC ; >= 6 barres M15
    clôturées depuis 07:00 ; tendance H1 (`_trend_of`) UP/DOWN → sens du trade ; au moins une des 3 dernières
    barres clôturées est venue au contact de la VWAP (bas <= VWAP + 0,25 ATR pour un achat) ; la barre de signal
    clôture au-delà de la
    VWAP de 0,1 ATR au moins et au-delà de la clôture précédente.
    Confirmation : corps de la barre de signal >= 35 % de son amplitude et clôture dans le sens du trade ; RSI14
    de la barre de contact dans la bande `rsi_lo`/`rsi_hi` (repli, pas retournement).
    Filtres : spread <= 12 % de l'ATR M15 et <= 15 % de l'ATR H1 (l'or paie cher) ; dispersion de la VWAP
    (sigma) entre 0,2 et 3 ATR — en dessous la séance n'a rien construit, au-dessus la VWAP n'a plus de sens.
    Logique de SL : sous (au-dessus de) la bande VWAP − 1 sigma, moins 0,1 ATR — la thèse est morte si la séance
    accepte des prix hors de la bande — distance bornée à [0,6 ATR ; `sl_atr` ATR].
    Plan de TP : bande opposée VWAP + 2 sigma puis extrême de la séance depuis 07:00 ; cible finale au moins
    `rr` R (`_set_tp_plan`).
    Invalidation : clôture M15 au-delà de la bande VWAP − 1 sigma (côté opposé au trade).
    Score : contact + tenue 20 · tendance 15 · MTF 0-15 · dispersion saine 0-10 · qualité de bougie 0-15 ·
    clôture au-delà de la précédente 5 — pénalités : RSI extrême, VWAP plate.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    hour = _bar_hour(le)
    if hour is None or not (7.0 <= hour < 17.0):
        return None
    sess = _bars_between(closed, 7.0, 24.0)
    if len(sess) < 6:
        return None
    vs = _vwap_sigma(sess)
    if vs is None:
        return None
    vwap, sigma = vs
    if not (0.2 * atr <= sigma <= 3.0 * atr):
        return None
    side = _side_from(_trend_of(lt))
    if side is None:
        return None
    s = side.sign
    entry = float(le["close"])
    if s * (entry - vwap) < 0.1 * atr:
        return None
    prev = closed.iloc[-2]
    if s * (entry - float(prev["close"])) <= 0 or s * (entry - float(le["open"])) <= 0:
        return None
    if _body_ratio(le) < 0.35:
        return None
    # contact avec la VWAP sur l'une des 3 dernières barres clôturées (signal inclus)
    last3 = closed.iloc[-3:]
    tol = 0.25 * atr      # « contact » = la barre est venue à 0,25 ATR de la VWAP
    touch_mask = (last3["low"] <= vwap + tol) if side is Side.BUY else (last3["high"] >= vwap - tol)
    if not bool(touch_mask.any()):
        return None
    touch = last3[touch_mask].iloc[-1]
    if not _valid(touch, "rsi14"):
        return None
    lo_r, hi_r = _rsi_band(p, side, 38.0, 62.0)
    if not (lo_r <= float(touch["rsi14"]) <= hi_r):
        return None
    if _spread_ratio(snap, atr) > 0.12 or _spread_ratio_h1(snap) > 0.15:
        return None
    raw_sl = vwap - s * (sigma + 0.1 * atr)
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.6, float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    sess_ext = float(sess["high"].max()) if side is Side.BUY else float(sess["low"].min())
    score = 20.0 + 15.0
    b, pros = _mtf_bonus(le, lt, side)
    score += b
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    ratio = sigma / atr
    score += _clamp(10.0 * (1.0 - abs(ratio - 1.0)), 0, 10)
    score += _clamp(_body_ratio(le) * 15.0, 0, 15) + 5.0
    pros += [f"repli au contact de la VWAP ancrée à 07:00 UTC ({vwap:.5f}) puis reprise",
             f"tendance {_trend_of(lt)} sur {spec.timeframes.get('trend', 'H1')}",
             f"RSI {float(touch['rsi14']):.0f} au contact (repli, pas retournement)",
             f"dispersion de séance {ratio:.2f} ATR"]
    if _volumes(sess) is None:
        cons.append("volume tick indisponible : VWAP calculée à poids égaux")
        score -= 5
    if (side is Side.BUY and le["rsi14"] > 75) or (side is Side.SELL and le["rsi14"] < 25):
        cons.append(f"RSI {float(le['rsi14']):.0f} extrême : entrée tardive possible")
        score -= 10
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de la bande VWAP -1 sigma (côté opposé au trade)", bt)
    cand = _set_tp_plan(cand, side, entry, [vwap + s * 2.0 * sigma, sess_ext], float(p.get("rr", 2.0)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L02 — gold_mean_reversion_asia (M15 / H1, métaux, session ASIA)
# --------------------------------------------------------------------------------------------------------------
@register("L02")
def strategy_l02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L02 — Or : fausse sortie des bornes de la plage asiatique, retour vers sa médiane.

    Thèse : pendant la séance asiatique l'or oscille dans une plage étroite faute de flux directionnel. Une barre
    qui dépasse à peine une borne puis clôture à l'intérieur est une prise de liquidité sans suite : le prix
    revient vers la médiane de la plage. Contrairement aux agents de cassure (C01/C08) qui ACHÈTENT la sortie de
    la plage asiatique, L02 la VEND ; contrairement aux agents de range génériques, la plage est définie par
    l'horloge (00:00-08:00 UTC), pas par des pivots.

    Règles d'entrée : symbole de classe `metals` ; barre clôturée entre 00:00 et 08:00 UTC ; >= 8 barres M15
    clôturées depuis 00:00 AVANT la barre de signal ; plage = (haut, bas) de ces barres seulement — la barre de
    signal en est exclue, sans quoi elle serait elle-même la borne et aucun dépassement ne serait mesurable ;
    largeur comprise entre 0,8 ATR M15 et 2,2 ATR H1 (séance calme) ; la barre de signal dépasse une borne de
    moins de 0,45 ATR et clôture à l'intérieur (>= 0,05 ATR sous la borne pour une vente) ; les bornes de la
    plage n'ont été atteintes que par des mèches (aucune clôture à moins de 0,15 ATR d'une borne).
    Confirmation : mèche de rejet >= 35 % de l'amplitude de la barre ; RSI14 dans [`rsi_lo`, `rsi_hi`] — la sortie
    n'est pas portée par le momentum.
    Filtres : spread <= 12 % de l'ATR M15 ; médiane de la plage à >= 0,5 ATR de l'entrée (sinon aucune marge).
    Logique de SL : au-delà de l'extrême du dépassement + 0,3 ATR (le balayage doit rester le point haut de la
    séance), distance bornée à [0,4 ATR ; `sl_atr` ATR].
    Plan de TP : médiane de la plage (prise partielle dès 1 R) puis borne opposée ; cible finale au moins `rr` R.
    Invalidation : clôture M15 hors de la plage asiatique du côté du dépassement.
    Score : plage calme 20 · rejet 0-20 · faible pénétration 0-15 · RSI neutre 0-10 · espace vers la médiane 0-20
    · qualité de clôture 0-10 — pénalité si l'ADX M15 montre déjà une tendance.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    hour = _bar_hour(le)
    if hour is None or not (0.0 <= hour < 8.0):
        return None
    asia = _bars_between(closed, 0.0, 8.0)
    if len(asia) < 9 or not _ohlc_ok(asia):
        return None
    # La plage est mesurée sur les barres qui PRÉCÈDENT la barre de signal. Si la barre de signal était incluse,
    # elle serait elle-même la borne de la plage (`high.max()` >= son propre haut) : `up_poke`/`dn_poke` seraient
    # négatifs ou nuls par construction et l'agent ne pourrait JAMAIS produire de candidat.
    prior = asia.iloc[:-1]
    hi, lo = float(prior["high"].max()), float(prior["low"].min())
    width = hi - lo
    atr_h1 = float(snap.atr_h1 or 0.0)
    if width <= 0 or width < 0.8 * atr or (atr_h1 > 0 and width > 2.2 * atr_h1):
        return None
    entry = float(le["close"])
    up_poke = float(le["high"]) - hi
    dn_poke = lo - float(le["low"])
    if up_poke > 0 and entry < hi - 0.05 * atr and up_poke <= 0.45 * atr:
        side, poke, level = Side.SELL, up_poke, hi
    elif dn_poke > 0 and entry > lo + 0.05 * atr and dn_poke <= 0.45 * atr:
        side, poke, level = Side.BUY, dn_poke, lo
    else:
        return None
    # « aucune borne acceptée en clôture » : comparer les clôtures de `prior` à `hi`/`lo`, qui sont les extrêmes
    # de ces mêmes barres, serait un test toujours faux (donc vide de sens). La condition réellement utile est que
    # les bornes n'aient été atteintes QUE par des mèches : aucune clôture de la plage ne colle à une borne.
    if float(prior["close"].max()) > hi - 0.15 * atr or float(prior["close"].min()) < lo + 0.15 * atr:
        return None  # une borne a déjà été acceptée en clôture : ce n'est plus une fausse sortie
    if _wick_against(le, side) < 0.35:
        return None
    lo_r, hi_r = float(p.get("rsi_lo", 30)), float(p.get("rsi_hi", 70))
    if not (lo_r <= float(le["rsi14"]) <= hi_r):
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    median = (hi + lo) / 2.0
    s = side.sign
    if s * (median - entry) < 0.5 * atr:
        return None
    raw_sl = (float(le["high"]) + 0.3 * atr) if side is Side.SELL else (float(le["low"]) - 0.3 * atr)
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.4, float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    dist = abs(entry - sl)
    room = s * (median - entry) / dist if dist > 0 else 0.0
    score = 20.0 + _clamp(_wick_against(le, side) * 20.0, 0, 20) + _clamp((0.45 - poke / atr) * 33.0, 0, 15)
    score += _clamp(10.0 - abs(float(le["rsi14"]) - 50.0) * 0.4, 0, 10) + _clamp(room * 10.0, 0, 20)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros = [f"plage asiatique calme ({width / atr:.1f} ATR M15) intacte depuis 00:00 UTC",
            f"dépassement de {poke / atr:.2f} ATR rejeté en clôture",
            f"mèche de rejet {_wick_against(le, side):.0%}", f"médiane de plage à {room:.1f} R"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if float(le["adx14"]) > 25:
        cons.append(f"ADX M15 {float(le['adx14']):.0f} : la séance n'est plus totalement sans direction")
        score -= 10
    if _opposed(lt, side):
        cons.append("tendance H1 opposée au retour à la moyenne")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.5)), score, pros, cons,
                  "clôture M15 hors de la plage asiatique du côté du dépassement", bt)
    opposite = lo if side is Side.SELL else hi
    cand = _set_tp_plan(cand, side, entry, [median, opposite], float(p.get("rr", 1.5)), first_r=1.0)
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L03 — index_trend_ny (M15 / H1, indices, sessions NEWYORK & OVERLAP)
# --------------------------------------------------------------------------------------------------------------
@register("L03")
def strategy_l03(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L03 — Indices : achat des creux DANS un canal de régression linéaire de la séance américaine.

    Thèse : un indice en tendance intrajournalière progresse le long d'une droite de régression avec des écarts
    bornés. Tant que la régression des 60 dernières barres explique réellement le prix (R² élevé), un repli vers
    le bas du canal est une opportunité mesurable ; la sortie du canal, elle, dit que le modèle linéaire n'est
    plus valable. Aucun autre agent n'utilise de régression : c'est le moteur propre de L03.

    Règles d'entrée : symbole de classe `indices` ; barre clôturée entre 12:00 et 21:00 UTC ; régression des 60
    clôtures clôturées valide avec R² >= 0,5 ; sens donné par le signe de la pente, confirmé par une tendance H1
    non opposée ; écart normalisé z = (clôture − droite)/sigma_résidus dans [−2,0 ; −0,2] pour un achat (repli
    dans le canal, pas cassure) ; pente >= 0,02 ATR par barre.
    Confirmation : ADX14 >= `adx_min` et clôture supérieure à la clôture précédente (le repli s'arrête).
    Filtres : spread <= 10 % de l'ATR M15 ; sigma des résidus entre 0,3 et 4 ATR (canal exploitable).
    Logique de SL : sous (au-dessus de) la borne extérieure du canal (droite − 2,4 sigma) − 0,2 ATR, distance
    bornée à [0,6 ATR ; `sl_atr` ATR] : sortir du canal invalide la régression, donc le trade.
    Plan de TP : droite de régression (retour à la moyenne du canal), bord opposé (droite + 2 sigma) puis
    projection de la droite 10 barres plus loin ; cible finale au moins `rr` R.
    Invalidation : clôture M15 hors du canal (−2,4 sigma) ou pente qui s'inverse.
    Score : R² 0-25 · pente 0-15 · MTF 0-15 · profondeur du repli 0-15 · ADX 0-10 · reprise de clôture 0-10.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 80:
        return None
    hour = _bar_hour(le)
    if hour is None or not (12.0 <= hour < 21.0):
        return None
    win = closed["close"].iloc[-60:].to_numpy(dtype=float)
    reg = _reg_channel(win)
    if reg is None:
        return None
    slope, line, sd, r2 = reg
    if r2 < 0.5 or not (0.3 * atr <= sd <= 4.0 * atr):
        return None
    side = Side.BUY if slope > 0 else Side.SELL
    s = side.sign
    if s * slope < 0.02 * atr:
        return None
    if _opposed(lt, side):
        return None
    entry = float(le["close"])
    z = s * (entry - line) / sd
    if not (-2.0 <= z <= -0.2):
        return None
    if float(le["adx14"]) < float(p.get("adx_min", 25)):
        return None
    if s * (entry - float(closed["close"].iloc[-2])) <= 0:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    raw_sl = line - s * (2.4 * sd + 0.2 * atr)
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.6, float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    score = _clamp((r2 - 0.5) * 50.0, 0, 25) + _clamp(abs(slope) / atr * 300.0, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b + _clamp((-z) * 7.5, 0, 15) + _clamp((float(le["adx14"]) - float(p.get("adx_min", 25))) * 1.0, 0, 10)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros += [f"canal de régression 60 barres valide (R² {r2:.2f})",
             f"pente {slope / atr:.3f} ATR/barre dans le sens du trade",
             f"repli à {z:.1f} sigma dans le canal (pas de sortie)", f"ADX {float(le['adx14']):.0f}"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if z < -1.6:
        cons.append("repli profond : le canal est sur le point d'être cassé")
    if r2 < 0.65:
        cons.append(f"linéarité moyenne (R² {r2:.2f}) : le canal peut s'élargir")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 hors du canal de régression (-2,4 sigma) ou pente inversée", bt)
    cand = _set_tp_plan(cand, side, entry, [line, line + s * 2.0 * sd, line + slope * 10.0 + s * sd],
                        float(p.get("rr", 2.0)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L04 — index_pullback_ny (M15 / H1, indices)
# --------------------------------------------------------------------------------------------------------------
@register("L04")
def strategy_l04(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L04 — Indices : repli sur le point de contrôle (POC) du profil de volume, valeur acceptée.

    Thèse : sur un indice, le prix où le plus de volume s'est échangé (POC) est la valeur de référence des 80
    dernières barres. Quand le marché cote AU-DESSUS de cette valeur et y revient sans l'accepter (mèche dans la
    zone de valeur, clôture au-dessus du POC), les acheteurs défendent la valeur : c'est le repli à acheter. Le
    moteur — profil de volume et zone de valeur 70 % — n'est utilisé par aucun autre agent (les autres replis
    visent une EMA, un Fibonacci ou un ancien niveau).

    Règles d'entrée : symbole de classe `indices` ; >= 60 barres clôturées avec volume tick exploitable ; profil
    de volume sur les 48 dernières barres clôturées (~12 h de cotation) → POC et zone de valeur ; tendance H1 UP/DOWN → sens du
    trade ; les 3 barres précédant le signal ont clôturé du bon côté du POC ; la barre de signal descend dans la
    zone (bas <= POC + 0,35 ATR pour un achat) et clôture au-dessus du POC, dans le sens du trade.
    Confirmation : volume tick de la barre de signal >= moyenne des 20 barres clôturées précédentes (la défense
    de la valeur se fait avec du volume) ; distance entrée→POC <= 1,5 ATR (repli, pas éloignement).
    Filtres : spread <= 10 % de l'ATR M15 ; zone de valeur d'une largeur >= 0,8 ATR (sinon profil dégénéré).
    Logique de SL : sous (au-dessus de) la borne opposée de la zone de valeur − 0,25 ATR : accepter la valeur du
    mauvais côté invalide la thèse ; distance bornée à [0,5 ATR ; `sl_atr` ATR].
    Plan de TP : borne haute (basse) de la zone de valeur, puis extension d'une demi-largeur de zone au-delà ;
    cible finale au moins `rr` R.
    Invalidation : clôture M15 au-delà du POC du mauvais côté.
    Score : acceptation de la valeur 20 · tendance 10 · MTF 0-15 · volume relatif 0-15 · proximité du POC 0-15 ·
    qualité de clôture 0-10 · largeur de zone saine 0-10.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60:
        return None
    window = closed.iloc[-48:]      # ~12 h de cotation en M15 : la valeur de la séance, pas celle de la semaine
    prof = _volume_profile(window)
    if prof is None:
        return None
    poc, va_low, va_high = prof
    if (va_high - va_low) < 0.8 * atr:
        return None
    side = _side_from(_trend_of(lt))
    if side is None:
        return None
    s = side.sign
    entry = float(le["close"])
    prev3 = closed.iloc[-4:-1]
    if not bool((s * (prev3["close"] - poc) > 0).all()):
        return None
    touched = (float(le["low"]) <= poc + 0.35 * atr) if side is Side.BUY else (float(le["high"]) >= poc - 0.35 * atr)
    if not touched or s * (entry - poc) <= 0 or s * (entry - float(le["open"])) <= 0:
        return None
    gap = abs(entry - poc)
    if gap > 1.5 * atr:
        return None
    vols = _volumes(closed.iloc[-21:])
    if vols is None:
        return None
    vol_ma = float(vols[:-1].mean())
    vol_now = float(vols[-1])
    if not (vol_ma > 0) or vol_now < vol_ma:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    far = va_low if side is Side.BUY else va_high
    raw_sl = far - s * 0.25 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 1.0)))
    if sl is None:
        return None
    width = va_high - va_low
    score = 20.0 + 10.0
    b, pros = _mtf_bonus(le, lt, side)
    score += b + _clamp((vol_now / vol_ma - 1.0) * 30.0, 0, 15) + _clamp((1.5 - gap / atr) * 12.0, 0, 15)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10) + _clamp(10.0 - abs(width / atr - 2.0) * 4.0, 0, 10)
    pros += [f"POC (valeur) à {poc:.5f}, zone de valeur large de {width / atr:.1f} ATR",
             "3 clôtures du bon côté de la valeur avant le repli",
             f"volume de défense {vol_now / vol_ma:.1f}x la moyenne 20 barres",
             f"repli à {gap / atr:.2f} ATR du POC"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    near = va_high if side is Side.BUY else va_low
    if s * (near - entry) <= 0:
        cons.append("le prix est déjà au bord opposé de la zone de valeur : marge réduite")
    if float(le["adx14"]) < 18:
        cons.append(f"ADX M15 {float(le['adx14']):.0f} faible : la valeur peut être re-testée plusieurs fois")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà du POC du mauvais côté (valeur acceptée)", bt)
    cand = _set_tp_plan(cand, side, entry, [near, near + s * 0.5 * width], float(p.get("rr", 2.0)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L05 — index_failed_breakout (M15 / H1, indices, session NEWYORK)
# --------------------------------------------------------------------------------------------------------------
@register("L05")
def strategy_l05(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L05 — Indices : piège au-delà de l'extrême de la VEILLE, retour vers la médiane du range journalier.

    Thèse : dans une séance américaine sans tendance, l'extrême de la veille attire le prix (stops et ordres au
    marché). Quand il est dépassé puis que les barres suivantes clôturent nettement à l'intérieur, la cassure a
    échoué et le prix repart chercher le milieu du range de la veille. Le niveau de référence est journalier (D1),
    ce qui distingue L05 des faux breakouts intrajournaliers des autres familles.

    Règles d'entrée : symbole de classe `indices` ; barre clôturée entre 12:00 et 21:00 UTC ; frame D1 avec >= 3
    barres → extrême de la veille (`daily_high_low`, avant-dernière barre D1) ; l'une des 4 dernières barres M15
    clôturées a dépassé l'extrême en séance (haut > plus haut de la veille) ; la barre de signal clôture à >= 0,15
    ATR À L'INTÉRIEUR du range de la veille ; aucune barre du jour n'avait clôturé au-delà avant le balayage.
    Confirmation : au moins 2 des 3 dernières clôtures à l'intérieur du range (rejet persistant) ; RSI14 revenu
    du mauvais côté de 50 (< 55 pour une vente) ou mèche >= 30 %.
    Filtres : ADX14 M15 <= 32 (pas de tendance franche à contrer) ; spread <= 10 % de l'ATR M15 ; pénétration
    <= 1,2 ATR (au-delà ce n'est plus un piège mais une vraie cassure).
    Logique de SL : au-delà de l'extrême du balayage + 0,3 ATR (un nouvel extrême nie le piège), distance bornée
    à [0,5 ATR ; `sl_atr` ATR].
    Plan de TP : médiane du range de la veille puis extrême opposé de la veille ; cible finale au moins `rr` R.
    Invalidation : clôture M15 au-delà de l'extrême de la veille.
    Score : piège 25 · rejet persistant 0-15 · faible pénétration 0-15 · absence de tendance 0-15 · espace vers
    la médiane 0-20 · qualité de clôture 0-10.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    hour = _bar_hour(le)
    if hour is None or not (12.0 <= hour < 21.0):
        return None
    d1 = snap.frames.get("D1")
    if d1 is None or len(d1) < 3:
        return None
    ph, pl = daily_high_low(d1)
    if not (np.isfinite(ph) and np.isfinite(pl)) or ph <= pl:
        return None
    entry = float(le["close"])
    recent = closed.iloc[-4:]
    swept_up = float(recent["high"].max()) > ph
    swept_dn = float(recent["low"].min()) < pl
    if swept_up and entry <= ph - 0.15 * atr:
        side, level, sweep_ext = Side.SELL, ph, float(recent["high"].max())
    elif swept_dn and entry >= pl + 0.15 * atr:
        side, level, sweep_ext = Side.BUY, pl, float(recent["low"].min())
    else:
        return None
    s = side.sign
    if abs(sweep_ext - level) > 1.2 * atr:
        return None
    day = _day_bars(closed)
    earlier = day.iloc[:-4] if len(day) > 4 else day.iloc[:0]
    if side is Side.SELL and bool((earlier["close"] > ph).any()):
        return None
    if side is Side.BUY and bool((earlier["close"] < pl).any()):
        return None
    last3 = closed["close"].iloc[-3:]
    inside = int(((last3 < ph) & (last3 > pl)).sum())
    if inside < 2:
        return None
    rsi = float(le["rsi14"])
    rsi_back = (rsi < 55.0) if side is Side.SELL else (rsi > 45.0)
    if not rsi_back and _wick_against(le, side) < 0.30:
        return None
    if float(le["adx14"]) > 32:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    raw_sl = sweep_ext - s * 0.3 * atr      # au-delà de l'extrême du balayage
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    dist = abs(entry - sl)
    mid = (ph + pl) / 2.0
    room = s * (mid - entry) / dist if dist > 0 else 0.0
    if room < 0.8:
        return None
    pen = abs(sweep_ext - level) / atr
    score = 25.0 + _clamp((inside - 1) * 7.5, 0, 15) + _clamp((1.2 - pen) * 12.5, 0, 15)
    score += _clamp((32.0 - float(le["adx14"])) * 1.0, 0, 15) + _clamp(room * 8.0, 0, 20)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros = [f"extrême de la veille ({level:.5f}) dépassé de {pen:.2f} ATR puis rejeté",
            f"{inside}/3 dernières clôtures de retour dans le range de la veille",
            f"ADX M15 {float(le['adx14']):.0f} : pas de tendance franche à contrer",
            f"médiane du range de la veille à {room:.1f} R"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if _opposed(lt, side):
        cons.append("tendance H1 opposée : le piège se trade contre le tf supérieur")
    if not rsi_back:
        cons.append(f"RSI {rsi:.0f} encore du côté de la cassure : rejet validé par la mèche seule")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'extrême de la veille", bt)
    opp = pl if side is Side.SELL else ph
    cand = _set_tp_plan(cand, side, entry, [mid, opp], float(p.get("rr", 2.0)), first_r=1.0)
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L06 — energy_trend_h1 (M15 / H1, énergies)
# --------------------------------------------------------------------------------------------------------------
@register("L06")
def strategy_l06(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L06 — Énergie : suivi de la CINÉMATIQUE de l'EMA50 (vitesse du prix moyen), objectif projeté.

    Thèse : le pétrole avance par poussées ; ce qui dure, c'est la VITESSE de sa moyenne mobile. Tant que
    l'EMA50 M15 se déplace d'au moins 0,05 ATR par barre, la poussée est en cours, et l'objectif raisonnable
    n'est pas un multiple de R arbitraire mais la distance que cette vitesse parcourt en 20 à 40 barres. Le SL
    s'élargit avec la vitesse (un marché rapide respire plus). Ce raisonnement « pente → objectif → SL » est
    propre à L06.

    Règles d'entrée : symbole de classe `energies` ; barre clôturée à partir de 06:00 UTC (hors creux asiatique
    illiquide) ; vitesse de l'EMA50 sur 10 barres clôturées >= 0,05 ATR/barre → sens du trade ; 3 dernières
    clôtures du bon côté de l'EMA50 ; clôture du bon côté de l'EMA20 ; distance clôture→EMA20 <= 1,0 ATR (on ne
    court pas après le prix).
    Confirmation : ADX14 >= `adx_min` et tendance H1 non opposée.
    Filtres : amplitude de la barre de signal <= 2,5 ATR (pas d'entrée sur une barre de choc, fréquente sur le
    pétrole) ; spread <= 12 % de l'ATR M15.
    Logique de SL : EMA50 − k·ATR avec k = 0,8 + 10·|vitesse|/ATR borné à [0,8 ; 1,5] (plus la poussée est
    rapide, plus le stop est large), puis distance bornée à [0,6 ATR ; `sl_atr` ATR].
    Plan de TP : projections cinématiques à 20 et 40 barres (clôture + vitesse × N) ; cible finale au moins `rr` R.
    Invalidation : clôture M15 au-delà de l'EMA50 ou vitesse de l'EMA50 qui s'annule.
    Score : vitesse 0-25 · alignement EMA 15 · ADX 0-15 · proximité de l'EMA20 0-15 · MTF 0-15 · qualité de
    clôture 0-10 — pénalité si la volatilité est déjà extrême (`vol_pct` > 85).
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    hour = _bar_hour(le)
    if hour is None or hour < 6.0:
        return None
    ema50 = closed["ema50"]
    if pd.isna(ema50.iloc[-1]) or pd.isna(ema50.iloc[-11]):
        return None
    speed = float(ema50.iloc[-1] - ema50.iloc[-11]) / 10.0      # déplacement de l'EMA50 par barre
    if abs(speed) < 0.05 * atr:
        return None
    side = Side.BUY if speed > 0 else Side.SELL
    s = side.sign
    last3 = closed.iloc[-3:]
    if not bool((s * (last3["close"] - last3["ema50"]) > 0).all()):
        return None
    entry = float(le["close"])
    if s * (entry - float(le["ema20"])) <= 0 or abs(entry - float(le["ema20"])) > 1.0 * atr:
        return None
    if float(le["adx14"]) < float(p.get("adx_min", 25)) or _opposed(lt, side):
        return None
    if _range(le) > 2.5 * atr:
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    k = _clamp(0.8 + 10.0 * abs(speed) / atr, 0.8, 1.5)
    raw_sl = float(le["ema50"]) - s * k * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.6, float(p.get("sl_atr", 1.8)))
    if sl is None:
        return None
    score = _clamp(abs(speed) / atr * 200.0, 0, 25) + 15.0
    score += _clamp((float(le["adx14"]) - float(p.get("adx_min", 25))) * 1.5, 0, 15)
    score += _clamp((1.0 - abs(entry - float(le["ema20"])) / atr) * 15.0, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b + _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros += [f"vitesse de l'EMA50 = {speed / atr:.3f} ATR/barre", "3 clôtures consécutives du bon côté de l'EMA50",
             f"clôture à {abs(entry - float(le['ema20'])) / atr:.2f} ATR de l'EMA20 (pas de poursuite)",
             f"ADX {float(le['adx14']):.0f}", f"stop élargi à k={k:.2f} ATR sous l'EMA50 (proportionnel à la vitesse)"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if _valid(le, "vol_pct") and float(le["vol_pct"]) > 85:
        cons.append(f"volatilité au {float(le['vol_pct']):.0f}e percentile : slippage probable")
        score -= 10
    if _trend_of(lt) == "FLAT":
        cons.append("tendance H1 neutre : la poussée est purement intrajournalière")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà de l'EMA50 ou vitesse de l'EMA50 qui s'annule", bt)
    cand = _set_tp_plan(cand, side, entry, [entry + speed * 20.0, entry + speed * 40.0], float(p.get("rr", 2.0)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L07 — crypto_trend_h4 (H4 / D1, crypto)
# --------------------------------------------------------------------------------------------------------------
@register("L07")
def strategy_l07(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L07 — Crypto : canal de Donchian 20/10 sur H4, entrée sur nouvelle borne, stop sur le canal de sortie.

    Thèse : la crypto cote 24 h/24 et produit des tendances longues entrecoupées de fausses sorties. La règle
    « clôture au-delà du plus haut des 20 barres, sortie sur le plus bas des 10 barres » (canal de Donchian) est
    le moteur de suivi de tendance le plus robuste sur ce marché, à condition de n'accepter que la PREMIÈRE
    cassure d'une série (les suivantes sont des poursuites). Aucun autre agent n'utilise de canal de Donchian ni
    de stop de canal.

    Règles d'entrée : symbole de classe `crypto` ; >= 60 barres H4 clôturées ; canal = plus haut / plus bas des
    20 barres clôturées PRÉCÉDANT la barre de signal ; clôture au-delà de la borne → sens du trade ; aucune des 5
    barres clôturées précédentes n'avait clôturé au-delà de cette borne (cassure fraîche).
    Confirmation : ADX14 >= `adx_min`, `mom10` du bon signe, tendance D1 non opposée.
    Filtres : ATR14 <= 2 × sa moyenne 50 barres (pas d'entrée en pleine explosion) ; spread <= 12 % de l'ATR H4 ;
    largeur du canal >= 1,5 ATR (sinon le canal est du bruit).
    Logique de SL : canal de sortie = plus bas (haut) des 10 barres clôturées − 0,2 ATR, distance bornée à
    [0,8 ATR ; `sl_atr` ATR] — c'est le niveau qui ferait sortir la position, pas un multiple arbitraire.
    Plan de TP : projection de la largeur du canal depuis la borne cassée, puis 1,5 × cette largeur ; cible
    finale au moins `rr` R.
    Invalidation : clôture H4 sous le canal de sortie (plus bas des 10 barres).
    Score : cassure 25 · fraîcheur 15 · ADX 0-15 · MTF 0-15 · momentum 0-10 · volatilité maîtrisée 0-10 ·
    qualité de clôture 0-10.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60 or not _ohlc_ok(closed.iloc[-30:]):
        return None
    prior = closed.iloc[-21:-1]
    hi20, lo20 = float(prior["high"].max()), float(prior["low"].min())
    width = hi20 - lo20
    if width < 1.5 * atr:
        return None
    entry = float(le["close"])
    if entry > hi20:
        side, level = Side.BUY, hi20
    elif entry < lo20:
        side, level = Side.SELL, lo20
    else:
        return None
    s = side.sign
    prev5 = closed["close"].iloc[-6:-1]
    if bool((s * (prev5 - level) > 0).any()):
        return None  # la borne a déjà été franchie : poursuite, pas cassure
    if float(le["adx14"]) < float(p.get("adx_min", 25)):
        return None
    if not _valid(le, "mom10") or s * float(le["mom10"]) <= 0:
        return None
    if _opposed(lt, side):
        return None
    atr_ma = float(closed["atr14"].iloc[-50:].mean())
    if not (atr_ma > 0) or atr > 2.0 * atr_ma:
        return None
    if _spread_ratio(snap, atr) > 0.12:
        return None
    exit_chan = float(closed["low"].iloc[-10:].min()) if side is Side.BUY else float(closed["high"].iloc[-10:].max())
    raw_sl = exit_chan - s * 0.2 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.8, float(p.get("sl_atr", 2.0)))
    if sl is None:
        return None
    score = 25.0 + 15.0 + _clamp((float(le["adx14"]) - float(p.get("adx_min", 25))) * 1.5, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b + _clamp(abs(float(le["mom10"])) / atr * 10.0, 0, 10)
    score += _clamp((2.0 - atr / atr_ma) * 10.0, 0, 10) + _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros += [f"cassure du canal de Donchian 20 barres ({level:.5f}), largeur {width / atr:.1f} ATR",
             "aucune clôture au-delà de la borne sur les 5 barres précédentes (cassure fraîche)",
             f"ADX {float(le['adx14']):.0f}, momentum 10 barres dans le sens du trade",
             f"ATR = {atr / atr_ma:.2f}x sa moyenne 50 (pas d'explosion)"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if abs(entry - level) > 0.8 * atr:
        cons.append(f"clôture {abs(entry - level) / atr:.1f} ATR au-delà de la borne : entrée étendue")
    if _trend_of(lt) == "FLAT":
        cons.append("tendance D1 neutre : la cassure H4 n'est pas soutenue par le tf supérieur")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "clôture H4 au-delà du canal de sortie (plus bas/haut des 10 barres)", bt)
    cand = _set_tp_plan(cand, side, entry, [level + s * width, level + s * 1.5 * width], float(p.get("rr", 2.5)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L08 — crypto_bollinger_mr (M15 / H1, crypto)
# --------------------------------------------------------------------------------------------------------------
@register("L08")
def strategy_l08(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L08 — Crypto : choc de RENDEMENT (liquidations) puis stabilisation, retour vers la moyenne.

    Thèse : en crypto, les cascades de liquidation produisent une barre dont le rendement vaut plusieurs
    écarts-types de la distribution récente. Ce n'est pas une information, c'est un déséquilibre mécanique : dès
    que la barre suivante cesse de faire de nouveaux extrêmes, le prix revient au moins vers sa moyenne. Le
    déclencheur est mesuré sur les RENDEMENTS (z-score de `ret1`), pas sur le prix : c'est ce qui distingue L08
    des agents de réintégration de bandes.

    Règles d'entrée : symbole de classe `crypto` ; >= 80 barres clôturées ; écart-type des 50 rendements
    clôturés précédant le choc > 0 ; barre de CHOC = avant-dernière barre clôturée avec |z| >= 2,5 et clôture
    au-delà de la bande de Bollinger (sous `bb_low` pour un achat) ; barre de SIGNAL = dernière barre clôturée,
    qui ne fait pas de nouvel extrême (pas de plus bas sous celui du choc), clôture dans le sens du rebond et
    réintègre les bandes.
    Confirmation : RSI14 de la barre de choc <= `rsi_lo` (>= `rsi_hi` pour une vente) ; amplitude de la barre de
    signal <= amplitude du choc (la panique retombe).
    Filtres : spread <= 15 % de l'ATR M15 ; `bb_mid` disponible et à >= 0,6 ATR de l'entrée (sinon aucune marge).
    Logique de SL : au-delà de l'extrême du choc − 0,3 ATR (le choc doit rester le point extrême), distance
    bornée à [0,5 ATR ; `sl_atr` ATR].
    Plan de TP : 50 % du corps du choc récupérés, puis médiane de Bollinger ; cible finale au moins `rr` R.
    Invalidation : nouvelle clôture au-delà de l'extrême du choc.
    Score : amplitude du choc 0-25 · RSI extrême 0-15 · réintégration des bandes 20 · apaisement 0-10 · espace
    vers la moyenne 0-20 · qualité de clôture 0-10 — pénalité si la tendance H1 est franchement opposée.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 80 or "ret1" not in closed.columns:
        return None
    if not _valid(le, "bb_mid", "bb_up", "bb_low"):
        return None
    shock = closed.iloc[-2]
    if not _valid(shock, "bb_up", "bb_low", "rsi14", "ret1"):
        return None
    hist = closed["ret1"].iloc[-53:-2].to_numpy(dtype=float)
    hist = hist[np.isfinite(hist)]
    if hist.size < 30:
        return None
    sd = float(hist.std(ddof=1))
    if not (sd > 0):
        return None
    z = float(shock["ret1"]) / sd
    if abs(z) < 2.5:
        return None
    side = Side.BUY if z < 0 else Side.SELL
    s = side.sign
    if side is Side.BUY and not float(shock["close"]) < float(shock["bb_low"]):
        return None
    if side is Side.SELL and not float(shock["close"]) > float(shock["bb_up"]):
        return None
    lo_r, hi_r = float(p.get("rsi_lo", 25)), float(p.get("rsi_hi", 75))
    if side is Side.BUY and float(shock["rsi14"]) > lo_r:
        return None
    if side is Side.SELL and float(shock["rsi14"]) < hi_r:
        return None
    entry = float(le["close"])
    ext = float(shock["low"]) if side is Side.BUY else float(shock["high"])
    new_ext = (float(le["low"]) < ext) if side is Side.BUY else (float(le["high"]) > ext)
    if new_ext:
        return None
    if s * (entry - float(le["open"])) <= 0 or s * (entry - float(shock["close"])) <= 0:
        return None
    reint = (entry > float(le["bb_low"])) if side is Side.BUY else (entry < float(le["bb_up"]))
    if not reint:
        return None
    if _range(le) > _range(shock):
        return None
    mid = float(le["bb_mid"])
    if s * (mid - entry) < 0.6 * atr:
        return None
    if _spread_ratio(snap, atr) > 0.15:
        return None
    raw_sl = ext - s * 0.3 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    dist = abs(entry - sl)
    room = s * (mid - entry) / dist if dist > 0 else 0.0
    score = _clamp((abs(z) - 2.5) * 12.0 + 12.0, 0, 25)
    score += _clamp(abs(50.0 - float(shock["rsi14"])) * 0.5, 0, 15) + 20.0
    score += _clamp((1.0 - _range(le) / max(_range(shock), 1e-12)) * 20.0, 0, 10)
    score += _clamp(room * 8.0, 0, 20) + _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros = [f"choc de rendement à {z:.1f} écarts-types (liquidations)",
            f"RSI {float(shock['rsi14']):.0f} sur la barre de choc",
            "barre suivante sans nouvel extrême et réintégration des bandes de Bollinger",
            f"médiane de Bollinger à {room:.1f} R"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if _opposed(lt, side):
        cons.append("tendance H1 franchement opposée : rebond technique contre le tf supérieur")
        score -= 10
    if float(le["adx14"]) > 35:
        cons.append(f"ADX M15 {float(le['adx14']):.0f} : marché encore directionnel")
    body_half = float(shock["open"]) + (float(shock["close"]) - float(shock["open"])) * 0.5
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.5)), score, pros, cons,
                  "nouvelle clôture au-delà de l'extrême de la barre de choc", bt)
    cand = _set_tp_plan(cand, side, entry, [body_half, mid], float(p.get("rr", 1.5)), first_r=1.0)
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L09 — jpy_cross_trend (M15 / H1, croisés JPY, sessions ASIA & LONDON)
# --------------------------------------------------------------------------------------------------------------
@register("L09")
def strategy_l09(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L09 — Croisés JPY : reconquête d'un chiffre rond (grille 50 pips) en tendance, cible = rond suivant.

    Thèse : les paires en yen sont les plus sensibles aux niveaux ronds (options à barrière, ordres des
    exportateurs, stops de détail alignés sur les demi-figures). Un franchissement net d'un multiple de 0,50 —
    tenu pendant la barre — attire le prix vers le rond suivant. Le moteur est une grille de prix absolue, pas
    une structure ni une moyenne : il n'existe nulle part ailleurs dans le dépôt.

    Règles d'entrée : symbole parmi les racines déclarées par l'agent et contenant « JPY » ; barre clôturée
    entre 00:00 et 10:00 UTC (Tokyo + ouverture de Londres) ; pas de grille exploitable si le pas (500 × point)
    sort de [0,5 ATR ; 4 ATR] ; tendance H1 UP/DOWN → sens du trade ; niveau = multiple de 0,50 immédiatement
    franchi par la clôture ; clôture au-delà du niveau de >= 0,15 ATR ; le bas (haut) de la barre reste du bon
    côté du niveau à 0,05 ATR près (le rond a tenu) ; le niveau a été franchi au cours des 8 barres clôturées
    précédentes, dont au plus 3 ont clôturé au-delà (franchissement récent, pas un niveau acquis de longue date).
    Confirmation : ADX14 >= `adx_min` et corps de la barre >= 40 % de son amplitude.
    Filtres : spread <= 10 % de l'ATR M15 ; rond suivant à >= 0,8 ATR (sinon la cible est dans le bruit).
    Logique de SL : 0,35 ATR de l'autre côté du rond reconquis (le niveau redevenu résistance/support invalide
    l'idée), distance bornée à [0,5 ATR ; `sl_atr` ATR].
    Plan de TP : rond suivant puis le rond d'après (grille de 0,50) ; cible finale au moins `rr` R.
    Invalidation : clôture M15 au-delà du chiffre rond reconquis (côté opposé au trade).
    Score : franchissement net 20 · tenue du niveau 0-15 · fraîcheur 15 · ADX 0-15 · MTF 0-15 · corps 0-10 ·
    espace jusqu'au rond suivant 0-10.
    """
    if not _market_ok(spec, snap) or "JPY" not in _root(snap.symbol):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40 or snap.spec is None:
        return None
    hour = _bar_hour(le)
    if hour is None or not (0.0 <= hour < 10.0):
        return None
    step = _figure_step(float(snap.spec.point))
    if step is None or not (0.5 * atr <= step <= 4.0 * atr):
        return None
    side = _side_from(_trend_of(lt))
    if side is None:
        return None
    s = side.sign
    entry = float(le["close"])
    # niveau de la grille immédiatement franchi par la clôture (sous l'entrée pour un achat)
    level = math.floor(entry / step) * step if side is Side.BUY else math.ceil(entry / step) * step
    if not np.isfinite(level) or level <= 0:
        return None
    if s * (entry - level) < 0.15 * atr:
        return None
    held = (float(le["low"]) >= level - 0.05 * atr) if side is Side.BUY else (float(le["high"]) <= level + 0.05 * atr)
    if not held:
        return None
    prev8 = closed["close"].iloc[-9:-1]
    beyond = int((s * (prev8 - level) > 0).sum())
    if beyond == len(prev8) or beyond > 3:
        return None  # le niveau est acquis depuis longtemps : ce n'est plus une reconquête
    if float(le["adx14"]) < float(p.get("adx_min", 25)) or _body_ratio(le) < 0.40:
        return None
    if s * (entry - float(le["open"])) <= 0:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    next_fig = level + s * step
    if s * (next_fig - entry) < 0.8 * atr:
        return None
    raw_sl = level - s * 0.35 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    dist = abs(entry - sl)
    hold = abs(float(le["low"]) - level) if side is Side.BUY else abs(level - float(le["high"]))
    score = 20.0 + _clamp((0.5 - hold / atr) * 30.0, 0, 15) + 15.0
    score += _clamp((float(le["adx14"]) - float(p.get("adx_min", 25))) * 1.5, 0, 15)
    b, pros = _mtf_bonus(le, lt, side)
    score += b + _clamp(_body_ratio(le) * 10.0, 0, 10) + _clamp(s * (next_fig - entry) / dist * 5.0, 0, 10)
    pros += [f"chiffre rond {level:.3f} franchi de {s * (entry - level) / atr:.2f} ATR et tenu sur la barre",
             f"franchissement récent ({beyond}/8 clôtures précédentes au-delà du niveau)",
             f"tendance {_trend_of(lt)} sur {spec.timeframes.get('trend', 'H1')}, ADX {float(le['adx14']):.0f}",
             f"rond suivant ({next_fig:.3f}) à {s * (next_fig - entry) / dist:.1f} R"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if step > 2.0 * atr:
        cons.append(f"grille large ({step / atr:.1f} ATR) : la cible peut demander plusieurs séances")
    if (side is Side.BUY and float(le["rsi14"]) > 75) or (side is Side.SELL and float(le["rsi14"]) < 25):
        cons.append(f"RSI {float(le['rsi14']):.0f} extrême au franchissement")
        score -= 10
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons,
                  "clôture M15 au-delà du chiffre rond reconquis (côté opposé au trade)", bt)
    cand = _set_tp_plan(cand, side, entry, [next_fig, level + s * 2.0 * step], float(p.get("rr", 2.0)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L10 — eur_cross_range (M15 / H1, croisés EUR)
# --------------------------------------------------------------------------------------------------------------
@register("L10")
def strategy_l10(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L10 — Croisés EUR : journée déjà étirée, retour vers l'OUVERTURE DU JOUR.

    Thèse : les croisés de l'euro (EURGBP, EURCHF…) sont des paires de faible amplitude : quand la journée a déjà
    parcouru l'essentiel de son amplitude habituelle d'un seul côté, la probabilité qu'elle continue diminue et
    l'ouverture du jour agit comme aimant (rééquilibrage des flux). L'ancrage est temporel — l'ouverture du jour
    UTC, mesurée sur les barres CLÔTURÉES — et l'amplitude de référence vient des journées précédentes (D1).

    Règles d'entrée : symbole parmi les racines déclarées par l'agent ; >= 10 barres M15 clôturées dans le jour
    UTC courant ; amplitude médiane des 5 dernières journées D1 clôturées (3 au minimum, le nombre réellement
    utilisé est dit dans les arguments) > 0 ; amplitude du jour >= 0,9 × cette
    médiane ; le jour est étiré d'un côté (extension depuis l'ouverture >= 60 % de l'amplitude du jour) → on
    trade le RETOUR (achat si la journée est étirée à la baisse) ; l'extrême du jour date des 6 dernières barres
    clôturées ; la barre de signal clôture au-delà du haut (bas) de la barre précédente et dans le sens du retour.
    Confirmation : RSI14 minimal (maximal) des 6 dernières barres <= `rsi_lo` (>= `rsi_hi`) et RSI de la barre de
    signal redressé au-dessus (en dessous) de ce creux.
    Filtres : pas de contre-tendance forte (tendance H1 opposée AVEC ADX >= 30 → refus) ; spread <= 15 % de
    l'ATR M15 ; ouverture du jour à >= 1,2 × la distance de SL (sinon la cible naturelle est trop proche).
    Logique de SL : au-delà de l'extrême du jour − 0,25 ATR (un nouvel extrême nie l'épuisement), distance bornée
    à [0,5 ATR ; `sl_atr` ATR].
    Plan de TP : moitié du chemin vers l'ouverture, puis ouverture du jour ; cible finale au moins `rr` R.
    Invalidation : nouvel extrême du jour dans le sens de l'extension.
    Score : extension 0-25 · RSI épuisé 0-15 · reprise 15 · espace vers l'ouverture 0-20 · calme (ADX bas) 0-15 ·
    qualité de clôture 0-10.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    day = _day_bars(closed)
    if len(day) < 10 or not _ohlc_ok(day):
        return None
    d1 = snap.frames.get("D1")
    if d1 is None or len(d1) < 4:
        return None
    prev_days = _closed(d1).iloc[-5:]
    if len(prev_days) < 3 or not _ohlc_ok(prev_days):
        return None
    med = float((prev_days["high"] - prev_days["low"]).median())
    day_open = float(day["open"].iloc[0])
    day_hi, day_lo = float(day["high"].max()), float(day["low"].min())
    day_range = day_hi - day_lo
    if not (med > 0) or day_range <= 0 or day_range < 0.9 * med:
        return None
    down_ext, up_ext = day_open - day_lo, day_hi - day_open
    if down_ext >= up_ext:
        side, extreme = Side.BUY, day_lo
        ext_share = down_ext / day_range
    else:
        side, extreme = Side.SELL, day_hi
        ext_share = up_ext / day_range
    if ext_share < 0.60:
        return None
    s = side.sign
    entry = float(le["close"])
    last6 = closed.iloc[-6:]
    made_recently = (float(last6["low"].min()) <= extreme + 1e-12) if side is Side.BUY else \
        (float(last6["high"].max()) >= extreme - 1e-12)
    if not made_recently:
        return None
    prev = closed.iloc[-2]
    trigger = (entry > float(prev["high"])) if side is Side.BUY else (entry < float(prev["low"]))
    if not trigger or s * (entry - float(le["open"])) <= 0:
        return None
    rsi_win = last6["rsi14"]
    if rsi_win.isna().any():
        return None
    lo_r, hi_r = float(p.get("rsi_lo", 30)), float(p.get("rsi_hi", 70))
    if side is Side.BUY and not (float(rsi_win.min()) <= lo_r and float(le["rsi14"]) > float(rsi_win.min())):
        return None
    if side is Side.SELL and not (float(rsi_win.max()) >= hi_r and float(le["rsi14"]) < float(rsi_win.max())):
        return None
    if _opposed(lt, side) and float(le["adx14"]) >= 30:
        return None
    if _spread_ratio(snap, atr) > 0.15:
        return None
    raw_sl = extreme - s * 0.25 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 0.8)))
    if sl is None:
        return None
    dist = abs(entry - sl)
    room = s * (day_open - entry) / dist if dist > 0 else 0.0
    if room < 1.2:
        return None
    score = _clamp((ext_share - 0.6) * 62.5, 0, 25)
    score += _clamp(abs(50.0 - float(rsi_win.min() if side is Side.BUY else rsi_win.max())) * 0.6, 0, 15) + 15.0
    score += _clamp(room * 8.0, 0, 20) + _clamp((30.0 - float(le["adx14"])) * 1.0, 0, 15)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros = [f"journée étirée à {ext_share:.0%} d'un seul côté "
            f"(amplitude {day_range / med:.1f}x la médiane des {len(prev_days)} journées D1 clôturées)",
            f"RSI épuisé à {float(rsi_win.min() if side is Side.BUY else rsi_win.max()):.0f} puis redressé",
            "clôture au-delà de l'extrême de la barre précédente (reprise)",
            f"ouverture du jour ({day_open:.5f}) à {room:.1f} R"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if _opposed(lt, side):
        cons.append("tendance H1 opposée (ADX modéré) : retour contre le tf supérieur")
    if day_range > 1.6 * med:
        cons.append(f"journée déjà {day_range / med:.1f}x la médiane : possible journée de tendance")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.5)), score, pros, cons,
                  "nouvel extrême du jour dans le sens de l'extension", bt)
    cand = _set_tp_plan(cand, side, entry, [entry + (day_open - entry) * 0.5, day_open], float(p.get("rr", 1.5)),
                        first_r=1.0)
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L11 — majors_london_bos (M15 / H1, majeures, sessions LONDON & OVERLAP)
# --------------------------------------------------------------------------------------------------------------
@register("L11")
def strategy_l11(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L11 — Majeures : « escalier » de Londres — plus bas strictement croissants barre après barre.

    Thèse : sur les paires majeures, la séance de Londres produit des tendances propres où CHAQUE barre M15
    respecte le plus bas de la précédente. Cette régularité barre à barre — mesurable et invalidable sans
    ambiguïté — est un signal de participation institutionnelle bien plus net qu'une structure de swings
    (qui ne se confirme qu'après coup). L11 achète la continuation de l'escalier et sort dès qu'une barre le
    rompt : c'est sa différence avec les agents de structure (BOS/CHoCH) et de tendance EMA.

    Règles d'entrée : symbole du groupe `forex_majors` ; barre clôturée entre 07:00 et 17:00 UTC ; tendance H1
    UP/DOWN → sens du trade ; escalier d'au moins 4 barres clôturées consécutives (plus bas croissants pour un
    achat, plus hauts décroissants pour une vente) se terminant sur la barre de signal ; hauteur de l'escalier
    (clôture − plus bas de départ) >= 0,8 ATR ; clôture de la barre de signal au-delà de la clôture précédente.
    Confirmation : ADX14 >= 20 ; aucune barre de l'escalier d'amplitude > 2,5 ATR (pas de choc dans la série).
    Filtres : spread <= 10 % de l'ATR M15 (les majeures ne justifient pas de payer un spread large) ; escalier de
    plus de 10 barres → pénalité (série mûre).
    Logique de SL : sous (au-dessus de) le plus bas (haut) de DÉPART de l'escalier − 0,2 ATR : c'est exactement
    le prix qui romprait la série, donc la thèse ; distance bornée à [0,5 ATR ; `sl_atr` ATR].
    Plan de TP : mesure de l'escalier (haut − bas) projetée à 1,0 × puis 1,618 × depuis l'entrée ; cible finale
    au moins `rr` R.
    Invalidation : une barre M15 clôturée fait un plus bas (haut) au-delà de celui de la barre précédente.
    Score : longueur de l'escalier 0-25 · MTF 0-15 · hauteur 0-15 · régularité (mèches contraires faibles) 0-15
    · ADX 0-10 · qualité de clôture 0-10 — pénalité si la série est longue.
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 40:
        return None
    hour = _bar_hour(le)
    if hour is None or not (7.0 <= hour < 17.0):
        return None
    side = _side_from(_trend_of(lt))
    if side is None:
        return None
    s = side.sign
    k = _streak(closed, side)
    if k < 4:
        return None
    seq = closed.iloc[-(k + 1):]
    if not _ohlc_ok(seq):
        return None
    entry = float(le["close"])
    start = float(seq["low"].iloc[0]) if side is Side.BUY else float(seq["high"].iloc[0])
    height = s * (entry - start)
    if height < 0.8 * atr:
        return None
    if s * (entry - float(closed["close"].iloc[-2])) <= 0:
        return None
    if float(le["adx14"]) < 20:
        return None
    if float((seq["high"] - seq["low"]).max()) > 2.5 * atr:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    raw_sl = start - s * 0.2 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 1.0)))
    if sl is None:
        return None
    measure = float(seq["high"].max() - seq["low"].min())
    wicks = float(np.mean([_wick_against(seq.iloc[i], side) for i in range(len(seq))]))
    score = _clamp((k - 3) * 6.0, 0, 25)
    b, pros = _mtf_bonus(le, lt, side)
    score += b + _clamp((height / atr - 0.8) * 15.0, 0, 15) + _clamp((0.4 - wicks) * 50.0, 0, 15)
    score += _clamp((float(le["adx14"]) - 20.0) * 1.0, 0, 10) + _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros += [f"escalier de {k} barres M15 sans rupture (plus bas/hauts monotones)",
             f"hauteur {height / atr:.1f} ATR depuis le départ de la série",
             f"mèches contraires moyennes {wicks:.0%} (série régulière)", f"ADX {float(le['adx14']):.0f}"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if k > 10:
        cons.append(f"série déjà longue ({k} barres) : essoufflement possible")
        score -= 10
    if measure < 1.2 * atr:
        cons.append("amplitude totale de la série modeste : objectif de projection limité")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "une barre M15 clôturée fait un plus bas/haut au-delà de celui de la barre précédente", bt)
    cand = _set_tp_plan(cand, side, entry, [entry + s * measure, entry + s * 1.618 * measure],
                        float(p.get("rr", 2.5)))
    return _finalize(cand, snap)


# --------------------------------------------------------------------------------------------------------------
# L12 — minors_h4_pullback (H4 / D1, mineures)
# --------------------------------------------------------------------------------------------------------------
@register("L12")
def strategy_l12(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """L12 — Mineures : COMPRESSION de volatilité à l'intérieur d'une tendance D1, reprise sur plus haute clôture.

    Thèse : les paires mineures (croisés) alternent de longues phases de tendance D1 et des pauses où l'ATR H4
    retombe sous sa moyenne longue. C'est dans ces pauses — et seulement là — que le rapport risque/rendement est
    bon : le stop tient dans une volatilité compressée alors que l'objectif se mesure sur la jambe précédente,
    formée en volatilité normale. L'entrée se fait sur la plus haute clôture des 5 dernières barres, et le stop
    est posé sur la MOYENNE des 5 derniers plus bas (et non sur l'extrême) : une mèche isolée ne sort pas la
    position.

    Règles d'entrée : symbole du groupe `forex_minors` ; >= 60 barres H4 clôturées ; ATR14 <= 0,9 × sa moyenne
    50 barres (compression) ; tendance D1 (`_trend_of`) UP/DOWN et tendance H4 alignée → sens du trade ; RSI14
    dans [`rsi_lo`, `rsi_hi`] (bande de continuation, miroir pour une vente) ; clôture > plus haute des 5
    clôtures précédentes (< plus basse pour une vente).
    Confirmation : `mom10` du bon signe et clôture du bon côté de l'EMA50 H4.
    Filtres : spread <= 10 % de l'ATR H4 ; jambe de référence (dernier swing bas → swing haut confirmé)
    disponible et comprise entre 1,5 et 12 ATR, sinon pas d'objectif mesurable.
    Logique de SL : moyenne des 5 derniers plus bas (hauts) clôturés − 0,5 ATR, distance bornée à
    [0,6 ATR ; `sl_atr` ATR].
    Plan de TP : projection de la jambe précédente (1,0 × puis 1,5 ×) depuis l'entrée ; cible finale au moins
    `rr` R.
    Invalidation : clôture H4 au-delà de la moyenne des 5 derniers plus bas (hauts).
    Score : compression 0-25 · alignement H4/D1 20 · RSI en bande 0-10 · momentum 0-15 · jambe mesurable 0-15 ·
    qualité de clôture 0-10 — pénalité si la compression est extrême (marché endormi).
    """
    if not _market_ok(spec, snap):
        return None
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    if len(closed) < 60:
        return None
    atr_ma = float(closed["atr14"].iloc[-50:].mean())
    if not (atr_ma > 0):
        return None
    ratio = atr / atr_ma
    if ratio > 0.9:
        return None
    tr_d1 = _trend_of(lt)
    side = _side_from(tr_d1)
    if side is None or _trend_of(le) != tr_d1:
        return None
    s = side.sign
    lo_r, hi_r = _rsi_band(p, side, 35.0, 60.0)
    if not (lo_r <= float(le["rsi14"]) <= hi_r):
        return None
    entry = float(le["close"])
    prev5 = closed["close"].iloc[-6:-1]
    if side is Side.BUY and entry <= float(prev5.max()):
        return None
    if side is Side.SELL and entry >= float(prev5.min()):
        return None
    if not _valid(le, "mom10") or s * float(le["mom10"]) <= 0 or s * (entry - float(le["ema50"])) <= 0:
        return None
    if _spread_ratio(snap, atr) > 0.10:
        return None
    sh, sl_ = swing_points(closed)
    if not sh or not sl_:
        return None
    if side is Side.BUY:
        later = [x for x in sh if x[0] > sl_[-1][0]]
        leg = (float(later[-1][1]) - float(sl_[-1][1])) if later else float("nan")
    else:
        later = [x for x in sl_ if x[0] > sh[-1][0]]
        leg = (float(sh[-1][1]) - float(later[-1][1])) if later else float("nan")
    if not np.isfinite(leg) or not (1.5 * atr <= leg <= 12.0 * atr):
        return None
    base = float(closed["low"].iloc[-5:].mean()) if side is Side.BUY else float(closed["high"].iloc[-5:].mean())
    raw_sl = base - s * 0.5 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.6, float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    score = _clamp((0.9 - ratio) * 60.0 + 10.0, 0, 25) + 20.0
    score += _clamp(10.0 - abs(float(le["rsi14"]) - (lo_r + hi_r) / 2.0) * 0.8, 0, 10)
    score += _clamp(abs(float(le["mom10"])) / atr * 15.0, 0, 15) + _clamp(leg / atr * 3.0, 0, 15)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    pros = [f"compression de volatilité : ATR H4 = {ratio:.2f}x sa moyenne 50",
            f"tendance {tr_d1} alignée sur D1 et H4", f"plus haute/basse clôture des 5 dernières barres",
            f"jambe de référence de {leg / atr:.1f} ATR (objectif mesurable)",
            "SL sur la moyenne des 5 derniers extrêmes (insensible à une mèche isolée)"]
    cons: list[str] = []
    note = _sl_note(side, entry, raw_sl, sl, atr)      # honnêteté : SL déplacé par rapport à la thèse
    if note:
        cons.append(note)
    if ratio < 0.45:
        cons.append(f"compression extrême ({ratio:.2f}) : le marché peut rester endormi longtemps")
        score -= 10
    if float(le["adx14"]) < 18:
        cons.append(f"ADX H4 {float(le['adx14']):.0f} faible : la tendance D1 porte seule le trade")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.5)), score, pros, cons,
                  "clôture H4 au-delà de la moyenne des 5 derniers plus bas/hauts", bt)
    cand = _set_tp_plan(cand, side, entry, [entry + s * leg, entry + s * 1.5 * leg], float(p.get("rr", 2.5)))
    return _finalize(cand, snap)
