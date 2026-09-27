"""Recherche dans un processus séparé (2026-09-27) : le registre n'a qu'un écrivain, l'orchestrateur."""
from __future__ import annotations

import json
from types import SimpleNamespace

from tradinglab.core.types import AgentStatus
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.research.worker import RegistreLectureSeule


def test_le_registre_du_processus_separe_n_ecrit_que_des_demandes(tmp_path):
    statuts = tmp_path / "agent_status.json"
    statuts.write_text(json.dumps({"status": {"E05": "LIVE"}, "challengers": []}), encoding="utf-8")
    avant = statuts.read_text(encoding="utf-8")
    reg = RegistreLectureSeule(statuts, tmp_path / "state" / "agent_status_requests.jsonl")
    reg.set_status("B01", AgentStatus.SHADOW, "test")
    reg.save()
    assert statuts.read_text(encoding="utf-8") == avant                      # jamais réécrit
    lignes = (tmp_path / "state" / "agent_status_requests.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lignes[0])["agent_id"] == "B01" and json.loads(lignes[0])["status"] == "SHADOW"
    assert reg.get("B01").status == "SHADOW"                                  # vue locale mise à jour


def test_l_orchestrateur_applique_les_demandes_puis_vide_la_file(tmp_path):
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(state_dir=tmp_path)
    appliques = []
    o.registry = SimpleNamespace(agents={"B01": object(), "CH101": object()},
                                 set_status=lambda aid, st, reason="": appliques.append((aid, st.value)))
    o.journal = SimpleNamespace(event=lambda *a, **k: None, warn=lambda *a, **k: None)
    f = tmp_path / "agent_status_requests.jsonl"
    f.write_text("\n".join([json.dumps({"agent_id": "B01", "status": "SHADOW"}),
                             json.dumps({"agent_id": "INCONNU", "status": "LIVE"}),
                             "pas du json",
                             json.dumps({"agent_id": "CH101", "status": "BACKTEST"})]), encoding="utf-8")
    assert o._apply_status_requests() == 2
    assert appliques == [("B01", "SHADOW"), ("CH101", "BACKTEST")]
    assert not f.exists() and not f.with_suffix(".processing").exists()
    assert o._apply_status_requests() == 0


def test_config_reelle_recherche_externe(settings):
    assert settings.learning["research_external"] is True
