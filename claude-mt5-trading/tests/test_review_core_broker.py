"""Tests de la revue « core-broker » : types, journal, horloge, config, état, adaptateurs mock et MT5 (stub).

Chaque test couvre un bug corrigé et échouait avant la correction.
"""
from __future__ import annotations

import json
import os
import types as _types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradinglab.core import clock
from tradinglab.core.config import load_dotenv
from tradinglab.core.journal import Journal, _scrub
from tradinglab.core.state import StateStore, SystemState
from tradinglab.core.types import OrderRequest, Side, Tick
from tradinglab.market_data.feed import MarketDataFeed
from tradinglab.mt5 import mt5_adapter
from tradinglab.mt5.mock_adapter import MockBroker

FIXED_NOW = datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------------------------------------
# Tick.age_seconds : un tick daté dans le futur n'est jamais « frais »
# ----------------------------------------------------------------------------------------------------------
def test_tick_in_future_is_not_fresh():
    t = Tick(symbol="EURUSD", time=FIXED_NOW + timedelta(hours=2), bid=1.1, ask=1.1001)
    assert t.age_seconds(FIXED_NOW) == float("inf")
    assert not (t.age_seconds(FIXED_NOW) <= 30)
    # tolérance de dérive : 1 s dans le futur reste un âge (négatif) normal
    t2 = Tick(symbol="EURUSD", time=FIXED_NOW + timedelta(seconds=1), bid=1.1, ask=1.1001)
    assert -1.0 <= t2.age_seconds(FIXED_NOW) <= 0.0


def test_feed_snapshot_future_tick_is_stale():
    class FutureTickBroker(MockBroker):
        def tick(self, symbol, force=False):
            t = super().tick(symbol, force=force)
            if t is None:
                return None
            return Tick(symbol=t.symbol, time=t.time + timedelta(hours=3), bid=t.bid, ask=t.ask)

    b = FutureTickBroker(seed=7)
    b.connect()
    b.set_now(FIXED_NOW)
    feed = MarketDataFeed(b, ["M15", "H1"], bars=300)
    snap = feed.snapshot("EURUSD", now=b.now())
    assert snap.data_fresh is False
    assert snap.data_quality != "OK"


# ----------------------------------------------------------------------------------------------------------
# Journal
# ----------------------------------------------------------------------------------------------------------
def test_scrub_keeps_token_counters_and_masks_secrets():
    out = _scrub({"input_tokens": 12, "output_tokens": 3, "max_tokens": 100, "api_token": "x",
                  "access_token": "y", "token": "z", "password": "p", "api_key": "k", "Authorization": "b"})
    assert out["input_tokens"] == 12 and out["output_tokens"] == 3 and out["max_tokens"] == 100
    for k in ("api_token", "access_token", "token", "password", "api_key", "Authorization"):
        assert out[k] == "***", k


def test_journal_write_failure_does_not_raise(tmp_path):
    j = Journal(tmp_path / "logs", component="t_review")
    j._file = lambda now: tmp_path / "nope" / "missing" / "journal.jsonl"  # répertoire inexistant → OSError
    j.error("boucle", detail="x")   # appelé dans les `except` des boucles : ne doit jamais lever
    rec = j.event("error", level="ERROR", message="boucle")
    assert rec["kind"] == "error"
    assert j.write_failures == 2
    # répertoire de nouveau accessible : l'écriture reprend normalement
    j._file = lambda now: tmp_path / "logs" / "journal-x.jsonl"
    j.event("weird", payload={1, 2, 3})  # set → json.dumps(default=str) le convertit, pas d'erreur
    assert j.write_failures == 2
    assert (tmp_path / "logs" / "journal-x.jsonl").exists()


# ----------------------------------------------------------------------------------------------------------
# Horloge : alignement serveur des barres H4/D1, ancrage W1 dimanche
# ----------------------------------------------------------------------------------------------------------
def test_bar_open_time_default_offset_unchanged():
    ts = datetime(2026, 1, 20, 10, 17, tzinfo=timezone.utc)
    assert clock.bar_open_time(ts, "M5") == datetime(2026, 1, 20, 10, 15, tzinfo=timezone.utc)
    assert clock.bar_open_time(ts, "H4") == datetime(2026, 1, 20, 8, 0, tzinfo=timezone.utc)
    assert clock.bar_open_time(ts, "D1") == datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc)


def test_bar_open_time_with_server_offset():
    ts = datetime(2026, 1, 20, 2, 30, tzinfo=timezone.utc)   # 05:30 serveur (UTC+3)
    off = 3 * 3600
    # H4 serveur ouverte à 04:00 serveur = 01:00 UTC ; sans décalage on aurait 00:00 UTC
    assert clock.bar_open_time(ts, "H4", off) == datetime(2026, 1, 20, 1, 0, tzinfo=timezone.utc)
    # D1 serveur ouverte à 00:00 serveur = 21:00 UTC la veille
    assert clock.bar_open_time(ts, "D1", off) == datetime(2026, 1, 19, 21, 0, tzinfo=timezone.utc)
    # timeframes intra-horaires inchangés
    assert clock.bar_open_time(ts, "H1", off) == datetime(2026, 1, 20, 2, 0, tzinfo=timezone.utc)
    # réglage global via set_server_utc_offset, remis à 0 ensuite
    clock.set_server_utc_offset(off)
    try:
        assert clock.bar_open_time(ts, "H4") == datetime(2026, 1, 20, 1, 0, tzinfo=timezone.utc)
    finally:
        clock.set_server_utc_offset(0)
    assert clock.SERVER_UTC_OFFSET_SEC == 0


def test_bar_open_time_w1_anchored_on_sunday():
    ts = datetime(2026, 1, 21, 15, 0, tzinfo=timezone.utc)  # mercredi
    w = clock.bar_open_time(ts, "W1")
    assert w.weekday() == 6 and w == datetime(2026, 1, 18, tzinfo=timezone.utc)
    # cohérent avec le resample W1 du MockBroker (bornes dimanche)
    b = MockBroker(seed=7, symbols=["EURUSD"])
    b.connect()
    df = b.rates("EURUSD", "W1", 5)
    assert all(t.weekday() == 6 for t in df["time"])


# ----------------------------------------------------------------------------------------------------------
# load_dotenv : BOM, export, commentaires en ligne, guillemets
# ----------------------------------------------------------------------------------------------------------
def test_load_dotenv_bom_export_comments_quotes(tmp_path, monkeypatch):
    for k in ("RV_LOGIN", "RV_PASSWORD", "RV_SERVER", "RV_KEY", "RV_QUOTED"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_bytes(
        b"\xef\xbb\xbfRV_LOGIN=5056132326 # demo\n"
        b"export RV_SERVER=MetaQuotes-Demo\n"
        b"RV_PASSWORD=\"abc # pas un commentaire\"\n"
        b"RV_KEY='k#1'   # commentaire\n"
        b"RV_QUOTED=plain  # deux espaces\n"
    )
    loaded = load_dotenv(env)
    assert os.environ["RV_LOGIN"] == "5056132326"
    assert int(os.environ["RV_LOGIN"]) == 5056132326
    assert os.environ["RV_SERVER"] == "MetaQuotes-Demo"
    assert os.environ["RV_PASSWORD"] == "abc # pas un commentaire"
    assert os.environ["RV_KEY"] == "k#1"
    assert os.environ["RV_QUOTED"] == "plain"
    assert loaded == ["RV_LOGIN", "RV_SERVER", "RV_PASSWORD", "RV_KEY", "RV_QUOTED"]


# ----------------------------------------------------------------------------------------------------------
# StateStore : save avec reprise, load tolérant, commandes sans perte, roll_day
# ----------------------------------------------------------------------------------------------------------
def test_state_save_retries_on_permission_error(tmp_path, monkeypatch):
    from tradinglab.core import state as state_mod
    store = StateStore(tmp_path / "state")
    store.state.equity = 123.0
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("sharing violation")
        return real(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", flaky)
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: None)
    store.save()
    assert calls["n"] == 3
    assert StateStore(tmp_path / "state").state.equity == 123.0


def test_state_load_keeps_last_state_on_oserror(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    store.state.equity = 42.0
    store.save()

    def locked(self, *a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_text", locked)
    st = store.reload()
    assert st.equity == 42.0
    assert not (tmp_path / "state" / "system_state.corrupt.json").exists()


def test_state_load_tolerates_invalid_sections(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    good = {"ticket": 1, "symbol": "EURUSD", "side": "BUY", "agent_id": "a", "candidate_id": "c", "entry": 1.1,
            "initial_sl": 1.09, "initial_volume": 0.1, "initial_risk_money": 10.0, "risk_percent": 0.1}
    bad = {"ticket": 2, "symbol": "GBPUSD"}  # champs obligatoires absents
    (d / "system_state.json").write_text(json.dumps({
        "mode": "SAFE_MODE", "daily": None, "model_budget": None, "executed_keys": {"k1": "ts"},
        "overall_peak_equity": 999.0, "consecutive_losses": 3, "bot_positions": {"1": good, "2": bad},
    }), encoding="utf-8")
    store = StateStore(d)
    st = store.state
    assert st.executed_keys == {"k1": "ts"} and st.overall_peak_equity == 999.0 and st.consecutive_losses == 3
    assert list(st.bot_positions) == ["1"]
    assert st.load_warnings and any("bot_position 2" in w for w in st.load_warnings)
    assert not (d / "system_state.corrupt.json").exists()
    # bot_positions null : l'état se charge quand même
    (d / "system_state.json").write_text(json.dumps({"bot_positions": None, "executed_keys": {"k2": "t"}}))
    st = StateStore(d).state
    assert st.bot_positions == {} and st.executed_keys == {"k2": "t"}


def test_pop_commands_does_not_lose_concurrent_push(tmp_path, monkeypatch):
    from tradinglab.core import state as state_mod
    store = StateStore(tmp_path / "state")
    store.push_command("PAUSE")
    real = os.replace

    def replace_then_push(src, dst):
        real(src, dst)
        # un autre processus pousse PANIC juste après le renommage (avant l'ancienne troncature)
        store.push_command("PANIC")

    monkeypatch.setattr(state_mod.os, "replace", replace_then_push)
    first = store.pop_commands()
    monkeypatch.setattr(state_mod.os, "replace", real)
    assert [c["command"] for c in first] == ["PAUSE"]
    assert [c["command"] for c in store.pop_commands()] == ["PANIC"]
    assert store.pop_commands() == []
    assert not store.commands_processing_path.exists()


def test_pop_commands_recovers_processing_file_after_crash(tmp_path):
    store = StateStore(tmp_path / "state")
    store.commands_processing_path.write_text(json.dumps({"command": "CLOSE_ALL_BOT"}) + "\n", encoding="utf-8")
    store.push_command("RESUME")
    cmds = [c["command"] for c in store.pop_commands()]
    assert cmds == ["CLOSE_ALL_BOT", "RESUME"]
    assert not store.commands_processing_path.exists()


def test_pop_commands_permission_error_keeps_commands(tmp_path, monkeypatch):
    from tradinglab.core import state as state_mod
    store = StateStore(tmp_path / "state")
    store.push_command("PANIC")

    def denied(src, dst):
        raise PermissionError("busy")

    monkeypatch.setattr(state_mod.os, "replace", denied)
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: None)
    assert store.pop_commands() == []
    monkeypatch.undo()
    assert [c["command"] for c in store.pop_commands()] == ["PANIC"]


def test_roll_day_with_zero_equity_does_not_freeze_day():
    st = SystemState()
    assert st.roll_day_if_needed(0.0, 0.0, FIXED_NOW.date()) is False
    assert st.daily.day != FIXED_NOW.date().isoformat()
    assert st.roll_day_if_needed(100000.0, 100000.0, FIXED_NOW.date()) is True
    assert st.daily.starting_equity == 100000.0
    # journée déjà ouverte avec une référence nulle : initialisée à la première equity valide
    st2 = SystemState()
    st2.daily.day = FIXED_NOW.date().isoformat()
    assert st2.roll_day_if_needed(50000.0, 50000.0, FIXED_NOW.date()) is False
    assert st2.daily.starting_equity == 50000.0
    st2.update_equity(49000.0, 50000.0)
    assert st2.daily_drawdown_percent() == pytest.approx(2.0)


# ----------------------------------------------------------------------------------------------------------
# MockBroker
# ----------------------------------------------------------------------------------------------------------
def test_mock_modify_position_refuses_zero_sl():
    b = MockBroker(seed=7)
    b.connect()
    b.set_now(FIXED_NOW)
    t = b.tick("EURUSD")
    res = b.order_send(OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.1, sl=t.ask - 0.0050, magic=51000))
    assert res.ok
    r0 = b.modify_position(res.ticket, 0.0, 0.0)
    assert not r0.ok and r0.retcode == -4
    assert b.position(res.ticket).sl == pytest.approx(t.ask - 0.0050)


def test_mock_live_stop_hit_on_new_bar_does_not_raise():
    b = MockBroker(seed=7, symbols=["EURUSD"], live=True)
    b.connect()
    start = b.now()
    b.set_now(start)
    spec = b.specs["EURUSD"]
    t = b.tick("EURUSD")
    dist = 1.5 * spec.min_stop_distance
    res = b.order_send(OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.1, sl=round(t.ask - dist, 5),
                                    tp=round(t.ask + dist, 5), magic=51000))
    assert res.ok
    b.set_now(start + timedelta(hours=6))
    acc = b.account_info()          # avant : RuntimeError « dictionary changed size during iteration »
    assert acc is not None
    assert b.positions() == []
    outs = [d for d in b._deals if d.entry == "OUT" and d.position_id == res.ticket]
    assert len(outs) == 1 and outs[0].comment in ("sl", "tp")


# ----------------------------------------------------------------------------------------------------------
# MT5Adapter avec un stub du package MetaTrader5 (jamais exécuté sous Linux sinon)
# ----------------------------------------------------------------------------------------------------------
class _Stub:
    """Stub minimal du module MetaTrader5 : constantes + fonctions pilotées par les tests."""
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 2, 3, 4, 5
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2

    def __init__(self):
        self.tick_value = None
        self.sent = []
        self.positions = []
        self.init_ok = True
        self.init_raises = False
        self.symbols = []

    def last_error(self):
        return (1, "stub")

    def initialize(self, **kw):
        if self.init_raises:
            raise RuntimeError("terminal absent")
        return self.init_ok

    def account_info(self):
        return _types.SimpleNamespace(login=1, server="s", trade_mode=0)

    def shutdown(self):
        pass

    def terminal_info(self):
        return _types.SimpleNamespace(connected=True)

    def symbols_get(self):
        return self.symbols

    def symbol_info_tick(self, symbol):
        return self.tick_value

    def symbol_info(self, symbol):
        return _types.SimpleNamespace(filling_mode=1, visible=False)

    def symbol_select(self, symbol, flag):
        return False

    def positions_get(self, **kw):
        return self.positions

    def order_send(self, r):
        self.sent.append(r)
        return _types.SimpleNamespace(retcode=10009, order=1, deal=1, price=1.0, volume=0.1, comment="ok")

    def order_check(self, r):
        return self.order_send(r)


@pytest.fixture
def stub(monkeypatch):
    st = _Stub()
    monkeypatch.setattr(mt5_adapter, "mt5", st)
    monkeypatch.setattr(mt5_adapter, "MT5_AVAILABLE", True)
    yield st
    clock.set_server_utc_offset(0)


def test_mt5_connect_invalid_login_returns_false(stub, monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "5056132326 # demo")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "MetaQuotes-Demo")
    a = mt5_adapter.MT5Adapter()
    assert a.connect() is False
    assert "MT5_LOGIN" in a.last_error()
    assert a.is_connected() is False


def test_mt5_connect_initialize_exception_returns_false(stub, monkeypatch):
    monkeypatch.delenv("MT5_LOGIN", raising=False)
    stub.init_raises = True
    a = mt5_adapter.MT5Adapter()
    assert a.connect() is False
    assert "initialize" in a.last_error()


def test_mt5_symbol_info_none_after_failed_select(stub):
    a = mt5_adapter.MT5Adapter()
    assert a.symbol_info("EURUSD") is None  # visible=False, symbol_select → False : None, pas AttributeError


def test_mt5_order_send_without_tick_returns_result(stub):
    a = mt5_adapter.MT5Adapter()
    a._connected = True
    req = OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.1, sl=1.09, magic=51000)
    res = a.order_send(req)
    assert not res.ok and res.retcode == -3 and "tick" in res.comment
    assert stub.sent == []  # rien n'a été envoyé
    assert a.order_check(req).retcode == -3
    stub.positions = [_types.SimpleNamespace(ticket=7, symbol="EURUSD", type=0, volume=0.1, magic=51000)]
    res = a.close_position(7)
    assert not res.ok and res.retcode == -3
    # exception interne → OrderResult -9, jamais d'exception
    stub.tick_value = _types.SimpleNamespace(bid=1.1, ask=1.1001, time=0, time_msc=0)
    stub.order_send = lambda r: (_ for _ in ()).throw(RuntimeError("IPC"))
    res = a.close_position(7)
    assert not res.ok and res.retcode == -9 and "IPC" in res.comment


def test_mt5_modify_position_refuses_zero_sl(stub):
    a = mt5_adapter.MT5Adapter()
    stub.positions = [_types.SimpleNamespace(ticket=7, symbol="EURUSD", type=0, volume=0.1, magic=51000)]
    res = a.modify_position(7, 0.0, 1.2)
    assert not res.ok and res.retcode == -4
    assert stub.sent == []
    assert a.modify_position(7, 1.09, 0.0).ok


def test_mt5_server_offset_calibration_and_utc_conversion(stub, monkeypatch):
    now_pc = 1_800_000_000.0
    monkeypatch.setattr(mt5_adapter.time, "time", lambda: now_pc)
    # serveur UTC+2 : le tick « maintenant » porte un epoch 7200 s dans le futur
    stub.symbols = [_types.SimpleNamespace(name="EURUSD", visible=True)]
    stub.tick_value = _types.SimpleNamespace(bid=1.1, ask=1.1001, last=0.0, volume=0,
                                             time=int(now_pc + 7200), time_msc=int((now_pc + 7200) * 1000))
    a = mt5_adapter.MT5Adapter()
    a._connected = True
    a._calibrate_offset(force=True)
    assert a.server_offset_sec == 7200
    assert clock.SERVER_UTC_OFFSET_SEC == 7200
    t = a.tick("EURUSD")
    now_dt = datetime.fromtimestamp(now_pc, tz=timezone.utc)
    assert abs((t.time - now_dt).total_seconds()) < 1.0
    assert 0 <= t.age_seconds(a.server_time()) < 1.0   # âge cohérent, plus d'âge négatif de 2 h
    # flux gelé : le tick de référence n'avance plus alors que l'horloge PC avance ; la mesure brute
    # dérive (−600 s par mesure) et l'offset ne doit PAS diminuer (la staleness reste visible)
    frozen = now_pc + 7200
    stub.tick_value = _types.SimpleNamespace(bid=1.1, ask=1.1001, last=0.0, volume=0,
                                             time=int(frozen), time_msc=int(frozen * 1000))
    clock_pc = {"t": now_pc}
    monkeypatch.setattr(mt5_adapter.time, "time", lambda: clock_pc["t"])
    for k in range(1, 8):
        clock_pc["t"] = now_pc + 600 * k
        a._calibrate_offset(force=True)
        assert a.server_offset_sec == 7200, k
    assert a.tick("EURUSD").age_seconds(a.server_time()) == pytest.approx(4200.0, abs=1.0)
    # recul d'heure serveur (UTC+2 → UTC+1) : mesure brute STABLE → acceptée après confirmation
    for k in range(8, 11):
        clock_pc["t"] = now_pc + 600 * k
        back = clock_pc["t"] + 3600
        stub.tick_value = _types.SimpleNamespace(bid=1.1, ask=1.1001, last=0.0, volume=0,
                                                 time=int(back), time_msc=int(back * 1000))
        a._calibrate_offset(force=True)
    assert a.server_offset_sec == 3600
    assert a.tick("EURUSD").age_seconds(a.server_time()) == pytest.approx(0.0, abs=1.0)
    # avance d'heure serveur (UTC+1 → UTC+2) : ticks dans le futur avec l'ancien offset → acceptée aussitôt
    fwd = clock_pc["t"] + 7200
    stub.tick_value = _types.SimpleNamespace(bid=1.1, ask=1.1001, last=0.0, volume=0,
                                             time=int(fwd), time_msc=int(fwd * 1000))
    a._calibrate_offset(force=True)
    assert a.server_offset_sec == 7200


def test_mt5_offset_not_calibrated_when_market_closed(stub, monkeypatch):
    now_pc = 1_800_000_000.0
    monkeypatch.setattr(mt5_adapter.time, "time", lambda: now_pc)
    old = now_pc - 48 * 3600  # dernier tick vendredi soir
    stub.symbols = [_types.SimpleNamespace(name="EURUSD", visible=True)]
    stub.tick_value = _types.SimpleNamespace(bid=1.1, ask=1.1001, last=0.0, volume=0, time=int(old),
                                             time_msc=int(old * 1000))
    a = mt5_adapter.MT5Adapter()
    a._connected = True
    assert a._calibrate_offset(force=True) is None
    assert a.server_offset_sec is None
    # sans calibration, server_time() se cale sur le tick de référence : aucun âge négatif de plusieurs heures
    assert a.tick("EURUSD").age_seconds(a.server_time()) == pytest.approx(0.0, abs=1.0)
