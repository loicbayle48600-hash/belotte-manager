"""Améliorations du 2026-09-24 (proposées puis « fais tous les points » par l'utilisateur).

1. filtre d'extension MESURÉ : chaque candidat porte `extension_atr` et `extension_filter_would_reject` ;
2. rapport hebdomadaire (lundi 08:00 Paris), une seule fois par semaine, toujours envoyé sur Telegram ;
4. commission d'ENTRÉE comptée dans le P&L des trades (deal IN des comptes Raw Spread) ;
5. suspension rapide : agent LIVE ≥ 10 trades avec profit factor < 0,5 → SUSPENDED ;
6. cohérence 25 % appliquée dès 0,3 % de profit (au lieu de 1 %) ;
7. paires exotiques : aucune entrée à ± 30 min du reset 17:00 New York, fermeture 10 min avant.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.types import Side  # noqa: E402


# ------------------------------------------------------------------ 4. commission d'entrée
def test_commission_d_entree_comptee_dans_le_pnl():
    from tradinglab.learning.post_trade import close_deals_summary

    t = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
    d = lambda entry, profit, com: SimpleNamespace(position_id=7, entry=entry, profit=profit, commission=com, swap=0.0,  # noqa: E731
                                                   volume=1.0, price=1.1, comment="tp", time=t)
    pnl, *_ = close_deals_summary([d("IN", 0.0, -3.5), d("OUT", 100.0, -3.5)], 7)
    assert pnl == pytest.approx(93.0), "100 $ de gain − 3,5 $ à l'entrée − 3,5 $ à la sortie"


# ------------------------------------------------------------------ 5. suspension rapide
def test_suspension_rapide_d_un_agent_qui_perd(settings, tmp_path):
    from tradinglab.agents.registry import AgentRegistry
    from tradinglab.core.types import AgentStatus
    from tradinglab.learning.store import LearningStore, TradeRecord
    from tradinglab.research.pipeline import DegradationManager

    reg = AgentRegistry(status_file=tmp_path / "agent_status.json")
    store = LearningStore(tmp_path / "learning.db")
    live = [a for a in reg.generators()][:2]
    perdant, gagnant = live[0].agent_id, live[1].agent_id
    for i, r in enumerate([1.0] + [-1.0] * 9):                 # PF 1/9 = 0,11 sur 10 trades
        store.record_trade(TradeRecord(ticket=i, agent_id=perdant, symbol="EURUSD", side="BUY", entry=1.0, sl=0.99,
                                       risk_money=100, risk_percent=0.1, result_r=r, pnl=100 * r,
                                       opened_at="2026-09-24T00:00:00+00:00", closed_at="2026-09-24T01:00:00+00:00", exit_reason="sl"))
    for i, r in enumerate([1.0, -1.0] * 5):                    # PF 1,0 : pas suspendu
        store.record_trade(TradeRecord(ticket=100 + i, agent_id=gagnant, symbol="EURUSD", side="BUY", entry=1.0, sl=0.99,
                                       risk_money=100, risk_percent=0.1, result_r=r, pnl=100 * r,
                                       opened_at="2026-09-24T00:00:00+00:00", closed_at="2026-09-24T01:00:00+00:00", exit_reason="sl"))
    changes = DegradationManager(reg, store, settings.raw.get("learning", {})).run()
    assert reg.get(perdant).status == AgentStatus.SUSPENDED.value
    assert reg.get(gagnant).status == AgentStatus.LIVE.value
    assert any(c["agent_id"] == perdant and "suspension rapide" in c.get("reason", "") for c in changes)


# ------------------------------------------------------------------ 6. cohérence dès 0,3 %
def test_coherence_appliquee_des_0_3_pourcent(settings):
    assert float(settings.prop["consistency_enforce_from_profit_percent"]) == 0.3


# ------------------------------------------------------------------ 7. rollover exotique
def _orch(settings, broker):
    from test_review_market_agents_orchestration import make_orch
    return make_orch(settings, broker)


def test_rollover_bloque_les_exotiques_autour_du_reset(settings, broker):
    """Règle ANNULÉE par l'utilisateur le 2026-09-24 (config retirée) : le code reste testé, activé ici à la main."""
    o = _orch(settings, broker)
    assert o._rollover_cfg() == {}, "annulée : aucune section rollover dans risk.yaml"
    assert o._rollover_block(SimpleNamespace(currency_base="USD", currency_profit="SGD"), datetime.now(timezone.utc)) == ""
    o.s.raw["rollover"] = {"exotic_currencies": ["SGD"], "block_entries_before_min": 30, "block_entries_after_min": 30,
                           "close_before_min": 10}
    ref = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    reset = o.trading_day.next_reset(ref)                         # 17:00 New York
    exo = SimpleNamespace(currency_base="USD", currency_profit="SGD")
    major = SimpleNamespace(currency_base="EUR", currency_profit="USD")
    assert "rollover dans" in o._rollover_block(exo, reset - timedelta(minutes=20))
    assert "rollover il y a" in o._rollover_block(exo, reset + timedelta(minutes=20))
    assert o._rollover_block(exo, reset - timedelta(hours=2)) == ""
    assert o._rollover_block(major, reset - timedelta(minutes=5)) == "", "paire majeure : jamais bloquée"


def test_gate_refuse_une_exotique_dans_la_fenetre(settings, broker):
    from test_risk_and_gate import ctx_for, gate_for, make_candidate, make_state

    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr, rollover_block="paire exotique : rollover dans 12 min"))
    chk = {x.name: x for x in res.checks}["07c_rollover_exotique"]
    assert not chk.ok and not res.approved
    res2, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert {x.name: x for x in res2.checks}["07c_rollover_exotique"].ok


# ------------------------------------------------------------------ 1. mesure d'extension
def test_extension_mesuree_sur_les_candidats(settings, broker):
    o = _orch(settings, broker)
    o.cycle()
    sym = next(iter(o.snapshots))
    df = o.snapshots[sym].frames["M15"]
    row = df.iloc[-2]
    c = SimpleNamespace(symbol=sym, timeframes=["M15", "H1"], entry=float(row["ema20"]) + 2 * float(row["atr14"]), review={})
    o._annotate_extension([c])
    assert c.review["extension_atr"] == pytest.approx(2.0, abs=0.01) and c.review["extension_filter_would_reject"] is True
    c2 = SimpleNamespace(symbol=sym, timeframes=["M15", "H1"], entry=float(row["ema20"]), review={})
    o._annotate_extension([c2])
    assert c2.review["extension_filter_would_reject"] is False


# ------------------------------------------------------------------ 2. rapport hebdomadaire
def test_rapport_hebdomadaire_une_fois_le_lundi(settings, broker):
    from tradinglab.monitoring.telegram_notifier import ALWAYS_KINDS, format_event

    o = _orch(settings, broker)
    lundi = datetime(2026, 9, 28, 7, 30, tzinfo=timezone.utc)      # 09:30 Paris
    o._maybe_weekly_report(lundi - timedelta(hours=2))              # 07:30 Paris : trop tôt
    assert not o.journal.read_day(kinds={"report_week"})
    o._maybe_weekly_report(lundi)
    o._maybe_weekly_report(lundi + timedelta(hours=1))
    ev = o.journal.read_day(kinds={"report_week"})
    assert len(ev) == 1 and ev[0]["week"] == "2026-S40" and "Semaine" in ev[0]["text"]
    assert "report_week" in ALWAYS_KINDS and "Rapport hebdomadaire" in format_event(ev[0])
