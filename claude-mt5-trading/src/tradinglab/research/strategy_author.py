"""Auteur de stratégies : les agents PROPOSENT des variantes, ils ne les mettent jamais en production.

Un LLM (rôle ``strategy_research``, TIER_A) ou, à défaut, une règle déterministe propose des variantes d'un agent
parent. Chaque variante est validée strictement en Python (screener connu, paramètres bornés, filtres connus,
marchés du parent) puis ajoutée au registre UNIQUEMENT en statut RESEARCH via ``AgentRegistry.add`` : elle doit
ensuite franchir tout le ``ResearchPipeline`` (backtest → OOS → walk-forward → Monte Carlo → shadow → revues →
promotion). Aucun autre statut n'est jamais produit ici, quoi que réponde le modèle.
"""
from __future__ import annotations

import copy
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..agents.registry import ALL_SESSIONS, AgentRegistry, AgentSpec
from ..agents.screeners import SCREENERS
from ..core.types import AgentStatus, Provenance, Regime, utcnow

ROLE = "strategy_research"
CREATED_BY = "strategy_author"

# bornes sûres par paramètre (min, max) : hors de ces bornes la variante est rejetée, quel que soit le parent
SAFE_BOUNDS: dict[str, tuple[float, float]] = {
    "sl_atr": (0.5, 3.0), "rr": (1.5, 4.0), "adx_min": (15, 40),
    "rsi_lo": (10, 90), "rsi_hi": (10, 90), "rsi_ext": (10, 90),
    "atr_ratio": (1.0, 3.0), "tol_atr": (0.1, 1.0), "vol_pct_max": (50, 100),
}
RELATIVE_TOLERANCE = 0.5          # ±50 % du parent pour les paramètres numériques sans borne sûre
KNOWN_SESSIONS = frozenset(ALL_SESSIONS)
KNOWN_REGIMES = frozenset(r.value for r in Regime if r is not Regime.NEWS_SHOCK)
TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
KNOWN_TFS = frozenset(TF_ORDER)
NAME_RE = re.compile(r"[^a-z0-9_]+")

SYSTEM_PROMPT = (
    "Tu es un chercheur quantitatif d'un laboratoire de trading algorithmique. Tu PROPOSES des variantes d'une stratégie "
    "existante ; tu ne décides de rien : chaque variante sera validée par du code déterministe puis devra franchir un pipeline "
    "complet (backtest, out-of-sample, walk-forward, Monte Carlo, shadow, revues) avant toute mise en production. "
    "Ta réponse est une interprétation de modèle (MODEL_INTERPRETATION), jamais un fait.\n"
    "Règles :\n"
    "- N'invente aucune donnée ni statistique : appuie-toi uniquement sur les éléments fournis.\n"
    "- base_strategy doit être une clé de screener AUTORISÉE (liste fournie) ou celle du parent.\n"
    "- Les paramètres numériques restent dans les bornes sûres fournies ou à ±50 % de la valeur du parent ; "
    "ne change pas les paramètres non numériques ; ne crée pas de nouveau paramètre.\n"
    "- markets : sous-ensemble des marchés du parent ; sessions, regimes, timeframes : parmi les valeurs autorisées.\n"
    "- Chaque variante doit différer du parent (paramètres ET/OU filtre : session, régime, timeframe).\n"
    "- Réponds UNIQUEMENT par un JSON de la forme exacte :\n"
    '{"variants": [{"name": str, "base_strategy": str, "params": {...}, "markets": [...], "sessions": [...], '
    '"regimes": [...], "timeframes": {"entry": str, "trend": str}, "thesis": str, "invalidation_rules": str}]}\n'
    "Aucun texte hors du JSON. Aucun champ status : le statut est imposé par le système (RESEARCH)."
)


@dataclass
class Rejection:
    parent_id: str
    reason: str
    variant: Any = field(default=None, repr=False)


class StrategyAuthor:
    """Propose des variantes de stratégie, toujours en RESEARCH, jamais autrement."""

    def __init__(self, registry: AgentRegistry, store, llm=None, learning_cfg: Optional[dict] = None, journal=None):
        self.registry = registry
        self.store = store
        self.llm = llm
        self.cfg = learning_cfg or {}
        self.journal = journal
        self.last_rejections: list[Rejection] = []

    # ------------------------------------------------------------------ configuration
    @property
    def max_variants(self) -> int:
        return max(1, int(self.cfg.get("challenger_generation", {}).get("max_new_per_cycle", 3)))

    @property
    def jitter(self) -> float:
        return float(self.cfg.get("challenger_generation", {}).get("parameter_jitter_percent", 20)) / 100

    @staticmethod
    def allowed_base_strategies(registry: AgentRegistry) -> list[str]:
        """Screeners génériques : clés de SCREENERS hors sentinelles et hors stratégies propres (clé = agent_id)."""
        return sorted(k for k in SCREENERS if not k.startswith("__") and k not in registry.agents)

    # ------------------------------------------------------------------ journal
    def _log(self, kind: str, **data) -> None:
        if self.journal is not None:
            self.journal.event(kind, **data)

    def _reject(self, parent: AgentSpec, reason: str, variant: Any = None) -> None:
        self.last_rejections.append(Rejection(parent.agent_id, reason, variant))
        if self.journal is not None:
            self.journal.warn("strategy_author : variante rejetée", parent_id=parent.agent_id, reason=reason,
                              variant_name=variant.get("name") if isinstance(variant, dict) else None)
        if self.store is not None:
            try:
                self.store.agent_event(parent.agent_id, "variant_rejected", {"reason": reason})
            except Exception:  # noqa: BLE001 - la base d'apprentissage ne doit jamais bloquer la recherche
                pass

    # ------------------------------------------------------------------ API
    def propose(self, parent: AgentSpec, context: Optional[dict] = None, n: Optional[int] = None) -> list[AgentSpec]:
        """Variantes créées en RESEARCH pour ``parent`` (au plus ``max_variants``). Liste vide si rien d'acceptable."""
        self.last_rejections = []
        context = dict(context or {})
        limit = min(int(n), self.max_variants) if n else self.max_variants
        if not parent.generates_trades:
            self._reject(parent, "le parent n'est pas un agent générateur")
            return []
        if parent.base_strategy not in SCREENERS and parent.strategy not in SCREENERS:
            self._reject(parent, "screener du parent inconnu")
            return []
        proposals: Optional[list[dict]] = None
        source = "deterministic"
        if self.llm is not None:
            proposals = self._ask_llm(parent, context, limit)
            if proposals is not None:
                source = "llm"
        if proposals is None:
            proposals = self._deterministic(parent, limit)
        out: list[AgentSpec] = []
        for raw in proposals:
            if len(out) >= limit:
                break
            v = self._validate(parent, raw)
            if v is None:
                continue
            out.append(self._create(parent, v, source))
        self._log("strategy_author", parent_id=parent.agent_id, source=source, created=[a.agent_id for a in out],
                  rejected=len(self.last_rejections))
        return out

    # ------------------------------------------------------------------ LLM
    def _ask_llm(self, parent: AgentSpec, context: dict, limit: int) -> Optional[list[dict]]:
        """None si le modèle est indisponible ou sa réponse inexploitable (→ repli déterministe)."""
        try:
            resp = self.llm.complete(ROLE, SYSTEM_PROMPT, self._user_prompt(parent, context, limit), max_tokens=1500,
                                     temperature=0.4, cache_key_extra=f"{parent.agent_id}|{utcnow().date()}")
        except Exception as e:  # noqa: BLE001
            self._log("strategy_author_llm_failed", parent_id=parent.agent_id, error=f"{type(e).__name__}: {e}")
            return None
        if resp is None:
            return None
        data = resp.json() if hasattr(resp, "json") else None
        if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
            self._reject(parent, "réponse LLM inexploitable (JSON attendu {\"variants\": [...]})")
            return None
        return [v for v in data["variants"]]

    def _user_prompt(self, parent: AgentSpec, context: dict, limit: int) -> str:
        stats = {}
        if self.store is not None:
            try:
                st = self.store.agent_stats(parent.agent_id)
                stats = {"sample_size": st.sample_size, "profit_factor": st.profit_factor, "expectancy_r": st.expectancy_r,
                         "max_drawdown_r": st.max_drawdown_r, "by_regime": st.by_regime, "by_session": st.by_session,
                         "degradation_score": getattr(st, "degradation_score", None)}
            except Exception:  # noqa: BLE001
                stats = {"provenance": Provenance.UNAVAILABLE.value}
        payload = {
            "parent": {k: v for k, v in parent.to_dict().items()
                       if k in ("agent_id", "family", "name", "base_strategy", "markets", "sessions", "timeframes", "regimes",
                                "params", "entry_rules", "invalidation_rules", "description")},
            "parent_stats": stats,
            "context": context,
            "allowed": {"base_strategies": self.allowed_base_strategies(self.registry), "sessions": sorted(KNOWN_SESSIONS),
                        "regimes": sorted(KNOWN_REGIMES), "timeframes": TF_ORDER, "markets": list(parent.markets)},
            "safe_bounds": SAFE_BOUNDS, "relative_tolerance": RELATIVE_TOLERANCE, "max_variants": limit,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------ repli déterministe
    def _deterministic(self, parent: AgentSpec, limit: int) -> list[dict]:
        """Jitter des paramètres (comme generate_challengers) ET restriction d'un filtre (session ou régime)."""
        rng = random.Random(f"{CREATED_BY}|{parent.agent_id}|{utcnow().date()}")
        jit = self.jitter
        out = []
        for i in range(limit):
            params = copy.deepcopy(parent.params)
            for k, v in params.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    nv = v * (1 + rng.uniform(-jit, jit))
                    lo, hi = SAFE_BOUNDS.get(k, (None, None))
                    if lo is not None:
                        nv = min(max(nv, lo), hi)
                    params[k] = int(round(nv)) if isinstance(v, int) else round(nv, 4)
            sessions, regimes = list(parent.sessions), list(parent.regimes)
            filt = "none"
            prefer_session = (i % 2 == 0)
            if prefer_session and len(parent.sessions) > 1 or len(parent.regimes) <= 1 < len(parent.sessions):
                sessions = [parent.sessions[i % len(parent.sessions)]]
                filt = f"session={sessions[0]}"
            elif len(parent.regimes) > 1:
                regimes = [parent.regimes[i % len(parent.regimes)]]
                filt = f"regime={regimes[0]}"
            out.append({"name": f"{parent.name}_v{i + 1}", "base_strategy": parent.base_strategy or parent.strategy,
                        "params": params, "markets": list(parent.markets), "sessions": sessions, "regimes": regimes,
                        "timeframes": dict(parent.timeframes),
                        "thesis": f"Variante déterministe du parent {parent.agent_id} : jitter ±{int(jit * 100)} % des paramètres, filtre {filt}",
                        "invalidation_rules": parent.invalidation_rules})
        return out

    # ------------------------------------------------------------------ validation stricte
    def _validate(self, parent: AgentSpec, raw: Any) -> Optional[dict]:  # noqa: C901 - règles explicites
        if not isinstance(raw, dict):
            self._reject(parent, "variante non-objet", raw)
            return None
        name = NAME_RE.sub("_", str(raw.get("name") or f"{parent.name}_var").lower()).strip("_")[:48] or f"{parent.name}_var"
        base = raw.get("base_strategy") or parent.base_strategy
        allowed = set(self.allowed_base_strategies(self.registry)) | {parent.base_strategy, parent.strategy} - {None}
        if not isinstance(base, str) or base not in allowed or base not in SCREENERS:
            self._reject(parent, f"base_strategy inconnue : {base!r}", raw)
            return None
        # paramètres : uniquement ceux du parent, numériques bornés, non numériques inchangés
        params_in = raw.get("params", {})
        if params_in is None:
            params_in = {}
        if not isinstance(params_in, dict):
            self._reject(parent, "params doit être un objet", raw)
            return None
        params = copy.deepcopy(parent.params)
        for k, v in params_in.items():
            if k not in parent.params:
                self._reject(parent, f"paramètre inconnu du parent : {k}", raw)
                return None
            pv = parent.params[k]
            if isinstance(pv, bool) or not isinstance(pv, (int, float)):
                if v != pv:
                    self._reject(parent, f"paramètre non numérique modifié : {k}", raw)
                    return None
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                self._reject(parent, f"paramètre {k} non numérique : {v!r}", raw)
                return None
            v = float(v)
            if v != v or v in (float("inf"), float("-inf")):
                self._reject(parent, f"paramètre {k} non fini", raw)
                return None
            if k in SAFE_BOUNDS:
                lo, hi = SAFE_BOUNDS[k]
                if not (lo <= v <= hi):
                    self._reject(parent, f"paramètre {k}={v} hors bornes sûres [{lo}, {hi}]", raw)
                    return None
            else:
                tol = abs(pv) * RELATIVE_TOLERANCE
                if abs(v - pv) > tol + 1e-12:
                    self._reject(parent, f"paramètre {k}={v} hors ±{int(RELATIVE_TOLERANCE * 100)} % du parent ({pv})", raw)
                    return None
            params[k] = int(round(v)) if isinstance(pv, int) else round(v, 4)
        if "rsi_lo" in params and "rsi_hi" in params and params["rsi_lo"] >= params["rsi_hi"]:
            self._reject(parent, "rsi_lo doit être < rsi_hi", raw)
            return None
        # marchés : sous-ensemble non vide de ceux du parent
        markets = raw.get("markets") or list(parent.markets)
        if not isinstance(markets, list) or not markets or any(m not in parent.markets for m in markets):
            self._reject(parent, f"markets hors de ceux du parent : {markets!r}", raw)
            return None
        sessions = raw.get("sessions") or list(parent.sessions)
        if not isinstance(sessions, list) or not sessions or any(s not in KNOWN_SESSIONS for s in sessions):
            self._reject(parent, f"sessions inconnues : {sessions!r}", raw)
            return None
        regimes = raw.get("regimes") or list(parent.regimes)
        if not isinstance(regimes, list) or not regimes or any(r not in KNOWN_REGIMES for r in regimes):
            self._reject(parent, f"regimes inconnus : {regimes!r}", raw)
            return None
        tfs = raw.get("timeframes") or dict(parent.timeframes)
        if not isinstance(tfs, dict):
            self._reject(parent, "timeframes doit être un objet", raw)
            return None
        entry, trend = tfs.get("entry", parent.timeframes.get("entry")), tfs.get("trend", parent.timeframes.get("trend"))
        if entry not in KNOWN_TFS or trend not in KNOWN_TFS or TF_ORDER.index(entry) >= TF_ORDER.index(trend):
            self._reject(parent, f"timeframes invalides : {tfs!r}", raw)
            return None
        thesis = str(raw.get("thesis") or "").strip()
        if not thesis:
            self._reject(parent, "thesis manquante", raw)
            return None
        inval = str(raw.get("invalidation_rules") or parent.invalidation_rules or "").strip()
        unchanged = (params == parent.params and sorted(markets) == sorted(parent.markets) and sorted(sessions) == sorted(parent.sessions)
                     and sorted(regimes) == sorted(parent.regimes) and entry == parent.timeframes.get("entry")
                     and trend == parent.timeframes.get("trend") and base == parent.base_strategy)
        if unchanged:
            self._reject(parent, "variante identique au parent", raw)
            return None
        return {"name": name, "base_strategy": base, "params": params, "markets": list(dict.fromkeys(markets)),
                "sessions": list(dict.fromkeys(sessions)), "regimes": list(dict.fromkeys(regimes)),
                "timeframes": {"entry": entry, "trend": trend}, "thesis": thesis[:1000], "invalidation_rules": inval[:500]}

    # ------------------------------------------------------------------ création (toujours RESEARCH)
    def _create(self, parent: AgentSpec, v: dict, source: str) -> AgentSpec:
        with self.registry._lock:
            cid = self.registry.next_challenger_id(parent)
            prov = Provenance.MODEL_INTERPRETATION.value if source == "llm" else Provenance.CALCULATED.value
            # même screener générique que le parent → la stratégie propre du parent reste applicable ; sinon screener générique
            strategy = parent.strategy if v["base_strategy"] == parent.base_strategy else v["base_strategy"]
            spec = AgentSpec(
                agent_id=cid, family=parent.family, name=v["name"], strategy=strategy, markets=v["markets"],
                sessions=v["sessions"], timeframes=v["timeframes"], regimes=v["regimes"], params=v["params"],
                status=AgentStatus.RESEARCH.value, version="1.0", model_tier_role=parent.model_tier_role,
                news_sensitive=parent.news_sensitive, cost_budget_usd=parent.cost_budget_usd,
                description=f"[{CREATED_BY}/{source}/{prov}] parent={parent.agent_id} : {v['thesis']}",
                entry_rules=(f"{parent.entry_rules} | " if parent.entry_rules else "") + f"thèse : {v['thesis']}",
                invalidation_rules=v["invalidation_rules"], sl_logic=parent.sl_logic, tp_logic=parent.tp_logic,
                filters=list(parent.filters), parent_id=parent.agent_id, created_by=CREATED_BY, base_strategy=v["base_strategy"],
            )
            assert spec.status == AgentStatus.RESEARCH.value
            self.registry.add(spec)
        if self.store is not None:
            try:
                self.store.agent_event(cid, "challenger_created", {"parent": parent.agent_id, "params": v["params"], "source": CREATED_BY,
                                                                   "provenance": prov, "sessions": v["sessions"], "regimes": v["regimes"]})
            except Exception:  # noqa: BLE001
                pass
        self._log("variant_created", agent_id=cid, parent_id=parent.agent_id, source=source, provenance=prov, name=v["name"])
        return spec
