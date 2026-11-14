"""Lecture déterministe des règles d'invalidation écrites en texte libre par les screeners.

Chaque `TradeCandidate.invalidation` est une phrase du type « clôture M15 au-delà de 1.6095 (niveau cassé) »
ou « clôture M5 de retour à l'intérieur du range asiatique (1.0810-1.0835) ». Jusqu'au 2026-09-21 seule la
variante « …EMA50… » était réellement exécutée : sur 8 290 candidats du jour, 25 formulations différentes,
une seule lue. Une règle promise mais jamais appliquée laissait la position courir jusqu'au SL plein.

Ce module n'interprète que ce qui est MESURABLE sans ambiguïté :
- un timeframe explicite (M1/M5/M15/M30/H1/H4/D1), sinon celui de l'agent (fourni par l'appelant) ;
- un ou deux niveaux de prix plausibles (dans ±15 % de l'entrée : « 0,5 ATR », « 61,8 % », « 20 barres »,
  « ADX < 25 » sont écartés). Deux niveaux = une zone (range, boîte, base) ; un niveau = un seuil.
La condition est toujours « clôture du côté défavorable » : pour un BUY, clôture SOUS le niveau (ou sous la
borne haute de la zone = retour dedans) ; pour un SELL, clôture AU-DESSUS (ou au-dessus de la borne basse).
Sans niveau exploitable : aucune règle (None) — on ne devine jamais, l'appelant garde ses repli (EMA50).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..core.types import Side

TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
_TF_RE = re.compile(r"\b(M1|M5|M15|M30|H1|H4|D1)\b")
_NUM_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)(?![\w.,])")
# unités qui disqualifient le nombre qui les précède (ce ne sont pas des prix)
_UNIT_AFTER_RE = re.compile(r"^\s*(%|atr|barres?|bars?|pips?|points?|min|h\b)", re.IGNORECASE)
_WORD_BEFORE_RE = re.compile(r"(ema|sma|adx|rsi|macd|<|>|≤|≥)\s*$", re.IGNORECASE)
PLAUSIBLE_RANGE = 0.15


@dataclass(frozen=True)
class InvalidationRule:
    timeframe: str          # "" = timeframe d'entrée de l'agent
    level: float            # seuil : borne haute de la zone pour un BUY, borne basse pour un SELL
    zone: tuple[float, float] | None = None

    def triggered(self, side: Side, close: float) -> bool:
        if close != close:  # NaN
            return False
        return close < self.level if side is Side.BUY else close > self.level

    def describe(self) -> str:
        tf = self.timeframe or "tf d'entrée"
        if self.zone:
            return f"clôture {tf} de retour dans {self.zone[0]}-{self.zone[1]}"
        return f"clôture {tf} au-delà de {self.level}"


def _levels(text: str, entry: float) -> list[float]:
    out: list[float] = []
    for m in _NUM_RE.finditer(text):
        before, after = text[: m.start()], text[m.end():]
        if _UNIT_AFTER_RE.match(after) or _WORD_BEFORE_RE.search(before):
            continue
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if entry > 0 and abs(v - entry) / entry <= PLAUSIBLE_RANGE:
            out.append(v)
    return out


def parse_invalidation(text: str, side: Side, entry: float) -> Optional[InvalidationRule]:
    """Règle mesurable extraite du texte, ou None si rien d'exploitable (jamais d'exception)."""
    if not text or entry <= 0:
        return None
    tf_m = _TF_RE.search(text)
    tf = tf_m.group(1) if tf_m else ""
    levels = _levels(text, entry)
    if not levels:
        return None
    if len(levels) >= 2:
        lo, hi = min(levels[:2]), max(levels[:2])
        level = hi if side is Side.BUY else lo
        return InvalidationRule(tf, level, (lo, hi))
    return InvalidationRule(tf, levels[0])
