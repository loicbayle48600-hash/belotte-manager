"""Position Manager : ADAPTIVE_R_MANAGEMENT (TP partiels, break-even, trailing ATR/structure).

Règles absolues : NEVER_WIDEN_STOP, NEVER_REMOVE_STOP ; la fermeture est toujours autorisée.
1R = risque monétaire initial (distance entrée → SL initial).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..core.journal import Journal
from ..core.state import BotPositionPlan, StateStore
from ..core.types import Position, Side, SymbolSpec, utcnow
from ..mt5.adapter import BrokerAdapter
from ..risk.risk_manager import round_volume_down
from ..risk.stop_loss import is_tighter_or_equal, normalize_price


@dataclass
class PMConfig:
    tp1_r: float = 1.5
    tp1_close_percent: float = 30
    tp2_r: float = 2.5
    tp2_close_percent: float = 40
    runner_percent: float = 30
    break_even_enabled: bool = True
    break_even_r: float = 1.2
    break_even_requires_structure: bool = True
    break_even_offset_r: float = 0.05
    trailing_enabled: bool = True
    trailing_start_r: float = 2.0
    trailing_atr_multiplier: float = 1.5
    trailing_use_market_structure: bool = True
    never_widen_stop: bool = True
    never_remove_stop: bool = True
    allow_early_exit_on_invalidation: bool = True
    regime_overrides: dict | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "PMConfig":
        return cls(**{k: cfg[k] for k in cls.__dataclass_fields__ if k in cfg})

    def for_regime(self, regime: str) -> "PMConfig":
        ov = (self.regime_overrides or {}).get(regime)
        if not ov:
            return self
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d.update({k: v for k, v in ov.items() if k in d})
        return PMConfig(**d)


@dataclass
class MarketContext:
    """Contexte déterministe fourni par l'orchestrateur pour une position."""
    atr: float
    last_swing_low: Optional[float] = None    # dernier swing bas confirmé
    last_swing_high: Optional[float] = None
    structure_ok: bool = True                 # structure favorable confirmée (pour BE)
    invalidated: bool = False                 # règle d'invalidation de l'agent déclenchée


def r_multiple(plan: BotPositionPlan, price: float) -> float:
    dist = abs(plan.entry - plan.initial_sl)
    if dist <= 0:
        return 0.0
    sign = 1 if plan.side == "BUY" else -1
    return sign * (price - plan.entry) / dist


class PositionManager:
    def __init__(self, broker: BrokerAdapter, store: StateStore, journal: Journal, cfg: PMConfig, magic: int):
        self.broker = broker
        self.store = store
        self.journal = journal
        self.cfg = cfg
        self.magic = magic

    # ---------- synchronisation ----------
    def sync(self) -> list[BotPositionPlan]:
        """Retire de l'état les positions disparues (fermées) et renvoie celles restantes."""
        state = self.store.state
        live = {p.ticket: p for p in self.broker.positions(magic=self.magic)}
        closed = []
        for t, plan in list(state.bot_positions.items()):
            if int(t) not in live:
                closed.append(plan)
                del state.bot_positions[t]
        # positions du bot présentes dans MT5 mais inconnues de l'état (restart) : adoption sans ré-ouverture
        for ticket, p in live.items():
            if str(ticket) not in state.bot_positions:
                sl = p.sl if p.has_sl else 0.0
                state.bot_positions[str(ticket)] = BotPositionPlan(
                    ticket=ticket, symbol=p.symbol, side=p.side.value, agent_id="ADOPTED", candidate_id="",
                    entry=p.price_open, initial_sl=sl or p.price_open, initial_volume=p.volume,
                    initial_risk_money=0.0, risk_percent=0.0, opened_at=p.time_open.isoformat(), last_sl=sl,
                    notes=["adoptée après redémarrage"])
                self.journal.warn("position bot adoptée après redémarrage", ticket=ticket, symbol=p.symbol, sl=p.sl)
        if closed:
            self.store.save()
        return closed

    # ---------- gestion ----------
    def manage(self, plan: BotPositionPlan, pos: Position, spec: SymbolSpec, ctx: MarketContext) -> list[str]:
        actions: list[str] = []
        cfg = self.cfg.for_regime(plan.regime)
        price = pos.price_current or (self.broker.tick(pos.symbol).bid if self.broker.tick(pos.symbol) else pos.price_open)
        r = r_multiple(plan, price)
        plan.max_r = max(plan.max_r, r)
        plan.min_r = min(plan.min_r, r)
        side = Side(plan.side)

        # 0. SL toujours présent
        if not pos.has_sl:
            target = plan.last_sl or plan.initial_sl
            res = self.broker.modify_position(pos.ticket, target, pos.tp)
            actions.append(f"SL manquant → remis {target} ({'ok' if res.ok else res.comment})")
            self.journal.warn("SL manquant sur position bot, remise", ticket=pos.ticket, sl=target, result=res.to_dict())
            if not res.ok:
                res2 = self.broker.close_position(pos.ticket, comment="TLAB no-SL close")
                actions.append(f"fermeture (SL impossible): {res2.ok}")
                return actions

        # 1. sortie anticipée sur invalidation
        if cfg.allow_early_exit_on_invalidation and ctx.invalidated:
            res = self.broker.close_position(pos.ticket, comment="TLAB invalidation")
            actions.append(f"invalidation → fermeture ({res.ok})")
            self.journal.event("early_exit", ticket=pos.ticket, reason="invalidation", r=r, result=res.to_dict())
            return actions

        # 2. TP partiels
        if not plan.tp1_done and r >= cfg.tp1_r:
            vol = round_volume_down(plan.initial_volume * cfg.tp1_close_percent / 100.0, spec)
            if vol >= spec.volume_min and vol < pos.volume:
                res = self.broker.close_position(pos.ticket, vol, comment="TLAB TP1")
                if res.ok:
                    plan.tp1_done = True
                    actions.append(f"TP1 {cfg.tp1_r}R: fermé {vol}")
                    self.journal.event("partial_tp", ticket=pos.ticket, level="TP1", r=r, volume=vol)
            else:
                plan.tp1_done = True  # volume trop petit pour un partiel : on passe
        if plan.tp1_done and not plan.tp2_done and r >= cfg.tp2_r:
            vol = round_volume_down(plan.initial_volume * cfg.tp2_close_percent / 100.0, spec)
            if vol >= spec.volume_min and vol < pos.volume:
                res = self.broker.close_position(pos.ticket, vol, comment="TLAB TP2")
                if res.ok:
                    plan.tp2_done = True
                    actions.append(f"TP2 {cfg.tp2_r}R: fermé {vol}")
                    self.journal.event("partial_tp", ticket=pos.ticket, level="TP2", r=r, volume=vol)
            else:
                plan.tp2_done = True

        # 3. break-even
        new_sl: Optional[float] = None
        if cfg.break_even_enabled and not plan.break_even_done and r >= cfg.break_even_r:
            if not cfg.break_even_requires_structure or ctx.structure_ok:
                dist = abs(plan.entry - plan.initial_sl)
                be = plan.entry + side.sign * dist * cfg.break_even_offset_r
                new_sl = be
                plan.break_even_done = True
                actions.append(f"break-even {be}")

        # 4. trailing (ATR + structure)
        if cfg.trailing_enabled and r >= cfg.trailing_start_r and ctx.atr > 0:
            trail = price - side.sign * ctx.atr * cfg.trailing_atr_multiplier
            if cfg.trailing_use_market_structure:
                sw = ctx.last_swing_low if side is Side.BUY else ctx.last_swing_high
                if sw is not None:
                    # structure : sous le swing bas (BUY) / au-dessus du swing haut (SELL), on garde le plus protecteur mais pas au-delà du prix
                    trail = max(trail, sw - 0.1 * ctx.atr) if side is Side.BUY else min(trail, sw + 0.1 * ctx.atr)
            plan.trailing_active = True
            if new_sl is None or (side is Side.BUY and trail > new_sl) or (side is Side.SELL and trail < new_sl):
                new_sl = trail

        # 5. application du SL (jamais élargi, jamais retiré, jamais au-delà du prix courant / stops_level)
        if new_sl is not None:
            new_sl = normalize_price(new_sl, spec)
            current = pos.sl if pos.has_sl else plan.last_sl
            if cfg.never_widen_stop and not is_tighter_or_equal(side, new_sl, current):
                actions.append(f"SL {new_sl} refusé (élargirait {current})")
            else:
                min_dist = spec.min_stop_distance
                too_close = abs(price - new_sl) < min_dist or (side is Side.BUY and new_sl >= price) or (side is Side.SELL and new_sl <= price)
                if too_close:
                    actions.append(f"SL {new_sl} trop proche du prix, ignoré")
                elif abs(new_sl - current) >= spec.tick_size:
                    res = self.broker.modify_position(pos.ticket, new_sl, pos.tp)
                    if res.ok:
                        plan.last_sl = new_sl
                        actions.append(f"SL → {new_sl}")
                        self.journal.event("sl_modified", ticket=pos.ticket, old=current, new=new_sl, r=r)
                    else:
                        actions.append(f"modify refusé: {res.comment}")
        self.store.save()
        return actions

    # ---------- commandes toujours autorisées ----------
    def close(self, ticket: int, reason: str = "manual") -> bool:
        res = self.broker.close_position(ticket, comment=f"TLAB {reason}"[:31])
        self.journal.event("close_command", ticket=ticket, reason=reason, result=res.to_dict())
        if res.ok:
            self.store.state.bot_positions.pop(str(ticket), None)
            self.store.save()
        return res.ok

    def close_all_bot(self, reason: str = "close_all") -> list[int]:
        done = []
        for p in self.broker.positions(magic=self.magic):
            if self.close(p.ticket, reason):
                done.append(p.ticket)
        return done

    def cancel_all_bot_orders(self) -> list[int]:
        done = []
        for o in self.broker.pending_orders(magic=self.magic):
            if self.broker.cancel_order(o.ticket).ok:
                done.append(o.ticket)
        return done

    def move_to_break_even(self, ticket: int, spec: SymbolSpec) -> bool:
        plan = self.store.state.bot_positions.get(str(ticket))
        pos = self.broker.position(ticket)
        if not plan or not pos:
            return False
        side = Side(plan.side)
        be = normalize_price(plan.entry + side.sign * abs(plan.entry - plan.initial_sl) * self.cfg.break_even_offset_r, spec)
        if not is_tighter_or_equal(side, be, pos.sl):
            return False
        res = self.broker.modify_position(ticket, be, pos.tp)
        if res.ok:
            plan.last_sl = be
            plan.break_even_done = True
            self.store.save()
        self.journal.event("break_even_command", ticket=ticket, sl=be, result=res.to_dict())
        return res.ok
