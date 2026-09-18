"""Garde journalière : perte max, réduction de risque après bonne journée, Giveback Guard."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.state import SystemState


@dataclass
class DailyDecision:
    entries_allowed: bool
    risk_percent: float
    setup_score_bonus: float
    reasons: list[str]


class DailyGuard:
    def __init__(self, risk_cfg: dict, daily_cfg: dict):
        self.base_risk = float(risk_cfg.get("risk_per_trade_percent", 0.25))
        self.max_daily_loss = float(risk_cfg.get("max_daily_loss_internal_percent", 1.0))
        self.max_consecutive = int(risk_cfg.get("max_consecutive_losses", 3))
        self.levels = []
        for i in (1, 2, 3):
            pct = daily_cfg.get(f"level_{i}_percent")
            if pct is not None:
                self.levels.append((float(pct), float(daily_cfg.get(f"level_{i}_new_risk_percent", self.base_risk)),
                                    float(daily_cfg.get(f"level_{i}_setup_score_bonus", 0))))
        self.levels.sort()
        self.max_giveback_pct = float(daily_cfg.get("max_giveback_percent", 25))

    def evaluate(self, state: SystemState) -> DailyDecision:
        """Met à jour les verrous de l'état et renvoie le risque autorisé pour une nouvelle entrée."""
        reasons: list[str] = []
        allowed = True
        risk = self.base_risk
        bonus = 0.0

        # 1. perte journalière interne
        if state.daily_drawdown_percent() >= self.max_daily_loss:
            state.lock_entries("DAILY_LOSS_LIMIT")
            reasons.append(f"perte journalière {state.daily_drawdown_percent():.2f}% >= {self.max_daily_loss}%")
            allowed = False

        # 2. pertes consécutives
        if state.consecutive_losses >= self.max_consecutive:
            state.lock_entries("MAX_CONSECUTIVE_LOSSES")
            reasons.append(f"{state.consecutive_losses} pertes consécutives")
            allowed = False

        # 3. réduction progressive après bonne journée (on continue à trader, plus petit)
        pnl_pct = state.daily_pnl_percent()
        for lvl_pct, new_risk, sc_bonus in self.levels:
            if pnl_pct >= lvl_pct:
                risk, bonus = new_risk, sc_bonus
        if risk != self.base_risk:
            reasons.append(f"profit jour {pnl_pct:.2f}% → risque réduit à {risk}% (+{bonus} score requis)")

        # 4. Giveback Guard : ne pas rendre plus de X % du pic de profit du jour
        peak = state.daily.peak_daily_pnl
        if peak > 0:
            floor = peak * (1 - self.max_giveback_pct / 100.0)
            if state.daily_pnl() <= floor:
                state.lock_entries("GIVEBACK_FLOOR")
                reasons.append(f"giveback : P&L {state.daily_pnl():.2f} <= plancher {floor:.2f} (pic {peak:.2f})")
                allowed = False
            elif "GIVEBACK_FLOOR" in state.lock_reasons:
                # le plancher reste actif jusqu'au lendemain : pas de déverrouillage intrajournalier
                allowed = False
                reasons.append("giveback : verrou maintenu jusqu'à la prochaine journée")
        return DailyDecision(entries_allowed=allowed and not state.new_trades_locked, risk_percent=risk,
                             setup_score_bonus=bonus, reasons=reasons)
