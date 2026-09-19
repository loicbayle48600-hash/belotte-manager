"""Shadow trading : les agents SHADOW/CANDIDATE tradent virtuellement (entry/sl/tp/timestamp) sans aucun ordre réel."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.types import AgentStatus, Side, TradeCandidate, utcnow
from ..learning.store import LearningStore, TradeRecord
from ..market_data.feed import MarketSnapshot

MAX_EXECUTED_KEYS = 2000


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
        # dict ordonné par ancienneté (insertion) : la troncature garde les clés les plus RÉCENTES, tous symboles confondus
        self.executed: dict[str, None] = {}
        self._load()

    def _load(self) -> None:
        if self.file.exists():
            try:
                d = json.loads(self.file.read_text(encoding="utf-8"))
                self.positions = {k: ShadowPosition(**v) for k, v in d.get("positions", {}).items()}
                self.executed = dict.fromkeys(list(d.get("executed", []))[-MAX_EXECUTED_KEYS:])
            except (json.JSONDecodeError, TypeError):
                pass

    def _save(self) -> None:
        """Écriture atomique (tempfile + os.replace) : un crash pendant l'écriture ne rend jamais l'état illisible."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.executed = dict.fromkeys(list(self.executed)[-MAX_EXECUTED_KEYS:])
        payload = json.dumps({"positions": {k: asdict(v) for k, v in self.positions.items()},
                              "executed": list(self.executed)}, ensure_ascii=False, indent=1)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.file)

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
            self.executed[c.idempotency_key] = None
            out.append(p)
        if out:
            self._save()
        return out

    def update(self, snapshots: dict[str, MarketSnapshot], now: Optional[datetime] = None, max_hours: float = 72.0) -> list[TradeRecord]:
        """Vérifie SL/TP sur les barres M5 récentes (high/low), SL prioritaire. Enregistre les trades fermés en mode 'shadow'.

        Une position dont le symbole n'a plus de snapshot ni de barres est abandonnée après ``max_hours`` (événement
        ``shadow_timeout_no_data``, aucun résultat inventé) pour ne pas occuper un slot indéfiniment.
        """
        now = now or utcnow()
        closed: list[TradeRecord] = []
        dirty = False
        for pid, p in list(self.positions.items()):
            opened = datetime.fromisoformat(p.opened_at)
            timed_out = (now - opened).total_seconds() > max_hours * 3600
            snap = snapshots.get(p.symbol)
            df = None
            if snap is not None:
                df = snap.frames.get("M5")
                if df is None:
                    df = snap.frames.get("M15")
            if df is None or len(df) < 3:
                if timed_out:
                    self.store.agent_event(p.agent_id, "shadow_timeout_no_data", {"position": pid, "symbol": p.symbol, "opened_at": p.opened_at})
                    del self.positions[pid]
                    dirty = True
                continue
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
            if exit_px is None and timed_out:
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
            dirty = True
        if dirty:
            self._save()
        return closed

    def summary(self) -> dict:
        return {"open": len(self.positions), "by_agent": {a: sum(1 for p in self.positions.values() if p.agent_id == a) for a in {p.agent_id for p in self.positions.values()}}}
