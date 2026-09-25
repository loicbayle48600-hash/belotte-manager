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
        # pic minimal (% de l'equity de départ) à partir duquel le Giveback Guard s'applique : un pic de
        # quelques euros de flottant ne doit pas verrouiller la journée ; défaut = premier palier de profit
        self.giveback_min_peak_pct = float(daily_cfg.get("giveback_min_peak_percent",
                                                         self.levels[0][0] if self.levels else 0.5))
        # "lock" (historique, à remettre pour le mode prop réel) : plancher touché → entrées gelées jusqu'au lendemain.
        # "reduce_risk" : on continue à trader à risque minimal (giveback_risk_percent).
        # "off" (décision utilisateur 2026-09-22 « continue normalement » — phase DEMO de collecte d'échantillons) :
        # le franchissement du plancher est journalisé mais ne restreint rien ; les réductions progressives
        # après bonne journée (section 3) restent seules à moduler le risque.
        self.giveback_action = str(daily_cfg.get("giveback_action", "lock")).strip().lower()
        self.giveback_risk = float(daily_cfg.get("giveback_risk_percent", 0.05))

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
        # Le verrou suit le compteur dans les deux sens : une clôture gagnante remet
        # `consecutive_losses` à 0 (orchestrateur, `_on_position_closed`), la série est donc finie et
        # le verrou n'a plus d'objet. Sans cette levée il ne tombait qu'au reset 17:00 New York, et la
        # remise à zéro du compteur devenait du code mort — le 2026-09-21 le bot est resté verrouillé
        # après un gain de +2,18 R sur USTEC, sans aucun moyen de sortir du verrou avant le reset.
        # Le verrou perte-jour (1.) n'est volontairement PAS symétrique : `daily_drawdown_percent()`
        # se calcule sur l'equity courante, il repasserait donc sous le seuil au moindre rebond du
        # flottant et le coupe-circuit se réarmerait en boucle autour de la limite.
        if state.consecutive_losses >= self.max_consecutive:
            state.lock_entries("MAX_CONSECUTIVE_LOSSES")
            reasons.append(f"{state.consecutive_losses} pertes consécutives")
            allowed = False
        else:
            state.unlock_entries("MAX_CONSECUTIVE_LOSSES")

        # 3. réduction progressive après bonne journée (on continue à trader, plus petit)
        pnl_pct = state.daily_pnl_percent()
        for lvl_pct, new_risk, sc_bonus in self.levels:
            if pnl_pct >= lvl_pct:
                # un palier « après bonne journée » ne peut que RÉDUIRE le risque (2026-09-25 : palier 1 à 0,15 % au-dessus
                # d'un risque de base de 0,125 % → il l'aurait augmenté après une journée à +0,75 %)
                risk, bonus = min(new_risk, self.base_risk), sc_bonus
        if risk != self.base_risk:
            reasons.append(f"profit jour {pnl_pct:.2f}% → risque réduit à {risk}% (+{bonus} score requis)")

        # 4. Giveback Guard : ne pas rendre plus de X % du pic de profit du jour
        peak = state.daily.peak_daily_pnl
        start_eq = state.daily.starting_equity
        peak_pct = 100.0 * peak / start_eq if start_eq else 0.0
        if peak > 0 and peak_pct >= self.giveback_min_peak_pct:
            floor = peak * (1 - self.max_giveback_pct / 100.0)
            if state.daily_pnl() <= floor:
                state.daily.giveback_breached = True
            if self.giveback_action in ("reduce_risk", "off"):
                # migration : un verrou posé sous l'ancien mode ne doit pas survivre au changement de politique
                state.unlock_entries("GIVEBACK_FLOOR")
                if state.daily.giveback_breached:
                    if self.giveback_action == "reduce_risk":
                        risk = min(risk, self.giveback_risk)
                        reasons.append(f"giveback : plancher {floor:.2f} touché (pic {peak:.2f}) → risque minimal {risk}% jusqu'à la prochaine journée")
                    else:
                        reasons.append(f"giveback : plancher {floor:.2f} touché (pic {peak:.2f}) — signalé, aucune restriction (giveback_action: off)")
            elif state.daily_pnl() <= floor:
                state.lock_entries("GIVEBACK_FLOOR")
                reasons.append(f"giveback : P&L {state.daily_pnl():.2f} <= plancher {floor:.2f} (pic {peak:.2f})")
                allowed = False
            elif "GIVEBACK_FLOOR" in state.lock_reasons:
                # le plancher reste actif jusqu'au lendemain : pas de déverrouillage intrajournalier
                allowed = False
                reasons.append("giveback : verrou maintenu jusqu'à la prochaine journée")
        return DailyDecision(entries_allowed=allowed and not state.new_trades_locked, risk_percent=risk,
                             setup_score_bonus=bonus, reasons=reasons)
