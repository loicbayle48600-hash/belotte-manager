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
from datetime import datetime, timezone
from typing import Optional

from ..core.state import SystemState, _parse_ts
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
    consistency_enforced: bool = False              # appliquer en direct (sizing + fermeture au plafond)
    consistency_enforce_from_profit_percent: float = 1.0   # ... dès que le profit net >= x % du solde initial
    max_lots_by_account_size: dict = field(default_factory=dict)   # {taille: {classe: lots max}}
    hedging_allowed: bool = False
    reversal_after_loss_cooldown_minutes: float = 0.0    # 0 = contrôle désactivé
    commissions_per_lot: dict = field(default_factory=dict)
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
        if symbol and side:
            out.append(self.hedge_check(state, symbol, side))
            out.append(self.reversal_check(state, symbol, side, now))
        return out

    # ---------- pratiques interdites (page « Ce qu'on n'autorise pas », 2026-09-23) ----------
    def hedge_check(self, state: SystemState, symbol: str, side: str) -> CheckResult:
        """Hedging interdit : aucune position du bot dans l'autre sens sur le même instrument."""
        if self.profile.hedging_allowed:
            return CheckResult("prop_no_hedge", True, "hedging autorisé par le profil")
        opposes = [p.ticket for p in state.bot_positions.values() if p.symbol == symbol and p.side != side]
        return CheckResult("prop_no_hedge", not opposes,
                           "aucune position opposée" if not opposes else f"position opposée ouverte sur {symbol} : {opposes}")

    def reversal_check(self, state: SystemState, symbol: str, side: str, now: Optional[datetime] = None) -> CheckResult:
        """Pas d'inversion immédiate après une perte (politique de jeu FOXX) : après une idée PERDANTE sur ce
        symbole, l'autre sens attend `reversal_after_loss_cooldown_minutes`."""
        cd = float(self.profile.reversal_after_loss_cooldown_minutes or 0.0)
        if cd <= 0:
            return CheckResult("prop_no_reversal", True, "contrôle désactivé")
        now = now or datetime.now(timezone.utc)
        for idea in state.trade_ideas.values():
            if idea.symbol != symbol or idea.side == side or idea.open_tickets or idea.realized_pnl >= 0:
                continue
            ts = _parse_ts(idea.last_activity_at)
            if ts is None:
                continue
            age_min = (now - ts).total_seconds() / 60.0
            if age_min < cd:
                return CheckResult("prop_no_reversal", False,
                                   f"perte {idea.side} sur {symbol} il y a {age_min:.0f} min : inversion refusée avant {cd:g} min")
        return CheckResult("prop_no_reversal", True, "aucune perte récente dans l'autre sens")

    # ---------- taille de lots (FAQ FOXX « Restrictions sur la taille des lots », 2026-09-23) ----------
    #: classes d'actifs du laboratoire → colonnes de la table FOXX
    LOT_CLASS = {"forex": "forex", "metals": "commodities", "energy": "commodities", "commodities": "commodities",
                 "indices": "indices", "index": "indices", "crypto": "crypto"}

    def max_lots(self, asset_class: str) -> Optional[float]:
        """Lots maximum pour cette classe d'actifs et la taille du compte du profil ; None si la table est absente.
        Ligne = plus grande taille <= account_size ; classe inconnue = colonne la plus stricte."""
        table = self.profile.max_lots_by_account_size or {}
        if not table:
            return None
        tailles = sorted(float(k) for k in table)
        eligibles = [t for t in tailles if t <= float(self.profile.account_size or 0.0)]
        row = table.get(int(eligibles[-1] if eligibles else tailles[0]))
        if row is None:
            row = table.get(str(int(eligibles[-1] if eligibles else tailles[0]))) or table.get(eligibles[-1] if eligibles else tailles[0])
        if not isinstance(row, dict) or not row:
            return None
        col = self.LOT_CLASS.get(str(asset_class or "").lower())
        if col is not None and col in row:
            return float(row[col])
        return float(min(float(v) for v in row.values()))

    def commission_per_lot(self, asset_class: str) -> float:
        """Commission aller-retour par lot (USD) de la prop firm pour cette classe ; 0 si non renseignée."""
        table = self.profile.commissions_per_lot or {}
        if not table:
            return 0.0
        col = self.LOT_CLASS.get(str(asset_class or "").lower())
        if col is not None and col in table:
            return float(table[col])
        return float(max(float(v) for v in table.values()))   # classe inconnue : la plus chère

    @staticmethod
    def open_idea_lots(state: SystemState, symbol: str, side: str) -> float:
        """Lots déjà ouverts par le bot sur la même idée (même symbole, même sens) — volume initial, borne haute."""
        return float(sum(float(p.initial_volume) for p in state.bot_positions.values()
                         if p.symbol == symbol and p.side == side))

    # ---------- cohérence 25 % appliquée en direct (décision utilisateur 2026-09-23) ----------
    def consistency_total_profit(self, state: SystemState) -> float:
        """Profit NET réalisé des idées du CYCLE DE PAIEMENT courant (gains et pertes) : le dénominateur de la
        règle — FOXX la contrôle au paiement, sur le profit du cycle."""
        return float(sum(i.realized_pnl for i in state.cycle_trade_ideas()))

    def payout_cycle_status(self, state: SystemState, now: Optional[datetime] = None) -> dict:
        """Où en est le cycle de paiement : jours de trading faits / requis, profit du cycle, cohérence du cycle,
        conditions FOXX (solde > initial, aucune position ouverte, pas de violation) → `eligible` / `ready`."""
        from .payout import PayoutRules

        rules = PayoutRules.from_profile(self.profile)
        index = len(state.payouts)
        required = rules.trading_days_required(index)
        days = state.cycle_trading_days()
        initial = self.reference_balance(state)
        profit = float(state.balance) - float(initial)
        cap = initial * rules.max_withdrawal_percent_per_cycle / 100.0
        share = 0.0
        gains = [i.realized_pnl for i in state.cycle_trade_ideas() if i.realized_pnl > 0]
        total = self.consistency_total_profit(state)
        if total > 0 and gains:
            share = 100.0 * max(gains) / total
        cons_ok = share <= self.profile.consistency_max_share_percent
        violation = self.overall_loss_percent(state) >= self.profile.max_overall_loss_hard_percent
        raw = self.profile.raw or {}
        auto = bool(raw.get("payout_auto_cycle", False))
        blocking = []
        if len(days) < required:
            blocking.append(f"{len(days)} jour(s) de trading sur {required} requis")
        if profit <= 0:
            blocking.append("solde ≤ solde initial : aucun profit à retirer")
        if not cons_ok:
            blocking.append(f"cohérence : meilleure idée = {share:.1f} % du profit du cycle (> {self.profile.consistency_max_share_percent:g} %) — continuer à trader")
        if violation:
            blocking.append("perte totale au-delà de la limite dure")
        eligible = not blocking
        amount = max(0.0, min(profit, cap)) if eligible else 0.0
        return {"auto_cycle": auto, "payout_index": index, "trading_days_done": len(days), "trading_days_required": required,
                "days_remaining": max(0, required - len(days)), "cycle_started_at": state.payout_cycle_started_at or "origine",
                "cycle_profit": round(profit, 2), "withdrawal_cap": round(cap, 2), "withdrawable": round(amount, 2),
                "consistency_share_percent": round(share, 2), "consistency_ok": cons_ok,
                "open_positions": len(state.bot_positions), "eligible": eligible,
                "ready": eligible and not state.bot_positions, "blocking_reasons": blocking,
                "window_open": bool(state.payout_window_since), "payouts_done": index,
                "last_payout": state.payouts[-1] if state.payouts else None,
                "profit_split_percent": rules.split_percent(index),
                "demo_as_funded": bool(raw.get("payout_demo_as_funded", False))}

    def consistency_gain_cap(self, state: SystemState, symbol: str, side: str,
                             now: Optional[datetime] = None) -> Optional[float]:
        """Gain maximal que l'idée (symbol, side) peut encore réaliser sans dépasser sa part autorisée.

        None = règle non appliquée (désactivée, ou profit net du cycle sous le seuil d'application : tant que
        le compte n'a pas 1 % de profit, la première idée gagnante pèse mécaniquement 100 % et FOXX demande
        simplement de continuer à trader). Sinon : part × (profit des autres idées) / (1 − part), soit
        « autres / 3 » à 25 % — ce que l'idée peut gagner pour rester à 25 % du total qui l'inclut.
        """
        if not self.profile.consistency_enforced:
            return None
        base = self.reference_balance(state)
        total = self.consistency_total_profit(state)
        if base <= 0 or total < base * self.profile.consistency_enforce_from_profit_percent / 100.0:
            return None
        idea = state.active_trade_idea(symbol, side, now, self.profile.trade_idea_aggregation_minutes)
        autres = total - (idea.realized_pnl if idea else 0.0)
        part = self.profile.consistency_max_share_percent / 100.0
        if autres <= 0 or part >= 1.0:
            return 0.0
        return autres * part / (1.0 - part)

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
            "payout_cycle": self.payout_cycle_status(state),
            "weekend_trading_allowed": self.profile.raw.get("weekend_trading_allowed"),
            "news_window_minutes": self.profile.raw.get("news_trading_window_minutes"),
            "news_trading_allowed_on_funded": self.profile.raw.get("news_trading_allowed_on_funded"),
            "internal_daily_limit": self.internal_daily_pct, "internal_overall_limit": self.internal_overall_pct,
        }
