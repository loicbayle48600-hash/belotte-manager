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
    # Volume rabote par `volume_max` du broker : la position risque MOINS que la cible.
    # Chez IC Markets, 16 cryptos sur 18 sont concernees (XLM plafonne a 0,6 % de la taille
    # visee, XRP a 4,5 %). L'ecretage etait silencieux : `risk_percent_effective` tombait a
    # 0,001 % sans que rien ne le signale.
    volume_capped: bool = False
    volume_wanted: float = 0.0        # volume avant ecretage (0 si aucun)


@dataclass
class RiskLimits:
    risk_per_trade_percent: float = 0.25
    max_risk_per_trade_percent: float = 0.35
    max_daily_loss_internal_percent: float = 1.0
    max_total_open_risk_percent: float = 1.0
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_consecutive_losses: int = 3
    #: plancher de risque *utile*, en fraction de `risk_per_trade_percent`. Sous ce seuil, le plafond de
    #: volume du broker a tellement raboté la position qu'elle ne peut plus rien changer au compte : le
    #: 2026-09-21 XRPUSD a été pris deux fois à 11,52 $ et 12,10 $ de risque sur 500 000 $ (0,0023 %), en
    #: occupant un emplacement et le quota d'une position par symbole. Exprimé en ratio et non en valeur
    #: absolue pour suivre automatiquement `risk_per_trade_percent` (cf. test_concentration_suit_le_risque).
    min_effective_risk_ratio: float = 0.2
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
    # NaN/inf ne sont détectés par aucune comparaison : refus explicite (jamais d'exception dans le sizing)
    if not all(math.isfinite(x) for x in (equity, entry, sl)) or entry <= 0 or sl <= 0 or entry == sl:
        return SizingResult(False, reason="entry/SL invalides (NaN/inf/0)")
    if spec.tick_value <= 0 or spec.tick_size <= 0 or spec.volume_step <= 0 or spec.volume_min <= 0:
        return SizingResult(False, reason="spécifications symbole incomplètes (tick_value/tick_size/volume_step/volume_min)")
    risk_money = equity * risk_percent / 100.0
    lpl = loss_per_lot(entry, sl, spec)
    if lpl <= 0:
        return SizingResult(False, reason="perte par lot nulle")
    raw = risk_money / lpl
    vol = round_volume_down(raw, spec)
    # Écrêtage = le volume voulu, DÉJÀ arrondi au pas, dépasse le plafond du broker.
    # Comparer `raw` brut ferait passer le simple arrondi au pas pour un écrêtage.
    sans_plafond = math.floor(raw / spec.volume_step + 1e-9) * spec.volume_step
    capped = sans_plafond > spec.volume_max + 1e-9
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
    if capped:
        part = 100.0 * eff_pct / risk_percent if risk_percent > 0 else 0.0
        return SizingResult(True, volume=vol, risk_money=eff_money, risk_percent_effective=eff_pct, loss_per_lot=lpl,
                            volume_capped=True, volume_wanted=raw,
                            reason=f"volume rabote par le broker : {raw:.2f} voulu, {spec.volume_max} autorise "
                                   f"-> risque {eff_pct:.4f}% au lieu de {risk_percent:.3f}% ({part:.1f}% de la cible)")
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
        # Budget jour : le verrou perte-jour ne regarde que le passé, le risque ouvert total ne regarde
        # que les positions. Entre les deux, une journée à -0,7 % avec 0,7 % encore en jeu pouvait finir
        # à -1,4 % pour un plafond de 1 % (constaté le 2026-09-21). On refuse l'entrée dont le pire cas
        # (toutes les positions au SL courant + la nouvelle) franchirait le plafond interne.
        worst = state.worst_case_daily_drawdown_percent(new_risk_money)
        out.append(CheckResult("daily_budget_incl_open_risk", worst < L.max_daily_loss_internal_percent + 1e-9,
                               f"pire cas jour {worst:.3f}% (solde - risque ouvert restant - nouveau) / {L.max_daily_loss_internal_percent}%"))
        out.append(CheckResult("max_consecutive_losses", state.consecutive_losses < L.max_consecutive_losses,
                               f"{state.consecutive_losses}/{L.max_consecutive_losses}"))
        # anti martingale / grid / averaging : refus d'une 2e position même sens sur le symbole (couvert par per_symbol=1)
        same_side = [p for p in state.bot_positions.values() if p.symbol == symbol and p.side == side.value]
        out.append(CheckResult("no_averaging_or_grid", L.allow_grid or L.allow_averaging_down or not same_side,
                               "position même sens déjà ouverte" if same_side else "ok"))
        return out
