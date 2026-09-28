"""Optimiseur systématique d'agents (2026-09-28) : grille, filtres, propositions et application par l'orchestrateur."""
from __future__ import annotations

import json
from types import SimpleNamespace

from tradinglab.agents.registry import AgentSpec
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.research import optimizer as opt


def test_grille_et_filtres():
    g = opt.grid()
    assert len(g) == len(opt.STRATEGIES) * len(opt.TIMEFRAMES) * len(opt.SL_ATR) * len(opt.RR) * len(opt.CLASSES)
    assert len(opt.grid(10)) == 10
    seuils = {"min_sample_size": 40, "min_profit_factor": 1.2, "min_expectancy_r": 0.10, "max_drawdown_r": 15.0}
    assert opt.survives_stage1({"sample_size": 60, "profit_factor": 1.4, "expectancy_r": 0.2, "max_drawdown_r": 8}, seuils)
    assert not opt.survives_stage1({"sample_size": 12, "profit_factor": 2.5, "expectancy_r": 0.8, "max_drawdown_r": 3}, seuils)
    assert opt.score({"sample_size": 200, "expectancy_r": 0.3}) > opt.score({"sample_size": 10, "expectancy_r": 2.5})
    assert opt.survives_stage2({"robustness_ratio": 0.6, "oos_expectancy_r": 0.1, "oos_trades": 25})
    assert not opt.survives_stage2({"robustness_ratio": 0.4, "oos_expectancy_r": 0.3, "oos_trades": 25})


def test_proposition_est_une_spec_valide_et_ids_libres():
    cfg = opt.Config("ema_trend", "H1", "H4", 1.5, 2.0, "forex")
    d = opt.proposal_spec(cfg, "X01", {"profit_factor": 1.4, "sample_size": 80, "expectancy_r": 0.2},
                          {"robustness_ratio": 0.7, "oos_expectancy_r": 0.15}, "SHADOW")
    spec = AgentSpec(**d)
    assert spec.generates_trades and spec.markets == ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"] and spec.params["sl_atr"] == 1.5
    assert opt.next_ids({"X01", "X03"}, 3) == ["X02", "X04", "X05"]


def test_orchestrateur_ajoute_les_propositions(tmp_path):
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(state_dir=tmp_path)
    ajoutes = []
    o.registry = SimpleNamespace(agents={"X01": object()}, add=lambda spec: ajoutes.append(spec.agent_id))
    o.journal = SimpleNamespace(event=lambda *a, **k: None, warn=lambda *a, **k: None)
    cfg = opt.Config("bollinger_mr", "M15", "H1", 1.5, 1.5, "crypto")
    lignes = [json.dumps(opt.proposal_spec(cfg, "X01", {}, {}, "SHADOW")), json.dumps(opt.proposal_spec(cfg, "X02", {}, {}, "SHADOW")), "{"]
    (tmp_path / "agent_proposals.jsonl").write_text("\n".join(lignes), encoding="utf-8")
    assert o._apply_agent_proposals() == 1 and ajoutes == ["X02"]
    assert not (tmp_path / "agent_proposals.jsonl").exists()


def test_dedupe_garde_le_mieux_classe_par_strategie_tf_classe_stop():
    """28/09 : 20 retenus = 6 configurations distinctes, `rr` ne changeant presque rien avec la gestion de position."""
    def r(strat, tf, cls, sl, rr):
        return {"config": {"strategy": strat, "entry_tf": tf, "trend_tf": "D1", "sl_atr": sl, "rr": rr, "asset_class": cls}}
    classes = [r("breakout_retest", "H4", "metals", 1.0, 1.5), r("breakout_retest", "H4", "metals", 1.0, 2.5),
               r("breakout_retest", "H4", "metals", 1.5, 3.0), r("macd_momentum", "H4", "metals", 1.0, 1.5),
               r("breakout_retest", "H1", "metals", 1.0, 1.5), r("breakout_retest", "H4", "forex", 1.0, 1.5)]
    out = opt.dedupe(classes)
    assert [(x["config"]["strategy"], x["config"]["entry_tf"], x["config"]["asset_class"], x["config"]["sl_atr"], x["config"]["rr"]) for x in out] == [
        ("breakout_retest", "H4", "metals", 1.0, 1.5), ("breakout_retest", "H4", "metals", 1.5, 3.0),
        ("macd_momentum", "H4", "metals", 1.0, 1.5), ("breakout_retest", "H1", "metals", 1.0, 1.5),
        ("breakout_retest", "H4", "forex", 1.0, 1.5)]
