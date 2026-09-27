"""Processus de recherche séparé (2026-09-27, plan pro : « sortir la recherche du processus de trading »).

Les backtests, hors échantillon, walk-forward et Monte Carlo des agents non-LIVE tournaient dans un thread de
l'orchestrateur : ils partageaient le GIL avec la boucle de trading et se limitaient à 5 agents par demi-heure. Ici
ils tournent dans leur propre processus, en priorité basse (start_all), avec leur propre connexion MT5 en lecture.

Le registre n'a qu'un seul écrivain, l'orchestrateur : ce processus ne modifie jamais `data/agent_status.json`. Il
dépose ses changements de statut (RESEARCH → BACKTEST → SHADOW → CANDIDATE → LIVE, rollback) dans
`state/agent_status_requests.jsonl`, que l'orchestrateur applique à son cycle de recherche.

Usage : python -m tradinglab.research.worker [--once]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..agents.registry import AgentRegistry
from ..core.types import AgentStatus


class RegistreLectureSeule(AgentRegistry):
    """Registre chargé depuis le fichier de statuts, sans jamais l'écrire : les changements deviennent des DEMANDES."""

    def __init__(self, status_file: Path, requests_file: Path, journal=None) -> None:
        super().__init__(status_file=status_file, journal=journal)
        self.requests_file = requests_file
        self.demandes: list[dict] = []

    def save(self) -> None:  # jamais d'écriture du fichier de statuts depuis ce processus
        return None

    def set_status(self, agent_id: str, status: AgentStatus, reason: str = "") -> None:
        with self._lock:
            a = self.agents[agent_id]
            a.status = status.value                      # vue locale, pour la suite du pipeline dans ce passage
            d = {"agent_id": agent_id, "status": status.value, "reason": reason,
                 "ts": datetime.now(timezone.utc).isoformat()}
            self.demandes.append(d)
            self.requests_file.parent.mkdir(parents=True, exist_ok=True)
            with self.requests_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")


def run_once(settings, broker, journal) -> list[str]:
    """Un passage : recharge les statuts, fait avancer les agents de la file. Renvoie les agents traités."""
    from ..learning.store import LearningStore
    from ..mt5.symbols import resolve_symbols
    from .pipeline import ResearchPipeline, research_queue

    reg = RegistreLectureSeule(settings.data_dir / "agent_status.json", settings.state_dir / "agent_status_requests.jsonl", journal)
    voulus = [x for grp, lst in settings.markets.items() if grp != "asset_class_rules" and isinstance(lst, list) for x in lst]
    symbols = [r for r in resolve_symbols(voulus, broker.symbols()).values() if r]
    specs = {sym: broker.symbol_info(sym) for sym in symbols}
    specs = {k: v for k, v in specs.items() if v}
    pipe = ResearchPipeline(reg, LearningStore(settings.data_dir / "learning.db"), settings.learning, settings.backtest,
                            lambda sym, tf, n: broker.rates(sym, tf, n), specs, settings.data_dir / "research", journal)
    cfg = settings.learning or {}
    faits = []
    for aid in research_queue(reg, pipe, cfg, datetime.now(timezone.utc), "research_worker_agents_per_cycle", 20):
        try:
            pipe.advance(aid)
            faits.append(aid)
        except Exception as e:  # noqa: BLE001 - un agent en erreur ne bloque pas les autres
            journal.warn("recherche (processus séparé) : étape échouée", agent_id=aid, error=f"{type(e).__name__}: {e}")
    journal.event("research_worker_pass", agents=faits, demandes=len(reg.demandes))
    return faits


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - processus
    from ..core.config import load_dotenv, load_settings
    from ..core.journal import Journal
    from ..mt5.mock_adapter import make_broker

    ap = argparse.ArgumentParser(description="Processus de recherche séparé (backtests des agents non-LIVE)")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    s = load_settings()
    load_dotenv(s.home / ".env")
    journal = Journal(s.logs_dir, s.system.get("timezone_local", "UTC"), component="research")
    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), s)
    intervalle = float(s.scheduler.get("research_interval_sec", 1800))
    while True:
        if not broker.is_connected() and not broker.connect():
            journal.warn("recherche (processus séparé) : terminal indisponible, nouvel essai plus tard")
        else:
            try:
                run_once(s, broker, journal)
            except Exception as e:  # noqa: BLE001 - le processus ne meurt jamais sur un passage raté
                journal.warn("recherche (processus séparé) : passage échoué", error=f"{type(e).__name__}: {e}")
        if args.once:
            return 0
        time.sleep(intervalle)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
