"""`ERREUR_EXECUTION` était un verdict mort : déclaré, documenté, jamais produit.

Constaté le 2026-09-21 en lisant `post_trade.py` après la clôture de GBPNZD :

    verdict: str = "VARIANCE_NORMALE"   # VARIANCE_NORMALE | ERREUR_STRATEGIE | ERREUR_EXECUTION | INDETERMINE

`ERREUR_EXECUTION` n'apparaissait que dans ce commentaire et dans `CLAUDE.md` §11 — aucune ligne
ne l'assignait. Et juste en dessous, une branche sans effet trahissait une intention inachevée :

    verdict = "VARIANCE_NORMALE" if rec.result_r <= 0 else "VARIANCE_NORMALE"

Conséquence : **toute** perte d'un agent ayant moins de 40 trades ressortait en VARIANCE_NORMALE,
y compris celles où l'idée était manifestement bonne et où seule la gestion avait échoué. C'est
exactement le cas XRPUSD signalé par l'utilisateur — TP1 encaissé à 1,5 R puis retour au stop
initial, sortie nette à -1,29 $ — qui ne pouvait pas être distingué d'une perte ordinaire.

La distinction tient à une asymétrie réelle : une erreur de *stratégie* est une affirmation
statistique (d'où le seuil de 40 trades), une erreur d'*exécution* est un fait observable sur un
seul trade. `rule_compliance` reste hors du calcul : il est rempli de booléens codés en dur.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.learning.post_trade import PostTradeAnalyzer  # noqa: E402
from tradinglab.learning.store import AgentStats, LearningStore, TradeRecord  # noqa: E402


def _rec(**kw) -> TradeRecord:
    base = dict(ticket=1, agent_id="B01", symbol="EURUSD", side="BUY", entry=1.08, sl=1.078,
                risk_money=625.0, risk_percent=0.125, result_r=-1.0, pnl=-625.0,
                opened_at="2026-09-21T08:00:00+00:00", closed_at="2026-09-21T09:00:00+00:00",
                exit_reason="sl", spread_points=10, mae_r=1.0, mfe_r=0.0)
    base.update(kw)
    return TradeRecord(**base)


def _analyzer(tmp_path, sample_size: int = 3, expectancy: float = 0.0, degradation: float = 0.0):
    a = PostTradeAnalyzer(LearningStore(tmp_path / "learning.db"), None, {"min_sample_size": 40})
    a.store.agent_stats = lambda agent_id: AgentStats(  # type: ignore[assignment]
        agent_id=agent_id, sample_size=sample_size, expectancy_r=expectancy, degradation_score=degradation)
    return a


# ------------------------------------------------------- erreur d'exécution
def test_gain_rendu_au_stop_est_une_erreur_d_execution(tmp_path):
    """Cas XRPUSD : le trade a valu 1,7 R puis est revenu au stop. L'idée était bonne."""
    rev = _analyzer(tmp_path).analyze(_rec(mfe_r=1.7, mae_r=1.0, result_r=-1.0))
    assert rev.verdict == "ERREUR_EXECUTION"
    assert any("1R" in f for f in rev.what_failed)


def test_spread_degrade_sur_une_perte_est_une_erreur_d_execution(tmp_path):
    rev = _analyzer(tmp_path).analyze(_rec(spread_points=120, mfe_r=0.1))
    assert rev.verdict == "ERREUR_EXECUTION"
    assert any("exécution dégradées" in f for f in rev.what_failed)


def test_un_seul_trade_suffit(tmp_path):
    """Différence de nature : l'erreur d'exécution ne demande pas 40 trades, contrairement à la stratégie."""
    rev = _analyzer(tmp_path, sample_size=1).analyze(_rec(mfe_r=1.5, mae_r=1.0))
    assert rev.verdict == "ERREUR_EXECUTION"


# ------------------------------------------------------- ce qui ne doit PAS changer
def test_perte_ordinaire_reste_variance_normale(tmp_path):
    """Stop touché sans excursion favorable et spread normal : rien à reprocher à l'exécution."""
    rev = _analyzer(tmp_path).analyze(_rec(mfe_r=0.2, mae_r=1.0, spread_points=10))
    assert rev.verdict == "VARIANCE_NORMALE"


def test_un_gain_n_est_jamais_une_erreur_d_execution(tmp_path):
    rev = _analyzer(tmp_path).analyze(_rec(result_r=2.1, pnl=1300.0, exit_reason="tp", mfe_r=2.4, mae_r=0.2))
    assert rev.verdict == "VARIANCE_NORMALE"


def test_sortie_inconnue_reste_indetermine(tmp_path):
    rev = _analyzer(tmp_path).analyze(_rec(exit_reason="UNKNOWN", mfe_r=1.5))
    assert rev.verdict == "INDETERMINE" and rev.challenger_needed is False


def test_agent_casse_statistiquement_prime(tmp_path):
    """Un agent à espérance négative sur 40+ trades reste un problème de stratégie, pas d'exécution."""
    a = _analyzer(tmp_path, sample_size=60, expectancy=-0.3, degradation=0.8)
    rev = a.analyze(_rec(mfe_r=1.6, mae_r=1.0))
    assert rev.verdict == "ERREUR_STRATEGIE"
    assert rev.challenger_needed is True


def test_les_quatre_verdicts_sont_atteignables(tmp_path):
    """Verrou contre la régression d'origine : plus aucun verdict déclaré ne doit rester mort."""
    obtenus = {
        _analyzer(tmp_path).analyze(_rec(mfe_r=0.2)).verdict,
        _analyzer(tmp_path).analyze(_rec(mfe_r=1.6, mae_r=1.0)).verdict,
        _analyzer(tmp_path).analyze(_rec(exit_reason="UNKNOWN")).verdict,
        _analyzer(tmp_path, 60, -0.3, 0.8).analyze(_rec()).verdict,
    }
    assert obtenus == {"VARIANCE_NORMALE", "ERREUR_EXECUTION", "INDETERMINE", "ERREUR_STRATEGIE"}


def test_spread_juge_en_part_du_stop_et_non_en_points(tmp_path):
    """2026-09-25 : US500 L03 à 50 points de spread (0,5 point d'indice pour un stop de 6,6 points, soit 8 % du
    risque) classé ERREUR_EXECUTION par le seuil fixe de 40 points. C'était un stop touché ordinaire."""
    us500 = _rec(symbol="US500", entry=7728.0, sl=7721.4, spread_points=50, features={"spread_sl_ratio": 0.076})
    assert _analyzer(tmp_path).analyze(us500).verdict == "VARIANCE_NORMALE"
    serre = _rec(spread_points=12, features={"spread_sl_ratio": 0.5})                  # 50 % du risque : dégradé
    rev = _analyzer(tmp_path).analyze(serre)
    assert rev.verdict == "ERREUR_EXECUTION" and any("50 % du risque" in f for f in rev.what_failed)


def test_part_du_spread_calculee_a_la_construction_du_record():
    from types import SimpleNamespace
    from tradinglab.learning.post_trade import build_trade_record
    plan = SimpleNamespace(ticket=7, agent_id="L03", symbol="US500", side="BUY", entry=7728.0, initial_sl=7721.4,
                           initial_risk_money=624.36, risk_percent=0.125, opened_at="2026-09-25T13:15:23+00:00",
                           regime="TRENDING", tp_plan=[], min_r=-0.98, max_r=0.0, tp1_done=False, tp2_done=False,
                           break_even_done=False, candidate_id="c")
    rec = build_trade_record(plan, [], {"spread_points": 50}, "2026-09-25T13:19:29+00:00", point=0.01)
    assert rec.features["spread_sl_ratio"] == pytest.approx(0.5 / 6.6, abs=1e-3)
