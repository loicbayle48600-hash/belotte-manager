"""Break-even obligatoire dès qu'un profit partiel est encaissé.

Constaté sur le compte réel le 2026-09-21, XRPUSD :

    23:28  TP1  300 unités à 1.4102   +2,49 $
    02:10  SL   700 unités à 1.3965   -3,78 $
                                 net  -1,29 $

TP1 ferme 30 % de la position à 1,5R, soit +0,45R encaissés, et laisse **70 % au risque
plein** (-0,70R). Tant que le stop reste à son niveau initial, un retour au stop produit
donc une perte nette de 0,25R **malgré un gain déjà pris**. C'est arithmétique, pas de la
malchance.

`break_even_requires_structure` garde tout son sens AVANT le premier profit : il évite de
resserrer sur du bruit et de se faire sortir prématurément. Après TP1, il n'a plus lieu
d'être — le trade doit devenir sans risque.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.types import Side  # noqa: E402
from tradinglab.execution.position_manager import MarketContext  # noqa: E402

from test_position_watchdog_orchestrator import open_buy, pm_for  # noqa: E402


def _monter_a(broker, plan, pos, facteur: float):
    """Place le prix à `facteur` x R au-dessus de l'entrée.

    La distance vient du plan (`entry` / `initial_sl`) et non du stop courant : une fois le
    break-even armé, le stop a bougé et recalculer depuis lui fausserait complètement l'échelle.
    """
    dist = plan.entry - plan.initial_sl
    broker.set_price(pos.symbol, plan.entry + facteur * dist)
    return broker.position(pos.ticket)


def test_avant_tp1_la_structure_reste_exigee(broker, tmp_path):
    """Comportement inchangé : à 1,3R sans structure, pas de break-even."""
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    pos = _monter_a(broker, plan, pos, 1.3)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert plan.tp1_done is False
    assert plan.break_even_done is False, "sans profit encaissé, l'exigence de structure protège du bruit"


def test_apres_tp1_le_break_even_est_force_sans_structure(broker, tmp_path):
    """Cœur du correctif : TP1 pris, donc stop remonté même si la structure ne valide pas."""
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    sl_initial = plan.initial_sl

    pos = _monter_a(broker, plan, pos, 1.6)                     # au-delà de TP1 (1,5R)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert plan.tp1_done is True, "TP1 doit être exécuté à 1,6R"

    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    pos = broker.position(pos.ticket)
    assert plan.break_even_done is True, "après TP1, le break-even ne dépend plus de la structure"
    assert pos.sl > sl_initial, "le stop doit avoir remonté au-dessus de son niveau initial"
    assert pos.sl >= plan.entry, "le stop doit atteindre au moins le prix d'entrée"


def test_le_stop_ne_redescend_jamais(broker, tmp_path):
    """Garde-fou `never_widen_stop` : une fois remonté, le stop ne recule pas."""
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    pos = _monter_a(broker, plan, pos, 1.6)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    sl_apres_be = broker.position(pos.ticket).sl

    pos = _monter_a(broker, plan, pos, 1.2)                     # le prix reflue mais reste au-dessus du BE
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    assert broker.position(pos.ticket).sl == pytest.approx(sl_apres_be), "le stop ne doit pas redescendre"


def test_scenario_xrp_devient_gagnant(broker, tmp_path):
    """Rejoue le scénario réel : TP1 puis retour au niveau d'entrée.

    Avant le correctif la position finissait au stop initial (perte nette). Désormais elle
    sort au break-even : le gain de TP1 est conservé.
    """
    pm, store = pm_for(broker, tmp_path)
    pos, plan = open_buy(broker, store)
    spec = broker.symbol_info("EURUSD")
    pos = _monter_a(broker, plan, pos, 1.7)                     # max_r observé sur XRPUSD
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=0.0012, structure_ok=False))

    pos = broker.position(pos.ticket)
    assert plan.tp1_done and plan.break_even_done
    assert pos.sl >= plan.entry, "le reste de la position ne peut plus perdre par rapport à l'entrée"
