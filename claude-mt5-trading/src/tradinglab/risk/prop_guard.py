"""Prop Guard : profil prop firm configurable, règles NON supposées.

Tant que PROP_RULES_VERIFIED est faux ou qu'une règle critique est UNKNOWN,
l'exécution PROP est bloquée. Le mode DEMO reste autorisé.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.state import SystemState
from ..core.types import CheckResult, TradeMode

CRITICAL_RULES = ["ea_allowed", "news_trading_window_minutes", "trading_day_definition", "daily_loss_basis"]


@dataclass
class PropProfile:
    prop_firm: str = "NONE"
    program: str = ""
    account_size: float = 0.0
    profit_target_percent: float = 0.0
    max_daily_loss_hard_percent: float = 4.0
    max_overall_loss_hard_percent: float = 8.0
    prop_rules_verified: bool = False
    user_explicitly_authorized_prop_automation: bool = False
    rules_source_url: str | None = None
    rules_version: str | None = None
    rules_verified_at: str | None = None
    unknown_rules: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict) -> "PropProfile":
        p = cls(**{k: cfg[k] for k in cls.__dataclass_fields__ if k in cfg and k not in ("unknown_rules", "raw")})
        p.raw = dict(cfg)
        p.unknown_rules = [r for r in CRITICAL_RULES if str(cfg.get(r, "UNKNOWN")).upper() == "UNKNOWN"]
        return p

    @property
    def ambiguous(self) -> bool:
        return bool(self.unknown_rules)


class PropGuard:
    def __init__(self, profile: PropProfile, autonomous_demo: bool, autonomous_prop_flag: bool,
                 internal_daily_pct: float, internal_overall_pct: float | None = None):
        self.profile = profile
        self.autonomous_demo = autonomous_demo
        self.autonomous_prop_flag = autonomous_prop_flag
        self.internal_daily_pct = internal_daily_pct
        self.internal_overall_pct = internal_overall_pct or (profile.max_overall_loss_hard_percent * 0.6)

    @property
    def prop_automation_allowed(self) -> bool:
        return (self.autonomous_prop_flag and self.profile.prop_rules_verified
                and self.profile.user_explicitly_authorized_prop_automation and not self.profile.ambiguous)

    def authorization(self, trade_mode: TradeMode) -> CheckResult:
        """Autorisation d'exécuter sur ce type de compte."""
        if trade_mode is TradeMode.DEMO:
            return CheckResult("account_authorization", self.autonomous_demo, "compte DEMO" if self.autonomous_demo else "AUTONOMOUS_TRADING_DEMO=false")
        if trade_mode in (TradeMode.REAL, TradeMode.CONTEST):
            if self.prop_automation_allowed:
                return CheckResult("account_authorization", True, "compte PROP/REAL autorisé (règles vérifiées + autorisation explicite)")
            why = []
            if not self.autonomous_prop_flag:
                why.append("AUTONOMOUS_TRADING_PROP=false")
            if not self.profile.prop_rules_verified:
                why.append("PROP_RULES_VERIFIED=false")
            if not self.profile.user_explicitly_authorized_prop_automation:
                why.append("USER_EXPLICITLY_AUTHORIZED_PROP_AUTOMATION=false")
            if self.profile.ambiguous:
                why.append("règles UNKNOWN: " + ",".join(self.profile.unknown_rules))
            return CheckResult("account_authorization", False, "compte non-DEMO bloqué : " + "; ".join(why))
        return CheckResult("account_authorization", False, "trade_mode UNKNOWN → refus")

    def limits(self, state: SystemState, new_risk_money: float = 0.0) -> list[CheckResult]:
        """Limites internes (plus prudentes) puis hard limits prop, avec marge de sécurité pour le risque à ajouter."""
        out = []
        dd_day = state.daily_drawdown_percent()
        dd_all = state.overall_drawdown_percent()
        add_pct = 100.0 * new_risk_money / state.equity if state.equity else 0.0
        out.append(CheckResult("internal_daily_dd", dd_day + add_pct < self.internal_daily_pct,
                               f"{dd_day:.3f}%+{add_pct:.3f}% < {self.internal_daily_pct}%"))
        out.append(CheckResult("internal_overall_dd", dd_all + add_pct < self.internal_overall_pct,
                               f"{dd_all:.3f}%+{add_pct:.3f}% < {self.internal_overall_pct:.2f}%"))
        hard_day = self.profile.max_daily_loss_hard_percent
        hard_all = self.profile.max_overall_loss_hard_percent
        out.append(CheckResult("prop_hard_daily", dd_day + add_pct < hard_day * 0.75, f"{dd_day:.3f}% vs hard {hard_day}% (marge 25%)"))
        out.append(CheckResult("prop_hard_overall", dd_all + add_pct < hard_all * 0.75, f"{dd_all:.3f}% vs hard {hard_all}% (marge 25%)"))
        return out

    def compliance_report(self, state: SystemState) -> dict:
        return {
            "prop_firm": self.profile.prop_firm, "program": self.profile.program,
            "prop_rules_verified": self.profile.prop_rules_verified,
            "prop_automation_allowed": self.prop_automation_allowed,
            "unknown_rules": self.profile.unknown_rules,
            "rules_source_url": self.profile.rules_source_url, "rules_version": self.profile.rules_version,
            "rules_verified_at": self.profile.rules_verified_at,
            "daily_dd_percent": state.daily_drawdown_percent(), "overall_dd_percent": state.overall_drawdown_percent(),
            "hard_daily_limit": self.profile.max_daily_loss_hard_percent,
            "hard_overall_limit": self.profile.max_overall_loss_hard_percent,
            "internal_daily_limit": self.internal_daily_pct, "internal_overall_limit": self.internal_overall_pct,
        }
