"""Règles FOXX Funded relevées le 2026-09-23 dans la FAQ et la page « Ce qu'on n'autorise pas » (collées par
l'utilisateur) et appliquées en direct.

**Taille de lots maximale « pour le respect de la cohérence »** : table par taille de compte et classe
d'actifs (500 000 $ : forex 10 lots, matières premières 3, indices 6, crypto 3). Le plafond vaut pour les
lots ouverts d'une même idée (même symbole, même sens) : le gate rabote le volume au reste disponible et
refuse quand il n'y a plus de reste.

**Cohérence 25 %** : aucune idée de trade ne peut représenter plus de 25 % du profit total du cycle.
Décision utilisateur : l'appliquer en direct dès que le profit net atteint 1 % du solde initial — en
dessous, la première idée gagnante pèse mécaniquement 100 % et FOXX demande simplement de continuer.
Le gain visé d'une nouvelle idée (risque × R du dernier TP) est plafonné à « profit des autres idées / 3 »
(le risque est réduit d'autant) ; une idée ouverte qui atteint ce plafond est fermée.

**Hedging interdit**, **pas d'inversion immédiate après une perte** (politique de jeu : 30 min de délai
avant l'autre sens sur le même instrument), **durée minimale** : aucune sortie anticipée sur invalidation
dans la première minute (le SL broker reste actif).
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.state import BotPositionPlan  # noqa: E402
from tradinglab.execution.gate import ExecutionGate  # noqa: E402
from tradinglab.risk.correlation_guard import CorrelationGuard, CorrelationLimits  # noqa: E402
from tradinglab.risk.daily_guard import DailyGuard  # noqa: E402
from tradinglab.risk.prop_guard import PropGuard, PropProfile  # noqa: E402
from tradinglab.risk.risk_manager import RiskLimits, RiskManager  # noqa: E402

from test_prop_foxx_rules import guard  # noqa: E402
from test_risk_and_gate import FIXED_NOW, ctx_for, make_candidate, make_state  # noqa: E402


def _checks(res):
    return {c.name: c for c in res.checks}


def _gate(settings, broker, **over):
    prof = PropProfile.from_config({**settings.prop, **over})
    prop = PropGuard(prof, True, settings.autonomous_prop, 1.0)
    return ExecutionGate(broker, RiskManager(RiskLimits.from_config(settings.risk)), prop,
                         DailyGuard(settings.risk, settings.daily_profit),
                         CorrelationGuard(CorrelationLimits.from_config(settings.correlation))), prop


def _plan(ticket, symbol, side, volume=0.1, opened_at=""):
    return BotPositionPlan(ticket=ticket, symbol=symbol, side=side, agent_id="X", candidate_id="c", entry=1.1,
                           initial_sl=1.09, initial_volume=volume, initial_risk_money=50.0, risk_percent=0.05,
                           opened_at=opened_at)


# ------------------------------------------------------------------ table de lots
#: table FOXX relevée le 2026-09-23 (désactivée dans la config du dépôt depuis le 2026-09-25, testée ici explicitement)
TABLE_FOXX = {5000: {"forex": 0.5, "commodities": 0.15, "indices": 0.38, "crypto": 0.2},
              10000: {"forex": 1.0, "commodities": 0.3, "indices": 0.75, "crypto": 0.38},
              100000: {"forex": 10.0, "commodities": 3.0, "indices": 6.0, "crypto": 3.0},
              500000: {"forex": 10.0, "commodities": 3.0, "indices": 6.0, "crypto": 3.0}}


def test_table_de_lots_par_taille_de_compte_et_classe(settings):
    pg = guard(settings, max_lots_by_account_size=TABLE_FOXX)   # profil 500 000 $
    assert [pg.max_lots(c) for c in ("forex", "metals", "energy", "indices", "crypto")] == [10.0, 3.0, 3.0, 6.0, 3.0]
    assert pg.max_lots("classe_inconnue") == 3.0, "sous-jacent inconnu : colonne la plus stricte"
    petit = guard(settings, account_size=7_000, max_lots_by_account_size=TABLE_FOXX)   # la plus grande ligne <= taille
    assert [petit.max_lots(c) for c in ("forex", "metals", "indices", "crypto")] == [0.5, 0.15, 0.38, 0.2]
    assert guard(settings, account_size=100_000, max_lots_by_account_size=TABLE_FOXX).max_lots("forex") == 10.0
    assert guard(settings, max_lots_by_account_size={}).max_lots("forex") is None


def test_le_gate_rabote_le_volume_au_plafond_de_lots(settings, broker):
    """Plafond forex ramené à ~60 % du volume voulu : le volume est raboté, le trade reste utile (> plancher de 20 %),
    `volume_capped` levé. Le plafond dérive du volume voulu pour ne pas dépendre de `risk_per_trade_percent`."""
    c, atr = make_candidate(broker)
    libre, _ = _gate(settings, broker)
    voulu = libre.evaluate(ctx_for(broker, c, make_state(), atr))[1].volume
    cap = round(voulu * 0.6, 2)
    table = {500000: {"forex": cap, "commodities": 3.0, "indices": 6.0, "crypto": 3.0}}
    gate, _ = _gate(settings, broker, max_lots_by_account_size=table)
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    chk = _checks(res)["19_prop_max_lots"]
    assert chk.ok and f"max {cap:g} lots" in chk.detail
    assert res.volume_wanted > cap and req is not None and req.volume == pytest.approx(cap) and res.volume_capped


def test_le_gate_refuse_quand_l_idee_a_deja_ses_lots(settings, broker):
    table = {500000: {"forex": 0.10, "commodities": 3.0, "indices": 6.0, "crypto": 3.0}}
    gate, _ = _gate(settings, broker, max_lots_by_account_size=table)
    c, atr = make_candidate(broker)
    st = make_state()
    st.bot_positions["1"] = _plan(1, "EURUSD", "BUY", volume=0.10)
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    chk = _checks(res)["19_prop_max_lots"]
    assert not chk.ok and "reste 0" in chk.detail and not res.approved


# ------------------------------------------------------------------ hedging / inversion
def test_hedging_refuse_meme_si_plusieurs_positions_par_symbole(settings, broker):
    gate, prop = _gate(settings, broker)
    c, atr = make_candidate(broker)                              # EURUSD BUY
    st = make_state()
    st.bot_positions["7"] = _plan(7, "EURUSD", "SELL")
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    chk = _checks(res)["19_prop_no_hedge"]
    assert not chk.ok and "opposée" in chk.detail and not res.approved
    st.bot_positions["7"].symbol = "GBPUSD"                     # autre instrument : pas un hedge
    assert _checks(gate.evaluate(ctx_for(broker, c, st, atr))[0])["19_prop_no_hedge"].ok
    assert prop.hedge_check(st, "EURUSD", "BUY").ok


def test_pas_d_inversion_dans_les_30_minutes_apres_une_perte(settings, broker):
    # contrôle désactivé dans la config du dépôt depuis le 2026-09-25 : testé ici avec 30 min
    gate, prop = _gate(settings, broker, reversal_after_loss_cooldown_minutes=30)
    c, atr = make_candidate(broker)                              # EURUSD BUY
    st = make_state()
    st.register_trade_idea("EURUSD", "SELL", 250.0, ticket=1, now=FIXED_NOW - timedelta(minutes=40))
    st.close_trade_idea_position(1, -300.0, FIXED_NOW - timedelta(minutes=10))     # perte SELL il y a 10 min
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    chk = _checks(res)["19_prop_no_reversal"]
    assert not chk.ok and "inversion refusée" in chk.detail and not res.approved
    assert prop.reversal_check(st, "EURUSD", "BUY", FIXED_NOW + timedelta(minutes=25)).ok, "35 min après : autorisé"
    assert prop.reversal_check(st, "EURUSD", "SELL", FIXED_NOW).ok, "même sens : pas une inversion"
    st2 = make_state()
    st2.register_trade_idea("EURUSD", "SELL", 250.0, ticket=2, now=FIXED_NOW - timedelta(minutes=40))
    st2.close_trade_idea_position(2, +300.0, FIXED_NOW - timedelta(minutes=10))     # gain : pas de « récupération »
    assert prop.reversal_check(st2, "EURUSD", "BUY", FIXED_NOW).ok
    assert guard(settings, reversal_after_loss_cooldown_minutes=0).reversal_check(st, "EURUSD", "BUY", FIXED_NOW).ok


# ------------------------------------------------------------------ cohérence 25 %
def _ideas(st, pnls, symbol="GBPUSD"):
    for i, pnl in enumerate(pnls, start=1):
        st.register_trade_idea(symbol, "BUY", 250.0, ticket=100 + i, now=FIXED_NOW - timedelta(hours=10 - i))
        st.close_trade_idea_position(100 + i, pnl, FIXED_NOW - timedelta(hours=10 - i, minutes=-30))


def test_coherence_non_appliquee_sous_le_seuil_de_profit(settings, broker):
    gate, prop = _gate(settings, broker)
    c, atr = make_candidate(broker)
    st = make_state()                                            # solde 100 000 → seuil 0,3 % = 300 $ (2026-09-24, 1 % avant)
    _ideas(st, [150.0, 100.0])                                   # net 250 $ : sous le seuil
    assert prop.consistency_gain_cap(st, "EURUSD", "BUY", FIXED_NOW) is None
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    assert _checks(res)["19_prop_consistency"].ok and "non applicable" in _checks(res)["19_prop_consistency"].detail


def test_coherence_reduit_le_risque_d_une_nouvelle_idee(settings, broker):
    """Profit net 12 000 $ (autres idées) → une nouvelle idée ne peut viser plus de 4 000 $. À 2,5 R le risque voulu
    (0,25 % = 250 $ → 625 $) reste sous le plafond ; à 25 R (6 250 $) il est réduit à 160 $."""
    gate, prop = _gate(settings, broker)
    st = make_state()
    _ideas(st, [5_000.0, 4_000.0, 3_000.0])                      # net 12 000 ≥ 1 % de 100 000
    assert prop.consistency_gain_cap(st, "EURUSD", "BUY", FIXED_NOW) == pytest.approx(4_000.0)
    c, atr = make_candidate(broker, rr=2.5)
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    assert _checks(res)["19_prop_consistency"].ok and "sous le plafond" in _checks(res)["19_prop_consistency"].detail
    c2, atr = make_candidate(broker, rr=60.0)
    res2, _ = gate.evaluate(ctx_for(broker, c2, st, atr))
    chk = _checks(res2)["19_prop_consistency"]
    assert chk.ok and "risque réduit" in chk.detail
    assert res2.risk_money <= 4_000.0 / 60.0 + 1e-6              # gain max 4 000 $ à 60 R → risque ≤ 67 $


def test_coherence_refuse_quand_tout_le_profit_vient_de_l_idee_en_cours(settings, broker):
    gate, prop = _gate(settings, broker)
    st = make_state()
    st.register_trade_idea("EURUSD", "BUY", 250.0, ticket=1, now=FIXED_NOW - timedelta(minutes=2))
    st.close_trade_idea_position(1, 2_000.0, FIXED_NOW - timedelta(minutes=1))     # idée encore active (< 10 min)
    assert prop.consistency_gain_cap(st, "EURUSD", "BUY", FIXED_NOW) == 0.0
    c, atr = make_candidate(broker)
    res, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    assert not _checks(res)["19_prop_consistency"].ok and not res.approved
    # une autre idée, elle, dispose de 2 000 / 3
    assert prop.consistency_gain_cap(st, "GBPUSD", "BUY", FIXED_NOW) == pytest.approx(2_000.0 / 3.0)


def test_coherence_desactivee_ne_change_rien(settings, broker):
    _, prop = _gate(settings, broker, consistency_enforced=False)
    st = make_state()
    _ideas(st, [5_000.0])
    assert prop.consistency_gain_cap(st, "EURUSD", "BUY", FIXED_NOW) is None


def test_idee_ouverte_fermee_au_plafond_de_coherence(settings, broker):
    """Orchestrateur : profit net 3 000 $ sur d'autres idées → plafond 1 000 $ ; une position dont le gain
    flottant atteint 1 000 $ est fermée (`consistency_cap_close`), à 200 $ elle reste ouverte."""
    from tradinglab.core.types import OrderKind, OrderRequest, Side

    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    st = o.state
    for t in list(st.bot_positions):                              # on ne juge que la position du test
        st.bot_positions.pop(t)
    _ideas(st, [1_500.0, 1_500.0])
    tick = broker.tick("EURUSD")
    res = broker.order_send(OrderRequest(symbol="EURUSD", side=Side.BUY, volume=1.0, sl=round(tick.ask - 0.02, 5),
                                         tp=0.0, kind=OrderKind.MARKET, magic=settings.magic, comment="TLAB:X"))
    assert res.ok
    o.pm.sync()
    assert str(res.ticket) in st.bot_positions
    cap = o.prop.consistency_gain_cap(st, "EURUSD", "BUY", broker.now())
    assert cap == pytest.approx(1_000.0)
    broker.set_price("EURUSD", tick.bid + 0.002)                 # ≈ +200 $ sur 1 lot : sous le plafond
    o._manage_positions()
    assert broker.position(res.ticket) is not None
    broker.set_price("EURUSD", tick.bid + 0.011)                 # ≈ +1 100 $ : plafond atteint
    o._manage_positions()
    assert broker.position(res.ticket) is None
    ev = [e for e in o.journal.read_day(kinds={"consistency_cap_close"})]
    assert ev and ev[-1]["ok"] is True and ev[-1]["cap"] == pytest.approx(1000.0) and ev[-1]["gain"] >= 1000.0


def test_pas_de_sortie_anticipee_dans_la_premiere_minute(settings, broker):
    from tradinglab.orchestration.orchestrator import MIN_HOLD_SEC

    from test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    plan = _plan(1, "EURUSD", "BUY", opened_at=(broker.now() - timedelta(seconds=30)).isoformat())
    assert MIN_HOLD_SEC == 60.0 and o._position_age_sec(plan) < MIN_HOLD_SEC
    plan.opened_at = (broker.now() - timedelta(seconds=90)).isoformat()
    assert o._position_age_sec(plan) >= MIN_HOLD_SEC
    plan.opened_at = "date illisible"
    assert o._position_age_sec(plan) == float("inf"), "date illisible : jamais bloquant"


# ------------------------------------------------------------------ commission dans le coût d'entrée
def test_la_commission_prop_s_ajoute_au_spread_dans_le_cout_d_entree(settings, broker):
    """FOXX : 7 $/lot forex. Sur EURUSD (tick 0,00001 = 1 $/lot) = 0,7 pip ajouté au spread dans `07b_spread_vs_sl`.
    Une commission énorme (profil de test) fait basculer le contrôle ; 0 $ (indices) ne change rien."""
    gate, prop = _gate(settings, broker)
    assert prop.commission_per_lot("forex") == 7.0 and prop.commission_per_lot("indices") == 0.0
    assert prop.commission_per_lot("metals") == 7.0 and prop.commission_per_lot("crypto") == 3.0
    assert prop.commission_per_lot("inconnue") == 7.0, "classe inconnue : la plus chère"
    c, atr = make_candidate(broker)
    res, _ = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    chk = _checks(res)["07b_spread_vs_sl"]
    assert chk.ok and "commission" in chk.detail
    # sans commission : la part du spread seule ; avec : strictement plus (0,7 pip sur 18 pips de stop ≈ 4 %)
    gate0, _ = _gate(settings, broker, commissions_per_lot={"forex": 0.0})
    chk0 = _checks(gate0.evaluate(ctx_for(broker, c, make_state(), atr))[0])["07b_spread_vs_sl"]
    part = int(chk.detail.split("=")[1].split("%")[0]); part0 = int(chk0.detail.split("=")[1].split("%")[0])
    assert part > part0 and "commission 0 %" in chk0.detail
    # commission délirante : le coût d'entrée dévore le risque → refus
    gate_x, _ = _gate(settings, broker, commissions_per_lot={"forex": 700.0})
    chk_x = _checks(gate_x.evaluate(ctx_for(broker, c, make_state(), atr))[0])["07b_spread_vs_sl"]
    assert not chk_x.ok
