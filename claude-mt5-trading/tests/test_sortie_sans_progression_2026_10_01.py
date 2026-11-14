"""2026-10-01, décision utilisateur (« oui, mais 8 h ») : un trade d'agent M1 / M5 / M15 qui n'a jamais atteint +0,2 R
au bout de 8 h est fermé au marché. Les agents H1 / H4 / D1, les positions adoptées et les dates illisibles sont exclus."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import yaml

from tradinglab.orchestration.orchestrator import Orchestrator

MAINTENANT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CFG = {"no_progress_exit": {"enabled": True, "max_hours": 8, "min_r": 0.2, "timeframes": ["M1", "M5", "M15"]}}


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(profit_management=CFG)
    tfs = {"M15A": "M15", "H1A": "H1", "D1A": "D1"}
    o.registry = SimpleNamespace(get=lambda aid: SimpleNamespace(timeframes={"entry": tfs[aid]}) if aid in tfs else None)
    o.now_fn = lambda: MAINTENANT
    return o


def _plan(agent="M15A", heures=9.0, max_r=0.1):
    return SimpleNamespace(agent_id=agent, max_r=max_r, opened_at=(MAINTENANT - timedelta(hours=heures)).isoformat())


def test_trade_m15_sans_progression_apres_8h_ferme():
    assert _orch()._sans_progression(_plan())


def test_avant_8h_ou_trade_parti_garde():
    o = _orch()
    assert not o._sans_progression(_plan(heures=7.5))
    assert not o._sans_progression(_plan(max_r=0.25)), "a déjà atteint +0,2 R"


def test_agents_plus_lents_et_adoptes_exclus():
    o = _orch()
    assert not o._sans_progression(_plan(agent="H1A"))
    assert not o._sans_progression(_plan(agent="D1A"))
    assert not o._sans_progression(_plan(agent="ADOPTED"))
    p = _plan()
    p.opened_at = "date illisible"
    assert not o._sans_progression(p)


def test_configuration():
    pm = yaml.safe_load(open("config/risk.yaml", encoding="utf-8"))
    def cherche(o):
        if isinstance(o, dict):
            if "no_progress_exit" in o:
                return o["no_progress_exit"]
            for v in o.values():
                r = cherche(v)
                if r is not None:
                    return r
        return None
    c = cherche(pm)
    assert c == {"enabled": True, "max_hours": 8, "min_r": 0.2, "timeframes": ["M1", "M5", "M15"]}
