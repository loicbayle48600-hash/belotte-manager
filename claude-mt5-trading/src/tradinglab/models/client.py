"""Client LLM : appels Claude via le SDK anthropic, avec cache, coût mesuré et repli déterministe.

Le client ne connaît ni le broker ni l'exécution : il renvoie du texte/JSON marqué
MODEL_INTERPRETATION. Si aucun modèle n'est disponible, `complete` renvoie None
et l'appelant doit dégrader proprement (jamais de donnée inventée).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..core.state import SystemState
from ..core.types import Provenance, utcnow
from .router import ModelRouter, RouteDecision

# prix indicatifs USD / million de tokens (entrée, sortie) — tarifs API Anthropic (référence vérifiée) ;
# surcharge possible via `prices` (models.yaml) : LLMClient(prices={...}) fusionne avec ces valeurs
#: barème local $/M tokens (entrée, sortie) — sert à estimer le coût quand l'API ne le renvoie pas.
#: Opus 5.5 et Fable 5.1 ajoutés le 2026-09-22 au tarif de leur génération : à corriger si la grille
#: publique diffère (le plafond `daily_budget_usd` protège de toute façon d'une dérive).
DEFAULT_PRICES = {"claude-fable-5": (10.0, 50.0), "claude-fable-5-1": (10.0, 50.0),
                  "claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
                  "claude-haiku-4-5-20251001": (1.0, 5.0), "claude-haiku-4-5": (1.0, 5.0)}
DEFAULT_REQUEST_TIMEOUT_SEC = 30.0   # un appel LLM bloqué ne doit jamais geler la boucle live (heartbeat 45 s)
MAX_CACHE_ENTRIES = 500
# Les modèles Claude 5 réfléchissent par défaut (« adaptive thinking ») : constaté le 2026-09-21 en production,
# max_tokens=500 partait intégralement dans un bloc `thinking` et le texte revenait VIDE (stop max_tokens),
# 100 % des revues tombaient en repli déterministe après 0,07 $ et ~35 s par candidat. Ces rôles rendent un
# petit JSON en quelques secondes : la réflexion étendue est désactivée explicitement.
# Exception Fable 5 (constaté le 2026-09-21, 400 « "thinking.type.disabled" is not supported for this model ») :
# il n'accepte que "adaptive", l'effort se règle via output_config.effort — "low" validé en production (end_turn).
THINKING = {"type": "disabled"}


ADAPTIVE_KWARGS = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
#: modèles connus pour refuser `thinking: disabled` (constaté : Fable 5 le 2026-09-21, Opus 5.5 le 2026-09-22).
#: La liste s'auto-complète au premier refus d'un modèle inconnu : aucune maintenance au prochain modèle.
ADAPTIVE_ONLY: set[str] = {"claude-fable-5", "claude-fable-5-1", "claude-opus-5-5"}


def thinking_kwargs(model: str) -> dict:
    if model in ADAPTIVE_ONLY or "fable" in model:
        return dict(ADAPTIVE_KWARGS)
    return {"thinking": THINKING}


def _is_disabled_thinking_refusal(exc: Exception) -> bool:
    txt = str(exc)
    return "thinking.type.disabled" in txt and "not supported" in txt


#: messages d'erreur signalant que la CLÉ API n'a plus de crédit / de quota (facturation, pas technique).
CREDIT_MARKERS = ("credit balance is too low", "credit balance too low", "insufficient_quota",
                  "insufficient credit", "billing", "quota exceeded", "payment required")


#: messages signalant que l'ABONNEMENT (terminal Claude) a atteint sa limite d'usage — distinct d'une
#: panne de crédit sur une clé API : ici aucun repli n'existe, il faut attendre la recharge du quota.
SUBSCRIPTION_LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "quota exceeded",
                              "you've reached your", "try again later", "resets at")
#: durée de mise en veille des appels LLM après une limite d'abonnement (2026-09-23, demande utilisateur :
#: « si on atteint le quota de mon abonnement, mets [un repli] en place »). Marteler le CLI n'accélère rien
#: et sature le journal ; le trading continue sur les règles déterministes, qui n'ont jamais besoin de LLM.
SUBSCRIPTION_COOLDOWN_SEC = 1800.0


def _is_subscription_limited(exc: Exception) -> bool:
    txt = str(exc).lower()
    return any(m in txt for m in SUBSCRIPTION_LIMIT_MARKERS)


def _is_credit_exhausted(exc: Exception) -> bool:
    """Vrai si l'appel a échoué faute de crédit sur la clé API (et non pour une raison technique).

    2026-09-22, demande utilisateur : « quand il n'y a plus de crédit API, continuer normalement dans un
    terminal Claude ». On ne bascule que sur ce motif précis — un timeout ou une panne réseau doit rester
    un repli déterministe, pas un changement de backend silencieux.
    """
    txt = str(exc).lower()
    if any(m in txt for m in CREDIT_MARKERS):
        return True
    status = getattr(exc, "status_code", None)
    return status == 402


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
                 sdk_client: Any = None, journal=None, request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
                 max_retries: int = 1, fallback_factory=None):
        self.router = router
        self.state = state
        self.cache_ttl = cache_ttl_sec
        self.prices = {**DEFAULT_PRICES, **{k: tuple(v) for k, v in (prices or {}).items()}}
        self.request_timeout = float(request_timeout_sec)
        self.max_retries = int(max_retries)
        self._cache: dict[str, tuple[float, LLMResponse]] = {}
        self._sdk = sdk_client
        self.journal = journal
        self.calls = 0
        # la revue adversariale lance bull/bear/devil en parallèle : cache et compteurs de budget sont partagés
        self._lock = threading.Lock()
        # backend de secours (terminal Claude) construit à la demande quand la clé API n'a plus de crédit
        self.fallback_factory = fallback_factory
        self.using_fallback = False
        self.paused_until = 0.0        # horodatage (time.time) jusqu'auquel les appels LLM sont suspendus
        self._skip_seen: dict = {}     # (rôle, nature de la raison) → dernier journal (llm_skipped dédupliqué)

    def _client(self):
        if self._sdk is None:
            import anthropic  # import paresseux
            # timeout borné (secondes) et un seul retry : mur d'horloge max ≈ timeout × 2 au lieu de 600 s × 3
            self._sdk = anthropic.Anthropic(timeout=self.request_timeout, max_retries=self.max_retries)
        return self._sdk

    def _switch_to_fallback(self, exc: Exception) -> bool:
        """Remplace le SDK par le backend de secours. Une seule bascule, jamais de retour arrière
        automatique (le crédit ne revient pas en cours de session) ; échec de construction → False."""
        if self.using_fallback or self.fallback_factory is None:
            return False
        try:
            backend = self.fallback_factory()
        except Exception as e:  # noqa: BLE001
            backend = None
            if self.journal:
                self.journal.warn("backend de secours indisponible", error=type(e).__name__)
        if backend is None:
            return False
        with self._lock:
            self._sdk = backend
            self.using_fallback = True
        if self.journal:
            self.journal.warn("crédit API épuisé : bascule sur le terminal Claude (abonnement)",
                              error=str(exc)[:160])
            self.journal.event("models", message="backend LLM basculé", backend="claude_code")
        return True

    #: un même motif de saut (rôle + raison sans chiffres) n'est journalisé qu'une fois par période
    SKIP_LOG_EVERY_SEC = 600.0

    def _skip(self, role: str, reason: str) -> None:
        """Journalise `llm_skipped` une fois par (rôle, nature de la raison) et par 10 min (2026-09-27 : 2 900 lignes
        identiques par jour). La décision, elle, est prise à chaque appel."""
        if not self.journal:
            return
        import re
        nature = re.sub(r"\d+(?:[.,]\d+)?", "#", reason)
        now = time.time()
        with self._lock:
            last = self._skip_seen.get((role, nature), 0.0)
            if now - last < self.SKIP_LOG_EVERY_SEC:
                return
            self._skip_seen[(role, nature)] = now
            if len(self._skip_seen) > 500:
                self._skip_seen.clear()
        self.journal.event("llm_skipped", role=role, reason=reason)

    def _prune_cache(self) -> None:
        """Purge les entrées périmées (TTL) et borne la taille : le processus tourne des jours, le cache ne doit pas croître sans limite."""
        now = time.time()
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.cache_ttl}
        while len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))

    def _cost(self, model: str, inp: int, out: int) -> float:
        pi, po = self.prices.get(model, (3.0, 15.0))
        return inp / 1e6 * pi + out / 1e6 * po

    def complete(self, role: str, system: str, user: str, max_tokens: int = 800, financial_importance: str = "normal",
                 cache_key_extra: str = "", cache_key: str | None = None) -> Optional[LLMResponse]:
        """`cache_key` : identité déclarée par l'appelant (ex. symbole|sens|agent|barre). Sans elle, la clé est
        le hachage du prompt complet — inutile quand le prompt embarque un id ou une date générés à chaque cycle."""
        with self._lock:
            pause = self.paused_until - time.time()
        if pause > 0:
            self._skip(role, f"quota abonnement atteint : reprise dans {pause / 60:.0f} min")
            return None
        with self._lock:
            decision: RouteDecision = self.router.route(role, self.state, financial_importance)
            fresh_hit = None
            if decision.use_llm:
                ident = f"{role}|{cache_key}" if cache_key else f"{system}|{user}|{cache_key_extra}"
                key = hashlib.sha256(f"{decision.model}|{ident}".encode()).hexdigest()
                hit = self._cache.get(key)
                if hit and time.time() - hit[0] < self.cache_ttl:
                    fresh_hit = hit[1]
                    # un succès de cache était invisible (ni journal ni compteur) : impossible de savoir
                    # combien d'appels il évitait réellement — compté ici (le routeur remet à zéro avec le jour)
                    self.state.model_budget.cache_hits_day += 1
                else:
                    # réservation du créneau sous le même verrou que le routage : le quota horaire est
                    # strictement respecté même avec N appels API en vol (le coût est ajouté au retour)
                    self.router.reserve(self.state, decision.tier)
        if not decision.use_llm:
            self._skip(role, decision.reason)
            return None
        if fresh_hit is not None:
            r = fresh_hit
            return LLMResponse(r.text, r.model, r.tier, r.input_tokens, r.output_tokens, 0.0, cached=True)
        try:
            # temperature est refusé (400) par Fable 5 / Opus 5 / Sonnet 5 : jamais envoyé.
            try:
                msg = self._client().messages.create(model=decision.model, max_tokens=max_tokens,
                                                     system=system, messages=[{"role": "user", "content": user}],
                                                     **thinking_kwargs(decision.model))
            except Exception as first:  # noqa: BLE001
                # plus de crédit sur la clé : on bascule sur le terminal Claude (abonnement) et on réessaie
                if _is_credit_exhausted(first) and self._switch_to_fallback(first):
                    msg = self._client().messages.create(model=decision.model, max_tokens=max_tokens,
                                                         system=system, messages=[{"role": "user", "content": user}],
                                                         **thinking_kwargs(decision.model))
                    return self._finish(msg, role, decision, max_tokens, key)
                # modèle qui n'accepte que la réflexion adaptive : on l'apprend et on réessaie UNE fois
                if not _is_disabled_thinking_refusal(first):
                    raise
                ADAPTIVE_ONLY.add(decision.model)
                if self.journal:
                    self.journal.event("models", message="réflexion adaptive imposée par le modèle",
                                       model=decision.model)
                msg = self._client().messages.create(model=decision.model, max_tokens=max_tokens,
                                                     system=system, messages=[{"role": "user", "content": user}],
                                                     **ADAPTIVE_KWARGS)
            text = "".join(getattr(b, "text", "") for b in msg.content)
            inp, out = int(msg.usage.input_tokens), int(msg.usage.output_tokens)
            stop = str(getattr(msg, "stop_reason", "") or "")
        except Exception as e:  # noqa: BLE001
            if _is_subscription_limited(e):
                # limite d'usage de l'abonnement : inutile de réessayer avant la recharge du quota.
                with self._lock:
                    first = self.paused_until <= time.time()
                    self.paused_until = time.time() + SUBSCRIPTION_COOLDOWN_SEC
                if self.journal and first:
                    self.journal.warn("quota abonnement atteint : revues LLM suspendues, trading poursuivi "
                                      "sur les regles deterministes",
                                      minutes=int(SUBSCRIPTION_COOLDOWN_SEC / 60), detail=str(e)[:160])
                    self.journal.event("models", message="appels LLM suspendus (quota abonnement)",
                                       cooldown_sec=SUBSCRIPTION_COOLDOWN_SEC)
                return None
            # timeout / erreur SDK : repli déterministe journalisé comme llm_skipped (l'appelant dégrade proprement)
            if self.journal:
                self.journal.warn("appel LLM échoué", role=role, model=decision.model, error=type(e).__name__)
                self._skip(role, f"erreur appel: {type(e).__name__}")
            return None
        return self._finish(msg, role, decision, max_tokens, key)

    def _finish(self, msg, role: str, decision: RouteDecision, max_tokens: int, key: str) -> LLMResponse:
        text = "".join(getattr(b, "text", "") for b in msg.content)
        inp, out = int(msg.usage.input_tokens), int(msg.usage.output_tokens)
        stop = str(getattr(msg, "stop_reason", "") or "")
        # le backend Claude Code renvoie le coût calculé par le harnais ; sinon barème local
        reported = getattr(msg, "cost_usd", None)
        cost = float(reported) if reported is not None else self._cost(decision.model, inp, out)
        resp = LLMResponse(text, decision.model, decision.tier, inp, out, cost)
        with self._lock:
            self.router.record_cost(self.state, cost)
            self.calls += 1
            self._prune_cache()
            self._cache[key] = (time.time(), resp)
        if self.journal:
            self.journal.event("llm_call", role=role, model=decision.model, tier=decision.tier, input_tokens=inp,
                               output_tokens=out, cost_usd=round(cost, 5), stop_reason=stop)
            if stop == "max_tokens":
                # réponse tronquée = JSON illisible = argent dépensé pour rien : visible dans le journal
                self.journal.warn("réponse LLM tronquée (max_tokens)", role=role, model=decision.model, max_tokens=max_tokens)
        return resp
