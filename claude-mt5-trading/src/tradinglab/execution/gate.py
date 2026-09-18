"""Execution Gate déterministe : 20 contrôles avant tout order_send().

C'est le SEUL chemin vers broker.order_send() pour une ouverture de position.
Aucun agent LLM, aucun outil MCP n'y accède directement : ils produisent des
TradeCandidate ; le gate décide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    min_rr_required: float = 1.5
    required_setup_score: float = 65.0
    min_sl_atr_ratio: float = 0.25
    max_sl_atr_ratio: float = 4.0
    deviation_points: int = 20
    magic: int = 51000
    comment_prefix: str = "TLAB"
    # identité du compte attendue (account_expected) : 0 / "" = non vérifiée par le gate (compatibilité)
    expected_login: int = 0
    expected_server: str = ""


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
        # 7. spread
        if ctx.tick and ctx.spec:
            sp = ctx.tick.spread_points(ctx.spec)
            ratio = (sp * ctx.spec.point) / ctx.atr if ctx.atr > 0 else 1.0
            add("07_spread", sp <= ctx.max_spread_points and ratio <= ctx.max_spread_atr_ratio,
                f"spread {sp} pts, {ratio:.3f} ATR (max {ctx.max_spread_points} pts / {ctx.max_spread_atr_ratio})")
        else:
            add("07_spread", False, "spread inconnu")
        # 8. news
        if ctx.news_check is None:
            add("08_news", False, "état news inconnu → refus")
        else:
            add("08_news", bool(ctx.news_check.ok), f"{ctx.news_check.state}: {ctx.news_check.reason}")
        # 9. risque (garde journalière + sizing)
        dd = self.daily.evaluate(ctx.state)
        add("09_daily_guard", dd.entries_allowed, "; ".join(dd.reasons) or "ok")
        sizing = None
        if ctx.account and ctx.spec:
            sizing = self.risk.size(ctx.account.equity, c.entry, c.sl, ctx.spec, risk_percent=dd.risk_percent)
            add("09b_sizing", sizing.ok, sizing.reason + (f" vol={sizing.volume} risque={sizing.risk_money:.2f}" if sizing.ok else ""))
        else:
            add("09b_sizing", False, "compte/spec manquants")
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
        # 12/13. drawdowns + 19. prop rules
        risk_money = sizing.risk_money if sizing and sizing.ok else 0.0
        for cr in self.prop.limits(ctx.state, risk_money):
            add({"internal_daily_dd": "12_daily_drawdown", "internal_overall_dd": "13_overall_drawdown",
                 "prop_hard_daily": "19_prop_hard_daily", "prop_hard_overall": "19_prop_hard_overall"}[cr.name], cr.ok, cr.detail)
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
        if approved and sizing:
            c.risk_percent = sizing.risk_percent_effective
        return result, (req if approved else None)
