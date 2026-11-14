"""Fil de recherche bloqué (2026-09-28) : les propositions X01–X06 de l'optimiseur ont attendu 7 h parce que le fil de
recherche de l'orchestrateur, seul à les appliquer, ne rendait plus la main sans laisser de trace.

- les files (statuts, propositions) sont appliquées dans la boucle principale ;
- un fil toujours en cours à l'échéance suivante est journalisé avec sa pile d'appels."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tradinglab.orchestration.orchestrator import Orchestrator


def _orch(tmp_path, external=True):
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(state_dir=tmp_path, learning={"research_external": external})
    o.journal = SimpleNamespace(events=[], warns=[])
    o.journal.event = lambda kind, **k: o.journal.events.append((kind, k))
    o.journal.warn = lambda msg, **k: o.journal.warns.append((msg, k))
    o._research_thread = None
    o._research_started_at = None
    o._research_lock = threading.Lock()
    return o


def test_fil_bloque_journalise_sa_pile(tmp_path):
    o = _orch(tmp_path)
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, name="research", daemon=True)
    t.start()
    time.sleep(0.3)                                          # laisser le fil atteindre son attente
    o._research_thread, o._research_started_at = t, 0.0
    try:
        o._start_research_thread()
        assert o._research_thread is t                      # pas de second fil
        assert len(o.journal.warns) == 1
        msg, k = o.journal.warns[0]
        assert msg == "cycle de recherche toujours en cours" and k["depuis_sec"] > 1000
        assert "wait" in k["pile"]                           # la pile montre où il attend
    finally:
        stop.set()


def test_files_appliquees_dans_la_boucle_principale(tmp_path):
    o = _orch(tmp_path)
    appels = []
    o._apply_status_requests = lambda: appels.append("statuts") or 0
    o._apply_agent_proposals = lambda: appels.append("propositions") or 0
    o._apply_research_files()
    assert appels == ["statuts", "propositions"]
    o2 = _orch(tmp_path, external=False)
    o2._apply_status_requests = lambda: appels.append("non")
    o2._apply_research_files()
    assert appels == ["statuts", "propositions"]            # mode interne : rien à appliquer
