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
    # sans la date de début du compte maître réel (elle filtrerait les trades fictifs des tests, 2026-09-24)
    sysf = tmp_path / "config" / "system.yaml"
    sysf.write_text("".join(l for l in sysf.read_text(encoding="utf-8").splitlines(keepends=True)
                            if not l.lstrip().startswith("master_account_since:")), encoding="utf-8")
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


def test_api_state(server, settings):
    status, ctype, body = _get(server, "/api/state")
    assert status == 200 and ctype.startswith("application/json")
    data = json.loads(body)
    assert data["mode"] == "SAFE_MODE"
    assert data["equity"] == 10100.0
    assert data["account_trade_mode"] == "DEMO"
    assert data["daily_pnl"] == 100.0
    assert data["prop"]["prop_firm"] == "FOXX_FUNDED"
    assert data["prop"]["autonomous_prop"] is True     # drapeau + règles vérifiées + autorisation utilisateur (2026-09-21)
    # valeur lue dans config/risk.yaml : figer un nombre ici casserait le test à chaque
    # ajustement du budget de risque, sans qu'aucune règle ne soit violée.
    assert data["risk"]["max_open_positions"] == int(settings.risk["max_open_positions"])
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
    assert "Tableau de bord" in body          # « Dashboard (lecture seule) » renommé le 2026-09-23
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
    # ``learning`` (agent_stats.json, volumineux) n'est envoyé qu'en repli, quand le classement est vide (2026-09-23)
    assert "learning" not in data
    assert data["leaderboard"][0]["agent_id"] == "agent_a"
    (home / "reports" / "leaderboard.json").write_text("[]", encoding="utf-8")
    from tradinglab.dashboards.server import DashboardData
    snap = DashboardData(home).snapshot()
    assert snap["leaderboard"] == [] and snap["learning"] == {"agent_a": {"trades": 3}}


def test_auth_token_exige_et_verifie(tmp_path):
    """DASHBOARD_AUTH_TOKEN défini (2026-09-22, accès extérieur via VPN) : 401 sans jeton, 200 avec
    (Basic mot-de-passe=jeton ou Bearer). Sans jeton configuré : comportement historique, tout passe."""
    import base64
    import urllib.request
    from urllib.error import HTTPError

    from tradinglab.dashboards.server import create_server
    import threading

    srv = create_server(home=tmp_path, host="127.0.0.1", port=0, auth_token="secret-test-123")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/health"
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError("l'accès sans jeton devrait être refusé")
        except HTTPError as e:
            assert e.code == 401 and "Basic" in e.headers.get("WWW-Authenticate", "")
        req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"loic:secret-test-123").decode()})
        assert urllib.request.urlopen(req, timeout=5).status == 200
        req = urllib.request.Request(url, headers={"Authorization": "Bearer secret-test-123"})
        assert urllib.request.urlopen(req, timeout=5).status == 200
        req = urllib.request.Request(url, headers={"Authorization": "Bearer mauvais"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("mauvais jeton accepté")
        except HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()
    # sans jeton configuré : pas d'authentification (usage local historique)
    srv2 = create_server(home=tmp_path, host="127.0.0.1", port=0, auth_token="")
    t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
    t2.start()
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{srv2.server_address[1]}/health", timeout=5).status == 200
    finally:
        srv2.shutdown()


def test_auth_username_et_tls(tmp_path, monkeypatch):
    """User `admin` exigé en Basic quand DASHBOARD_AUTH_USER est défini ; DASHBOARD_TLS=1 sert en HTTPS
    (certificat auto-signé généré dans state/ et réutilisé)."""
    import base64
    import ssl
    import threading
    import urllib.request
    from urllib.error import HTTPError

    from tradinglab.dashboards.server import create_server

    monkeypatch.setenv("DASHBOARD_AUTH_USER", "admin")
    monkeypatch.setenv("DASHBOARD_TLS", "1")
    srv = create_server(home=tmp_path, host="127.0.0.1", port=0, auth_token="s3cret")
    assert getattr(srv, "tls_enabled", False)
    assert (tmp_path / "state" / "dashboard_cert.pem").exists()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # auto-signé : le test vérifie le chiffrement, pas la chaîne
    url = f"https://127.0.0.1:{srv.server_address[1]}/health"
    try:
        good = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()})
        assert urllib.request.urlopen(good, timeout=5, context=ctx).status == 200
        bad_user = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"loic:s3cret").decode()})
        try:
            urllib.request.urlopen(bad_user, timeout=5, context=ctx)
            raise AssertionError("mauvais utilisateur accepté")
        except HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()
    # le certificat est réutilisé au démarrage suivant (pas de régénération)
    before = (tmp_path / "state" / "dashboard_cert.pem").read_bytes()
    srv2 = create_server(home=tmp_path, host="127.0.0.1", port=0, auth_token="s3cret")
    srv2.server_close()
    assert (tmp_path / "state" / "dashboard_cert.pem").read_bytes() == before


def test_page_de_connexion_et_cookie(tmp_path, monkeypatch):
    """Chrome n'affiche pas toujours la popup Basic (constaté le 2026-09-22) : GET / non authentifié sert un
    formulaire, POST /login pose le cookie de session, le cookie donne accès aux routes API. Les API nues
    gardent leur 401, les identifiants faux sont refusés, et POST ailleurs reste interdit (405)."""
    import threading
    import urllib.request
    from urllib.error import HTTPError
    from urllib.parse import urlencode

    from tradinglab.dashboards.server import create_server

    monkeypatch.setenv("DASHBOARD_AUTH_USER", "admin")
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    srv = create_server(home=tmp_path, host="127.0.0.1", port=0, auth_token="Loic-test")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        body = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "Se connecter" in body                                    # formulaire, pas un 401 sec
        try:
            urllib.request.urlopen(base + "/api/state", timeout=5)
            raise AssertionError("API sans jeton acceptée")
        except HTTPError as e:
            assert e.code == 401
        req = urllib.request.Request(base + "/login", data=urlencode({"user": "admin", "password": "Loic-test"}).encode(), method="POST")
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        resp = opener.open(req, timeout=5)
        cookie = None
        for h, v in resp.headers.items():
            pass
        # urllib suit la redirection : récupérer le cookie via une requête sans redirection
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener2 = urllib.request.build_opener(NoRedirect)
        try:
            opener2.open(urllib.request.Request(base + "/login", data=urlencode({"user": "admin", "password": "Loic-test"}).encode(), method="POST"), timeout=5)
        except HTTPError as e:
            assert e.code == 303
            cookie = e.headers.get("Set-Cookie", "")
        assert cookie and cookie.startswith("tladash=Loic-test") and "HttpOnly" in cookie
        req = urllib.request.Request(base + "/health", headers={"Cookie": "tladash=Loic-test"})
        assert urllib.request.urlopen(req, timeout=5).status == 200
        # mauvais mot de passe / mauvais cookie → refus
        try:
            opener2.open(urllib.request.Request(base + "/login", data=urlencode({"user": "admin", "password": "faux"}).encode(), method="POST"), timeout=5)
            raise AssertionError("mauvais mot de passe accepté")
        except HTTPError as e:
            assert e.code == 401
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/health", headers={"Cookie": "tladash=faux"}), timeout=5)
            raise AssertionError("mauvais cookie accepté")
        except HTTPError as e:
            assert e.code == 401
        # POST ailleurs : toujours lecture seule
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/state", data=b"x", method="POST"), timeout=5)
            raise AssertionError("POST hors /login accepté")
        except HTTPError as e:
            assert e.code == 405
    finally:
        srv.shutdown()


def test_panneau_de_controle_et_stats(home: Path, monkeypatch):
    """Panneau de contrôle (2026-09-22) : /control et /stats servis authentifiés, POST /api/command whiteliste
    les commandes d'exploitation et les pousse dans la file officielle ; sans cookie → 401 ; commande hors
    liste → 400 ; /api/stats agrège les revues post-trade par jour/semaine/mois/année."""
    import threading
    import urllib.request
    from urllib.error import HTTPError

    from tradinglab.dashboards.server import create_server

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(home / "logs" / f"journal-{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": f"{day}T10:00:00+00:00", "kind": "post_trade_review", "agent_id": "B01",
                            "result_r": 2.0, "pnl": 1200.0}) + "\n")
        f.write(json.dumps({"ts_utc": f"{day}T11:00:00+00:00", "kind": "post_trade_review", "agent_id": "C06",
                            "result_r": -1.0, "pnl": -600.0}) + "\n")
    monkeypatch.setenv("DASHBOARD_AUTH_USER", "admin")
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    srv = create_server(home=home, host="127.0.0.1", port=0, auth_token="tk")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    ck = {"Cookie": "tladash=tk"}
    try:
        # pages authentifiées ; sans cookie → formulaire de connexion
        for path, needle in (("/control", "Panneau de contrôle"), ("/stats", "Statistiques")):
            body = urllib.request.urlopen(urllib.request.Request(base + path, headers=ck), timeout=5).read().decode()
            assert needle in body, path
            body = urllib.request.urlopen(base + path, timeout=5).read().decode()
            assert "Se connecter" in body, path
        # stats agrégées
        st = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/stats", headers=ck), timeout=5).read())
        assert st["total"]["trades"] == 2 and st["total"]["pnl"] == 600.0 and st["total"]["win_rate"] == 50.0
        assert st["daily"][-1]["key"] == day and st["daily"][-1]["n"] == 2
        assert st["weekly"] and st["monthly"] and st["yearly"] and len(st["agents"]) == 2
        # commande d'exploitation : whitelist + file officielle
        def post(cmd, headers):
            req = urllib.request.Request(base + "/api/command", data=json.dumps({"command": cmd}).encode(),
                                         headers={"Content-Type": "application/json", **headers}, method="POST")
            return urllib.request.urlopen(req, timeout=5)
        out = json.loads(post("PAUSE", ck).read())
        assert out["ok"] and out["queued"]["command"] == "PAUSE" and out["queued"]["source"] == "dashboard"
        lines = (home / "state" / "commands.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert json.loads(lines[-1])["command"] == "PAUSE"
        try:
            post("PAUSE", {})
            raise AssertionError("commande sans authentification acceptée")
        except HTTPError as e:
            assert e.code == 401
        try:
            post("SIMULATE_WITHDRAWAL", ck)
            raise AssertionError("commande hors whitelist acceptée")
        except HTTPError as e:
            assert e.code == 400
    finally:
        srv.shutdown()


def test_api_restart_lance_le_redemarrage_dans_le_mode_courant(home: Path, monkeypatch):
    """Bouton « Redémarrer » (2026-09-22) : authentifié, lance stop/start via le lanceur détaché dans le
    mode courant (SAFE_MODE dans cet état → SAFE ; AUTO reste AUTO). Sans session → 401."""
    import threading
    import urllib.request
    from urllib.error import HTTPError

    from tradinglab.dashboards import server as srvmod

    calls = []
    monkeypatch.setattr(srvmod, "RESTART_LAUNCHER", lambda h, mode: calls.append((h, mode)))
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH_USER", raising=False)
    srv = srvmod.create_server(home=home, host="127.0.0.1", port=0, auth_token="tk")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        js = {"Content-Type": "application/json"}
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/restart", data=b"{}", method="POST", headers=js), timeout=5)
            raise AssertionError("redémarrage sans session accepté")
        except HTTPError as e:
            assert e.code == 401
        # POST /api/* sans Content-Type: application/json → 415 (2026-09-23)
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/restart", data=b"{}", method="POST",
                                                          headers={"Cookie": "tladash=tk"}), timeout=5)
            raise AssertionError("POST sans Content-Type JSON accepté")
        except HTTPError as e:
            assert e.code == 415
        d = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/restart", data=b"{}", method="POST",
                                                                     headers={"Cookie": "tladash=tk", **js}), timeout=5).read())
        assert d["ok"] and d["mode"] == "SAFE" and calls == [(home, "SAFE")]
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------
# Page d'accueil refondue (2026-09-23) : bandeau, jauges, journal humain, cartes de positions
# ----------------------------------------------------------------------------
def _plan(ticket: int, symbol: str, side: str, risk_money: float, **kw):
    from tradinglab.core.state import BotPositionPlan

    return BotPositionPlan(ticket=ticket, symbol=symbol, side=side, agent_id="B08", candidate_id=f"c{ticket}",
                           entry=1.0, initial_sl=0.99, initial_volume=1.0, initial_risk_money=risk_money,
                           risk_percent=0.1, **kw)


def test_snapshot_expose_cycle_paiement_correlation_et_risque_par_classe(home: Path, settings):
    """/api/state porte le statut du cycle FOXX calculé par le PropGuard (UNE seule définition de la
    cohérence, celle appliquée par l'orchestrateur), les plafonds de corrélation (sans secret) et le risque
    ouvert par classe d'actifs (risque initial des positions du bot en % de l'equity)."""
    store = StateStore(home / "state")
    st = store.reload()
    st.initial_balance = 10000.0
    st.bot_positions = {
        "1": _plan(1, "EURUSD", "BUY", 50.0),
        "2": _plan(2, "GBPUSD", "SELL", 30.0, tp1_done=True, break_even_done=True),
        "3": _plan(3, "XAUUSD", "SELL", 20.0),
    }
    store.save(st)
    srv = create_server(home, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        _, _, body = _get(srv, "/api/state")
    finally:
        srv.shutdown()
        srv.server_close()
    data = json.loads(body)
    rbc = data["open_risk_by_class"]
    assert rbc["forex"] == {"risk_money": 80.0, "risk_percent": round(100.0 * 80.0 / 10100.0, 4), "positions": 2}
    assert rbc["metals"] == {"risk_money": 20.0, "risk_percent": round(100.0 * 20.0 / 10100.0, 4), "positions": 1}
    pc = data["payout_cycle"]
    assert isinstance(pc, dict)
    for key in ("trading_days_done", "trading_days_required", "consistency_share_percent", "consistency_ok",
                "cycle_profit", "eligible", "ready", "blocking_reasons", "open_positions"):
        assert key in pc, key
    assert pc["consistency_ok"] is True and pc["consistency_share_percent"] == 0.0   # aucun gain réalisé
    assert pc["open_positions"] == 3 and pc["ready"] is False
    corr = data["correlation"]
    assert corr["max_asset_class_risk_percent"] == float(settings.correlation["max_asset_class_risk_percent"])
    assert not any("token" in k or "secret" in k for k in corr)


def test_snapshot_sans_position_ni_equity_ne_invente_rien(home: Path):
    """Sans position : dictionnaire vide (pas de classe à 0 inventée) ; equity inconnue (0) → pourcentage None."""
    from tradinglab.dashboards.server import DashboardData

    dd = DashboardData(home)
    assert dd.snapshot()["open_risk_by_class"] == {}
    st = dd.store.reload()
    st.equity = 0.0
    st.bot_positions = {"1": _plan(1, "EURUSD", "BUY", 50.0)}
    dd.store.save(st)
    rbc = dd.snapshot()["open_risk_by_class"]
    assert rbc["forex"]["risk_money"] == 50.0 and rbc["forex"]["risk_percent"] is None


def test_page_accueil_bandeau_jauges_journal_et_positions(server):
    """La page d'accueil contient le bandeau « État en un coup d'œil », les jauges de risque, le journal
    humain filtré (avec bouton « tout voir »), les cartes de positions et les messages d'états vides en
    français ; aucune bibliothèque externe ; 401 sur /api/state → redirection vers /login."""
    _, _, body = _get(server, "/")
    # bandeau sticky : 4 tuiles
    assert "État en un coup d'œil" in body
    for tile in ("tile_bot", "tile_today", "tile_pos", "tile_cycle"):
        assert f'id="{tile}"' in body
    assert "orchestrateur silencieux" in body and "mode prudence (SAFE_MODE)" in body and "arrêt d'urgence" in body
    # jauges de risque + risque par classe
    assert 'id="gauges"' in body and "function gauge(" in body and 'id="risk_by_class"' in body
    assert "Perte jour" in body and "Perte totale" in body and "Idée la plus risquée" in body
    assert "ratio >= 0.75 ? 'bad'" in body                     # rouge dès 75 % de la limite
    # une seule définition de la cohérence : celle du PropGuard
    assert "pc.consistency_share_percent" in body and "s.consistency_share_percent" not in body
    assert "du profit du cycle (limite" in body
    # journal humain filtré par types + bouton « tout voir »
    assert "/api/state?kinds=" in body and "position_opened,post_trade_review,partial_tp,early_exit,sl_modified" in body
    assert "consistency_cap_close,copy_trade" in body and 'id="journal_toggle"' in body and "tout voir" in body
    assert "e.kind === 'copy_trade' && e.ok !== false" in body
    # cartes de positions (une par position) et flottant = equity − balance
    assert "poscard" in body and "ouverte depuis" in body and "s.equity - s.balance" in body
    assert "invalidation" in body and "stop remonté" in body
    # leaderboard : top 5, ≥ 3 trades, observation < 40, lien classement complet
    assert "en observation (&lt; 40 trades)" in body and "n >= 3" in body and "slice(0, 5)" in body
    assert 'href="/stats"' in body and "classement complet" in body
    # simplifications
    assert "symbole" in body and "dernier cycle" in body and "Modèles : " in body and "<summary>Règles</summary>" in body
    assert 'id="regimes"' in body and 'id="agents"' in body
    # états vides et libellés en français
    for msg in ("aucune position ouverte", "aucun setup ce cycle", "aucun événement de trading aujourd'hui",
                "pas encore de trade fermé", "Taux de réussite", "Solde", "Battement orchestrateur",
                "Battement watchdog", "Nouvelles entrées : autorisées", "Nouvelles entrées : bloquées",
                "vérifiées le", "Tableau de bord"):
        assert msg in body, msg
    assert "Dashboard (lecture seule)" not in body
    # 401 → /login ; format monétaire fr-FR arrondi à l'unité ; aucune ressource externe
    assert "r.status === 401" in body and "'/login'" in body
    assert "Math.round(v).toLocaleString('fr-FR')" in body
    assert "http://" not in body.split("<body>")[0] and "cdn." not in body and "<script src=" not in body
    # les anciennes lignes remplacées par les jauges ont disparu
    for old in ("Drawdown jour (interne)", "Drawdown global (interne)", "Perte jour (base prop)", "Perte totale (base prop)"):
        assert old not in body, old



# ----------------------------------------------------------------------------
# Seconde passe (2026-09-23) : robustesse, performance, sécurité, pages /stats et /control
# ----------------------------------------------------------------------------
def _srv(home: Path, token: str | None = None):
    from tradinglab.dashboards.server import create_server

    srv = create_server(home=home, host="127.0.0.1", port=0, auth_token=token)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _open(url: str, headers: dict | None = None, data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    return urllib.request.urlopen(req, timeout=5)


def test_journal_tail_plafonne_la_lecture_arriere_pour_un_type_rare(tmp_path: Path):
    """Constat 2026-09-23 : ``?kinds=order`` relisait 73 Mo en 9 s à chaque appel. La lecture arrière est
    plafonnée (doublements / octets) et renvoie ce qui a été trouvé ; sans plafond, l'événement rare est trouvé."""
    from tradinglab.dashboards.server import JOURNAL_TAIL_FILTERED, read_journal_tail

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logs = tmp_path / "logs"
    logs.mkdir()
    with open(logs / f"journal-{day}.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "kind": "rare", "message": "unique"}) + "\n")
        for i in range(2000):                       # ≈ 200 Ko de bruit après l'événement rare
            f.write(json.dumps({"ts_utc": "2026-01-01T00:00:01+00:00", "kind": "candidate", "payload": "x" * 90, "i": i}) + "\n")
    capped = read_journal_tail(logs, JOURNAL_TAIL_FILTERED, kinds={"rare"}, window=1024, max_bytes=8_000_000, max_doublings=4)
    assert capped == []                             # ≤ 16 Ko relus : rien trouvé, aucune relecture complète
    small_cap = read_journal_tail(logs, JOURNAL_TAIL_FILTERED, kinds={"rare"}, window=1024, max_bytes=4096, max_doublings=50)
    assert small_cap == []                          # plafond en octets atteint
    full = read_journal_tail(logs, JOURNAL_TAIL_FILTERED, kinds={"rare"}, window=1024, max_bytes=8_000_000, max_doublings=None)
    assert [e["message"] for e in full] == ["unique"]
    # en production : fenêtre 512 Ko, plafond 8 Mo = 4 doublements ; un journal de 73 Mo n'est jamais relu en entier
    assert read_journal_tail(logs, JOURNAL_TAIL_FILTERED, kinds={"rare"}) == full
    assert len(read_journal_tail(logs, JOURNAL_TAIL_FILTERED, kinds={"candidate"}, window=1024)) == JOURNAL_TAIL_FILTERED
    assert len(read_journal_tail(logs, 5, window=1024)) == 5


def test_api_journal_limit_offset_kinds(server):
    """/api/journal : ``limit`` (défaut 500, plafond 5 000), ``offset``, ``kinds`` ; total dans X-Total-Count."""
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    r = _open(base + "/api/journal?limit=1")
    assert json.loads(r.read()) == [{"ts_utc": "2026-01-01T00:00:00+00:00", "kind": "info", "message": "hello"}]
    assert r.headers.get("X-Total-Count") == "2"
    assert [e["kind"] for e in json.loads(_open(base + "/api/journal?limit=1&offset=1").read())] == ["order"]
    assert [e["kind"] for e in json.loads(_open(base + "/api/journal?kinds=order").read())] == ["order"]
    assert json.loads(_open(base + "/api/journal?offset=50").read()) == []
    assert len(json.loads(_open(base + "/api/journal?limit=999999").read())) == 2      # plafonné, pas d'erreur
    with pytest.raises(urllib.error.HTTPError) as exc:
        _open(base + "/api/journal?limit=abc")
    assert exc.value.code == 400
    from tradinglab.dashboards.server import JOURNAL_API_MAX_LIMIT, DashboardData
    dd = DashboardData(server.data.home)
    events, total = dd.journal(None, None, 10 ** 9, 0)
    assert total == 2 and len(events) == 2 and JOURNAL_API_MAX_LIMIT == 5_000


def test_gzip_si_accepte_et_en_tetes_de_securite(server):
    """gzip seulement si ``Accept-Encoding`` le permet et si le corps est assez grand ; CSP / X-Frame-Options /
    Referrer-Policy sur toutes les réponses ; aucune version de serveur ni de Python divulguée."""
    import gzip

    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    r = _open(base + "/api/state", {"Accept-Encoding": "gzip, deflate"})
    assert r.headers.get("Content-Encoding") == "gzip" and "Accept-Encoding" in r.headers.get("Vary", "")
    data = json.loads(gzip.decompress(r.read()))
    assert data["mode"] == "SAFE_MODE"
    r2 = _open(base + "/api/state")
    assert r2.headers.get("Content-Encoding") is None and json.loads(r2.read())["mode"] == "SAFE_MODE"
    r3 = _open(base + "/health", {"Accept-Encoding": "gzip"})
    assert r3.headers.get("Content-Encoding") is None                      # petit corps : pas compressé
    for resp in (r2, r3):
        assert resp.headers.get("Content-Security-Policy", "").startswith("default-src 'self'")
        assert "connect-src 'self'" in resp.headers["Content-Security-Policy"]
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Strict-Transport-Security") is None       # HTTP simple : pas de HSTS
        srvh = resp.headers.get("Server", "")
        assert "Python" not in srvh and "/" not in srvh and srvh.strip() == "TradingLabDashboard"


def test_limitation_des_tentatives_puis_logout(tmp_path, monkeypatch):
    """5 échecs d'authentification par IP → 429 pendant 60 s (même avec le bon jeton) ; le compteur expire ;
    GET /logout efface le cookie et renvoie vers /login."""
    from urllib.error import HTTPError

    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH_USER", raising=False)
    srv, base = _srv(tmp_path, "bon-jeton")
    try:
        for _ in range(5):
            with pytest.raises(HTTPError) as exc:
                _open(base + "/health", {"Authorization": "Bearer faux"})
            assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            _open(base + "/health", {"Authorization": "Bearer bon-jeton"})
        assert exc.value.code == 429 and int(exc.value.headers.get("Retry-After", "0")) >= 1
        assert "trop de tentatives" in json.loads(exc.value.read())["error"]
        with pytest.raises(HTTPError) as exc:                       # POST bloqué aussi
            _open(base + "/api/command", {"Content-Type": "application/json", "Authorization": "Bearer bon-jeton"},
                  json.dumps({"command": "PAUSE"}).encode(), "POST")
        assert exc.value.code == 429
        # blocage expiré → accès rétabli et compteur remis à zéro
        srv._auth_failures["127.0.0.1"][1] = 0.0
        assert _open(base + "/health", {"Authorization": "Bearer bon-jeton"}).status == 200
        assert "127.0.0.1" not in srv._auth_failures
        # sans preuve présentée, ce n'est pas une tentative : jamais compté
        for _ in range(7):
            with pytest.raises(HTTPError):
                _open(base + "/health")
        assert _open(base + "/health", {"Authorization": "Bearer bon-jeton"}).status == 200
        # /logout : cookie effacé, redirection /login, accessible sans session
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        with pytest.raises(HTTPError) as exc:
            urllib.request.build_opener(NoRedirect).open(base + "/logout", timeout=5)
        assert exc.value.code == 303 and exc.value.headers.get("Location") == "/login"
        assert "tladash=;" in exc.value.headers.get("Set-Cookie", "") and "Max-Age=0" in exc.value.headers["Set-Cookie"]
    finally:
        srv.shutdown(); srv.server_close()


def test_health_et_post_api_sans_json(server):
    """/health dit la vérité (le panneau écrit dans la file de commandes : read_only False) ; POST /api/* sans
    Content-Type JSON → 415 ; POST hors routes connues → 405."""
    from urllib.error import HTTPError

    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    h = json.loads(_open(base + "/health").read())
    assert h == {"ok": True, "read_only": False, "auth": False, "tls": False, "server_time_utc": h["server_time_utc"]}
    with pytest.raises(HTTPError) as exc:
        _open(base + "/api/command", {"Content-Type": "text/plain"}, b'{"command":"PAUSE"}', "POST")
    assert exc.value.code == 415
    with pytest.raises(HTTPError) as exc:
        _open(base + "/api/state", {"Content-Type": "application/json"}, b"{}", "POST")
    assert exc.value.code == 405


def test_snapshot_projette_le_journal_et_les_idees_ouvertes(home: Path):
    """journal_tail : projection (champs lus par humanEvent + ``summary`` ≤ 240) ; ``trade_ideas`` retiré au profit
    de ``trade_ideas_open`` ; ``bot_positions_count`` ; le store est en lecture seule (aucun fichier mis de côté)."""
    from tradinglab.dashboards.server import DashboardData, project_event

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    big = {"ts_utc": f"{day}T10:00:00+00:00", "ts_local": f"{day}T12:00:00+02:00", "component": "orchestrator", "level": "INFO",
           "kind": "position_opened", "ticket": 7, "symbol": "EURUSD", "side": "BUY", "agent_id": "B02", "entry": 1.1,
           "trade_idea_risk_money": 50.0, "candidate": {"reasoning": "x" * 5000, "tp_plan": [1, 2, 3]}}
    review = {"ts_utc": f"{day}T11:00:00+00:00", "kind": "post_trade_review", "ticket": 7, "pnl": 12.5, "result_r": 0.5,
              "review": {"pnl": 12.5, "result_r": 0.5, "exit_reason": "tp", "verdict": "VARIANCE_NORMALE", "long_text": "y" * 3000}}
    with open(home / "logs" / f"journal-{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(big) + "\n" + json.dumps(review) + "\n")
    dd = DashboardData(home)
    assert dd.store.readonly is True
    st = dd.store.reload()
    st.register_trade_idea("EURUSD", "BUY", 50.0, ticket=7)
    st.register_trade_idea("GBPUSD", "SELL", 30.0)                 # aucun ticket ouvert : pas dans la projection
    st.bot_positions = {"7": _plan(7, "EURUSD", "BUY", 50.0)}
    StateStore(home / "state").save(st)
    snap = dd.snapshot({"position_opened", "post_trade_review"})
    assert "trade_ideas" not in snap and snap["bot_positions_count"] == 1
    assert snap["trade_ideas_open"] == [{"symbol": "EURUSD", "side": "BUY", "risk_money": 50.0}]
    tail = snap["journal_tail"]
    assert [e["kind"] for e in tail] == ["position_opened", "post_trade_review"]
    po, rv = tail
    assert "candidate" not in po and po["symbol"] == "EURUSD" and po["trade_idea_risk_money"] == 50.0 and po["entry"] == 1.1
    assert len(po["summary"]) <= 241 and po["ts_local"].endswith("+02:00") and po["component"] == "orchestrator"
    assert rv["review"] == {"pnl": 12.5, "result_r": 0.5, "exit_reason": "tp", "verdict": "VARIANCE_NORMALE"}
    assert len(json.dumps(tail)) < 1500
    assert project_event({"kind": "x", "message": "m" * 300})["summary"].endswith("…")
    # fichier d'état illisible pendant l'écriture atomique : dernier état connu conservé, rien renommé
    (home / "state" / "system_state.json").write_text("{tronqué", encoding="utf-8")
    assert dd.snapshot()["equity"] == 10100.0
    assert not (home / "state" / "system_state.corrupt.json").exists()


def test_stats_courbe_equity_planchers_classes_et_cache(home: Path, monkeypatch):
    """/api/stats : ``equity_curve`` (solde initial + cumul learning.db), ``drawdown_curve`` (% depuis le solde
    initial), ``floors`` (état + limites dures), ``by_asset_class`` ; les journaux inchangés ne sont pas relus."""
    import sqlite3

    from tradinglab.dashboards import server as srvmod

    st = StateStore(home / "state").reload()
    st.initial_balance = 10000.0
    st.daily.reference_equity = 10050.0
    StateStore(home / "state").save(st)
    (home / "data").mkdir(exist_ok=True)
    con = sqlite3.connect(home / "data" / "learning.db")
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mode TEXT, symbol TEXT, side TEXT, pnl REAL, result_r REAL, "
                "exit_reason TEXT, agent_id TEXT, opened_at TEXT, closed_at TEXT)")
    rows = [("EURUSD", "BUY", 100.0, 1.0, "2026-09-20T10:00:00+00:00", "2026-09-20T11:00:00+00:00"),
            ("XAUUSD", "SELL", -50.0, -1.0, "2026-09-21T10:00:00+00:00", "2026-09-21T11:00:00+00:00"),
            ("US30", "BUY", 200.0, 2.0, "2026-09-19T10:00:00+00:00", "2026-09-22T11:00:00+00:00")]
    for sym, side, pnl, r, o, c in rows:
        con.execute("INSERT INTO trades (mode, symbol, side, pnl, result_r, exit_reason, agent_id, opened_at, closed_at) "
                    "VALUES ('live',?,?,?,?,'sl','B01',?,?)", (sym, side, pnl, r, o, c))
    con.execute("INSERT INTO trades (mode, symbol, side, pnl, result_r, opened_at, closed_at) VALUES ('shadow','EURUSD','BUY',9999,1,'x','y')")
    con.commit(); con.close()
    # journal clos avec une revue, journal du jour avec une autre (même ticket : symbole résolu à travers les jours)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (home / "logs" / "journal-2026-01-05.jsonl").write_text(
        json.dumps({"ts_utc": "2026-01-05T10:00:00+00:00", "kind": "position_opened", "ticket": 1, "symbol": "EURUSD"}) + "\n"
        + json.dumps({"ts_utc": "2026-01-05T11:00:00+00:00", "kind": "post_trade_review", "ticket": 1, "agent_id": "B01", "result_r": 1.0, "pnl": 100.0}) + "\n",
        encoding="utf-8")
    with open(home / "logs" / f"journal-{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": f"{day}T10:00:00+00:00", "kind": "post_trade_review", "ticket": 1, "agent_id": "B01", "result_r": -1.0, "pnl": -20.0}) + "\n")
    dd = srvmod.DashboardData(home)
    calls: list[str] = []
    orig = srvmod.DashboardData._scan_journal_file
    monkeypatch.setattr(srvmod.DashboardData, "_scan_journal_file", staticmethod(lambda f: (calls.append(f.name), orig(f))[1]))
    s = dd.stats()
    assert s["total"]["trades"] == 2 and s["total"]["pnl"] == 80.0
    assert s["symbols"][0]["symbol"] == "EURUSD" and s["symbols"][0]["n"] == 2
    assert sorted(calls) == ["journal-2026-01-05.jsonl", f"journal-{day}.jsonl"]
    curve = s["equity_curve"]
    assert [p["equity"] for p in curve] == [10000.0, 10100.0, 10050.0, 10250.0]
    assert curve[0]["symbol"] is None and curve[-1]["symbol"] == "US30" and curve[-1]["pnl"] == 200.0
    assert curve[-1]["ts"] == "2026-09-22T11:00:00+00:00"                               # trié par clôture
    assert [p["percent"] for p in s["drawdown_curve"]] == [0.0, 1.0, 0.5, 2.5]
    assert s["floors"] == {"daily_floor": round(10050.0 - 10000.0 * 0.04, 2), "overall_floor": round(10000.0 * 0.92, 2),
                           "reference_equity": 10050.0, "initial_balance": 10000.0}
    assert s["limits"]["max_overall_loss_hard_percent"] == 8.0 and s["initial_balance"] == 10000.0
    bac = {r["asset_class"]: r for r in s["by_asset_class"]}
    assert bac["indices"] == {"asset_class": "indices", "trades": 1, "wins": 1, "pnl": 200.0, "r": 2.0, "win_rate": 100.0}
    assert bac["forex"]["pnl"] == 100.0 and bac["metals"]["win_rate"] == 0.0 and "other" not in bac
    assert s["trading_day"] and s["payouts"] == [] and s["currency"] == "EUR"
    # cache : journaux inchangés → aucun nouveau balayage ; journal du jour modifié → seul lui est relu
    dd._stats_cache = None
    calls.clear()
    dd.stats()
    assert calls == []
    dd._stats_cache = None
    with open(home / "logs" / f"journal-{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": f"{day}T12:00:00+00:00", "kind": "post_trade_review", "ticket": 1, "agent_id": "B01", "result_r": 2.0, "pnl": 40.0}) + "\n")
    s2 = dd.stats()
    assert calls == [f"journal-{day}.jsonl"] and s2["total"]["trades"] == 3
    # courbe vide sans solde initial ni trade : rien d'inventé ; échantillonnage ≤ CURVE_MAX_POINTS, dernier point gardé
    assert srvmod.DashboardData._equity_curves([], 10000.0) == ([], [])
    assert srvmod.DashboardData._equity_curves([{"pnl": 1, "closed_at": "x"}], None) == ([], [])
    many = [{"pnl": 1.0, "closed_at": f"2026-01-01T00:00:{i % 60:02d}.{i:06d}"} for i in range(600)]
    pts, dd_curve = srvmod.DashboardData._equity_curves(many, 1000.0)
    assert len(pts) <= srvmod.CURVE_MAX_POINTS + 1 and pts[-1]["equity"] == 1600.0 and len(dd_curve) == len(pts)


def test_payout_done_depuis_le_panneau(home: Path, monkeypatch):
    """PAYOUT_DONE <montant> autorisé depuis le panneau (montant numérique ≥ 0 exigé, 0 = renoncer)."""
    from urllib.error import HTTPError

    from tradinglab.dashboards.server import PANEL_COMMANDS

    assert "PAYOUT_DONE" in PANEL_COMMANDS and "SIMULATE_WITHDRAWAL" not in PANEL_COMMANDS
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH_USER", raising=False)
    srv, base = _srv(home, "tk")
    hdr = {"Content-Type": "application/json", "Cookie": "tladash=tk"}
    try:
        out = json.loads(_open(base + "/api/command", hdr, json.dumps({"command": "PAYOUT_DONE", "arg": "1234,5"}).encode(), "POST").read())
        assert out["ok"] and out["queued"]["command"] == "PAYOUT_DONE" and out["queued"]["args"] == {"target": "1234.50"}
        last = json.loads((home / "state" / "commands.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
        assert last["args"]["target"] == "1234.50" and last["source"] == "dashboard"
        for bad in ("abc", "", "-5", None):
            with pytest.raises(HTTPError) as exc:
                _open(base + "/api/command", hdr, json.dumps({"command": "PAYOUT_DONE", "arg": bad}).encode(), "POST")
            assert exc.value.code == 400, bad
    finally:
        srv.shutdown(); srv.server_close()


def test_pages_stats_et_controle_nouveaux_blocs_et_mobile(home: Path, monkeypatch):
    """Pages /stats et /control : blocs de la seconde passe, échappement, accessibilité, lisibilité téléphone."""
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH_USER", raising=False)
    srv, base = _srv(home, "tk")
    ck = {"Cookie": "tladash=tk"}
    try:
        stats = _open(base + "/stats", ck).read().decode()
        control = _open(base + "/control", ck).read().decode()
        index = _open(base + "/", ck).read().decode()
        login = _open(base + "/login", ck).read().decode()
    finally:
        srv.shutdown(); srv.server_close()
    assert "Se connecter" in login
    for page in (stats, control):
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
        assert "min-height:44px" in page and "overflow-x:hidden" in page and "@media (max-width:640px)" in page
        assert "function esc(" in page and "cdn." not in page and "<script src=" not in page
        assert 'href="/logout"' in page
    # /stats : gestion d'erreurs, cycle raconté, courbes, classes, comptes suiveurs
    for needle in ("Statistiques indisponibles (HTTP", 'href="/login"', "Number.isFinite", "Europe/Paris", 'role="tablist"',
                   'role="tab"', "aria-selected", "aria-label=", "Cycle de paiement FOXX", "Montant à retirer si tout est vert",
                   "Part trader", "aucun paiement encore", "Retrait simulé automatiquement (compte démo traité comme financé)",
                   "demo_as_funded", "equity_curve", "drawdown_curve", "floors", "plancher jour", "plancher global",
                   "by_asset_class", "Par classe d'actifs", "drawCons", "P&L fermé aujourd'hui", "trading_day", "Flottant",
                   "Écart vs maître", "Dernière synchro", "age>120", "Réplications en échec", "failed_copies", "groupActions",
                   "Taux de réussite", "Solde", 'class="hide-sm"', "esc(a.name)", "esc(x.reason", "esc(p.symbol)",
                   "acct='master'", "normTs", "curSym", "overflow-x:auto"):
        assert needle in stats, needle
    assert "Win rate" not in stats and "Balance</small>" not in stats
    # /control : pilules, contexte, raisons, boutons désactivés, confirmation avec symboles, paiement
    for needle in ("Object.keys(bp).length", "risque ouvert", "flottant", "mode_reasons", "lock_reasons",
                   "$('btn_resume').disabled=(d.mode==='AUTO')", "$('btn_pause').disabled=(d.mode==='PAUSED')",
                   "positionSymbols", "Positions : ", "PAYOUT_DONE", 'id="paycard"', "pc.ready||pc.window_open",
                   "P&amp;L jour (equity, flottant inclus)", "esc(f.name)", "setFactor(${i})", ".grid{grid-template-columns:1fr}"):
        assert needle in control, needle
    assert "d.bot_positions_count??d.open_positions??'?'" not in control
    # /index : la jauge lit la projection ``trade_ideas_open`` (seule modification autorisée de la page d'accueil)
    assert "s.trade_ideas_open" in index and "s.trade_ideas ||" not in index


def test_main_n_avertit_que_sans_jeton(monkeypatch, capsys, tmp_path):
    """main() : l'avertissement « non authentifié » n'apparaît que si aucun jeton n'est configuré."""
    from tradinglab.dashboards import server as srvmod

    class Fake:
        server_address = ("0.0.0.0", 1)
        tls_enabled = False

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(srvmod, "create_server", lambda *a, **k: Fake())
    monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    assert srvmod.main(["--host", "0.0.0.0", "--home", str(tmp_path)]) == 0
    assert "n'est pas authentifié" in capsys.readouterr().err
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "x")
    assert srvmod.main(["--host", "0.0.0.0", "--home", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "n'est pas authentifié" not in err and "authentifié" in err


def test_attribut_hidden_toujours_respecte():
    """Rendu téléphone 2026-09-24 : `.cmd{display:flex}` écrasait l'attribut `hidden`, la carte « Paiement effectué »
    s'affichait hors cycle de paiement. Chaque page impose `[hidden]{display:none!important}`."""
    from tradinglab.dashboards import server as srv
    for name in ("INDEX_HTML", "STATS_HTML", "CONTROL_HTML"):
        assert "[hidden]{display:none!important}" in getattr(srv, name), name


def test_resume_par_compte_suiveur_sur_l_accueil(home, monkeypatch):
    """Demande utilisateur 2026-09-24 : le résumé du bandeau (aujourd'hui, positions, cohérence) pour CHAQUE compte,
    pas seulement le maître. `snapshot()` expose `accounts_glance` calculé depuis copy_status_<prefix>.json."""
    from datetime import timedelta

    from tradinglab.core.types import utcnow
    from tradinglab.dashboards.server import INDEX_HTML, DashboardData

    (home / "config" / "copy_trading.yaml").write_text(
        "copy_trading:\n  enabled: true\n  magic_number: 52000\n  followers:\n  - name: \"bg\"\n    env_prefix: COPY1\n    size_factor: 0.5\n    enabled: true\n",
        encoding="utf-8")
    (home / "state").mkdir(exist_ok=True)
    now = utcnow()
    t = lambda sym, pnl, h: {"symbol": sym, "side": "BUY", "pnl": pnl, "opened_at": (now - timedelta(hours=h, minutes=30)).isoformat(),
                             "closed_at": (now - timedelta(hours=h)).isoformat()}
    (home / "state" / "copy_status_COPY1.json").write_text(json.dumps({
        "name": "bg", "ts_utc": now.isoformat(), "equity": 5100.0, "balance": 5050.0,
        "positions": [{"symbol": "EURUSD"}], "closed": [t("EURUSD", 30.0, 0), t("GBPUSD", -10.0, 0), t("XAUUSD", 40.0, 72)]}),
        encoding="utf-8")
    g = DashboardData(home).snapshot()["accounts_glance"]
    assert len(g) == 1 and g[0]["name"] == "bg" and g[0]["floating"] == 50.0 and g[0]["positions"] == 1
    assert g[0]["today_trades"] == 2 and g[0]["today_wins"] == 1 and g[0]["today_losses"] == 1 and g[0]["today_pnl"] == 20.0
    assert g[0]["trading_days"] == 2 and g[0]["consistency_share_percent"] == 66.67 and g[0]["consistency_ok"] is False
    assert "renderAccounts" in INDEX_HTML and "Compte maître" in INDEX_HTML


def test_stats_du_maitre_repartent_du_changement_de_compte(home):
    """Changement de compte maître (2026-09-24) : `master_account_since` fait repartir les statistiques du maître
    à zéro ; les trades antérieurs restent en base pour les agents mais ne comptent plus dans le P&L du maître."""
    import sqlite3

    from tradinglab.dashboards.server import DashboardData

    sysf = home / "config" / "system.yaml"
    sysf.write_text(sysf.read_text(encoding="utf-8").replace("system:\n", 'system:\n  master_account_since: "2026-09-24T15:30:00+00:00"\n', 1),
                    encoding="utf-8")
    (home / "data").mkdir(exist_ok=True)
    con = sqlite3.connect(home / "data" / "learning.db")
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mode TEXT, symbol TEXT, side TEXT, pnl REAL, opened_at TEXT, closed_at TEXT)")
    con.executemany("INSERT INTO trades (mode, symbol, side, pnl, opened_at, closed_at) VALUES ('live',?,?,?,?,?)",
                    [("EURUSD", "BUY", -900.0, "2026-09-24T10:00:00+00:00", "2026-09-24T11:00:00+00:00"),
                     ("GBPUSD", "BUY", 300.0, "2026-09-24T16:00:00+00:00", "2026-09-24T17:00:00+00:00")])
    con.commit(); con.close()
    (home / "logs").mkdir(exist_ok=True)
    with open(home / "logs" / "journal-2026-09-24.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": "2026-09-24T11:00:00+00:00", "kind": "post_trade_review", "agent_id": "B01", "ticket": 1,
                            "result_r": -1.0, "pnl": -900.0}) + "\n")
        f.write(json.dumps({"ts_utc": "2026-09-24T17:00:00+00:00", "kind": "post_trade_review", "agent_id": "C06", "ticket": 2,
                            "result_r": 1.0, "pnl": 300.0}) + "\n")
    d = DashboardData(home)
    assert [t["symbol"] for t in d._master_trades()] == ["GBPUSD"]
    st = d.stats()
    assert st["total"]["trades"] == 1 and st["total"]["pnl"] == 300.0


def test_page_qualite_et_api(home: Path):
    """Plan pro du 2026-09-25, point 5 : page Qualité (espérance + marge, coût, glissement, gel)."""
    from tradinglab.learning.store import LearningStore, TradeRecord
    (home / "data").mkdir(exist_ok=True)
    st = LearningStore(home / "data" / "learning.db")
    for i in range(12):
        st.record_trade(TradeRecord(ticket=i, agent_id="E05", symbol="EURUSD", side="BUY", entry=1.1, sl=1.098,
                                    risk_money=628.0, risk_percent=0.125, result_r=2.0 if i % 2 else -1.0, pnl=0.0,
                                    opened_at="2026-09-26T10:00:00+00:00", closed_at=f"2026-09-26T10:{i:02d}:00+00:00",
                                    exit_reason="tp" if i % 2 else "sl", features={"cost_ratio": 0.1}))
    srv = create_server(home, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, ctype, body = _get(srv, "/qualite")
        assert code == 200 and "text/html" in ctype and "Qualité" in body
        code, _, body = _get(srv, "/api/quality")
        q = json.loads(body)
        assert q["global"]["n"] == 12 and q["agents"][0]["agent_id"] == "E05" and q["global"]["cout_moy_pct"] == 10.0
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)



def test_page_shadow_et_recherche_repond_sans_erreur(home: Path):
    """2026-10-01 : la page /shadow affichait « 'Settings' object has no attribute 'strategies' » (mauvais nom de
    réglage, non couvert par le test du tableau qui appelait shadow_board directement)."""
    from tradinglab.dashboards.server import DashboardData

    d = DashboardData(home).shadow()
    assert "erreur" not in d, d.get("erreur")
    assert {"resume", "familles", "agents", "recherche", "criteres"} <= set(d)
    assert d["criteres"]["min_shadow"] == 20 and d["criteres"]["perdant_n"] == 50
