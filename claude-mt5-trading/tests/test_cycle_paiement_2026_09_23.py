"""Cycle de paiement automatique FOXX (décision utilisateur 2026-09-23) : « je trade tous les jours et tous les
7 jours je retire tous mes bénéfices ; les comptes démo font comme un compte financé ».

- le cycle compte les journées de trading postérieures au dernier paiement ; 14 requises pour le premier
  paiement, 7 ensuite (`PayoutRules`) ;
- éligible = jours faits, solde > solde initial, cohérence du cycle ≤ 25 %, aucune violation ; FOXX exige
  aucune position ouverte à la demande ;
- fenêtre de paiement : entrées verrouillées (PAYOUT_WINDOW), positions fermées, puis retrait de tout le
  profit (plafond 15 % du solde initial). DEMO : retrait simulé (le solde vu par le labo baisse) ; compte
  financé : `payout_ready`, verrou jusqu'à PAYOUT_DONE <montant> ;
- cohérence non respectée → aucune fenêtre, on continue à trader (règle FOXX) ; la cohérence appliquée
  en direct (`19_prop_consistency`) ne regarde que les idées du cycle courant.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.state import StateStore, SystemState  # noqa: E402
from tradinglab.core.types import OrderKind, OrderRequest, Side  # noqa: E402

from test_prop_foxx_rules import guard  # noqa: E402
from test_risk_and_gate import FIXED_NOW  # noqa: E402


def _state(balance: float, initial: float = 500_000.0, days: int = 0) -> SystemState:
    st = SystemState()
    st.update_equity(initial, initial)
    st.equity = st.balance = balance
    st.trading_days = [f"2026-09-{d + 1:02d}" for d in range(days)]
    return st


def _idea(st, symbol, pnl, when, ticket):
    st.register_trade_idea(symbol, "BUY", 250.0, ticket=ticket, now=when)
    st.close_trade_idea_position(ticket, pnl, when + timedelta(minutes=30))


def test_statut_du_cycle_jours_profit_coherence(settings):
    pg = guard(settings)
    st = _state(510_000.0, days=10)
    _idea(st, "EURUSD", 6_000.0, FIXED_NOW, 1); _idea(st, "GBPUSD", 4_000.0, FIXED_NOW + timedelta(hours=1), 2)
    s = pg.payout_cycle_status(st, FIXED_NOW)
    assert s["trading_days_done"] == 10 and s["trading_days_required"] == 14 and s["days_remaining"] == 4
    assert s["cycle_profit"] == 10_000.0 and not s["eligible"] and "14 requis" in s["blocking_reasons"][0]
    st.trading_days = [f"2026-09-{d + 1:02d}" for d in range(14)]
    s = pg.payout_cycle_status(st, FIXED_NOW)
    assert not s["eligible"] and "cohérence" in s["blocking_reasons"][0], "EURUSD = 60 % du profit du cycle"
    for i in range(3, 9):                               # six autres idées gagnantes : plus aucune idée > 25 %
        _idea(st, f"PAIRE{i}", 4_000.0, FIXED_NOW + timedelta(hours=i), 10 + i)
    s = pg.payout_cycle_status(st, FIXED_NOW)
    assert s["eligible"] and s["ready"] and s["withdrawable"] == 10_000.0 and s["profit_split_percent"] == 70
    st.balance = 600_000.0                              # profit 100 k > plafond 15 % = 75 k
    assert pg.payout_cycle_status(st, FIXED_NOW)["withdrawable"] == 75_000.0
    # après un paiement : jours du cycle = ceux qui suivent, 7 requis, idées anciennes hors cohérence
    st.record_payout(75_000.0, FIXED_NOW.replace(year=2026, month=9, day=14), trading_days=14)
    st.balance = 500_000.0
    s = pg.payout_cycle_status(st, FIXED_NOW)
    assert s["trading_days_done"] == 0 and s["trading_days_required"] == 7 and s["payouts_done"] == 1
    assert pg.consistency_total_profit(st) == 0.0, "les idées d'avant le paiement ne comptent plus"
    assert s["profit_split_percent"] == 80


def test_etat_persiste_le_cycle(tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.record_payout(1234.5, FIXED_NOW, simulated=True, trading_days=14)
    store.save()
    relu = StateStore(tmp_path / "state").state
    assert relu.payouts[0]["amount"] == 1234.5 and relu.payout_cycle_started_at == FIXED_NOW.isoformat()


def test_demo_traite_comme_finance_retrait_simule_automatique(settings, broker):
    """Orchestrateur : 14 jours faits, profit, cohérence OK, une position ouverte → fenêtre (verrou + fermeture),
    puis retrait simulé de tout le profit, paiement enregistré, verrou levé, nouveau cycle."""
    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    st = o.state
    for t in list(st.bot_positions):
        st.bot_positions.pop(t)
    for p in broker.positions(magic=settings.magic):
        broker.close_position(p.ticket)
    initial = st.initial_balance
    now = broker.now()
    for i in range(8):                                  # 8 idées gagnantes : aucune > 25 %
        _idea(st, f"P{i}", 1_000.0, now - timedelta(hours=8 - i), 500 + i)
    st.trading_days = [(now - timedelta(days=20 - d)).date().isoformat() for d in range(14)]
    broker.balance = initial + 8_000.0                  # le broker montre le profit
    st.update_equity(broker.balance, broker.balance)
    tick = broker.tick("EURUSD")
    res = broker.order_send(OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.1, sl=round(tick.ask - 0.02, 5), tp=0.0,
                                         kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:X"))
    o.pm.sync()
    assert str(res.ticket) in st.bot_positions
    o._maybe_payout(now)
    assert "PAYOUT_WINDOW" in st.lock_reasons and st.payout_window_since
    assert broker.position(res.ticket) is None, "FOXX : aucune position ouverte à la demande"
    ev = o.journal.read_day(kinds={"payout_window"})
    assert ev and ev[-1]["eligible"] is True
    # cycle suivant : à plat → retrait simulé de tout le profit
    o.pm.sync()
    o._maybe_payout(now + timedelta(seconds=15))
    assert st.payouts and st.payouts[0]["simulated"] is True and st.payouts[0]["amount"] == pytest.approx(8_000.0)
    assert st.simulated_withdrawn_total() == pytest.approx(8_000.0)
    assert "PAYOUT_WINDOW" not in st.lock_reasons and st.payout_window_since == ""
    done = o.journal.read_day(kinds={"payout_done"})
    assert done and done[-1]["trader_share"] == pytest.approx(5_600.0) and done[-1]["profit_split_percent"] == 70
    s = o.prop.payout_cycle_status(st, now + timedelta(seconds=20))
    assert s["trading_days_done"] == 0 and s["trading_days_required"] == 7 and s["cycle_profit"] == pytest.approx(0.0)


def test_compte_finance_attend_payout_done(settings, broker):
    from tradinglab.core.types import TradeMode

    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    st = o.state
    for t in list(st.bot_positions):
        st.bot_positions.pop(t)
    for p in broker.positions(magic=settings.magic):
        broker.close_position(p.ticket)
    now = broker.now()
    for i in range(8):
        _idea(st, f"P{i}", 1_000.0, now - timedelta(hours=8 - i), 600 + i)
    st.trading_days = [(now - timedelta(days=20 - d)).date().isoformat() for d in range(14)]
    st.balance = st.equity = st.initial_balance + 8_000.0
    st.account_trade_mode = TradeMode.REAL.value       # compte financé : le retrait est une démarche humaine
    o._maybe_payout(now)
    o._maybe_payout(now + timedelta(seconds=15))
    assert st.payout_window_since == "REAL_PENDING" and "PAYOUT_WINDOW" in st.lock_reasons and not st.payouts
    assert o.journal.read_day(kinds={"payout_ready"})
    o._maybe_payout(now + timedelta(seconds=30))       # pas de doublon d'événement
    assert len(o.journal.read_day(kinds={"payout_ready"})) == 1
    res = o.handle_command("PAYOUT_DONE", {"target": "8000"}, "cli")
    assert res["ok"] and st.payouts[0]["simulated"] is False and st.payouts[0]["amount"] == 8_000.0
    assert "PAYOUT_WINDOW" not in st.lock_reasons and st.payout_window_since == ""


def test_coherence_non_respectee_pas_de_fenetre(settings, broker):
    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    st = o.state
    now = broker.now()
    _idea(st, "EURUSD", 8_000.0, now - timedelta(hours=2), 700)          # une seule idée = 100 %
    st.trading_days = [(now - timedelta(days=20 - d)).date().isoformat() for d in range(14)]
    st.balance = st.equity = st.initial_balance + 8_000.0
    o._maybe_payout(now)
    assert "PAYOUT_WINDOW" not in st.lock_reasons and not st.payout_window_since and not st.payouts
    s = o.prop.payout_cycle_status(st, now)
    assert not s["eligible"] and "cohérence" in s["blocking_reasons"][0]
