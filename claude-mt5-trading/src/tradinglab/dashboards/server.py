"""Dashboard local en LECTURE SEULE (http.server, stdlib uniquement).

Principes :
- le dashboard ne participe JAMAIS à la sécurité du système : il ne peut ni envoyer
  d'ordres, ni modifier l'état, ni pousser de commandes ; il se contente de lire
  ``state/system_state.json``, le journal du jour, ``data/agent_stats.json`` et
  ``reports/leaderboard.json`` ;
- aucun secret n'est affiché : le fichier ``.env`` n'est jamais lu, et les clés de
  configuration ressemblant à un secret sont retirées avant envoi ;
- une valeur absente est présentée comme "UNKNOWN / UNAVAILABLE", jamais inventée.

Routes :
- ``/``                         page HTML auto-rafraîchie (fetch /api/state toutes les 5 s) ;
- ``/api/state``                état public + prop + risk + journal_tail (``?kinds=order,gate``) ;
- ``/api/journal?day=YYYY-MM-DD`` journal complet du jour (par défaut aujourd'hui, UTC).

Usage : ``python -m tradinglab.dashboards.server --port 8765 --host 127.0.0.1 --home /chemin``.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from ..core.config import CONFIG_FILES, Settings, project_home
from ..core.state import StateStore
from ..core.types import utcnow
from ..monitoring.watchdog import read_watchdog_report

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
JOURNAL_TAIL = 50
HEARTBEAT_WARN_SEC = 45
# Fenêtre initiale (octets) lue en fin de journal pour /api/state ; doublée tant que < JOURNAL_TAIL événements.
JOURNAL_TAIL_WINDOW_BYTES = 512_000
# Champs du rapport watchdog exposés au dashboard (lecture directe de state/watchdog.json).
WATCHDOG_FIELDS = ("safe_mode_request", "reasons", "actions", "positions_without_sl", "data_fresh", "orchestrator_alive")

# Clés de configuration jamais exposées (même logique que core.journal).
_SECRET_KEY = re.compile(r"(password|passwd|secret|api[_-]?key|token|credential)", re.IGNORECASE)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------------
# Chargement des données (lecture seule)
# ----------------------------------------------------------------------------
def load_public_settings(home: Path) -> Settings:
    """Charge les YAML de ``config/`` SANS lire le fichier ``.env``.

    Réplique volontairement la fusion de ``load_settings`` afin que le dashboard
    n'ait jamais accès aux credentials (il n'en a pas besoin).
    """
    raw: dict = {}
    cfg_dir = home / "config"
    for name in CONFIG_FILES:
        f = cfg_dir / f"{name}.yaml"
        if not f.exists():
            continue
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(raw.get(k), dict):
                raw[k].update(v)
            else:
                raw[k] = v
    return Settings(home=home, raw=raw)


def scrub_secrets(obj: Any) -> Any:
    """Retire récursivement toute clé ressemblant à un secret."""
    if isinstance(obj, dict):
        return {k: scrub_secrets(v) for k, v in obj.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(obj, list):
        return [scrub_secrets(v) for v in obj]
    return obj


def _json_safe(obj: Any) -> Any:
    """Rend un objet sérialisable en JSON strict (inf/nan -> None, sets -> listes…)."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return None
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def read_journal_day(logs_dir: Path, day: str | None = None, kinds: set[str] | None = None) -> list[dict]:
    """Lit ``logs/journal-YYYY-MM-DD.jsonl`` (lignes invalides ignorées)."""
    day = day or utcnow().strftime("%Y-%m-%d")
    if not _DAY_RE.match(day):
        return []
    f = Path(logs_dir) / f"journal-{day}.jsonl"
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if kinds is None or rec.get("kind") in kinds:
            out.append(rec)
    return out


def _parse_journal_lines(lines: list[bytes], kinds: set[str] | None) -> list[dict]:
    out: list[dict] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if kinds is None or rec.get("kind") in kinds:
            out.append(rec)
    return out


def read_journal_tail(logs_dir: Path, limit: int = JOURNAL_TAIL, kinds: set[str] | None = None,
                      day: str | None = None, window: int = JOURNAL_TAIL_WINDOW_BYTES) -> list[dict]:
    """Derniers ``limit`` événements du journal du jour SANS relire tout le fichier.

    Le journal atteint plusieurs dizaines de Mo en journée : on lit seulement une fenêtre en fin
    de fichier (première ligne partielle ignorée), élargie (x2) tant que moins de ``limit``
    événements filtrés sont trouvés et que le début du fichier n'est pas atteint.
    """
    day = day or utcnow().strftime("%Y-%m-%d")
    if not _DAY_RE.match(day) or limit <= 0:
        return []
    f = Path(logs_dir) / f"journal-{day}.jsonl"
    try:
        size = f.stat().st_size
    except OSError:
        return []
    window = max(1, int(window))
    while True:
        start = max(0, size - window)
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # ligne partielle (coupée par le seek)
        events = _parse_journal_lines(lines, kinds)
        if len(events) >= limit or start == 0:
            return events[-limit:]
        window *= 2


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _heartbeat_age(ts: Any) -> float | None:
    """Âge (s) d'un heartbeat ISO 8601 ; None si absent/invalide (UNKNOWN, jamais inventé)."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        hb = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if hb.tzinfo is None:
        return None
    return (utcnow() - hb).total_seconds()


class DashboardData:
    """Assemble les données exposées par ``/api/state`` (jamais d'écriture)."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.settings = load_public_settings(self.home)
        self.store = StateStore(self.settings.state_dir)

    def snapshot(self, kinds: set[str] | None = None) -> dict:
        state = self.store.reload()
        d = state.public_dict()
        # Les clés d'idempotence sont internes et volumineuses : inutiles au dashboard.
        d.pop("executed_keys", None)
        d["prop"] = scrub_secrets(self.settings.prop)
        d["prop"]["autonomous_prop"] = self.settings.autonomous_prop
        d["risk"] = scrub_secrets(self.settings.risk)
        d["system"] = scrub_secrets(
            {k: v for k, v in self.settings.system.items() if k in ("autonomous_demo", "autonomous_prop", "heartbeat_max_age_sec", "magic_number")}
        )
        d["journal_tail"] = read_journal_tail(self.settings.logs_dir, JOURNAL_TAIL, kinds=kinds)
        d["journal_day"] = utcnow().strftime("%Y-%m-%d")
        d["server_time_utc"] = utcnow().isoformat()
        d["orchestrator_heartbeat_age_sec"] = self.store.heartbeat_age("orchestrator")
        # Heartbeat watchdog lu DIRECTEMENT dans state/watchdog.json (et non via la copie faite par
        # l'orchestrateur) : orchestrateur mort ≠ watchdog mort, l'opérateur doit pouvoir distinguer.
        wd = read_watchdog_report(self.settings.state_dir) or {}
        d["watchdog_heartbeat_age_sec"] = _heartbeat_age(wd.get("heartbeat"))
        d["watchdog"] = {k: wd.get(k) for k in WATCHDOG_FIELDS}
        d["heartbeat_warn_sec"] = int(self.settings.system.get("heartbeat_max_age_sec", HEARTBEAT_WARN_SEC))
        d["model_usage"] = d.get("model_budget", {})
        d["learning"] = _read_json_file(self.home / "data" / "agent_stats.json", {})
        d["leaderboard"] = _read_json_file(self.home / "reports" / "leaderboard.json", [])
        return _json_safe(scrub_secrets(d))

    def journal(self, day: str | None) -> list[dict]:
        return _json_safe(read_journal_day(self.settings.logs_dir, day))


# ----------------------------------------------------------------------------
# Serveur HTTP
# ----------------------------------------------------------------------------
class DashboardServer(ThreadingHTTPServer):
    """ThreadingHTTPServer portant la source de données (lecture seule)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], data: DashboardData):
        self.data = data
        super().__init__(server_address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TradingLabDashboard/1.0"
    server: DashboardServer  # type: ignore[assignment]

    # -- utilitaires --
    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature imposée
        # Silencieux par défaut : le dashboard ne doit pas polluer les logs du système.
        pass

    # -- routes (GET uniquement : aucune écriture possible) --
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        try:
            if parts.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif parts.path == "/api/state":
                kinds_raw = ",".join(query.get("kinds", []))
                kinds = {k.strip() for k in kinds_raw.split(",") if k.strip()} or None
                self._send_json(self.server.data.snapshot(kinds))
            elif parts.path == "/api/journal":
                day = (query.get("day") or [None])[0]
                if day and not _DAY_RE.match(day):
                    self._send_json({"error": "paramètre day invalide (YYYY-MM-DD attendu)"}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(self.server.data.journal(day))
            elif parts.path == "/health":
                self._send_json({"ok": True, "read_only": True, "server_time_utc": utcnow().isoformat()})
            else:
                self._send_json({"error": "route inconnue"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - défense : jamais de trace de pile vers le client
            self._send_json({"error": f"erreur interne: {type(exc).__name__}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _refuse(self) -> None:
        self._send_json({"error": "dashboard en lecture seule : méthode refusée"}, HTTPStatus.METHOD_NOT_ALLOWED)

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815


def create_server(home: Path | None = None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Construit le serveur (non démarré). ``port=0`` choisit un port libre."""
    home = Path(home) if home else project_home()
    return DashboardServer((host, int(port)), DashboardData(home))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboard local en lecture seule (Claude MT5 Trading Lab).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port d'écoute (défaut {DEFAULT_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"hôte d'écoute (défaut {DEFAULT_HOST})")
    parser.add_argument("--home", type=Path, default=None, help="racine du projet (défaut : TRADINGLAB_HOME ou le dépôt)")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"AVERTISSEMENT : hôte {args.host} non local ; le dashboard n'est pas authentifié.", file=sys.stderr)
    server = create_server(args.home, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Dashboard (lecture seule) : http://{host}:{port}/  — Ctrl+C pour arrêter", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ----------------------------------------------------------------------------
# Page HTML (JS vanilla, CSS inline, thème sombre, responsive)
# ----------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude MT5 Trading Lab — Dashboard (lecture seule)</title>
<style>
:root{--bg:#0f1216;--card:#171b21;--line:#262c35;--fg:#e6e9ee;--muted:#8a93a3;--ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;--info:#3498db;--purple:#9b59b6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:#12161b;position:sticky;top:0;z-index:2}
header h1{font-size:16px;margin:0;font-weight:600}
header .meta{color:var(--muted);font-size:12px}
main{padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px}
.card.wide{grid-column:1/-1}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-variant-numeric:tabular-nums}
.kv dt{color:var(--muted)}
.kv dd{margin:0;text-align:right;word-break:break-word}
.big{font-size:22px;font-weight:700}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:12px;color:#000;background:var(--muted)}
.badge.ok{background:var(--ok)}.badge.warn{background:var(--warn)}.badge.bad{background:var(--bad);color:#fff}.badge.info{background:var(--info);color:#fff}.badge.purple{background:var(--purple);color:#fff}
.huge{font-size:28px;padding:6px 14px;letter-spacing:.1em}
.pos{color:var(--ok)}.neg{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
th,td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}
.scroll{overflow:auto;max-height:360px}
.reasons{color:var(--muted);font-size:12px;margin-top:4px}
.journal{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;max-height:420px;overflow:auto}
.journal div{padding:2px 0;border-bottom:1px solid var(--line);white-space:pre-wrap;word-break:break-word}
.journal .k{color:var(--info);font-weight:700}
.journal .t{color:var(--muted)}
.unknown{color:var(--muted);font-style:italic}
#error{display:none;background:var(--bad);color:#fff;padding:6px 10px;border-radius:4px}
.chips span{display:inline-block;background:#232932;border:1px solid var(--line);border-radius:4px;padding:2px 6px;margin:2px;font-size:12px}
@media (max-width:640px){main{grid-template-columns:1fr;padding:12px}.huge{font-size:22px}}
</style>
</head>
<body>
<header>
  <h1>Claude MT5 Trading Lab — Dashboard (lecture seule)</h1>
  <span class="meta">Aucune action possible depuis cette page. Rafraîchissement toutes les 5 s. Heure serveur (UTC) : <span id="server_time">—</span></span>
  <span id="error"></span>
</header>
<main>
  <section class="card"><h2>Compte &amp; MT5</h2>
    <div style="margin-bottom:8px"><span id="trade_mode" class="badge huge">UNKNOWN</span></div>
    <dl class="kv">
      <dt>MT5</dt><dd id="mt5"></dd>
      <dt>Login</dt><dd id="login"></dd>
      <dt>Serveur</dt><dd id="server"></dd>
      <dt>Devise</dt><dd id="currency"></dd>
    </dl>
  </section>
  <section class="card"><h2>Mode système</h2>
    <div><span id="mode" class="badge huge">UNKNOWN</span></div>
    <div class="reasons" id="mode_reasons"></div>
    <div style="margin-top:8px"><span id="locked" class="badge">UNKNOWN</span></div>
    <div class="reasons" id="lock_reasons"></div>
  </section>
  <section class="card"><h2>Equity &amp; P&amp;L</h2>
    <dl class="kv">
      <dt>Equity</dt><dd id="equity" class="big"></dd>
      <dt>Balance</dt><dd id="balance"></dd>
      <dt>P&amp;L du jour</dt><dd id="daily_pnl"></dd>
      <dt>Drawdown jour</dt><dd id="dd_day"></dd>
      <dt>Drawdown global</dt><dd id="dd_all"></dd>
      <dt>Risque ouvert</dt><dd id="open_risk"></dd>
      <dt>Pertes consécutives</dt><dd id="consec"></dd>
      <dt>Trades clos (jour)</dt><dd id="trades_closed"></dd>
    </dl>
  </section>
  <section class="card"><h2>Limites prop</h2>
    <dl class="kv">
      <dt>Profil</dt><dd id="prop_firm"></dd>
      <dt>Programme</dt><dd id="prop_program"></dd>
      <dt>Perte jour max (hard)</dt><dd id="prop_daily"></dd>
      <dt>Perte globale max (hard)</dt><dd id="prop_overall"></dd>
      <dt>Objectif profit</dt><dd id="prop_target"></dd>
      <dt>PROP_RULES_VERIFIED</dt><dd id="prop_verified"></dd>
      <dt>AUTONOMOUS_PROP</dt><dd id="prop_auto"></dd>
      <dt>Risque / trade (interne)</dt><dd id="risk_per_trade"></dd>
      <dt>Perte jour interne</dt><dd id="risk_daily"></dd>
      <dt>Positions max</dt><dd id="risk_maxpos"></dd>
    </dl>
  </section>
  <section class="card"><h2>News &amp; heartbeats</h2>
    <dl class="kv">
      <dt>News</dt><dd id="news"></dd>
      <dt>Calendrier</dt><dd id="calendar"></dd>
      <dt>Heartbeat orchestrateur</dt><dd id="hb_orch"></dd>
      <dt>Heartbeat watchdog</dt><dd id="hb_wd"></dd>
      <dt>Watchdog : SAFE_MODE demandé</dt><dd id="wd_safe"></dd>
      <dt>Watchdog : données fraîches</dt><dd id="wd_fresh"></dd>
      <dt>Watchdog : positions sans SL</dt><dd id="wd_nosl"></dd>
      <dt>Watchdog : raisons</dt><dd id="wd_reasons"></dd>
      <dt>Watchdog : actions</dt><dd id="wd_actions"></dd>
      <dt>Redémarrages</dt><dd id="restarts"></dd>
      <dt>Démarré à</dt><dd id="started_at"></dd>
    </dl>
  </section>
  <section class="card"><h2>Régimes par symbole</h2><div class="chips" id="regimes"></div></section>
  <section class="card"><h2>Agents actifs</h2><div class="chips" id="agents"></div></section>
  <section class="card"><h2>Usage modèles</h2>
    <dl class="kv">
      <dt>Dépensé (USD, jour)</dt><dd id="spent"></dd>
      <dt>Jour</dt><dd id="budget_day"></dd>
    </dl>
    <div class="scroll"><table><thead><tr><th>Tier</th><th>Appels (heure)</th><th>Appels (jour)</th></tr></thead><tbody id="tiers"></tbody></table></div>
  </section>
  <section class="card wide"><h2>Top setups</h2>
    <div class="scroll"><table><thead><tr><th>Symbole</th><th>Sens</th><th>Score</th><th>Agent</th><th>Verdict</th></tr></thead><tbody id="setups"></tbody></table></div>
  </section>
  <section class="card wide"><h2>Positions du bot</h2>
    <div class="scroll"><table><thead><tr><th>Ticket</th><th>Symbole</th><th>Sens</th><th>Entrée</th><th>SL initial</th><th>SL actuel</th><th>Max R</th><th>TP1</th><th>TP2</th><th>BE</th></tr></thead><tbody id="positions"></tbody></table></div>
  </section>
  <section class="card wide"><h2>Learning / leaderboard</h2>
    <div class="scroll"><table><thead><tr><th>Agent</th><th>Statut</th><th>Trades</th><th>Profit factor</th><th>Expectancy (R)</th></tr></thead><tbody id="leaderboard"></tbody></table></div>
  </section>
  <section class="card wide"><h2>Journal du jour (50 derniers événements)</h2><div class="journal" id="journal"></div></section>
</main>
<script>
(function(){
  var UNK = 'UNKNOWN / UNAVAILABLE';
  function $(id){ return document.getElementById(id); }
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function missing(v){ return v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v)); }
  function txt(v){ return missing(v) ? '<span class="unknown">'+UNK+'</span>' : esc(v); }
  function num(v, d){ return missing(v) || typeof v !== 'number' ? txt(v) : esc(v.toFixed(d === undefined ? 2 : d)); }
  function pct(v){ return missing(v) || typeof v !== 'number' ? txt(v) : esc(v.toFixed(2)+' %'); }
  function bool(v){ return missing(v) ? txt(v) : (v ? 'oui' : 'non'); }
  function set(id, html){ $(id).innerHTML = html; }
  function badge(id, text, cls){ var e = $(id); e.textContent = text; e.className = e.className.replace(/\b(ok|warn|bad|info|purple)\b/g,'').trim() + (cls ? ' ' + cls : ''); }
  function signed(v, suffix){ if (missing(v) || typeof v !== 'number') return txt(v); var c = v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); return '<span class="'+c+'">'+esc((v > 0 ? '+' : '')+v.toFixed(2)+(suffix||''))+'</span>'; }
  function hb(v, warn){ if (missing(v)) return '<span class="badge bad">'+UNK+'</span>'; var cls = v > warn ? 'bad' : 'ok'; return '<span class="badge '+cls+'">'+esc(v.toFixed(1))+' s</span>'; }
  function rows(id, list, fn, cols){ if (!list || !list.length){ set(id, '<tr><td colspan="'+cols+'" class="unknown">'+UNK+'</td></tr>'); return; } set(id, list.map(fn).join('')); }

  function render(s){
    $('server_time').textContent = s.server_time_utc || UNK;
    var tm = s.account_trade_mode || 'UNKNOWN';
    badge('trade_mode', tm, tm === 'DEMO' ? 'ok' : (tm === 'REAL' ? 'bad' : (tm === 'CONTEST' ? 'warn' : '')));
    set('mt5', s.mt5_connected === true ? '<span class="badge ok">CONNECTÉ</span>' : (s.mt5_connected === false ? '<span class="badge bad">NON CONNECTÉ</span>' : txt(null)));
    set('login', s.account_login ? txt(s.account_login) : txt(null));
    set('server', txt(s.account_server));
    set('currency', txt(s.currency));

    var mode = s.mode || 'UNKNOWN';
    badge('mode', mode, {SAFE_MODE:'warn', AUTO:'ok', PAUSED:'info', PANIC:'bad'}[mode] || '');
    set('mode_reasons', (s.mode_reasons && s.mode_reasons.length) ? esc(s.mode_reasons.join(' · ')) : '');
    if (missing(s.new_trades_locked)) badge('locked', 'NEW_TRADES_LOCKED : ' + UNK, '');
    else badge('locked', s.new_trades_locked ? 'NEW_TRADES_LOCKED' : 'Nouvelles entrées autorisées', s.new_trades_locked ? 'bad' : 'ok');
    set('lock_reasons', (s.lock_reasons && s.lock_reasons.length) ? esc(s.lock_reasons.join(' · ')) : '');

    var cur = s.currency ? ' ' + s.currency : '';
    set('equity', num(s.equity) + esc(cur));
    set('balance', num(s.balance) + esc(cur));
    set('daily_pnl', signed(s.daily_pnl, cur) + ' (' + signed(s.daily_pnl_percent, ' %') + ')');
    set('dd_day', pct(s.daily_drawdown_percent));
    set('dd_all', pct(s.overall_drawdown_percent));
    set('open_risk', pct(s.open_risk_percent));
    set('consec', txt(s.consecutive_losses));
    set('trades_closed', txt(s.daily && s.daily.trades_closed));

    var p = s.prop || {}, r = s.risk || {};
    set('prop_firm', txt(p.prop_firm));
    set('prop_program', txt(p.program));
    set('prop_daily', pct(p.max_daily_loss_hard_percent));
    set('prop_overall', pct(p.max_overall_loss_hard_percent));
    set('prop_target', pct(p.profit_target_percent));
    set('prop_verified', missing(p.prop_rules_verified) ? txt(null) : '<span class="badge '+(p.prop_rules_verified ? 'ok' : 'warn')+'">'+(p.prop_rules_verified ? 'VÉRIFIÉES' : 'NON VÉRIFIÉES')+'</span>');
    set('prop_auto', missing(p.autonomous_prop) ? txt(null) : '<span class="badge '+(p.autonomous_prop ? 'bad' : 'ok')+'">'+(p.autonomous_prop ? 'ACTIF' : 'INACTIF')+'</span>');
    set('risk_per_trade', pct(r.risk_per_trade_percent));
    set('risk_daily', pct(r.max_daily_loss_internal_percent));
    set('risk_maxpos', txt(r.max_open_positions));

    set('news', missing(s.news_data_degraded) ? txt(null) : '<span class="badge '+(s.news_data_degraded ? 'warn' : 'ok')+'">'+(s.news_data_degraded ? 'DEGRADED' : 'OK')+'</span>');
    set('calendar', missing(s.calendar_data_degraded) ? txt(null) : '<span class="badge '+(s.calendar_data_degraded ? 'warn' : 'ok')+'">'+(s.calendar_data_degraded ? 'DEGRADED' : 'OK')+'</span>');
    var warn = typeof s.heartbeat_warn_sec === 'number' ? s.heartbeat_warn_sec : 45;
    set('hb_orch', hb(s.orchestrator_heartbeat_age_sec, warn));
    set('hb_wd', hb(s.watchdog_heartbeat_age_sec, warn));
    var wd = s.watchdog || {};
    set('wd_safe', missing(wd.safe_mode_request) ? txt(null) : '<span class="badge '+(wd.safe_mode_request ? 'bad' : 'ok')+'">'+(wd.safe_mode_request ? 'OUI' : 'NON')+'</span>');
    set('wd_fresh', missing(wd.data_fresh) ? txt(null) : '<span class="badge '+(wd.data_fresh ? 'ok' : 'bad')+'">'+(wd.data_fresh ? 'OUI' : 'PÉRIMÉES')+'</span>');
    set('wd_nosl', txt(wd.positions_without_sl));
    set('wd_reasons', (wd.reasons && wd.reasons.length) ? wd.reasons.map(esc).join('<br>') : txt(null));
    set('wd_actions', (wd.actions && wd.actions.length) ? wd.actions.map(esc).join('<br>') : txt(null));
    set('restarts', txt(s.restarts));
    set('started_at', txt(s.started_at));

    var reg = s.regimes || {}, rk = Object.keys(reg);
    set('regimes', rk.length ? rk.map(function(k){ return '<span>'+esc(k)+' : '+esc(reg[k])+'</span>'; }).join('') : txt(null));
    set('agents', (s.active_agents && s.active_agents.length) ? s.active_agents.map(function(a){ return '<span>'+esc(a)+'</span>'; }).join('') : txt(null));

    var mu = s.model_usage || {};
    set('spent', num(mu.spent_usd, 4));
    set('budget_day', txt(mu.day));
    var th = mu.calls_by_tier_hour || {}, td = mu.calls_by_tier_day || {};
    var tiers = {}; Object.keys(th).forEach(function(k){ tiers[k] = 1; }); Object.keys(td).forEach(function(k){ tiers[k] = 1; });
    rows('tiers', Object.keys(tiers).sort(), function(t){ return '<tr><td>'+esc(t)+'</td><td>'+txt(th[t])+'</td><td>'+txt(td[t])+'</td></tr>'; }, 3);

    rows('setups', s.top_setups, function(x){ x = x || {}; return '<tr><td>'+txt(x.symbol)+'</td><td>'+txt(x.side)+'</td><td>'+txt(x.setup_score)+'</td><td>'+txt(x.agent_id)+'</td><td>'+txt(x.verdict)+'</td></tr>'; }, 5);

    var bp = s.bot_positions || {};
    rows('positions', Object.keys(bp).map(function(k){ return bp[k]; }), function(x){ x = x || {}; return '<tr><td>'+txt(x.ticket)+'</td><td>'+txt(x.symbol)+'</td><td>'+txt(x.side)+'</td><td>'+num(x.entry, 5)+'</td><td>'+num(x.initial_sl, 5)+'</td><td>'+num(x.last_sl, 5)+'</td><td>'+num(x.max_r)+'</td><td>'+bool(x.tp1_done)+'</td><td>'+bool(x.tp2_done)+'</td><td>'+bool(x.break_even_done)+'</td></tr>'; }, 10);

    var lb = s.leaderboard; if (!Array.isArray(lb)) lb = [];
    if (!lb.length && s.learning && typeof s.learning === 'object') {
      lb = Object.keys(s.learning).map(function(k){ var v = s.learning[k] || {}; return Object.assign({agent_id: v.agent_id || k}, v); });
    }
    rows('leaderboard', lb, function(x){ x = x || {}; return '<tr><td>'+txt(x.agent_id)+'</td><td>'+txt(x.status)+'</td><td>'+txt(x.trades)+'</td><td>'+num(x.profit_factor)+'</td><td>'+num(x.expectancy_r)+'</td></tr>'; }, 5);

    var j = s.journal_tail || [];
    if (!j.length) set('journal', '<div class="unknown">'+UNK+'</div>');
    else set('journal', j.slice().reverse().map(function(e){
      var t = (e.ts_utc || '').slice(11, 19);
      var summary = e.message || e.summary || e.reason || '';
      if (!summary) { var o = {}; Object.keys(e).forEach(function(k){ if (['ts_utc','ts_local','component','kind'].indexOf(k) < 0) o[k] = e[k]; }); summary = JSON.stringify(o); }
      if (summary.length > 240) summary = summary.slice(0, 240) + '…';
      return '<div><span class="t">'+esc(t)+'</span> <span class="k">'+esc(e.kind || '?')+'</span> '+esc(summary)+'</div>';
    }).join(''));
  }

  function refresh(){
    fetch('/api/state', {cache: 'no-store'}).then(function(r){ if (!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(s){ $('error').style.display = 'none'; render(s); })
      .catch(function(e){ var el = $('error'); el.style.display = 'inline-block'; el.textContent = 'Données indisponibles : ' + e.message; });
  }
  refresh();
  setInterval(refresh, 5000);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
