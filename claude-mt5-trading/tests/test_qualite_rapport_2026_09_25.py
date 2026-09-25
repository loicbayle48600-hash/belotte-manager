"""Qualité mesurée, gel des réglages et rapport de 17 h New York (plan pro du 2026-09-25, points 2, 5 et 8)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from tradinglab.learning.quality import agent_quality, daily_report_text, freeze_status, summarize
from tradinglab.learning.store import LearningStore, TradeRecord


def _rec(i, agent, r, closed, cost=None, slip=None):
    f = {}
    if cost is not None:
        f["cost_ratio"] = cost
    if slip is not None:
        f["slippage_r"] = slip
    return TradeRecord(ticket=i, agent_id=agent, symbol="EURUSD", side="BUY", entry=1.1, sl=1.098, risk_money=628.0,
                       risk_percent=0.125, result_r=r, pnl=628.0 * r, opened_at=closed, closed_at=closed,
                       exit_reason="tp" if r > 0 else "sl", mfe_r=max(r, 0.2), mae_r=0.5, features=f)


def _db(tmp_path, recs):
    st = LearningStore(tmp_path / "learning.db")
    for r in recs:
        st.record_trade(r)
    return tmp_path / "learning.db"


def test_marge_d_incertitude_et_verdicts(tmp_path):
    recs = [_rec(i, "E05", 2.0 if i % 3 else -1.0, f"2026-09-2{2 + i % 3}T10:{i:02d}:00+00:00", cost=0.1, slip=0.02)
            for i in range(30)]
    recs += [_rec(100 + i, "B01", -1.0, f"2026-09-24T11:{i:02d}:00+00:00") for i in range(12)]
    recs += [_rec(200, "C08", 2.0, "2026-09-24T12:00:00+00:00")]
    q = agent_quality(_db(tmp_path, recs))
    par = {a["agent_id"]: a for a in q["agents"]}
    assert par["E05"]["verdict"] == "avantage prouvé" and par["E05"]["ic_bas"] > 0
    assert par["E05"]["cout_moy_pct"] == 10.0 and par["E05"]["glissement_moy_r"] == 0.02
    assert par["B01"]["verdict"] == "perdant prouvé"
    assert par["C08"]["verdict"] == "trop peu de trades"
    assert q["agents"][0]["agent_id"] == "C08"            # tri par espérance


def test_gel_des_reglages(tmp_path):
    db = _db(tmp_path, [_rec(i, "E05", 1.0, f"2026-09-26T10:{i:02d}:00+00:00") for i in range(7)]
             + [_rec(50, "E05", 1.0, "2026-09-25T10:00:00+00:00")])
    fs = freeze_status(db, {"version": "lab-v1", "depuis": "2026-09-26T00:00:00+00:00", "trades_requis": 100})
    assert fs["trades"] == 7 and fs["restant"] == 93 and not fs["termine"]
    assert freeze_status(db, None) is None


def test_rapport_du_jour(tmp_path):
    db = _db(tmp_path, [_rec(1, "E05", 2.0, "2026-09-25T15:00:00+00:00", cost=0.12),
                        _rec(2, "B01", -1.0, "2026-09-25T16:00:00+00:00"),
                        _rec(3, "B01", -1.0, "2026-09-24T16:00:00+00:00")])                  # veille : hors fenêtre
    txt = daily_report_text(db, datetime(2026, 9, 24, 21, tzinfo=timezone.utc), datetime(2026, 9, 25, 21, tzinfo=timezone.utc),
                            [{"nom": "demo 1", "trades": 2, "pnl": 640.0}], ["DD jour 1.1% >= limite interne 1.0%"],
                            {"version": "lab-v1", "depuis": "2026-09-25T00:00:00+00:00", "trades_requis": 100}, "2026-09-25")
    assert "Maître : 2 trades · +1.00 R" in txt and "Coût d'entrée moyen : 12 %" in txt
    assert "demo 1 : 2 trades · +640 $" in txt and "Meilleurs : E05 +2.00 R" in txt and "Pires : B01 -1.00 R" in txt
    assert "DD jour" in txt and "2/100 trades" in txt


def test_rapport_declenche_une_fois_au_passage_de_17h_new_york(tmp_path):
    from tradinglab.core.trading_day import TradingDayCalendar
    from tradinglab.orchestration.orchestrator import Orchestrator

    class _J:
        def __init__(self):
            self.events = []

        def event(self, kind, **kw):
            self.events.append((kind, kw))

        def warn(self, *a, **k):
            raise AssertionError(k)

    o = Orchestrator.__new__(Orchestrator)
    o.state = SimpleNamespace(last_daily_report="")
    o.trading_day = TradingDayCalendar()
    o.journal = _J()
    o._daily_report_text = lambda now, label: f"rapport {label}"
    o._maybe_daily_report(datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc))      # 16 h NY : mémorise seulement
    o._maybe_daily_report(datetime(2026, 9, 25, 20, 30, tzinfo=timezone.utc))
    assert o.journal.events == []
    o._maybe_daily_report(datetime(2026, 9, 25, 21, 1, tzinfo=timezone.utc))      # 17 h 01 NY : rapport de la journée close
    o._maybe_daily_report(datetime(2026, 9, 25, 21, 5, tzinfo=timezone.utc))
    assert o.journal.events == [("report_day", {"day": "2026-09-25", "text": "rapport 2026-09-25"})]


def test_telegram_rapport_du_jour_toujours_envoye():
    from tradinglab.monitoring.telegram_notifier import ALWAYS_KINDS, format_event
    assert "report_day" in ALWAYS_KINDS
    assert "Rapport du jour" in format_event({"kind": "report_day", "text": "x"})
