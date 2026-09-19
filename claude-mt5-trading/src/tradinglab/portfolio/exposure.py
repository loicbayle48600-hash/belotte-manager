"""Exposition de portefeuille : facteurs devise, classes d'actifs, clusters corrélés."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.types import Side
from ..mt5.symbols import asset_class_of, currencies_of


@dataclass
class ExposureLine:
    symbol: str
    side: Side
    risk_money: float
    base: str
    quote: str
    asset_class: str


@dataclass
class ExposureReport:
    currency_factor: dict[str, float] = field(default_factory=dict)   # devise -> risque net signé (+ long)
    asset_class: dict[str, float] = field(default_factory=dict)       # classe -> risque total (abs)
    total_risk: float = 0.0
    lines: list[ExposureLine] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"currency_factor": self.currency_factor, "asset_class": self.asset_class, "total_risk": self.total_risk}


def build_line(symbol: str, side: Side, risk_money: float, spec_base: str = "", spec_quote: str = "",
               asset_rules: dict | None = None) -> ExposureLine:
    base, quote = currencies_of(symbol, spec_base, spec_quote)
    return ExposureLine(symbol, side, risk_money, base, quote, asset_class_of(symbol, asset_rules))


def compute_exposure(lines: list[ExposureLine]) -> ExposureReport:
    rep = ExposureReport(lines=list(lines))
    for ln in lines:
        s = ln.side.sign * ln.risk_money
        rep.currency_factor[ln.base] = rep.currency_factor.get(ln.base, 0.0) + s
        rep.currency_factor[ln.quote] = rep.currency_factor.get(ln.quote, 0.0) - s
        rep.asset_class[ln.asset_class] = rep.asset_class.get(ln.asset_class, 0.0) + ln.risk_money
        rep.total_risk += ln.risk_money
    return rep
