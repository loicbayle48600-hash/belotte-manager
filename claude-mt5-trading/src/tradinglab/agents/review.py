"""Revue adversariale (Bull / Bear / Devil's Advocate) et Trade Arbiter.

- Avec un modèle disponible : chaque rôle produit un JSON (arguments, score de
  conviction) marqué MODEL_INTERPRETATION ; l'arbitre (senior) tranche.
- Sans modèle (clé absente, budget épuisé) : arbitre déterministe documenté.
- Dans TOUS les cas, le Risk Gate a le dernier mot après un APPROVE.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..core.types import Provenance, TradeCandidate, Verdict
from ..models.client import LLMClient

SYSTEM_COMMON = (
    "Tu es un analyste de trading rigoureux. Tu ne connais que les données fournies. "
    "N'invente aucune donnée : si une information manque, écris UNKNOWN. "
    "Un score interne n'est jamais une probabilité de gain. Réponds UNIQUEMENT en JSON."
)


@dataclass
class ReviewResult:
    verdict: Verdict
    arbiter: str                    # "llm:<model>" | "deterministic"
    bull: dict = field(default_factory=dict)
    bear: dict = field(default_factory=dict)
    devil: dict = field(default_factory=dict)
    rationale: str = ""
    cost_usd: float = 0.0
    provenance: Provenance = Provenance.CALCULATED
    hard: bool = False              # verdict de sécurité déterministe (news…) : jamais soumis à l'arbitre LLM
    llm_consulted: bool = False     # True si des appels LLM ont été tentés pour cette revue

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "arbiter": self.arbiter, "bull": self.bull, "bear": self.bear,
                "devil": self.devil, "rationale": self.rationale, "cost_usd": self.cost_usd, "provenance": self.provenance.value,
                "hard": self.hard, "llm_consulted": self.llm_consulted}


class AdversarialReview:
    def __init__(self, llm: Optional[LLMClient], required_score: float = 65.0, min_rr: float = 1.5, min_sample_for_stats: int = 40):
        self.llm = llm
        self.required_score = required_score
        self.min_rr = min_rr
        self.min_sample = min_sample_for_stats

    # ---------- déterministe ----------
    def deterministic(self, c: TradeCandidate, score_bonus: float = 0.0) -> ReviewResult:
        need = self.required_score + score_bonus
        reasons = []
        if c.data_quality != "OK":
            return ReviewResult(Verdict.REJECT, "deterministic", rationale=f"data_quality={c.data_quality}", hard=True)
        if c.news_state.startswith("BLOCKED") or c.news_state == "SHOCK":
            # WAIT de sécurité (règle news déterministe) : aucun LLM ne peut le transformer en APPROVE
            return ReviewResult(Verdict.WAIT, "deterministic", rationale=f"news_state={c.news_state}", hard=True)
        if c.rr < self.min_rr:
            return ReviewResult(Verdict.REJECT, "deterministic", rationale=f"rr {c.rr:.2f} < {self.min_rr}")
        stats = c.historical_stats or {}
        if stats.get("sample_size", 0) >= self.min_sample and stats.get("expectancy_r", 0.0) < 0:
            return ReviewResult(Verdict.REJECT, "deterministic", rationale="expectancy historique négative sur échantillon suffisant")
        if c.setup_score >= need:
            reasons.append(f"score {c.setup_score:.0f} >= {need:.0f}")
            v = Verdict.APPROVE
        elif c.setup_score >= need - 10:
            v = Verdict.WAIT
            reasons.append(f"score {c.setup_score:.0f} proche du seuil {need:.0f} : attendre confirmation")
        else:
            v = Verdict.REJECT
            reasons.append(f"score {c.setup_score:.0f} < {need:.0f}")
        if len(c.arguments_against) > len(c.arguments_for) and v is Verdict.APPROVE:
            v = Verdict.WAIT
            reasons.append("plus d'arguments contre que pour")
        return ReviewResult(v, "deterministic", rationale="; ".join(reasons))

    # ---------- LLM ----------
    def _ask(self, role: str, instruction: str, c: TradeCandidate, importance: str = "normal") -> tuple[dict, float]:
        if self.llm is None:
            return {}, 0.0
        user = instruction + "\n\nCANDIDAT:\n" + json.dumps(c.to_dict(), ensure_ascii=False, default=str)[:6000]
        resp = self.llm.complete(role, SYSTEM_COMMON, user, max_tokens=500, financial_importance=importance, cache_key_extra=c.id)
        if resp is None:
            return {}, 0.0
        data = resp.json()
        if not isinstance(data, dict):
            # JSON valide mais non-objet (liste, chaîne, nombre) ou absent : sortie non exploitable, jamais une exception
            data = {"raw": resp.text[:500], "parse_error": "réponse non-objet"}
        data["_model"] = resp.model
        data["_provenance"] = Provenance.MODEL_INTERPRETATION.value
        return data, resp.cost_usd

    def review(self, c: TradeCandidate, score_bonus: float = 0.0) -> ReviewResult:
        det = self.deterministic(c, score_bonus)
        if self.llm is None or det.verdict is Verdict.REJECT or det.hard:
            c.verdict = det.verdict
            c.review = det.to_dict()
            return det
        det.llm_consulted = True
        bull, c1 = self._ask("bull_thesis", 'Construis la meilleure thèse HAUSSIÈRE/pour ce trade. JSON: {"arguments": [...], "conviction_0_100": n, "unknowns": [...]}', c)
        bear, c2 = self._ask("bear_thesis", 'Construis la meilleure thèse CONTRE ce trade. JSON: {"arguments": [...], "conviction_0_100": n, "unknowns": [...]}', c)
        devil, c3 = self._ask("devil_advocate", 'Cherche les failles : faux breakout, surapprentissage, corrélation, qualité des données, exécution. JSON: {"flaws": [...], "severity_0_100": n}', c)
        arb, c4 = self._ask("trade_arbiter",
                            "Tu es l'arbitre. Décide APPROVE / WAIT / REJECT / NO_TRADE en pesant bull, bear et devil ci-dessous. "
                            "Le Risk Gate déterministe aura le dernier mot. JSON: {\"verdict\": \"...\", \"rationale\": \"...\"}\n"
                            + json.dumps({"bull": bull, "bear": bear, "devil": devil}, ensure_ascii=False)[:4000], c, importance="high")
        total = c1 + c2 + c3 + c4
        if not arb or str(arb.get("verdict", "")).upper() not in Verdict.__members__:
            det.bull, det.bear, det.devil, det.cost_usd = bull, bear, devil, total
            det.rationale += " | arbitre LLM indisponible → décision déterministe"
            c.verdict, c.review = det.verdict, det.to_dict()
            return det
        v = Verdict[str(arb["verdict"]).upper()]
        # l'arbitre LLM ne peut pas outrepasser un rejet déterministe ni approuver sous le seuil de score - 10
        if det.verdict is Verdict.WAIT and v is Verdict.APPROVE and c.setup_score < self.required_score + score_bonus - 10:
            v = Verdict.WAIT
        res = ReviewResult(v, f"llm:{arb.get('_model', '?')}", bull, bear, devil, str(arb.get("rationale", ""))[:800], total,
                           Provenance.MODEL_INTERPRETATION, llm_consulted=True)
        c.verdict, c.review = v, res.to_dict()
        return res
