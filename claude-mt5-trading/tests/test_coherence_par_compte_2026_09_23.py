"""Statistiques et cohérence 25 % PAR COMPTE dans le dashboard (2026-09-23, demande utilisateur).

Avant : l'onglet d'un compte suiveur ne montrait que l'equity, les positions et les dernières réplications ;
la cohérence n'était calculée que pour le maître, et seulement dans RISK. Désormais :
- chaque copieur exporte les trades fermés de son compte (deals MT5 à son magic, 90 jours, rafraîchis
  toutes les 60 s) dans `state/copy_status_<prefix>.json` (`closed`) ;
- `learning/consistency.py` reconstitue les idées de trade (même symbole, même sens, réouverture < 10 min)
  et calcule la part de chaque idée dans le profit net ; `account_stats` produit cartes, séries et par paire ;
- `/api/stats` porte `consistency` pour le maître (trades live de `learning.db`) et `accounts[i].stats`
  (dont `consistency`) pour chaque suiveur ; la page Statistiques trace un graphique de cohérence par compte.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.learning.consistency import account_stats, consistency_from_trades, group_ideas, trades_from_deals  # noqa: E402

T0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def _t(symbol, side, pnl, opened_min, closed_min):
    return {"symbol": symbol, "side": side, "pnl": pnl, "opened_at": (T0 + timedelta(minutes=opened_min)).isoformat(),
            "closed_at": (T0 + timedelta(minutes=closed_min)).isoformat()}


def test_idees_regroupees_meme_sens_reouverture_sous_10_min():
    trades = [_t("EURUSD", "BUY", 100, 0, 30), _t("EURUSD", "BUY", 50, 35, 60),      # rouvert 5 min après : même idée
              _t("EURUSD", "BUY", 80, 90, 120),                                       # 30 min après : nouvelle idée
              _t("EURUSD", "SELL", -40, 0, 10), _t("XAUUSD", "BUY", 300, 0, 5)]
    ideas = group_ideas(trades)
    assert [(i["symbol"], i["side"], i["pnl"], i["trades"]) for i in ideas] == [
        ("EURUSD", "BUY", 150.0, 2), ("EURUSD", "SELL", -40.0, 1), ("XAUUSD", "BUY", 300.0, 1), ("EURUSD", "BUY", 80.0, 1)]


def test_part_de_la_meilleure_idee_sur_le_profit_net():
    trades = [_t("EURUSD", "BUY", 100, 0, 30), _t("XAUUSD", "BUY", 300, 0, 5), _t("GBPUSD", "SELL", -100, 0, 5)]
    cons = consistency_from_trades(trades)
    assert cons["total_profit"] == 300.0 and cons["share_percent"] == 100.0 and not cons["within_limit"]
    assert cons["ideas"][0]["symbol"] == "XAUUSD" and cons["ideas"][0]["share_percent"] == 100.0
    assert cons["ideas"][-1]["share_percent"] == 0.0, "une idée perdante ne détient aucune part"
    vide = consistency_from_trades([])
    assert vide["share_percent"] == 0.0 and vide["within_limit"] and "aucun profit" in vide["detail"]
    perte = consistency_from_trades([_t("EURUSD", "BUY", -50, 0, 5)])
    assert perte["total_profit"] == -50.0 and perte["share_percent"] == 0.0


def test_trades_reconstitues_depuis_les_deals_mt5():
    def deal(pid, entry, side, profit, minute, magic=52000, comment=""):
        return SimpleNamespace(position_id=pid, symbol="EURUSD", side=side, volume=0.1, profit=profit, commission=-0.7,
                               swap=0.0, time=T0 + timedelta(minutes=minute), magic=magic, entry=entry, comment=comment)
    deals = [deal(1, "IN", "BUY", 0.0, 0, comment="TLABCOPY 99"), deal(1, "OUT", "SELL", 120.0, 30),
             deal(2, "IN", "SELL", 0.0, 5), deal(2, "OUT", "BUY", -60.0, 15),
             deal(3, "IN", "BUY", 0.0, 40),                                        # encore ouverte : pas un trade
             deal(4, "IN", "BUY", 0.0, 0, magic=51000), deal(4, "OUT", "SELL", 500.0, 1, magic=51000)]   # autre magic
    trades = trades_from_deals(deals, magic=52000)
    assert [(t["position_id"], t["side"], t["pnl"]) for t in trades] == [(2, "SELL", -61.4), (1, "BUY", 118.6)]
    assert trades[1]["comment"] == "TLABCOPY 99" and trades[1]["opened_at"].startswith("2026-09-23T10:00")
    # sans deal IN (historique tronqué) : le sens vient du deal de sortie, inversé
    seul = trades_from_deals([deal(9, "OUT", "SELL", 10.0, 3)], magic=52000)
    assert seul[0]["side"] == "BUY"


def test_stats_par_compte_series_et_coherence():
    trades = [_t("EURUSD", "BUY", 100, 0, 30), _t("XAUUSD", "BUY", 300, 0, 5), _t("GBPUSD", "SELL", -100, 0, 5)]
    st = account_stats(trades, lambda d: d.date().isoformat())
    assert st["total"] == {"trades": 3, "wins": 2, "win_rate": 66.7, "pnl": 300.0, "best": 300.0, "worst": -100.0}
    assert st["daily"][-1]["key"] == "2026-09-23" and st["daily"][-1]["n"] == 3
    assert st["symbols"][0]["symbol"] == "XAUUSD" and st["consistency"]["share_percent"] == 100.0
    assert account_stats([], lambda d: d.date().isoformat())["total"]["trades"] == 0


def test_le_copieur_exporte_les_trades_fermes(tmp_path):
    from tradinglab.copy.copier import CopyTrader
    from tradinglab.mt5.mock_adapter import MockBroker

    from test_copy_trading import master, mpos

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "master_positions.json"
    status = tmp_path / "copy_status_COPYX.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())),
                     encoding="utf-8")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, status_file=status, stale_after_sec=10_000)
    assert [a.kind for a in tr.sync(now=b.now())] == ["open"]
    assert json.loads(status.read_text(encoding="utf-8"))["closed"] == []
    mfile.write_text(json.dumps(master(positions=[], ts=b.now())), encoding="utf-8")
    assert [a.kind for a in tr.sync(now=b.now() + timedelta(seconds=5))] == ["close"]
    # cache 60 s : la clôture apparaît au sync suivant la fenêtre de rafraîchissement
    tr.sync(now=b.now() + timedelta(seconds=70))
    closed = json.loads(status.read_text(encoding="utf-8"))["closed"]
    assert len(closed) == 1 and closed[0]["symbol"] == "EURUSD" and closed[0]["side"] == "BUY" and "pnl" in closed[0]
    assert closed[0]["comment"].startswith("TLABCOPY 1")


def test_api_stats_porte_la_coherence_du_maitre_et_de_chaque_compte(home: Path, monkeypatch):
    import sqlite3
    import threading
    import urllib.request

    from tradinglab.copy.registry import register_follower
    from tradinglab.dashboards.server import create_server

    # maître : deux idées gagnantes en base d'apprentissage
    (home / "data").mkdir(exist_ok=True)
    con = sqlite3.connect(home / "data" / "learning.db")
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mode TEXT, symbol TEXT, side TEXT, pnl REAL, opened_at TEXT, closed_at TEXT)")
    for sym, side, pnl in (("EURUSD", "BUY", 900.0), ("XAUUSD", "SELL", 100.0)):
        con.execute("INSERT INTO trades (mode, symbol, side, pnl, opened_at, closed_at) VALUES ('live',?,?,?,?,?)",
                    (sym, side, pnl, T0.isoformat(), (T0 + timedelta(minutes=30)).isoformat()))
    con.commit(); con.close()
    # suiveur : fichier d'état écrit par son copieur, avec trades fermés
    (home / "config" / "copy_trading.yaml").write_text(
        "copy_trading:\n  enabled: true\n  magic_number: 52000\n  poll_interval_sec: 5\n  stale_after_sec: 120\n  followers: []\n",
        encoding="utf-8")
    (home / ".env").write_text("MT5_LOGIN=1\n", encoding="utf-8")
    term = home / "mt5-copy1" / "terminal64.exe"; term.parent.mkdir(); term.write_bytes(b"x")
    register_follower(home, "Démo 10k", "1", "p", "s", str(term), 1.0)
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "copy_status_COPY1.json").write_text(json.dumps({
        "name": "Démo 10k", "ts_utc": T0.isoformat(), "size_factor": 1.0, "equity": 10000.0, "balance": 10000.0,
        "positions": [], "actions": [],
        "closed": [_t("EURUSD", "BUY", 60, 0, 30), _t("EURUSD", "BUY", 20, 35, 40), _t("GBPUSD", "SELL", 20, 0, 5)]}),
        encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_AUTH_USER", "admin")
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    srv = create_server(home=home, host="127.0.0.1", port=0, auth_token="tk")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        st = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/stats", headers={"Cookie": "tladash=tk"}), timeout=5).read())
        assert st["consistency"]["share_percent"] == 90.0 and not st["consistency"]["within_limit"]
        assert st["consistency"]["ideas"][0]["symbol"] == "EURUSD"
        acc = [a for a in st["accounts"] if a["env_prefix"] == "COPY1"][0]
        fs = acc["stats"]
        assert fs["total"]["trades"] == 3 and fs["total"]["pnl"] == 100.0
        assert fs["consistency"]["share_percent"] == 80.0, "EURUSD BUY ×2 (réouvert 5 min après) = une idée de 80 $"
        assert fs["consistency"]["n_ideas"] == 2 and fs["daily"][-1]["n"] == 3
        page = urllib.request.urlopen(urllib.request.Request(base + "/stats", headers={"Cookie": "tladash=tk"}), timeout=5).read().decode()
        assert "Cohérence 25 %" in page and "drawCons" in page and 'id="fcons"' in page
    finally:
        srv.shutdown(); srv.server_close()
