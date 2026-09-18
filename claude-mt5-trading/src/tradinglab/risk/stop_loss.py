"""Validation déterministe du Stop Loss. Aucune position sans SL valide."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..core.types import Side, SymbolSpec


@dataclass
class SLCheck:
    ok: bool
    reason: str = ""


def validate_stop_loss(side: Side, entry: float, sl: Optional[float], spec: SymbolSpec,
                       atr: float = 0.0, min_sl_atr_ratio: float = 0.25, max_sl_atr_ratio: float = 4.0,
                       reference_price: Optional[float] = None) -> SLCheck:
    """Refuse : SL None / 0 / absent, mauvais côté, trop proche (stops_level ou < ratio ATR), trop loin, non arrondi."""
    if sl is None:
        return SLCheck(False, "SL absent (None)")
    try:
        sl = float(sl)
    except (TypeError, ValueError):
        return SLCheck(False, "SL non numérique")
    ref = reference_price if reference_price else entry
    # NaN/inf rendent toutes les comparaisons fausses : refus explicite avant tout test de distance
    if not (math.isfinite(sl) and math.isfinite(entry) and math.isfinite(ref)):
        return SLCheck(False, "SL/entrée/prix de référence non finis (NaN/inf)")
    if sl <= 0:
        return SLCheck(False, "SL = 0 ou négatif")
    if side is Side.BUY and sl >= ref:
        return SLCheck(False, f"SL {sl} du mauvais côté pour un BUY (ref {ref})")
    if side is Side.SELL and sl <= ref:
        return SLCheck(False, f"SL {sl} du mauvais côté pour un SELL (ref {ref})")
    dist = abs(ref - sl)
    min_broker = spec.min_stop_distance
    if dist < min_broker:
        return SLCheck(False, f"SL trop proche : {dist:.{spec.digits}f} < stops_level {min_broker:.{spec.digits}f}")
    if atr and atr > 0:
        if dist < atr * min_sl_atr_ratio:
            return SLCheck(False, f"SL trop proche : {dist:.{spec.digits}f} < {min_sl_atr_ratio} x ATR ({atr:.{spec.digits}f})")
        if dist > atr * max_sl_atr_ratio:
            return SLCheck(False, f"SL trop loin : {dist:.{spec.digits}f} > {max_sl_atr_ratio} x ATR")
    return SLCheck(True, "SL valide")


def normalize_price(price: float, spec: SymbolSpec) -> float:
    return round(round(price / spec.tick_size) * spec.tick_size, spec.digits)


def is_tighter_or_equal(side: Side, new_sl: float, old_sl: float) -> bool:
    """Vrai si new_sl ne recule pas (jamais élargir un stop)."""
    if not old_sl or old_sl <= 0:
        return True
    return new_sl >= old_sl if side is Side.BUY else new_sl <= old_sl
