"""Model Router : choix automatique du modèle Claude par rôle, budget et quotas.

- Les identifiants de modèles ne sont jamais supposés stables : vérification via
  l'API Anthropic (models.list) quand une clé est présente ; sinon le routeur
  fonctionne en mode déterministe (TIER_D) et le signale.
- Budget quotidien (MODEL_DAILY_BUDGET_USD) + quotas horaires par tier.
- Quand le budget est atteint : l'activité LLM non essentielle est réduite ;
  Risk Guard / Watchdog ne dépendent jamais du routeur.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.state import ModelBudget, SystemState
from ..core.types import utcnow

TIER_ORDER = ["TIER_A", "TIER_B", "TIER_C", "TIER_D"]


@dataclass
class RouteDecision:
    tier: str
    model: Optional[str]           # None => Python / déterministe
    reason: str
    estimated_cost_usd: float = 0.0
    provenance: str = "ROUTER"

    @property
    def use_llm(self) -> bool:
        return self.model is not None and self.model != "python"


@dataclass
class ModelRouter:
    cfg: dict
    available_models: list[str] = field(default_factory=list)
    verified_at: Optional[datetime] = None
    verification_error: str = ""
    role_to_tier: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for tier, tcfg in self.cfg.get("tiers", {}).items():
            for role in tcfg.get("roles", []):
                self.role_to_tier[role] = tier

    # ---------- disponibilité ----------
    def refresh_availability(self, client=None) -> list[str]:
        """Interroge l'API (client anthropic) si possible. Sans clé : liste vide, mode déterministe."""
        if not self.cfg.get("verify_official_availability", True):
            self.available_models = [m for t in self.cfg.get("tiers", {}).values() for m in t.get("candidates", []) if m != "python"]
            self.verified_at = utcnow()
            return self.available_models
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                self.verification_error = "ANTHROPIC_API_KEY absent : mode déterministe (aucun appel LLM)"
                self.available_models = []
                return []
            try:
                import anthropic  # noqa: WPS433
                client = anthropic.Anthropic()
            except Exception as e:  # noqa: BLE001
                self.verification_error = f"SDK anthropic indisponible: {e}"
                self.available_models = []
                return []
        try:
            ids: list[str] = []
            page = client.models.list(limit=100)
            for m in getattr(page, "data", []) or []:
                ids.append(getattr(m, "id", str(m)))
            self.available_models = ids
            self.verified_at = utcnow()
            self.verification_error = ""
        except Exception as e:  # noqa: BLE001
            self.verification_error = f"models.list a échoué: {type(e).__name__}"
            self.available_models = []
        return self.available_models

    def needs_refresh(self) -> bool:
        if self.verified_at is None:
            return True
        return (utcnow() - self.verified_at).total_seconds() > float(self.cfg.get("refresh_interval_sec", 3600))

    # ---------- budget ----------
    def _budget(self, state: SystemState) -> ModelBudget:
        b = state.model_budget
        now = utcnow()
        day, hour = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H")
        if b.day != day:
            b.day, b.spent_usd, b.calls_by_tier_day = day, 0.0, {}
        if b.hour != hour:
            b.hour, b.calls_by_tier_hour = hour, {}
        return b

    def budget_exhausted(self, state: SystemState) -> bool:
        b = self._budget(state)
        return b.spent_usd >= float(self.cfg.get("daily_budget_usd", 0.0))

    def _hourly_cap(self, tier: str) -> int:
        return int({"TIER_A": self.cfg.get("max_fable_calls_per_hour", 6), "TIER_B": self.cfg.get("max_opus_calls_per_hour", 30),
                    "TIER_C": self.cfg.get("max_worker_calls_per_hour", 200)}.get(tier, 10**9))

    def record_call(self, state: SystemState, tier: str, cost_usd: float) -> None:
        b = self._budget(state)
        b.spent_usd += cost_usd
        b.calls_by_tier_hour[tier] = b.calls_by_tier_hour.get(tier, 0) + 1
        b.calls_by_tier_day[tier] = b.calls_by_tier_day.get(tier, 0) + 1

    # ---------- routage ----------
    def _first_available(self, tier: str) -> Optional[str]:
        for m in self.cfg.get("tiers", {}).get(tier, {}).get("candidates", []):
            if m == "python":
                return "python"
            if m in self.available_models:
                return m
        return None

    def route(self, role: str, state: SystemState, financial_importance: str = "normal", deterministic: bool = False) -> RouteDecision:
        """Choisit tier/modèle pour un rôle. financial_importance ∈ {low, normal, high}."""
        if deterministic or not self.cfg.get("auto_route", True):
            return RouteDecision("TIER_D", None, "tâche déterministe → Python")
        tier = self.role_to_tier.get(role, "TIER_C")
        if tier == "TIER_D":
            return RouteDecision("TIER_D", None, "rôle déterministe")
        if not self.available_models:
            return RouteDecision("TIER_D", None, "aucun modèle vérifié disponible → déterministe (" + (self.verification_error or "liste vide") + ")")
        b = self._budget(state)
        if b.spent_usd >= float(self.cfg.get("daily_budget_usd", 0.0)) and financial_importance != "high":
            return RouteDecision("TIER_D", None, f"budget quotidien atteint ({b.spent_usd:.2f} USD) → activité LLM non essentielle réduite")
        # ne jamais utiliser un tier plus coûteux si un moins cher suffit : on descend si quota atteint ou modèle absent
        start = TIER_ORDER.index(tier)
        for t in TIER_ORDER[start:]:
            if t == "TIER_D":
                break
            model = self._first_available(t)
            if model is None:
                continue
            if b.calls_by_tier_hour.get(t, 0) >= self._hourly_cap(t):
                continue
            cost = float(self.cfg.get("tiers", {}).get(t, {}).get("est_cost_per_call_usd", 0.0))
            return RouteDecision(t, model, f"rôle {role} → {t} ({model})" + (" (rétrogradé)" if t != tier else ""), cost)
        return RouteDecision("TIER_D", None, "quotas/modèles épuisés → déterministe")

    def status(self, state: SystemState) -> dict:
        b = self._budget(state)
        return {"available_models": self.available_models, "verified_at": self.verified_at.isoformat() if self.verified_at else None,
                "verification_error": self.verification_error, "spent_usd": b.spent_usd,
                "daily_budget_usd": self.cfg.get("daily_budget_usd"), "calls_hour": b.calls_by_tier_hour, "calls_day": b.calls_by_tier_day}
