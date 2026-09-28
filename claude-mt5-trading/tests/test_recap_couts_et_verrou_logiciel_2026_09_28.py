"""Récap Telegram détaillé (brut / commission / spread / net) et verrou de profit logiciel (2026-09-28)."""
from __future__ import annotations

from datetime import datetime, timezone

from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore
from tradinglab.core.types import Deal, OrderRequest, Side
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager
from tradinglab.learning.post_trade import build_trade_record, close_costs
from tradinglab.monitoring.telegram_notifier import format_event

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _deal(entry, profit=0.0, commission=0.0, swap=0.0, volume=0.5, price=1.1, comment=""):
    return Deal(ticket=1, order=1, position_id=77, symbol="EURUSD", side=Side.BUY, volume=volume, price=price, profit=profit,
                commission=commission, swap=swap, time=NOW, magic=51000, entry=entry, comment=comment)


def test_couts_dans_le_record_et_le_recap():
    plan = BotPositionPlan(77, "EURUSD", "BUY", "B01", "c1", 1.1000, 1.0950, 0.5, 250.0, 0.05, opened_at=NOW.isoformat())
    deals = [_deal("IN", commission=-1.75), _deal("OUT", profit=60.0, commission=-1.75, swap=-0.4, comment="[tp]")]
    rec = build_trade_record(plan, deals, {"spread_points": 8}, NOW.isoformat(), point=0.00001, value_per_price=100000.0)
    assert close_costs(deals, 77) == {"brut": 60.0, "commission": -3.5, "swap": -0.4}
    assert rec.pnl == 56.1 and rec.features["brut"] == 60.0 and rec.features["commission"] == -3.5
    assert rec.features["spread_cost"] == 4.0                      # 8 points × 0,00001 × 100 000 $/unité × 0,5 lot
    txt = format_event({"kind": "post_trade_review", "ts_local": "2026-09-28T14:00:00", "symbol": "EURUSD", "side": "BUY",
                        "agent_id": "B01", "pnl": 56.1, "result_r": 0.22, "volume": 0.5, "exit_reason": "tp",
                        "brut": 60.0, "commission": -3.5, "swap": -0.4, "spread_cost": 4.0, "review": {"verdict": "OK"}})
    assert "brut 60" in txt and "commission -3.5" in txt and "swap -0.4" in txt and "spread à l'entrée ≈ 4" in txt
    assert "net <b>56.1 $</b>" in txt and "0.5 lot" in txt and "sortie tp" in txt


def test_recap_sans_couts_reste_lisible():
    txt = format_event({"kind": "post_trade_review", "ts_local": "2026-09-28T14:00:00", "symbol": "US500", "agent_id": "C03",
                        "pnl": -250.0, "result_r": -1.0, "review": {"verdict": "ERREUR"}})
    assert "net <b>-250 $</b>" in txt and "brut" not in txt


def _bid(broker, ticket, cible):
    """Amène le bid (prix courant d'un achat) exactement à `cible`, quel que soit le spread du courtier simulé."""
    broker.set_price("EURUSD", cible)
    ecart = cible - broker.position(ticket).price_current
    if abs(ecart) > 1e-9:
        broker.set_price("EURUSD", cible + ecart)


def _pm(broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.roll_day_if_needed(100000, 100000, NOW.date())
    store.state.update_equity(100000, 100000)
    return PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(), 51000), store


def test_verrou_logiciel_ferme_au_marche_quand_le_stop_broker_ne_peut_pas_tenir_le_verrou(broker, tmp_path):
    """USTEC 28/09 : le verrou (0,14 R) était trop près du prix pour le broker ; le prix est repassé dessous.
    Stop de 10 pips : la distance minimale du broker (2 pips) vaut 0,2 R, aucun stop en profit n'est plaçable à +0,165 R."""
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig(protect_profit_risk_ratio=0.16, protect_profit_lock_ratio=0.14)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.3, t.ask - 0.0010, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "B01", "c1", pos.price_open, pos.sl, pos.volume, 300.0, 0.3,
                           opened_at=NOW.isoformat(), last_sl=pos.sl, regime="TRENDING")
    store.state.bot_positions[str(pos.ticket)] = plan
    dist = pos.price_open - pos.sl
    spec = broker.symbol_info("EURUSD")
    _bid(broker, pos.ticket, pos.price_open + 0.165 * dist)             # protection déclenchée, aucun stop en profit plaçable
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert plan.trailing_forced and broker.position(pos.ticket).sl == plan.initial_sl, acts
    _bid(broker, pos.ticket, pos.price_open + 0.12 * dist)              # sous le verrou : fermeture au marché
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert any("verrou de profit : fermeture au marché" in a for a in acts) and broker.position(pos.ticket) is None, acts


def test_verrou_tenu_par_le_stop_broker_pas_de_fermeture(broker, tmp_path):
    pm, store = _pm(broker, tmp_path)
    pm.cfg = PMConfig(protect_profit_risk_ratio=0.16, protect_profit_lock_ratio=0.14)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.3, t.ask - 0.0100, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "B01", "c1", pos.price_open, pos.sl, pos.volume, 300.0, 0.3,
                           opened_at=NOW.isoformat(), last_sl=pos.sl, regime="TRENDING")
    store.state.bot_positions[str(pos.ticket)] = plan
    dist = pos.price_open - pos.sl
    spec = broker.symbol_info("EURUSD")
    broker.set_price("EURUSD", pos.price_open + 0.5 * dist)            # loin : le stop se pose au verrou
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert broker.position(pos.ticket).sl >= pos.price_open + 0.14 * dist - 1e-9
    sl = broker.position(pos.ticket).sl                                # le suivi (1,5 ATR) est même au-dessus du verrou
    broker.set_price("EURUSD", pos.price_open + 0.45 * dist)           # léger repli, au-dessus du stop : rien
    acts = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert broker.position(pos.ticket) is not None and broker.position(pos.ticket).sl == sl and not any("fermeture" in a for a in acts)
