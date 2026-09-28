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
    # la commission d'ENTRÉE (Raw Spread IC Markets, ~3,5 $/lot par côté) est portée par le deal IN : sans elle, le
    # P&L surestimait chaque trade de la moitié de sa commission (2026-09-24, point 4)
    ins = [d for d in deals if d.position_id == position_id and d.entry in ("IN", "INOUT")]
    pnl = sum(d.profit + d.commission + d.swap for d in outs) + sum(d.commission for d in ins)
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


def close_costs(deals: list[Deal], position_id: int) -> dict:
    """Décomposition du résultat d'une position (2026-09-28, demande utilisateur : « sur le récap Telegram, le prix de
    la commission, du spread et le bénéfice net ») : brut (deals OUT), commission (IN + OUT), swap."""
    mine = [d for d in deals if d.position_id == position_id]
    outs = [d for d in mine if d.entry in ("OUT", "OUT_BY")]
    return {"brut": round(sum(d.profit for d in outs), 2),
            "commission": round(sum(d.commission for d in mine), 2),
            "swap": round(sum(d.swap for d in outs), 2)}


def build_trade_record(plan: BotPositionPlan, deals: list[Deal], candidate: dict | None, closed_at: str,
                       session: str = "", news_context: str = "", macro_context: str = "", strict: bool = False,
                       point: float = 0.0, value_per_price: float = 0.0) -> TradeRecord:
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
    feats = {"setup_score": c.get("setup_score"), "atr": c.get("atr"), "rr": c.get("rr"), "spread_points": c.get("spread_points"),
             "version": c.get("version"), "cost_ratio": c.get("cost_ratio"), "slippage_r": c.get("slippage_r")}
    feats.update(c.get("features", {}) if isinstance(c.get("features"), dict) else {})
    if point and c.get("spread_points") and plan.entry and plan.initial_sl and abs(plan.entry - plan.initial_sl) > 0:
        # part du risque mangée par le spread à l'entrée : la même mesure que le contrôle 07b du gate
        feats["spread_sl_ratio"] = round(float(c["spread_points"]) * float(point) / abs(plan.entry - plan.initial_sl), 4)
    # coûts en devise du compte (2026-09-28) : brut / commission / swap lus dans les deals ; spread à l'entrée estimé
    # (points × valeur d'un point pour le volume initial) — le broker ne le facture pas séparément, il est dans le brut
    if reason != "UNKNOWN":
        feats.update(close_costs(deals, plan.ticket))
        if point and value_per_price and c.get("spread_points"):
            feats["spread_cost"] = round(float(c["spread_points"]) * float(point) * float(value_per_price) * float(plan.initial_volume), 2)
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
        rule_compliance={"sl_present": not getattr(plan, "sl_missing_seen", False),
                         "sl_never_widened": not getattr(plan, "sl_widened_seen", False),
                         "partials": [plan.tp1_done, plan.tp2_done], "break_even": plan.break_even_done},
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
        # 2026-09-25 : un seuil fixe de 40 points classait ERREUR_EXECUTION un US500 à 50 points (0,5 point d'indice,
        # spread normal = 8 % du risque). Le spread est jugé en part du stop comme au gate (07b, max 35 %) ; le seuil
        # en points ne sert plus que si cette part est inconnue (candidat perdu après un redémarrage).
        ratio = (rec.features or {}).get("spread_sl_ratio")
        max_ratio = float(self.cfg.get("max_spread_sl_ratio", 0.35))
        if ratio is not None:
            exec_quality = "OK" if float(ratio) <= max_ratio else f"spread élevé ({float(ratio) * 100:.0f} % du risque)"
        else:
            exec_quality = "OK" if rec.spread_points <= 40 else f"spread élevé ({rec.spread_points} pts)"
        verdict = "VARIANCE_NORMALE"
        challenger = False
        # ERREUR_EXECUTION : constatable sur UN seul trade, contrairement à ERREUR_STRATEGIE qui exige
        # une statistique. Deux cas, tous deux mesurés et non déduits :
        #   - le trade a été payant d'au moins 1R puis est revenu au stop : l'idée était juste, c'est la
        #     gestion qui a rendu le gain (cas XRPUSD du 2026-09-21, TP1 encaissé puis sortie nette en perte) ;
        #   - les conditions d'exécution à l'entrée étaient dégradées (spread au-delà de la limite).
        # `rule_compliance` : `sl_present` / `sl_never_widened` sont des constats du position manager
        # (`sl_missing_seen`, `sl_widened_seen`, observés à chaque passage). Un manquement est signalé dans
        # `what_failed` mais ne change pas le verdict : ce n'est ni une erreur de stratégie ni une erreur
        # d'exécution du bot (le SL est remis/fermé aussitôt), c'est un incident broker/terminal à suivre.
        rc = rec.rule_compliance or {}
        if rc.get("sl_present") is False:
            failed.append("position vue sans SL au moins une fois (remis ou fermée par le bot)")
        if rc.get("sl_never_widened") is False:
            failed.append("SL broker vu plus large que le SL du plan (élargissement externe)")
        if rec.result_r <= 0:
            if rec.exit_reason == "sl" and mae > 0.95 and mfe >= 1.0:
                verdict = "ERREUR_EXECUTION"
            elif exec_quality != "OK":
                verdict = "ERREUR_EXECUTION"
                failed.append(f"conditions d'exécution dégradées à l'entrée : {exec_quality}")
        st = stats or self.store.agent_stats(rec.agent_id)
        min_n = int(self.cfg.get("min_sample_size", 40))
        if st.sample_size >= min_n:
            # un agent statistiquement cassé prime sur l'incident d'exécution du jour
            if st.expectancy_r < 0 and st.degradation_score >= 0.5:
                verdict, challenger = "ERREUR_STRATEGIE", True
                failed.append(f"expectancy {st.expectancy_r:.2f}R négative sur {st.sample_size} trades, dégradation {st.degradation_score:.2f}")
            elif st.degradation_score >= 0.5:
                challenger = True
        review = PostTradeReview(
            ticket=rec.ticket, agent_id=rec.agent_id, result_r=rec.result_r, pnl=rec.pnl, exit_reason=rec.exit_reason,
            scenario=rec.reasoning_summary or "UNKNOWN", regime=rec.regime, news_macro=f"{rec.news_context}/{rec.macro_context}",
            execution_quality=exec_quality, sl_quality=sl_quality, exit_quality=exit_quality, mfe_r=mfe, mae_r=mae,
            rule_compliance=rec.rule_compliance, what_worked=worked, what_failed=failed, verdict=verdict, challenger_needed=challenger,
        )
        if self.llm is not None and abs(rec.result_r) >= 1.0:
            # 800 tokens : ce rôle est routé sur Fable 5, qui n'accepte que la réflexion adaptive — une partie
            # du budget part en réflexion, et 400 tronquait la réponse (constaté le 2026-09-22 00:47, stop max_tokens)
            resp = self.llm.complete("post_trade_root_cause",
                                     "Tu analyses un trade fermé. N'invente rien ; réponds en JSON compact {\"root_cause\": str, \"lesson\": str, \"strategy_error_or_variance\": str}, chaque champ <= 40 mots.",
                                     json.dumps({"trade": rec.__dict__, "review": review.to_dict()}, ensure_ascii=False, default=str)[:6000],
                                     max_tokens=800, financial_importance="low")
            if resp:
                review.llm_notes = resp.json() or {"raw": resp.text[:300]}
                review.provenance = Provenance.MODEL_INTERPRETATION.value
        return review
