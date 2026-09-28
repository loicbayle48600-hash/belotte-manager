"""Décisions du 28/09 au soir (revue par agents + accord utilisateur « tout sauf 5 ») : corrections A1–A6 et mesures C."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import yaml

from tradinglab.core.journal import Journal
from tradinglab.core.state import BotPositionPlan, StateStore, SystemState
from tradinglab.core.types import OrderRequest, Side, Verdict
from tradinglab.copy.copier import COPY_COMMENT_PREFIX, plan_sync
from tradinglab.execution.position_manager import MarketContext, PMConfig, PositionManager
from tradinglab.orchestration.orchestrator import Orchestrator
from tradinglab.shadow.shadow import ShadowTrader

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _cand(agent="B02", symbol="XAUUSD", key="k1", tfs=("M15", "H1"), version="1.0"):
    return SimpleNamespace(id="c_" + key, agent_id=agent, agent_version=version, symbol=symbol, side=Side.SELL, entry=3800.0, sl=3810.0,
                           tp_plan=[3780.0], sl_distance=10.0, regime=SimpleNamespace(value="TRENDING"), session="LONDON",
                           setup_score=70.0, bar_time="2026-09-28T11:45:00+00:00", idempotency_key=key, timeframes=list(tfs))


# ---------------- A1 : l'ombre oublie un signal exécuté en réel
def test_ombre_oublie_le_signal_execute(tmp_path):
    store = SimpleNamespace(record_trade=lambda r: None, agent_event=lambda *a, **k: None)
    sh = ShadowTrader(store, tmp_path / "sh.json", max_open=10)
    sh.open_from_candidates([_cand(key="k1")], NOW, mode="paper", reason="au-delà des revues IA")
    sh.open_from_candidates([_cand(agent="E05", symbol="US500", key="k2")], NOW, mode="paper", reason="x")
    assert len(sh.positions) == 2 and sh.forget("k1") == 1 and len(sh.positions) == 1
    assert sh.forget("inconnue") == 0
    sh2 = ShadowTrader(store, tmp_path / "sh.json", max_open=10)                # la clé survit au disque
    assert [p.key for p in sh2.positions.values()] == ["k2"]


# ---------------- A3 : élargissement du stop copié seulement du côté perdant
def _master(sl):
    return {"ts_utc": "2026-09-28T12:00:00+00:00", "equity": 500000.0, "magic": 51000,
            "positions": [{"ticket": 1, "symbol": "XAGUSD", "side": "SELL", "volume": 1.06, "sl": sl, "tp": 60.07, "price_open": 61.226}]}


class _Spec:
    volume_min, volume_step, volume_max, digits, point = 0.01, 0.01, 100.0, 3, 0.001


class _FPos:
    def __init__(self, sl):
        self.ticket, self.symbol, self.volume, self.sl, self.tp = 9, "SILVER", 0.21, sl, 60.07
        self.comment = f"{COPY_COMMENT_PREFIX}1"


def test_copie_break_even_sans_elargissement():
    mapping = {1: {"ticket": 9, "mv": 1.06, "fv": 0.21, "spread_offset": 0.071}}
    kw = dict(mapping=mapping, sym_map={"XAGUSD": "SILVER"})
    acts = plan_sync(_master(61.30), [_FPos(61.875)], 100000.0, 1.0, {"SILVER": _Spec()}, **kw)
    assert [(a.kind, a.sl) for a in acts] == [("modify", 61.371)]          # stop encore du côté perdant : élargi
    acts = plan_sync(_master(61.10), [_FPos(61.371)], 100000.0, 1.0, {"SILVER": _Spec()}, **kw)
    assert [(a.kind, a.sl) for a in acts] == [("modify", 61.10)]           # break-even : recopié tel quel, en profit chez le suiveur


# ---------------- A4 : verrou logiciel, fermeture refusée → temporisation
def test_verrou_refuse_temporise(broker, tmp_path):
    store = StateStore(tmp_path / "state")
    store.state.roll_day_if_needed(100000, 100000, NOW.date()); store.state.update_equity(100000, 100000)
    pm = PositionManager(broker, store, Journal(tmp_path / "logs", component="t"), PMConfig(protect_profit_risk_ratio=0.16, protect_profit_lock_ratio=0.14), 51000)
    t = broker.tick("EURUSD")
    r = broker.order_send(OrderRequest("EURUSD", Side.BUY, 0.3, t.ask - 0.0010, magic=51000))
    pos = broker.position(r.ticket)
    plan = BotPositionPlan(pos.ticket, "EURUSD", "BUY", "B01", "c1", pos.price_open, pos.sl, pos.volume, 300.0, 0.3, opened_at=NOW.isoformat(), last_sl=pos.sl)
    store.state.bot_positions[str(pos.ticket)] = plan
    dist = pos.price_open - pos.sl
    spec = broker.symbol_info("EURUSD")

    def bid(cible):
        broker.set_price("EURUSD", cible); e = cible - broker.position(pos.ticket).price_current
        if abs(e) > 1e-9: broker.set_price("EURUSD", cible + e)
    bid(pos.price_open + 0.165 * dist)
    pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert plan.trailing_forced
    broker.reject_close = True
    bid(pos.price_open + 0.12 * dist)
    a1 = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    a2 = pm.manage(plan, broker.position(pos.ticket), spec, MarketContext(atr=0.0004, structure_ok=True))
    assert any("fermeture refusée" in a and "60 s" in a for a in a1) and any("fermeture reportée" in a for a in a2)
    assert broker.position(pos.ticket) is not None
    broker.reject_close = False


# ---------------- C2 : agent jamais approuvé par l'IA
def test_agent_jamais_approuve_saute_l_ia():
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"llm_skip_never_approved_after": 30})
    o.state = SystemState()
    c = _cand(agent="B06")
    assert not o._jamais_approuve(c)
    o._revues_ia(c).update(revues=30, approuves=0)
    assert o._jamais_approuve(c)
    o._revues_ia(c)["approuves"] = 1
    assert not o._jamais_approuve(c)
    assert not o._jamais_approuve(_cand(agent="B06", version="1.1"))       # nouvelle version : compteur à zéro
    o.s = SimpleNamespace(execution={})
    assert not o._jamais_approuve(c)                                        # réglage absent : jamais


# ---------------- C3 / C9 : forex court terme en papier, heures forex bloquées
def _orch_forex(heure, classe="forex"):
    o = Orchestrator.__new__(Orchestrator)
    o.s = SimpleNamespace(execution={"forex_short_term_paper_only": True, "forex_blocked_hours_utc": [9, 10, 13], "max_spread_sl_ratio": 0.2})
    o.broker = SimpleNamespace(symbol_info=lambda s: SimpleNamespace(asset_class=classe, trade_allowed=True))
    o.now_fn = lambda: datetime(2026, 9, 28, heure, 30, tzinfo=timezone.utc)
    o._cost_note = lambda c: None
    return o


def test_forex_court_terme_et_heures_bloquees():
    o = _orch_forex(12)
    assert o._forex_court_terme(_cand(symbol="EURUSD", tfs=("M15", "H1")))
    assert not o._forex_court_terme(_cand(symbol="EURUSD", tfs=("H1", "H4")))
    assert not _orch_forex(12, "indices")._forex_court_terme(_cand(symbol="US500", tfs=("M15", "H1")))
    assert o._prefiltre_cout(_cand(symbol="EURUSD", tfs=("H1", "H4"))) == ""
    assert "09 h UTC" in _orch_forex(9)._prefiltre_cout(_cand(symbol="EURUSD", tfs=("H1", "H4")))
    assert _orch_forex(9, "metals")._prefiltre_cout(_cand(symbol="XAUUSD", tfs=("H1", "H4"))) == ""


# ---------------- C4 : risque forex plafonné dans le gate
def test_gate_risque_par_classe(settings, broker):
    from test_prop_foxx_lots_coherence_2026_09_23 import _gate
    from test_risk_and_gate import ctx_for, make_candidate, make_state
    gate, _ = _gate(settings, broker)
    st = make_state()
    c, atr = make_candidate(broker, rr=2.5)
    plein, _ = gate.evaluate(ctx_for(broker, c, st, atr))
    moitie, _ = gate.evaluate(ctx_for(broker, c, st, atr, risk_by_class={"forex": 0.025}))
    assert plein.approved and moitie.approved and moitie.risk_money < plein.risk_money
    assert moitie.risk_percent <= 0.025 + 1e-9 < plein.risk_percent          # risque effectif (après arrondi du volume)


# ---------------- réglages
def test_reglages_du_28_09_soir():
    risk = yaml.safe_load(open("config/risk.yaml", encoding="utf-8"))
    assert risk["risk"]["risk_per_trade_by_class"] == {"forex": 0.025}
    ex = yaml.safe_load(open("config/system.yaml", encoding="utf-8"))["execution"]
    assert ex["llm_skip_never_approved_after"] == 30 and ex["forex_short_term_paper_only"] is True
    assert ex["forex_blocked_hours_utc"] == []            # décision utilisateur 28/09 au soir : le forex garde le droit de trader à ces heures
    deg = yaml.safe_load(open("config/strategies.yaml", encoding="utf-8"))["learning"]["degradation"]
    assert deg["trades_mode"] == "live+paper"
