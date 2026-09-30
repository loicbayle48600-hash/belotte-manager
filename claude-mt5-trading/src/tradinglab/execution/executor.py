"""Exécuteur : order_send après approbation du gate, puis vérification post-fill (SL présent, volume, TP).

Si le SL manque après le fill : correction immédiate ; sinon fermeture ; puis SAFE_MODE.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ..core.journal import Journal
from ..core.state import BotPositionPlan, StateStore, SystemMode
from ..core.types import GateResult, OrderRequest, OrderResult, Position, Side, TradeCandidate, utcnow
from ..mt5.adapter import BrokerAdapter
from ..risk.risk_manager import loss_per_lot
from ..risk.stop_loss import normalize_price

# le terminal MT5 peut ne pas refléter la position immédiatement après order_send : quelques tentatives espacées
FIND_POSITION_ATTEMPTS = 5
FIND_POSITION_DELAY_SEC = 0.2


@dataclass
class ExecutionOutcome:
    executed: bool
    order: Optional[OrderResult]
    position: Optional[Position]
    sl_verified: bool
    message: str


class Executor:
    def __init__(self, broker: BrokerAdapter, store: StateStore, journal: Journal,
                 idea_window_minutes: float = 10.0):
        self.broker = broker
        self.store = store
        self.journal = journal
        # fenêtre d'agrégation des idées de trade (prop firm : rouvrir dans le même sens sous 10 min)
        self.idea_window_minutes = float(idea_window_minutes)
        # cible broker fixe en multiples de R depuis le prix réel (profit_management.final_target_r ; 0 = cible du signal)
        self.final_target_r = 0.0

    def _cible_finale(self, pos: Position) -> Optional[Position]:
        """Pose la cible broker à exactement `final_target_r` R du prix d'exécution RÉEL (2026-09-30, version lab-v2).

        Le gate décide toujours sur la cible structurelle du signal (RR ≥ 1,5 au scan et au tick) : seules les sorties
        changent. Rejeu des mêmes signaux (9 agents live, 29 agents de recherche) : une cible fixe à 3 R fait mieux que
        la cible du signal (médiane 2,4 R) sur 7 agents live sur 9 et 25 sur 29, sur les deux moitiés de l'historique,
        avec un drawdown plus bas d'environ 10 % ; le TP2 à 2,5 R peut enfin se déclencher. Le SL n'est jamais touché."""
        if pos.sl is None or pos.sl <= 0 or self.final_target_r <= 0:
            return None
        r_reel = abs(pos.price_open - pos.sl)
        if r_reel <= 0:
            return None
        try:
            spec = self.broker.symbol_info(pos.symbol)
        except Exception:  # noqa: BLE001 - spec indisponible : on garde la cible d'origine, jamais d'approximation
            return None
        if spec is None:
            return None
        sens = 1.0 if pos.side == Side.BUY else -1.0
        cible = normalize_price(pos.price_open + sens * self.final_target_r * r_reel, spec)
        if pos.tp and abs(pos.tp - cible) < spec.point / 2:
            return None
        ancienne = pos.tp
        res = self.broker.modify_position(pos.ticket, pos.sl, cible)
        self.journal.event("cible_finale", ticket=pos.ticket, symbol=pos.symbol, r=self.final_target_r, ancienne=ancienne,
                           nouvelle=cible, ok=res.ok, retcode=res.retcode)
        if not res.ok:
            self.journal.warn("cible finale non posée : cible du signal conservée", ticket=pos.ticket, retcode=res.retcode)
        return self.broker.position(pos.ticket) if res.ok else None

    def _preserver_rr_cible(self, pos: Position, candidate: TradeCandidate) -> Optional[Position]:
        """Replace la cible broker au multiple de R que le gate a approuvé. Ne la rapproche jamais.

        Le gate pose `tp_plan[-1]` (la cible la plus lointaine) comme TP broker, calculé sur l'entrée du
        candidat — c'est-à-dire sur le prix du scan. Quand le marché a bougé entre le scan et l'envoi, cette
        cible se retrouve à un multiple de R plus faible qu'approuvé : le 2026-09-21 EURAUD est passé de
        2,08 R au plan à 1,35 R réel (entrée 1,61045 au lieu de ~1,61004, SL 1,609).

        C'est plus gênant qu'un simple manque à gagner. `tp_plan` n'est jamais relu par le position manager,
        qui gère en multiples de R (TP1 1,5 R, TP2 2,5 R, runner). Une cible broker tombée sous 1,5 R ferme
        donc **100 % de la position avant que l'échelle n'ait pu commencer** — le gagnant à +1,14 R du jour a
        cette forme. On restaure la géométrie approuvée ; le SL n'est pas touché, le risque reste identique.
        """
        if not candidate.tp_plan or pos.tp is None or pos.tp <= 0:
            return None
        r_plan, r_reel = candidate.sl_distance, abs(pos.price_open - pos.sl)
        if r_plan <= 0 or r_reel <= 0:
            return None
        rr_approuve = abs(candidate.tp_plan[-1] - candidate.entry) / r_plan
        rr_reel = abs(pos.tp - pos.price_open) / r_reel
        if rr_reel >= rr_approuve - 1e-9:
            return None                       # cible intacte (ou fill favorable) : ne rien toucher
        try:
            spec = self.broker.symbol_info(pos.symbol)
        except Exception:  # noqa: BLE001 - spec indisponible : on laisse la cible d'origine, jamais d'approximation
            return None
        sens = 1.0 if pos.side == Side.BUY else -1.0
        cible = normalize_price(pos.price_open + sens * rr_approuve * r_reel, spec)
        # `ancienne` est relevée AVANT l'appel : l'adaptateur peut modifier l'objet Position en place,
        # auquel cas la journaliser après ferait afficher la nouvelle valeur dans les deux champs.
        ancienne = pos.tp
        res = self.broker.modify_position(pos.ticket, pos.sl, cible)
        self.journal.warn("cible broker replacée au R approuvé", ticket=pos.ticket, rr_approuve=rr_approuve,
                          rr_reel=rr_reel, ancienne=ancienne, nouvelle=cible, ok=res.ok, retcode=res.retcode)
        return self.broker.position(pos.ticket) if res.ok else None

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
        pos = None
        for attempt in range(FIND_POSITION_ATTEMPTS):
            pos = self._find_position(req, res)
            if pos is not None:
                break
            if attempt < FIND_POSITION_ATTEMPTS - 1:
                time.sleep(FIND_POSITION_DELAY_SEC)
        if pos is None:
            # candidat journalisé intégralement pour permettre la reconstruction du plan après adoption
            self.journal.error("position introuvable après fill", ticket=res.ticket, symbol=req.symbol,
                               attempts=FIND_POSITION_ATTEMPTS, candidate=candidate.to_dict())
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
        # risque réel après fill : |price_open − sl| × volume (le slippage peut l'écarter du risque planifié)
        planned = risk_money * (pos.volume / req.volume if req.volume else 1.0)
        actual = self._actual_risk(pos)
        if actual is not None and actual > planned * 1.10:
            self.journal.warn("risque réel après fill supérieur au risque planifié", ticket=pos.ticket,
                              planned=planned, actual=actual, price_open=pos.price_open, sl=pos.sl)
        if self.final_target_r > 0:
            pos = self._cible_finale(pos) or pos
        else:
            pos = self._preserver_rr_cible(pos, candidate) or pos
        initial_risk = actual if actual is not None else planned
        equity = state.equity
        plan = BotPositionPlan(
            ticket=pos.ticket, symbol=pos.symbol, side=pos.side.value, agent_id=candidate.agent_id,
            candidate_id=candidate.id, entry=pos.price_open, initial_sl=pos.sl, initial_volume=pos.volume,
            initial_risk_money=initial_risk,
            risk_percent=(100.0 * initial_risk / equity) if (actual is not None and equity) else risk_percent,
            regime=candidate.regime.value, tp_plan=list(candidate.tp_plan), opened_at=utcnow().isoformat(),
            last_sl=pos.sl, invalidation=candidate.invalidation,
        )
        state.bot_positions[str(pos.ticket)] = plan
        # agrégation « idée de trade » exigée par la prop firm : le risque cumulé des positions prises
        # dans le même sens (réouverture sous N minutes comprise) est plafonné, jamais remis à zéro.
        idea = state.register_trade_idea(pos.symbol, pos.side.value, initial_risk, ticket=pos.ticket,
                                         now=utcnow(), window_minutes=self.idea_window_minutes)
        state.record_trading_day(utcnow())
        self.store.save()
        self.journal.event("position_opened", ticket=pos.ticket, symbol=pos.symbol, side=pos.side.value, volume=pos.volume,
                           entry=pos.price_open, sl=pos.sl, tp=pos.tp, agent_id=candidate.agent_id,
                           trade_idea_id=idea.idea_id, trade_idea_risk_money=round(idea.risk_money, 2),
                           trade_idea_entries=idea.entries, candidate=candidate.to_dict())
        return ExecutionOutcome(True, res, pos, True, "ok")

    def _actual_risk(self, pos: Position) -> Optional[float]:
        """Perte au SL (devise du compte) calculée sur le fill réel ; None si spec/SL indisponibles."""
        if not pos.has_sl:
            return None
        try:
            spec = self.broker.symbol_info(pos.symbol)
        except Exception:  # noqa: BLE001
            spec = None
        if spec is None or spec.tick_size <= 0 or spec.tick_value <= 0:
            return None
        return loss_per_lot(pos.price_open, pos.sl, spec) * pos.volume

    def _find_position(self, req: OrderRequest, res: OrderResult) -> Optional[Position]:
        if res.ticket:
            p = self.broker.position(res.ticket)
            if p:
                return p
        cands = [p for p in self.broker.positions(magic=req.magic) if p.symbol == req.symbol and p.side == req.side
                 and str(p.ticket) not in self.store.state.bot_positions]
        return sorted(cands, key=lambda p: p.time_open)[-1] if cands else None
