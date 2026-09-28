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
from ..risk.risk_manager import loss_per_lot, round_volume_down
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
    # 2026-09-28, demande utilisateur : « quand une position atteint +100 $, mettre directement le stop suiveur pour
    # rester en positif », puis « adapte, il y a plus de positions avec moins de lots » : le seuil est exprimé en
    # fraction du risque initial (0,16 = 100 $ pour les 625 $ risqués à 0,125 % ; 40 $ à 0,05 %), il suit donc la
    # taille des lots et vaut aussi chez les suiveurs. `protect_profit_money` : seuil absolu en devise du compte,
    # optionnel (le premier atteint déclenche). 0 = désactivé.
    protect_profit_risk_ratio: float = 0.0
    protect_profit_money: float = 0.0
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
    invalidation_reason: str = ""             # règle lue (texte) pour le journal / le post-trade


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
        """Retire de l'état les positions disparues (fermées) et renvoie celles-ci.

        Une liste vide obtenue pendant une déconnexion ou une erreur API n'est PAS une fermeture : dans ce cas
        la synchronisation est ignorée (aucune position retirée de l'état, aucun trade fantôme).
        """
        state = self.store.state
        if not self.broker.is_connected():
            self.journal.warn("broker déconnecté : synchronisation des positions ignorée")
            return []
        try:
            live = {p.ticket: p for p in self.broker.positions(magic=self.magic)}
        except Exception as e:  # noqa: BLE001 - positions indisponibles : ne jamais conclure à une fermeture
            self.journal.warn("positions indisponibles, sync ignorée", error=str(e))
            return []
        closed = []
        for t, plan in list(state.bot_positions.items()):
            if int(t) not in live:
                closed.append(plan)
                del state.bot_positions[t]
        # positions du bot présentes dans MT5 mais inconnues de l'état (restart) : adoption sans ré-ouverture
        for ticket, p in live.items():
            if str(ticket) not in state.bot_positions:
                sl = p.sl if p.has_sl else 0.0
                # risque réel estimé depuis le SL connu : la position adoptée compte dans max_total_open_risk
                # et le Correlation Guard (jamais un risque « 0 » invisible)
                risk = self._estimate_risk(p, sl)
                risk_pct = 100.0 * risk / state.equity if state.equity else 0.0
                state.bot_positions[str(ticket)] = BotPositionPlan(
                    ticket=ticket, symbol=p.symbol, side=p.side.value, agent_id="ADOPTED", candidate_id="",
                    entry=p.price_open, initial_sl=sl or p.price_open, initial_volume=p.volume,
                    initial_risk_money=risk, risk_percent=risk_pct, opened_at=p.time_open.isoformat(), last_sl=sl,
                    notes=["adoptée après redémarrage"])
                self.journal.warn("position bot adoptée après redémarrage", ticket=ticket, symbol=p.symbol, sl=p.sl,
                                  risk_estimated=risk)
        if closed:
            self.store.save()
        return closed

    def _estimate_risk(self, p: Position, sl: float) -> float:
        """Perte au SL (devise du compte) d'une position adoptée ; 0.0 si SL ou spec indisponibles."""
        if not sl or sl <= 0:
            return 0.0
        try:
            spec = self.broker.symbol_info(p.symbol)
        except Exception:  # noqa: BLE001
            spec = None
        if spec is None or spec.tick_size <= 0 or spec.tick_value <= 0:
            return 0.0
        return loss_per_lot(p.price_open, sl, spec) * p.volume

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
            plan.sl_missing_seen = True
            target = plan.last_sl or plan.initial_sl
            res = self.broker.modify_position(pos.ticket, target, pos.tp)
            actions.append(f"SL manquant → remis {target} ({'ok' if res.ok else res.comment})")
            self.journal.warn("SL manquant sur position bot, remise", ticket=pos.ticket, sl=target, result=res.to_dict())
            if not res.ok:
                res2 = self.broker.close_position(pos.ticket, comment="TLAB no-SL close")
                actions.append(f"fermeture (SL impossible): {res2.ok}")
                return actions

        # 0b. SL broker plus large que le dernier SL connu du plan → constat pour le post-trade (le SL n'est
        # jamais élargi par le bot ; un élargissement vient du terminal ou d'un tiers). On ne le resserre pas
        # d'office ici : le watchdog et le contrôle `never_widen_stop` couvrent l'action.
        known = plan.last_sl or plan.initial_sl
        tol = max(spec.tick_size, 0.0) * 1.5   # tolérance d'un tick : arrondis broker, jamais un vrai recul
        widened = (known - pos.sl > tol) if side is Side.BUY else (pos.sl - known > tol)
        if pos.has_sl and known and widened:
            if not plan.sl_widened_seen:
                self.journal.warn("SL broker plus large que le plan", ticket=pos.ticket, sl_broker=pos.sl, sl_plan=known)
            plan.sl_widened_seen = True

        # 1. sortie anticipée sur invalidation
        if cfg.allow_early_exit_on_invalidation and ctx.invalidated:
            res = self.broker.close_position(pos.ticket, comment="TLAB invalidation")
            actions.append(f"invalidation → fermeture ({res.ok})")
            self.journal.event("early_exit", ticket=pos.ticket, symbol=pos.symbol, reason="invalidation",
                               rule=ctx.invalidation_reason, r=r, result=res.to_dict())
            return actions

        # 2. TP partiels
        if not plan.tp1_done and r >= cfg.tp1_r:
            vol = round_volume_down(plan.initial_volume * cfg.tp1_close_percent / 100.0, spec)
            if vol >= spec.volume_min and vol < pos.volume:
                res = self.broker.close_position(pos.ticket, vol, comment="TLAB TP1")
                if res.ok:
                    plan.tp1_done = True
                    actions.append(f"TP1 {cfg.tp1_r}R: fermé {vol}")
                    # `gain_estime` est CALCULÉ (R x risque initial x part fermée), pas lu chez le
                    # broker : `OrderResult` ne porte pas le résultat réalisé. Le montant exact
                    # arrive à la clôture complète, via `post_trade_review`.
                    part = vol / plan.initial_volume if plan.initial_volume else 0.0
                    self.journal.event("partial_tp", ticket=pos.ticket, symbol=pos.symbol, level="TP1",
                                       r=round(r, 3), volume=vol, volume_restant=round(pos.volume - vol, 4),
                                       gain_estime=round(r * plan.initial_risk_money * part, 2),
                                       provenance="CALCULATED")
            else:
                plan.tp1_done = True  # volume trop petit pour un partiel : on passe
        if plan.tp1_done and not plan.tp2_done and r >= cfg.tp2_r:
            vol = round_volume_down(plan.initial_volume * cfg.tp2_close_percent / 100.0, spec)
            if vol >= spec.volume_min and vol < pos.volume:
                res = self.broker.close_position(pos.ticket, vol, comment="TLAB TP2")
                if res.ok:
                    plan.tp2_done = True
                    actions.append(f"TP2 {cfg.tp2_r}R: fermé {vol}")
                    # `gain_estime` est CALCULÉ (R x risque initial x part fermée), pas lu chez le
                    # broker : `OrderResult` ne porte pas le résultat réalisé. Le montant exact
                    # arrive à la clôture complète, via `post_trade_review`.
                    part = vol / plan.initial_volume if plan.initial_volume else 0.0
                    self.journal.event("partial_tp", ticket=pos.ticket, symbol=pos.symbol, level="TP2",
                                       r=round(r, 3), volume=vol, volume_restant=round(pos.volume - vol, 4),
                                       gain_estime=round(r * plan.initial_risk_money * part, 2),
                                       provenance="CALCULATED")
            else:
                plan.tp2_done = True

        # 3a. protection du profit (2026-09-28, demande utilisateur) : dès que le profit flottant atteint le seuil
        #     (fraction du risque initial, ou montant absolu), break-even et stop suiveur sont armés (`trailing_forced`,
        #     même mécanique que le bouton break-even), quel que soit le R par ailleurs
        ratio = float(cfg.protect_profit_risk_ratio or 0.0)
        seuil = float(cfg.protect_profit_money or 0.0)
        if (ratio > 0 or seuil > 0) and not plan.trailing_forced:
            profit = float(getattr(pos, "profit", 0.0) or 0.0)
            if not profit and plan.initial_volume > 0:
                # broker sans profit flottant (simulé) : estimation R × risque initial, au prorata du volume restant
                profit = r * plan.initial_risk_money * (float(pos.volume) / float(plan.initial_volume))
            par_ratio = ratio > 0 and max(r, plan.max_r) >= ratio
            par_montant = seuil > 0 and profit >= seuil
            if par_ratio or par_montant:
                plan.trailing_forced, plan.trailing_active = True, True
                cible = f"{ratio:.2f} R (≈ {ratio * plan.initial_risk_money:.0f} $)" if par_ratio else f"{seuil:.0f} $"
                actions.append(f"profit protégé : +{cible} atteints, break-even et stop suiveur armés")
                self.journal.event("profit_protection", ticket=pos.ticket, profit=round(profit, 2), r=round(r, 3),
                                   seuil_r=ratio, seuil_money=seuil)
        # 3. break-even (le drapeau break_even_done n'est posé qu'une fois le SL effectivement au-delà du BE :
        #    un BE refusé par le broker ou trop proche du prix est retenté au cycle suivant)
        new_sl: Optional[float] = None
        be_pending = False
        # `plan.max_r` et non `r` : le seuil doit être jugé sur le MEILLEUR point atteint depuis l'ouverture,
        # pas sur l'instant présent. Sinon un pic survenu entre deux cycles — ou avant un redémarrage, ou
        # avant un changement de réglage — n'arme jamais le stop, et le trade revient au stop plein malgré
        # un profit qui a bel et bien existé. Constaté deux fois : CADCHF (+1,11 R → -1,17 R, -586 $) et
        # NETH25 (+1,05 R → -1,03 R, -521 $). Le niveau reste borné par `is_tighter_or_equal` plus bas :
        # si le prix est déjà repassé sous le break-even, le broker refuse et rien ne s'élargit jamais.
        if cfg.break_even_enabled and not plan.break_even_done and (max(r, plan.max_r) >= cfg.break_even_r or plan.trailing_forced):
            # `tp1_done` lève l'exigence de structure : une fois un profit partiel encaissé,
            # le break-even n'est plus négociable. TP1 ferme 30 % à 1,5R (+0,45R) et laisse
            # 70 % au risque plein (-0,70R) : sans remontée du stop, un retour au stop initial
            # garantit une perte NETTE de 0,25R malgré un gain déjà pris. Constaté sur XRPUSD
            # le 2026-09-21 : TP1 +2,49 $ puis stop -3,78 $, net -1,29 $, structure jamais validée.
            # Avant TP1 l'exigence garde son sens : elle évite de se faire sortir sur du bruit.
            if plan.tp1_done or plan.trailing_forced or not cfg.break_even_requires_structure or ctx.structure_ok:
                be = self._be_level(plan, side, spec)
                new_sl = be
                be_pending = True
                actions.append(f"break-even {be}")

        # 4. trailing (ATR + structure). 2026-09-25, demande utilisateur : démarre à +1,05 R (au lieu de 2 R) et ne
        #    descend jamais sous le break-even (plancher ci-dessous) : une fois +1,05 R atteint, le trade reste en
        #    bénéfice et le stop suit le prix. `max_r` : un pic entre deux cycles arme aussi le suivi.
        #    `trailing_forced` : armé par le bouton break-even (2026-09-28), le suivi commence sans attendre le seuil.
        if cfg.trailing_enabled and ctx.atr > 0 and (max(r, plan.max_r) >= cfg.trailing_start_r or plan.trailing_forced):
            trail = price - side.sign * ctx.atr * cfg.trailing_atr_multiplier
            if cfg.trailing_use_market_structure:
                sw = ctx.last_swing_low if side is Side.BUY else ctx.last_swing_high
                if sw is not None:
                    # structure : sous le swing bas (BUY) / au-dessus du swing haut (SELL), on garde le plus protecteur mais pas au-delà du prix
                    trail = max(trail, sw - 0.1 * ctx.atr) if side is Side.BUY else min(trail, sw + 0.1 * ctx.atr)
            be_floor = self._be_level(plan, side, spec)
            trail = max(trail, be_floor) if side is Side.BUY else min(trail, be_floor)
            plan.trailing_active = True
            if new_sl is None or (side is Side.BUY and trail > new_sl) or (side is Side.SELL and trail < new_sl):
                new_sl = trail

        # 5. application du SL (jamais élargi, jamais retiré, jamais au-delà du prix courant / stops_level)
        if new_sl is not None:
            new_sl = normalize_price(new_sl, spec)
            current = pos.sl if pos.has_sl else plan.last_sl
            if cfg.never_widen_stop and not is_tighter_or_equal(side, new_sl, current):
                actions.append(f"SL {new_sl} refusé (élargirait {current})")
                if be_pending:
                    plan.break_even_done = True   # le SL courant est déjà au-delà du break-even
            else:
                min_dist = spec.min_stop_distance
                too_close = abs(price - new_sl) < min_dist or (side is Side.BUY and new_sl >= price) or (side is Side.SELL and new_sl <= price)
                if too_close:
                    actions.append(f"SL {new_sl} trop proche du prix, ignoré")
                elif abs(new_sl - current) >= spec.tick_size:
                    res = self.broker.modify_position(pos.ticket, new_sl, pos.tp)
                    if res.ok:
                        plan.last_sl = new_sl
                        if be_pending:
                            plan.break_even_done = True
                        actions.append(f"SL → {new_sl}")
                        self.journal.event("sl_modified", ticket=pos.ticket, old=current, new=new_sl, r=r)
                    else:
                        actions.append(f"modify refusé: {res.comment}")
                else:
                    # SL courant déjà au niveau demandé (écart < tick) : le break-even est acquis
                    if be_pending:
                        plan.break_even_done = True
        self.store.save()
        return actions

    #: commission aller-retour par lot selon la classe d'actif (branchée par l'orchestrateur sur le profil prop)
    commission_per_lot = None
    #: commission ramenée en distance de prix (spec, prix) -> float ; prioritaire sur `commission_per_lot` (crypto)
    commission_price = None

    def _be_level(self, plan: BotPositionPlan, side: Side, spec: Optional[SymbolSpec]) -> float:
        """Niveau de break-even : entrée + offset (0,05 R) + commission ramenée en distance de prix.

        2026-09-25, demande utilisateur (« rester toujours en bénéfice ») : avec un stop serré, la commission FOXX
        (7 $/lot forex) dépassait les 0,05 R de l'offset — un trade sorti au break-even finissait en perte nette.
        Commission par lot / (valeur d'un tick par lot / taille du tick) = distance de prix, indépendante du volume."""
        dist = abs(plan.entry - plan.initial_sl)
        com = 0.0
        if self.commission_price is not None and spec is not None:
            try:
                com = float(self.commission_price(spec, plan.entry))
            except Exception:  # noqa: BLE001 - commission inconnue : l'offset seul s'applique
                com = 0.0
            return plan.entry + side.sign * (dist * self.cfg.break_even_offset_r + com)
        if self.commission_per_lot is not None and spec is not None and getattr(spec, "tick_value", 0) > 0:
            try:
                com = float(self.commission_per_lot(spec.asset_class)) * spec.tick_size / spec.tick_value
            except Exception:  # noqa: BLE001 - commission inconnue : l'offset seul s'applique
                com = 0.0
        return plan.entry + side.sign * (dist * self.cfg.break_even_offset_r + com)

    # ---------- commandes toujours autorisées ----------
    def close(self, ticket: int, reason: str = "manual") -> bool:
        """Ferme une position du bot. Le plan reste dans l'état : c'est sync() qui constate la disparition
        au cycle suivant, comme pour une sortie par SL/TP, afin que post-trade, stats journalières et
        apprentissage soient toujours produits (tout est journalisé)."""
        res = self.broker.close_position(ticket, comment=f"TLAB {reason}"[:31])
        self.journal.event("close_command", ticket=ticket, reason=reason, result=res.to_dict())
        plan = self.store.state.bot_positions.get(str(ticket))
        if plan is not None:
            plan.notes.append(f"close_command:{reason}" + ("" if res.ok else ":refusée"))
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

    def move_to_break_even(self, ticket: int, spec: Optional[SymbolSpec]) -> bool:
        """Bouton « break-even » (panneau / CLI). 2026-09-28, demande utilisateur : le stop est placé juste au-dessus de
        l'entrée, EN PROFIT (entrée + 0,05 R + commission ; si le prix est trop près de ce niveau pour le broker, au plus
        près du prix que le broker accepte, tant que cela reste en profit), et le stop suiveur est armé dès maintenant
        (`trailing_forced`) sans attendre +1,05 R : le trade est protégé en positif et suit le prix."""
        plan = self.store.state.bot_positions.get(str(ticket))
        pos = self.broker.position(ticket)
        if not plan or not pos or spec is None:
            if plan and pos and spec is None:
                self.journal.warn("break-even impossible : spécifications symbole indisponibles", ticket=ticket)
            return False
        side = Side(plan.side)
        be = normalize_price(self._be_level(plan, side, spec), spec)
        tick = self.broker.tick(pos.symbol)
        # référence du broker pour la distance du stop : bid pour un achat, ask pour une vente
        price = ((tick.bid if side is Side.BUY else tick.ask) if tick else None) or pos.price_current or pos.price_open
        # jamais au-delà du prix courant ni sous stops_level (même règle que manage()) : si le BE est trop près, on
        # recule au niveau le plus proche accepté par le broker (+ 1 tick de marge), s'il reste au-dessus de l'entrée
        marge = max(float(spec.min_stop_distance), float(spec.tick_size)) + float(spec.tick_size)
        limite = normalize_price(price - side.sign * marge, spec)
        if side.sign * (be - limite) > 0:
            if side.sign * (limite - plan.entry) <= 0:
                self.journal.warn("break-even impossible : le prix n'est pas encore en profit", ticket=ticket, sl=be, price=price)
                return False
            be = limite
        if not is_tighter_or_equal(side, be, pos.sl):
            # le stop est déjà au-delà : on n'élargit jamais, mais le suivi est armé quand même
            plan.trailing_forced, plan.trailing_active = True, True
            self.store.save()
            self.journal.event("break_even_command", ticket=ticket, sl=pos.sl, deja_au_dela=True, suivi=True)
            return True
        res = self.broker.modify_position(ticket, be, pos.tp)
        if res.ok:
            plan.last_sl = be
            plan.break_even_done = True
            plan.trailing_forced, plan.trailing_active = True, True
            self.store.save()
        self.journal.event("break_even_command", ticket=ticket, sl=be, suivi=res.ok, result=res.to_dict())
        return res.ok
