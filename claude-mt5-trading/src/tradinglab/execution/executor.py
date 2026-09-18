"""Exécuteur : order_send après approbation du gate, puis vérification post-fill (SL présent, volume, TP).

Si le SL manque après le fill : correction immédiate ; sinon fermeture ; puis SAFE_MODE.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.journal import Journal
from ..core.state import BotPositionPlan, StateStore, SystemMode
from ..core.types import GateResult, OrderRequest, OrderResult, Position, TradeCandidate, utcnow
from ..mt5.adapter import BrokerAdapter


@dataclass
class ExecutionOutcome:
    executed: bool
    order: Optional[OrderResult]
    position: Optional[Position]
    sl_verified: bool
    message: str


class Executor:
    def __init__(self, broker: BrokerAdapter, store: StateStore, journal: Journal):
        self.broker = broker
        self.store = store
        self.journal = journal

    def execute(self, candidate: TradeCandidate, gate: GateResult, req: OrderRequest, risk_money: float,
                risk_percent: float) -> ExecutionOutcome:
        state = self.store.state
        if not gate.approved or req is None:
            return ExecutionOutcome(False, None, None, False, "gate non approuvé")
        # marquer AVANT l'envoi : un crash entre send et save ne doit jamais provoquer une ré-entrée
        state.mark_executed(candidate.idempotency_key)
        self.store.save()
        res = self.broker.order_send(req)
        self.journal.event("order_send", candidate_id=candidate.id, agent_id=candidate.agent_id, request=req.to_dict(),
                           result=res.to_dict(), gate=gate.to_dict())
        if not res.ok:
            return ExecutionOutcome(False, res, None, False, f"order_send refusé retcode={res.retcode} {res.comment}")
        pos = self._find_position(req, res)
        if pos is None:
            self.journal.error("position introuvable après fill", ticket=res.ticket, symbol=req.symbol)
            state.set_mode(SystemMode.SAFE_MODE, "position introuvable après fill")
            self.store.save()
            return ExecutionOutcome(True, res, None, False, "position introuvable après fill → SAFE_MODE")
        sl_ok = pos.has_sl
        if not sl_ok:
            fix = self.broker.modify_position(pos.ticket, req.sl, req.tp)
            self.journal.warn("SL absent après fill : tentative de correction", ticket=pos.ticket, result=fix.to_dict())
            pos = self.broker.position(pos.ticket) or pos
            sl_ok = pos.has_sl
            if not sl_ok:
                close = self.broker.close_position(pos.ticket, comment="TLAB no-SL close")
                self.journal.error("SL impossible à poser : position fermée", ticket=pos.ticket, result=close.to_dict())
                state.set_mode(SystemMode.SAFE_MODE, "SL impossible après fill")
                self.store.save()
                return ExecutionOutcome(True, res, None, False, "SL impossible → position fermée → SAFE_MODE")
        if abs(pos.volume - req.volume) > 1e-9:
            self.journal.warn("volume rempli différent du volume demandé", ticket=pos.ticket, asked=req.volume, filled=pos.volume)
        plan = BotPositionPlan(
            ticket=pos.ticket, symbol=pos.symbol, side=pos.side.value, agent_id=candidate.agent_id,
            candidate_id=candidate.id, entry=pos.price_open, initial_sl=pos.sl, initial_volume=pos.volume,
            initial_risk_money=risk_money * (pos.volume / req.volume if req.volume else 1.0), risk_percent=risk_percent,
            regime=candidate.regime.value, tp_plan=list(candidate.tp_plan), opened_at=utcnow().isoformat(),
            last_sl=pos.sl, invalidation=candidate.invalidation,
        )
        state.bot_positions[str(pos.ticket)] = plan
        self.store.save()
        self.journal.event("position_opened", ticket=pos.ticket, symbol=pos.symbol, side=pos.side.value, volume=pos.volume,
                           entry=pos.price_open, sl=pos.sl, tp=pos.tp, agent_id=candidate.agent_id, candidate=candidate.to_dict())
        return ExecutionOutcome(True, res, pos, True, "ok")

    def _find_position(self, req: OrderRequest, res: OrderResult) -> Optional[Position]:
        if res.ticket:
            p = self.broker.position(res.ticket)
            if p:
                return p
        cands = [p for p in self.broker.positions(magic=req.magic) if p.symbol == req.symbol and p.side == req.side
                 and str(p.ticket) not in self.store.state.bot_positions]
        return sorted(cands, key=lambda p: p.time_open)[-1] if cands else None
