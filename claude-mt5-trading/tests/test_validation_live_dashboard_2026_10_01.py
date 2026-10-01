"""2026-10-01, demande utilisateur : sur la page Shadow du dashboard, valider soi-même le passage en live des agents
prêts. Verdict recalculé côté serveur, demande passée par la file des statuts (appliquée par l'orchestrateur)."""
from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tradinglab.learning.shadow_board import demander_passage_live, shadow_board
from tradinglab.learning.store import LearningStore, TradeRecord

PROJET = Path(__file__).resolve().parents[1]


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    shutil.copytree(PROJET / "config", home / "config")
    for d in ("data", "state", "reports", "logs"):
        (home / d).mkdir(parents=True, exist_ok=True)
    st = LearningStore(home / "data" / "learning.db")
    for agent, rs, base in (("X90", [2.0, -1.0] * 50, 0), ("X91", [2.0, -1.0] * 25, 1000), ("X92", [2.0, -1.0] * 50, 2000)):
        for i, r in enumerate(rs):
            st.record_trade(TradeRecord(ticket=base + i, agent_id=agent, symbol="EURUSD", side="BUY", entry=1.0, sl=0.99,
                                        risk_money=100, risk_percent=0.1, result_r=r, pnl=100 * r, mode="shadow",
                                        opened_at="2026-10-01T08:00:00+00:00", closed_at="2026-10-01T09:00:00+00:00",
                                        exit_reason="tp"))
    (home / "data" / "agent_status.json").write_text(json.dumps(
        {"status": {"X90": "SHADOW", "X91": "SHADOW", "X92": "LIVE"}}), encoding="utf-8")
    return home


def _cfg(home: Path) -> dict:
    import yaml

    return yaml.safe_load((home / "config" / "strategies.yaml").read_text(encoding="utf-8"))["learning"]


def _demandes(home: Path) -> list[dict]:
    f = home / "state" / "agent_status_requests.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def test_validation_seulement_pour_un_agent_pret(tmp_path):
    home = _home(tmp_path)
    cfg = _cfg(home)
    assert cfg["min_shadow_trades"] == 100
    pas_pret = demander_passage_live(home, cfg, "X91")                 # 50 trades seulement
    assert not pas_pret["ok"] and "pas prêt" in pas_pret["error"]
    deja = demander_passage_live(home, cfg, "X92")
    assert not deja["ok"] and "LIVE" in deja["error"]
    assert not demander_passage_live(home, cfg, "X90; rm")["ok"]
    assert not demander_passage_live(home, cfg, "Z99")["ok"]
    assert _demandes(home) == []
    ok = demander_passage_live(home, cfg, "x90")
    assert ok["ok"] and ok["agent_id"] == "X90"
    d = _demandes(home)
    assert len(d) == 1 and d[0]["agent_id"] == "X90" and d[0]["status"] == "LIVE" and d[0]["source"] == "dashboard"
    assert "100 trades shadow" in d[0]["reason"]
    assert demander_passage_live(home, cfg, "X90")["ok"] and len(_demandes(home)) == 1, "pas de doublon"
    lignes = {a["agent_id"]: a for a in shadow_board(home, cfg)["agents"]}
    assert lignes["X90"]["demande_live"] and not lignes["X91"]["demande_live"]


def test_route_du_dashboard_authentifiee(tmp_path, monkeypatch):
    from tradinglab.dashboards.server import SHADOW_HTML, create_server

    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH_USER", raising=False)
    home = _home(tmp_path)
    srv = create_server(home=home, host="127.0.0.1", port=0, auth_token="bon-jeton")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(corps: dict, jeton: str | None = "bon-jeton"):
        hdr = {"Content-Type": "application/json", **({"Authorization": f"Bearer {jeton}"} if jeton else {})}
        req = urllib.request.Request(base + "/api/shadow/live", data=json.dumps(corps).encode(), headers=hdr, method="POST")
        return urllib.request.urlopen(req, timeout=10)
    try:
        with pytest.raises(HTTPError) as exc:
            post({"agent_id": "X90"}, None)
        assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            post({"agent_id": "X91"})
        assert exc.value.code == 400 and "pas prêt" in json.loads(exc.value.read())["error"]
        out = json.loads(post({"agent_id": "X90"}).read())
        assert out["ok"] and [d["agent_id"] for d in _demandes(home)] == ["X90"]
        journal = "".join(p.read_text(encoding="utf-8") for p in (home / "logs").glob("journal-*.jsonl"))
        assert "agent_live_demande" in journal
    finally:
        srv.shutdown()
        srv.server_close()
    assert "/api/shadow/live" in SHADOW_HTML and "Valider le live" in SHADOW_HTML
