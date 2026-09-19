"""Tests de revue — zone « ops-api-scripts » (watchdog, CLI, MCP, dashboard, smoke test, scripts).

Chaque test correspond à une correction et échouait avant celle-ci.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import threading
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from tradinglab.api import cli as cli_mod
from tradinglab.core.journal import Journal
from tradinglab.core.state import StateStore
from tradinglab.core.types import Side, utcnow
from tradinglab.dashboards import server as dash_mod
from tradinglab.dashboards.server import create_server, read_journal_day, read_journal_tail
from tradinglab.mcp import server as mcp_mod
from tradinglab.monitoring import watchdog as wd_mod
from tradinglab.monitoring.watchdog import Watchdog, read_watchdog_report
from tradinglab.risk.risk_manager import SizingResult

from conftest import FIXED_NOW

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_test_demo", SCRIPTS / "smoke_test_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# smoke_test_demo.py
# ---------------------------------------------------------------------------
def _open_foreign_buy(broker, magic: int, comment: str = "AGENT:ORCH"):
    """Position BUY EURUSD du bot (même magic) qui n'appartient PAS au smoke test."""
    from tradinglab.core.types import OrderRequest
    t = broker.tick("EURUSD")
    req = OrderRequest(symbol="EURUSD", side=Side.BUY, volume=0.01, sl=round(t.ask - 0.0050, 5), tp=0.0, magic=magic, comment=comment)
    res = broker.order_send(req)
    assert res.ok
    return broker.position(res.ticket)


def test_find_smoke_position_never_selects_a_foreign_position(broker, settings):
    smoke = _load_smoke()
    foreign = _open_foreign_buy(broker, settings.magic)
    # ticket introuvable (ticket heuristique MT5) : l'ancien repli prenait la première position du magic
    assert smoke.find_smoke_position(broker, 999999, "EURUSD", settings.magic) is None
    # une position de test (commentaire TLAB:SMOKE) est retrouvée, la plus récente
    mine = _open_foreign_buy(broker, settings.magic, comment="TLAB:SMOKE")
    found = smoke.find_smoke_position(broker, 999999, "EURUSD", settings.magic)
    assert found is not None and found.ticket == mine.ticket and found.ticket != foreign.ticket
    # trouvée par ticket mais sans le commentaire de test → refus (aucune action sur cette position)
    assert smoke.find_smoke_position(broker, foreign.ticket, "EURUSD", settings.magic) is None


def _run_smoke_main(monkeypatch, home, broker, capsys):
    smoke = _load_smoke()
    monkeypatch.setattr(smoke, "make_broker", lambda kind, s: broker)
    monkeypatch.setattr(smoke, "forex_market_open", lambda dt: True)
    monkeypatch.setattr(smoke.time, "sleep", lambda x: None)
    monkeypatch.setattr(sys, "argv", ["smoke", "--home", str(home), "--broker", "mock", "--yes"])
    rc = smoke.main()
    return rc, capsys.readouterr().out


def test_smoke_main_picks_its_own_position_not_the_foreign_one(monkeypatch, home, settings, broker, capsys):
    """Ticket non retrouvé après fill (position() None) : le repli retrouve la position TLAB:SMOKE,
    jamais la première position du magic (ici une position de l'orchestrateur, ouverte AVANT)."""
    foreign = _open_foreign_buy(broker, settings.magic)
    sl_before = foreign.sl
    monkeypatch.setattr(broker, "position", lambda ticket: None)  # simule MT5 : position() None juste après le fill
    rc, out = _run_smoke_main(monkeypatch, home, broker, capsys)
    assert rc == 0 and "REUSSI" in out
    still = [p for p in broker.positions(magic=settings.magic) if p.ticket == foreign.ticket]
    assert still and still[0].sl == sl_before, "la position étrangère doit rester ouverte et intacte"
    assert len(broker.positions(magic=settings.magic)) == 1  # la position de test, elle, est bien fermée


def test_smoke_main_aborts_instead_of_touching_foreign_position(monkeypatch, home, settings, broker, capsys):
    """order_send « ok » (ticket heuristique) mais aucune position de test visible + position étrangère
    sur le symbole : abandon (code 10) sans aucune modification/fermeture de la position étrangère."""
    from tradinglab.core.types import OrderResult
    foreign = _open_foreign_buy(broker, settings.magic)
    sl_before = foreign.sl
    monkeypatch.setattr(broker, "order_send", lambda req: OrderResult(ok=True, retcode=10009, ticket=424242, comment="done", request=req.to_dict()))
    rc, out = _run_smoke_main(monkeypatch, home, broker, capsys)
    assert rc == 10 and "introuvable" in out and "REUSSI" not in out
    still = [p for p in broker.positions(magic=settings.magic) if p.ticket == foreign.ticket]
    assert still and still[0].sl == sl_before, "la position étrangère doit rester ouverte et intacte"


def test_smoke_main_detects_open_position_by_ticket_not_comment(monkeypatch, home, settings, broker, capsys):
    """close « ok » mais position toujours ouverte avec commentaire réécrit par le broker → ECHEC (code 12)."""
    from tradinglab.core.types import OrderResult

    def fake_close(ticket, volume=None, comment=""):
        broker._positions[ticket].comment = "[sl]"  # broker qui réécrit le commentaire
        return OrderResult(ok=True, retcode=10009, ticket=ticket, comment="accepted")

    monkeypatch.setattr(broker, "close_position", fake_close)
    rc, out = _run_smoke_main(monkeypatch, home, broker, capsys)
    assert rc == 12 and "encore ouverte" in out and "REUSSI" not in out
    assert broker.positions(magic=settings.magic)


def test_check_smoke_sizing_refuses_when_risk_not_computable(broker):
    smoke = _load_smoke()
    spec = broker.symbol_info("EURUSD")
    # sizing en échec pour specs incomplètes (tick_value=0 chez MT5) : loss_per_lot = 0 → REFUS, pas d'ordre
    vol, refus = smoke.check_smoke_sizing(SizingResult(False, reason="spécifications symbole incomplètes (tick_value/tick_size)"), spec, 100000.0, 0.35)
    assert vol == 0.0 and refus
    vol, refus = smoke.check_smoke_sizing(SizingResult(False, reason="perte par lot nulle"), spec, 100000.0, 0.35)
    assert vol == 0.0 and refus
    # volume minimum au-dessus du risque → refus explicite
    vol, refus = smoke.check_smoke_sizing(SizingResult(False, loss_per_lot=100000.0, reason="volume minimum trop risqué"), spec, 1000.0, 0.35)
    assert vol == 0.0 and "risque max" in refus
    # cas nominal
    vol, refus = smoke.check_smoke_sizing(SizingResult(True, volume=0.05, loss_per_lot=300.0), spec, 100000.0, 0.35)
    assert vol == 0.05 and refus == ""


def test_smoke_never_widens_stop_helper_used():
    src = (SCRIPTS / "smoke_test_demo.py").read_text(encoding="utf-8")
    assert "is_tighter_or_equal(Side.BUY, new_sl, pos.sl)" in src


# ---------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------
def test_watchdog_stale_ticks_add_reason_and_request_safe_mode(settings, broker):
    store = StateStore(settings.state_dir)
    broker.stale_ticks = True  # tick daté de 30 min : flux figé
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"), reference_symbol="EURUSD")
    rep = wd.check_once()
    assert rep.data_fresh is False
    assert any("données périmées" in r for r in rep.reasons) and rep.safe_mode_request
    assert read_watchdog_report(settings.state_dir)["safe_mode_request"] is True


def test_watchdog_resolves_reference_symbol_with_broker_suffix(settings, broker):
    """Racine EURUSD → symbole broker EURUSD.m : sans résolution, tick() renverrait None (faux « périmé »)."""
    store = StateStore(settings.state_dir)
    orig_tick = broker.tick
    monkeypatch_symbols = ["EURUSD.m", "GBPUSD.m"]
    broker.symbols = lambda: list(monkeypatch_symbols)
    broker.tick = lambda sym, force=False: orig_tick("EURUSD", force) if sym == "EURUSD.m" else None
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"), reference_symbol="EURUSD")
    rep = wd.check_once()
    assert rep.data_fresh is True and not rep.reasons


def test_watchdog_main_passes_reference_symbol(monkeypatch, home, settings):
    captured = {}

    def fake_run(self):
        captured["wd"] = self

    monkeypatch.setattr(Watchdog, "run", fake_run)
    rc = wd_mod.main(["--home", str(home), "--broker", "mock"])
    assert rc == 0
    assert captured["wd"].ref_symbol == "EURUSD"  # premier symbole de markets.yaml


def test_read_watchdog_report_tolerates_io_errors(settings, monkeypatch):
    p = settings.state_dir / "watchdog.json"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"heartbeat": utcnow().isoformat(), "safe_mode_request": False}), encoding="utf-8")
    assert read_watchdog_report(settings.state_dir)["safe_mode_request"] is False

    def boom(self, *a, **k):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(Path, "read_text", boom)
    assert read_watchdog_report(settings.state_dir) is None  # jamais d'exception dans le chemin critique


def test_read_watchdog_report_rejects_non_object(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "watchdog.json").write_text("[1, 2]", encoding="utf-8")
    assert read_watchdog_report(settings.state_dir) is None


def test_watchdog_write_retries_on_permission_error(settings, broker, monkeypatch):
    store = StateStore(settings.state_dir)
    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(wd_mod.os, "replace", flaky_replace)
    monkeypatch.setattr(wd_mod.time, "sleep", lambda x: None)
    wd = Watchdog(settings, broker, store, Journal(settings.logs_dir, component="wd"), reference_symbol="EURUSD")
    rep = wd.check_once()
    assert calls["n"] == 3
    assert read_watchdog_report(settings.state_dir)["heartbeat"] == rep.heartbeat


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------
def _call_tool(srv, name, args):
    res = asyncio.run(srv.call_tool(name, args))
    content = res.content if hasattr(res, "content") else res
    text = content[0].text if hasattr(content[0], "text") else content[0]["text"]
    return json.loads(text)


def test_mcp_command_rejects_resume(home, settings):
    assert "RESUME" not in mcp_mod.MCP_ALLOWED_COMMANDS
    srv = mcp_mod.build_server(home)
    out = _call_tool(srv, "command", {"name": "resume"})
    assert out["ok"] is False and "RESUME" in out["error"]
    # aucune commande RESUME n'a été déposée dans la file de l'orchestrateur
    cmds = settings.state_dir / "commands.jsonl"
    assert not cmds.exists() or "RESUME" not in cmds.read_text(encoding="utf-8")
    # les commandes de sécurité restent acceptées
    out = _call_tool(srv, "command", {"name": "PAUSE"})
    assert out.get("ok", True) is not False
    assert "PAUSE" in cmds.read_text(encoding="utf-8")


def test_mcp_ensure_broker_reconnects(monkeypatch, settings):
    class FakeBroker:
        def __init__(self):
            self.connected = False
            self.attempts = 0

        def connect(self):
            self.attempts += 1
            self.connected = self.attempts >= 2  # MT5 pas prêt au 1er appel
            return self.connected

        def is_connected(self):
            return self.connected

    fb = FakeBroker()
    monkeypatch.setattr(mcp_mod, "make_broker", lambda kind, s: fb)
    cache: dict = {}
    assert mcp_mod.ensure_broker(cache, settings) is fb and not fb.connected
    assert mcp_mod.ensure_broker(cache, settings) is fb and fb.connected and fb.attempts == 2
    mcp_mod.ensure_broker(cache, settings)
    assert fb.attempts == 2  # connecté : plus de tentative inutile
    fb.connected = False  # terminal redémarré
    mcp_mod.ensure_broker(cache, settings)
    assert fb.attempts == 3 and fb.connected


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_status_json_is_strict_without_infinity(home, settings, capsys):
    rc = cli_mod.main(["STATUS", "--home", str(home), "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and "Infinity" not in out and "NaN" not in out
    data = json.loads(out)
    assert data["orchestrator_heartbeat_age_sec"] is None  # aucun heartbeat → UNKNOWN, pas Infinity


def test_cli_json_safe_normalises():
    assert cli_mod.json_safe({"a": float("inf"), "b": [float("nan"), 1.5], "c": {1, }}) == {"a": None, "b": [None, 1.5], "c": [1]}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _write_journal(logs: Path, day: str, n: int, sparse_kind_first: int = 0) -> Path:
    logs.mkdir(parents=True, exist_ok=True)
    f = logs / f"journal-{day}.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for i in range(n):
            kind = "order" if i < sparse_kind_first else "gate"
            fh.write(json.dumps({"ts_utc": f"2026-01-01T00:00:{i:02d}", "kind": kind, "i": i, "pad": "x" * 400}) + "\n")
        fh.write("ligne invalide\n")
    return f


def test_read_journal_tail_matches_full_read_without_reading_whole_file(tmp_path):
    day = utcnow().strftime("%Y-%m-%d")
    f = _write_journal(tmp_path / "logs", day, 3000)
    full = read_journal_day(tmp_path / "logs")
    tail = read_journal_tail(tmp_path / "logs", 50, window=4096)
    assert tail == full[-50:] and [e["i"] for e in tail] == list(range(2950, 3000))
    # fenêtre initiale volontairement plus petite qu'une ligne : la première ligne partielle est ignorée
    assert read_journal_tail(tmp_path / "logs", 5, window=100) == full[-5:]
    # filtre kinds rare (événements seulement en début de fichier) : élargissement jusqu'au début
    _write_journal(tmp_path / "logs2", day, 3000, sparse_kind_first=10)
    orders = read_journal_tail(tmp_path / "logs2", 50, kinds={"order"}, window=4096)
    assert [e["i"] for e in orders] == list(range(10))
    assert read_journal_tail(tmp_path / "logs2", 50, kinds={"absent"}, window=4096) == []
    assert read_journal_tail(tmp_path / "nope", 50) == []
    assert f.stat().st_size > 1_000_000


def test_read_journal_tail_reads_only_end_of_file(tmp_path, monkeypatch):
    day = utcnow().strftime("%Y-%m-%d")
    _write_journal(tmp_path / "logs", day, 3000)
    read_bytes = {"n": 0}
    real_open = open

    def counting_open(file, mode="r", *a, **k):
        fh = real_open(file, mode, *a, **k)
        if "b" in mode:
            orig = fh.read

            def read(*ra, **rk):
                data = orig(*ra, **rk)
                read_bytes["n"] += len(data)
                return data
            fh.read = read
        return fh

    monkeypatch.setattr("builtins.open", counting_open)
    tail = read_journal_tail(tmp_path / "logs", 50)
    assert len(tail) == 50 and read_bytes["n"] <= dash_mod.JOURNAL_TAIL_WINDOW_BYTES


@pytest.fixture
def dash_home(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    store = StateStore(tmp_path / "state")
    store.save()  # aucun watchdog_heartbeat recopié par l'orchestrateur (orchestrateur mort)
    (tmp_path / "state" / "watchdog.json").write_text(json.dumps({
        "heartbeat": utcnow().isoformat(), "safe_mode_request": True, "reasons": ["données périmées"],
        "actions": ["ticket 1: fermé"], "positions_without_sl": 1, "data_fresh": False, "orchestrator_alive": False,
    }), encoding="utf-8")
    return tmp_path


def test_dashboard_reads_watchdog_report_directly(dash_home):
    srv = create_server(dash_home, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
    assert isinstance(data["watchdog_heartbeat_age_sec"], (int, float)) and data["watchdog_heartbeat_age_sec"] < 45
    assert data["watchdog"]["safe_mode_request"] is True and data["watchdog"]["reasons"] == ["données périmées"]
    assert data["watchdog"]["positions_without_sl"] == 1 and data["watchdog"]["data_fresh"] is False
    assert 'id="wd_safe"' in html and 'id="wd_reasons"' in html


def test_dashboard_heartbeat_age_helper():
    assert dash_mod._heartbeat_age(None) is None and dash_mod._heartbeat_age("pas une date") is None
    assert dash_mod._heartbeat_age("2026-01-01T00:00:00") is None  # naïf : inconnu plutôt qu'inventé
    assert 0 <= dash_mod._heartbeat_age((utcnow() - timedelta(seconds=3)).isoformat()) < 10


# ---------------------------------------------------------------------------
# Scripts PowerShell (vérifications statiques : pas de PowerShell sous Linux)
# ---------------------------------------------------------------------------
def test_install_windows_robocopy_never_overwrites_env_data_reports():
    src = (SCRIPTS / "install_windows.ps1").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if "Invoke-NativeStream 'robocopy'" in l)
    for token in ("'data'", "'reports'", "'backups'", "'.env'", "'*.db'", "'state'", "'logs'"):
        assert token in line, token


def test_start_all_exits_non_zero_when_component_died_and_handles_null_commandline():
    src = (SCRIPTS / "start_all.ps1").read_text(encoding="utf-8")
    assert "Where-Object { -not $_.vivant }" in src and "exit 1" in src
    assert src.rstrip().endswith("exit 0")
    assert "-not $p.CommandLine" in src  # CommandLine illisible → considéré présent, jamais relancé en double


def test_stop_all_waits_for_effective_pause_and_cleans_pending_pause():
    src = (SCRIPTS / "stop_all.ps1").read_text(encoding="utf-8")
    assert "[Math]::Max($GraceSec, 20)" in src
    assert "(Get-SystemMode) -eq 'PAUSED'" in src
    assert "function Remove-PendingPause" in src and "$c.command" in src
