"""Stratégies dédiées crypto (famille P, 2026-09-26, demande utilisateur : « trader 7 j/7, recherche et ajoute des
stratégies crypto »). Trois idées documentées, codées de façon déterministe sur la dernière barre CLÔTURÉE :

- `donchian_breakout` — suivi de tendance par cassure de canal de Donchian (plus haut / plus bas des N dernières
  barres), la forme de momentum de série temporelle la mieux documentée sur les cryptomonnaies (ensembles de canaux
  Donchian à plusieurs périodes, stops calés sur la volatilité). Deux agents : 20 et 55 barres H4.
- `rsi2_reversion` — retour à la moyenne RSI(2) de L. Connors, dans le sens de la tendance longue (EMA200) ; réservé
  aux grandes cryptos (BTC, ETH) : sur les petites, le spread absorbe le setup.
- `weekend_window` — saisonnalité du bitcoin : le week-end, la plage 15:00-21:00 UTC concentre des rendements plus
  forts et plus stables ; effet « ouverture de l'Asie » le dimanche soir. Achat seulement, filtré par la tendance H4.

Aucune de ces stratégies ne garantit un gain : ce sont des hypothèses mesurées ensuite par le journal, la page Qualité
et la validation (`research/validate_live`).
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from ..core.types import Side, TradeCandidate
from ..market_data.indicators import rsi
from .registry import AgentSpec
from .screeners import _build, _clamp, _ctx, _mtf_bonus, _trend_of, register


@register("donchian_breakout")
def donchian_breakout(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    n = int(p.get("lookback", 20))
    closed = e.iloc[:-1]
    if len(closed) < n + 3:
        return None
    canal = closed.iloc[-(n + 1):-1]                       # N barres AVANT la dernière clôturée
    haut, bas = float(canal["high"].max()), float(canal["low"].min())
    canal_prec = closed.iloc[-(n + 2):-2]                  # canal qui précédait la barre d'avant
    haut_p, bas_p = float(canal_prec["high"].max()), float(canal_prec["low"].min())
    close, prev_close = float(le["close"]), float(closed["close"].iloc[-2])
    # cassure FRAÎCHE : la barre précédente, jugée contre SON propre canal, n'avait pas encore cassé
    if close > haut and prev_close <= haut_p:
        side = Side.BUY
    elif close < bas and prev_close >= bas_p:
        side = Side.SELL
    else:
        return None
    if _trend_of(lt) == ("DOWN" if side is Side.BUY else "UP"):
        return None                                        # jamais contre la tendance du tf supérieur
    sl = close - side.sign * float(p.get("sl_atr", 1.5)) * atr
    depassement = abs(close - (haut if side is Side.BUY else bas)) / atr
    score = 60 + _clamp(depassement * 10, 0, 8)
    b, pros = _mtf_bonus(le, lt, side)
    pros.append(f"cassure fraîche du canal de Donchian {n} barres ({haut if side is Side.BUY else bas:.5g})")
    cons = ["faux départs fréquents en range : le stop large (volatilité) les absorbe"]
    return _build(spec, snap, side, close, sl, float(p.get("rr", 2.5)), score + b, pros, cons,
                  f"clôture de retour dans le canal Donchian {n}", bt)


@register("rsi2_reversion")
def rsi2_reversion(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    closed = e.iloc[:-1]
    r2 = rsi(closed["close"].astype(float), 2)
    if len(r2) == 0 or pd.isna(r2.iloc[-1]):
        return None
    v = float(r2.iloc[-1])
    close = float(le["close"])
    if v < float(p.get("rsi_lo", 10)) and close > float(le["ema200"]) and _trend_of(lt) != "DOWN":
        side = Side.BUY
    elif v > float(p.get("rsi_hi", 90)) and close < float(le["ema200"]) and _trend_of(lt) != "UP":
        side = Side.SELL
    else:
        return None
    sl = close - side.sign * float(p.get("sl_atr", 1.5)) * atr
    extreme = (float(p.get("rsi_lo", 10)) - v) if side is Side.BUY else (v - float(p.get("rsi_hi", 90)))
    score = 60 + _clamp(extreme, 0, 8)
    b, pros = _mtf_bonus(le, lt, side)
    pros.append(f"RSI(2) extrême ({v:.0f}) dans le sens de la tendance longue (EMA200)")
    cons = ["retour à la moyenne : risque de poursuite si la tendance se retourne"]
    return _build(spec, snap, side, close, sl, float(p.get("rr", 1.5)), score + b, pros, cons,
                  "clôture au-delà de l'EMA200 contre le trade", bt)


@register("weekend_window")
def weekend_window(spec: AgentSpec, snap) -> Optional[TradeCandidate]:
    c = _ctx(spec, snap)
    if not c:
        return None
    e, t, le, lt, atr, bt = c
    p = spec.params
    ts = pd.Timestamp(le["time"])
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    # barre CLÔTURÉE : l'entrée se fait à l'ouverture de la barre suivante, donc on teste l'heure de fin de barre
    heure_fin = (ts + pd.Timedelta(hours=1)).hour
    fenetres = p.get("windows", [[5, 15, 17], [6, 15, 17], [6, 23, 24]])   # [jour (lun=0), heure début, heure fin)
    if not any(ts.weekday() == int(j) and int(h0) <= heure_fin < int(h1) for j, h0, h1 in fenetres):
        return None
    if _trend_of(lt) == "DOWN" or float(le.get("mom10", 0.0) or 0.0) <= 0:
        return None                                        # achat seulement, jamais contre une tendance H4 baissière
    close = float(le["close"])
    sl = close - float(p.get("sl_atr", 1.5)) * atr
    b, pros = _mtf_bonus(le, lt, Side.BUY)
    pros.append("fenêtre de saisonnalité du week-end (bitcoin, rendements documentés 15-21 h UTC)")
    cons = ["effet statistique faible trade par trade ; liquidité du week-end réduite"]
    return _build(spec, snap, Side.BUY, close, sl, float(p.get("rr", 2.0)), 60 + b, pros, cons,
                  "clôture H1 sous l'EMA50", bt)
