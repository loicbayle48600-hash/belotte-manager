"""2026-10-01, décision utilisateur (« 4 ok ») : moins de redémarrages.
- les réglages config/{risk, strategies, system, prop_firms}.yaml sont relus à chaud quand ils changent (fichier stable
  sur deux cycles), un fichier illisible est refusé (ancienne configuration conservée) ;
- les demandes de statut d'agent sont appliquées dès leur dépôt, plus seulement au cycle de recherche."""
from __future__ import annotations

import json
import os
import time

from test_review_market_agents_orchestration import make_orch


def _toucher(path, texte):
    path.write_text(texte, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))      # mtime franchement différente


def test_risque_et_gestion_relus_sans_redemarrer(settings, broker):
    o = make_orch(settings, broker)
    o._recharger_config_si_change()                                       # mémorise l'état de départ
    f = settings.home / "config" / "risk.yaml"
    t = f.read_text(encoding="utf-8")
    assert "risk_per_trade_percent: 0.125" in t and "tp1_r: 1.5" in t
    _toucher(f, t.replace("risk_per_trade_percent: 0.125", "risk_per_trade_percent: 0.2").replace("tp1_r: 1.5", "tp1_r: 1.4"))
    assert not o._recharger_config_si_change(), "premier passage : on attend que le fichier soit stable"
    assert o._recharger_config_si_change()
    assert o.risk.limits.risk_per_trade_percent == 0.2 and o.gate.risk is o.risk and o.pm.cfg.tp1_r == 1.4
    assert o.s.risk["risk_per_trade_percent"] == 0.2
    assert not o._recharger_config_si_change(), "rien de nouveau"


def test_fichier_illisible_ancienne_configuration_conservee(settings, broker):
    o = make_orch(settings, broker)
    o._recharger_config_si_change()
    avant = o.risk.limits.risk_per_trade_percent
    _toucher(settings.home / "config" / "risk.yaml", "risk: [ceci n'est pas : du yaml valide" + chr(10))
    o._recharger_config_si_change()
    assert not o._recharger_config_si_change()
    assert o.risk.limits.risk_per_trade_percent == avant


def test_demande_de_statut_appliquee_tout_de_suite(settings, broker):
    o = make_orch(settings, broker)
    agent = next(a for a in o.registry.agents.values() if a.generates_trades and a.status == "LIVE")
    with open(settings.state_dir / "agent_status_requests.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"agent_id": agent.agent_id, "status": "SHADOW", "reason": "test"}) + "" + chr(10))
    o._appliquer_demandes_si_presentes()
    assert o.registry.get(agent.agent_id).status == "SHADOW"
    assert not (settings.state_dir / "agent_status_requests.jsonl").exists()
