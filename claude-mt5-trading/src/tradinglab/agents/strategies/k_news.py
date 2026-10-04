"""Famille K — stratégies news-sensibles (K01, K02), statut SHADOW dans le registre (`famille I`).

Thème commun : le prix porte la trace d'un événement macro. Ces deux agents ne lisent AUCUN flux de news
(aucun import de `news/`, du broker, de l'exécution, de l'orchestrateur ou des modèles LLM) : ils ne
travaillent que sur l'EMPREINTE de l'événement dans les barres CLÔTURÉES. Le hub de news et le gate
(`08_news`) restent seuls juges de l'autorisation de trader autour d'un événement ; ici on mesure ce que
l'événement a déjà fait au prix.

- **K01 — post_news_stabilization_entry** : une barre de CHOC isolée (amplitude très supérieure à la normale,
  corps directionnel), puis une phase de DIGESTION contenue dans cette barre et de plus en plus étroite ;
  l'entrée n'a lieu qu'à la sortie de cette base de digestion, jamais sur le choc lui-même.
- **K02 — macro_surprise_momentum** : une REVALORISATION statistiquement anormale (z-score du déplacement sur
  `win` barres par rapport à sa propre distribution), portée par des corps et non par des mèches, tenue par le
  marché (peu de rendu depuis l'extrême) et visible sur le tf supérieur ; l'entrée suit la revalorisation tant
  qu'elle est tenue.

Les deux logiques sont disjointes (l'une exige une contraction après le choc, l'autre exige la poursuite sans
contraction) et disjointes des modules déjà écrits (B tendance, C cassure, D repli, E retournement,
F structure, G volatilité) comme des screeners génériques de repli (`AgentSpec.base_strategy` :
`mtf_trend_pullback` pour K01, `atr_expansion` pour K02) : l'ancrage est ici un ÉVÉNEMENT daté (barre de choc
ou fenêtre de revalorisation), pas une moyenne mobile, un range de session ni un régime d'ATR.

Conventions communes (voir `agents/screeners.py`) :
- décision sur la dernière barre CLÔTURÉE (`last_closed`, `frame.iloc[:-1]`) ; la dernière ligne des frames est
  la barre en formation et n'est JAMAIS lue ;
- `_ctx` fournit (frame d'entrée, frame de tendance, barre clôturée d'entrée, barre clôturée de tendance, ATR14
  du tf d'entrée, horodatage de la barre clôturée) ;
- `_build` construit le `TradeCandidate` (pénalité de spread) et refuse tout SL du mauvais côté ; le plan de TP
  générique est ensuite remplacé par le plan propre à l'agent (`_set_tp_plan`, `rr >= 1.5` garanti) ;
- le SL final est revalidé par `risk.stop_loss.validate_stop_loss` (côté, stops_level, 0,25-4 ATR H1) : un SL
  refusé donne `None`, jamais une valeur corrigée à la volée ;
- `setup_score` 0-100 = somme documentée de composantes : ce n'est PAS une probabilité de gain ;
- données insuffisantes ou indicateur NaN → `None`, jamais une valeur inventée.
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
# Une cible au-delà de 6 R n'est pas un objectif réaliste : elle est ignorée (même convention que les familles C et G).
MAX_TARGET_RR = 6.0
# Seuils par défaut de K01/K02. Le MÊME seuil sert au filtre d'entrée ET à la mesure de la composante de score
# correspondante : une valeur différente entre les deux rendrait le score incohérent avec la condition d'entrée
# (et fausserait les challengers, dont les paramètres numériques sont bruités de +/-20 % par `DegradationManager`).
K01_SHOCK_MULT = 1.8          # amplitude du choc / amplitude médiane des `ref` barres précédentes
K01_CONTRACT_MAX = 0.65       # amplitude moyenne de la base de digestion / amplitude du choc
K01_SHOCK_BODY = 0.45         # corps minimal de la barre de choc (fraction de son amplitude)
K02_BODY_SHARE = 0.55         # somme des corps / amplitude de la fenêtre de revalorisation


# --------------------------------------------------------------------------------------------------------------
# Utilitaires locaux (purs, déterministes)
# --------------------------------------------------------------------------------------------------------------
def _closed(df: pd.DataFrame) -> pd.DataFrame:
    """Barres clôturées uniquement (la dernière ligne est la barre en formation)."""
    return df.iloc[:-1]


def _range(row: pd.Series) -> float:
    return float(row["high"] - row["low"])


def _body(row: pd.Series) -> float:
    """Corps signé de la bougie (close − open)."""
    return float(row["close"] - row["open"])


def _body_ratio(row: pd.Series) -> float:
    """Corps / amplitude de la bougie (0 si amplitude nulle)."""
    rng = _range(row)
    return float(abs(_body(row)) / rng) if rng > 0 else 0.0


def _close_pos(row: pd.Series, side: Side) -> float:
    """Position de la clôture dans l'amplitude, orientée : 1 = clôture à l'extrême favorable au trade."""
    rng = _range(row)
    if rng <= 0:
        return 0.5
    pos = float((row["close"] - row["low"]) / rng)
    return pos if side is Side.BUY else 1.0 - pos


def _ohlc_ok(df: pd.DataFrame) -> bool:
    """Vrai si les colonnes OHLC de la fenêtre sont toutes exploitables (aucun NaN)."""
    cols = ("open", "high", "low", "close")
    if len(df) == 0 or not all(c in df.columns for c in cols):
        return False
    return not bool(df[list(cols)].isna().to_numpy().any())


def _spread_ratio(snap, atr: float) -> float:
    """Spread courant exprimé en fraction de l'ATR du tf d'entrée (inf si inconnu : le filtre refuse alors)."""
    if atr <= 0 or snap.spec is None:
        return float("inf")
    return float(snap.spread_points * snap.spec.point / atr)


def _rsi_band(side: Side, lo: float, hi: float) -> tuple[float, float]:
    """Bande de RSI acceptée : [lo, hi] pour un achat, bande miroir [100−hi, 100−lo] pour une vente."""
    return (lo, hi) if side is Side.BUY else (100.0 - hi, 100.0 - lo)


def _bound_sl(snap, side: Side, entry: float, sl: float, atr: float, lo: float, hi: float) -> Optional[float]:
    """Ramène la distance entrée→SL dans [max(lo·ATR, 0,3·ATR, 0,3·ATR H1) ; min(hi, 3)·ATR] sans changer de côté.

    Renvoie None si le SL brut est du mauvais côté de l'entrée ou si la borne basse dépasse la borne haute
    (configuration incohérente : on refuse plutôt que d'inventer un stop). Sans ce refus, `abs(entry - sl)`
    retournerait silencieusement un stop du bon côté à partir d'un niveau faux : ce serait une valeur inventée.
    """
    if not np.isfinite(sl) or not np.isfinite(entry) or atr <= 0:
        return None
    if side.sign * (entry - sl) <= 0:
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

    - `targets` : cibles issues de la mesure de l'agent (amplitude du choc, ampleur de la revalorisation) ;
      seules celles situées entre 0,5 R et `MAX_TARGET_RR` R dans le sens du trade sont retenues ;
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


def _opposed(lt: pd.Series, side: Side) -> bool:
    """Vrai si la tendance EMA du tf supérieur est franchement opposée au sens du trade."""
    tr = _trend_of(lt)
    return (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP")


def _shock_bar(closed: pd.DataFrame, oldest: int, newest: int, ref: int, mult: float,
               body_min: float) -> Optional[tuple[int, float, float]]:
    """Barre de CHOC DIRECTIONNELLE la plus marquée de la fenêtre [n−oldest, n−newest[ : (indice, amplitude, ratio).

    L'amplitude de chaque candidate est comparée à l'amplitude MÉDIANE des `ref` barres qui la PRÉCÈDENT (la
    référence n'est donc jamais contaminée par le choc lui-même ni par la digestion qui suit). Sont retenues les
    seules barres dont le ratio atteint `mult` ET dont le corps atteint `body_min` de leur amplitude : on garde
    celle de plus fort ratio (à ratio égal, la plus récente — ordre total, donc déterministe).

    Toutes les candidates sont examinées, pas seulement la plus ample : si la barre la plus ample de la fenêtre
    est un doji, le motif n'est pas abandonné alors qu'une vraie barre d'événement se trouve juste à côté.
    Renvoie None si l'historique est trop court, si les données sont inexploitables ou si aucune candidate ne
    satisfait les deux critères.
    """
    n = len(closed)
    if n < ref + oldest + 2 or oldest <= newest or newest < 0 or ref <= 0:
        return None
    window = closed.iloc[n - oldest:n - newest]
    if len(window) == 0 or not _ohlc_ok(window):
        return None
    rng = (window["high"] - window["low"]).astype(float).to_numpy()
    if not bool(np.isfinite(rng).all()) or rng.max() <= 0:
        return None
    hl = (closed["high"] - closed["low"]).astype(float)
    best: Optional[tuple[float, int, float]] = None
    for k in range(len(rng)):
        shock_range = float(rng[k])
        if shock_range <= 0:
            continue
        j = k + (n - oldest)                                   # indice positionnel dans `closed`
        base = hl.iloc[max(0, j - ref):j]
        if len(base) < max(20, ref // 3):
            continue                                           # référence trop courte : aucun ratio inventé
        med = float(base.median())
        if not np.isfinite(med) or med <= 0:
            continue
        ratio = shock_range / med
        if ratio < mult or _body_ratio(closed.iloc[j]) < body_min:
            continue
        if best is None or (ratio, j) > (best[0], best[1]):
            best = (ratio, j, shock_range)
    if best is None:
        return None
    return best[1], best[2], best[0]


def _displacement_z(closes: pd.Series, win: int, sample: int) -> tuple[float, float]:
    """(déplacement net sur `win` barres, z-score de ce déplacement dans sa propre distribution).

    La distribution de référence est celle des déplacements `win` barres des `sample` valeurs PRÉCÉDENTES,
    celle du déplacement courant exclue. (nan, nan) si l'échantillon est trop court ou dégénéré : aucun
    z-score n'est inventé.
    """
    s = closes.astype(float)
    d = s.diff(win)
    if len(d) < sample + win + 2:
        return float("nan"), float("nan")
    cur = float(d.iloc[-1])
    ref = d.iloc[-(sample + 1):-1].dropna()
    if len(ref) < sample // 2 or not np.isfinite(cur):
        return float("nan"), float("nan")
    sd = float(ref.std())
    mu = float(ref.mean())
    if not np.isfinite(sd) or sd <= 0 or not np.isfinite(mu):
        return cur, float("nan")
    return cur, (cur - mu) / sd


# --------------------------------------------------------------------------------------------------------------
# K01 — post_news_stabilization_entry (M15 / H1) — SHADOW
# --------------------------------------------------------------------------------------------------------------
@register("K01")
def strategy_k01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """K01 — Sortie de la base de DIGESTION qui suit une barre de choc : on trade la stabilisation, pas l'événement.

    Thèse : la barre qui absorbe un événement macro est intradable (spread écarté, deux sens en quelques
    secondes). Ce qui est exploitable, c'est la phase qui suit : si le marché DIGÈRE le choc — barres contenues
    dans l'amplitude de la barre de choc, de plus en plus étroites, clôtures qui conservent le déplacement — la
    sortie de cette base prolonge le plus souvent le sens du choc. Si au contraire le choc est effacé, aucune
    base ne se forme et l'agent ne propose rien.

    Règles d'entrée : une barre de choc située entre `newest` et `oldest` barres clôturées avant la barre de
    signal (fenêtre bornée à `max_digest` + 2 : au-delà, la base serait trop longue et le motif rejeté de toute
    façon), d'amplitude >= `shock_mult` × l'amplitude médiane des `ref` barres qui la PRÉCÈDENT, avec un corps
    >= `shock_body` de son amplitude (événement directionnel, pas un aller-retour) ; à critères satisfaits, c'est
    la barre de plus fort ratio qui est retenue, pas simplement la plus ample ; sens = sens de ce corps ;
    puis une base de digestion de 2 à `max_digest` barres clôturées dont l'amplitude TOTALE ne dépasse pas
    `base_span_max` × l'amplitude du choc et qui ne rend pas l'événement (son extrême défavorable reste en deçà
    de l'extrême défavorable de la barre de choc) ; enfin la barre de signal clôture au-delà de l'extrême
    favorable de cette base.
    Confirmation : amplitude MOYENNE des barres de la base <= `contract_max` × l'amplitude du choc (la
    volatilité est retombée) ; toutes les clôtures de la base du bon côté du milieu de la barre de choc (le
    déplacement est conservé) ; corps de la barre de signal >= 0,35 de son amplitude.
    Filtres : RSI14 de la dernière barre de la base dans [`rsi_lo` + 10 ; `rsi_hi` + 25] pour un achat (bande
    miroir pour une vente) — le biais de l'événement tient sans surchauffe ; tendance du tf supérieur non
    franchement opposée ; amplitude de la barre de signal <= 0,9 × celle du choc (on n'entre pas sur un second
    choc) ; entrée à moins de 0,5 ATR au-delà de l'extrême de la barre de choc (pas de poursuite) ; spread
    <= 12 % de l'ATR du tf d'entrée.
    Logique de SL : sous (au-dessus de) la BASE DE DIGESTION elle-même (extrême défavorable de la base et de la
    barre de signal) − 0,25 ATR : c'est le niveau dont la perte signifie que la digestion a échoué. Distance
    ramenée dans [0,5 ATR ; `sl_atr` ATR] (`sl_atr` sert de plafond).
    Plan de TP : mouvement mesuré par le choc — 1,5 R (prise partielle) et l'amplitude du choc projetée depuis
    l'extrême de la base (×0,6 puis ×1,0), triées dans le sens du trade et limitées à 3 niveaux ; cible finale
    = la plus lointaine entre `rr` × R et ces projections (`rr >= 1,5` garanti).
    Invalidation : clôture du tf d'entrée de retour à l'intérieur de la base de digestion, ou au-delà du milieu
    de la barre de choc (le marché rend l'événement).
    Score : 25 (choc directionnel + base tenue) + 0-12 ampleur du choc + 0-13 qualité de la contraction
    + 0-10 conservation du déplacement + 0-15 alignement MTF + 0-10 corps de la barre de signal + 5 entrée non
    poursuivie − 10 si la base est longue (> 5 barres, l'élan se dissipe).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    ref = int(p.get("ref", 60))
    max_digest = int(p.get("max_digest", 6))
    shock_mult = float(p.get("shock_mult", K01_SHOCK_MULT))
    contract_max = float(p.get("contract_max", K01_CONTRACT_MAX))
    # Cohérence fenêtre de recherche / longueur de base : une barre de choc située `oldest` barres avant la fin
    # laisse une base de `oldest − 2` barres. Chercher au-delà de `max_digest + 2` reviendrait à retenir des
    # positions dont la base sera systématiquement rejetée plus bas (zone morte silencieuse) ; `newest` est tenu
    # à 3 au minimum pour que la base compte au moins les 2 barres clôturées exigées.
    oldest = min(int(p.get("oldest", 9)), max_digest + 2)
    newest = max(int(p.get("newest", 3)), 3)
    if max_digest < 2 or oldest <= newest:
        return None
    if len(closed) < ref + oldest + 5 or not _ohlc_ok(closed.iloc[-(oldest + 2):]):
        return None
    shock = _shock_bar(closed, oldest, newest, ref, shock_mult, float(p.get("shock_body", K01_SHOCK_BODY)))
    if shock is None:
        return None
    j, shock_range, ratio = shock
    sb = closed.iloc[j]
    if shock_range <= 0 or _body(sb) == 0.0:
        return None
    side = Side.BUY if _body(sb) > 0 else Side.SELL
    n = len(closed)
    digest = closed.iloc[j + 1:n - 1]                       # entre le choc et la barre de signal
    if not (2 <= len(digest) <= max_digest) or not _ohlc_ok(digest):
        return None
    hi_s, lo_s = float(sb["high"]), float(sb["low"])
    span = float(digest["high"].max() - digest["low"].min())
    if span <= 0 or span > float(p.get("base_span_max", 1.0)) * shock_range:
        return None                                          # pas de base : le marché continue de s'agiter
    opp_digest = float(digest["low"].min()) if side is Side.BUY else float(digest["high"].max())
    if side.sign * (opp_digest - (lo_s if side is Side.BUY else hi_s)) < 0:
        return None                                          # la base rend l'événement : il n'y a rien à digérer
    mean_rng = float((digest["high"] - digest["low"]).astype(float).mean())
    contract = mean_rng / shock_range
    if not np.isfinite(contract) or contract > contract_max:
        return None                                          # la volatilité n'est pas retombée
    mid = 0.5 * (hi_s + lo_s)
    if not bool((side.sign * (digest["close"].astype(float) - mid) > 0).all()):
        return None                                          # le déplacement du choc n'est pas conservé
    rsi_prev = float(digest["rsi14"].iloc[-1]) if _valid(digest.iloc[-1], "rsi14") else float("nan")
    lo_b, hi_b = _rsi_band(side, float(p.get("rsi_lo", 40)) + 10.0, float(p.get("rsi_hi", 60)) + 25.0)
    if not np.isfinite(rsi_prev) or not (lo_b <= rsi_prev <= hi_b):
        return None
    base_ext = float(digest["high"].max()) if side is Side.BUY else float(digest["low"].min())
    entry = float(le["close"])
    if side.sign * (entry - base_ext) <= 0:
        return None                                          # la base n'est pas encore libérée
    if _body_ratio(le) < 0.35 or side.sign * _body(le) <= 0:
        return None
    if _range(le) > 0.9 * shock_range:
        return None                                          # second choc : ce n'est plus une reprise digérée
    shock_ext = hi_s if side is Side.BUY else lo_s
    if side.sign * (entry - shock_ext) > 0.5 * atr:
        return None                                          # poursuite : l'événement est déjà re-joué
    if _opposed(lt, side):
        return None
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return None
    base_opp = min(float(digest["low"].min()), float(le["low"])) if side is Side.BUY else \
        max(float(digest["high"].max()), float(le["high"]))
    raw_sl = base_opp - side.sign * 0.25 * atr
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.5, float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    # Les composantes mesurent l'écart au seuil RÉELLEMENT utilisé par le filtre (mêmes variables), jamais à une
    # autre valeur par défaut : sinon un setup accepté pourrait marquer 0 sur le critère qui l'a fait accepter.
    score = 25.0 + _clamp((ratio - shock_mult) * 15.0, 0, 12)
    score += _clamp((contract_max - contract) * 45.0, 0, 13)
    hold = abs(float(digest["close"].astype(float).iloc[-1]) - mid) / shock_range
    score += _clamp(hold * 25.0, 0, 10)
    pros = [f"barre de choc d'amplitude ×{ratio:.2f} par rapport à la médiane {ref} barres, corps "
            f"{_body_ratio(sb):.0%} de l'amplitude",
            f"base de digestion de {len(digest)} barre(s) : amplitude totale {span / shock_range:.0%} et "
            f"amplitude moyenne {contract:.0%} de celle du choc",
            f"clôtures de la base du bon côté du milieu du choc (RSI {rsi_prev:.0f}, biais tenu sans surchauffe)",
            "sortie de la base par une clôture, pas sur l'événement lui-même"]
    cons = ["barre de choc dans l'historique récent : élargissement du spread possible autour de l'événement"]
    b, mtf = _mtf_bonus(le, lt, side)
    score += b
    pros += mtf
    if not mtf:
        cons.append(f"tendance {spec.timeframes.get('trend', 'H1')} non alignée (neutre)")
    score += _clamp(_body_ratio(le) * 10.0, 0, 10)
    if side.sign * (entry - shock_ext) <= 0:
        score += 5
        pros.append("entrée encore à l'intérieur de l'amplitude du choc (pas de poursuite)")
    else:
        cons.append("entrée légèrement au-delà de l'extrême du choc")
    if len(digest) > 5:
        score -= 10
        cons.append(f"base de {len(digest)} barres : l'élan de l'événement se dissipe")
    if spread > 0.06:
        cons.append(f"spread {spread:.2f} ATR après événement")
    inv = (f"clôture {spec.timeframes.get('entry', 'M15')} de retour dans la base de digestion "
           f"({base_ext:.5g}) ou au-delà du milieu de la barre de choc ({mid:.5g})")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons, inv, bt)
    targets = [base_ext + side.sign * 0.6 * shock_range, base_ext + side.sign * shock_range]
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)


# --------------------------------------------------------------------------------------------------------------
# K02 — macro_surprise_momentum (M15 / H1) — SHADOW
# --------------------------------------------------------------------------------------------------------------
@register("K02")
def strategy_k02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """K02 — Revalorisation statistiquement anormale et TENUE : on suit la surprise macro tant qu'elle n'est pas rendue.

    Thèse : une surprise macro se lit comme une revalorisation — un déplacement sur quelques barres dont
    l'ampleur sort de la distribution habituelle de l'instrument (z-score), réalisé en CORPS de bougies (le
    marché re-price, il ne balaie pas des stops) et que le marché TIENT (peu de rendu depuis l'extrême). Tant
    que la revalorisation est tenue, la suite du déplacement est plus probable que son annulation ; dès qu'une
    moitié est rendue, la thèse est morte.

    Règles d'entrée : |z-score| du déplacement de clôture sur `win` barres, mesuré dans la distribution des
    `sample` déplacements précédents, >= `z_min` ; sens = signe du déplacement ; au moins `win − 1` barres de
    la fenêtre clôturent dans ce sens.
    Confirmation : somme des corps de la fenêtre >= `body_share` × son amplitude totale (revalorisation en
    corps, pas en mèches) ; rendu depuis l'extrême de la fenêtre <= `giveback_max` × le déplacement ; barre
    clôturée du tf supérieur de même sens avec un corps >= 0,4 × son ATR14 (la revalorisation se voit aussi sur
    le tf supérieur, confirmation indépendante des EMA).
    Filtres : amplitude moyenne de la fenêtre >= `atr_ratio` × l'amplitude médiane des `sample` barres
    précédentes (sans élargissement de volatilité, ce n'est pas une surprise) ; RSI14 non explosif
    (<= 92 / >= 8 : au-delà on achète le dernier tick de la réaction) ; clôture de la barre de signal dans la
    moitié favorable de son amplitude ; spread <= 12 % de l'ATR du tf d'entrée.
    Logique de SL : stop de RENDU — placé à mi-chemin de la revalorisation (origine + 0,5 × déplacement)
    diminué de 0,25 ATR de tampon : on n'accepte pas que plus de la moitié de la surprise soit rendue. Distance
    ramenée dans [0,6 ATR ; `sl_atr` ATR] (`sl_atr` sert de plafond). Aucun swing, aucune moyenne mobile
    n'intervient : le stop est calibré sur l'ÉVÉNEMENT.
    Plan de TP : seconde jambe de la revalorisation — 1,5 R (prise partielle) et le déplacement projeté depuis
    l'entrée (×0,8 puis ×1,5), triés dans le sens du trade et limités à 3 niveaux ; cible finale = la plus
    lointaine entre `rr` × R et ces projections (`rr >= 1,5` garanti).
    Invalidation : clôture du tf d'entrée au-delà du point médian de la revalorisation (plus de la moitié
    rendue), ce qui est aussi le niveau du stop avant tampon.
    Score : 25 (revalorisation anormale + suivi) + 0-15 z-score au-delà du seuil + 0-12 part des corps
    + 0-10 faiblesse du rendu + 0-15 confirmation du tf supérieur + 0-10 qualité de clôture + 0-8 élargissement
    de volatilité − 10 si RSI déjà tendu (>= 80 / <= 20).
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = _closed(e)
    win = int(p.get("win", 4))
    sample = int(p.get("sample", 200))
    if win < 2 or len(closed) < sample + win + 5:
        return None
    window = closed.iloc[-win:]
    if not _ohlc_ok(window):
        return None
    disp, z = _displacement_z(closed["close"], win, sample)
    z_min = float(p.get("z_min", 1.8))
    if not np.isfinite(disp) or not np.isfinite(z) or abs(z) < z_min or disp == 0.0:
        return None
    side = Side.BUY if disp > 0 else Side.SELL
    bodies = window["close"].astype(float) - window["open"].astype(float)
    if int((side.sign * bodies > 0).sum()) < win - 1:
        return None                                           # la revalorisation n'est pas suivie
    span = float(window["high"].max() - window["low"].min())
    if span <= 0:
        return None
    body_share = float(bodies.abs().sum()) / span
    body_share_min = float(p.get("body_share", K02_BODY_SHARE))
    if body_share < body_share_min:
        return None                                           # déplacement en mèches : balayage, pas revalorisation
    ext = float(window["high"].max()) if side is Side.BUY else float(window["low"].min())
    entry = float(le["close"])
    giveback = side.sign * (ext - entry) / abs(disp)
    giveback_max = float(p.get("giveback_max", 0.45))
    if not np.isfinite(giveback) or giveback > giveback_max:
        return None                                           # le marché a déjà rendu la surprise
    med_rng = float((closed["high"] - closed["low"]).astype(float).iloc[-(sample + win):-win].median())
    mean_rng = float((window["high"] - window["low"]).astype(float).mean())
    atr_ratio = float(p.get("atr_ratio", 1.5))
    if not np.isfinite(med_rng) or med_rng <= 0 or mean_rng < atr_ratio * med_rng:
        return None                                           # pas d'élargissement : rien d'exceptionnel
    if not _valid(lt, "atr14", "open", "close"):
        return None
    atr_t = float(lt["atr14"])
    if atr_t <= 0 or side.sign * _body(lt) < 0.4 * atr_t:
        return None                                           # le tf supérieur ne confirme pas la revalorisation
    if (side is Side.BUY and le["rsi14"] > 92) or (side is Side.SELL and le["rsi14"] < 8):
        return None
    if _close_pos(le, side) < 0.5:
        return None
    spread = _spread_ratio(snap, atr)
    if spread > 0.12:
        return None
    origin = float(closed["close"].iloc[-(win + 1)])
    midpoint = origin + 0.5 * disp
    raw_sl = midpoint - side.sign * 0.25 * atr
    if side.sign * (entry - raw_sl) <= 0:
        return None                                           # l'entrée est déjà sous le point médian : thèse morte
    sl = _bound_sl(snap, side, entry, raw_sl, atr, 0.6, float(p.get("sl_atr", 1.5)))
    if sl is None:
        return None
    # Chaque composante mesure l'écart au seuil RÉELLEMENT utilisé par le filtre (mêmes variables) : un seuil
    # codé en dur ici donnerait un score faux dès qu'un challenger bruite `body_share` ou `giveback_max`.
    score = 25.0 + _clamp((abs(z) - z_min) * 12.0, 0, 15) + _clamp((body_share - body_share_min) * 40.0, 0, 12)
    score += _clamp((giveback_max - giveback) * 25.0, 0, 10)
    score += _clamp((abs(_body(lt)) / atr_t - 0.4) * 25.0, 0, 15)
    score += _clamp(_close_pos(le, side) * 10.0, 0, 10)
    score += _clamp((mean_rng / med_rng - atr_ratio) * 10.0, 0, 8)
    pros = [f"déplacement de {win} barres à {z:+.1f} écart-type de sa distribution ({sample} observations)",
            f"revalorisation en corps ({body_share:.0%} de l'amplitude de la fenêtre)",
            f"rendu limité à {giveback:.0%} du déplacement depuis l'extrême",
            f"barre {spec.timeframes.get('trend', 'H1')} clôturée de même sens (corps {abs(_body(lt)) / atr_t:.1f} ATR)",
            f"amplitude moyenne ×{mean_rng / med_rng:.2f} par rapport à la médiane {sample} barres"]
    cons = ["momentum de surprise : un démenti ou une révision peut annuler la revalorisation en une barre",
            "entrée après le premier déplacement : une partie du mouvement est déjà faite",
            f"z-score descriptif : fenêtres de {win} barres chevauchantes et queues de distribution épaisses — "
            f"|z| >= {z_min:.1f} est bien plus fréquent que sous une loi normale, ce n'est pas une probabilité"]
    if _opposed(lt, side):
        cons.append(f"tendance EMA {spec.timeframes.get('trend', 'H1')} opposée : revalorisation à contre-tendance")
    if (side is Side.BUY and le["rsi14"] >= 80) or (side is Side.SELL and le["rsi14"] <= 20):
        score -= 10
        cons.append(f"RSI {le['rsi14']:.0f} déjà tendu : suite du déplacement plus chère")
    if spread > 0.06:
        cons.append(f"spread {spread:.2f} ATR dans une phase de revalorisation")
    inv = (f"clôture {spec.timeframes.get('entry', 'M15')} au-delà du point médian de la revalorisation "
           f"({midpoint:.5g}) : plus de la moitié de la surprise rendue")
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), _clamp(score), pros, cons, inv, bt)
    targets = [entry + side.sign * 0.8 * abs(disp), entry + side.sign * 1.5 * abs(disp)]
    return _finalize(_set_tp_plan(cand, side, entry, targets, float(p.get("rr", 2.0))), snap)
