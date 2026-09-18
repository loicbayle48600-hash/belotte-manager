"""Types de base partagés par tout le système (déterministes, sans dépendance LLM)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Tolérance (secondes) pour un tick daté légèrement dans le futur (dérive d'horloge normale).
# Au-delà, `Tick.age_seconds()` renvoie `inf` : données considérées non fraîches (fail-safe).
TICK_FUTURE_TOLERANCE_SEC = 5.0


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderKind(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class TradeMode(str, Enum):
    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"
    UNKNOWN = "UNKNOWN"


class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    BREAKOUT = "BREAKOUT"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    NEWS_SHOCK = "NEWS_SHOCK"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    UNCERTAIN = "UNCERTAIN"


class AgentStatus(str, Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    CANDIDATE = "CANDIDATE"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class SystemMode(str, Enum):
    SAFE_MODE = "SAFE_MODE"
    AUTO = "AUTO"
    PAUSED = "PAUSED"
    PANIC = "PANIC"


class Provenance(str, Enum):
    FACT = "FACT"
    CALCULATED = "CALCULATED"
    MODEL_INTERPRETATION = "MODEL_INTERPRETATION"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    WAIT = "WAIT"
    REJECT = "REJECT"
    NO_TRADE = "NO_TRADE"


class Session(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEWYORK = "NEWYORK"
    OVERLAP_LDN_NY = "OVERLAP_LDN_NY"
    OFF = "OFF"


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float          # valeur d'un tick pour 1 lot, en devise du compte
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int    # distance minimale SL/TP imposée par le broker (en points)
    trade_allowed: bool
    currency_base: str
    currency_profit: str
    currency_margin: str
    asset_class: str = "forex"
    spread_points: int = 0
    root: str = ""             # nom sans suffixe broker (EURUSD pour EURUSD.m)

    @property
    def min_stop_distance(self) -> float:
        return self.stops_level_points * self.point


@dataclass
class Tick:
    symbol: str
    time: datetime
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0
    provenance: Provenance = Provenance.FACT

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def spread_points(self, spec: SymbolSpec) -> int:
        return int(round((self.ask - self.bid) / spec.point))

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """Âge du tick en secondes par rapport à `now`.

        Garde-fou déterministe : un tick daté dans le futur au-delà de `TICK_FUTURE_TOLERANCE_SEC`
        (horloge PC / heure serveur incohérentes, décalage non calibré) est traité comme infiniment
        périmé (`inf`) au lieu de renvoyer un âge négatif qui passerait tout contrôle `age <= max`.
        """
        now = now or utcnow()
        age = (now - self.time).total_seconds()
        if age < -TICK_FUTURE_TOLERANCE_SEC:
            return float("inf")
        return age


@dataclass
class AccountInfo:
    login: int
    server: str
    trade_mode: TradeMode
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str
    leverage: int
    name: str = ""
    hedging: bool = True
    connected: bool = True
    provenance: Provenance = Provenance.FACT

    def public_dict(self) -> dict:
        d = asdict(self)
        d["trade_mode"] = self.trade_mode.value
        d["provenance"] = self.provenance.value
        return d


@dataclass
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    time_open: datetime
    magic: int
    comment: str = ""
    price_current: float = 0.0
    swap: float = 0.0

    @property
    def has_sl(self) -> bool:
        return self.sl is not None and self.sl > 0


@dataclass
class PendingOrder:
    ticket: int
    symbol: str
    side: Side
    kind: OrderKind
    volume: float
    price: float
    sl: float
    tp: float
    magic: int
    comment: str = ""


@dataclass
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    side: Side
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    time: datetime
    magic: int
    entry: str      # IN | OUT | INOUT
    comment: str = ""


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    volume: float
    sl: float
    tp: float = 0.0
    kind: OrderKind = OrderKind.MARKET
    price: float = 0.0
    magic: int = 0
    comment: str = ""
    deviation_points: int = 20
    agent_id: str = ""
    candidate_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["kind"] = self.kind.value
        return d


@dataclass
class OrderResult:
    ok: bool
    retcode: int
    ticket: int = 0
    price: float = 0.0
    volume: float = 0.0
    comment: str = ""
    request: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class GateResult:
    approved: bool
    checks: list[CheckResult] = field(default_factory=list)
    reason: str = ""
    adjusted_volume: float = 0.0

    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "adjusted_volume": self.adjusted_volume,
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass
class TradeCandidate:
    symbol: str
    side: Side
    entry: float
    sl: float
    tp_plan: list[float]
    timeframes: list[str]
    regime: Regime
    agent_id: str
    agent_version: str = "1"
    setup_score: float = 0.0        # score interne 0-100 — PAS une probabilité de gain
    data_quality: str = "OK"
    spread_points: int = 0
    news_state: str = "UNKNOWN"
    macro_alignment: str = "UNKNOWN"
    rr: float = 0.0
    historical_stats: dict = field(default_factory=dict)
    sample_size: int = 0
    correlation_impact: str = "UNKNOWN"
    risk_percent: float = 0.0
    invalidation: str = ""
    arguments_for: list[str] = field(default_factory=list)
    arguments_against: list[str] = field(default_factory=list)
    atr: float = 0.0
    session: str = "OFF"
    bar_time: str = ""              # clôture de barre ayant généré le setup (idempotence)
    provenance: Provenance = Provenance.CALCULATED
    id: str = field(default_factory=lambda: new_id("cand"))
    created_at: datetime = field(default_factory=utcnow)
    verdict: Optional[Verdict] = None
    review: dict = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return f"{self.symbol}|{self.side.value}|{self.agent_id}|{self.bar_time}"

    @property
    def sl_distance(self) -> float:
        return abs(self.entry - self.sl)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["regime"] = self.regime.value
        d["provenance"] = self.provenance.value
        d["created_at"] = self.created_at.isoformat()
        d["verdict"] = self.verdict.value if self.verdict else None
        return d


@dataclass
class NewsItem:
    timestamp: datetime
    source: str
    title: str
    assets: list[str]
    importance: str            # LOW | MEDIUM | HIGH
    direction: str = "UNKNOWN" # BULLISH | BEARISH | NEUTRAL | UNKNOWN
    confidence: float = 0.0
    surprise: Optional[float] = None
    impact_minutes: int = 60
    quality: str = "structured_api"
    provenance: Provenance = Provenance.FACT
    conflict_with: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("news"))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["provenance"] = self.provenance.value
        return d


@dataclass
class CalendarEvent:
    timestamp: datetime
    source: str
    title: str
    currency: str
    importance: str
    actual: Optional[float] = None
    forecast: Optional[float] = None
    previous: Optional[float] = None
    provenance: Provenance = Provenance.FACT
    id: str = field(default_factory=lambda: new_id("cal"))

    @property
    def surprise(self) -> Optional[float]:
        if self.actual is None or self.forecast is None:
            return None
        return self.actual - self.forecast

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["provenance"] = self.provenance.value
        return d


def enum_value(x: Any) -> Any:
    return x.value if isinstance(x, Enum) else x
