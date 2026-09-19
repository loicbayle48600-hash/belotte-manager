"""Revue post-trade : après chaque trade fermé, analyse structurée (déterministe + LLM optionnel).

Ne modifie jamais une stratégie après une seule perte : produit un rapport et,
si nécessaire, une recommandation "besoin de challenger" fondée sur les stats.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..core.state import BotPositionPlan
from ..core.types import Deal, Provenance, Side
from ..models.client import LLMClient
from .store import AgentStats, LearningStore, TradeRecord


@dataclass
class PostTradeReview:
    ticket: int
    agent_id: str
    result_r: float
    pnl: float
    exit_reason: str
    scenario: str
    regime: str
    news_macro: str
    execution_quality: str
    sl_quality: str
    exit_quality: str
    mfe_r: float
    mae_r: float
    rule_compliance: dict
    what_worked: list[str] = field(default_factory=list)
    what_failed: list[str] = field(default_factory=list)
    verdict: str = "VARIANCE_NORMALE"     # VARIANCE_NORMALE | ERREUR_STRATEGIE | ERREUR_EXECUTION | INDETERMINE
    challenger_needed: bool = False
    llm_notes: Optional[dict] = None
    provenance: str = Provenance.CALCULATED.value

    def to_dict(self) -> dict:
        return self.__dict__


class ClosedTradeUnknown(RuntimeError):
    """Aucun deal de sortie retrouvé pour la position : le résultat est UNAVAILABLE (pas 0)."""


def close_deals_summary(deals: list[Deal], position_id: int) -> tuple[float, float, float, str, Optional[str]]:
    """(pnl_total, volume_sorti, prix_moyen_sortie, raison, closed_at) à partir des deals OUT."""
    outs = [d for d in deals if d.position_id == position_id and d.entry in ("OUT", "OUT_BY")]
    if not outs:
        return 0.0, 0.0, 0.0, "UNKNOWN", None
    pnl = sum(d.profit + d.commission + d.swap for d in outs)
    vol = sum(d.volume for d in outs)
    px = sum(d.price * d.volume for d in outs) / vol if vol else 0.0
    reasons = {d.comment.lower() for d in outs}
    if any("sl" in r or "stop" in r for r in reasons):
        reason = "sl"
    elif any("tp" in r for r in reasons):
        reason = "tp"
    elif any("invalid" in r for r in reasons):
        reason = "invalidation"
    elif any("panic" in r or "manual" in r or "close" in r for r in reasons):
        reason = "manual"
    else:
        reason = "mixed"
    return pnl, vol, px, reason, max(d.time for d in outs).isoformat()


def build_trade_record(plan: BotPositionPlan, deals: list[Deal], candidate: dict | None, closed_at: str,
                       session: str = "", news_context: str = "", macro_context: str = "", strict: bool = False) -> TradeRecord:
    """Construit le TradeRecord d'une position fermée à partir des deals OUT.

    Sans deal de sortie retrouvé (historique MT5 pas encore synchronisé, fenêtre d'historique dépassée…), le résultat
    n'est PAS connu : avec ``strict=True`` on lève ``ClosedTradeUnknown`` (l'appelant réessaie plus tard) ; sinon le
    record est renvoyé avec ``exit_reason="UNKNOWN"`` et ``features["outcome_provenance"]="UNAVAILABLE"`` — les
    statistiques (``LearningStore.agent_stats``) et la revue post-trade l'excluent comme résultat inconnu.
    """
    pnl, vol, px, reason, _ = close_deals_summary(deals, plan.ticket)
    if reason == "UNKNOWN" and strict:
        raise ClosedTradeUnknown(f"aucun deal de sortie pour la position {plan.ticket} ({plan.symbol})")
    result_r = pnl / plan.initial_risk_money if plan.initial_risk_money else 0.0
    from datetime import datetime
    try:
        dur = (datetime.fromisoformat(closed_at) - datetime.fromisoformat(plan.opened_at)).total_seconds()
    except (ValueError, TypeError):
        dur = 0.0
    c = candidate or {}
    feats = {"setup_score": c.get("setup_score"), "atr": c.get("atr"), "rr": c.get("rr"), "spread_points": c.get("spread_points")}
    feats.update(c.get("features", {}) if isinstance(c.get("features"), dict) else {})
    # jamais de None persisté : une valeur inconnue (candidat absent après redémarrage, position adoptée) = clé absente
    feats = {k: v for k, v in feats.items() if v is not None}
    if reason == "UNKNOWN":
        feats["outcome_provenance"] = Provenance.UNAVAILABLE.value
    return TradeRecord(
        ticket=plan.ticket, agent_id=plan.agent_id, symbol=plan.symbol, side=plan.side, entry=plan.entry, sl=plan.initial_sl,
        risk_money=plan.initial_risk_money, risk_percent=plan.risk_percent, result_r=round(result_r, 4), pnl=round(pnl, 2),
        opened_at=plan.opened_at, closed_at=closed_at, agent_version=str(c.get("agent_version", "1.0")),
        session=session or c.get("session", ""), regime=plan.regime, timeframes=c.get("timeframes", []), features=feats,
        tp=plan.tp_plan, spread_points=int(c.get("spread_points", 0) or 0), news_context=news_context or c.get("news_state", ""),
        macro_context=macro_context or c.get("macro_alignment", ""), reasoning_summary=(c.get("review") or {}).get("rationale", ""),
        mae_r=round(-min(0.0, plan.min_r), 3), mfe_r=round(max(0.0, plan.max_r), 3), duration_sec=dur, exit_reason=reason,
        rule_compliance={"sl_present": True, "sl_never_widened": True, "partials": [plan.tp1_done, plan.tp2_done], "break_even": plan.break_even_done},
        candidate_id=plan.candidate_id, review=c.get("review", {}) or {},
    )


class PostTradeAnalyzer:
    def __init__(self, store: LearningStore, llm: Optional[LLMClient] = None, learning_cfg: dict | None = None):
        self.store = store
        self.llm = llm
        self.cfg = learning_cfg or {}

    def analyze(self, rec: TradeRecord, stats: Optional[AgentStats] = None) -> PostTradeReview:
        if rec.exit_reason == "UNKNOWN":
            # résultat inconnu : aucune leçon à tirer, aucun LLM, aucun challenger (rien n'est inventé)
            return PostTradeReview(
                ticket=rec.ticket, agent_id=rec.agent_id, result_r=rec.result_r, pnl=rec.pnl, exit_reason=rec.exit_reason,
                scenario=rec.reasoning_summary or "UNKNOWN", regime=rec.regime, news_macro=f"{rec.news_context}/{rec.macro_context}",
                execution_quality="UNKNOWN", sl_quality="UNKNOWN", exit_quality="UNKNOWN", mfe_r=rec.mfe_r, mae_r=rec.mae_r,
                rule_compliance=rec.rule_compliance, what_failed=["aucun deal de sortie retrouvé : résultat UNAVAILABLE"],
                verdict="INDETERMINE", challenger_needed=False, provenance=Provenance.UNAVAILABLE.value,
            )
        worked, failed = [], []
        mfe, mae = rec.mfe_r, rec.mae_r
        sl_quality = "OK"
        if rec.exit_reason == "sl" and mae > 0.95 and mfe >= 1.0:
            sl_quality = "SL touché après excursion favorable >= 1R : gestion (BE/partiel) à examiner"
            failed.append("le trade a été positif de 1R avant de revenir au SL")
        elif rec.exit_reason == "sl" and mfe < 0.3:
            sl_quality = "SL touché sans excursion favorable : timing/entrée à examiner"
            failed.append("aucune excursion favorable : entrée prématurée ou mauvais sens")
        exit_quality = "OK"
        if rec.result_r > 0 and mfe > rec.result_r + 1.0:
            exit_quality = f"sortie à {rec.result_r:.2f}R alors que MFE {mfe:.2f}R : runner/trailing à examiner"
        if rec.result_r > 0:
            worked.append(f"résultat +{rec.result_r:.2f}R, sortie {rec.exit_reason}")
        exec_quality = "OK" if rec.spread_points <= 40 else f"spread élevé ({rec.spread_points} pts)"
        verdict = "VARIANCE_NORMALE"
        challenger = False
        st = stats or self.store.agent_stats(rec.agent_id)
        min_n = int(self.cfg.get("min_sample_size", 40))
        if st.sample_size >= min_n:
            if st.expectancy_r < 0 and st.degradation_score >= 0.5:
                verdict, challenger = "ERREUR_STRATEGIE", True
                failed.append(f"expectancy {st.expectancy_r:.2f}R négative sur {st.sample_size} trades, dégradation {st.degradation_score:.2f}")
            elif st.degradation_score >= 0.5:
                challenger = True
        else:
            verdict = "VARIANCE_NORMALE" if rec.result_r <= 0 else "VARIANCE_NORMALE"
        review = PostTradeReview(
            ticket=rec.ticket, agent_id=rec.agent_id, result_r=rec.result_r, pnl=rec.pnl, exit_reason=rec.exit_reason,
            scenario=rec.reasoning_summary or "UNKNOWN", regime=rec.regime, news_macro=f"{rec.news_context}/{rec.macro_context}",
            execution_quality=exec_quality, sl_quality=sl_quality, exit_quality=exit_quality, mfe_r=mfe, mae_r=mae,
            rule_compliance=rec.rule_compliance, what_worked=worked, what_failed=failed, verdict=verdict, challenger_needed=challenger,
        )
        if self.llm is not None and abs(rec.result_r) >= 1.0:
            resp = self.llm.complete("post_trade_root_cause",
                                     "Tu analyses un trade fermé. N'invente rien ; réponds en JSON {\"root_cause\": str, \"lesson\": str, \"strategy_error_or_variance\": str}",
                                     json.dumps({"trade": rec.__dict__, "review": review.to_dict()}, ensure_ascii=False, default=str)[:6000],
                                     max_tokens=400, financial_importance="low")
            if resp:
                review.llm_notes = resp.json() or {"raw": resp.text[:300]}
                review.provenance = Provenance.MODEL_INTERPRETATION.value
        return review
