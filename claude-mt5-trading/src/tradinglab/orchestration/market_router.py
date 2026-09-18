"""Market Router : régime → agents compatibles → screeners Python → candidats classés.

Entonnoir : 100+ agents → N compatibles régime/session/marché → screeners → candidats
→ enrichissement (stats historiques, news, corrélation) → classement → revue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..agents.registry import AgentRegistry, AgentSpec
from ..agents.screeners import run_screener
from ..core.types import AgentStatus, TradeCandidate
from ..market_data.feed import MarketSnapshot
from ..mt5.symbols import root_of


@dataclass
class RoutingReport:
    symbols: int = 0
    agents_registered: int = 0
    agents_activated: dict[str, list[str]] = field(default_factory=dict)   # symbol -> agent_ids
    raw_candidates: int = 0
    deduped_candidates: int = 0
    regimes: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"symbols": self.symbols, "agents_registered": self.agents_registered,
                "agents_activated": {k: len(v) for k, v in self.agents_activated.items()},
                "raw_candidates": self.raw_candidates, "deduped_candidates": self.deduped_candidates,
                "regimes": self.regimes, "skipped": self.skipped}


class MarketRouter:
    def __init__(self, registry: AgentRegistry, groups: dict[str, list[str]] | None = None,
                 statuses: tuple[str, ...] = (AgentStatus.LIVE.value,)):
        self.registry = registry
        self.statuses = statuses
        self.group_of: dict[str, str] = {}
        for g, syms in (groups or {}).items():
            for s in syms:
                self.group_of[root_of(s)] = g

    def route(self, snapshots: dict[str, MarketSnapshot], statuses: Optional[tuple[str, ...]] = None) -> tuple[list[TradeCandidate], RoutingReport]:
        rep = RoutingReport(symbols=len(snapshots), agents_registered=len(self.registry))
        cands: list[TradeCandidate] = []
        for sym, snap in snapshots.items():
            rep.regimes[sym] = snap.regime.regime.value
            if snap.data_quality != "OK":
                rep.skipped[sym] = snap.data_quality
                continue
            if snap.regime.regime.value == "NEWS_SHOCK":
                rep.skipped[sym] = "NEWS_SHOCK"
                continue
            root = snap.spec.root or root_of(sym)
            agents = self.registry.active_for(snap.regime.regime.value, snap.session.value, snap.spec.asset_class, root,
                                              self.group_of.get(root), statuses or self.statuses)
            rep.agents_activated[sym] = [a.agent_id for a in agents]
            for a in agents:
                c = run_screener(a, snap)
                if c is not None:
                    cands.append(c)
        rep.raw_candidates = len(cands)
        cands = self.dedupe(cands)
        rep.deduped_candidates = len(cands)
        return cands, rep

    @staticmethod
    def dedupe(cands: list[TradeCandidate]) -> list[TradeCandidate]:
        """Une seule idée par (symbole, sens) : on garde le meilleur score et on note les agents concordants."""
        best: dict[tuple[str, str], TradeCandidate] = {}
        for c in sorted(cands, key=lambda x: x.setup_score, reverse=True):
            k = (c.symbol, c.side.value)
            if k not in best:
                best[k] = c
                c.review["concurring_agents"] = [c.agent_id]
            else:
                best[k].review.setdefault("concurring_agents", []).append(c.agent_id)
        out = list(best.values())
        for c in out:
            n = len(c.review.get("concurring_agents", []))
            if n > 1:
                c.setup_score = min(100.0, c.setup_score + min(8.0, 2.0 * (n - 1)))
                c.arguments_for.append(f"{n} agents concordants")
        # idées opposées sur le même symbole : conflit → on ne garde que la meilleure, pénalisée
        by_sym: dict[str, list[TradeCandidate]] = {}
        for c in out:
            by_sym.setdefault(c.symbol, []).append(c)
        final = []
        for sym, lst in by_sym.items():
            if len(lst) > 1:
                lst.sort(key=lambda x: x.setup_score, reverse=True)
                lst[0].setup_score = max(0.0, lst[0].setup_score - 10)
                lst[0].arguments_against.append("signal opposé d'autres agents sur le même symbole")
            final.append(lst[0])
        return sorted(final, key=lambda x: x.setup_score, reverse=True)
