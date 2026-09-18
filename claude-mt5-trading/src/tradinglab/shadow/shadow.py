"""Shadow trading : les agents SHADOW/CANDIDATE tradent virtuellement (entry/sl/tp/timestamp) sans aucun ordre réel."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.types import AgentStatus, Side, TradeCandidate, utcnow
from ..learning.store import LearningStore, TradeRecord
from ..market_data.feed import MarketSnapshot


@dataclass
class ShadowPosition:
    id: str
    agent_id: str
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    opened_at: str
    regime: str
    session: str
    setup_score: float
    max_r: float = 0.0
    min_r: float = 0.0
    bar_time: str = ""


class ShadowTrader:
    def __init__(self, store: LearningStore, state_file: Path, risk_money: float = 100.0, max_open: int = 50):
        self.store = store
        self.file = Path(state_file)
        self.risk_money = risk_money
        self.max_open = max_open
        self.positions: dict[str, ShadowPosition] = {}
        self.executed: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.file.exists():
            try:
                d = json.loads(self.file.read_text(encoding="utf-8"))
                self.positions = {k: ShadowPosition(**v) for k, v in d.get("positions", {}).items()}
                self.executed = set(d.get("executed", [])[-2000:])
            except (json.JSONDecodeError, TypeError):
                pass

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"positions": {k: asdict(v) for k, v in self.positions.items()},
                                         "executed": sorted(self.executed)[-2000:]}, ensure_ascii=False, indent=1), encoding="utf-8")

    def open_from_candidates(self, cands: list[TradeCandidate], now: Optional[datetime] = None) -> list[ShadowPosition]:
        now = now or utcnow()
        out = []
        for c in cands:
            if len(self.positions) >= self.max_open or c.idempotency_key in self.executed:
                continue
            if any(p.symbol == c.symbol and p.agent_id == c.agent_id for p in self.positions.values()):
                continue
            p = ShadowPosition(id=f"sh_{c.id}", agent_id=c.agent_id, symbol=c.symbol, side=c.side.value, entry=c.entry, sl=c.sl,
                               tp=c.tp_plan[-1] if c.tp_plan else c.entry + c.side.sign * 2.5 * c.sl_distance,
                               opened_at=now.isoformat(), regime=c.regime.value, session=c.session, setup_score=c.setup_score, bar_time=c.bar_time)
            self.positions[p.id] = p
            self.executed.add(c.idempotency_key)
            out.append(p)
        if out:
            self._save()
        return out

    def update(self, snapshots: dict[str, MarketSnapshot], now: Optional[datetime] = None, max_hours: float = 72.0) -> list[TradeRecord]:
        """Vérifie SL/TP sur les barres M5 récentes (high/low), SL prioritaire. Enregistre les trades fermés en mode 'shadow'."""
        now = now or utcnow()
        closed: list[TradeRecord] = []
        for pid, p in list(self.positions.items()):
            snap = snapshots.get(p.symbol)
            if snap is None:
                continue
            df = snap.frames.get("M5")
            if df is None:
                df = snap.frames.get("M15")
            if df is None or len(df) < 3:
                continue
            opened = datetime.fromisoformat(p.opened_at)
            bars = df[df["time"] > opened].iloc[:-1]
            side = Side(p.side)
            dist = abs(p.entry - p.sl)
            exit_px, reason = None, ""
            for b in bars.itertuples():
                hi, lo = float(b.high), float(b.low)
                r_hi = side.sign * (hi - p.entry) / dist
                r_lo = side.sign * (lo - p.entry) / dist
                p.max_r = max(p.max_r, r_hi, r_lo)
                p.min_r = min(p.min_r, r_hi, r_lo)
                if (side is Side.BUY and lo <= p.sl) or (side is Side.SELL and hi >= p.sl):
                    exit_px, reason = p.sl, "sl"
                    break
                if (side is Side.BUY and hi >= p.tp) or (side is Side.SELL and lo <= p.tp):
                    exit_px, reason = p.tp, "tp"
                    break
            if exit_px is None and (now - opened).total_seconds() > max_hours * 3600:
                exit_px, reason = float(df["close"].iloc[-2]), "timeout"
            if exit_px is None:
                continue
            r = side.sign * (exit_px - p.entry) / dist
            rec = TradeRecord(ticket=0, agent_id=p.agent_id, symbol=p.symbol, side=p.side, entry=p.entry, sl=p.sl, risk_money=self.risk_money,
                              risk_percent=0.0, result_r=round(r, 4), pnl=round(r * self.risk_money, 2), opened_at=p.opened_at,
                              closed_at=now.isoformat(), mode="shadow", regime=p.regime, session=p.session, tp=[p.tp],
                              features={"setup_score": p.setup_score}, mae_r=round(-min(0.0, p.min_r), 3), mfe_r=round(max(0.0, p.max_r), 3),
                              exit_reason=reason, candidate_id=p.id)
            self.store.record_trade(rec)
            closed.append(rec)
            del self.positions[pid]
        if closed:
            self._save()
        return closed

    def summary(self) -> dict:
        return {"open": len(self.positions), "by_agent": {a: sum(1 for p in self.positions.values() if p.agent_id == a) for a in {p.agent_id for p in self.positions.values()}}}
