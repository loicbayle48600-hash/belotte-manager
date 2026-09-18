"""Tests du dashboard local en lecture seule (http.server)."""
from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradinglab.core.state import StateStore, SystemState
from tradinglab.dashboards.server import create_server

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    shutil.copytree(PROJECT / "config", tmp_path / "config")
    # Un .env factice : il ne doit JAMAIS être lu ni exposé par le dashboard.
    (tmp_path / ".env").write_text("MT5_PASSWORD=super-secret-xyz\n", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    st = SystemState(
        mode="SAFE_MODE",
        mt5_connected=True,
        account_trade_mode="DEMO",
        account_login=123456,
        account_server="MetaQuotes-Demo",
        equity=10100.0,
        balance=10000.0,
        currency="EUR",
        regimes={"EURUSD": "TRENDING"},
        active_agents=["agent_a"],
        orchestrator_heartbeat=datetime.now(timezone.utc).isoformat(),
    )
    st.daily.starting_equity = 10000.0
    store.save(st)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logs = tmp_path / "logs"
    logs.mkdir()
    with open(logs / f"journal-{day}.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "kind": "info", "message": "hello"}) + "\n")
        f.write(json.dumps({"ts_utc": "2026-01-01T00:00:01+00:00", "kind": "order", "symbol": "EURUSD"}) + "\n")
        f.write("ligne invalide\n")
    return tmp_path


@pytest.fixture
def server(home: Path):
    srv = create_server(home, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _get(srv, path: str):
    host, port = srv.server_address[:2]
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")


def test_api_state(server):
    status, ctype, body = _get(server, "/api/state")
    assert status == 200 and ctype.startswith("application/json")
    data = json.loads(body)
    assert data["mode"] == "SAFE_MODE"
    assert data["equity"] == 10100.0
    assert data["account_trade_mode"] == "DEMO"
    assert data["daily_pnl"] == 100.0
    assert data["prop"]["prop_firm"] == "FOXX_FUNDED"
    assert data["prop"]["autonomous_prop"] is False
    assert data["risk"]["max_open_positions"] == 3
    assert isinstance(data["orchestrator_heartbeat_age_sec"], (int, float))
    assert data["orchestrator_heartbeat_age_sec"] < 45
    assert data["watchdog_heartbeat_age_sec"] is None  # jamais de heartbeat -> inconnu (pas Infinity)
    assert "server_time_utc" in data
    assert data["learning"] == {} and data["leaderboard"] == []
    assert [e["kind"] for e in data["journal_tail"]] == ["info", "order"]
    assert "executed_keys" not in data
    assert "super-secret-xyz" not in body


def test_api_state_kinds_filter(server):
    _, _, body = _get(server, "/api/state?kinds=order,gate")
    data = json.loads(body)
    assert [e["kind"] for e in data["journal_tail"]] == ["order"]


def test_index_html(server):
    status, ctype, body = _get(server, "/")
    assert status == 200 and ctype.startswith("text/html")
    assert "Dashboard" in body
    assert "lecture seule" in body


def test_api_journal(server):
    _, _, body = _get(server, "/api/journal")
    data = json.loads(body)
    assert isinstance(data, list) and len(data) == 2
    _, _, body = _get(server, "/api/journal?day=1999-01-01")
    assert json.loads(body) == []
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/journal?day=bad")
    assert exc.value.code == 400


def test_read_only_and_unknown_route(server):
    host, port = server.server_address[:2]
    req = urllib.request.Request(f"http://{host}:{port}/api/state", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 405
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/nope")
    assert exc.value.code == 404


def test_learning_and_leaderboard_files(home: Path):
    (home / "data").mkdir()
    (home / "data" / "agent_stats.json").write_text(json.dumps({"agent_a": {"trades": 3}}), encoding="utf-8")
    (home / "reports").mkdir()
    (home / "reports" / "leaderboard.json").write_text(json.dumps([{"agent_id": "agent_a", "status": "SHADOW"}]), encoding="utf-8")
    srv = create_server(home, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        _, _, body = _get(srv, "/api/state")
    finally:
        srv.shutdown()
        srv.server_close()
    data = json.loads(body)
    assert data["learning"] == {"agent_a": {"trades": 3}}
    assert data["leaderboard"][0]["agent_id"] == "agent_a"
