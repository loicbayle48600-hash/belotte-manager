"""Simulation de paiement : éligibilité, plafond, partage, effet sur les planchers.

Le point vérifié en priorité : avec un drawdown **statique**, un retrait fait baisser
le solde sans faire baisser le plancher, donc il réduit le coussin d'exactement le
montant retiré. L'hypothèse alternative (plancher recalé) est calculée en parallèle
mais n'est jamais présentée comme la règle réelle.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.state import SystemState  # noqa: E402
from tradinglab.risk.payout import PayoutRules, simulate_payout  # noqa: E402
from tradinglab.risk.prop_guard import PropProfile  # noqa: E402


def _profile(settings) -> PropProfile:
    return PropProfile.from_config(settings.prop)


def _state(balance: float, initial: float = 500_000.0, trading_days: int = 0) -> SystemState:
    st = SystemState()
    st.update_equity(initial, initial)          # fige initial_balance à la première synchronisation
    st.equity = balance
    st.balance = balance
    st.trading_days = [f"2026-09-{d + 1:02d}" for d in range(trading_days)]
    return st


def test_sans_profit_non_eligible(settings):
    r = simulate_payout(_state(500_000.0, trading_days=20), _profile(settings))
    assert r["eligible"] is False
    assert any("aucun profit" in m for m in r["blocking_reasons"])
    assert r["max_withdrawable"] == 0.0


def test_jours_de_trading_insuffisants(settings):
    r = simulate_payout(_state(530_000.0, trading_days=3), _profile(settings))
    assert r["eligible"] is False
    assert any("3 jour(s) de trading sur 14 requis" in m for m in r["blocking_reasons"])


def test_eligible_et_plafond_15_pourcent(settings):
    # profit de 100 000 mais plafond à 15 % du solde initial = 75 000
    r = simulate_payout(_state(600_000.0, trading_days=14), _profile(settings))
    assert r["eligible"] is True and r["blocking_reasons"] == []
    assert r["profit"] == pytest.approx(100_000.0)
    assert r["withdrawal_cap"] == pytest.approx(75_000.0)
    assert r["max_withdrawable"] == pytest.approx(75_000.0)


def test_profit_inferieur_au_plafond(settings):
    r = simulate_payout(_state(520_000.0, trading_days=14), _profile(settings))
    assert r["max_withdrawable"] == pytest.approx(20_000.0)   # le profit, pas le plafond


def test_montant_superieur_au_maximum_est_bloquant(settings):
    r = simulate_payout(_state(520_000.0, trading_days=14), _profile(settings), amount=50_000.0)
    assert r["eligible"] is False
    assert any("maximum retirable" in m for m in r["blocking_reasons"])


def test_partage_des_profits_par_rang(settings):
    rules = PayoutRules.from_profile(_profile(settings))
    assert [rules.split_percent(i) for i in (0, 1, 2, 5)] == [70, 80, 90, 90]
    assert rules.trading_days_required(0) == 14 and rules.trading_days_required(1) == 7
    r = simulate_payout(_state(520_000.0, trading_days=14), _profile(settings), amount=10_000.0)
    assert r["profit_split_percent"] == 70
    assert r["trader_share"] == pytest.approx(7_000.0) and r["firm_share"] == pytest.approx(3_000.0)


def test_retrait_reduit_le_coussin_en_drawdown_statique(settings):
    """Cœur du sujet : le plancher ne bouge pas, donc le coussin baisse du montant retiré."""
    profile = _profile(settings)
    st = _state(600_000.0, trading_days=14)
    avant = simulate_payout(st, profile, amount=0.0)["after"]["static"]
    apres = simulate_payout(st, profile, amount=75_000.0)["after"]["static"]

    assert apres["overall_floor"] == avant["overall_floor"]          # plancher ancré sur le solde initial
    assert apres["overall_floor"] == pytest.approx(460_000.0)        # 500 000 - 8 %
    assert apres["balance"] == pytest.approx(525_000.0)
    perte_de_coussin = avant["buffer_to_overall_floor"] - apres["buffer_to_overall_floor"]
    assert perte_de_coussin == pytest.approx(75_000.0)
    assert apres["violates_overall_floor"] is False


def test_recalage_apres_retrait_beneficiaire_reduit_le_coussin(settings):
    """Après un retrait bénéficiaire, recaler le plancher le REMONTE : c'est la pire hypothèse."""
    r = simulate_payout(_state(600_000.0, trading_days=14), _profile(settings), amount=75_000.0)
    static, rebased = r["after"]["static"], r["after"]["rebased"]
    assert static["overall_floor"] == pytest.approx(460_000.0)       # 500 000 - 8 %
    assert rebased["overall_floor"] == pytest.approx(483_000.0)      # 525 000 - 8 %
    assert rebased["buffer_to_overall_floor"] < static["buffer_to_overall_floor"]
    # le rapport retient la moins favorable des deux et signale l'inconnu
    assert r["worst_case"] == "rebased" and r["floor_basis_assumed"] == "rebased"
    assert r["floor_rebase_rule"] == "UNKNOWN"


def test_worst_case_bascule_sur_static_en_drawdown(settings):
    """En perte, le recalage abaisse le plancher : c'est alors `static` la pire hypothèse."""
    r = simulate_payout(_state(470_000.0, trading_days=14), _profile(settings), amount=0.0)
    assert r["after"]["rebased"]["overall_floor"] < r["after"]["static"]["overall_floor"]
    assert r["worst_case"] == "static"


def test_retrait_du_capital_violerait_le_plancher(settings):
    """Retirer 15 % sans avoir fait de profit ferait passer sous le plancher des 8 %."""
    r = simulate_payout(_state(500_000.0, trading_days=14), _profile(settings), amount=75_000.0)
    assert r["eligible"] is False
    assert r["after"]["static"]["violates_overall_floor"] is True
    assert r["after"]["static"]["balance"] == pytest.approx(425_000.0)


def test_simulation_ne_modifie_pas_l_etat(settings):
    st = _state(600_000.0, trading_days=14)
    avant = (st.balance, st.equity, st.initial_balance, list(st.trading_days))
    simulate_payout(st, _profile(settings), amount=50_000.0)
    assert (st.balance, st.equity, st.initial_balance, list(st.trading_days)) == avant
