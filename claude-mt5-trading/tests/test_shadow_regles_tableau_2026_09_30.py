"""2026-09-30, décisions utilisateur :
- un agent SHADOW à 50 trades avec un PF < 0,8 est suspendu automatiquement ;
- la famille N (saisonnalité) à 50 trades avec un PF >= 1,3 est SIGNALÉE pour une revue live, jamais promue directement ;
- la page « Shadow & recherche » du dashboard (/shadow, /api/shadow) résume le shadow et la recherche GPU."""
from __future__ import annotations

import json


def _trades(store, agent_id, results, base=0):
    from tradinglab.learning.store import TradeRecord

    for i, r in enumerate(results):
        store.record_trade(TradeRecord(ticket=base + i, agent_id=agent_id, symbol="EURUSD", side="BUY", entry=1.0, sl=0.99,
                                       risk_money=100, risk_percent=0.1, result_r=r, pnl=100 * r, mode="shadow",
                                       opened_at="2026-09-30T00:00:00+00:00", closed_at="2026-09-30T01:00:00+00:00",
                                       exit_reason="sl"))


def _setup(tmp_path, settings):
    from tradinglab.agents.registry import AgentRegistry
    from tradinglab.core.types import AgentStatus
    from tradinglab.learning.store import LearningStore

    reg = AgentRegistry(status_file=tmp_path / "agent_status.json")
    store = LearningStore(tmp_path / "learning.db")
    gens = [a for a in reg.agents.values() if a.generates_trades]
    perdant, jeune = gens[0].agent_id, gens[1].agent_id
    n_agent = next(a.agent_id for a in gens if a.agent_id.startswith("N"))
    for aid in (perdant, jeune, n_agent):
        reg.set_status(aid, AgentStatus.SHADOW)
    return reg, store, perdant, jeune, n_agent


def test_regles_shadow_suspension_et_revue(tmp_path, settings):
    from tradinglab.core.types import AgentStatus
    from tradinglab.research.pipeline import DegradationManager

    cfg = settings.raw.get("learning", {})
    deg = cfg["degradation"]
    assert deg["shadow_suspend_min_trades"] == 50 and deg["shadow_suspend_max_profit_factor"] == 0.8
    assert deg["shadow_review_families"] == ["N"] and deg["shadow_review_min_trades"] == 50
    assert deg["shadow_review_min_profit_factor"] == 1.3

    reg, store, perdant, jeune, n_agent = _setup(tmp_path, settings)
    _trades(store, perdant, [1.0, -1.0, -1.0] * 17, base=0)          # 51 trades, PF 0,5
    _trades(store, jeune, [1.0, -1.0, -1.0] * 10, base=1000)         # 30 trades seulement : pas encore jugé
    _trades(store, n_agent, [2.0, -1.0] * 25, base=2000)             # 50 trades, PF 2,0

    class J:
        def __init__(self):
            self.ev = []

        def event(self, kind, **kw):
            self.ev.append((kind, kw))

    j = J()
    dm = DegradationManager(reg, store, cfg, j)
    changes = dm.run()
    assert reg.get(perdant).status == AgentStatus.SUSPENDED.value
    assert reg.get(jeune).status == AgentStatus.SHADOW.value
    assert reg.get(n_agent).status == AgentStatus.SHADOW.value, "jamais promu directement"
    assert any(c["agent_id"] == perdant and "shadow perdant" in c["reason"] for c in changes)
    revues = [kw for k, kw in j.ev if k == "shadow_revue_live"]
    assert [r["agent_id"] for r in revues] == [n_agent]
    dm.run()                                                           # signalé une seule fois
    assert len([1 for k, _ in j.ev if k == "shadow_revue_live"]) == 1


def test_tableau_shadow_et_page(tmp_path, settings):
    from tradinglab.learning.shadow_board import shadow_board

    reg, store, perdant, jeune, n_agent = _setup(tmp_path, settings)
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    (home / "state").mkdir()
    (home / "reports").mkdir()
    from tradinglab.learning.store import LearningStore

    st2 = LearningStore(home / "data" / "learning.db")
    _trades(st2, perdant, [1.0, -1.0, -1.0] * 17)
    _trades(st2, n_agent, [2.0, -1.0] * 25, base=500)
    _trades(st2, jeune, [2.0, -1.0] * 50, base=1000)                 # 100 trades, PF 2,0
    (home / "data" / "agent_status.json").write_text(json.dumps({"status": {perdant: "SHADOW", n_agent: "SHADOW", jeune: "SHADOW"}}), encoding="utf-8")
    (home / "state" / "shadow_positions.json").write_text(json.dumps({"positions": {"a": {"agent_id": "X99"}}}), encoding="utf-8")
    (home / "state" / "recherche_continue.json").write_text(json.dumps({"passages": 3, "configurations": 1234}), encoding="utf-8")
    (home / "reports" / "massive_continu_1_2026-09-30_0725.json").write_text(json.dumps({
        "date": "2026-09-30T07:25:00+00:00", "etapes": {"testees": 1000, "t_min": 5.4, "significatives": 3, "controle": 2},
        "propositions": ["X99"], "retenues": [{"config": {"strategy": "exhaustion", "entry_tf": "M5", "asset_class": "u_forex",
                                                           "sessions": ["NEWYORK"]},
                                                "apprentissage": {"pf": 1.8}, "controle": {"pf": 1.6, "n": 300}}]}), encoding="utf-8")
    d = shadow_board(home, settings.raw.get("learning", {}))
    par_id = {a["agent_id"]: a for a in d["agents"]}
    assert par_id[perdant]["verdict"] == "perdant"
    # 2026-10-01, décision utilisateur : 100 trades shadow avant le live (20 avant)
    assert par_id[n_agent]["verdict"] == "en cours", "50 trades : pas encore jugé"
    assert par_id[jeune]["verdict"] == "prêt pour revue live"
    assert par_id["X99"]["ouvertes"] == 1 and par_id["X99"]["n"] == 0
    assert d["criteres"]["min_shadow"] == 100 and d["criteres"]["perdant_n"] == 50
    fam_n = next(f for f in d["familles"] if f["famille"] == "N")
    assert fam_n["n"] == 50 and fam_n["pf"] == 2.0
    r = d["recherche"]
    assert r["configurations"] == 1234 and r["passages"][0]["propositions"] == 1
    assert r["idees"][0]["agent_id"] == "X99" and r["idees"][0]["pf_controle"] == 1.6

    from tradinglab.dashboards import server

    assert "/api/shadow" in server.SHADOW_HTML and 'href="/shadow"' in server.STATS_HTML
