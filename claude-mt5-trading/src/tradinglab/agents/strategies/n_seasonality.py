"""Famille N — saisonnalité (N01, N02), statut SHADOW : hypothèses à valider par le pipeline.

Décision utilisateur du 2026-09-21 (« tout ce qui peut être utile ») : une famille saisonnalité,
construite en SHADOW et validée par le pipeline champion/challenger comme n'importe quel agent.

Thème commun : un biais CALENDAIRE mesuré dans l'historique D1 du symbole lui-même. Rien n'est
importé d'une étude externe : si le biais n'existe pas dans les données du symbole, l'agent ne
propose rien. La saisonnalité n'est jamais un signal seul — elle doit être confirmée par la
structure du tf d'entrée et ne jamais aller contre la tendance D1.

- **N01 — weekday_seasonality** : rendement moyen D1 (clôture→clôture) du jour de semaine courant,
  mesuré sur >= ``min_days`` barres D1 clôturées. Le biais n'est retenu que s'il est statistiquement
  net (|t| >= ``t_min`` avec t = moyenne / (écart-type / racine(n)), n >= ``min_per_day`` occurrences)
  ; sens = signe du biais, jamais contre la tendance D1.
- **N02 — month_turn_indices** : biais de RETOURNEMENT DE MOIS des indices actions (fenêtre = 2
  derniers jours calendaires + ``first_days`` premiers jours de Bourse du mois), retenu seulement si
  l'historique D1 DU SYMBOLE confirme (rendement moyen des jours en fenêtre > 0 ET > rendement moyen
  hors fenêtre, >= ``min_window_days`` occurrences). Achat uniquement (le biais documenté est long),
  jamais quand la tendance D1 est baissière.

Différences avec les familles existantes : l'ancrage est un CALENDRIER (jour de semaine, position
dans le mois), pas une moyenne mobile (B/D), un range de session (C), un excès (E), un swing (F), un
régime d'ATR (G) ni l'empreinte d'un événement (K).

Conventions communes (voir `agents/screeners.py` et `k_news.py`) :
- décision sur la dernière barre CLÔTURÉE de chaque tf ; la barre en formation n'est JAMAIS lue
  (le « jour courant » vient de l'horodatage de la dernière barre M15 clôturée, connu à la décision) ;
- données insuffisantes ou NaN → ``None``, jamais une valeur inventée ;
- SL structurel via ``_structure_sl``, revalidé par ``validate_stop_loss`` (refus → ``None``) ;
- ``setup_score`` = somme documentée de composantes, PAS une probabilité de gain ;
- chaque candidat porte en argument CONTRE le rappel que la saisonnalité est un biais statistique.
"""
from __future__ import annotations

import calendar
from typing import Optional

import numpy as np
import pandas as pd

from ...core.types import Side, TradeCandidate
from ...risk.stop_loss import validate_stop_loss
from ..registry import AgentSpec
from ..screeners import _build, _clamp, _ctx, _mtf_bonus, _structure_sl, _trend_of, register

# seuils par défaut (les challengers bruitent les params numériques de +/-20 % : mêmes valeurs que specs)
N01_T_MIN = 2.0            # |t| minimal du biais du jour de semaine
N01_MIN_DAYS = 150         # barres D1 clôturées minimales pour mesurer quoi que ce soit
N01_MIN_PER_DAY = 25       # occurrences minimales du jour de semaine courant
N02_FIRST_DAYS = 3         # jours de Bourse en début de mois inclus dans la fenêtre
N02_LAST_CAL_DAYS = 2      # jours CALENDAIRES de fin de mois inclus (approximation documentée)
N02_MIN_WINDOW_DAYS = 15   # occurrences minimales de jours « en fenêtre » dans l'historique
MAX_SPREAD_ATR = 0.12      # même garde-fou de spread que la famille K


def _closed_d1(t: pd.DataFrame, today) -> Optional[pd.DataFrame]:
    """Barres D1 clôturées STRICTEMENT antérieures au jour courant, avec temps et clôtures exploitables."""
    d = t.iloc[:-1]
    if len(d) == 0 or "time" not in d.columns:
        return None
    ts = pd.to_datetime(d["time"], errors="coerce")
    close = pd.to_numeric(d["close"], errors="coerce")
    ok = ts.notna() & close.notna()
    d = pd.DataFrame({"time": ts[ok], "close": close[ok]})
    d = d[d["time"].dt.date < today]
    return d if len(d) else None


def _returns(d: pd.DataFrame) -> pd.DataFrame:
    r = d["close"].pct_change()
    out = pd.DataFrame({"time": d["time"], "ret": r}).dropna()
    return out


def _entry_trigger(le: pd.Series, side: Side) -> bool:
    """Confirmation minimale du tf d'entrée : clôture du bon côté de l'EMA20, corps dans le sens, RSI non extrême."""
    if side is Side.BUY:
        return bool(le["close"] > le["ema20"] and le["close"] > le["open"] and le["rsi14"] < 70)
    return bool(le["close"] < le["ema20"] and le["close"] < le["open"] and le["rsi14"] > 30)


def _spread_ok(snap, atr: float) -> bool:
    if atr <= 0 or snap.spec is None:
        return False
    return (snap.spread_points * snap.spec.point / atr) <= MAX_SPREAD_ATR


@register("N01")
def strategy_n01(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """N01 — Biais du jour de semaine, mesuré sur l'historique D1 du symbole et confirmé en M15.

    Règles d'entrée : sur >= `min_days` barres D1 clôturées antérieures au jour courant (jour de la
    dernière barre M15 clôturée), rendement moyen clôture→clôture du même jour de semaine ; retenu si
    n >= `min_per_day` occurrences et |t| >= `t_min` (t = moyenne / (écart-type / racine(n))) ; sens =
    signe du biais ; tendance D1 jamais franchement opposée ; barre M15 clôturée dans le sens (clôture
    du bon côté de l'EMA20, corps directionnel, RSI14 non extrême) ; spread <= 12 % de l'ATR M15.
    Logique de SL : structurel (`_structure_sl`, dernier swing confirmé, borné par `sl_atr`).
    Plan de TP : générique de `_build` (1,5 R / 2,5 R / `rr` final).
    Invalidation : clôture M15 au-delà du SL structurel (le swing d'appui est rendu).
    Score : 20 (biais net + confirmation M15) + 0-25 force du biais ((|t|-t_min)×8, plafonné)
    + 0-15 constance du jour (part de jours dans le sens − 50 %, ×60) + 0-15 alignement MTF.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    today = pd.to_datetime(bt).date()
    d = _closed_d1(t, today)
    if d is None or len(d) < int(p.get("min_days", N01_MIN_DAYS)):
        return None
    r = _returns(d)
    wd = today.weekday()
    if wd >= 5:                                     # jour de semaine seulement (le D1 crypto cote le week-end)
        return None
    sub = r[r["time"].dt.weekday == wd]["ret"]
    n = int(len(sub))
    if n < int(p.get("min_per_day", N01_MIN_PER_DAY)):
        return None
    m, s = float(sub.mean()), float(sub.std(ddof=1))
    if not np.isfinite(m) or not np.isfinite(s) or s <= 0:
        return None
    tstat = m / (s / np.sqrt(n))
    t_min = float(p.get("t_min", N01_T_MIN))
    if abs(tstat) < t_min:
        return None
    side = Side.BUY if m > 0 else Side.SELL
    tr = _trend_of(lt)
    if (side is Side.BUY and tr == "DOWN") or (side is Side.SELL and tr == "UP"):
        return None
    if not _entry_trigger(le, side) or not _spread_ok(snap, atr):
        return None
    entry = float(le["close"])
    sl = _structure_sl(e, side, entry, atr, float(p.get("sl_atr", 1.2)))
    day_share = float((sub > 0).mean()) if side is Side.BUY else float((sub < 0).mean())
    score = 20.0
    score += min(25.0, (abs(tstat) - t_min) * 8.0)
    score += _clamp((day_share - 0.5) * 60.0, 0, 15)
    bonus, pros_mtf = _mtf_bonus(le, lt, side)
    score += bonus
    jours = ("lundi", "mardi", "mercredi", "jeudi", "vendredi")
    pros = [f"biais {jours[wd]} mesuré : {m * 100:+.3f} %/jour sur {n} occurrences (t={tstat:+.1f})",
            f"{day_share * 100:.0f} % des {jours[wd]}s dans le sens du biais", *pros_mtf]
    cons = ["saisonnalité = biais statistique passé, pas une garantie ; à confirmer en SHADOW"]
    level = f"{sl:.5f}"
    inv = f"clôture M15 sous {level} (swing d'appui rendu)" if side is Side.BUY else \
          f"clôture M15 au-dessus de {level} (swing d'appui rendu)"
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 2.0)), score, pros, cons, inv, bt)
    if cand is None:
        return None
    chk = validate_stop_loss(cand.side, cand.entry, cand.sl, snap.spec, atr=float(cand.atr or 0.0))
    return cand if chk.ok else None


@register("N02")
def strategy_n02(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    """N02 — Retournement de mois sur indices : biais long des derniers/premiers jours du mois, confirmé en M15.

    Règles d'entrée : le jour courant (jour de la dernière barre M15 clôturée) est « en fenêtre » s'il
    est parmi les `first_days` premiers jours de BOURSE du mois (comptés sur les barres D1 clôturées du
    mois) ou dans les `last_cal_days` derniers jours CALENDAIRES du mois (approximation assumée : les
    jours fériés de fin de mois réduisent la fenêtre, ils ne l'élargissent jamais). Le biais doit
    exister dans l'historique D1 du symbole : rendement moyen des jours en fenêtre > 0, > rendement
    moyen hors fenêtre, sur >= `min_window_days` occurrences (historique >= `min_days` barres).
    ACHAT uniquement ; tendance D1 jamais baissière ; barre M15 clôturée haussière au-dessus de
    l'EMA20, RSI14 < 70 ; spread <= 12 % de l'ATR M15.
    Logique de SL : structurel (`_structure_sl`, borné par `sl_atr`).
    Plan de TP : générique de `_build` (1,5 R / 2,5 R / `rr` final).
    Invalidation : clôture M15 sous le SL structurel (le swing d'appui est rendu).
    Score : 20 (fenêtre + biais confirmé + confirmation M15) + 0-25 avantage de la fenêtre
    ((moyenne fenêtre − moyenne hors fenêtre) en unités de 0,05 %/jour, ×5, plafonné)
    + 0-15 constance (part de jours en fenêtre positifs − 50 %, ×60) + 0-15 alignement MTF.
    """
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    today = pd.to_datetime(bt).date()
    d = _closed_d1(t, today)
    if d is None or len(d) < int(p.get("min_days", N01_MIN_DAYS)):
        return None
    first_days = int(p.get("first_days", N02_FIRST_DAYS))
    last_cal = int(p.get("last_cal_days", N02_LAST_CAL_DAYS))
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    trading_day = int((d["time"].dt.date.map(lambda x: (x.year, x.month) == (today.year, today.month))).sum()) + 1
    in_window_today = trading_day <= first_days or (days_in_month - today.day) < last_cal
    if not in_window_today:
        return None
    r = _returns(d)
    dates = r["time"].dt.date
    month_len = dates.map(lambda x: calendar.monthrange(x.year, x.month)[1])
    tday = r["time"].groupby([dates.map(lambda x: (x.year, x.month))]).cumcount() + 1
    in_win = (tday <= first_days) | ((month_len - dates.map(lambda x: x.day)) < last_cal)
    r_in, r_out = r["ret"][in_win], r["ret"][~in_win]
    if len(r_in) < int(p.get("min_window_days", N02_MIN_WINDOW_DAYS)) or len(r_out) < 30:
        return None
    m_in, m_out = float(r_in.mean()), float(r_out.mean())
    if not (np.isfinite(m_in) and np.isfinite(m_out)) or m_in <= 0 or m_in <= m_out:
        return None
    side = Side.BUY
    if _trend_of(lt) == "DOWN":
        return None
    if not _entry_trigger(le, side) or not _spread_ok(snap, atr):
        return None
    entry = float(le["close"])
    sl = _structure_sl(e, side, entry, atr, float(p.get("sl_atr", 1.2)))
    share = float((r_in > 0).mean())
    score = 20.0
    score += min(25.0, (m_in - m_out) / 0.0005 * 5.0)
    score += _clamp((share - 0.5) * 60.0, 0, 15)
    bonus, pros_mtf = _mtf_bonus(le, lt, side)
    score += bonus
    pros = [f"retournement de mois (jour de Bourse {trading_day} / {today.day}e du mois) : "
            f"{m_in * 100:+.3f} %/jour en fenêtre vs {m_out * 100:+.3f} % hors fenêtre ({len(r_in)} occ.)",
            f"{share * 100:.0f} % des jours en fenêtre positifs", *pros_mtf]
    cons = ["saisonnalité = biais statistique passé, pas une garantie ; à confirmer en SHADOW"]
    inv = f"clôture M15 sous {sl:.5f} (swing d'appui rendu)"
    cand = _build(spec, snap, side, entry, sl, float(p.get("rr", 1.8)), score, pros, cons, inv, bt)
    if cand is None:
        return None
    chk = validate_stop_loss(cand.side, cand.entry, cand.sl, snap.spec, atr=float(cand.atr or 0.0))
    return cand if chk.ok else None
