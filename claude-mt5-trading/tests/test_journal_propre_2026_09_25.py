"""Journal propre (plan pro du 2026-09-25, point 7) : alertes du watchdog dédupliquées."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from tradinglab.monitoring.watchdog import Watchdog


class _J:
    def __init__(self):
        self.events = []

    def event(self, kind, **kw):
        self.events.append((kind, kw))


def _wd():
    w = Watchdog.__new__(Watchdog)
    w.journal, w._alert_key, w._alert_ts = _J(), None, 0.0
    return w


def test_meme_alerte_journalisee_une_fois_puis_rappel_toutes_les_15_min():
    w = _wd()
    for t, dd in enumerate(["1.13", "1.14", "1.15"]):
        w._emit_alert(SimpleNamespace(reasons=[f"DD jour {dd}% >= limite interne 1.0%"], actions=[]), now=float(t * 3))
    assert len(w.journal.events) == 1
    w._emit_alert(SimpleNamespace(reasons=["DD jour 1.2% >= limite interne 1.0%"], actions=[]), now=901.0)
    assert len(w.journal.events) == 2 and w.journal.events[-1][1].get("rappel") is True


def test_nouvelle_alerte_action_et_retour_a_la_normale():
    w = _wd()
    w._emit_alert(SimpleNamespace(reasons=["DD jour 1.1%"], actions=[]), now=0.0)
    w._emit_alert(SimpleNamespace(reasons=["DD jour 1.1%", "orchestrateur silencieux depuis 90s"], actions=[]), now=3.0)
    w._emit_alert(SimpleNamespace(reasons=["DD jour 1.1%"], actions=["SAFE_MODE"]), now=6.0)
    w._emit_alert(SimpleNamespace(reasons=[], actions=[]), now=9.0)
    w._emit_alert(SimpleNamespace(reasons=[], actions=[]), now=12.0)
    assert [k for k, _ in w.journal.events] == ["watchdog_alert", "watchdog_alert", "watchdog_alert", "watchdog_ok"]


def test_candidat_et_decision_du_gate_journalises_une_fois_par_nature():
    from tradinglab.orchestration.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.journal = _J()
    for _ in range(5):
        o._journal_once("gate", "gate:US500|BUY|L03|t", "06_fresh_price|10_stop_loss", approved=False)
    o._journal_once("gate", "gate:US500|BUY|L03|t", "06_fresh_price", approved=False)       # nature changée
    o._journal_once("gate", "gate:US500|BUY|L03|t", "OK", force=True, approved=True)
    o._journal_once("gate", "gate:US500|BUY|L03|t", "OK", force=True, approved=True)        # approbation : toujours
    assert [kw["approved"] for _, kw in o.journal.events] == [False, False, True, True]


def test_version_du_code_lisible():
    from tradinglab.core.version import code_version
    v = code_version()
    assert isinstance(v, str) and v


def test_trade_porte_version_cout_et_glissement():
    from tradinglab.learning.post_trade import build_trade_record
    plan = SimpleNamespace(ticket=7, agent_id="E05", symbol="EURUSD", side="BUY", entry=1.1, initial_sl=1.098,
                           initial_risk_money=628.0, risk_percent=0.125, opened_at="2026-09-25T13:00:00+00:00",
                           regime="TRENDING", tp_plan=[], min_r=0.0, max_r=0.0, tp1_done=False, tp2_done=False,
                           break_even_done=False, candidate_id="c")
    rec = build_trade_record(plan, [], {"version": "lab-v1", "cost_ratio": 0.12, "slippage_r": 0.03}, "2026-09-25T14:00:00+00:00")
    assert rec.features["version"] == "lab-v1" and rec.features["cost_ratio"] == 0.12 and rec.features["slippage_r"] == 0.03


def test_gate_exporte_le_cout_d_entree(settings, broker):
    import pandas as pd
    from tradinglab.core.types import Side
    from tests.test_risk_and_gate import ctx_for, gate_for, make_candidate, make_state
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, "EURUSD", Side.BUY)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, correlations=pd.DataFrame()))
    assert res.approved and 0 < res.cost_ratio < 0.35


def test_revue_ia_bornee_dans_le_temps():
    """2026-09-25 : un appel IA bloqué portait le cycle à 50 s (tolérance du watchdog : 45 s)."""
    import time as _t
    from tradinglab.core.types import Verdict
    from tradinglab.orchestration.orchestrator import Orchestrator

    class _Rev:
        def review(self, c, bonus):
            if c.symbol == "LENT":
                _t.sleep(2.0)
            c.verdict, c.review = Verdict.APPROVE, {"llm": True}

        def deterministic(self, c, bonus):
            return SimpleNamespace(verdict=Verdict.WAIT, to_dict=lambda: {"det": True})

    o = Orchestrator.__new__(Orchestrator)
    o.review, o.journal = _Rev(), _J()
    o.journal.warn = lambda msg, **kw: o.journal.events.append(("warn", kw))
    rapide = SimpleNamespace(symbol="EURUSD", agent_id="E05", verdict=None, review={})
    lent = SimpleNamespace(symbol="LENT", agent_id="B02", verdict=None, review={})
    t0 = _t.monotonic()
    o._review_with_deadline([rapide, lent], 0.0, deadline=0.5)
    assert _t.monotonic() - t0 < 1.5
    assert rapide.verdict is Verdict.APPROVE and rapide.review == {"llm": True}
    assert lent.verdict is Verdict.WAIT and "hors délai" in lent.review["llm_skipped"]
    _t.sleep(2.0)
    assert lent.verdict is Verdict.WAIT                    # la revue tardive n'a rien modifié


def test_revue_ia_hors_delai_ne_valide_jamais_une_entree():
    """2026-09-26 : IA hors délai → le repli déterministe approuvait (score 63 ≥ 55) un BTCUSD que l'IA avait mis en
    attente 5 min plus tôt ; −617 $. Désormais APPROVE devient WAIT quand l'avis IA manque."""
    import time as _t
    from tradinglab.core.types import Verdict
    from tradinglab.orchestration.orchestrator import Orchestrator

    class _Rev:
        def review(self, c, bonus):
            _t.sleep(1.0)

        def deterministic(self, c, bonus):
            return SimpleNamespace(verdict=Verdict.APPROVE, to_dict=lambda: {"verdict": "APPROVE"})

    o = Orchestrator.__new__(Orchestrator)
    o.review, o.journal = _Rev(), _J()
    o.journal.warn = lambda msg, **kw: None
    c = SimpleNamespace(symbol="BTCUSD", agent_id="C01", verdict=None, review={})
    o._review_with_deadline([c], 0.0, deadline=0.2)
    assert c.verdict is Verdict.WAIT and c.review["verdict"] == "WAIT" and "hors délai" in c.review["llm_skipped"]


def test_note_crypto_du_week_end_dans_le_dossier_ia():
    """2026-09-26 : l'IA jugeait « incohérente » la session Londres/New York d'un samedi sur la crypto."""
    from datetime import datetime, timezone
    from tradinglab.orchestration.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    specs = {"BTCUSD": SimpleNamespace(asset_class="crypto"), "NETH25": SimpleNamespace(asset_class="indices")}
    o.broker = SimpleNamespace(symbol_info=lambda s: specs.get(s))
    samedi, lundi = datetime(2026, 9, 26, 15, tzinfo=timezone.utc), datetime(2026, 9, 28, 15, tzinfo=timezone.utc)
    assert "24 h/24" in o._market_note(SimpleNamespace(symbol="BTCUSD", created_at=samedi))
    assert o._market_note(SimpleNamespace(symbol="NETH25", created_at=samedi)) is None
    assert o._market_note(SimpleNamespace(symbol="BTCUSD", created_at=lundi)) is None


def test_note_de_contexte_transmise_a_l_ia():
    from tradinglab.agents.review import AdversarialReview

    vus = []

    class _LLM:
        def complete(self, role, system, user, **kw):
            vus.append(user)
            return None

    rv = AdversarialReview(_LLM(), 65, 1.5)
    rv.market_note = lambda c: "NOTE-TEST"
    c = SimpleNamespace(to_dict=lambda: {"symbol": "BTCUSD"}, symbol="BTCUSD", side=SimpleNamespace(value="BUY"),
                        agent_id="E01", bar_time="t")
    rv._ask("bull_thesis", "x", c)
    assert "NOTE-TEST" in vus[0] and "contexte_marche" in vus[0]


def test_cout_en_prix_et_pourcentage_du_stop_dans_le_dossier_ia():
    """2026-09-27 : l'IA comparait 500 POINTS de spread BTC (5 $) à un stop de 91,8 $ et bloquait tout le BTC."""
    from tradinglab.agents.review import AdversarialReview
    from tradinglab.orchestration.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    spec = SimpleNamespace(point=0.01, digits=2, asset_class="crypto", tick_size=0.01, tick_value=0.01)
    o.broker = SimpleNamespace(symbol_info=lambda s: spec)
    o.prop = SimpleNamespace(commission_price=lambda sp, px: px * 0.004 / 100)
    c = SimpleNamespace(symbol="BTCUSD", entry=84000.0, sl=83908.2, spread_points=500)
    note = o._cost_note(c)
    assert note["spread_prix"] == 5.0 and note["spread_pct_du_stop"] == pytest.approx(5.4, abs=0.1)

    vus = []

    class _LLM:
        def complete(self, role, system, user, **kw):
            vus.append(user)
            return None

    rv = AdversarialReview(_LLM(), 65, 1.5)
    rv.cost_note = lambda cand: note
    cand = SimpleNamespace(to_dict=lambda: {"symbol": "BTCUSD", "spread_points": 500}, symbol="BTCUSD",
                           side=SimpleNamespace(value="BUY"), agent_id="P16", bar_time="t")
    rv._ask("bull_thesis", "x", cand)
    assert "cout_entree" in vus[0] and '"spread_points"' not in vus[0]


def test_prefiltre_cout_et_symbole_ferme_avant_la_revue_ia():
    """2026-09-27 : POL/LNK/DOT (clôture seulement) et les cryptos chères occupaient les créneaux de la revue IA."""
    from tradinglab.orchestration.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    specs = {"POLUSD": SimpleNamespace(trade_allowed=False, point=0.0001, digits=4, asset_class="crypto"),
             "BTCUSD": SimpleNamespace(trade_allowed=True, point=0.01, digits=2, asset_class="crypto"),
             "KSMUSD": SimpleNamespace(trade_allowed=True, point=0.01, digits=2, asset_class="crypto")}
    o.broker = SimpleNamespace(symbol_info=lambda s: specs[s])
    o.prop = SimpleNamespace(commission_price=lambda sp, px: 0.0)
    o.s = SimpleNamespace(execution={"max_spread_sl_ratio": 0.20})
    assert "clôture seulement" in o._prefiltre_cout(SimpleNamespace(symbol="POLUSD", entry=0.117, sl=0.114, spread_points=56))
    assert o._prefiltre_cout(SimpleNamespace(symbol="BTCUSD", entry=84000.0, sl=83900.0, spread_points=500)) == ""
    assert "coût" in o._prefiltre_cout(SimpleNamespace(symbol="KSMUSD", entry=46.6, sl=45.9, spread_points=449))



def test_llm_skipped_journalise_une_fois_par_motif_et_par_10_min():
    """2026-09-27 : 2 900 lignes `llm_skipped` identiques par jour."""
    import threading
    from tradinglab.models.client import LLMClient
    cl = LLMClient.__new__(LLMClient)
    cl.journal, cl._skip_seen, cl._lock = _J(), {}, threading.Lock()
    for i in range(5):
        cl._skip("bull_thesis", f"quota abonnement atteint : reprise dans {i} min")
    cl._skip("bear_thesis", "quota abonnement atteint : reprise dans 3 min")
    assert len(cl.journal.events) == 2
    cl._skip_seen[("bull_thesis", "quota abonnement atteint : reprise dans # min")] -= 601
    cl._skip("bull_thesis", "quota abonnement atteint : reprise dans 9 min")
    assert len(cl.journal.events) == 3


def test_rapport_du_jour_affiche_les_ecarts_de_copie(tmp_path):
    from datetime import datetime, timezone
    from tradinglab.learning.quality import daily_report_text
    from tradinglab.learning.store import LearningStore
    LearningStore(tmp_path / "l.db")
    txt = daily_report_text(tmp_path / "l.db", datetime(2026, 9, 26, 21, tzinfo=timezone.utc),
                            datetime(2026, 9, 27, 21, tzinfo=timezone.utc),
                            [{"nom": "moneta", "trades": 1, "pnl": 12.0,
                              "ecarts": ["positions du maître non portées : GBPAUD", "NZDJPY : Market closed (×8)"]}],
                            [], None, "2026-09-27")
    assert "écart : positions du maître non portées : GBPAUD" in txt and "Market closed" in txt
