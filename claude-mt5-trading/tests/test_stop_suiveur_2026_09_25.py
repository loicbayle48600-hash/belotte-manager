"""Stop suiveur dès +1,05 R, toujours en bénéfice (demande utilisateur du 2026-09-25).

« Ajoute un stop loss suiveur : quand ça passe en positif à 1,05, rester toujours en bénéfice. » Le suivi démarre à
+1,05 R (il attendait +2 R) et ne descend jamais sous le break-even, qui couvre maintenant la commission : avec un
stop serré, 7 $/lot de commission dépassaient les 0,05 R d'offset et un trade sorti « au break-even » perdait."""
from __future__ import annotations

from tradinglab.core.types import Side
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager
from tests.test_position_watchdog_orchestrator import open_buy, pm_for

CFG = dict(break_even_r=1.0, break_even_requires_structure=False, break_even_offset_r=0.05,
           trailing_start_r=1.05, trailing_atr_multiplier=1.5, trailing_use_market_structure=False)


def _pm(broker, tmp_path, commission=0.0):
    pm, store = pm_for(broker, tmp_path)
    pm.cfg = PMConfig(**CFG)
    pm.commission_per_lot = (lambda asset_class: commission) if commission else None
    return pm, store


def _move(broker, pm, plan, pos, r, atr=0.0030):
    spec = broker.symbol_info("EURUSD")
    dist = plan.entry - plan.initial_sl
    broker.set_price("EURUSD", plan.entry + r * dist)
    pos = broker.position(pos.ticket)
    pm.manage(plan, pos, spec, MarketContext(atr=atr))
    return broker.position(pos.ticket)


def test_a_1_05_r_le_stop_est_en_benefice_puis_suit_le_prix(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pos, plan = open_buy(broker, store, sl_dist=0.0030)
    dist = plan.entry - plan.initial_sl
    pos = _move(broker, pm, plan, pos, 1.1)                      # +1,1 R au prix de marché (spread compris)
    assert plan.trailing_active
    assert pos.sl >= plan.entry + 0.05 * dist - 1e-9            # en bénéfice dès +1,05 R
    sl_1 = pos.sl
    pos = _move(broker, pm, plan, pos, 3.0)                      # le prix monte : le stop suit
    assert pos.sl > sl_1 and pos.sl >= plan.entry + 1.4 * dist
    sl_2 = pos.sl
    pos = _move(broker, pm, plan, pos, 2.0)                      # le prix recule : le stop ne recule jamais
    assert pos.sl == sl_2


def test_le_break_even_couvre_la_commission(broker, tmp_path):
    pm, store = _pm(broker, tmp_path, commission=7.0)
    pos, plan = open_buy(broker, store, sl_dist=0.0008)          # stop serré : 7 $/lot > 0,05 R
    spec = broker.symbol_info("EURUSD")
    com_price = 7.0 * spec.tick_size / spec.tick_value
    be = pm._be_level(plan, Side.BUY, spec)
    assert be == plan.entry + 0.05 * (plan.entry - plan.initial_sl) + com_price
    pos = _move(broker, pm, plan, pos, 1.5, atr=0.0030)
    assert pos.sl >= be - spec.tick_size


def test_sous_1_05_r_pas_de_suivi(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pos, plan = open_buy(broker, store, sl_dist=0.0030)
    _move(broker, pm, plan, pos, 0.9)
    assert not plan.trailing_active and not plan.break_even_done


def test_config_reelle_demarre_a_1_05(settings):
    assert float(settings.profit_management["trailing_start_r"]) == 1.05
