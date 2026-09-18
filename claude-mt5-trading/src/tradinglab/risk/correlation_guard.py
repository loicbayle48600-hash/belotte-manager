"""Correlation / Portfolio Guard : empêche la multiplication de paris équivalents.

Trois plafonds (en % de l'equity) :
- MAX_CURRENCY_FACTOR_RISK_PERCENT : exposition nette sur une devise (USD commun à EURUSD BUY + GBPUSD BUY + USDCHF SELL…) ;
- MAX_ASSET_CLASS_RISK_PERCENT     : concentration par classe d'actif ;
- MAX_CORRELATED_CLUSTER_RISK_PERCENT : positions dont la corrélation glissante des rendements > seuil (signée par direction).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..core.state import SystemState
from ..core.types import CheckResult, Side
from ..portfolio.exposure import ExposureLine, build_line, compute_exposure


@dataclass
class CorrelationLimits:
    max_correlated_cluster_risk_percent: float = 0.5
    max_currency_factor_risk_percent: float = 0.6
    max_asset_class_risk_percent: float = 0.75
    correlation_threshold: float = 0.7
    lookback_bars: int = 200

    @classmethod
    def from_config(cls, cfg: dict) -> "CorrelationLimits":
        return cls(**{k: cfg[k] for k in cls.__dataclass_fields__ if k in cfg})


class CorrelationGuard:
    def __init__(self, limits: CorrelationLimits, asset_rules: dict | None = None):
        self.limits = limits
        self.asset_rules = asset_rules or {}

    def _open_lines(self, state: SystemState) -> list[ExposureLine]:
        return [build_line(p.symbol, Side(p.side), p.initial_risk_money, asset_rules=self.asset_rules)
                for p in state.bot_positions.values()]

    def check(self, state: SystemState, symbol: str, side: Side, risk_money: float,
              correlations: Optional[pd.DataFrame] = None, spec_base: str = "", spec_quote: str = "") -> list[CheckResult]:
        eq = state.equity or 1.0
        L = self.limits
        lines = self._open_lines(state)
        new = build_line(symbol, side, risk_money, spec_base, spec_quote, self.asset_rules)
        rep = compute_exposure(lines + [new])
        out: list[CheckResult] = []

        worst_ccy, worst_val = max(rep.currency_factor.items(), key=lambda kv: abs(kv[1]), default=("", 0.0))
        pct = 100.0 * abs(worst_val) / eq
        out.append(CheckResult("currency_factor_risk", pct <= L.max_currency_factor_risk_percent + 1e-9,
                               f"{worst_ccy} net {pct:.3f}% / {L.max_currency_factor_risk_percent}%"))

        cls_val = rep.asset_class.get(new.asset_class, 0.0)
        pct_cls = 100.0 * cls_val / eq
        out.append(CheckResult("asset_class_risk", pct_cls <= L.max_asset_class_risk_percent + 1e-9,
                               f"{new.asset_class} {pct_cls:.3f}% / {L.max_asset_class_risk_percent}%"))

        # cluster corrélé : positions existantes dont la corrélation signée avec le nouveau pari dépasse le seuil
        cluster = risk_money
        members = []
        if correlations is not None and symbol in correlations.columns:
            for ln in lines:
                if ln.symbol in correlations.columns:
                    c = float(correlations.loc[symbol, ln.symbol]) if ln.symbol in correlations.index else 0.0
                    signed = c * side.sign * ln.side.sign
                    if signed >= L.correlation_threshold:
                        cluster += ln.risk_money
                        members.append(f"{ln.symbol}({c:.2f})")
        else:
            # sans matrice : cluster par devise commune dans le même sens de facteur
            for ln in lines:
                shared = ({new.base, new.quote} & {ln.base, ln.quote})
                if shared:
                    ccy = shared.pop()
                    new_sign = side.sign * (1 if ccy == new.base else -1)
                    old_sign = ln.side.sign * (1 if ccy == ln.base else -1)
                    if new_sign == old_sign:
                        cluster += ln.risk_money
                        members.append(f"{ln.symbol}[{ccy}]")
        pct_cluster = 100.0 * cluster / eq
        out.append(CheckResult("correlated_cluster_risk", pct_cluster <= L.max_correlated_cluster_risk_percent + 1e-9,
                               f"cluster {pct_cluster:.3f}% / {L.max_correlated_cluster_risk_percent}% {members}"))
        return out

    def exposure_summary(self, state: SystemState) -> dict:
        return compute_exposure(self._open_lines(state)).to_dict()
