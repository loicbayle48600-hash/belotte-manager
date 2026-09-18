"""Risk Manager déterministe : calcul du volume, limites de risque, compteurs.

Aucun agent LLM ne choisit le lot : le volume est dérivé de equity, risque %, entry,
SL, spécifications du symbole ; arrondi vers le BAS (jamais au-dessus du risque).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..core.state import SystemState
from ..core.types import CheckResult, Side, SymbolSpec


@dataclass
class SizingResult:
    ok: bool
    volume: float = 0.0
    risk_money: float = 0.0
    risk_percent_effective: float = 0.0
    loss_per_lot: float = 0.0
    reason: str = ""


@dataclass
class RiskLimits:
    risk_per_trade_percent: float = 0.25
    max_risk_per_trade_percent: float = 0.35
    max_daily_loss_internal_percent: float = 1.0
    max_total_open_risk_percent: float = 1.0
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_consecutive_losses: int = 3
    allow_martingale: bool = False
    allow_grid: bool = False
    allow_averaging_down: bool = False
    allow_remove_sl: bool = False
    allow_risk_increase_after_entry: bool = False

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskLimits":
        return cls(**{k: cfg[k] for k in cls.__dataclass_fields__ if k in cfg})


def round_volume_down(volume: float, spec: SymbolSpec) -> float:
    steps = math.floor(volume / spec.volume_step + 1e-9)
    v = steps * spec.volume_step
    return round(max(0.0, min(v, spec.volume_max)), 8)


def loss_per_lot(entry: float, sl: float, spec: SymbolSpec) -> float:
    """Perte (devise du compte) pour 1 lot si le SL est touché."""
    ticks = abs(entry - sl) / spec.tick_size
    return ticks * spec.tick_value


def compute_volume(equity: float, risk_percent: float, entry: float, sl: float, spec: SymbolSpec,
                   max_risk_percent: float) -> SizingResult:
    if equity <= 0:
        return SizingResult(False, reason="equity nulle")
    if entry <= 0 or sl <= 0 or entry == sl:
        return SizingResult(False, reason="entry/SL invalides")
    if spec.tick_value <= 0 or spec.tick_size <= 0:
        return SizingResult(False, reason="spécifications symbole incomplètes (tick_value/tick_size)")
    risk_money = equity * risk_percent / 100.0
    lpl = loss_per_lot(entry, sl, spec)
    if lpl <= 0:
        return SizingResult(False, reason="perte par lot nulle")
    raw = risk_money / lpl
    vol = round_volume_down(raw, spec)
    if vol < spec.volume_min:
        # le volume minimum broker implique-t-il un risque acceptable ?
        min_risk = spec.volume_min * lpl
        min_pct = 100.0 * min_risk / equity
        if min_pct > max_risk_percent:
            return SizingResult(False, reason=f"volume minimum {spec.volume_min} = {min_pct:.3f}% > max {max_risk_percent}% → REFUS",
                                loss_per_lot=lpl)
        vol = spec.volume_min
    eff_money = vol * lpl
    eff_pct = 100.0 * eff_money / equity
    if eff_pct > max_risk_percent + 1e-9:
        return SizingResult(False, reason=f"risque effectif {eff_pct:.3f}% > max {max_risk_percent}%", loss_per_lot=lpl)
    return SizingResult(True, volume=vol, risk_money=eff_money, risk_percent_effective=eff_pct, loss_per_lot=lpl,
                        reason="ok")


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    # ---------- risque par trade (ajusté par la garde journalière) ----------
    def base_risk_percent(self) -> float:
        return self.limits.risk_per_trade_percent

    def size(self, equity: float, entry: float, sl: float, spec: SymbolSpec, risk_percent: float | None = None) -> SizingResult:
        rp = min(risk_percent if risk_percent is not None else self.limits.risk_per_trade_percent,
                 self.limits.max_risk_per_trade_percent)
        return compute_volume(equity, rp, entry, sl, spec, self.limits.max_risk_per_trade_percent)

    # ---------- limites de portefeuille ----------
    def check_limits(self, state: SystemState, symbol: str, side: Side, new_risk_money: float,
                     open_positions_symbol: int, open_positions_total: int) -> list[CheckResult]:
        L = self.limits
        out: list[CheckResult] = []
        out.append(CheckResult("max_open_positions", open_positions_total < L.max_open_positions,
                               f"{open_positions_total}/{L.max_open_positions}"))
        out.append(CheckResult("max_positions_per_symbol", open_positions_symbol < L.max_positions_per_symbol,
                               f"{symbol}: {open_positions_symbol}/{L.max_positions_per_symbol}"))
        total_pct = state.open_risk_percent() + (100.0 * new_risk_money / state.equity if state.equity else 0.0)
        out.append(CheckResult("max_total_open_risk", total_pct <= L.max_total_open_risk_percent + 1e-9,
                               f"{total_pct:.3f}% / {L.max_total_open_risk_percent}%"))
        out.append(CheckResult("daily_loss_internal", state.daily_drawdown_percent() < L.max_daily_loss_internal_percent,
                               f"DD jour {state.daily_drawdown_percent():.3f}% / {L.max_daily_loss_internal_percent}%"))
        out.append(CheckResult("max_consecutive_losses", state.consecutive_losses < L.max_consecutive_losses,
                               f"{state.consecutive_losses}/{L.max_consecutive_losses}"))
        # anti martingale / grid / averaging : refus d'une 2e position même sens sur le symbole (couvert par per_symbol=1)
        same_side = [p for p in state.bot_positions.values() if p.symbol == symbol and p.side == side.value]
        out.append(CheckResult("no_averaging_or_grid", L.allow_grid or L.allow_averaging_down or not same_side,
                               "position même sens déjà ouverte" if same_side else "ok"))
        return out
