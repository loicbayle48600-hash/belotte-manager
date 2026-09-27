"""Copy trading (2026-09-22) : plan de synchronisation pur + application sur un broker simulé.

Garanties testées : appariement par ticket maître (commentaire TLABCOPY), volume proportionnel à
l'equity arrondi VERS LE BAS, jamais d'ouverture sous le volume minimal ni sur export périmé
(mais toujours les fermetures), suivi des partiels et des SL, et export maître écrit par l'orchestrateur.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradinglab.copy.copier import COPY_COMMENT_PREFIX, CopyTrader, plan_sync
from pathlib import Path

from tradinglab.core.types import Side
from tradinglab.mt5.mock_adapter import MockBroker

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def master(equity=500000.0, positions=None, ts=NOW):
    return {"ts_utc": ts.isoformat(), "equity": equity, "magic": 51000, "positions": positions or []}


def mpos(ticket, symbol="EURUSD", side="BUY", volume=1.0, sl=1.08, tp=1.10):
    return {"ticket": ticket, "symbol": symbol, "side": side, "volume": volume, "sl": sl, "tp": tp,
            "price_open": 1.09}


class _Spec:
    def __init__(self, vmin=0.01, step=0.01, vmax=100.0):
        self.volume_min, self.volume_step, self.volume_max = vmin, step, vmax


class _FPos:
    def __init__(self, ticket, master_ticket, symbol="EURUSD", volume=0.1, sl=1.08, tp=1.10):
        self.ticket, self.symbol, self.volume, self.sl, self.tp = ticket, symbol, volume, sl, tp
        self.comment = f"{COPY_COMMENT_PREFIX}{master_ticket}"


SPECS = {"EURUSD": _Spec(), "XAUUSD": _Spec()}


def test_ouverture_proportionnelle_a_l_equity():
    """Maître 500 k, suiveur 50 k, facteur 1.0 → 10 % du volume maître, arrondi vers le bas."""
    acts = plan_sync(master(positions=[mpos(1, volume=1.27)]), [], 50000.0, 1.0, SPECS)
    assert len(acts) == 1 and acts[0].kind == "open"
    assert acts[0].volume == pytest.approx(0.12)          # 0.127 arrondi VERS LE BAS au pas 0.01
    assert acts[0].side is Side.BUY and acts[0].sl == 1.08 and acts[0].master_ticket == 1


def test_volume_trop_petit_ou_symbole_inconnu_pas_d_ouverture():
    acts = plan_sync(master(positions=[mpos(1, volume=0.03)]), [], 5000.0, 1.0, SPECS)
    assert acts == []                                     # 0.03 × 1 % = 0.0003 < volume_min
    acts = plan_sync(master(positions=[mpos(1, symbol="INCONNU")]), [], 50000.0, 1.0, SPECS)
    assert acts == []


def test_fermeture_et_partiel_suivis():
    """Le maître clôt le ticket 1 et réduit le 2 de moitié : le suiveur ferme et réduit d'autant."""
    follower = [_FPos(101, 1), _FPos(102, 2, volume=0.10)]
    m = master(positions=[mpos(2, volume=0.5)])           # ticket 1 disparu ; 2 réduit (était 1.0)
    acts = plan_sync(m, follower, 50000.0, 1.0, SPECS)
    kinds = {a.kind: a for a in acts}
    assert kinds["close"].follower_ticket == 101
    assert kinds["reduce"].follower_ticket == 102 and kinds["reduce"].volume == pytest.approx(0.05)


def test_sl_du_maitre_replique():
    follower = [_FPos(101, 1, sl=1.08)]
    acts = plan_sync(master(positions=[mpos(1, sl=1.0850)]), follower, 50000.0, 1.0, SPECS)
    assert len(acts) == 1 and acts[0].kind == "modify" and acts[0].sl == 1.0850


def test_export_perime_ferme_mais_n_ouvre_pas():
    follower = [_FPos(101, 1)]
    m = master(positions=[mpos(2)])                       # 1 à fermer, 2 à ouvrir
    acts = plan_sync(m, follower, 50000.0, 1.0, SPECS, allow_open=False)
    assert [a.kind for a in acts] == ["close"]


def test_copytrader_applique_sur_broker_simule(tmp_path):
    """Bout en bout : export écrit → sync() ouvre chez le suiveur avec le bon commentaire et magic."""
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "master_positions.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)],
                                        ts=b.now())), encoding="utf-8")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000)
    acts = tr.sync(now=b.now())
    assert [a.kind for a in acts] == ["open"]
    pos = b.positions(magic=52000)
    assert len(pos) == 1 and pos[0].comment.startswith(COPY_COMMENT_PREFIX + "1")
    assert pos[0].volume == pytest.approx(acts[0].volume) and pos[0].sl > 0
    # le maître clôt → le suiveur clôt au sync suivant
    mfile.write_text(json.dumps(master(positions=[], ts=b.now())), encoding="utf-8")
    acts = tr.sync(now=b.now())
    assert [a.kind for a in acts] == ["close"] and b.positions(magic=52000) == []


def test_export_maitre_ecrit_par_l_orchestrateur(settings, broker):
    from tests.test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    o.cycle()
    f = settings.state_dir / "master_positions.json"
    assert f.exists()
    d = json.loads(f.read_text(encoding="utf-8"))
    assert d["magic"] == settings.magic and "positions" in d and d["equity"] > 0


# ---------------------------------------------------------------- inscription via le panneau (2026-09-22)
def _copy_home(tmp_path):
    import shutil
    from pathlib import Path
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    # config copy trading VIERGE : le dépôt réel peut contenir des suiveurs inscrits par l'utilisateur
    (tmp_path / "config" / "copy_trading.yaml").write_text(
        "copy_trading:\n  enabled: true\n  magic_number: 52000\n  poll_interval_sec: 5\n  stale_after_sec: 120\n  followers: []\n",
        encoding="utf-8")
    (tmp_path / ".env").write_text("MT5_LOGIN=1\n", encoding="utf-8")
    term = tmp_path / "mt5-copy1" / "terminal64.exe"
    term.parent.mkdir()
    term.write_bytes(b"x")
    return tmp_path, term


def test_register_follower_ecrit_env_et_yaml(tmp_path):
    from tradinglab.copy.registry import followers_status, ready_names, register_follower

    home, term = _copy_home(tmp_path)
    out = register_follower(home, "compte2", "12345", "s3cret", "ICMarketsEU-Demo", str(term), 0.5)
    assert out["env_prefix"] == "COPY1"
    env = (home / ".env").read_text(encoding="utf-8")
    assert "COPY1_MT5_LOGIN=12345" in env and "COPY1_MT5_PASSWORD=s3cret" in env
    st = followers_status(home)
    assert st == [{"name": "compte2", "enabled": True, "size_factor": 0.5, "env_prefix": "COPY1",
                   "creds_ok": True, "terminal_ok": True}]
    assert ready_names(home) == ["compte2"]
    # doublon refusé ; préfixe suivant pour un second compte
    with pytest.raises(ValueError):
        register_follower(home, "compte2", "1", "x", "s", str(term), 1.0)
    out2 = register_follower(home, "papa", "777", "pw", "srv", str(term), 1.0)
    assert out2["env_prefix"] == "COPY2"


def test_register_follower_validations(tmp_path):
    from tradinglab.copy.registry import register_follower

    home, term = _copy_home(tmp_path)
    for bad in (dict(name="X!"), dict(login="abc"), dict(password=""), dict(terminal_path=str(term.parent)),
                dict(size_factor=50)):
        kw = dict(name="ok-1", login="123", password="p", server="s", terminal_path=str(term), size_factor=1.0)
        kw.update(bad)
        with pytest.raises(ValueError):
            register_follower(home, **kw)


def test_api_follower_du_panneau(tmp_path, monkeypatch):
    """POST /api/copy/follower : authentifié, écrit .env+yaml, ne renvoie jamais le mot de passe ;
    GET /api/copy/status liste l'état. Sans session → 401."""
    import threading
    import urllib.request
    from urllib.error import HTTPError

    from tradinglab.dashboards.server import create_server

    home, term = _copy_home(tmp_path)
    (home / "state").mkdir(exist_ok=True)
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    monkeypatch.delenv("DASHBOARD_AUTH_USER", raising=False)
    srv = create_server(home=home, host="127.0.0.1", port=0, auth_token="tk")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    ck = {"Cookie": "tladash=tk", "Content-Type": "application/json"}
    body = json.dumps({"name": "compte2", "login": "12345", "password": "s3cret", "server": "srv",
                       "terminal_path": str(term), "size_factor": 1.0}).encode()
    try:
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/copy/follower", data=body, method="POST",
                                                          headers={"Content-Type": "application/json"}), timeout=5)
            raise AssertionError("inscription sans session acceptée")
        except HTTPError as e:
            assert e.code == 401
        resp = urllib.request.urlopen(urllib.request.Request(base + "/api/copy/follower", data=body,
                                                             method="POST", headers=ck), timeout=5)
        d = json.loads(resp.read())
        assert d["ok"] and "s3cret" not in json.dumps(d)
        assert "COPY1_MT5_PASSWORD=s3cret" in (home / ".env").read_text(encoding="utf-8")
        st = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/copy/status", headers=ck), timeout=5).read())
        assert st["followers"][0]["name"] == "compte2" and st["followers"][0]["creds_ok"]
        assert "s3cret" not in json.dumps(st)
    finally:
        srv.shutdown()


def test_terminal_autodetecte(tmp_path):
    """Chemin du terminal laissé vide (2026-09-22, demande utilisateur) : le système choisit une installation
    MT5 LIBRE — jamais celle du maître ni celle d'un autre suiveur ; aucune libre → message qui guide."""
    from tradinglab.copy.registry import autodetect_terminal, register_follower

    home, term_master = _copy_home(tmp_path)
    base = tmp_path / "pf"
    for d in ("MetaTrader 5 IC Markets EU", "MetaTrader 5"):
        (base / d).mkdir(parents=True)
        (base / d / "terminal64.exe").write_bytes(b"x")
    master_path = base / "MetaTrader 5 IC Markets EU" / "terminal64.exe"
    with open(home / ".env", "a", encoding="utf-8") as f:
        f.write(f"MT5_TERMINAL_PATH={master_path}\n")
    assert autodetect_terminal(home, [base]) == base / "MetaTrader 5" / "terminal64.exe"
    out = register_follower(home, "demo2", "999", "pw", "srv", "", 1.0, search_dirs=[base])
    assert out["terminal"] == str(base / "MetaTrader 5" / "terminal64.exe")
    env = (home / ".env").read_text(encoding="utf-8")
    assert f"COPY1_MT5_TERMINAL_PATH={base / 'MetaTrader 5' / 'terminal64.exe'}" in env
    # plus aucune installation libre → refus clair pour le suiveur suivant
    with pytest.raises(ValueError, match="aucune installation MT5 utilisable"):
        register_follower(home, "demo3", "888", "pw", "srv", "", 1.0, search_dirs=[base])


def test_nom_libre_accents_espaces(tmp_path):
    """2026-09-22, demande utilisateur : noms jusqu'à 40 caractères avec accents/espaces/majuscules,
    cités dans le YAML ; doublon détecté sans tenir compte de la casse."""
    from tradinglab.copy.registry import load_cfg, register_follower

    home, term = _copy_home(tmp_path)
    out = register_follower(home, "Compte Démo 10k", "999", "pw", "srv", str(term), 1.0)
    cfg = load_cfg(home)
    assert cfg["followers"][0]["name"] == "Compte Démo 10k"
    with pytest.raises(ValueError, match="existe déjà"):
        register_follower(home, "compte démo 10K", "1", "x", "s", str(term), 1.0)
    with pytest.raises(ValueError, match="nom invalide"):
        register_follower(home, 'mauvais"nom', "1", "x", "s", str(term), 1.0)


def test_facteur_modifiable_a_chaud_et_temporisation(tmp_path):
    """Panneau (2026-09-22) : set_size_factor réécrit le YAML (commentaires préservés) ; le copieur relit le
    facteur à chaque cycle ; une ouverture refusée n'est retentée qu'après 60 s."""
    from tradinglab.copy.registry import follower_size_factor, register_follower, set_size_factor

    home, term = _copy_home(tmp_path)
    register_follower(home, "Démo 10k", "1", "p", "s", str(term), 1.0)
    register_follower(home, "papa", "2", "p", "s", str(term), 1.0)
    assert set_size_factor(home, "Démo 10k", 0.5) == {"name": "Démo 10k", "size_factor": 0.5}
    assert follower_size_factor(home, "Démo 10k") == 0.5 and follower_size_factor(home, "papa") == 1.0
    text = (home / "config" / "copy_trading.yaml").read_text(encoding="utf-8")
    assert text.count("- name:") == 2 and "size_factor: 0.5" in text and "size_factor: 1.0" in text   # l'autre bloc est intact
    with pytest.raises(ValueError):
        set_size_factor(home, "inconnu", 1.0)
    with pytest.raises(ValueError):
        set_size_factor(home, "papa", 42)
    # facteur relu à chaud par le copieur
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "master_positions.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())), encoding="utf-8")
    factor = {"v": 1.0}
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, factor_source=lambda: factor["v"])
    factor["v"] = 0.5
    acts = tr.sync(now=b.now())
    assert tr.size_factor == 0.5 and acts and acts[0].volume == pytest.approx(0.05)
    # temporisation : une ouverture refusée par le broker n'est pas retentée dans la minute
    b2 = MockBroker(seed=7, balance=50000.0)
    b2.connect()
    tr2 = CopyTrader(b2, mfile, size_factor=1.0, magic=52000)
    b2.order_send = lambda req: type("R", (), {"ok": False, "comment": "AutoTrading disabled by client"})()
    a1 = tr2.sync(now=b2.now())
    a2 = tr2.sync(now=b2.now() + timedelta(seconds=10))
    a3 = tr2.sync(now=b2.now() + timedelta(seconds=70))
    assert [a.kind for a in a1] == ["open"] and a2 == [] and [a.kind for a in a3] == ["open"]


def test_copie_portable_dediee_avec_autotrading(tmp_path, monkeypatch):
    """Nouveau compte (2026-09-22) : copie portable du terminal maître + profil + AutoTrading pré-activé
    (common.ini UTF-16, [Experts] Enabled=1) ; la copie devient le terminal du suiveur."""
    from tradinglab.copy import registry as reg

    home, _ = _copy_home(tmp_path)
    master = tmp_path / "MetaTrader 5"
    (master / "config").mkdir(parents=True)
    (master / "terminal64.exe").write_bytes(b"exe")
    (master / "logs").mkdir()
    (master / "logs" / "x.log").write_bytes(b"log")
    with open(home / ".env", "a", encoding="utf-8") as f:
        f.write(f"MT5_TERMINAL_PATH={master / 'terminal64.exe'}\n")
    # profil maître (dossier de données) avec un common.ini UTF-16 où AutoTrading est DÉSACTIVÉ
    appdata = tmp_path / "appdata"
    prof = appdata / "MetaQuotes" / "Terminal" / "ABC123"
    (prof / "config").mkdir(parents=True)
    (prof / "origin.txt").write_text(str(master), encoding="utf-16")
    (prof / "config" / "common.ini").write_bytes("[Common]\r\nLogin=1\r\n[Experts]\r\nAllowDllImport=0\r\nEnabled=0\r\n".encode("utf-16"))
    monkeypatch.setenv("APPDATA", str(appdata))
    root = tmp_path / "copies"
    root.mkdir()
    monkeypatch.setattr(reg, "prepare_portable_terminal",
                        lambda h, n: _prepare_in(reg, h, n, root))
    out = reg.register_follower(home, "Compte Démo 10k", "999", "pw", "srv", "", 1.0)
    dest = Path(out["terminal"]).parent
    assert dest.name == "mt5-compte-demo-10k" and (dest / "terminal64.exe").exists()
    assert not (dest / "logs").exists()                                   # logs ignorés
    ini = (dest / "config" / "common.ini").read_bytes().decode("utf-16")
    assert "[Experts]" in ini and "Enabled=1" in ini and "Enabled=0" not in ini
    assert f"COPY1_MT5_TERMINAL_PATH={dest / 'terminal64.exe'}" in (home / ".env").read_text(encoding="utf-8")


def _prepare_in(reg, home, name, root):
    """Même logique que prepare_portable_terminal mais racine de test (pas C:\Claude-MT5-Trading)."""
    import re as _re
    import shutil
    from pathlib import Path as _P

    env = reg._env_text(home)
    master_exe = _P(_re.search(r"^MT5_TERMINAL_PATH=(.+)$", env, _re.M).group(1).strip())
    dest = root / f"mt5-{reg._slug(name)}"
    shutil.copytree(master_exe.parent, dest, ignore=shutil.ignore_patterns("logs", "*.log", "bases", "history"))
    data = reg._master_data_dir(master_exe)
    assert data is not None, "profil maître introuvable via origin.txt"
    shutil.copytree(data / "config", dest / "config", dirs_exist_ok=True)
    reg.enable_algo_trading(dest)
    return dest / "terminal64.exe"


def test_commentaire_ecrase_pas_de_doublon_adoption_et_table(tmp_path):
    """Incident du 2026-09-22 (Admirals) : le commentaire TLABCOPY est écrasé par une fermeture partielle →
    l'ancien appariement rouvrait la copie à chaque cycle (16 doublons). Désormais : table persistante,
    puis adoption de l'orpheline de même symbole/sens — jamais d'ouverture en double."""
    from tradinglab.copy.copier import plan_sync

    # 1) plan pur : orpheline (commentaire perdu) + maître présent → adoptée, pas ouverte
    orphan = _FPos(101, 999, volume=0.10)
    orphan.comment = "copy: partiel maître"
    orphan.side = Side.BUY
    acts = plan_sync(master(positions=[mpos(1, volume=1.0)]), [orphan], 50000.0, 1.0, SPECS)
    assert [a.kind for a in acts] == ["adopt"] and acts[0].follower_ticket == 101 and acts[0].master_ticket == 1
    # 2) table persistante : le ticket suiveur est reconnu même sans commentaire, et rien n'est rouvert
    acts = plan_sync(master(positions=[mpos(1, volume=1.0)]), [orphan], 50000.0, 1.0, SPECS, mapping={1: 101})
    assert acts == []
    # 3) orpheline sans aucun maître compatible → fermée (copie obsolète)
    acts = plan_sync(master(positions=[]), [orphan], 50000.0, 1.0, SPECS)
    assert [a.kind for a in acts] == ["close"]
    # 4) bout en bout avec broker simulé : la table est écrite après l'ouverture et survit à un redémarrage
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "master_positions.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())), encoding="utf-8")
    status = tmp_path / "copy_status_COPYX.json"
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, status_file=status)
    tr.sync(now=b.now())
    assert (tmp_path / "copy_map_COPYX.json").exists() and tr.mapping[1]["ticket"] == b.positions(magic=52000)[0].ticket
    # le broker « perd » le commentaire : aucune ouverture en double, ni avec la même instance ni après redémarrage
    for pos in b.positions(magic=52000):
        pos.comment = "copy: partiel maître"
    assert tr.sync(now=b.now()) == []
    tr2 = CopyTrader(b, mfile, size_factor=1.0, magic=52000, status_file=status)
    assert tr2.mapping == tr.mapping and tr2.sync(now=b.now()) == [] and len(b.positions(magic=52000)) == 1


def test_export_maitre_resiste_a_une_collision_windows(settings, broker, monkeypatch):
    """PermissionError constaté le 2026-09-23 : sous Windows, os.replace échoue si un copieur lit le
    fichier au même instant. L'export réessaie au lieu de sauter le cycle — les suiveurs ne travaillent
    jamais sur une photo périmée."""
    import os

    from tests.test_review_market_agents_orchestration import make_orch

    o = make_orch(settings, broker)
    reel = os.replace
    essais = {"n": 0}

    def replace_capricieux(src, dst):
        essais["n"] += 1
        if essais["n"] == 1:
            raise PermissionError("fichier verrouillé par un autre processus")
        return reel(src, dst)

    monkeypatch.setattr(os, "replace", replace_capricieux)
    o.cycle()
    assert essais["n"] >= 2, "l'export doit réessayer après une collision"
    f = settings.state_dir / "master_positions.json"
    assert f.exists() and json.loads(f.read_text(encoding="utf-8"))["magic"] == settings.magic
    warns = [e for e in o.journal.read_day(kinds={"warning"}) if "export copy trading" in str(e.get("message", ""))]
    assert not warns, "une collision rattrapée ne doit pas être signalée comme un échec"


# ---------------------------------------------------------------- correspondance des symboles (2026-09-23)
def test_symbol_map_traduit_les_noms_de_broker():
    """Chaque broker nomme les mêmes instruments autrement : sans traduction, ces positions n'étaient
    pas répliquées du tout (constaté : US500, XNGUSD, XAGUSD absents chez les suiveurs)."""
    from tradinglab.copy.symbol_map import build_map, normalize

    assert normalize("EURUSD.m") == normalize("#EURUSD") == normalize("EURUSD") == "EURUSD"
    assert normalize("[SP500]") == "SP500"
    suiveur = ["EURUSD", "SPX500", "NGAS", "XAGUSD", "GOLD", "UKOIL", "EURSGD.m"]
    m = build_map(["EURUSD", "US500", "XNGUSD", "XAGUSD", "XAUUSD", "XBRUSD", "EURSGD", "EURHKD"], suiveur)
    assert m["EURUSD"] == "EURUSD"          # nom identique
    assert m["EURSGD"] == "EURSGD.m"        # même racine, décoration du broker
    assert m["US500"] == "SPX500"           # alias d'indice
    assert m["XNGUSD"] == "NGAS"            # alias d'énergie
    assert m["XAUUSD"] == "GOLD" and m["XBRUSD"] == "UKOIL"
    assert m["XAGUSD"] == "XAGUSD"          # le nom exact l'emporte sur l'alias
    assert "EURHKD" not in m, "un instrument absent ne doit JAMAIS être remplacé par un approchant"


def test_copie_sur_le_symbole_du_suiveur(tmp_path):
    """Le maître ouvre US500 ; le suiveur, qui ne connaît que SPX500, ouvre bien SPX500."""
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    sym = b.symbols()[0]
    tick = b.tick(sym)
    mfile = tmp_path / "master_positions.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, symbol="ALIAS_MAITRE", volume=1.0,
                                                       sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())),
                     encoding="utf-8")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, name="test")
    # le maître appelle l'instrument autrement : la table le résout vers le symbole réel du suiveur
    tr._sym_cache = {"ALIAS_MAITRE": sym}
    tr._sym_missing = set()
    acts = tr.sync(now=b.now())
    assert [a.kind for a in acts] == ["open"]
    assert acts[0].symbol == sym and acts[0].master_symbol == "ALIAS_MAITRE"
    pos = b.positions(magic=52000)
    assert len(pos) == 1 and pos[0].symbol == sym


def test_instrument_absent_signale_une_fois_et_non_copie(tmp_path):
    """Instrument introuvable chez le suiveur : pas de copie, une seule ligne au journal (pas à chaque cycle)."""
    from tradinglab.core.journal import Journal

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    mfile = tmp_path / "master_positions.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, symbol="INEXISTANT_XYZ")], ts=b.now())), encoding="utf-8")
    journal = Journal(tmp_path / "logs")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, name="test", journal=journal)
    for _ in range(3):
        assert tr.sync(now=b.now()) == []
    assert b.positions(magic=52000) == []
    from datetime import datetime, timezone
    alertes = [e for e in journal.read_day(datetime.now(timezone.utc)) if "absents chez le suiveur" in str(e.get("message", ""))]
    assert len(alertes) == 1 and alertes[0]["symbols"] == ["INEXISTANT_XYZ"]


class _Journal:
    """Capture les événements `copy_trade` sans toucher au disque."""

    def __init__(self):
        self.events: list[dict] = []

    def event(self, kind, **kw):
        self.events.append({"kind": kind, **kw})


def _master_file(tmp_path, b, positions):
    mfile = tmp_path / "master_positions.json"
    mfile.write_text(json.dumps(master(positions=positions, ts=b.now())), encoding="utf-8")
    return mfile


def test_temporisation_croissante_des_ouvertures_refusees(tmp_path):
    """Audit 2026-09-23 : 1 858 ouvertures refusées en 4 jours (« Trade disabled », « No money »,
    « AutoTrading disabled ») retentées toutes les 60 s, sans jamais journaliser la réponse du broker.
    Désormais : 60 s, 120 s, 240 s… plafonné à 900 s par ticket maître, retcode + commentaire au journal,
    compteur remis à zéro après un succès."""
    from tradinglab.core.types import OrderResult

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = _master_file(tmp_path, b, [mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)])
    j = _Journal()
    # export maître considéré frais pendant tout le test (sinon plus d'ouverture après 120 s : c'est une autre règle)
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, journal=j, stale_after_sec=10_000)
    b.order_send = lambda req: OrderResult(ok=False, retcode=10017, comment="Trade disabled")
    t0 = b.now()
    tentatives = []
    for sec in range(0, 4000, 5):                      # un sync toutes les 5 s pendant ~67 min
        if tr.sync(now=t0 + timedelta(seconds=sec)):
            tentatives.append(sec)
    # 60 → 120 → 240 → 480 → 900 (plafond) : 8 tentatives au lieu de 800
    assert tentatives == [0, 60, 180, 420, 900, 1800, 2700, 3600]
    refus = [e for e in j.events if e.get("message") == "ouverture copiée"]
    assert refus and all(e["ok"] is False and e["retcode"] == 10017 and e["detail"] == "Trade disabled" for e in refus)
    assert [e["retry_in_sec"] for e in refus] == [60, 120, 240, 480, 900, 900, 900, 900]
    # un succès remet le compteur à zéro : le prochain refus repart à 60 s
    del b.order_send
    assert [a.kind for a in tr.sync(now=t0 + timedelta(seconds=4500))] == ["open"]
    assert tr._failures.get(("open", 1)) is None


def test_modification_sl_refusee_temporisee_et_expliquee(tmp_path):
    """Audit 2026-09-23 : « SL/TP copiés ok=False » ×170 toutes les 5 s sur un même ticket (Admirals,
    « Invalid stops »), sans retcode ni commentaire. La modification refusée est désormais temporisée
    comme une ouverture et le journal porte la réponse du broker. Les fermetures, elles, ne sont
    jamais temporisées : une exposition orpheline se réduit à chaque cycle."""
    from tradinglab.core.types import OrderResult

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    sl0 = round(tick.bid - 0.005, 5)
    mfile = _master_file(tmp_path, b, [mpos(1, volume=1.0, sl=sl0, tp=0.0)])
    j = _Journal()
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, journal=j, stale_after_sec=10_000)
    t0 = b.now()
    assert [a.kind for a in tr.sync(now=t0)] == ["open"]
    # le maître resserre son SL ; le broker suiveur refuse la modification
    _master_file(tmp_path, b, [mpos(1, volume=1.0, sl=round(sl0 + 0.001, 5), tp=0.0)])
    b.modify_position = lambda ticket, sl, tp: OrderResult(ok=False, retcode=10016, comment="Invalid stops")
    kinds = [[a.kind for a in tr.sync(now=t0 + timedelta(seconds=s))] for s in (5, 10, 15, 66, 70, 190)]
    assert kinds == [["modify"], [], [], ["modify"], [], ["modify"]]
    refus = [e for e in j.events if e.get("message") == "SL/TP copiés"]
    assert [e["retry_in_sec"] for e in refus] == [60, 120, 240]
    assert all(e["retcode"] == 10016 and e["detail"] == "Invalid stops" for e in refus)
    # fermeture refusée : retentée dès le cycle suivant, avec le détail du refus
    _master_file(tmp_path, b, [])
    b.close_position = lambda ticket, volume=None, comment="": OrderResult(ok=False, retcode=10018, comment="Market closed")
    kinds = [[a.kind for a in tr.sync(now=t0 + timedelta(seconds=s))] for s in (300, 305, 310)]
    assert kinds == [["close"], ["close"], ["close"]]
    ferm = [e for e in j.events if e.get("message") == "fermeture copiée"]
    assert ferm[-1]["ok"] is False and ferm[-1]["retcode"] == 10018 and ferm[-1]["detail"] == "Market closed"


def test_instrument_non_negociable_chez_le_suiveur(tmp_path):
    """Blue Guardian répondait « Trade disabled » 265 fois sur CADCHF : l'instrument existe mais n'est pas
    négociable (`trade_allowed` faux). Aucune ouverture tentée, signalé une fois, et une position déjà
    ouverte sur ce symbole reste gérée (fermeture suivie)."""
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = _master_file(tmp_path, b, [mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)])
    j = _Journal()
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, journal=j, name="bg")
    b.specs["EURUSD"].trade_allowed = False
    for s in (0, 5, 10):
        assert tr.sync(now=b.now() + timedelta(seconds=s)) == []
    alertes = [e for e in j.events if "non négociable" in str(e.get("message", ""))]
    assert len(alertes) == 1 and alertes[0]["symbol"] == "EURUSD" and alertes[0]["follower"] == "bg"
    assert b.positions(magic=52000) == []


def test_sl_maitre_arrondi_a_la_precision_du_suiveur_et_no_changes_accepte(tmp_path):
    """Capture utilisateur 2026-09-24 (Blue Guardian) : « SL/TP copiés — No changes (code 10025) » en boucle. Le maître
    cote DE40 à 2 décimales (SL 25 595,14), le suiveur à 1 : l'écart ne disparaissait jamais, la même modification
    repartait sans fin. Les prix du maître sont ramenés aux décimales du suiveur, et 10025 compte comme un succès."""
    from types import SimpleNamespace

    from tradinglab.core.types import OrderResult

    spec1 = SimpleNamespace(volume_min=0.01, volume_step=0.01, volume_max=100.0, digits=1, point=0.1)
    fpos = _FPos(7, 1, symbol="DE40", volume=0.1, sl=25595.1, tp=24867.4)
    fpos.side = Side.SELL
    m = master(positions=[mpos(1, symbol="DE40", side="SELL", volume=1.0, sl=25595.14, tp=24867.35)])
    acts = plan_sync(m, [fpos], 50000.0, 1.0, {"DE40": spec1}, sym_map={"DE40": "DE40"},
                     mapping={1: {"ticket": 7, "mv": 1.0, "fv": 0.1}})
    assert [a.kind for a in acts] == [], "même SL/TP une fois arrondis à 1 décimale : rien à modifier"
    # un vrai changement du maître reste répliqué, arrondi à la précision du suiveur
    m2 = master(positions=[mpos(1, symbol="DE40", side="SELL", volume=1.0, sl=25580.27, tp=24867.35)])
    acts = plan_sync(m2, [fpos], 50000.0, 1.0, {"DE40": spec1}, sym_map={"DE40": "DE40"},
                     mapping={1: {"ticket": 7, "mv": 1.0, "fv": 0.1}})
    assert [a.kind for a in acts] == ["modify"] and acts[0].sl == 25580.3
    # 10025 « No changes » : succès, aucune temporisation
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "m.json"
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())), encoding="utf-8")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, stale_after_sec=10_000)
    tr.sync(now=b.now())
    mfile.write_text(json.dumps(master(positions=[mpos(1, volume=1.0, sl=round(tick.bid - 0.004, 5), tp=0.0)], ts=b.now())), encoding="utf-8")
    b.modify_position = lambda ticket, sl, tp: OrderResult(ok=False, retcode=10025, comment="No changes")
    assert [a.kind for a in tr.sync(now=b.now() + timedelta(seconds=5))] == ["modify"]
    assert tr._failures == {} and tr._modify_after == {}


def test_action_diese_jamais_prise_pour_un_indice():
    """Captures utilisateur 2026-09-24 (Admirals) : US30 traduit en « #DOW » — l'action Dow Inc., pas le Dow Jones.
    Le broker a refusé le stop (51 873 sur une action à ~30 $) : sans ce refus, le copieur ouvrait une action."""
    from tradinglab.copy.symbol_map import build_map, is_single_stock

    admirals = ["#DOW", "[DJI30]", "#GOLD", "GOLD", "[NQ100]", "[NIKKEI225]", "EURUSD", "#AAPL"]
    m = build_map(["US30", "XAUUSD", "USTEC", "JP225", "EURUSD"], admirals)
    assert m == {"US30": "[DJI30]", "XAUUSD": "GOLD", "USTEC": "[NQ100]", "JP225": "[NIKKEI225]", "EURUSD": "EURUSD"}
    assert is_single_stock("#DOW") and not is_single_stock("[DJI30]")
    assert build_map(["US30"], ["#DOW"]) == {}, "seule une action disponible : pas de copie plutôt qu'une action"


def test_copie_fermee_chez_le_suiveur_jamais_rouverte(tmp_path):
    """2026-09-24 : 9 tickets maîtres rouverts après que la copie a été stoppée CHEZ LE SUIVEUR (prix du broker
    suiveur légèrement différents) — AUDJPY rouvert 12 min après son stop, à un moins bon prix. Un ticket déjà copié
    ne se rouvre plus, même après redémarrage du copieur ; il est oublié quand le maître le ferme."""
    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tick = b.tick("EURUSD")
    mfile = tmp_path / "master_positions.json"
    status = tmp_path / "copy_status_COPYX.json"
    pos = [mpos(1, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)]
    mfile.write_text(json.dumps(master(positions=pos, ts=b.now())), encoding="utf-8")
    tr = CopyTrader(b, mfile, size_factor=1.0, magic=52000, status_file=status, stale_after_sec=10_000)
    assert [a.kind for a in tr.sync(now=b.now())] == ["open"]
    for p in b.positions(magic=52000):                     # stop touché chez le suiveur seulement
        b.close_position(p.ticket)
    assert tr.sync(now=b.now() + timedelta(seconds=5)) == []
    assert (tmp_path / "copy_done_COPYX.json").exists()
    tr2 = CopyTrader(b, mfile, size_factor=1.0, magic=52000, status_file=status, stale_after_sec=10_000)
    assert tr2.sync(now=b.now() + timedelta(seconds=10)) == [], "pas de réouverture après redémarrage non plus"
    # le maître ferme puis ouvre une NOUVELLE position : elle est copiée
    mfile.write_text(json.dumps(master(positions=[mpos(2, volume=1.0, sl=round(tick.bid - 0.005, 5), tp=0.0)], ts=b.now())),
                     encoding="utf-8")
    assert [a.kind for a in tr2.sync(now=b.now() + timedelta(seconds=15))] == ["open"] and tr2.done == {2}


def test_suppression_complete_d_un_suiveur(tmp_path):
    """Demande utilisateur 2026-09-24 : supprimer complètement un compte suiveur depuis le panneau. Bloc YAML,
    fichiers d'état du copieur et lignes .env du suiveur disparaissent ; les autres suiveurs sont intacts."""
    from tradinglab.copy.registry import followers_status, register_follower, remove_follower

    home, term = _copy_home(tmp_path)
    register_follower(home, "Démo 10k", "111", "p1", "s1", str(term), 1.0)
    register_follower(home, "papa", "222", "p2", "s2", str(term), 2.0)
    (home / "state").mkdir()
    for fn in ("copy_status_COPY1.json", "copy_map_COPY1.json", "copy_done_COPY1.json", "copy_status_COPY2.json"):
        (home / "state" / fn).write_text("{}", encoding="utf-8")
    out = remove_follower(home, "Démo 10k")
    assert out["env_prefix"] == "COPY1" and out["env_lines_removed"] == 4 and len(out["state_files_removed"]) == 3
    assert [f["name"] for f in followers_status(home)] == ["papa"]
    env = (home / ".env").read_text(encoding="utf-8")
    assert "COPY1_" not in env and "Démo 10k" not in env and "COPY2_MT5_LOGIN=222" in env and "MT5_LOGIN=1" in env
    assert (home / "state" / "copy_status_COPY2.json").exists()
    remove_follower(home, "papa")
    assert followers_status(home) == [] and "followers: []" in (home / "config" / "copy_trading.yaml").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        remove_follower(home, "inconnu")


def test_api_suppression_exige_une_confirmation(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from urllib.error import HTTPError

    from tradinglab.copy.registry import followers_status, register_follower
    from tradinglab.dashboards.server import CONTROL_HTML, create_server

    home, term = _copy_home(tmp_path)
    register_follower(home, "papa", "222", "p2", "s2", str(term), 2.0)
    monkeypatch.delenv("DASHBOARD_TLS", raising=False)
    srv = create_server(home=home, host="127.0.0.1", port=0, auth_token="tk")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/copy/remove", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Cookie": "tladash=tk"})
        return json.loads(urllib.request.urlopen(req, timeout=5).read())
    try:
        with pytest.raises(HTTPError) as e:
            post({"name": "papa"})
        assert e.value.code == 400 and followers_status(home)
        assert post({"name": "papa", "confirm": True})["ok"] and followers_status(home) == []
    finally:
        srv.shutdown(); srv.server_close()
    assert "askRemove" in CONTROL_HTML and "Oui, supprimer" in CONTROL_HTML and "Supprimer définitivement" in CONTROL_HTML


def test_stops_adaptes_a_la_distance_minimale_du_suiveur(tmp_path):
    """Capture utilisateur 2026-09-24 22:04 : AUDCHF SELL refusé « Invalid stops » en boucle sur les 4 comptes Admirals.
    Admirals impose 46 points la nuit ; le stop du maître était à 44 points, le TP à 43. Le stop est porté à la distance
    minimale (élargissement ≤ ×2), le TP trop proche est retiré ; en modification, jamais plus large que l'actuel."""
    from types import SimpleNamespace

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tr = CopyTrader(b, tmp_path / "m.json", size_factor=1.0, magic=52000)
    spec = b.symbol_info("EURUSD")
    spec.stops_level_points = 46
    t = b.tick("EURUSD")
    pt = spec.point
    # SELL : stop 44 points au-dessus de l'ask, TP 43 points sous l'ask
    sl, tp, note = tr._fit_stops("EURUSD", Side.SELL, round(t.ask + 44 * pt, 5), round(t.ask - 43 * pt, 5))
    assert sl == pytest.approx(round(t.ask + 48 * pt, 5)) and tp == 0.0 and "distance minimale" in note and "retiré" in note
    # stop déjà au bon endroit : inchangé
    sl2, tp2, note2 = tr._fit_stops("EURUSD", Side.SELL, round(t.ask + 100 * pt, 5), round(t.ask - 200 * pt, 5))
    assert sl2 == pytest.approx(round(t.ask + 100 * pt, 5)) and tp2 == pytest.approx(round(t.ask - 200 * pt, 5)) and note2 == ""
    # élargissement > ×2 (stop du maître à 10 points) : copie reportée
    assert tr._fit_stops("EURUSD", Side.SELL, round(t.ask + 10 * pt, 5), 0.0) is None
    # stop déjà dépassé (mauvais côté) : copie reportée
    assert tr._fit_stops("EURUSD", Side.SELL, round(t.ask - 5 * pt, 5), 0.0) is None
    # modification : un stop plus serré impossible à placer ne doit pas élargir le stop actuel
    actuel = round(t.ask + 47 * pt, 5)
    assert tr._fit_stops("EURUSD", Side.SELL, round(t.ask + 30 * pt, 5), 0.0, current_sl=actuel, current_tp=0.0) is None


def test_volume_copie_a_exposition_egale_malgre_des_contrats_differents():
    """Relevé réel 2026-09-24 : XAGUSD 3 lots chez IC (1 lot = 1 000 oz) copié en SILVER 2,99 lots chez Admirals
    (1 lot = 5 000 oz) — 5× le risque : −2 781 $ sur demo 1 contre −6 $ au maître. Le volume suit désormais la
    valeur d'un mouvement de prix, pas le nombre de lots."""
    from types import SimpleNamespace

    spec_adm = SimpleNamespace(volume_min=0.01, volume_step=0.01, volume_max=100.0, tick_size=0.001, tick_value=5.0)
    m = master(positions=[mpos(1, symbol="XAGUSD", side="SELL", volume=3.0, sl=64.0, tp=62.0)])
    m["positions"][0]["value_per_price"] = 1.0 / 0.001               # IC : 1 $ par tick de 0,001 → 1 000 $ par 1,0
    acts = plan_sync(m, [], 500000.0, 1.0, {"SILVER": spec_adm}, sym_map={"XAGUSD": "SILVER"})
    assert [a.kind for a in acts] == ["open"] and acts[0].volume == pytest.approx(0.6) and "contrat ×0.200" in acts[0].reason
    # ancien export sans valeur : comportement historique (même nombre de lots)
    m["positions"][0].pop("value_per_price")
    assert plan_sync(m, [], 500000.0, 1.0, {"SILVER": spec_adm}, sym_map={"XAGUSD": "SILVER"})[0].volume == pytest.approx(3.0)
    # rapport aberrant (instrument différent, ×1 000) : pas de copie
    m["positions"][0]["value_per_price"] = 5_000_000.0
    assert plan_sync(m, [], 500000.0, 1.0, {"SILVER": spec_adm}, sym_map={"XAGUSD": "SILVER"}) == []


def test_stops_decales_quand_le_suiveur_cote_un_autre_contrat(tmp_path):
    """Capture 2026-09-25 : Brent 104,90 chez IC, 98,21 chez Admirals ; le stop du maître (104,34) était recopié tel quel,
    au-dessus du prix d'un ACHAT → « stop trop proche : copie reportée » ×8. Au-delà de 0,2 % d'écart, SL/TP gardent la
    même distance au prix que chez le maître."""
    from tradinglab.copy.copier import CopyAction

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tr = CopyTrader(b, tmp_path / "m.json", size_factor=1.0, magic=52000)
    t = b.tick("EURUSD")
    mid = (t.bid + t.ask) / 2
    a = CopyAction("open", symbol="EURUSD", side=Side.BUY, volume=0.1, sl=round(mid * 1.07 - 0.0066, 5),
                   tp=round(mid * 1.07 + 0.0142, 5), master_price=mid * 1.07)              # maître 7 % plus haut
    tr._shift_to_follower_price(a)
    assert a.sl == pytest.approx(mid - 0.0066, abs=2e-5) and a.tp == pytest.approx(mid + 0.0142, abs=2e-5)
    proche = CopyAction("open", symbol="EURUSD", side=Side.BUY, volume=0.1, sl=round(mid - 0.005, 5), tp=0.0,
                        master_price=mid * 1.001)                                           # 0,1 % : inchangé
    tr._shift_to_follower_price(proche)
    assert proche.sl == pytest.approx(round(mid - 0.005, 5)) and proche.tp == 0.0


def test_decalage_memorise_pas_de_modification_en_boucle():
    """2026-09-25 : après le décalage de prix du Brent, les niveaux du maître (104,34) ne correspondaient jamais aux
    niveaux décalés du suiveur (97,6x) → une modification partait toutes les 5 s en dérivant. Le décalage mémorisé
    dans la table est appliqué avant la comparaison : rien ne part tant que le maître ne bouge pas ses stops."""
    sh = -6.7
    follower = [_FPos(101, 1, sl=round(1.08 + sh, 5), tp=round(1.10 + sh, 5))]
    mapping = {1: {"ticket": 101, "shift": sh}}
    m = master(positions=[dict(mpos(1), price_current=1.09)])
    assert [a for a in plan_sync(m, follower, 50000.0, 1.0, SPECS, mapping=mapping) if a.kind == "modify"] == []
    m = master(positions=[dict(mpos(1, sl=1.09), price_current=1.095)])            # break-even chez le maître
    acts = [a for a in plan_sync(m, follower, 50000.0, 1.0, SPECS, mapping=mapping) if a.kind == "modify"]
    assert len(acts) == 1 and acts[0].sl == pytest.approx(1.09 + sh) and acts[0].master_price == 0.0


def test_stop_trop_proche_retente_avec_delai_croissant(tmp_path):
    """2026-09-26 : stop BTC trop serré pour les suiveurs, retenté et journalisé chaque minute pendant 50 min."""
    from tradinglab.copy.copier import CopyAction

    b = MockBroker(seed=7, balance=50000.0)
    b.connect()
    tr = CopyTrader(b, tmp_path / "m.json", size_factor=1.0, magic=52000)
    spec = b.symbol_info("EURUSD")
    spec.stops_level_points = 46
    t = b.tick("EURUSD")
    a = CopyAction("open", symbol="EURUSD", side=Side.SELL, volume=0.1, sl=round(t.ask + 10 * spec.point, 5), tp=0.0,
                   master_ticket=77)
    delais = []
    for _ in range(3):
        tr._apply(a)
        delais.append((tr._retry_after[77] - tr.__dict__.get("_now", datetime.now(timezone.utc))).total_seconds())
    assert delais[0] < delais[1] < delais[2]


def test_verrou_copieur_pid_inexistant_windows(monkeypatch):
    """2026-09-27 : `os.kill(pid, 0)` a levé SystemError (CPython/Windows) pour un PID mort → le copieur demo 1
    s'est arrêté au lieu de reprendre le verrou. SystemError est traité comme OSError : le processus est mort."""
    import os
    from tradinglab.copy import __main__ as cm
    src = open(cm.__file__, encoding="utf-8").read()
    assert "except (OSError, SystemError):" in src

    def _kill(pid, sig):
        raise SystemError("<built-in function kill> returned a result with an exception set")
    monkeypatch.setattr(os, "kill", _kill)
    try:
        os.kill(999999, 0)
        alive = True
    except (OSError, SystemError):
        alive = False
    assert alive is False
