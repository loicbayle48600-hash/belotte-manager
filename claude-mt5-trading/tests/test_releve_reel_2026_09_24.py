"""« Les vrais stats de chaque compte » (demande utilisateur 2026-09-24, capture MT5 à l'appui).

Le dashboard affichait pour le maître +622,90 $ (journal, commission d'entrée manquante) quand l'historique MT5
disait bénéfice 738,40 / commission −161 / net 577,40. Chaque compte a désormais un RELEVÉ tiré de l'historique
MT5 (mêmes rubriques que l'onglet Historique : bénéfice, dépôt, swap, commission, solde) et ses statistiques
viennent de ses trades réels (bot et manuels, commissions et swaps compris).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.learning.consistency import account_statement  # noqa: E402

T = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)


def _d(pid, entry, profit, com, kind="TRADE", minute=0, symbol="AUDSGD"):
    return SimpleNamespace(position_id=pid, entry=entry, profit=profit, commission=com, swap=0.0, kind=kind, symbol=symbol,
                           side="SELL" if entry == "IN" else "BUY", volume=10.0, time=T + timedelta(minutes=minute), magic=51000,
                           comment="")


def deals_capture():
    """Les chiffres de la capture MT5 de l'utilisateur (21:29) : 3 positions fermées."""
    return [_d(0, "", 500000.0, 0.0, kind="BALANCE"),
            _d(1, "IN", 0.0, -35.0), _d(1, "OUT", 695.11, -35.0, minute=5),
            _d(2, "IN", 0.0, -35.0, symbol="GBPCAD"), _d(2, "OUT", 28.29, -35.0, minute=6, symbol="GBPCAD"),
            _d(3, "IN", 0.0, -10.5, symbol="XAGUSD"), _d(3, "OUT", 15.0, -10.5, minute=7, symbol="XAGUSD")]


def test_releve_identique_a_l_historique_mt5():
    st = account_statement(deals_capture(), equity=502655.19)
    assert st["profit"] == 738.40 and st["deposits"] == 500000.0 and st["commission"] == -161.0 and st["swap"] == 0.0
    assert st["net"] == 577.40 and st["balance"] == 500577.40 and st["equity"] == 502655.19
    assert [(t["symbol"], t["pnl"]) for t in st["trades"]] == [("AUDSGD", 625.11), ("GBPCAD", -41.71), ("XAGUSD", -6.0)]


def test_le_dashboard_du_maitre_lit_le_releve(tmp_path):
    import shutil

    from tradinglab.core.state import StateStore
    from tradinglab.dashboards.server import DashboardData

    shutil.copytree(ROOT / "config", tmp_path / "config")
    store = StateStore(tmp_path / "state")
    store.state.account_login = 53068680
    store.save()
    st = account_statement(deals_capture(), equity=502655.19)
    (tmp_path / "state" / "statement_master.json").write_text(json.dumps({"login": 53068680, **st}), encoding="utf-8")
    (tmp_path / "logs").mkdir()
    stats = DashboardData(tmp_path).stats()
    assert stats["total"]["trades"] == 3 and stats["total"]["pnl"] == 577.40 and stats["total"]["wins"] == 1
    assert stats["statement"]["commission"] == -161.0 and stats["statement"]["balance"] == 500577.40
    assert {a["agent"] for a in stats["agents"]} == {"manuel"}, "sans revue post-trade : attribué « manuel »"
    # relevé d'un AUTRE compte (ancien maître) : ignoré
    (tmp_path / "state" / "statement_master.json").write_text(json.dumps({"login": 1, **st}), encoding="utf-8")
    assert DashboardData(tmp_path)._master_statement() is None


def test_le_copieur_exporte_le_releve_du_suiveur(tmp_path):
    from tradinglab.copy.copier import CopyTrader
    from tradinglab.mt5.mock_adapter import MockBroker

    from test_copy_trading import master, mpos

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "m.json"
    status = tmp_path / "copy_status_COPYX.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())), encoding="utf-8")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, status_file=status, stale_after_sec=10_000)
    tr.sync(now=b.now())
    releve = json.loads(status.read_text(encoding="utf-8"))["statement"]
    assert set(releve) >= {"profit", "deposits", "swap", "commission", "net", "balance", "equity"} and "trades" not in releve


def test_remise_a_zero_des_statistiques_depuis_une_date():
    """2026-09-25 : comptes démo remis à 500 000 $ — les statistiques repartent d'une date ; le solde est celui du broker."""
    st = account_statement(deals_capture(), equity=500000.0, since=(T + timedelta(minutes=6, seconds=30)).isoformat(), balance=500000.0)
    assert st["balance"] == 500000.0 and st["deposits"] == 0.0 and st["since"]
    assert [t["symbol"] for t in st["trades"]] == ["XAGUSD"], "seul le trade fermé après la date compte"
