"""Agents mis en avant et file du pipeline de recherche (demandes utilisateur du 2026-09-25)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tradinglab.core.types import AgentStatus
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.research.pipeline import Stage, ValidationRecord

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)


def test_les_agents_mis_en_avant_passent_devant_un_meilleur_score():
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(learning={"agents_prioritaires": ["E05"]})
    cands = [SimpleNamespace(agent_id="B07", setup_score=90.0), SimpleNamespace(agent_id="E05", setup_score=66.0),
             SimpleNamespace(agent_id="C02", setup_score=80.0)]
    assert [c.agent_id for c in sorted(cands, key=o._priorite, reverse=True)] == ["E05", "B07", "C02"]


def test_config_reelle_met_en_avant_les_neuf_agents(settings):
    assert set(settings.learning["agents_prioritaires"]) == {"E05", "F06", "B02", "C06", "B04", "E01", "D06", "D07", "C08"}


def _orch(records: dict, agents: list, per_cycle=2):
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(learning={"research_agents_per_cycle": per_cycle, "research_retry_hours": 24})
    o.registry = SimpleNamespace(agents={a: SimpleNamespace(agent_id=a, generates_trades=True, status=AgentStatus.SHADOW.value)
                                         for a in agents})
    o.research = SimpleNamespace(record=lambda aid: records.setdefault(aid, ValidationRecord(aid)))
    return o


def test_un_echec_recent_ne_bloque_plus_la_file():
    """Avant : K01 et K02 échouaient au backtest à chaque passage et prenaient les 2 places pour toujours."""
    recent = {"passed": False, "metrics": {}, "ts": (NOW - timedelta(hours=1)).isoformat()}
    records = {"K01": ValidationRecord("K01", stages={Stage.BACKTEST.value: dict(recent)}),
               "K02": ValidationRecord("K02", stages={Stage.BACKTEST.value: dict(recent)})}
    o = _orch(records, ["K01", "K02", "CH101", "CH102", "CH103"])
    assert o._research_queue(NOW) == ["CH101", "CH102"]


def test_les_plus_anciennement_essayes_passent_en_premier():
    vieux = {"passed": False, "metrics": {}, "ts": (NOW - timedelta(days=3)).isoformat()}
    moins_vieux = {"passed": False, "metrics": {}, "ts": (NOW - timedelta(days=2)).isoformat()}
    records = {"A": ValidationRecord("A", stages={Stage.BACKTEST.value: moins_vieux}),
               "B": ValidationRecord("B", stages={Stage.BACKTEST.value: vieux})}
    o = _orch(records, ["A", "B", "C"], per_cycle=3)
    assert o._research_queue(NOW) == ["C", "B", "A"]                 # jamais essayé, puis le plus ancien


def test_shadow_plafond_par_agent(tmp_path):
    """Un agent de cycle long ne peut plus occuper toutes les places virtuelles."""
    from tradinglab.learning.store import LearningStore
    from tradinglab.shadow.shadow import ShadowTrader
    sh = ShadowTrader(LearningStore(tmp_path / "l.db"), tmp_path / "s.json", max_open=400, max_per_agent=2)
    def cand(i, agent):
        return SimpleNamespace(id=f"c{i}", agent_id=agent, symbol=f"S{i}", side=SimpleNamespace(value="BUY", sign=1),
                               entry=1.0, sl=0.99, tp_plan=[1.02], sl_distance=0.01, regime=SimpleNamespace(value="TRENDING"),
                               session="LONDON", setup_score=70.0, bar_time="t", idempotency_key=f"k{i}")
    sh.open_from_candidates([cand(i, "O01") for i in range(5)] + [cand(9, "CH101")])
    agents = [p.agent_id for p in sh.positions.values()]
    assert agents.count("O01") == 2 and agents.count("CH101") == 1


def test_config_reelle_shadow(settings):
    assert int(settings.learning["shadow_max_open"]) >= 400


def test_backtest_sur_plusieurs_symboles_du_marche(tmp_path):
    """Un seul symbole (EURUSD) donnait 2 à 6 trades : aucun agent ne pouvait atteindre les 20 trades exigés."""
    from tradinglab.research.pipeline import ResearchPipeline
    specs = {s: SimpleNamespace(root=s, asset_class=c) for s, c in
             [("EURUSD", "forex"), ("GBPUSD", "forex"), ("USDJPY", "forex"), ("AUDUSD", "forex"), ("XAUUSD", "metals")]}
    rp = ResearchPipeline(SimpleNamespace(), SimpleNamespace(), {}, {"symbols_per_agent": 3}, lambda *a: None, specs,
                          tmp_path / "r")
    assert rp._symbols_for(SimpleNamespace(markets=["forex_majors"])) == ["EURUSD", "GBPUSD", "USDJPY"]
    assert rp._symbols_for(SimpleNamespace(markets=["metals"])) == ["XAUUSD"]


def test_validation_live_resume_sans_rien_modifier():
    from tradinglab.research.pipeline import Stage
    from tradinglab.research.validate_live import validate

    class _Rec:
        def __init__(self, ok):
            self.stages = {s.value: {"passed": s.value in ok, "metrics": {"sample_size": 30, "profit_factor": 1.5,
                                                                          "expectancy_r": 0.3, "symbols": ["EURUSD"]}}
                           for s in (Stage.BACKTEST, Stage.OUT_OF_SAMPLE, Stage.WALK_FORWARD, Stage.MONTE_CARLO)}

        def passed(self, st):
            return self.stages[st.value]["passed"]

    ok_par_agent = {"E05": {"BACKTEST", "OUT_OF_SAMPLE", "WALK_FORWARD", "MONTE_CARLO"}, "B01": {"BACKTEST"}}
    vus = []

    def etape(nom):
        def f(spec):
            vus.append((spec.agent_id, nom))
            return _Rec(ok_par_agent[spec.agent_id])
        return f

    pipe = SimpleNamespace(stage_backtest=etape("BACKTEST"), stage_out_of_sample=etape("OUT_OF_SAMPLE"),
                           stage_walk_forward=etape("WALK_FORWARD"), stage_monte_carlo=etape("MONTE_CARLO"),
                           record=lambda aid: _Rec(ok_par_agent[aid]))
    res = validate(pipe, [SimpleNamespace(agent_id="B01", name="b"), SimpleNamespace(agent_id="E05", name="e")], journal=lambda *_: None)
    assert [r["agent_id"] for r in res] == ["E05", "B01"] and res[0]["valide"] and res[1]["etapes_reussies"] == 1
    assert ("B01", "WALK_FORWARD") not in vus                                   # arrêt à la première étape échouée (OOS)


def test_baisse_temporaire_du_score_minimal_expire_seule():
    """2026-09-26 : score minimal à 55 « juste pour aujourd'hui », retour automatique à 65 à 17 h New York."""
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"required_setup_score": 65,
                                     "required_setup_score_temporaire": {"valeur": 55, "jusqu_a": "2026-09-26T21:00:00+00:00"}})
    assert o._required_score(datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)) == 55.0
    assert o._required_score(datetime(2026, 9, 26, 21, 0, tzinfo=timezone.utc)) == 65.0
    o.s.execution["required_setup_score_temporaire"] = {"valeur": 55, "jusqu_a": "pas une date"}
    assert o._required_score(datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)) == 65.0


def test_plafond_spread_atr_par_classe_d_actif():
    """2026-09-27 (accord utilisateur) : SOL (0,185 ATR) refusé par 07 alors que son coût ne fait que 12 % du stop."""
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"max_spread_atr_ratio": 0.15, "max_spread_atr_ratio_by_class": {"crypto": 0.25}})
    assert o._max_spread_atr_ratio(SimpleNamespace(asset_class="crypto")) == 0.25
    assert o._max_spread_atr_ratio(SimpleNamespace(asset_class="forex")) == 0.15
    assert o._max_spread_atr_ratio(None) == 0.15


def test_config_reelle_crypto_0_25_et_score_55_dimanche(settings):
    assert settings.execution["max_spread_atr_ratio_by_class"]["crypto"] == 0.25
    assert settings.execution["required_setup_score_temporaire"]["jusqu_a"] == "2026-09-27T21:00:00+00:00"


def test_consigne_arbitre_wait_reserve_aux_confirmations_precises():
    import inspect
    from tradinglab.agents.review import AdversarialReview
    src = inspect.getsource(AdversarialReview.review)
    assert "WAIT uniquement si une confirmation PRÉCISE" in src


def test_stop_minimal_par_classe_et_revues_ia_configurables():
    """2026-09-27 : stop ≥ 0,75 ATR H1 hors crypto (rejeu +13 R), crypto inchangée ; revues IA par cycle configurables."""
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"min_sl_atr_ratio": 0.25, "min_sl_atr_ratio_by_class": {"forex": 0.75},
                                     "max_llm_reviews_per_cycle": 5})
    assert o._min_sl_atr_ratio(SimpleNamespace(asset_class="forex")) == 0.75
    assert o._min_sl_atr_ratio(SimpleNamespace(asset_class="crypto")) == 0.25
    assert o._max_llm_reviews() == 5
    o.s.execution["max_llm_reviews_per_cycle"] = "x"
    assert o._max_llm_reviews() == 3
