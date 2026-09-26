"""Revue adversariale (Bull / Bear / Devil's Advocate) et Trade Arbiter.

- Avec un modèle disponible : chaque rôle produit un JSON (arguments, score de
  conviction) marqué MODEL_INTERPRETATION ; l'arbitre (senior) tranche.
- Sans modèle (clé absente, budget épuisé) : arbitre déterministe documenté.
- Dans TOUS les cas, le Risk Gate a le dernier mot après un APPROVE.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from ..core.types import Provenance, TradeCandidate, Verdict
from ..models.client import LLMClient

SYSTEM_COMMON = (
    "Tu es un analyste de trading rigoureux. Tu ne connais que les données fournies. "
    "N'invente aucune donnée : si une information manque, écris UNKNOWN. "
    "Un score interne n'est jamais une probabilité de gain. "
    "Le dimensionnement du risque, la corrélation avec les positions ouvertes, les plafonds d'exposition et le "
    "spread sont vérifiés APRÈS toi par un Risk Gate déterministe : ne les juge pas, juge la logique du setup "
    "(structure, régime, niveaux, invalidation, contexte). Un historique court (sample_size faible) signifie "
    "agent récent : statistiques non significatives, à ne pas lire comme un edge négatif. "
    "Réponds UNIQUEMENT en JSON compact, sans bloc de code ni prose autour : au plus 4 éléments par liste, "
    "chaque élément ≤ 25 mots."
)
# Mesuré le 2026-09-21 (Opus 5, réflexion désactivée, consigne compacte) : thèses 220-500 tokens de sortie,
# ~5-9 s chacune. Sans la consigne, 500 tokens ne suffisaient pas et le JSON tronqué était illisible.
THESIS_MAX_TOKENS = 700
# 400 tronquait 2 appels sur 12 le 2026-09-21 au soir (rationales plus riches depuis le prompt setup-only)
ARBITER_MAX_TOKENS = 600
# champs du candidat régénérés à chaque cycle : exclus du prompt pour que deux cycles sur la même barre
# produisent le même prompt (lisibilité du journal) — l'identité de cache est `_cache_key`
_VOLATILE_FIELDS = ("id", "created_at", "review", "verdict")
# champs jamais renseignés au moment de la revue (calculés ensuite par le risk manager / le gate) : les laisser à
# 0.0 / UNKNOWN faisait rejeter les candidats pour « risk_percent=0.0 incohérent » (constaté le 2026-09-21 18:13)
_DOWNSTREAM_FIELDS = ("risk_percent", "correlation_impact")


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
        # note de contexte de marché ajoutée au dossier de l'IA (branchée par l'orchestrateur) : callable(c) -> str | None
        self.market_note = None
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
    @staticmethod
    def _cache_key(c: TradeCandidate) -> str:
        """Même symbole, même sens, même agent, même barre → mêmes thèses (TTL du client en garde-fou).
        L'ancienne clé `c.id` changeait à chaque cycle : aucune réutilisation, 4 appels payés par cycle et par candidat."""
        return f"{c.symbol}|{c.side.value}|{c.agent_id}|{c.bar_time}"

    def _ask(self, role: str, instruction: str, c: TradeCandidate, importance: str = "normal",
             max_tokens: int = THESIS_MAX_TOKENS) -> tuple[dict, float]:
        if self.llm is None:
            return {}, 0.0
        payload = {k: v for k, v in c.to_dict().items() if k not in _VOLATILE_FIELDS + _DOWNSTREAM_FIELDS}
        note = None
        if self.market_note is not None:
            try:
                note = self.market_note(c)
            except Exception:  # noqa: BLE001 - une note de contexte ne doit jamais empêcher la revue
                note = None
        if note:
            payload["contexte_marche"] = note
        user = instruction + "\n\nCANDIDAT:\n" + json.dumps(payload, ensure_ascii=False, default=str)[:6000]
        resp = self.llm.complete(role, SYSTEM_COMMON, user, max_tokens=max_tokens, financial_importance=importance,
                                 cache_key=self._cache_key(c))
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
        # les trois thèses sont indépendantes : en parallèle (≈ 9 s au lieu de ≈ 27 s, boucle live à 15 s)
        theses = (
            ("bull_thesis", 'Construis la meilleure thèse HAUSSIÈRE/pour ce trade. JSON: {"arguments": [...], "conviction_0_100": n, "unknowns": [...]}'),
            ("bear_thesis", 'Construis la meilleure thèse CONTRE ce trade. JSON: {"arguments": [...], "conviction_0_100": n, "unknowns": [...]}'),
            ("devil_advocate", 'Cherche les failles : faux breakout, surapprentissage, corrélation, qualité des données, exécution. JSON: {"flaws": [...], "severity_0_100": n}'),
        )
        with ThreadPoolExecutor(max_workers=len(theses), thread_name_prefix="review") as ex:
            (bull, c1), (bear, c2), (devil, c3) = list(ex.map(lambda t: self._ask(t[0], t[1], c), theses))
        arb, c4 = self._ask("trade_arbiter",
                            "Tu es l'arbitre. Décide APPROVE / WAIT / REJECT / NO_TRADE en pesant bull, bear et devil ci-dessous. "
                            "Le Risk Gate déterministe aura le dernier mot. JSON: {\"verdict\": \"...\", \"rationale\": \"...\"} — rationale en une phrase, 50 mots max.\n"
                            + json.dumps({"bull": bull, "bear": bear, "devil": devil}, ensure_ascii=False)[:4000], c,
                            importance="high", max_tokens=ARBITER_MAX_TOKENS)
        total = c1 + c2 + c3 + c4
        if not arb or str(arb.get("verdict", "")).upper() not in Verdict.__members__:
            det.bull, det.bear, det.devil, det.cost_usd = bull, bear, devil, total
            # 2026-09-26 : une revue IA commencée mais sans verdict ne valide JAMAIS une entrée. Le verdict
            # déterministe (souvent APPROVE, simple seuil de score) prenait la place d'un avis IA manquant.
            if det.verdict is Verdict.APPROVE:
                det.verdict = Verdict.WAIT
                det.rationale += " | arbitre IA sans verdict → attente (jamais d'entrée sans son avis)"
            else:
                det.rationale += " | arbitre IA sans verdict → décision déterministe"
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
