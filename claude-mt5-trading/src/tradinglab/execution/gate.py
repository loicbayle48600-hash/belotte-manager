"""Execution Gate déterministe : 20 contrôles avant tout order_send().

C'est le SEUL chemin vers broker.order_send() pour une ouverture de position.
Aucun agent LLM, aucun outil MCP n'y accède directement : ils produisent des
TradeCandidate ; le gate décide.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from ..core.state import SystemState
from ..core.types import (AccountInfo, CheckResult, GateResult, OrderRequest, Side, SymbolSpec, Tick, TradeCandidate,
                          TradeMode, Verdict, utcnow)
from ..mt5.adapter import BrokerAdapter
from ..risk.correlation_guard import CorrelationGuard
from ..risk.daily_guard import DailyGuard
from ..risk.prop_guard import PropGuard
from ..risk.risk_manager import RiskManager
from ..risk.stop_loss import normalize_price, validate_stop_loss


@dataclass
class GateContext:
    """Tout ce dont le gate a besoin, fourni par l'orchestrateur (aucun accès LLM ici)."""
    candidate: TradeCandidate
    state: SystemState
    account: Optional[AccountInfo]
    spec: Optional[SymbolSpec]
    tick: Optional[Tick]
    atr: float
    data_quality: str = "OK"
    market_open: bool = True
    news_check: Any = None                 # objet avec .ok / .state / .reason (NewsCheck) ou None
    correlations: Optional[pd.DataFrame] = None
    now: Optional[datetime] = None
    watchdog_alive: bool = True
    open_positions_symbol: int = 0
    open_positions_total: int = 0
    max_spread_points: int = 40
    max_spread_atr_ratio: float = 0.15
    # part maximale du RISQUE (distance entrée→stop) que le spread a le droit de consommer.
    # 2026-09-22, accord utilisateur : mesuré sur le journal, certains symboles proposaient un stop
    # PLUS COURT que le spread lui-même (GBPTRY : spread médian 4 126 pts pour un stop médian de 38 pts,
    # soit 108x). Un tel trade est perdu d'avance : le coût d'entrée dépasse le risque assumé.
    max_spread_sl_ratio: float = 0.35
    min_rr_required: float = 1.5
    rollover_block: str = ""          # non vide : paire exotique dans la fenêtre du reset 17:00 NY (raison)
    # contrôles neutralisés par la configuration (2026-09-25, décision utilisateur « reviens au 19 septembre ») :
    # toujours évalués et journalisés, mais ils ne refusent plus rien
    disabled_checks: tuple = ()
    required_setup_score: float = 65.0
    min_sl_atr_ratio: float = 0.25
    max_sl_atr_ratio: float = 4.0
    deviation_points: int = 20
    magic: int = 51000
    comment_prefix: str = "TLAB"
    # identité du compte attendue (account_expected) : 0 / "" = non vérifiée par le gate (compatibilité)
    expected_login: int = 0
    expected_server: str = ""


#: classe d'actif -> clé de `execution.max_spread_points` dans system.yaml
SPREAD_KEY_BY_ASSET_CLASS = {"metals": "XAU", "indices": "indices", "crypto": "crypto", "energies": "energies"}


def max_spread_points_for(asset_class: str, cfg: dict) -> int:
    """Plafond de spread applicable à une classe d'actif.

    Une classe absente de `cfg` retombe sur `default`. C'est ce silence qui avait rendu
    la crypto intradable : spread réel ~500 points contre un défaut à 40, donc contrôle
    `07_spread` refusé à chaque candidat, sans message distinctif.
    """
    default = int(cfg.get("default", 40))
    key = SPREAD_KEY_BY_ASSET_CLASS.get(str(asset_class or "").lower())
    return int(cfg.get(key, default)) if key else default


class ExecutionGate:
    def __init__(self, broker: BrokerAdapter, risk: RiskManager, prop: PropGuard, daily: DailyGuard,
                 corr: CorrelationGuard):
        self.broker = broker
        self.risk = risk
        self.prop = prop
        self.daily = daily
        self.corr = corr
        # les lignes d'exposition existantes utilisent les devises réelles des specs broker (cf. CorrelationGuard)
        if getattr(self.corr, "spec_lookup", None) is None:
            self.corr.spec_lookup = broker.symbol_info

    def evaluate(self, ctx: GateContext) -> tuple[GateResult, Optional[OrderRequest]]:
        c = ctx.candidate
        checks: list[CheckResult] = []
        req: Optional[OrderRequest] = None
        now = ctx.now or utcnow()

        def add(name: str, ok: bool, detail: str = "") -> bool:
            if name in ctx.disabled_checks and not ok:
                ok, detail = True, f"désactivé (retour au 19/09) — aurait refusé : {detail}"
            checks.append(CheckResult(name, bool(ok), detail))
            return bool(ok)

        # 1. health : chaque cause manquante est nommée explicitement dans le journal
        connected = self.broker.is_connected()
        problems = [m for ok, m in ((connected, "broker déconnecté"), (ctx.watchdog_alive, "watchdog absent")) if not ok]
        add("01_health", not problems, "; ".join(problems) or "ok")
        # 2. account : equity > 0 et identité (login/serveur) conforme à account_expected si fournie
        acc_ok = ctx.account is not None and ctx.account.equity > 0
        acc_detail = f"equity={ctx.account.equity if ctx.account else 'UNKNOWN'}"
        if ctx.account is not None:
            unexpected = []
            if ctx.expected_login and int(ctx.account.login) != int(ctx.expected_login):
                unexpected.append(f"login {ctx.account.login} ≠ attendu {ctx.expected_login}")
            if ctx.expected_server and str(ctx.account.server) != str(ctx.expected_server):
                unexpected.append(f"serveur {ctx.account.server} ≠ attendu {ctx.expected_server}")
            if unexpected:
                acc_ok = False
                acc_detail += "; compte inattendu : " + "; ".join(unexpected)
        add("02_account", acc_ok, acc_detail)
        # 3. autorisation demo/prop
        auth = self.prop.authorization(ctx.account.trade_mode if ctx.account else TradeMode.UNKNOWN)
        add("03_authorization", auth.ok, auth.detail)
        # 4. fraîcheur données
        add("04_data_fresh", ctx.data_quality == "OK" and ctx.tick is not None, f"data_quality={ctx.data_quality}")
        # 5. symbole tradable
        add("05_symbol_tradable", ctx.spec is not None and ctx.spec.trade_allowed and ctx.market_open,
            "ok" if ctx.spec and ctx.spec.trade_allowed and ctx.market_open else "symbole non tradable / marché fermé")
        # 6. prix frais et cohérent avec l'entrée prévue
        if ctx.tick and ctx.spec and ctx.atr > 0:
            ref = ctx.tick.ask if c.side is Side.BUY else ctx.tick.bid
            drift = abs(ref - c.entry)
            add("06_fresh_price", drift <= 0.5 * ctx.atr, f"écart entrée/prix {drift:.{ctx.spec.digits}f} (≤ 0.5 ATR)")
        else:
            add("06_fresh_price", False, "tick/ATR indisponible")
        # 6b. RR au prix COURANT, cible structurelle inchangée. `20_final_approval` juge `c.rr` calculé sur
        # le prix du scan ; la dérive tolérée par 06 (0,5 ATR) ramenait un 2 R à 1,19 R (USTEC, 2026-09-21),
        # et l'exécuteur repoussait alors le TP au-delà du niveau que la stratégie avait identifié — une cible
        # que le marché n'a aucune raison particulière d'atteindre. On refuse plutôt l'entrée dégradée.
        if ctx.tick and c.tp_plan and c.sl:
            ref = ctx.tick.ask if c.side is Side.BUY else ctx.tick.bid
            dist_sl = abs(ref - c.sl)
            rr_live = abs(c.tp_plan[-1] - ref) / dist_sl if dist_sl > 0 else 0.0
            add("06b_rr_live", rr_live >= ctx.min_rr_required, f"rr live {rr_live:.2f} (≥ {ctx.min_rr_required}) au prix {ref}")
        else:
            add("06b_rr_live", False, "tick/TP/SL indisponible")
        # 7. spread (absolu et relatif à la volatilité)
        if ctx.tick and ctx.spec:
            sp = ctx.tick.spread_points(ctx.spec)
            ratio = (sp * ctx.spec.point) / ctx.atr if ctx.atr > 0 else 1.0
            add("07_spread", sp <= ctx.max_spread_points and ratio <= ctx.max_spread_atr_ratio,
                f"spread {sp} pts, {ratio:.3f} ATR (max {ctx.max_spread_points} pts / {ctx.max_spread_atr_ratio})")
        else:
            add("07_spread", False, "spread inconnu")
        cost_part = 0.0                   # coût d'entrée / risque, exporté dans le GateResult
        # 7b. spread rapporté au RISQUE du trade : le coût d'entrée ne doit pas dévorer la distance au stop.
        # Le contrôle 07 (spread / ATR) ne voit pas ce cas : un stop bien plus serré que l'ATR passe malgré
        # un spread énorme. Sans repère (tick/spec/SL manquant) → refus, comme partout ailleurs dans le gate.
        if ctx.tick and ctx.spec and c.sl:
            ref = ctx.tick.ask if c.side is Side.BUY else ctx.tick.bid
            dist_sl = abs(ref - c.sl)
            sp_price = ctx.tick.spread_points(ctx.spec) * ctx.spec.point
            # Commission de la prop firm (FOXX : 7 $/lot forex et matières premières, 3 $ crypto, 0 indice — relevé
            # le 2026-09-23) convertie en distance de prix : commission / (valeur d'un tick par lot / taille du tick).
            # Elle s'ajoute au spread : c'est le coût réel d'entrée rapporté à la distance au stop.
            com_price = self.prop.commission_price(ctx.spec, ref)
            part = (sp_price + com_price) / dist_sl if dist_sl > 0 else 1.0
            part_sp = sp_price / dist_sl if dist_sl > 0 else 1.0
            cost_part = part
            add("07b_spread_vs_sl", part <= ctx.max_spread_sl_ratio,
                f"spread {part_sp * 100:.0f} % + commission {max(0.0, part - part_sp) * 100:.0f} % = {part * 100:.0f} % du risque "
                f"(max {ctx.max_spread_sl_ratio * 100:.0f} %)")
        else:
            add("07b_spread_vs_sl", False, "spread/SL indisponible")
        # 7c. rollover (2026-09-24) : pas de paire exotique autour du reset 17:00 New York (spread qui explose)
        add("07c_rollover_exotique", not ctx.rollover_block, ctx.rollover_block or "hors fenêtre de rollover")
        # 8. news
        if ctx.news_check is None:
            add("08_news", False, "état news inconnu → refus")
        else:
            add("08_news", bool(ctx.news_check.ok), f"{ctx.news_check.state}: {ctx.news_check.reason}")
        # 9. risque (garde journalière + sizing)
        dd = self.daily.evaluate(ctx.state)
        add("09_daily_guard", dd.entries_allowed, "; ".join(dd.reasons) or "ok")
        sizing = None
        px = c.entry
        if ctx.account and ctx.spec:
            # Taille calculée sur le prix COURANT, pas sur `c.entry` qui date du scan. Les screeners
            # signalent sur du mouvement : le prix continue dans le sens du trade entre le scan et l'envoi,
            # l'entrée se dégrade et la distance au SL grandit d'autant. Comme `06_fresh_price` tolère
            # jusqu'à 0,5 ATR de dérive, un SL à 1,5 ATR encaissait déjà +33 % de risque, davantage s'il
            # était plus serré. Mesuré le 2026-09-21 sur cinq fills : +26, +28, +34, +62 et +83 %, jamais
            # en dessous. Le biais est structurel, pas du bruit — il disparaît en dimensionnant sur le tick.
            px = (ctx.tick.ask if c.side is Side.BUY else ctx.tick.bid) if ctx.tick else c.entry
            spec_eff = ctx.spec
            risk_pct = dd.risk_percent
            # 19. plafond de lots FOXX par classe d'actifs (positions déjà ouvertes sur la même idée comprises) :
            # le volume est raboté au reste disponible, jamais dépassé ; plus de reste → refus
            cap_lots = self.prop.max_lots(ctx.spec.asset_class)
            if cap_lots is None:
                add("19_prop_max_lots", True, "aucune table de lots dans le profil prop")
            else:
                deja = self.prop.open_idea_lots(ctx.state, c.symbol, c.side.value)
                restant = round(cap_lots - deja, 8)
                add("19_prop_max_lots", restant > 0,
                    f"{ctx.spec.asset_class}: max {cap_lots:g} lots par idée, déjà {deja:g}, reste {max(0.0, restant):g}")
                if restant > 0 and restant < ctx.spec.volume_max:
                    spec_eff = replace(ctx.spec, volume_max=restant)
            # 19. cohérence 25 % : le gain visé (risque × R du dernier TP) ne peut dépasser la part autorisée
            cap_gain = self.prop.consistency_gain_cap(ctx.state, c.symbol, c.side.value, ctx.now)
            if cap_gain is None:
                add("19_prop_consistency", True, "règle de cohérence non applicable (profit du cycle sous le seuil)")
            else:
                dist = abs(px - c.sl)
                r_max = (abs(c.tp_plan[-1] - px) / dist) if (c.tp_plan and dist > 0) else 0.0
                if cap_gain <= 0 or r_max <= 0:
                    add("19_prop_consistency", False, f"gain autorisé {cap_gain:.2f} : aucune idée ne peut plus peser < 25 %")
                else:
                    risk_cap_pct = 100.0 * (cap_gain / r_max) / ctx.account.equity if ctx.account.equity else risk_pct
                    if risk_cap_pct < risk_pct:
                        add("19_prop_consistency", True,
                            f"gain plafonné à {cap_gain:.2f} ({r_max:.2f} R) → risque réduit de {risk_pct:.4f}% à {risk_cap_pct:.4f}%")
                        risk_pct = risk_cap_pct
                    else:
                        add("19_prop_consistency", True, f"gain visé sous le plafond {cap_gain:.2f}")
            sizing = self.risk.size(ctx.account.equity, px, c.sl, spec_eff, risk_percent=risk_pct)
            add("09b_sizing", sizing.ok, sizing.reason + (f" vol={sizing.volume} risque={sizing.risk_money:.2f}" if sizing.ok else ""))
        else:
            add("09b_sizing", False, "compte/spec manquants")
            add("19_prop_max_lots", False, "compte/spec manquants")
            add("19_prop_consistency", False, "compte/spec manquants")
        # 10. SL
        if ctx.spec:
            ref = (ctx.tick.ask if c.side is Side.BUY else ctx.tick.bid) if ctx.tick else c.entry
            slc = validate_stop_loss(c.side, c.entry, c.sl, ctx.spec, ctx.atr, ctx.min_sl_atr_ratio, ctx.max_sl_atr_ratio, ref)
            add("10_stop_loss", slc.ok, slc.reason)
        else:
            add("10_stop_loss", False, "spec manquante")
        # 11. volume
        vol_ok = bool(sizing and sizing.ok and ctx.spec and ctx.spec.volume_min <= sizing.volume <= ctx.spec.volume_max)
        add("11_volume", vol_ok, f"{sizing.volume if sizing else 'n/a'}")
        # 11b. risque utile : `volume_max` peut raboter la position au point qu'elle ne puisse plus rien
        # changer au compte, tout en consommant un emplacement et le quota d'une position par symbole.
        # Le 2026-09-21 XRPUSD est passé deux fois à 0,0023 % de risque (11,52 $ et 12,10 $ sur 500 000 $).
        # Ce n'est pas un refus de prudence mais d'utilité : un pari qui ne pèse rien n'a pas sa place.
        plancher = self.risk.limits.min_effective_risk_ratio * dd.risk_percent
        if sizing and sizing.ok:
            add("11b_risque_utile", sizing.risk_percent_effective >= plancher - 1e-12,
                f"risque effectif {sizing.risk_percent_effective:.4f}% (plancher {plancher:.4f}% = "
                f"{self.risk.limits.min_effective_risk_ratio:g} x {dd.risk_percent:g}%)")
        else:
            add("11b_risque_utile", False, "sizing invalide")
        # 12/13. drawdowns + 19. prop rules
        risk_money = sizing.risk_money if sizing and sizing.ok else 0.0
        names = {"internal_daily_dd": "12_daily_drawdown", "internal_overall_dd": "13_overall_drawdown",
                 "prop_hard_daily": "19_prop_hard_daily", "prop_hard_overall": "19_prop_hard_overall",
                 "prop_trade_idea_risk": "19_prop_trade_idea", "prop_weekend_sessions": "19_prop_weekend",
                 "prop_no_hedge": "19_prop_no_hedge", "prop_no_reversal": "19_prop_no_reversal"}
        for cr in self.prop.limits(ctx.state, risk_money, symbol=c.symbol, side=c.side.value, now=ctx.now,
                                   asset_class=ctx.spec.asset_class if ctx.spec else ""):
            # un contrôle prop ajouté plus tard sans entrée de correspondance reste appliqué (préfixe 19_)
            add(names.get(cr.name, "19_" + cr.name), cr.ok, cr.detail)
        # 14. exposition ouverte (limites risk manager)
        for cr in self.risk.check_limits(ctx.state, c.symbol, c.side, risk_money, ctx.open_positions_symbol, ctx.open_positions_total):
            add("14_" + cr.name, cr.ok, cr.detail)
        # 15. corrélation
        for cr in self.corr.check(ctx.state, c.symbol, c.side, risk_money, ctx.correlations,
                                  ctx.spec.currency_base if ctx.spec else "", ctx.spec.currency_profit if ctx.spec else ""):
            add("15_" + cr.name, cr.ok, cr.detail)
        # 16. idée dupliquée (idempotence, y compris après restart)
        add("16_duplicate_idea", not ctx.state.has_executed(c.idempotency_key), c.idempotency_key)
        # 17. position existante sur le symbole (gérée aussi par max_positions_per_symbol)
        add("17_existing_position", ctx.open_positions_symbol == 0, f"{ctx.open_positions_symbol} position(s) sur {c.symbol}")
        # 18. order_check broker
        if sizing and sizing.ok and ctx.spec:
            req = OrderRequest(symbol=c.symbol, side=c.side, volume=sizing.volume, sl=normalize_price(c.sl, ctx.spec),
                               tp=normalize_price(c.tp_plan[-1], ctx.spec) if c.tp_plan else 0.0, magic=ctx.magic,
                               comment=f"{ctx.comment_prefix}:{c.agent_id}"[:31], deviation_points=ctx.deviation_points,
                               agent_id=c.agent_id, candidate_id=c.id)
            chk = self.broker.order_check(req)
            add("18_order_check", chk.ok, f"retcode={chk.retcode} {chk.comment}")
        else:
            add("18_order_check", False, "pas de requête (sizing invalide)")
        # 20. approbation finale déterministe
        allowed, why = ctx.state.entries_allowed()
        rr_ok = c.rr >= ctx.min_rr_required
        score_ok = c.setup_score >= ctx.required_setup_score + dd.setup_score_bonus
        verdict_ok = c.verdict is Verdict.APPROVE
        add("20_final_approval", allowed and rr_ok and score_ok and verdict_ok,
            f"{why}; rr={c.rr:.2f}(≥{ctx.min_rr_required}); score={c.setup_score:.0f}(≥{ctx.required_setup_score + dd.setup_score_bonus:.0f}); verdict={c.verdict.value if c.verdict else None}")

        approved = all(ch.ok for ch in checks)
        failed = [ch for ch in checks if not ch.ok]
        result = GateResult(approved=approved, checks=checks, reason="APPROVED" if approved else "; ".join(f"{f.name}: {f.detail}" for f in failed[:6]),
                            adjusted_volume=sizing.volume if sizing and sizing.ok else 0.0)
        if sizing and sizing.ok:
            # Le risque qui part avec l'ordre est celui du sizing sur le tick, pas un recalcul sur `c.entry` :
            # avant, l'orchestrateur redimensionnait sur le prix du scan et transmettait à l'exécuteur un
            # `risk_money` qui ne correspondait pas au volume envoyé (plan de position et journal faux).
            result.risk_money = sizing.risk_money
            result.risk_percent = sizing.risk_percent_effective
            result.sizing_price = float(px)
            result.volume_wanted = sizing.volume_wanted
            result.volume_capped = sizing.volume_capped
            result.cost_ratio = round(float(cost_part), 4)
        if approved and sizing:
            c.risk_percent = sizing.risk_percent_effective
        return result, (req if approved else None)
