"""Client LLM : appels Claude via le SDK anthropic, avec cache, coût mesuré et repli déterministe.

Le client ne connaît ni le broker ni l'exécution : il renvoie du texte/JSON marqué
MODEL_INTERPRETATION. Si aucun modèle n'est disponible, `complete` renvoie None
et l'appelant doit dégrader proprement (jamais de donnée inventée).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..core.state import SystemState
from ..core.types import Provenance, utcnow
from .router import ModelRouter, RouteDecision

# prix indicatifs USD / million de tokens (entrée, sortie) — utilisés pour la mesure de coût ; à ajuster via models.yaml
DEFAULT_PRICES = {"claude-fable-5-1": (15.0, 75.0), "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (3.0, 15.0),
                  "claude-haiku-4-5-20251001": (1.0, 5.0)}


@dataclass
class LLMResponse:
    text: str
    model: str
    tier: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool = False
    provenance: Provenance = Provenance.MODEL_INTERPRETATION

    def json(self) -> Optional[dict]:
        t = self.text.strip()
        if "```" in t:
            t = t.split("```")[1]
            t = t[4:] if t.startswith("json") else t
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            s, e = t.find("{"), t.rfind("}")
            if s >= 0 and e > s:
                try:
                    return json.loads(t[s:e + 1])
                except json.JSONDecodeError:
                    return None
        return None


class LLMClient:
    def __init__(self, router: ModelRouter, state: SystemState, cache_ttl_sec: int = 240, prices: dict | None = None,
                 sdk_client: Any = None, journal=None):
        self.router = router
        self.state = state
        self.cache_ttl = cache_ttl_sec
        self.prices = prices or DEFAULT_PRICES
        self._cache: dict[str, tuple[float, LLMResponse]] = {}
        self._sdk = sdk_client
        self.journal = journal
        self.calls = 0

    def _client(self):
        if self._sdk is None:
            import anthropic  # import paresseux
            self._sdk = anthropic.Anthropic()
        return self._sdk

    def _cost(self, model: str, inp: int, out: int) -> float:
        pi, po = self.prices.get(model, (3.0, 15.0))
        return inp / 1e6 * pi + out / 1e6 * po

    def complete(self, role: str, system: str, user: str, max_tokens: int = 800, financial_importance: str = "normal",
                 temperature: float = 0.2, cache_key_extra: str = "") -> Optional[LLMResponse]:
        decision: RouteDecision = self.router.route(role, self.state, financial_importance)
        if not decision.use_llm:
            if self.journal:
                self.journal.event("llm_skipped", role=role, reason=decision.reason)
            return None
        key = hashlib.sha256(f"{decision.model}|{system}|{user}|{cache_key_extra}".encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_ttl:
            r = hit[1]
            return LLMResponse(r.text, r.model, r.tier, r.input_tokens, r.output_tokens, 0.0, cached=True)
        try:
            msg = self._client().messages.create(model=decision.model, max_tokens=max_tokens, temperature=temperature,
                                                 system=system, messages=[{"role": "user", "content": user}])
            text = "".join(getattr(b, "text", "") for b in msg.content)
            inp, out = int(msg.usage.input_tokens), int(msg.usage.output_tokens)
        except Exception as e:  # noqa: BLE001
            if self.journal:
                self.journal.warn("appel LLM échoué", role=role, model=decision.model, error=type(e).__name__)
            return None
        cost = self._cost(decision.model, inp, out)
        self.router.record_call(self.state, decision.tier, cost)
        self.calls += 1
        resp = LLMResponse(text, decision.model, decision.tier, inp, out, cost)
        self._cache[key] = (time.time(), resp)
        if self.journal:
            self.journal.event("llm_call", role=role, model=decision.model, tier=decision.tier, input_tokens=inp,
                               output_tokens=out, cost_usd=round(cost, 5))
        return resp
