"""Retrait simulé : décalage du solde côté labo, compte DEMO uniquement.

MT5 n'autorise aucune opération de solde sur un compte démo. Le labo retranche donc
le montant retiré de l'equity et du solde qu'il utilise, pour que le bot se comporte
comme après un paiement (sizing, planchers prop, Daily Guard).

Deux invariants tenus par ces tests :
- un retrait n'est **pas** une perte de trading : le Daily Guard ne doit pas le voir ;
- le **solde initial** ne bouge jamais : le drawdown prop reste statique.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.state import SystemState  # noqa: E402

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def _synced(balance: float = 500_000.0) -> SystemState:
    st = SystemState()
    st.update_equity(balance, balance)
    st.daily.starting_equity = balance
    st.daily.starting_balance = balance
    st.daily.reference_equity = balance
    return st


def test_retrait_diminue_solde_et_equity():
    st = _synced()
    rec = st.register_simulated_withdrawal(20_000.0, NOW)
    assert rec["amount"] == pytest.approx(20_000.0) and rec["ts"].startswith("2026-09-21")
    assert st.balance == pytest.approx(480_000.0)
    assert st.equity == pytest.approx(480_000.0)
    assert st.simulated_withdrawn_total() == pytest.approx(20_000.0)


def test_solde_initial_jamais_modifie():
    """Le drawdown prop est statique : la base des 4 % / 8 % ne suit pas le retrait."""
    st = _synced()
    st.register_simulated_withdrawal(50_000.0, NOW)
    assert st.initial_balance == pytest.approx(500_000.0)
    assert st.prop_reference_balance() == pytest.approx(500_000.0)
    assert st.prop_overall_floor(8.0) == pytest.approx(460_000.0)


def test_retrait_n_est_pas_compte_comme_une_perte_du_jour():
    """Sans décalage des références, 20 000 retirés seraient vus comme 4 % de perte."""
    st = _synced()
    st.register_simulated_withdrawal(20_000.0, NOW)
    assert st.daily_pnl() == pytest.approx(0.0)
    assert st.prop_daily_loss_percent() == pytest.approx(0.0)
    assert st.daily.reference_equity == pytest.approx(480_000.0)


def test_perte_de_trading_apres_retrait_reste_mesuree():
    st = _synced()
    st.register_simulated_withdrawal(20_000.0, NOW)
    st.update_equity(475_000.0, 480_000.0)      # le broker n'a pas bougé : 5 000 de perte flottante
    assert st.equity == pytest.approx(455_000.0)
    assert st.daily_pnl() == pytest.approx(-25_000.0)


def test_le_decalage_survit_a_une_resynchronisation():
    """update_equity relit le broker : le retrait doit rester appliqué."""
    st = _synced()
    st.register_simulated_withdrawal(30_000.0, NOW)
    st.update_equity(500_000.0, 500_000.0)      # MT5 affiche toujours 500 000
    assert st.balance == pytest.approx(470_000.0)
    assert st.broker_balance == pytest.approx(500_000.0)   # la vérité broker reste visible


def test_retraits_cumules_et_reset():
    st = _synced()
    st.register_simulated_withdrawal(10_000.0, NOW)
    st.register_simulated_withdrawal(15_000.0, NOW)
    assert st.simulated_withdrawn_total() == pytest.approx(25_000.0)
    st.update_equity(500_000.0, 500_000.0)
    assert st.balance == pytest.approx(475_000.0)
    assert st.clear_simulated_withdrawals() == 2
    st.update_equity(500_000.0, 500_000.0)
    assert st.balance == pytest.approx(500_000.0) and st.simulated_withdrawn_total() == 0.0


def test_montant_nul_ou_negatif_refuse():
    st = _synced()
    for bad in (0.0, -5.0):
        with pytest.raises(ValueError):
            st.register_simulated_withdrawal(bad, NOW)
    assert st.simulated_withdrawals == []


def test_solde_initial_fige_sur_le_broker_apres_redemarrage():
    """Un état rechargé avec des retraits ne doit pas figer initial_balance sur le solde décalé."""
    st = SystemState()
    st.simulated_withdrawals = [{"ts": NOW.isoformat(), "amount": 40_000.0}]
    st.update_equity(500_000.0, 500_000.0)      # première synchronisation APRÈS un retrait
    assert st.initial_balance == pytest.approx(500_000.0)   # pas 460 000
    assert st.balance == pytest.approx(460_000.0)
