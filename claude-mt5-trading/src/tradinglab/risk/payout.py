"""Simulation de paiement (retrait) selon les règles relevées de la prop firm.

**Lecture seule** : ce module n'écrit aucun état, n'envoie aucun ordre et ne
modifie aucune limite. Il répond à trois questions :

1. suis-je éligible à un paiement, et sinon pourquoi ;
2. quel montant maximal puis-je demander ;
3. où se situent les planchers de drawdown **après** le retrait.

Inconnu assumé — la source relevée le 2026-09-19
(`docs/prop/foxx_funded_regles.md`) précise que le drawdown est **statique**,
mais **ne dit pas** si le plancher est recalé après un paiement. Les deux
hypothèses sont donc calculées :

- ``static``  : le plancher reste ancré sur le solde initial d'origine ;
- ``rebased`` : le plancher est recalculé sur le solde après retrait.

Aucune des deux n'est « la prudente » dans l'absolu : après un retrait bénéficiaire
le recalage **remonte** le plancher et réduit le coussin, alors qu'en drawdown il
l'abaisse. Le rapport désigne donc la pire des deux pour les chiffres du moment
(``worst_case``) et la retient comme hypothèse de travail (``floor_basis_assumed``),
en portant la mention ``UNKNOWN`` tant que la prop firm n'a pas confirmé la règle.
Conformément au principe « jamais de donnée inventée », aucune n'est présentée
comme la règle réelle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: part du trader par rang de paiement, si la configuration ne dit rien
DEFAULT_SPLIT_SEQUENCE = (70, 80, 90)


@dataclass
class PayoutRules:
    """Règles de paiement lues dans ``config/prop_firms.yaml``."""

    first_after_trading_days: int = 14
    interval_trading_days: int = 7
    request_window_hours: int = 24
    max_withdrawal_percent_per_cycle: float = 15.0
    profit_split_sequence: tuple[int, ...] = DEFAULT_SPLIT_SEQUENCE
    floor_rebased_after_withdrawal: str = "UNKNOWN"
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_profile(cls, profile) -> "PayoutRules":
        cfg = dict(getattr(profile, "raw", {}) or {})
        seq = cfg.get("payout_profit_split_sequence") or list(DEFAULT_SPLIT_SEQUENCE)
        try:
            seq = tuple(int(x) for x in seq if x is not None)
        except (TypeError, ValueError):
            seq = DEFAULT_SPLIT_SEQUENCE
        return cls(
            first_after_trading_days=int(cfg.get("payout_first_after_trading_days", 14) or 14),
            interval_trading_days=int(cfg.get("payout_interval_trading_days", 7) or 7),
            request_window_hours=int(cfg.get("payout_request_window_hours", 24) or 24),
            max_withdrawal_percent_per_cycle=float(cfg.get("max_withdrawal_percent_per_cycle", 15.0) or 15.0),
            profit_split_sequence=seq or DEFAULT_SPLIT_SEQUENCE,
            floor_rebased_after_withdrawal=str(cfg.get("payout_floor_rebased_after_withdrawal", "UNKNOWN")),
            raw=cfg,
        )

    def split_percent(self, payout_index: int) -> int:
        """Part du trader pour le paiement de rang ``payout_index`` (0 = premier)."""
        if not self.profit_split_sequence:
            return 0
        i = max(0, int(payout_index))
        return int(self.profit_split_sequence[min(i, len(self.profit_split_sequence) - 1)])

    def trading_days_required(self, payout_index: int) -> int:
        return self.first_after_trading_days if int(payout_index) <= 0 else self.interval_trading_days


def _floors(initial_basis: float, profile) -> tuple[float, float]:
    """Planchers jour et total pour une base de calcul donnée (jamais négatifs)."""
    daily = initial_basis * (1.0 - float(profile.max_daily_loss_hard_percent) / 100.0)
    overall = initial_basis * (1.0 - float(profile.max_overall_loss_hard_percent) / 100.0)
    return max(daily, 0.0), max(overall, 0.0)


def simulate_payout(state, profile, amount: Optional[float] = None, payout_index: int = 0) -> dict:
    """Simule une demande de paiement. Ne modifie ni ``state`` ni ``profile``.

    ``amount`` : montant demandé ; ``None`` simule le maximum autorisé.
    ``payout_index`` : rang du paiement (0 = premier), pour le partage des profits
    et le nombre de jours de trading exigé.
    """
    rules = PayoutRules.from_profile(profile)
    initial = float(state.prop_reference_balance(getattr(profile, "account_size", 0.0)))
    balance = float(getattr(state, "balance", 0.0) or 0.0)
    equity = float(getattr(state, "equity", 0.0) or 0.0)
    profit = balance - initial
    days = len(getattr(state, "trading_days", []) or [])
    days_required = rules.trading_days_required(payout_index)

    cap = initial * rules.max_withdrawal_percent_per_cycle / 100.0
    max_withdrawable = max(min(profit, cap), 0.0)

    blocking: list[str] = []
    if balance <= initial:
        blocking.append(f"solde {balance:.2f} <= solde initial {initial:.2f} (aucun profit à retirer)")
    if days < days_required:
        blocking.append(f"{days} jour(s) de trading sur {days_required} requis")
    if float(state.prop_overall_loss_percent(getattr(profile, "account_size", 0.0))) >= float(profile.max_overall_loss_hard_percent):
        blocking.append("perte totale au-delà de la limite dure : compte en violation")
    if getattr(profile, "ambiguous", False):
        blocking.append(f"règles critiques non renseignées : {', '.join(profile.unknown_rules)}")

    requested = max_withdrawable if amount is None else max(float(amount), 0.0)
    over_cap = requested > max_withdrawable + 1e-9
    if over_cap:
        blocking.append(f"montant demandé {requested:.2f} > maximum retirable {max_withdrawable:.2f}")

    split = rules.split_percent(payout_index)
    after: dict[str, dict] = {}
    for hypothesis in ("static", "rebased"):
        new_balance = balance - requested
        basis = initial if hypothesis == "static" else new_balance
        daily_floor, overall_floor = _floors(basis, profile)
        after[hypothesis] = {
            "balance": round(new_balance, 2),
            "daily_floor": round(daily_floor, 2),
            "overall_floor": round(overall_floor, 2),
            "buffer_to_overall_floor": round(new_balance - overall_floor, 2),
            "buffer_percent": round((new_balance - overall_floor) / basis * 100.0, 3) if basis > 0 else 0.0,
            "violates_overall_floor": new_balance < overall_floor,
        }

    # la pire hypothèse est celle qui laisse le moins de marge : elle sert de référence
    assumed = min(after, key=lambda h: after[h]["buffer_to_overall_floor"])
    return {
        "eligible": not blocking,
        "blocking_reasons": blocking,
        "payout_index": int(payout_index),
        "trading_days": days,
        "trading_days_required": days_required,
        "initial_balance": round(initial, 2),
        "balance": round(balance, 2),
        "equity": round(equity, 2),
        "profit": round(profit, 2),
        "withdrawal_cap": round(cap, 2),
        "cap_percent_of_initial": rules.max_withdrawal_percent_per_cycle,
        "max_withdrawable": round(max_withdrawable, 2),
        "requested": round(requested, 2),
        "profit_split_percent": split,
        "trader_share": round(requested * split / 100.0, 2),
        "firm_share": round(requested * (100 - split) / 100.0, 2),
        "request_window_hours": rules.request_window_hours,
        "after": after,
        "floor_basis_assumed": assumed,
        "worst_case": assumed,
        "floor_rebase_rule": rules.floor_rebased_after_withdrawal,
        "floor_rebase_note": ("la source ne précise pas si le plancher est recalé après un paiement ; "
                              "les deux hypothèses sont calculées et la moins favorable est retenue"),
        "kyc_verified": "UNKNOWN",
        "kyc_note": "vérification KYC exigée par la prop firm : démarche humaine, non vérifiable ici",
        "provenance": "CALCULATED",
    }
