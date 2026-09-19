"""Tests de la revue « market-agents-orchestration » : feed, registre, screeners, revue adversariale, orchestrateur.

Chaque test couvre un bug corrigé et échouait avant la correction.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import timedelta

import pandas as pd
import pytest

from tradinglab.agents import registry as registry_mod, screeners as screeners_mod
from tradinglab.agents.registry import AgentRegistry, AgentSpec
from tradinglab.agents.review import AdversarialReview
from tradinglab.agents.screeners import _build, structure_bos
from tradinglab.core.state import StateStore, SystemMode
from tradinglab.core.types import AgentStatus, Regime, Side, Tick, TradeCandidate, Verdict
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.models.client import LLMResponse
from tradinglab.mt5.mock_adapter import MockBroker
from tradinglab.orchestration import orchestrator as orch_mod
from tradinglab.orchestration.orchestrator import MAX_LLM_REVIEWS_PER_CYCLE, Orchestrator
from tests.conftest import FIXED_NOW


class FakeLLM:
    """Client LLM factice : renvoie toujours le même texte, compte les appels, aucune donnée réseau."""

    def __init__(self, text: str = '{"verdict": "APPROVE", "rationale": "ok", "arguments": [], "conviction_0_100": 90}', hook=None):
        self.text = text
        self.calls = 0
        self.hook = hook

    def complete(self, role, system, user, max_tokens=800, financial_importance="normal", temperature=0.2, cache_key_extra=""):
        self.calls += 1
        if self.hook:
            self.hook(self, role)
        return LLMResponse(self.text, "fake-model", "TIER_B", 10, 10, 0.001)


def make_orch(settings, broker, mode="AUTO"):
    o = Orchestrator(settings, broker, mode, now_fn=broker.now)
    o.scheduler.intervals["research"] = 10**9
    assert o.startup()
    return o


def candidate(**kw) -> TradeCandidate:
    base = dict(symbol="EURUSD", side=Side.BUY, entry=1.08, sl=1.077, tp_plan=[1.0875], timeframes=["M15"],
                regime=Regime.TRENDING, agent_id="B01", setup_score=80, rr=2.5)
    base.update(kw)
    return TradeCandidate(**base)


# ----------------------------------------------------------------------------------------------------------
# 1. SAFE_MODE (commande / exception) n'est plus annulé par _maybe_go_auto
# ----------------------------------------------------------------------------------------------------------
def test_safe_mode_command_holds_until_resume(settings, broker):
    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    o.store.push_command("SAFE_MODE", {}, "test")
    for _ in range(3):
        o.cycle()
        assert o.state.mode == SystemMode.SAFE_MODE.value
    assert any("commande" in r for r in o.state.mode_reasons)
    o.handle_command("RESUME", {})
    assert o.state.mode == SystemMode.AUTO.value


def test_cycle_exception_keeps_safe_mode_until_resume(settings, broker, monkeypatch):
    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    monkeypatch.setattr(orch_mod.time, "sleep", lambda *_: None)
    real_cycle = o.cycle
    calls = {"n": 0}

    def failing_cycle():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_cycle()
    o.cycle = failing_cycle
    o.run(max_cycles=4)   # 1 exception puis 3 cycles sains
    assert o.state.mode == SystemMode.SAFE_MODE.value
    assert o.requested_mode == "SAFE"
    o.handle_command("RESUME", {})
    assert o.state.mode == SystemMode.AUTO.value


# ----------------------------------------------------------------------------------------------------------
# 2. Connexion refusée au démarrage : initialisation rejouée au premier cycle connecté
# ----------------------------------------------------------------------------------------------------------
def test_deferred_init_after_failed_startup_connection(settings, broker, monkeypatch):
    monkeypatch.setattr(orch_mod.time, "sleep", lambda *_: None)
    broker.set_connected(False)
    real_connect = broker.connect
    broker.connect = lambda: False
    o = Orchestrator(settings, broker, "AUTO", now_fn=broker.now)
    o.scheduler.intervals["research"] = 10**9
    assert o.startup() is False
    assert o.universe == {} and o.research is None
    s1 = o.cycle()
    assert s1.get("error") == "broker déconnecté" and o.universe == {}
    broker.connect = real_connect
    s2 = o.cycle()
    assert "error" not in s2
    assert o.universe and o.research is not None and o._initialized
    assert o.state.account_login == broker.login
    assert any(e["kind"] == "account" for e in o.journal.read_day(kinds={"account"}))
    assert o.snapshots


# ----------------------------------------------------------------------------------------------------------
# 3. Fraîcheur du tick : âge journalisé, âge négatif hors tolérance = STALE
# ----------------------------------------------------------------------------------------------------------
def test_feed_tick_age_logged_and_future_tick_is_stale(broker):
    feed = MarketDataFeed(broker, ["H1"], bars=300, max_tick_age_sec=30)
    snap = feed.snapshot("EURUSD", now=broker.now())
    pub = snap.to_public_dict()
    assert "tick_age_sec" in pub and pub["tick_age_sec"] is not None and -30 <= pub["tick_age_sec"] <= 30
    assert snap.data_quality == "OK"

    class ServerOffsetBroker(MockBroker):
        def tick(self, symbol, force=False):
            t = super().tick(symbol, force=force)
            return None if t is None else Tick(symbol=t.symbol, time=t.time + timedelta(hours=2), bid=t.bid, ask=t.ask)

    b = ServerOffsetBroker(seed=7)
    b.connect(); b.set_now(FIXED_NOW)
    snap2 = MarketDataFeed(b, ["H1"], bars=300, max_tick_age_sec=30).snapshot("EURUSD", now=b.now())
    assert snap2.data_fresh is False and snap2.data_quality == "STALE"
    assert snap2.to_public_dict()["tick_age_sec"] is None   # âge non fini : jamais une valeur inventée


# ----------------------------------------------------------------------------------------------------------
# 4. Cache par barre : pas de mise en cache d'une réponse vide ou d'une barre non avancée
# ----------------------------------------------------------------------------------------------------------
def test_feed_cache_not_written_when_last_bar_unchanged(broker):
    fixed = broker.rates("EURUSD", "H1", 300)
    calls = {"n": 0}

    class FrozenBroker(MockBroker):
        def rates(self, symbol, timeframe, count):
            calls["n"] += 1
            return fixed.copy()

    b = FrozenBroker(seed=7)
    b.connect(); b.set_now(FIXED_NOW)
    feed = MarketDataFeed(b, ["H1"], bars=300)
    now = broker.now()
    feed.frame("EURUSD", "H1", now)
    assert calls["n"] == 1
    feed.frame("EURUSD", "H1", now + timedelta(seconds=10))          # même barre → cache
    assert calls["n"] == 1
    feed.frame("EURUSD", "H1", now + timedelta(hours=1))             # nouvelle barre mais dernière ligne identique
    assert calls["n"] == 2
    feed.frame("EURUSD", "H1", now + timedelta(hours=1, seconds=5))  # non mis en cache → re-lecture
    assert calls["n"] == 3


def test_feed_cache_not_written_on_empty_response(broker):
    calls = {"n": 0}

    class FlakyBroker(MockBroker):
        def rates(self, symbol, timeframe, count):
            calls["n"] += 1
            if calls["n"] == 1:
                return pd.DataFrame()
            return super().rates(symbol, timeframe, count)

    b = FlakyBroker(seed=7)
    b.connect(); b.set_now(FIXED_NOW)
    feed = MarketDataFeed(b, ["H1"], bars=300)
    assert len(feed.frame("EURUSD", "H1", b.now())) == 0
    assert len(feed.frame("EURUSD", "H1", b.now())) == 300 and calls["n"] == 2


# ----------------------------------------------------------------------------------------------------------
# 5. Registre : verrou + écriture atomique
# ----------------------------------------------------------------------------------------------------------
def test_registry_save_is_atomic(tmp_path, monkeypatch):
    f = tmp_path / "data" / "agent_status.json"
    reg = AgentRegistry(status_file=f)
    replaced = []
    real = os.replace

    def spy(src, dst):
        replaced.append(str(dst))
        return real(src, dst)
    monkeypatch.setattr(registry_mod.os, "replace", spy)
    reg.set_status("B01", AgentStatus.SUSPENDED)
    assert replaced and replaced[-1] == str(f)
    assert json.loads(f.read_text(encoding="utf-8"))["status"]["B01"] == "SUSPENDED"
    assert not [p for p in f.parent.iterdir() if p.suffix == ".tmp"]
    assert isinstance(reg._lock, type(threading.RLock()))


def test_registry_concurrent_add_and_iteration(tmp_path):
    reg = AgentRegistry(status_file=tmp_path / "agent_status.json")
    parent = reg.get("B01")
    errors = []

    def adder():
        try:
            for i in range(300):
                reg.add(AgentSpec(agent_id=f"CH{1000 + i}", family="B", name="x", strategy="ema_trend", markets=["forex"],
                                  sessions=["LONDON"], timeframes={"entry": "M15", "trend": "H1"}, regimes=["TRENDING"],
                                  status="RESEARCH", parent_id=parent.agent_id, created_by="test"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    t = threading.Thread(target=adder)
    t.start()
    try:
        for _ in range(2000):
            reg.generators(); reg.by_status(AgentStatus.LIVE); reg.summary()
    except RuntimeError as e:
        errors.append(e)
    t.join()
    assert not errors


# ----------------------------------------------------------------------------------------------------------
# 6. agent_status.json corrompu / incompatible : jamais de crash, jamais silencieux
# ----------------------------------------------------------------------------------------------------------
def test_registry_corrupt_status_file_is_quarantined(tmp_path):
    f = tmp_path / "agent_status.json"
    f.write_text('{"status": {"B01": "SUSPEN', encoding="utf-8")   # tronqué (crash pendant l'écriture)
    reg = AgentRegistry(status_file=f)
    assert not f.exists() and (tmp_path / "agent_status.corrupt.json").exists()
    assert reg.get("B01").status == AgentStatus.LIVE.value
    f.write_text("[1, 2]", encoding="utf-8")                          # JSON valide mais non-objet
    AgentRegistry(status_file=f)
    assert not f.exists()


def test_registry_incompatible_challenger_and_invalid_status_are_ignored(tmp_path):
    f = tmp_path / "agent_status.json"
    f.write_text(json.dumps({"status": {"B01": "SUSPENDED", "B02": "BOGUS"},
                             "challengers": [{"agent_id": "CH101", "family": "B"},                       # champs obligatoires manquants
                                             {"agent_id": "CH102", "family": "B", "name": "ok", "strategy": "ema_trend",
                                              "markets": ["forex"], "sessions": ["LONDON"], "timeframes": {}, "regimes": [],
                                              "status": "RESEARCH", "created_by": "pipeline"}]}), encoding="utf-8")
    reg = AgentRegistry(status_file=f)           # ne lève plus TypeError
    assert reg.get("B01").status == "SUSPENDED"
    assert reg.get("B02").status == AgentStatus.LIVE.value
    assert reg.get("CH101") is None and reg.get("CH102") is not None


# ----------------------------------------------------------------------------------------------------------
# 7. Commande malformée : le lot continue (PANIC exécuté)
# ----------------------------------------------------------------------------------------------------------
def test_malformed_command_does_not_lose_panic(settings, broker):
    o = make_orch(settings, broker)
    o.store.push_command("BREAK_EVEN", {"target": "abc"}, "test")   # ValueError avant correction
    o.store.push_command("CLOSE", "EURUSD", "test")                  # args non-dict (AttributeError avant correction)
    o.store.push_command("PANIC", {}, "test")
    s = o.cycle()
    assert "error" not in s
    assert o.state.mode == SystemMode.PANIC.value and "PANIC" in o.state.lock_reasons
    cmds = [e for e in o.journal.read_day(kinds={"command"}) if e.get("command") == "BREAK_EVEN"]
    assert cmds and cmds[-1]["ok"] is False and "ticket" in cmds[-1]["reason"]


# ----------------------------------------------------------------------------------------------------------
# 8. Réponse LLM JSON valide mais non-objet
# ----------------------------------------------------------------------------------------------------------
def test_review_non_object_llm_json_does_not_raise():
    llm = FakeLLM(text="[1, 2]")
    rv = AdversarialReview(llm, 65, 1.5)
    c = candidate()
    res = rv.review(c)
    assert res.verdict is Verdict.APPROVE and res.arbiter == "deterministic"
    assert res.bull.get("parse_error") and llm.calls == 4
    rv2 = AdversarialReview(FakeLLM(text='"APPROVE"'), 65, 1.5)
    assert rv2.review(candidate()).arbiter == "deterministic"


# ----------------------------------------------------------------------------------------------------------
# 9. Revue LLM : heartbeat persisté, aucun appel si entrées interdites, N meilleurs candidats
# ----------------------------------------------------------------------------------------------------------
def test_llm_review_skipped_when_entries_not_allowed_and_heartbeat_persisted(settings, broker):
    o = make_orch(settings, broker)
    seen = []

    def hook(fake, role):
        seen.append(StateStore(settings.state_dir).load().orchestrator_heartbeat)
    fake = FakeLLM(hook=hook)
    o.review.llm = fake
    s1 = o.cycle()                                    # SAFE_MODE : entrées interdites
    assert s1["candidates"] >= 1 and fake.calls == 0
    cands = [e for e in o.journal.read_day(kinds={"candidate"})]
    assert cands and all(e["candidate"]["review"].get("llm_skipped") for e in cands)
    o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    broker.set_now(broker.now() + timedelta(minutes=5))
    s = o.cycle()
    assert fake.calls <= 4 * MAX_LLM_REVIEWS_PER_CYCLE
    if fake.calls:
        assert seen[0] == s["ts"]                     # heartbeat du cycle courant déjà sur disque pendant la revue


# ----------------------------------------------------------------------------------------------------------
# 10. _build refuse un SL du mauvais côté
# ----------------------------------------------------------------------------------------------------------
def test_build_rejects_stop_on_wrong_side(broker):
    snap = MarketDataFeed(broker, ["M15", "H1"], bars=300).snapshot("EURUSD", now=broker.now())
    spec = AgentRegistry().get("F03")
    assert _build(spec, snap, Side.BUY, 1.10, 1.101, 2.0, 70, [], [], "x", "t") is None
    assert _build(spec, snap, Side.SELL, 1.10, 1.099, 2.0, 70, [], [], "x", "t") is None
    assert _build(spec, snap, Side.BUY, 1.10, 1.099, 2.0, 70, [], [], "x", "t") is not None


def test_structure_bos_rejects_swing_low_above_entry(broker, monkeypatch):
    snap = MarketDataFeed(broker, ["M15", "H1"], bars=300).snapshot("EURUSD", now=broker.now())
    spec = AgentRegistry().get("F03")   # break_of_structure
    entry = float(snap.frames["M15"].iloc[-2]["close"])
    atr = float(snap.frames["M15"].iloc[-2]["atr14"])
    monkeypatch.setattr(screeners_mod, "structure_label", lambda df, *a, **k: "HH_HL")
    # dernier swing bas confirmé AU-DESSUS de l'entrée (impulsion), dernier swing haut sous l'entrée
    monkeypatch.setattr(screeners_mod, "swing_points", lambda df, *a, **k: ([(5, entry - 0.5 * atr)], [(6, entry + 1.0 * atr)]))
    assert structure_bos(spec, snap) is None


# ----------------------------------------------------------------------------------------------------------
# 11. WAIT news déterministe : jamais soumis à l'arbitre LLM
# ----------------------------------------------------------------------------------------------------------
def test_news_wait_is_hard_and_bypasses_llm():
    llm = FakeLLM()
    rv = AdversarialReview(llm, 65, 1.5)
    c = candidate(news_state="BLOCKED_PRE_NEWS")
    res = rv.review(c)
    assert res.verdict is Verdict.WAIT and res.arbiter == "deterministic" and res.hard
    assert c.verdict is Verdict.WAIT and llm.calls == 0
    c2 = candidate(news_state="SHOCK")
    assert rv.review(c2).verdict is Verdict.WAIT and llm.calls == 0
    c3 = candidate(news_state="OK")
    assert rv.review(c3).arbiter.startswith("llm:") and llm.calls == 4


# ----------------------------------------------------------------------------------------------------------
# 12. _gate : heartbeat watchdog non parsable → watchdog absent, pas d'exception
# ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("hb", ["garbage", "2026-01-20T09:59:50", 12345, None])
def test_gate_tolerates_unparsable_watchdog_heartbeat(settings, broker, hb):
    o = make_orch(settings, broker)
    o.cycle()
    payload = {"safe_mode_request": False, "reasons": []}
    if hb is not None:
        payload["heartbeat"] = hb
    (settings.state_dir / "watchdog.json").write_text(json.dumps(payload), encoding="utf-8")
    c = candidate(bar_time=broker.now().isoformat())
    res, _ = o._gate(c, broker.now(), None)
    health = next(ch for ch in res.checks if ch.name == "01_health")
    if hb == "2026-01-20T09:59:50":
        assert health.ok, "horodatage naïf interprété en UTC"
    else:
        assert not health.ok and "watchdog" in health.detail


# ----------------------------------------------------------------------------------------------------------
# 13. Une exception sur un symbole n'annule pas le cycle
# ----------------------------------------------------------------------------------------------------------
def test_snapshot_exception_on_one_symbol_does_not_abort_cycle(settings, broker):
    o = make_orch(settings, broker)
    real = o.feed.snapshot

    def flaky(sym, now=None, news_shock=False):
        if sym == "GBPUSD":
            raise KeyError("colonne manquante")
        return real(sym, now=now, news_shock=news_shock)
    o.feed.snapshot = flaky
    s = o.cycle()
    assert "error" not in s and "EURUSD" in o.snapshots and "GBPUSD" not in o.snapshots
    warns = [e for e in o.journal.read_day() if e.get("message") == "snapshot impossible"]
    assert warns and warns[-1]["symbol"] == "GBPUSD" and "KeyError" in warns[-1]["error"]


# ----------------------------------------------------------------------------------------------------------
# 14. Changement de compte en cours d'exécution
# ----------------------------------------------------------------------------------------------------------
def test_account_change_mid_run_locks_and_safe_mode(settings, broker):
    o = make_orch(settings, broker)
    o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.AUTO.value
    broker.login = 999999
    o.cycle()
    assert o.state.mode == SystemMode.SAFE_MODE.value and "ACCOUNT_MISMATCH" in o.state.lock_reasons
    assert o.state.account_login == 999999
    o.cycle(); o.cycle(); o.cycle()
    assert o.state.mode == SystemMode.SAFE_MODE.value           # pas de retour AUTO sans décision humaine
    expected_login = int((settings.get("account_expected", {}) or {}).get("login", 0))
    assert any(e.get("previous", {}).get("login") == expected_login for e in o.journal.read_day(kinds={"account"}))
