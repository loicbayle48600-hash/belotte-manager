"""Audit complet du 2026-09-21 : défauts de l'orchestrateur et de l'état corrigés ce jour.

Chaque test échouait avant la correction correspondante.

1. **Risque incohérent entre gate et exécuteur.** Le gate dimensionne le volume sur le tick courant
   (`px = tick.ask/bid`), mais l'orchestrateur recalculait `risk.size()` sur `c.entry` — le prix du
   *scan* — et transmettait CE risque à `executor.execute()`. Après une dérive de prix entre scan et
   gate, `initial_risk_money` du plan (donc l'agrégation « idée de trade » prop, le journal et la
   revue post-trade) ne correspondait plus au volume réellement envoyé. `GateResult` porte désormais
   `risk_money / risk_percent / sizing_price / volume_wanted / volume_capped` et l'orchestrateur ne
   recalcule plus rien.

2. **Contexte macro recalculé.** `macro = out.get("macro") or self._macro_context()` : `out` ne
   contenait jamais `macro` (il vit dans `summary["macro"]` de `cycle()`), la même chose était
   recalculée à chaque scan. `cycle()` le passe maintenant à `_scan_and_execute`.

3. **Écritures d'état inutiles.** `store.save()` était appelé pour CHAQUE candidat revu, alors que
   le heartbeat qu'il était censé rafraîchir est figé au début du cycle : N écritures atomiques du
   même fichier pour rien. On ne re-persiste qu'après un appel LLM réel, heartbeat rafraîchi.

4. **Timeframe d'invalidation.** Les screeners promettent « clôture au-delà de l'EMA50 du tf
   d'entrée », mais `_manage_positions` lisait M15 en dur : un agent H1 pouvait être sorti sur une
   clôture M15 qu'il n'avait jamais promise. On lit le tf d'entrée de la spec de l'agent.

5. **Durée de cycle invisible.** Un cycle plus long que `heartbeat_max_age_sec` (45 s) fait déclarer
   l'orchestrateur mort par le watchdog et bloque les entrées (`01_health`) sans aucune trace de la
   cause. `last_cycle["duration_sec"]` + avertissement journal au-delà de la tolérance.

6. **Retrait simulé et changement de jour.** `roll_day_if_needed` recevait l'equity BROKER (brute)
   et posait `starting_equity` brute, tandis que `self.equity` est nette des retraits simulés : le
   lendemain d'un retrait, la journée s'ouvrait avec une « perte » égale au montant retiré (verrou
   `DAILY_LOSS_LIMIT` possible dès l'ouverture, plancher prop faussé). Même incohérence dans le
   watchdog, qui comparait `acc.equity` brute aux références nettes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradinglab.core.state import BotPositionPlan, SystemMode, SystemState
from tradinglab.core.types import GateResult, OrderRequest, Regime, Side, Tick, TradeCandidate, Verdict
from tradinglab.execution.executor import ExecutionOutcome
from tradinglab.orchestration import orchestrator as orch_mod
from tradinglab.orchestration.market_router import RoutingReport
from tradinglab.orchestration.orchestrator import Orchestrator
from tests.conftest import FIXED_NOW
from tests.test_risk_and_gate import ctx_for, gate_for, make_candidate, make_state


def make_orch(settings, broker, mode="AUTO"):
    o = Orchestrator(settings, broker, mode, now_fn=broker.now)
    o.scheduler.intervals["research"] = 10**9
    assert o.startup()
    return o


def _auto(o):
    o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    return o


# ------------------------------------------------------------------ 1. risque du gate → exécuteur
def test_gate_result_porte_le_sizing_calcule_sur_le_tick(settings, broker):
    """Le `GateResult` approuvé expose le risque calculé sur le tick, pas sur `c.entry`."""
    gate, risk = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    st = make_state()
    res, req = gate.evaluate(ctx_for(broker, c, st, atr))
    assert res.approved, res.reason
    tick = broker.tick(c.symbol)
    assert res.sizing_price == pytest.approx(tick.ask)
    attendu = risk.size(st.equity, tick.ask, c.sl, broker.symbol_info(c.symbol))
    assert res.risk_money == pytest.approx(attendu.risk_money)
    assert res.risk_percent == pytest.approx(attendu.risk_percent_effective)
    assert res.volume_wanted == pytest.approx(attendu.volume_wanted)
    assert req.volume == pytest.approx(attendu.volume)
    d = res.to_dict()
    assert {"risk_money", "risk_percent", "sizing_price", "volume_wanted", "volume_capped"} <= set(d)


def test_gate_result_sans_sizing_reste_a_zero(settings, broker):
    """Sans compte ni spec, aucun sizing n'existe : le résultat ne porte aucun risque inventé."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr, account=None, spec=None))
    assert not res.approved and req is None
    assert res.risk_money == 0.0 and res.sizing_price == 0.0 and res.volume_wanted == 0.0


def test_gate_refuse_garde_le_sizing_pour_le_diagnostic(settings, broker):
    """Comme `adjusted_volume`, le sizing d'un refus reste lisible dans le journal (diagnostic `11b`),
    mais aucun `OrderRequest` n'accompagne le refus : rien ne peut partir avec ce risque."""
    gate, _ = gate_for(settings, broker)
    c, atr = make_candidate(broker, score=10.0)          # sous le score requis → refus
    res, req = gate.evaluate(ctx_for(broker, c, make_state(), atr))
    assert not res.approved and req is None
    assert res.risk_money > 0 and res.sizing_price == pytest.approx(broker.tick(c.symbol).ask)


def test_sizing_du_gate_differe_du_recalcul_sur_le_prix_du_scan(settings, broker):
    """Le défaut d'origine : quand le prix a dérivé entre scan et gate, le recalcul sur `c.entry`
    donne un autre risque que celui du volume envoyé. C'est le premier qui accompagne l'ordre."""
    gate, risk = gate_for(settings, broker)
    c, atr = make_candidate(broker)
    st = make_state()
    tick = broker.tick(c.symbol)
    # le marché a monté de 0.5 ATR depuis le scan : le SL est plus loin du prix d'exécution
    drift = 0.5 * atr
    t2 = Tick(symbol=tick.symbol, time=tick.time, bid=tick.bid + drift, ask=tick.ask + drift)
    res, req = gate.evaluate(ctx_for(broker, c, st, atr, tick=t2))
    assert res.approved, res.reason
    ancien = risk.size(st.equity, c.entry, c.sl, broker.symbol_info(c.symbol))
    assert ancien.volume != pytest.approx(req.volume)              # le recalcul historique ne décrivait pas l'ordre
    assert res.risk_money == pytest.approx(risk.size(st.equity, t2.ask, c.sl, broker.symbol_info(c.symbol)).risk_money)


def _force_candidate(o, monkeypatch, c: TradeCandidate):
    """Route un seul candidat approuvé déterministement, sans dépendre du marché simulé."""
    monkeypatch.setattr(o.router_, "route", lambda snapshots, statuses=None: ([c] if statuses == ("LIVE",) else [], RoutingReport()))
    det = o.review.deterministic

    class _Det:
        verdict = Verdict.APPROVE
        def to_dict(self):
            return {"verdict": "APPROVE", "arbiter": "test"}
    monkeypatch.setattr(o.review, "deterministic", lambda cand, bonus=0.0: _Det())
    return det


def test_orchestrateur_transmet_le_risque_du_gate_a_l_executeur(settings, broker, monkeypatch):
    o = _auto(make_orch(settings, broker))
    c, _ = make_candidate(broker)
    c.verdict = None
    _force_candidate(o, monkeypatch, c)
    gate_res = GateResult(approved=True, risk_money=123.45, risk_percent=0.111, sizing_price=1.2345,
                          volume_wanted=9.9, volume_capped=True)
    req = OrderRequest(symbol=c.symbol, side=c.side, volume=0.5, sl=c.sl, tp=c.tp_plan[0])
    monkeypatch.setattr(o, "_gate", lambda cand, now, corr: (gate_res, req))
    captured = {}

    def fake_execute(cand, gate, request, risk_money, risk_percent):
        captured.update(gate=gate, req=request, risk_money=risk_money, risk_percent=risk_percent)
        return ExecutionOutcome(False, None, None, False, "test")
    monkeypatch.setattr(o.executor, "execute", fake_execute)
    broker.set_now(broker.now() + timedelta(minutes=5))
    o.cycle()
    assert captured["risk_money"] == 123.45 and captured["risk_percent"] == 0.111
    assert captured["gate"] is gate_res and captured["req"] is req
    warns = [e for e in o.journal.read_day(kinds={"warning"}) if "rabote" in e.get("message", "")]
    assert warns, "l'écrêtage de volume signalé par le gate doit être journalisé"
    w = str(warns[-1])
    assert "9.9" in w and "0.5" in w and "123.45" in w


# ------------------------------------------------------------------ 2. macro passé par cycle()
def test_macro_calcule_une_seule_fois_par_cycle(settings, broker, monkeypatch):
    o = _auto(make_orch(settings, broker))
    calls = {"n": 0}
    real = o._macro_context

    def counted():
        calls["n"] += 1
        return real()
    monkeypatch.setattr(o, "_macro_context", counted)
    broker.set_now(broker.now() + timedelta(minutes=5))
    s = o.cycle()
    assert "routing" in s, "le scan doit avoir tourné pour que le test soit probant"
    assert calls["n"] == 1


def test_scan_sans_macro_le_recalcule(settings, broker):
    """Appel direct (tests, outils) sans contexte : on garde le repli."""
    o = _auto(make_orch(settings, broker))
    dd = o.daily.evaluate(o.state)
    out = o._scan_and_execute(broker.now(), dd)
    assert "routing" in out


# ------------------------------------------------------------------ 3. hygiène des écritures d'état
def test_pas_de_save_par_candidat_sans_llm(settings, broker, monkeypatch):
    o = _auto(make_orch(settings, broker))
    assert o.review.llm is None
    saves = {"n": 0}
    real = o.store.save

    def counted(*a, **k):
        saves["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(o.store, "save", counted)
    broker.set_now(broker.now() + timedelta(minutes=5))
    s = o.cycle()
    assert s["candidates"] >= 3, "il faut plusieurs candidats pour distinguer une écriture par candidat"
    # 1 avant la revue + 1 en fin de cycle + 2 par entrée exécutée (marquage d'idempotence, plan) ;
    # avant la correction : + 1 par candidat revu.
    assert saves["n"] <= 2 + 2 * s["entries"]


def test_save_apres_appel_llm_rafraichit_le_heartbeat(settings, broker, monkeypatch):
    from tests.test_review_market_agents_orchestration import FakeLLM
    o = _auto(make_orch(settings, broker))
    o.review.llm = FakeLLM()
    saves = {"n": 0}
    real = o.store.save

    def counted(*a, **k):
        saves["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(o.store, "save", counted)
    broker.set_now(broker.now() + timedelta(minutes=5))
    s = o.cycle()
    if o.review.llm.calls:
        assert saves["n"] >= 3                       # avant revue + après un appel LLM + fin de cycle
        assert o.state.orchestrator_heartbeat == s["ts"]


# ------------------------------------------------------------------ 4. timeframe d'invalidation
def test_timeframe_d_invalidation_suit_le_tf_d_entree_de_l_agent(settings, broker):
    o = make_orch(settings, broker)
    h1 = [a for a in o.registry.agents.values() if a.timeframes.get("entry") == "H1"]
    m5 = [a for a in o.registry.agents.values() if a.timeframes.get("entry") == "M5"]
    assert h1 and m5, "le registre doit contenir des agents H1 et M5 pour que le test ait un sens"
    assert o._invalidation_timeframe(h1[0].agent_id) == "H1"
    assert o._invalidation_timeframe(m5[0].agent_id) == "M5"
    assert o._invalidation_timeframe("ADOPTED") == "M15"
    assert o._invalidation_timeframe("") == "M15"


def test_manage_positions_lit_le_frame_du_tf_d_entree(settings, broker, monkeypatch):
    o = _auto(make_orch(settings, broker))
    h1_agent = next(a for a in o.registry.agents.values() if a.timeframes.get("entry") == "H1")
    sym = next(iter(o.universe.values()))
    t = broker.tick(sym)
    plan = BotPositionPlan(ticket=999, symbol=sym, side="BUY", agent_id=h1_agent.agent_id, candidate_id="c",
                           entry=t.ask, initial_sl=t.ask - 0.002, initial_volume=0.1, initial_risk_money=20.0,
                           risk_percent=0.1, invalidation="clôture au-delà de l'EMA50 du tf d'entrée")
    o.state.bot_positions["999"] = plan
    from tradinglab.core.types import Position
    pos = Position(ticket=999, symbol=sym, side=Side.BUY, volume=0.1, price_open=t.ask, sl=t.ask - 0.002, tp=0.0,
                   profit=0.0, time_open=broker.now(), magic=51000)
    monkeypatch.setattr(o.broker, "position", lambda ticket: pos if int(ticket) == 999 else None)
    seen = []
    snap = o.snapshots[sym]
    real_get = snap.frames.get

    def spy(key, default=None):
        seen.append(key)
        return real_get(key, default)
    monkeypatch.setattr(snap, "frames", type("F", (), {"get": staticmethod(spy)})())
    monkeypatch.setattr(o.pm, "manage", lambda *a, **k: [])
    o._manage_positions()
    assert seen and seen[0] == "H1"


# ------------------------------------------------------------------ 5. durée de cycle
def test_duree_de_cycle_dans_last_cycle(settings, broker):
    o = make_orch(settings, broker)
    s = o.cycle()
    assert "duration_sec" in s and s["duration_sec"] >= 0.0
    assert o.state.last_cycle["duration_sec"] == s["duration_sec"]


def test_cycle_trop_long_est_journalise(settings, broker, monkeypatch):
    o = make_orch(settings, broker)
    limit = float(o.s.system.get("heartbeat_max_age_sec", 45))
    ticks = iter([0.0, limit + 10.0, limit + 10.0, limit + 10.0])
    monkeypatch.setattr(orch_mod.time, "monotonic", lambda: next(ticks, limit + 10.0))
    s = o.cycle()
    assert s["duration_sec"] > limit
    assert any("tolérance du watchdog" in str(e) for e in o.journal.read_day())


def test_cycle_court_ne_journalise_rien(settings, broker):
    o = make_orch(settings, broker)
    o.cycle()
    assert not any("tolérance du watchdog" in str(e) for e in o.journal.read_day())


# ------------------------------------------------------------------ 6. retrait simulé et changement de jour
NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def test_lendemain_d_un_retrait_ne_commence_pas_en_perte():
    st = SystemState()
    st.roll_day_if_needed(500_000.0, 500_000.0, NOW)
    st.update_equity(500_000.0, 500_000.0)
    st.register_simulated_withdrawal(20_000.0, NOW)
    assert st.equity == pytest.approx(480_000.0) and st.daily_pnl() == pytest.approx(0.0)
    # le broker affiche toujours 500 000 le lendemain ; le labo doit repartir de 480 000
    lendemain = NOW + timedelta(days=1)
    assert st.roll_day_if_needed(500_000.0, 500_000.0, lendemain)
    st.update_equity(500_000.0, 500_000.0)
    assert st.daily.starting_equity == pytest.approx(480_000.0)
    assert st.daily.reference_equity == pytest.approx(480_000.0)
    assert st.daily_pnl() == pytest.approx(0.0)
    assert st.prop_daily_loss_percent() == pytest.approx(0.0)


def test_perte_reelle_le_lendemain_reste_mesuree():
    st = SystemState()
    st.roll_day_if_needed(500_000.0, 500_000.0, NOW)
    st.update_equity(500_000.0, 500_000.0)
    st.register_simulated_withdrawal(20_000.0, NOW)
    st.roll_day_if_needed(500_000.0, 500_000.0, NOW + timedelta(days=1))
    st.update_equity(495_000.0, 500_000.0)          # 5 000 de perte flottante côté broker
    assert st.daily_pnl() == pytest.approx(-5_000.0)


def test_sans_retrait_rien_ne_change():
    st = SystemState()
    st.roll_day_if_needed(500_000.0, 500_000.0, NOW)
    assert st.daily.starting_equity == 500_000.0 and st.daily.reference_equity == 500_000.0


def test_watchdog_compare_une_equity_nette(settings, broker):
    from tradinglab.core.journal import Journal
    from tradinglab.core.state import StateStore
    from tradinglab.monitoring.watchdog import Watchdog
    store = StateStore(settings.state_dir)
    st = store.state
    acc = broker.account_info()
    st.roll_day_if_needed(acc.equity, acc.balance, broker.now())
    st.update_equity(acc.equity, acc.balance)
    st.register_simulated_withdrawal(acc.equity * 0.02, broker.now())      # 2 % retirés
    store.save()
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"), reference_symbol="EURUSD")
    rep = wd.check_once()
    # un retrait n'est pas une perte de trading : aucun drawdown interne, aucune raison d'alerte
    assert rep.daily_dd_percent == pytest.approx(0.0, abs=1e-6)
    assert not any("DD jour" in r for r in rep.reasons)
