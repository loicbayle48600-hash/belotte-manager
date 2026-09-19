"""Prop Guard : profil prop firm configurable, règles NON supposées.

Tant que PROP_RULES_VERIFIED est faux, qu'une règle critique est UNKNOWN, que l'utilisateur n'a pas
autorisé explicitement l'exécution prop ou que l'approbation d'EA exigée par la prop firm n'est pas
obtenue, l'exécution PROP est bloquée. Le mode DEMO reste autorisé.

Les bases de calcul appliquées ici sont celles relevées chez FOXX Funded
(`docs/prop/foxx_funded_regles.md`, relevé du 2026-09-19) :

- la journée de trading commence au reset de 17:00 America/New_York, pas à minuit UTC ;
- le plancher journalier vaut `max(solde, equity) au reset − 4 % du SOLDE INITIAL` ;
- la perte totale est un drawdown **statique** mesuré sur le solde initial, jamais sur un pic d'equity ;
- les positions prises dans le même sens sur le même instrument (et celles rouvertes sous 10 minutes)
  forment une seule « idée de trade » dont la perte cumulée est plafonnée à 2 % du solde initial.

Les limites internes du laboratoire restent plus strictes ; le prop guard n'assouplit jamais rien.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.state import SystemState
from ..core.trading_day import TradingDayCalendar
from ..core.types import CheckResult, TradeMode

CRITICAL_RULES = ["ea_allowed", "news_trading_window_minutes", "trading_day_definition", "daily_loss_basis"]

#: valeurs textuelles qui signifient « règle non renseignée » et rendent le profil ambigu
UNSET_VALUES = ("UNKNOWN", "", "NONE", "NULL")


def _is_unset(value) -> bool:
    return value is None or str(value).strip().upper() in UNSET_VALUES


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
    # --- règles relevées (valeurs par défaut prudentes si le fichier est ancien) ---
    trading_day_reset_hour: int = 17
    trading_day_reset_minute: int = 0
    trading_day_timezone: str = "America/New_York"
    max_risk_per_trade_idea_percent: float = 2.0
    trade_idea_aggregation_minutes: float = 10.0
    consistency_max_share_percent: float = 25.0
    min_trading_days: int = 0
    stop_loss_mandatory: bool = True
    ea_requires_approval: bool = True
    ea_approval_obtained: bool = False
    unknown_rules: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict) -> "PropProfile":
        p = cls(**{k: cfg[k] for k in cls.__dataclass_fields__
                   if k in cfg and cfg[k] is not None and k not in ("unknown_rules", "raw")})
        p.raw = dict(cfg)
        # une règle absente, `null`/`~`, vide ou "UNKNOWN" n'est PAS renseignée : elle rend le profil ambigu
        p.unknown_rules = [r for r in CRITICAL_RULES if _is_unset(cfg.get(r))]
        return p

    @property
    def ambiguous(self) -> bool:
        return bool(self.unknown_rules)

    @property
    def ea_approval_missing(self) -> bool:
        """L'EA doit être approuvé par la prop firm avant toute exécution automatisée (démarche humaine)."""
        return bool(self.ea_requires_approval) and not bool(self.ea_approval_obtained)

    def trading_day_calendar(self) -> TradingDayCalendar:
        return TradingDayCalendar(reset_hour=self.trading_day_reset_hour,
                                  reset_minute=self.trading_day_reset_minute,
                                  tz_name=self.trading_day_timezone)


class PropGuard:
    def __init__(self, profile: PropProfile, autonomous_demo: bool, autonomous_prop_flag: bool,
                 internal_daily_pct: float, internal_overall_pct: float | None = None):
        self.profile = profile
        self.autonomous_demo = autonomous_demo
        self.autonomous_prop_flag = autonomous_prop_flag
        self.internal_daily_pct = internal_daily_pct
        self.internal_overall_pct = internal_overall_pct or (profile.max_overall_loss_hard_percent * 0.6)

    # marge de sécurité appliquée aux limites dures de la prop firm (on n'approche jamais le seuil)
    HARD_LIMIT_MARGIN = 0.75

    @property
    def blocking_reasons(self) -> list[str]:
        why: list[str] = []
        if not self.autonomous_prop_flag:
            why.append("AUTONOMOUS_TRADING_PROP=false")
        if not self.profile.prop_rules_verified:
            why.append("PROP_RULES_VERIFIED=false")
        if not self.profile.user_explicitly_authorized_prop_automation:
            why.append("USER_EXPLICITLY_AUTHORIZED_PROP_AUTOMATION=false")
        if self.profile.ambiguous:
            why.append("règles UNKNOWN: " + ",".join(self.profile.unknown_rules))
        if self.profile.ea_approval_missing:
            why.append("EA_APPROVAL_OBTAINED=false (approbation de l'EA par la prop firm non obtenue)")
        return why

    @property
    def prop_automation_allowed(self) -> bool:
        return not self.blocking_reasons

    def authorization(self, trade_mode: TradeMode) -> CheckResult:
        """Autorisation d'exécuter sur ce type de compte."""
        if trade_mode is TradeMode.DEMO:
            return CheckResult("account_authorization", self.autonomous_demo, "compte DEMO" if self.autonomous_demo else "AUTONOMOUS_TRADING_DEMO=false")
        if trade_mode in (TradeMode.REAL, TradeMode.CONTEST):
            if self.prop_automation_allowed:
                return CheckResult("account_authorization", True, "compte PROP/REAL autorisé (règles vérifiées + autorisation explicite + EA approuvé)")
            return CheckResult("account_authorization", False, "compte non-DEMO bloqué : " + "; ".join(self.blocking_reasons))
        return CheckResult("account_authorization", False, "trade_mode UNKNOWN → refus")

    # ---------- mesures exprimées dans la base de la prop firm ----------
    def reference_balance(self, state: SystemState) -> float:
        return state.prop_reference_balance(self.profile.account_size)

    def daily_loss_percent(self, state: SystemState) -> float:
        return state.prop_daily_loss_percent(self.profile.account_size)

    def overall_loss_percent(self, state: SystemState) -> float:
        return state.prop_overall_loss_percent(self.profile.account_size)

    def weekend_check(self, asset_class: str = "", now: Optional[datetime] = None) -> CheckResult:
        """Week-end : FOXX n'autorise que les cryptomonnaies.

        Le week-end est celui du marché, borné par le reset de la prop firm : de vendredi 17:00
        America/New_York à dimanche 17:00. C'est exactement l'intervalle où la journée de trading prop
        tombe un samedi ou un dimanche.
        """
        allowed = str(self.profile.raw.get("weekend_trading_allowed", "CRYPTO_ONLY") or "").strip().upper()
        if now is None or allowed in ("", "UNKNOWN", "TRUE", "ALL"):
            return CheckResult("prop_weekend_sessions", True, f"week-end: {allowed or 'non évalué'}")
        day = self.profile.trading_day_calendar().day(now)
        if day.weekday() < 5:
            return CheckResult("prop_weekend_sessions", True, f"journée prop {day.isoformat()} (semaine)")
        cls = str(asset_class or "").strip().lower()
        ok = allowed == "CRYPTO_ONLY" and cls == "crypto"
        return CheckResult("prop_weekend_sessions", ok,
                           f"journée prop {day.isoformat()} (week-end) : {allowed}, classe d'actif "
                           f"{cls or 'inconnue'}")

    def activity_status(self, state: SystemState, now: Optional[datetime] = None) -> dict:
        """Activité minimale : nombre de jours de trading effectués et délai depuis la dernière entrée.

        Informatif : la prop firm exige un minimum de jours de trading et au moins une transaction par
        semaine, mais ces règles ne se contrôlent pas à l'ouverture d'une position.
        """
        since = state.days_since_last_trade(now)
        return {"trading_days": len(state.trading_days), "min_trading_days": self.profile.min_trading_days,
                "min_trading_days_reached": len(state.trading_days) >= self.profile.min_trading_days,
                "days_since_last_trade": None if since is None else round(since, 2),
                "weekly_activity_at_risk": bool(since is not None and since >= 6.0)}

    def limits(self, state: SystemState, new_risk_money: float = 0.0, symbol: str = "", side: str = "",
               now: Optional[datetime] = None, asset_class: str = "") -> list[CheckResult]:
        """Limites internes (plus prudentes) puis hard limits prop, avec marge pour le risque à ajouter."""
        out = []
        add_risk = max(0.0, new_risk_money)
        # --- limites internes : mesurées sur l'equity, comme le reste du laboratoire ---
        dd_day = state.daily_drawdown_percent()
        dd_all = state.overall_drawdown_percent()
        add_pct_equity = 100.0 * add_risk / state.equity if state.equity else 0.0
        out.append(CheckResult("internal_daily_dd", dd_day + add_pct_equity < self.internal_daily_pct,
                               f"{dd_day:.3f}%+{add_pct_equity:.3f}% < {self.internal_daily_pct}%"))
        out.append(CheckResult("internal_overall_dd", dd_all + add_pct_equity < self.internal_overall_pct,
                               f"{dd_all:.3f}%+{add_pct_equity:.3f}% < {self.internal_overall_pct:.2f}%"))
        # --- limites dures prop : toujours exprimées en % du SOLDE INITIAL ---
        base = self.reference_balance(state)
        add_pct_base = 100.0 * add_risk / base if base else 0.0
        prop_day = self.daily_loss_percent(state)
        prop_all = self.overall_loss_percent(state)
        hard_day = self.profile.max_daily_loss_hard_percent * self.HARD_LIMIT_MARGIN
        hard_all = self.profile.max_overall_loss_hard_percent * self.HARD_LIMIT_MARGIN
        floor_day = state.prop_daily_floor(self.profile.max_daily_loss_hard_percent, self.profile.account_size)
        floor_all = state.prop_overall_floor(self.profile.max_overall_loss_hard_percent, self.profile.account_size)
        out.append(CheckResult("prop_hard_daily", prop_day + add_pct_base < hard_day,
                               f"perte jour {prop_day:.3f}%+{add_pct_base:.3f}% du solde initial "
                               f"< {hard_day:.2f}% (dur {self.profile.max_daily_loss_hard_percent}%, plancher {floor_day:.2f})"))
        out.append(CheckResult("prop_hard_overall", prop_all + add_pct_base < hard_all,
                               f"perte totale {prop_all:.3f}%+{add_pct_base:.3f}% du solde initial "
                               f"< {hard_all:.2f}% (dur {self.profile.max_overall_loss_hard_percent}%, plancher {floor_all:.2f})"))
        # --- plafond par idée de trade (positions agrégées, réouverture sous N minutes comprise) ---
        idea_cap = self.profile.max_risk_per_trade_idea_percent * self.HARD_LIMIT_MARGIN
        if symbol and side and base > 0:
            projected = state.projected_trade_idea_risk(symbol, side, add_risk, now,
                                                        self.profile.trade_idea_aggregation_minutes)
            projected_pct = 100.0 * projected / base
            out.append(CheckResult("prop_trade_idea_risk", projected_pct < idea_cap,
                                   f"risque cumulé de l'idée {symbol} {side} {projected_pct:.3f}% du solde initial "
                                   f"< {idea_cap:.2f}% (dur {self.profile.max_risk_per_trade_idea_percent}%, "
                                   f"agrégation {self.profile.trade_idea_aggregation_minutes:g} min)"))
        else:
            out.append(CheckResult("prop_trade_idea_risk", True, "idée de trade non évaluable (symbole/sens absents)"))
        out.append(self.weekend_check(asset_class, now))
        return out

    def consistency_status(self, state: SystemState) -> dict:
        """Règle de cohérence : part du profit total détenue par la meilleure idée.

        Non bloquante — chez FOXX elle est contrôlée au moment du paiement et n'élimine pas le compte ;
        le dépassement oblige simplement à continuer de trader jusqu'au retour sous le seuil.
        """
        share = state.consistency_share_percent()
        cap = self.profile.consistency_max_share_percent
        return {"share_percent": round(share, 3), "max_share_percent": cap, "within_limit": share <= cap,
                "blocking": False,
                "detail": ("aucun profit enregistré" if share == 0.0 else
                           f"meilleure idée = {share:.1f} % du profit total (seuil {cap} % au paiement)")}

    def compliance_report(self, state: SystemState) -> dict:
        return {
            "prop_firm": self.profile.prop_firm, "program": self.profile.program,
            "prop_rules_verified": self.profile.prop_rules_verified,
            "prop_automation_allowed": self.prop_automation_allowed,
            "blocking_reasons": self.blocking_reasons,
            "unknown_rules": self.profile.unknown_rules,
            "ea_requires_approval": self.profile.ea_requires_approval,
            "ea_approval_obtained": self.profile.ea_approval_obtained,
            "rules_source_url": self.profile.rules_source_url, "rules_version": self.profile.rules_version,
            "rules_verified_at": self.profile.rules_verified_at,
            "trading_day": f"reset {self.profile.trading_day_reset_hour:02d}:{self.profile.trading_day_reset_minute:02d} "
                           f"{self.profile.trading_day_timezone}",
            "trading_day_key": state.daily.day,
            "reference_balance": self.reference_balance(state),
            "daily_dd_percent": state.daily_drawdown_percent(), "overall_dd_percent": state.overall_drawdown_percent(),
            "prop_daily_loss_percent": round(self.daily_loss_percent(state), 3),
            "prop_overall_loss_percent": round(self.overall_loss_percent(state), 3),
            "prop_daily_floor": round(state.prop_daily_floor(self.profile.max_daily_loss_hard_percent,
                                                             self.profile.account_size), 2),
            "prop_overall_floor": round(state.prop_overall_floor(self.profile.max_overall_loss_hard_percent,
                                                                 self.profile.account_size), 2),
            "hard_daily_limit": self.profile.max_daily_loss_hard_percent,
            "hard_overall_limit": self.profile.max_overall_loss_hard_percent,
            "max_risk_per_trade_idea_percent": self.profile.max_risk_per_trade_idea_percent,
            "trade_idea_aggregation_minutes": self.profile.trade_idea_aggregation_minutes,
            "consistency": self.consistency_status(state),
            "activity": self.activity_status(state),
            "weekend_trading_allowed": self.profile.raw.get("weekend_trading_allowed"),
            "news_window_minutes": self.profile.raw.get("news_trading_window_minutes"),
            "news_trading_allowed_on_funded": self.profile.raw.get("news_trading_allowed_on_funded"),
            "internal_daily_limit": self.internal_daily_pct, "internal_overall_limit": self.internal_overall_pct,
        }
