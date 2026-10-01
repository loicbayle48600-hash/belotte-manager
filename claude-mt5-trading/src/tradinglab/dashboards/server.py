"""Dashboard local (http.server, stdlib uniquement) : lecture de l'état + panneau de contrôle.

Principes :
- le dashboard ne participe JAMAIS à la sécurité du système : il ne modifie ni l'état ni le broker ;
  les seules écritures possibles passent par la file officielle de commandes (``state/commands.jsonl``,
  mêmes commandes que la CLI, toutes journalisées) et par le registre des comptes suiveurs ;
- il lit ``state/system_state.json`` en LECTURE SEULE (``StateStore(readonly=True)`` : une lecture
  déchirée pendant l'écriture atomique de l'orchestrateur garde le dernier état connu, jamais de
  fichier mis de côté), le journal du jour, ``data/learning.db``, ``data/agent_stats.json`` et
  ``reports/leaderboard.json`` ;
- aucun secret n'est affiché : le fichier ``.env`` n'est jamais lu, et les clés de configuration
  ressemblant à un secret sont retirées avant envoi ;
- une valeur absente est présentée comme "UNKNOWN / UNAVAILABLE", jamais inventée.

Routes (GET) :
- ``/``, ``/control``, ``/stats``   pages HTML (JS vanilla, aucune ressource externe) ;
- ``/login``, ``/logout``          formulaire de session (cookie) et effacement du cookie ;
- ``/api/state``                   état public + prop + risk + journal_tail projeté (``?kinds=order,gate``
                                   → 100 derniers événements filtrés) ;
- ``/api/journal``                 journal du jour : ``?day=YYYY-MM-DD&kinds=a,b&limit=500&offset=0``
                                   (limite 5 000 événements par réponse) ;
- ``/api/stats``                   statistiques agrégées (périodes, agents, paires, classes d'actifs,
                                   courbe d'equity, planchers, cycle de paiement, comptes suiveurs) ;
- ``/api/copy/status``, ``/health`` (``read_only``, ``auth``, ``tls``).

Routes (POST, ``Content-Type: application/json`` exigé sous ``/api/``) :
- ``/api/command`` (whitelist ``PANEL_COMMANDS``), ``/api/restart``, ``/api/copy/follower``,
  ``/api/copy/factor`` ; ``/login`` (formulaire).

Sécurité : jeton ``DASHBOARD_AUTH_TOKEN`` (Basic / Bearer / cookie), ``DASHBOARD_TLS=1`` (HTTPS auto-signé),
en-têtes CSP / X-Frame-Options / Referrer-Policy / HSTS, 5 échecs d'authentification par IP → 60 s
d'attente, réponses gzip si le client l'accepte.

Usage : ``python -m tradinglab.dashboards.server --port 8765 --host 127.0.0.1 --home /chemin``.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hmac
import json
import os
import math
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
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
# Fenêtre d'événements quand /api/state est appelé avec ``kinds`` (journal humain de l'accueil).
JOURNAL_TAIL_FILTERED = 100
HEARTBEAT_WARN_SEC = 45
# Fenêtre initiale (octets) lue en fin de journal pour /api/state ; doublée tant que < JOURNAL_TAIL événements.
JOURNAL_TAIL_WINDOW_BYTES = 512_000
# Plafond de la lecture arrière (constat 2026-09-23 : ``?kinds=order`` relisait 73 Mo en 9 s à chaque appel) :
# au plus 8 Mo (= 4 doublements de la fenêtre initiale), puis on renvoie ce qui a été trouvé.
JOURNAL_TAIL_MAX_BYTES = 8_000_000
JOURNAL_TAIL_MAX_DOUBLINGS: int | None = None      # plafond optionnel en nombre d'élargissements
# /api/journal : jamais plus de 5 000 événements sérialisés par réponse.
JOURNAL_API_DEFAULT_LIMIT = 500
JOURNAL_API_MAX_LIMIT = 5_000
# Authentification : 5 échecs par IP → 60 s d'attente (en mémoire, réinitialisé au redémarrage).
LOGIN_MAX_FAILURES = 5
LOGIN_BLOCK_SEC = 60.0
# Compression gzip des réponses (si ``Accept-Encoding`` le permet) à partir de cette taille.
GZIP_MIN_BYTES = 1024
# Points max des courbes d'equity / drawdown de /api/stats.
CURVE_MAX_POINTS = 400
# Longueur max du résumé d'un événement projeté dans ``journal_tail``.
EVENT_SUMMARY_MAX = 240
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
                      day: str | None = None, window: int = JOURNAL_TAIL_WINDOW_BYTES,
                      max_bytes: int = JOURNAL_TAIL_MAX_BYTES, max_doublings: int | None = JOURNAL_TAIL_MAX_DOUBLINGS) -> list[dict]:
    """Derniers ``limit`` événements du journal du jour SANS relire tout le fichier.

    Le journal atteint plusieurs dizaines de Mo en journée : on lit seulement une fenêtre en fin
    de fichier (première ligne partielle ignorée), élargie (x2) tant que moins de ``limit``
    événements filtrés sont trouvés et que le début du fichier n'est pas atteint. La lecture arrière
    est plafonnée (``max_bytes`` octets ou ``max_doublings`` élargissements) : un type d'événement rare
    renvoie alors ce qui a été trouvé plutôt que de relire tout le fichier à chaque appel.
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
    doublings = 0
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
        if window >= max_bytes or (max_doublings is not None and doublings >= max_doublings):
            return events[-limit:]      # plafond atteint : on renvoie ce qui a été trouvé
        window = min(window * 2, max(max_bytes, 1))
        doublings += 1


# Champs d'un événement conservés dans ``journal_tail`` (ceux que lit ``humanEvent()`` / ``rawEvent()``
# de la page d'accueil) ; le reste est résumé dans ``summary`` (≤ EVENT_SUMMARY_MAX caractères).
EVENT_FIELDS = ("ts_utc", "ts_local", "kind", "component", "level", "symbol", "ticket", "side", "agent_id",
                "message", "reason", "trade_idea_risk_money", "entry", "pnl", "result_r", "exit_reason", "verdict",
                "r", "gain_estime", "volume_restant", "rule", "old", "new", "mode", "command", "source", "ok",
                "error", "reasons", "actions", "withdrawable", "simulated", "amount", "trader_share", "skipped",
                "gain", "cap", "detail", "retcode", "volume", "master_symbol", "master_ticket")
_EVENT_META = ("ts_utc", "ts_local", "component", "kind")


def project_event(e: dict, max_summary: int = EVENT_SUMMARY_MAX) -> dict:
    """Projection compacte d'un événement du journal pour ``/api/state`` (un événement ``position_opened``
    embarque le candidat complet : plusieurs Ko inutiles à l'accueil)."""
    out: dict = {}
    for k in EVENT_FIELDS:
        if k in e:
            v = e[k]
            if isinstance(v, list):
                v = [x if isinstance(x, (str, int, float, bool)) or x is None else str(x) for x in v[:20]]
            elif isinstance(v, dict):
                v = str(v)[:max_summary]
            out[k] = v
    rv = e.get("review")
    if isinstance(rv, dict):
        out["review"] = {k: rv[k] for k in ("pnl", "result_r", "exit_reason", "verdict") if k in rv}
    res = e.get("result")
    if isinstance(res, dict) and "ok" in res:
        out["result"] = {"ok": res["ok"]}
    closed = e.get("closed")
    if isinstance(closed, list):
        out["closed"] = [(c.get("symbol") if isinstance(c, dict) else c) for c in closed[:50]]
    summary = e.get("message") or e.get("summary") or e.get("reason") or ""
    if not summary:
        try:
            summary = json.dumps({k: v for k, v in e.items() if k not in _EVENT_META}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            summary = ""
    summary = str(summary)
    if len(summary) > max_summary:
        summary = summary[:max_summary] + "…"
    out["summary"] = summary
    return out


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
        # lecteur concurrent : ne renomme jamais un system_state.json illisible, garde le dernier état connu
        self.store = StateStore(self.settings.state_dir, readonly=True)
        self._stats_lock = threading.Lock()
        # cache des journaux clos : chemin -> ((mtime_ns, taille), résultat du balayage)
        self._journal_cache: dict[str, tuple[tuple[int, int], dict]] = {}

    def snapshot(self, kinds: set[str] | None = None) -> dict:
        state = self.store.reload()
        d = state.public_dict()
        # Les clés d'idempotence sont internes et volumineuses : inutiles au dashboard.
        d.pop("executed_keys", None)
        # Idées de trade : seules les idées encore ouvertes (symbole, sens, risque cumulé) servent à la jauge
        # « idée la plus risquée » de l'accueil ; l'historique complet (des dizaines d'entrées) reste dans l'état.
        ideas = d.pop("trade_ideas", None) or {}
        d["trade_ideas_open"] = [{"symbol": i.get("symbol"), "side": i.get("side"), "risk_money": i.get("risk_money")}
                                 for i in ideas.values() if isinstance(i, dict) and i.get("open_tickets")]
        d["bot_positions_count"] = len(d.get("bot_positions") or {})
        d["prop"] = scrub_secrets(self.settings.prop)
        d["prop"]["autonomous_prop"] = self.settings.autonomous_prop
        d["risk"] = scrub_secrets(self.settings.risk)
        d["system"] = scrub_secrets(
            {k: v for k, v in self.settings.system.items() if k in ("autonomous_demo", "autonomous_prop", "heartbeat_max_age_sec", "magic_number")}
        )
        limit = JOURNAL_TAIL_FILTERED if kinds else JOURNAL_TAIL
        d["journal_tail"] = [project_event(e) for e in read_journal_tail(self.settings.logs_dir, limit, kinds=kinds)]
        d["journal_day"] = utcnow().strftime("%Y-%m-%d")
        d["server_time_utc"] = utcnow().isoformat()
        # 2026-10-01 (« la session Sydney je la vois jamais ») : session de marché en cours, forex et crypto
        from ..core.clock import current_session
        maintenant = utcnow()
        d["session_marche"] = {"forex": current_session(maintenant).value,
                               "crypto": current_session(maintenant, round_the_clock=True).value}
        d["orchestrator_heartbeat_age_sec"] = self.store.heartbeat_age("orchestrator")
        # Heartbeat watchdog lu DIRECTEMENT dans state/watchdog.json (et non via la copie faite par
        # l'orchestrateur) : orchestrateur mort ≠ watchdog mort, l'opérateur doit pouvoir distinguer.
        wd = read_watchdog_report(self.settings.state_dir) or {}
        d["watchdog_heartbeat_age_sec"] = _heartbeat_age(wd.get("heartbeat"))
        d["watchdog"] = {k: wd.get(k) for k in WATCHDOG_FIELDS}
        d["heartbeat_warn_sec"] = int(self.settings.system.get("heartbeat_max_age_sec", HEARTBEAT_WARN_SEC))
        d["model_usage"] = d.get("model_budget", {})
        d["leaderboard"] = _read_json_file(self.home / "reports" / "leaderboard.json", [])
        # ``learning`` (agent_stats.json, volumineux) n'est envoyé qu'en repli, quand le classement est vide.
        if not d["leaderboard"]:
            d["learning"] = _read_json_file(self.home / "data" / "agent_stats.json", {})
        # Cycle de paiement / cohérence : UNE seule définition (celle du PropGuard, utilisée par l'orchestrateur),
        # afin que l'accueil affiche exactement ce que le bot applique (2026-09-23).
        d["payout_cycle"] = self._payout_cycle(state)
        d["correlation"] = scrub_secrets(self.settings.correlation)
        d["open_risk_by_class"] = self._open_risk_by_class(state)
        d["accounts_glance"] = self._accounts_glance()
        return _json_safe(scrub_secrets(d))

    def _accounts_glance(self) -> list[dict]:
        """Même résumé que le bandeau du maître, pour CHAQUE compte suiveur (demande utilisateur 2026-09-24) :
        P&L fermé du jour de trading (reset 17:00 New York), trades G/P, positions, flottant, cohérence 25 % et
        jours de trading, calculés depuis `state/copy_status_<prefix>.json` (écrit par le copieur). Aucune donnée
        inventée : un compte sans fichier d'état apparaît avec `status: null`."""
        from datetime import datetime as _dt

        from ..core.trading_day import TradingDayCalendar
        from ..learning.consistency import consistency_from_trades

        out: list[dict] = []
        try:
            from ..copy.registry import followers_status

            followers = followers_status(self.settings.home)
        except Exception:  # noqa: BLE001 - config illisible : pas de résumé par compte
            return out
        cal = TradingDayCalendar.from_config(self.settings.prop)
        today = cal.day_key(utcnow())
        share_max = float(self.settings.prop.get("consistency_max_share_percent", 25.0))
        window = float(self.settings.prop.get("trade_idea_aggregation_minutes", 10.0))
        for f in followers:
            row = {"name": f.get("name"), "env_prefix": f.get("env_prefix"), "status": None}
            sf = self.settings.state_dir / f"copy_status_{f.get('env_prefix')}.json"
            st = _read_json_file(sf, None)
            if isinstance(st, dict):
                closed = [t for t in (st.get("closed") or []) if isinstance(t, dict)]
                jours, du_jour = set(), []
                for t in closed:
                    try:
                        k = cal.day_key(_dt.fromisoformat(str(t.get("closed_at"))))
                    except (TypeError, ValueError):
                        continue
                    jours.add(k)
                    if k == today:
                        du_jour.append(t)
                eq, bal = st.get("equity"), st.get("balance")
                cons = consistency_from_trades(closed, share_max, window)
                row.update(status="ok", ts_utc=st.get("ts_utc"), equity=eq, balance=bal,
                           floating=(round(float(eq) - float(bal), 2) if isinstance(eq, (int, float)) and isinstance(bal, (int, float)) else None),
                           positions=len(st.get("positions") or []),
                           today_pnl=round(sum(float(t.get("pnl") or 0.0) for t in du_jour), 2),
                           today_trades=len(du_jour), today_wins=sum(1 for t in du_jour if float(t.get("pnl") or 0.0) > 0),
                           today_losses=sum(1 for t in du_jour if float(t.get("pnl") or 0.0) <= 0),
                           trading_days=len(jours), consistency_share_percent=cons["share_percent"],
                           consistency_ok=cons["within_limit"], consistency_total_profit=cons["total_profit"])
            out.append(row)
        return out

    def _payout_cycle(self, state: Any) -> dict | None:
        """Statut du cycle FOXX calculé par le PropGuard ; None si indisponible (jamais inventé)."""
        try:
            from ..risk.prop_guard import PropGuard, PropProfile

            guard = PropGuard(PropProfile.from_config(self.settings.prop), self.settings.autonomous_demo,
                              self.settings.autonomous_prop, float(self.settings.risk.get("max_daily_loss_internal_percent", 1.0)))
            return guard.payout_cycle_status(state)
        except Exception:  # noqa: BLE001 - lecture seule : une erreur ne doit jamais faire tomber /api/state
            return None

    def _open_risk_by_class(self, state: Any) -> dict:
        """Risque ouvert (risque initial des positions du bot, en % de l'equity) par classe d'actifs.

        Retourne ``{classe: {"risk_money": x, "risk_percent": y, "positions": n}}`` ; ``risk_percent`` vaut None
        quand l'equity est inconnue (0), jamais un chiffre plausible.
        """
        from ..mt5.symbols import asset_class_of

        try:
            rules = self.settings.section("asset_class_rules") or {}
        except Exception:  # noqa: BLE001
            rules = {}
        equity = float(getattr(state, "equity", 0.0) or 0.0)
        out: dict[str, dict] = {}
        for plan in (getattr(state, "bot_positions", {}) or {}).values():
            symbol = str(getattr(plan, "symbol", "") or "")
            if not symbol:
                continue
            cls = asset_class_of(symbol, rules)
            money = float(getattr(plan, "initial_risk_money", 0.0) or 0.0)
            row = out.setdefault(cls, {"risk_money": 0.0, "risk_percent": None, "positions": 0})
            row["risk_money"] = round(row["risk_money"] + money, 2)
            row["positions"] += 1
        for row in out.values():
            row["risk_percent"] = round(100.0 * row["risk_money"] / equity, 4) if equity > 0 else None
        return out

    def journal(self, day: str | None, kinds: set[str] | None = None, limit: int = JOURNAL_API_DEFAULT_LIMIT,
                offset: int = 0) -> tuple[list[dict], int]:
        """Journal d'un jour, filtré par ``kinds``, paginé (``offset``/``limit``, limite plafonnée à
        ``JOURNAL_API_MAX_LIMIT``). Retourne (événements, total filtré)."""
        limit = max(1, min(int(limit), JOURNAL_API_MAX_LIMIT))
        offset = max(0, int(offset))
        events = read_journal_day(self.settings.logs_dir, day, kinds=kinds)
        return _json_safe(events[offset:offset + limit]), len(events)

    _stats_cache: tuple[float, dict] | None = None

    @staticmethod
    def _scan_journal_file(f: Path) -> dict:
        """Balaye un journal : ``opened`` (ticket -> symbole) et ``reviews`` (revues post-trade brutes)."""
        opened: dict[int, str] = {}
        reviews: list[dict] = []
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    if '"post_trade_review"' not in line and '"position_opened"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(e, dict):
                        continue
                    if e.get("kind") == "position_opened" and e.get("ticket") is not None:
                        try:
                            opened[int(e["ticket"])] = str(e.get("symbol", "?"))
                        except (TypeError, ValueError):
                            pass
                        continue
                    if e.get("kind") != "post_trade_review":
                        continue
                    r, pnl = e.get("result_r"), e.get("pnl")
                    if r is None or pnl is None:
                        continue
                    try:
                        reviews.append({"ts_utc": str(e.get("ts_utc", "")), "agent": str(e.get("agent_id", "?")),
                                        "ticket": int(e.get("ticket") or 0), "r": float(r), "pnl": float(pnl)})
                    except (TypeError, ValueError):
                        continue
        except OSError:
            pass
        return {"opened": opened, "reviews": reviews}

    def _journal_trades(self, day_key) -> list[dict]:
        """Trades fermés (revues post-trade) de tous les journaux. Un journal dont (mtime, taille) n'a pas
        changé n'est pas relu : seul le journal du jour courant, qui grossit, est rebalayé."""
        files = sorted(self.settings.logs_dir.glob("journal-*.jsonl"))
        seen: set[str] = set()
        scans: list[dict] = []
        for f in files:
            key = str(f)
            try:
                st_ = f.stat()
                sig = (st_.st_mtime_ns, st_.st_size)
            except OSError:
                continue
            seen.add(key)
            cached = self._journal_cache.get(key)
            if cached is None or cached[0] != sig:
                cached = (sig, self._scan_journal_file(f))
                self._journal_cache[key] = cached
            scans.append(cached[1])
        for key in [k for k in self._journal_cache if k not in seen]:
            self._journal_cache.pop(key, None)
        ticket_symbol: dict[int, str] = {}
        for s in scans:
            ticket_symbol.update(s["opened"])
        trades: list[dict] = []
        since = self._master_since()
        corriges = self._learning_pnl_by_ticket()
        for s in scans:
            for rv in s["reviews"]:
                if since and str(rv.get("ts_utc", "")) < since:
                    continue            # trade d'un ancien compte maître (archivé) : hors statistiques du maître actuel
                try:
                    day = day_key(datetime.fromisoformat(rv["ts_utc"]))
                except (TypeError, ValueError):
                    continue
                if not day:
                    continue
                # P&L de learning.db quand il existe : c'est la valeur de référence, corrigible (ex. commission
                # d'entrée ajoutée le 2026-09-24) ; le journal, lui, reste figé tel qu'écrit à la clôture
                pnl_ref = corriges.get(int(rv.get("ticket") or 0), rv["pnl"])
                trades.append({"day": day, "agent": rv["agent"], "symbol": ticket_symbol.get(rv["ticket"], "?"),
                               "r": rv["r"], "pnl": pnl_ref, "ticket": int(rv.get("ticket") or 0)})
        return trades

    def shadow(self) -> dict:
        """Page Shadow & recherche (2026-09-30, demande utilisateur : suivre le shadow et la recherche GPU sur le
        dashboard). Lecture seule, mise en cache 60 s."""
        cached = getattr(self, "_shadow_cache", None)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        from ..learning.shadow_board import shadow_board

        try:
            result = shadow_board(self.home, dict(self.settings.learning or {}))   # section « learning » de strategies.yaml
        except Exception as e:  # noqa: BLE001 - lecture seule : jamais de 500 pour un fichier illisible
            result = {"erreur": f"{type(e).__name__}: {e}"}
        self._shadow_cache = (time.time(), result)
        return result

    def quality(self, gel_only: bool = False) -> dict:
        """Page Qualité (plan pro du 2026-09-25, point 5) : espérance par agent avec sa marge d'incertitude,
        coût d'entrée, glissement, MFE / MAE, et avancement du gel des réglages."""
        from ..learning.quality import agent_quality, freeze_status

        db = self.settings.data_dir / "learning.db"
        if not db.exists():
            return {"global": {"n": 0}, "agents": [], "gel": None}
        gel = self.settings.system.get("gel_reglages") or None
        since = str(gel.get("depuis")) if (gel_only and gel and gel.get("depuis")) else None
        q = agent_quality(db, since=since)
        q["gel"] = freeze_status(db, gel)
        q["periode"] = "version gelée" if since else "tout l'historique live"
        return q

    def stats(self) -> dict:
        """Statistiques de trading agrégées depuis les revues post-trade du journal (2026-09-22, demande
        utilisateur : vues jour / semaine / mois / année). Résultat mis en cache 60 s, journaux clos mis en
        cache par (chemin, mtime, taille), recalcul sous verrou (un seul thread à la fois)."""
        cached = self._stats_cache
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        with self._stats_lock:
            cached = self._stats_cache
            if cached and time.time() - cached[0] < 60:
                return cached[1]
            result = self._compute_stats()
            self._stats_cache = (time.time(), result)
            return result

    def _compute_stats(self) -> dict:
        # jour = JOURNÉE DE TRADING (reset 17:00 New York), comme le P&L jour du panneau — pas le jour UTC.
        # Constaté le 2026-09-22 : le panneau disait +5 982 $ (equity, flottant inclus) et l'onglet Jour un
        # autre montant (trades fermés, jour calendaire UTC) ; les deux définitions ne pouvaient pas coïncider.
        from ..core.trading_day import TradingDayCalendar

        cal = TradingDayCalendar.from_config(self.settings.prop)
        trades = self._journal_trades(cal.day_key)
        statement = self._master_statement()
        if statement is not None:
            # « les vrais stats de chaque compte » (2026-09-24) : TOUS les trades réels du compte maître (historique MT5,
            # commissions et swaps compris) ; l'agent et le R viennent du journal par ticket, « manuel » sinon
            par_ticket = {t["ticket"]: t for t in trades if t.get("ticket")}
            reels = []
            for t in statement.get("trades") or []:
                try:
                    day = cal.day_key(datetime.fromisoformat(str(t.get("closed_at"))))
                except (TypeError, ValueError):
                    continue
                j = par_ticket.get(int(t.get("position_id") or 0), {})
                reels.append({"day": day, "agent": j.get("agent", "manuel"), "symbol": t.get("symbol", "?"),
                              "r": float(j.get("r", 0.0) or 0.0), "pnl": float(t.get("pnl") or 0.0),
                              "ticket": int(t.get("position_id") or 0)})
            trades = reels
        def bucket(items: dict, key: str, t: dict) -> None:
            b = items.setdefault(key, {"pnl": 0.0, "r": 0.0, "n": 0, "wins": 0})
            b["pnl"] += t["pnl"]; b["r"] += t["r"]; b["n"] += 1; b["wins"] += 1 if t["pnl"] > 0 else 0
        days: dict = {}; weeks: dict = {}; months: dict = {}; years: dict = {}; agents: dict = {}; symbols: dict = {}
        for t in trades:
            d = date.fromisoformat(t["day"])
            iso = d.isocalendar()
            bucket(days, t["day"], t)
            bucket(weeks, f"{iso[0]}-S{iso[1]:02d}", t)
            bucket(months, t["day"][:7], t)
            bucket(years, t["day"][:4], t)
            bucket(agents, t["agent"], t)
            if t["symbol"] != "?":
                bucket(symbols, t["symbol"], t)
        def series(items: dict, limit: int) -> list[dict]:
            out = [{"key": k, "pnl": round(v["pnl"], 2), "r": round(v["r"], 2), "n": v["n"],
                    "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else 0.0}
                   for k, v in sorted(items.items())]
            return out[-limit:]
        n = len(trades); wins = sum(1 for t in trades if t["pnl"] > 0)
        top = sorted(agents.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        agents_out = [{"agent": k, "pnl": round(v["pnl"], 2), "r": round(v["r"], 2), "n": v["n"],
                       "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else 0.0} for k, v in top]
        symbols_out = [{"symbol": k, "pnl": round(v["pnl"], 2), "r": round(v["r"], 2), "n": v["n"],
                        "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else 0.0}
                       for k, v in sorted(symbols.items(), key=lambda kv: kv[1]["pnl"], reverse=True)]
        result = {"total": {"trades": n, "wins": wins, "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
                            "pnl": round(sum(t["pnl"] for t in trades), 2), "r": round(sum(t["r"] for t in trades), 2),
                            "best": round(max((t["pnl"] for t in trades), default=0.0), 2),
                            "worst": round(min((t["pnl"] for t in trades), default=0.0), 2)},
                  "daily": series(days, 60), "weekly": series(weeks, 26), "monthly": series(months, 24),
                  "yearly": series(years, 10), "agents": agents_out, "symbols": symbols_out,
                  "statement": ({k: v for k, v in statement.items() if k != "trades"} if statement else None)}
        # sous-onglet par compte (2026-09-22, demande utilisateur) : état vivant de chaque suiveur,
        # écrit par son processus copieur dans state/copy_status_<prefix>.json
        accounts = []
        try:
            from ..copy.registry import followers_status

            for f in followers_status(self.settings.home):
                sf = self.settings.state_dir / f"copy_status_{f['env_prefix']}.json"
                status = None
                if sf.exists():
                    try:
                        status = json.loads(sf.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        status = None
                accounts.append({**f, "status": status})
        except Exception:  # noqa: BLE001 - la page stats ne doit jamais tomber pour un fichier d'état
            accounts = []
        # cohérence 25 % du maître (2026-09-23, demande utilisateur) : idées de trade reconstituées depuis les
        # trades live de learning.db (symbole + sens + réouverture sous 10 min), lecture seule
        from ..learning.consistency import account_stats, consistency_from_trades

        share_max = float(self.settings.prop.get("consistency_max_share_percent", 25.0))
        window = float(self.settings.prop.get("trade_idea_aggregation_minutes", 10.0))
        cycle_start = ""
        stt = self.store.reload()          # même lecteur (readonly) que /api/state : jamais de fichier mis de côté
        result["currency"] = stt.currency or ""
        result["trading_day"] = cal.day_key(utcnow())
        result["payouts"] = list(stt.payouts or [])
        result["simulated_withdrawn_total"] = stt.simulated_withdrawn_total()
        result["initial_balance"] = float(stt.initial_balance or 0.0) or None
        result["limits"] = {k: self.settings.prop.get(k) for k in
                            ("max_daily_loss_hard_percent", "max_overall_loss_hard_percent", "consistency_max_share_percent",
                             "max_withdrawal_percent_per_cycle", "max_risk_per_trade_idea_percent")}
        try:
            cycle_start = stt.payout_cycle_started_at or ""
            from ..risk.prop_guard import PropGuard, PropProfile
            prof = PropProfile.from_config(self.settings.prop)
            result["payout_cycle"] = PropGuard(prof, self.settings.autonomous_demo, self.settings.autonomous_prop,
                                               float(self.settings.risk.get("max_daily_loss_internal_percent", 1.0))).payout_cycle_status(stt)
        except Exception:  # noqa: BLE001 - la page stats ne doit jamais tomber pour l'état
            result["payout_cycle"] = None
        all_master = ([{"symbol": t.get("symbol"), "side": t.get("side"), "pnl": float(t.get("pnl") or 0.0),
                        "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at")} for t in statement.get("trades") or []]
                      if statement is not None else self._master_trades())
        master_trades = [t for t in all_master if not cycle_start or str(t.get("closed_at") or "") >= cycle_start]
        result["consistency"] = consistency_from_trades(master_trades, share_max, window)
        result["floors"] = self._floors(stt)
        result["equity_curve"], result["drawdown_curve"] = self._equity_curves(all_master, result["initial_balance"])
        result["by_asset_class"] = self._by_asset_class(all_master)
        # réplications en échec : le fichier d'état d'un copieur ne porte pas ``ok`` ; seul le journal du jour
        # (événements copy_trade ok=false, composant « copy-<nom> ») les connaît — lecture arrière plafonnée
        failed_events = [e for e in read_journal_tail(self.settings.logs_dir, JOURNAL_TAIL_FILTERED, kinds={"copy_trade"})
                         if e.get("ok") is False]
        for a in accounts:
            st_ = a.get("status") or {}
            closed = st_.get("closed") if isinstance(st_, dict) else None
            a["stats"] = account_stats(list(closed or []), cal.day_key, share_max, window)
            a["failed_copies"] = _group_failed_copies([e for e in failed_events if e.get("component") == f"copy-{a.get('name', '')}"])
        result["accounts"] = accounts
        return result

    def _floors(self, stt: Any) -> dict:
        """Planchers en devise du compte : jour = equity de référence − solde initial × perte jour dure ;
        global = solde initial × (1 − perte totale dure). None quand une donnée manque (jamais inventé)."""
        ib = float(getattr(stt, "initial_balance", 0.0) or 0.0)
        ref = float(getattr(getattr(stt, "daily", None), "reference_equity", 0.0) or 0.0)
        hard_day = self.settings.prop.get("max_daily_loss_hard_percent")
        hard_all = self.settings.prop.get("max_overall_loss_hard_percent")
        daily_floor = overall_floor = None
        try:
            if ib > 0 and ref > 0 and hard_day is not None:
                daily_floor = round(ref - ib * float(hard_day) / 100.0, 2)
            if ib > 0 and hard_all is not None:
                overall_floor = round(ib * (1.0 - float(hard_all) / 100.0), 2)
        except (TypeError, ValueError):
            daily_floor = overall_floor = None
        return {"daily_floor": daily_floor, "overall_floor": overall_floor, "reference_equity": ref or None,
                "initial_balance": ib or None}

    @staticmethod
    def _equity_curves(trades: list[dict], initial_balance: float | None) -> tuple[list[dict], list[dict]]:
        """Courbe d'equity fermée (solde initial + cumul des P&L de learning.db, triés par clôture) et
        variation en % depuis le solde initial (négatif = drawdown). ≤ CURVE_MAX_POINTS points chacune."""
        if not initial_balance or initial_balance <= 0 or not trades:
            return [], []
        ordered = sorted((t for t in trades if t.get("closed_at")), key=lambda t: str(t["closed_at"]))
        if not ordered:
            return [], []
        pts: list[dict] = [{"ts": str(ordered[0].get("opened_at") or ordered[0]["closed_at"]), "equity": round(initial_balance, 2),
                            "symbol": None, "pnl": None}]
        cum = float(initial_balance)
        for t in ordered:
            cum += float(t.get("pnl") or 0.0)
            pts.append({"ts": str(t["closed_at"]), "equity": round(cum, 2), "symbol": t.get("symbol"), "pnl": round(float(t.get("pnl") or 0.0), 2)})
        if len(pts) > CURVE_MAX_POINTS:
            step = math.ceil(len(pts) / CURVE_MAX_POINTS)
            kept = pts[::step]
            if kept[-1] is not pts[-1]:
                kept.append(pts[-1])
            pts = kept
        dd = [{"ts": p["ts"], "percent": round(100.0 * (p["equity"] - initial_balance) / initial_balance, 3)} for p in pts]
        return pts, dd

    def _by_asset_class(self, trades: list[dict]) -> list[dict]:
        """Trades, P&L et taux de réussite par classe d'actifs (``asset_class_of``), P&L décroissant."""
        from ..mt5.symbols import asset_class_of

        try:
            rules = self.settings.section("asset_class_rules") or {}
        except Exception:  # noqa: BLE001
            rules = {}
        acc: dict[str, dict] = {}
        for t in trades:
            cls = asset_class_of(str(t.get("symbol") or ""), rules)
            row = acc.setdefault(cls, {"asset_class": cls, "trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
            pnl = float(t.get("pnl") or 0.0)
            row["trades"] += 1
            row["wins"] += 1 if pnl > 0 else 0
            row["pnl"] += pnl
            row["r"] += float(t.get("result_r") or 0.0)
        out = []
        for row in acc.values():
            n = row["trades"]
            out.append({"asset_class": row["asset_class"], "trades": n, "wins": row["wins"], "pnl": round(row["pnl"], 2),
                        "r": round(row["r"], 2), "win_rate": round(100.0 * row["wins"] / n, 1) if n else 0.0})
        return sorted(out, key=lambda x: x["pnl"], reverse=True)

    def _master_statement(self) -> dict | None:
        """Relevé réel du compte maître (`state/statement_master.json`, écrit par l'orchestrateur) s'il concerne le
        compte actuellement connecté ; None sinon (repli sur le journal / learning.db)."""
        f = self.settings.state_dir / "statement_master.json"
        data = _read_json_file(f, None)
        if not isinstance(data, dict):
            return None
        try:
            login = int(self.store.reload().account_login or 0)
        except (TypeError, ValueError):
            login = 0
        if login and int(data.get("login") or 0) != login:
            return None
        return data

    def _learning_pnl_by_ticket(self) -> dict[int, float]:
        """{ticket: pnl} des trades live de learning.db (lecture seule ; base absente → {})."""
        import sqlite3

        db = self.settings.data_dir / "learning.db"
        if not db.exists():
            return {}
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                return {int(t): float(p or 0.0) for t, p in con.execute("SELECT ticket, pnl FROM trades WHERE mode='live'")
                        if t is not None}
            finally:
                con.close()
        except (sqlite3.Error, ValueError, TypeError):
            return {}

    def _master_since(self) -> str:
        """Début du compte maître actuel (`system.yaml` → `master_account_since`, ISO UTC). Changement de compte maître
        (2026-09-24, demande utilisateur « repartir sur de bonnes bases ») : les statistiques du maître repartent de
        cette date ; l'historique antérieur reste en base (les agents le gardent) et archivé sous l'ancien nom."""
        v = str(self.settings.system.get("master_account_since") or "").strip()
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat() if v else ""
        except ValueError:
            return ""

    def _master_trades(self) -> list[dict]:
        """Trades live du maître depuis `learning.db` (lecture seule ; base absente → liste vide)."""
        import sqlite3

        db = self.settings.data_dir / "learning.db"
        if not db.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
                extra = [c for c in ("result_r", "exit_reason", "agent_id") if c in cols]
                sql = "SELECT symbol, side, pnl, opened_at, closed_at" + "".join(f", {c}" for c in extra) + " FROM trades WHERE mode='live'"
                rows = con.execute(sql).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []
        out = []
        since = self._master_since()
        for r in rows:
            if since and str(r[4] or "") < since:
                continue                # ancien compte maître : ses trades restent en base pour les AGENTS, pas ici
            t = {"symbol": r[0], "side": r[1], "pnl": float(r[2] or 0.0), "opened_at": r[3], "closed_at": r[4]}
            for i, c in enumerate(extra):
                t[c] = r[5 + i]
            out.append(t)
        return out


def _group_failed_copies(events: list[dict]) -> list[dict]:
    """Regroupe les échecs de copie identiques consécutifs (symbole, sens, détail) en une ligne « ×N »."""
    out: list[dict] = []
    for e in events:
        row = {"ts": e.get("ts_utc"), "last_ts": e.get("ts_utc"), "symbol": e.get("symbol"), "master_symbol": e.get("master_symbol"),
               "side": e.get("side"), "volume": e.get("volume"), "ticket": e.get("ticket"), "message": e.get("message"), "detail": e.get("detail"),
               "retcode": e.get("retcode"), "retry_in_sec": e.get("retry_in_sec"), "n": 1}
        prev = out[-1] if out else None
        if prev and all(prev[k] == row[k] for k in ("symbol", "side", "message", "detail", "retcode")):
            prev["n"] += 1
            prev["last_ts"] = row["ts"]
            prev["volume"] = row["volume"]
        else:
            out.append(row)
    return out


# ----------------------------------------------------------------------------
# Serveur HTTP
# ----------------------------------------------------------------------------
class DashboardServer(ThreadingHTTPServer):
    """ThreadingHTTPServer portant la source de données (lecture seule)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], data: DashboardData, auth_token: str | None = None):
        self.data = data
        # jeton d'accès (DASHBOARD_AUTH_TOKEN, ajouté le 2026-09-22 pour l'accès depuis l'extérieur via VPN) :
        # s'il est défini, toute requête doit le présenter (Basic : mot de passe = jeton, ou Bearer).
        # Vide/absent → comportement historique sans authentification (usage 127.0.0.1 uniquement).
        self.auth_token = (auth_token or "").strip() or None
        self.auth_user = (os.environ.get("DASHBOARD_AUTH_USER") or "").strip() or None
        self.tls_enabled = False
        # tentatives d'authentification échouées par IP : ip -> [nombre, bloqué_jusqu'à (time.time())]
        self._auth_failures: dict[str, list[float]] = {}
        self._auth_lock = threading.Lock()
        super().__init__(server_address, DashboardHandler)

    # -- limitation des tentatives (5 échecs → 60 s d'attente, en mémoire) --
    def auth_wait_sec(self, ip: str) -> float:
        """Secondes d'attente restantes pour cette IP (0 = libre)."""
        with self._auth_lock:
            rec = self._auth_failures.get(ip)
            if not rec:
                return 0.0
            remaining = rec[1] - time.time()
            if rec[1] and remaining <= 0:
                self._auth_failures.pop(ip, None)     # blocage expiré : compteur remis à zéro
                return 0.0
            return max(0.0, remaining)

    def auth_failed(self, ip: str) -> None:
        with self._auth_lock:
            rec = self._auth_failures.setdefault(ip, [0.0, 0.0])
            rec[0] += 1
            if rec[0] >= LOGIN_MAX_FAILURES:
                rec[1] = time.time() + LOGIN_BLOCK_SEC

    def auth_succeeded(self, ip: str) -> None:
        with self._auth_lock:
            self._auth_failures.pop(ip, None)


LOGIN_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trading Lab — Connexion</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{background:#1e293b;padding:2rem 2.5rem;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.4);width:min(90vw,340px)}
h1{font-size:1.15rem;margin:0 0 1.2rem}label{display:block;font-size:.85rem;margin:.8rem 0 .25rem;color:#94a3b8}
input{width:100%;box-sizing:border-box;padding:.55rem .7rem;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:1rem}
button{margin-top:1.2rem;width:100%;padding:.6rem;border:0;border-radius:8px;background:#38bdf8;color:#0f172a;font-weight:700;font-size:1rem;cursor:pointer}
.err{color:#f87171;font-size:.85rem;margin-top:.8rem}</style></head><body>
<form method="post" action="/login"><h1>📊 Trading Lab — Dashboard</h1>
<label for="u">Utilisateur</label><input id="u" name="user" autocomplete="username" required>
<label for="p">Mot de passe</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Se connecter</button>__ERR__</form></body></html>"""


def _launch_restart(home: Path, mode: str) -> None:
    """Redémarrage complet (2026-09-22, demande utilisateur) : stop_all puis start_all, lancés dans un
    PowerShell DÉTACHÉ — le dashboard lui-même est tué par stop_all, le processus enfant doit lui survivre."""
    import subprocess

    stop = home / "scripts" / "stop_all.ps1"
    start = home / "scripts" / "start_all.ps1"
    cmd = f"& '{stop}'; Start-Sleep -Seconds 3; & '{start}' -Mode {mode}"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                     cwd=str(home), creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


RESTART_LAUNCHER = _launch_restart   # remplacé dans les tests

#: commandes d'exploitation autorisées depuis le panneau (mêmes effets que la CLI, même file, même journal).
#: SIMULATE_WITHDRAWAL exclu : outil d'analyse, pas une commande d'exploitation.
#: PAYOUT_DONE <montant> (2026-09-23) : le retrait a été demandé sur le tableau FOXX → nouveau cycle.
PANEL_COMMANDS = {"PAUSE", "RESUME", "SAFE_MODE", "CLOSE", "CLOSE_ALL_BOT", "PANIC", "BREAK_EVEN", "PAYOUT_DONE", "RESET_LOSSES", "CLOSE_WINNERS"}
#: commandes du panneau exigeant un argument numérique (montant ≥ 0).
PANEL_AMOUNT_COMMANDS = {"PAYOUT_DONE"}

CONTROL_HTML = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trading Lab — Contrôle</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;--sky:#38bdf8;
--green:#34d399;--amber:#fbbf24;--red:#f87171;--orange:#fb923c}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);margin:0;padding:1rem;overflow-x:hidden}
.wrap{max-width:760px;margin:0 auto;min-width:0}
h1{font-size:1.25rem;margin:.2rem 0}
.crumb{font-size:.93rem;color:var(--mut);margin-bottom:1rem}.crumb a{color:var(--sky);text-decoration:none}
.errbox{background:var(--red);color:#0f172a;padding:.6rem .9rem;border-radius:8px;margin-bottom:1rem;font-weight:600}.errbox a{color:#0f172a}
.state{display:flex;gap:.5rem;flex-wrap:wrap;margin:0 0 .5rem}
.pill{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:.4rem .9rem;font-size:.95rem;min-height:44px;display:inline-flex;align-items:center;gap:.4rem;max-width:100%}
.pill b{font-variant-numeric:tabular-nums}
#mode.AUTO{color:var(--green)} #mode.SAFE_MODE{color:var(--amber)} #mode.PAUSED{color:var(--orange)} #mode.PANIC{color:var(--red)}
.ctx{color:var(--mut);font-size:.95rem;margin:0 0 1.2rem;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:.8rem}
.cmd{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1rem;display:flex;flex-direction:column;gap:.5rem;min-width:0}
.cmd h2{font-size:1rem;margin:0;display:flex;align-items:center;gap:.5rem}
.cmd p{font-size:.9rem;color:var(--mut);margin:0;flex:1}
.cmd button,.confirm button,.b-btn{border:0;border-radius:8px;padding:.6rem .8rem;font-weight:700;font-size:1rem;cursor:pointer;color:#0f172a;min-height:44px}
.cmd button:disabled{opacity:.4;cursor:not-allowed}
.b-green{background:var(--green)}.b-amber{background:var(--amber)}.b-sky{background:var(--sky)}
.b-orange{background:var(--orange)}.b-red{background:var(--red)}.b-grey{background:#334155;color:var(--txt)}
.reasons{font-size:.9rem;color:var(--amber);margin:0}
.confirm{display:none;background:#0f172a;border:1px solid var(--line);border-radius:8px;padding:.6rem;font-size:.95rem;word-break:break-word}
.confirm.on{display:block}
.confirm .row{display:flex;gap:.5rem;margin-top:.5rem}
.confirm button{flex:1}
input{width:100%;min-height:44px;font-size:1rem;padding:.5rem .7rem;border-radius:8px;border:1px solid var(--line);background:#0f172a;color:var(--txt)}
label{font-size:.9rem;color:var(--mut);display:block}
.frow{display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap;border-bottom:1px solid var(--line);padding:.5rem 0}
.frow .fac{display:flex;align-items:center;gap:.35rem;font-size:.9rem;color:var(--mut)}
.frow .fac input{width:5.5rem}
.frow .fac button{padding:.4rem .7rem;border-radius:8px;border:0;background:var(--sky);color:#0f172a;font-weight:700;cursor:pointer;min-height:44px}
#toast{position:fixed;left:50%;bottom:1.2rem;transform:translateX(-50%);background:var(--panel);
border:1px solid var(--line);border-radius:10px;padding:.7rem 1.2rem;font-size:.95rem;opacity:0;transition:opacity .3s;pointer-events:none;max-width:92vw}
#toast.on{opacity:1}
.foot{margin-top:1.6rem;font-size:.9rem;color:var(--mut)}
@media (max-width:640px){body{padding:.75rem}.grid{grid-template-columns:1fr}.pill{font-size:.9rem;padding:.35rem .7rem}}
</style></head><body><div class="wrap">
<h1>🎛️ Panneau de contrôle</h1>
<div class="crumb"><a href="/">← Tableau de bord</a> · <a href="/stats">Statistiques</a> · <a href="/logout">Déconnexion</a> · les commandes passent par la file officielle du bot et sont toutes journalisées</div>
<div id="err" class="errbox" hidden></div>
<div class="state">
  <span class="pill">Mode <b id="mode">…</b></span>
  <span class="pill">Positions <b id="pos">…</b></span>
  <span class="pill">Equity <b id="eq">…</b></span>
  <span class="pill">P&amp;L jour (equity, flottant inclus) <b id="pnl">…</b></span>
  <span class="pill" id="locks" hidden>Verrous <b id="lockv"></b></span>
</div>
<p class="ctx" id="ctx">—</p>
<div class="grid">
  <div class="cmd"><h2>⏸️ Pause</h2><p>Suspend les nouvelles entrées. Les positions ouvertes restent gérées (TP, break-even, stops).</p>
    <button class="b-amber" id="btn_pause" onclick="ask('PAUSE',this)">Mettre en pause</button><div class="confirm"></div></div>
  <div class="cmd"><h2>▶️ Reprise</h2><p>Ré-autorise le trading autonome (accepté uniquement sur le compte DEMO attendu).</p>
    <p class="reasons" id="mode_reasons"></p>
    <button class="b-green" id="btn_resume" onclick="ask('RESUME',this)">Reprendre</button><div class="confirm"></div></div>
  <div class="cmd"><h2>🛡️ Safe mode</h2><p>Mode prudence : aucune nouvelle entrée tant que le système n'a pas retrouvé des cycles sains.</p>
    <button class="b-sky" onclick="ask('SAFE_MODE',this)">Activer</button><div class="confirm"></div></div>
  <div class="cmd"><h2>⚖️ Break-even</h2><p>Force le passage du stop à l'entrée sur toutes les positions qui sont en profit.</p>
    <button class="b-sky" onclick="ask('BREAK_EVEN',this)">Armer partout</button><div class="confirm"></div></div>
  <div class="cmd"><h2>🔓 Lever le verrou « pertes consécutives »</h2><p>Remet à zéro le compteur de pertes d'affilée : les entrées reprennent si aucun autre verrou (perte du jour, giveback, règles prop) n'est actif.</p>
    <button class="b-green" onclick="ask('RESET_LOSSES',this)">Lever le verrou</button><div class="confirm"></div></div>
  <div class="cmd"><h2>💰 Encaisser les gagnantes</h2><p>Ferme tout de suite chaque position du bot en profit et laisse les positions en perte actives (avec leur stop et leur TP).</p>
    <button class="b-green" onclick="ask('CLOSE_WINNERS',this)">Encaisser les gagnantes</button><div class="confirm"></div></div>
  <div class="cmd"><h2>📕 Tout fermer</h2><p>Ferme proprement toutes les positions du bot au marché. Le mode de trading reste inchangé.</p>
    <button class="b-orange" onclick="ask('CLOSE_ALL_BOT',this)">Fermer les positions</button><div class="confirm"></div></div>
  <div class="cmd"><h2>🚨 Arrêt d'urgence</h2><p>Ferme tout, annule tout, verrouille le trading. À n'utiliser qu'en dernier recours.</p>
    <button class="b-red" onclick="ask('PANIC',this)">Arrêt d'urgence</button><div class="confirm"></div></div>
  <div class="cmd"><h2>🔄 Redémarrer le bot</h2><p>Relance tous les processus (orchestrateur, watchdog, dashboard, copieurs) dans le mode actuel. Les positions ne sont pas touchées. ~60 à 90 s.</p>
    <button class="b-sky" onclick="askRestart(this)">Redémarrer</button><div class="confirm"></div></div>
  <div class="cmd" id="paycard" hidden><h2>💰 Paiement effectué</h2>
    <p id="paytxt">Le cycle est prêt : demande le retrait sur le tableau de bord FOXX, puis saisis ici le montant retiré pour ouvrir le cycle suivant (0 = renoncer à ce paiement).</p>
    <label for="pay_amount">Montant retiré</label><input id="pay_amount" inputmode="decimal" placeholder="montant">
    <button class="b-green" onclick="askPayout(this)">Paiement effectué</button><div class="confirm"></div></div>
</div>
<h1 style="margin-top:1.8rem;font-size:1.1rem">👥 Comptes — copy trading</h1>
<div class="cmd" style="margin-top:.6rem">
  <div id="flist"></div>
  <p>Réplique les trades du bot vers un autre compte MT5, avec une taille proportionnelle à son equity.
  Prérequis : une <b>installation MT5 dédiée</b> à ce compte sur ce PC (copie portable — demande à
  l'assistant, il la prépare en 5 min). Les identifiants sont stockés uniquement sur ce PC (.env),
  jamais réaffichés.</p>
  <button class="b-sky" id="faddbtn" onclick="document.getElementById('fform').hidden=false;this.hidden=true">＋ Ajouter un compte suiveur</button>
  <div id="fform" hidden style="display:flex;flex-direction:column;gap:.45rem">
    <input id="f_name" placeholder="Nom (2-40 caractères, ex: Compte démo 10k)">
    <input id="f_login" placeholder="Login MT5 (numéro de compte)" inputmode="numeric">
    <input id="f_pass" type="password" placeholder="Mot de passe MT5">
    <input id="f_server" placeholder="Serveur (ex: ICMarketsEU-Demo)">
    <label>Facteur de taille — 1.0 = même risque que le maître (proportionnel au solde du compte), 0.5 = deux fois moins
    <input id="f_factor" value="1.0" inputmode="decimal" style="margin-top:.25rem"></label>
    <p style="font-size:.9rem;color:#94a3b8;margin:0">Le terminal MT5 dédié est détecté automatiquement sur ce PC.</p>
    <button class="b-green" onclick="addFollower()">Enregistrer ce compte</button>
  </div>
</div>
<div class="foot">Réglages des seuils de risque : sur demande auprès de l'assistant (bornés et testés avant application). Cette page nécessite la même connexion que le tableau de bord.</div>
</div><div id="toast"></div>
<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function isNum(v){return typeof v==='number'&&Number.isFinite(v)}
let CUR='',STATE=null,FOLLOWERS=[];
function curSym(){return {USD:'$',EUR:'€',GBP:'£',CHF:'CHF'}[CUR]||CUR||'$'}
function money(v){return isNum(v)?Math.round(v).toLocaleString('fr-FR')+' '+curSym():'—'}
function smoney(v){return isNum(v)?(v>=0?'+':'')+money(v):'—'}
function pct(v){return isNum(v)?v.toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})+' %':'—'}
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),4000)}
function showErr(m){const e=$('err');if(!e)return;if(!m){e.hidden=true;e.innerHTML='';return}e.hidden=false;e.innerHTML=m}
async function loadFollowers(){
  try{
    const r=await fetch('/api/copy/status',{cache:'no-store'});
    if(r.status===401){showErr('Session expirée — <a href="/login">se reconnecter</a>');return}
    const d=await r.json();
    const el=$('flist');FOLLOWERS=d.followers||[];
    if(!FOLLOWERS.length){el.innerHTML='<p style="color:#94a3b8">Aucun compte suiveur pour le moment.</p>';return}
    el.innerHTML=FOLLOWERS.map((f,i)=>{
      const ok=f.creds_ok&&f.terminal_ok;
      return `<div class="frow"><span>👤 <b>${esc(f.name)}</b></span>
        <span class="fac">facteur × <input id="fac_${i}" value="${esc(f.size_factor)}" inputmode="decimal" aria-label="facteur de ${esc(f.name)}">
          <button type="button" onclick="setFactor(${i})">OK</button></span>
        <span class="fdel"><button type="button" class="b-red" onclick="askRemove(${i},this)">🗑️ Supprimer</button><div class="confirm"></div></span>
        <span style="color:${ok?'#34d399':'#fbbf24'}">${ok?'✅ prêt':(f.creds_ok?'⚠️ terminal introuvable':'⚠️ identifiants incomplets')}</span></div>`}).join('');
  }catch(e){}
}
async function setFactor(i){
  const f=FOLLOWERS[i];if(!f)return;const val=($('fac_'+i)||{}).value;
  try{
    const r=await fetch('/api/copy/factor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:f.name,size_factor:val})});
    const d=await r.json();toast(d.ok?('✅ '+d.note):('❌ '+d.error));if(d.ok)loadFollowers();
  }catch(e){toast('❌ erreur réseau')}
}
function askRemove(i,btn){
  const f=FOLLOWERS[i];if(!f)return;
  openConfirm(btn,`Supprimer définitivement le compte « ${f.name} » ? Son copieur s'arrête et ses identifiants sont effacés. `+
    `Les positions déjà copiées restent ouvertes sur ce compte, avec leur stop et leur TP.`,'Oui, supprimer','b-red',()=>removeFollower(i));
}
async function removeFollower(i){
  const f=FOLLOWERS[i];if(!f)return;
  try{
    const r=await fetch('/api/copy/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:f.name,confirm:true})});
    const d=await r.json();
    if(d.ok){toast('✅ '+d.note);loadFollowers();setTimeout(restartBot,1500)}else toast('❌ '+d.error);
  }catch(e){toast('❌ erreur réseau')}
}
async function addFollower(){
  const body={name:$('f_name').value,login:$('f_login').value,password:$('f_pass').value,server:$('f_server').value,
              terminal_path:'',size_factor:$('f_factor').value};
  try{
    const r=await fetch('/api/copy/follower',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){toast('✅ Compte enregistré — redémarrage automatique du bot pour lancer le copieur…');$('f_pass').value='';$('fform').hidden=true;$('faddbtn').hidden=false;loadFollowers();setTimeout(restartBot,1500)}
    else toast('❌ '+d.error);
  }catch(e){toast('❌ erreur réseau')}
}
const NEED_CONFIRM={CLOSE_WINNERS:"Fermer maintenant toutes les positions du bot en profit ? Les positions en perte restent ouvertes.",CLOSE_ALL_BOT:"Fermer TOUTES les positions du bot au prix du marché ?",PANIC:"Arrêt d'urgence : tout fermer et verrouiller le trading ?"};
function openConfirm(btn,question,yesLabel,yesClass,onYes){
  const box=btn.parentElement.querySelector('.confirm');
  document.querySelectorAll('.confirm').forEach(c=>{c.classList.remove('on');c.innerHTML=''});
  const yes=document.createElement('button');yes.type='button';yes.className=yesClass;yes.textContent=yesLabel;
  yes.onclick=()=>{onYes();box.classList.remove('on')};
  const no=document.createElement('button');no.type='button';no.className='b-grey';no.textContent='Annuler';
  no.onclick=()=>box.classList.remove('on');
  const row=document.createElement('div');row.className='row';row.append(yes,no);
  box.textContent=question;box.append(row);box.classList.add('on');
}
function positionSymbols(){const bp=(STATE&&STATE.bot_positions)||{};return Object.keys(bp).map(k=>(bp[k]||{}).symbol||'?')}
function ask(cmd,btn){
  let q=NEED_CONFIRM[cmd];
  if(!q){send(cmd);return}
  if(cmd==='CLOSE_ALL_BOT'){const syms=positionSymbols();q+=syms.length?(' Positions : '+syms.join(', ')+'.'):' (aucune position ouverte actuellement).'}
  openConfirm(btn,q,'Oui, confirmer','b-red',()=>send(cmd));
}
function askRestart(btn){openConfirm(btn,'Redémarrer tous les processus du bot ?','Oui, redémarrer','b-sky',restartBot)}
function askPayout(btn){
  const raw=String(($('pay_amount')||{}).value||'').replace(',','.').trim();
  const amount=Number(raw);
  if(raw===''||!Number.isFinite(amount)||amount<0){toast('❌ montant invalide (nombre ≥ 0, 0 = renoncer)');return}
  const q=amount>0?('Confirmer le paiement de '+money(amount)+' ? Un nouveau cycle démarre.'):'Renoncer à ce paiement et démarrer un nouveau cycle ?';
  openConfirm(btn,q,'Oui, confirmer','b-green',()=>send('PAYOUT_DONE',amount.toFixed(2)));
}
async function restartBot(){
  try{
    const r=await fetch('/api/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(!d.ok){toast('❌ '+(d.error||'refusé'));return}
    toast('🔄 '+d.note);
    let left=90;const t=$('toast');
    const iv=setInterval(()=>{left-=5;t.textContent='🔄 Redémarrage en cours… retour dans ~'+left+' s';t.classList.add('on');
      if(left<=0){clearInterval(iv);location.reload()}},5000);
  }catch(e){toast('❌ erreur réseau')}
}
async function send(cmd,arg){
  try{
    const body={command:cmd};if(arg!==undefined)body.arg=arg;
    const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    toast(d.ok?('✅ '+cmd+' envoyée — prise en compte au prochain cycle (≤15 s)'):('❌ '+(d.error||'refusée')));
    setTimeout(refresh,2000);
  }catch(e){toast('❌ erreur réseau')}
}
async function refresh(){
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    if(r.status===401){showErr('Session expirée — <a href="/login">se reconnecter</a>');return}
    if(!r.ok){showErr('État indisponible (HTTP '+r.status+')');return}
    const d=await r.json();STATE=d;CUR=d.currency||'';showErr('');
    const m=$('mode');m.textContent=d.mode||'?';m.className=d.mode||'';
    const bp=d.bot_positions||{};const n=Object.keys(bp).length;
    $('pos').textContent=String(n);
    $('eq').textContent=money(d.equity);
    const p=$('pnl');const v=d.daily_pnl;
    p.textContent=smoney(v);p.style.color=isNum(v)?(v>=0?'#34d399':'#f87171'):'';
    const floating=(isNum(d.equity)&&isNum(d.balance))?d.equity-d.balance:null;
    $('ctx').textContent=n+' position'+(n===1?'':'s')+' ouverte'+(n===1?'':'s')+', flottant '+smoney(floating)+', risque ouvert '+pct(d.open_risk_percent);
    const lr=d.lock_reasons||[];const lk=$('locks');lk.hidden=!lr.length;$('lockv').textContent=lr.join(', ');
    const mr=[].concat(d.mode_reasons||[]).concat(lr.map(x=>'verrou : '+x));
    $('mode_reasons').textContent=mr.length?mr.join(' · '):'';
    $('btn_resume').disabled=(d.mode==='AUTO');
    $('btn_pause').disabled=(d.mode==='PAUSED');
    const pc=d.payout_cycle||null;const card=$('paycard');
    const show=!!(pc&&(pc.ready||pc.window_open));
    card.hidden=!show;
    if(show){
      const inp=$('pay_amount');if(inp&&inp.value===''&&isNum(pc.withdrawable))inp.value=pc.withdrawable.toFixed(2);
      $('paytxt').textContent=(pc.window_open?'Fenêtre de paiement ouverte (positions fermées). ':'Cycle prêt. ')+
        'Retirable : '+money(pc.withdrawable)+(isNum(pc.profit_split_percent)?' (part trader '+pc.profit_split_percent+' %)':'')+
        '. Demande le retrait sur le tableau FOXX puis saisis le montant retiré (0 = renoncer).';
    }
  }catch(e){showErr('État indisponible : '+esc(e.message||e))}
}
loadFollowers();refresh();setInterval(refresh,5000);
</script></body></html>"""


STATS_HTML = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trading Lab — Statistiques</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;--sky:#38bdf8;--green:#34d399;--red:#f87171;--amber:#fbbf24}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);margin:0;padding:1rem;overflow-x:hidden}
.wrap{max-width:900px;margin:0 auto;min-width:0}
h1{font-size:1.25rem;margin:.2rem 0}
h2{font-size:1.05rem;margin:1.4rem 0 .6rem}
.crumb{font-size:.93rem;color:var(--mut);margin-bottom:1rem}.crumb a{color:var(--sky);text-decoration:none}
.errbox{background:var(--red);color:#0f172a;padding:.6rem .9rem;border-radius:8px;margin-bottom:1rem;font-weight:600}.errbox a{color:#0f172a}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin-bottom:1rem}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.8rem 1rem;min-width:0}
.card small{color:var(--mut);font-size:.875rem;display:block}
.card b{font-size:1.2rem;font-variant-numeric:tabular-nums;word-break:break-word}
.card .sub{color:var(--mut);font-size:.875rem;display:block;margin-top:.15rem}
.tabs{display:flex;gap:.4rem;margin-bottom:.8rem;flex-wrap:wrap}
.tabs button{background:var(--panel);border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:.5rem .9rem;cursor:pointer;font-size:.95rem;min-height:44px}
.tabs button.on{background:var(--sky);color:#0f172a;font-weight:700;border-color:var(--sky)}
.chartbox{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:.8rem;margin-bottom:1rem;position:relative;min-width:0}
canvas{width:100%;height:240px;display:block;touch-action:pan-y}
canvas.short{height:130px;margin-top:.5rem}
.tip{position:absolute;background:#0f172a;border:1px solid var(--line);border-radius:8px;padding:.4rem .6rem;font-size:.875rem;pointer-events:none;white-space:nowrap;z-index:2}
.note{color:var(--mut);font-size:.9rem;margin:.4rem 0 0}
table{border-collapse:collapse;width:100%;font-size:.9rem;background:var(--panel)}
th{text-align:left;color:var(--mut);font-size:.875rem;padding:.6rem .7rem;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:.55rem .7rem;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.pos{color:var(--green)}.neg{color:var(--red)}.mut{color:var(--mut)}.warn{color:var(--amber)}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-bottom:1rem;max-width:100%}
.paybox{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1rem;margin-bottom:1rem}
.paybox p{margin:.35rem 0}
.progress{height:12px;background:#0f172a;border:1px solid var(--line);border-radius:6px;overflow:hidden;margin:.4rem 0}
.progress div{height:100%;background:var(--sky)}
.conds{list-style:none;padding:0;margin:.6rem 0}.conds li{padding:.25rem 0}
.bars .row{display:grid;grid-template-columns:7rem 1fr auto;gap:.5rem;align-items:center;padding:.3rem 0;font-variant-numeric:tabular-nums}
.bars .bar{height:12px;background:#0f172a;border-radius:6px;overflow:hidden}.bars .bar div{height:100%}
.fail{border-color:var(--red)}
@media (max-width:640px){body{padding:.75rem}.hide-sm{display:none}canvas{height:220px}canvas.short{height:110px}.card b{font-size:1.1rem}.bars .row{grid-template-columns:5.5rem 1fr auto}}
</style></head><body><div class="wrap">
<h1>📈 Statistiques</h1>
<div class="crumb"><a href="/">← Tableau de bord</a> · <a href="/control">Panneau de contrôle</a> · <a href="/qualite">Qualité</a> · <a href="/shadow">Shadow &amp; recherche</a> · <a href="/logout">Déconnexion</a> · trades <b>fermés</b> uniquement (le flottant des positions ouvertes n'est pas compté), journée de trading = reset 17:00 New York, heures Europe/Paris, mise à jour toutes les 60 s</div>
<div id="err" class="errbox" hidden></div>
<div class="tabs" id="atabs" role="tablist" aria-label="Comptes" hidden></div>
<div id="fview" hidden></div>
<div id="mview">
<div id="releve"></div>
<div class="cards" id="cards"></div>
<h2>Courbe d'equity fermée et planchers</h2>
<div class="chartbox" id="eqbox"><canvas id="eqchart" aria-label="Courbe d'equity fermée avec les planchers jour et global"></canvas><div id="eqtip" class="tip" hidden></div>
<canvas id="ddchart" class="short" aria-label="Variation en % depuis le solde initial, avec la limite de perte totale"></canvas><p class="note" id="eqnote"></p></div>
<div class="tabs" id="tabs" role="tablist" aria-label="Période">
  <button data-p="daily" role="tab" aria-selected="true" class="on">Jour</button><button data-p="weekly" role="tab" aria-selected="false">Semaine</button>
  <button data-p="monthly" role="tab" aria-selected="false">Mois</button><button data-p="yearly" role="tab" aria-selected="false">Année</button>
</div>
<div class="chartbox" id="chartbox"><canvas id="chart" aria-label="P&L fermé par période et cumul"></canvas></div>
<h2>Cycle de paiement FOXX</h2>
<div class="paybox" id="paybox"></div>
<h2>Cohérence 25 % — part de chaque idée de trade dans le profit du cycle</h2>
<div class="chartbox" id="consbox"><canvas id="cons" aria-label="Part de chaque idée de trade dans le profit du cycle"></canvas><p id="constxt" class="note"></p></div>
<h2>Par classe d'actifs</h2>
<div class="chartbox bars" id="classes"></div>
<h2>Par période</h2>
<div class="tablewrap"><table id="ptable"><thead><tr><th>Période</th><th>Trades</th><th>Taux de réussite</th><th class="hide-sm">R total</th><th>P&amp;L</th></tr></thead><tbody></tbody></table></div>
<h2>Par paire — des plus performantes aux moins performantes</h2>
<div class="tablewrap"><table id="stable"><thead><tr><th>Paire</th><th>Trades</th><th>Taux de réussite</th><th class="hide-sm">R total</th><th>P&amp;L</th></tr></thead><tbody></tbody></table></div>
<h2>Par agent (classement complet)</h2>
<div class="tablewrap"><table id="atable"><thead><tr><th>Agent</th><th>Trades</th><th>Taux de réussite</th><th class="hide-sm">R total</th><th>P&amp;L</th></tr></thead><tbody></tbody></table></div>
</div>
</div>
<script>
let DATA=null,period='daily',acct='master',fperiod='daily',CUR='';
const $=id=>document.getElementById(id);
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function isNum(v){return typeof v==='number'&&Number.isFinite(v)}
function curSym(){return {USD:'$',EUR:'€',GBP:'£',CHF:'CHF'}[CUR]||CUR||'$'}
function money(v){return isNum(v)?Math.round(v).toLocaleString('fr-FR')+' '+curSym():'—'}
function fmt(v){return isNum(v)?(v>=0?'+':'')+money(v):'—'}
function pct(v,d){d=(d==null)?1:d;return isNum(v)?v.toLocaleString('fr-FR',{minimumFractionDigits:d,maximumFractionDigits:d})+' %':'—'}
function rfmt(v){return isNum(v)?(v>=0?'+':'')+v.toLocaleString('fr-FR',{maximumFractionDigits:2})+' R':'—'}
function cls(v){return isNum(v)?(v>0?'pos':(v<0?'neg':'')):''}
function normTs(ts){if(!ts)return NaN;return Date.parse(String(ts).replace(/(\.\d{3})\d+/,'$1'))}
function paris(ts,withDate){const t=normTs(ts);if(!Number.isFinite(t))return String(ts||'—').slice(11,16);const d=new Date(t);
  return withDate?d.toLocaleString('fr-FR',{timeZone:'Europe/Paris',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):d.toLocaleTimeString('fr-FR',{timeZone:'Europe/Paris',hour:'2-digit',minute:'2-digit'})}
function showErr(html){const e=$('err');if(!e)return;if(!html){e.hidden=true;e.innerHTML='';return}e.hidden=false;e.innerHTML=html}
function setup(id,H){const c=$(id);if(!c)return null;const dpr=window.devicePixelRatio||1;const W=c.clientWidth||320;c.width=W*dpr;c.height=H*dpr;
  const x=c.getContext('2d');if(!x)return null;x.scale(dpr,dpr);x.clearRect(0,0,W,H);x.font='13px system-ui';return {c,x,W,H}}
function emptyMsg(g,msg){g.x.fillStyle='#94a3b8';g.x.textAlign='left';g.x.fillText(msg,16,32)}
const PERIODS={daily:'Jour',weekly:'Semaine',monthly:'Mois',yearly:'Année'};
function tabsHtml(list,attr,current){return list.map(([k,l])=>`<button data-${attr}="${esc(k)}" role="tab" aria-selected="${current===k}" class="${current===k?'on':''}">${l}</button>`).join('')}
/* ---------- graphiques ---------- */
function drawBars(id,rows){
  const g=setup(id,$(id)&&$(id).clientHeight>0?$(id).clientHeight:240);if(!g)return;const {x,W,H}=g;
  if(!rows||!rows.length){emptyMsg(g,'Pas encore de données');return}
  const pad={l:60,r:10,t:12,b:34};const iw=W-pad.l-pad.r,ih=H-pad.t-pad.b;
  let cum=0;const cums=rows.map(r=>cum+=(isNum(r.pnl)?r.pnl:0));
  const vals=rows.map(r=>isNum(r.pnl)?r.pnl:0);
  const lo=Math.min(0,...vals,...cums),hi=Math.max(0,...vals,...cums);
  const y=v=>pad.t+ih*(1-(v-lo)/((hi-lo)||1));
  x.strokeStyle='#334155';x.lineWidth=1;x.beginPath();x.moveTo(pad.l,y(0));x.lineTo(W-pad.r,y(0));x.stroke();
  x.fillStyle='#94a3b8';x.textAlign='right';
  [lo,0,hi].forEach(v=>x.fillText(Math.round(v).toLocaleString('fr-FR'),pad.l-6,y(v)+4));
  const step=iw/rows.length,bw=Math.max(3,step*0.6);
  rows.forEach((r,i)=>{const bx=pad.l+i*step+(step-bw)/2;x.fillStyle=vals[i]>=0?'#34d399':'#f87171';
    const y0=y(0),y1=y(vals[i]);x.fillRect(bx,Math.min(y0,y1),bw,Math.max(2,Math.abs(y0-y1)))});
  x.strokeStyle='#38bdf8';x.lineWidth=2;x.beginPath();
  cums.forEach((v,i)=>{const px=pad.l+i*step+step/2;i?x.lineTo(px,y(v)):x.moveTo(px,y(v))});x.stroke();
  const last=rows.length-1;x.fillStyle='#38bdf8';x.beginPath();x.arc(pad.l+last*step+step/2,y(cums[last]),3.5,0,7);x.fill();
  x.fillStyle='#94a3b8';x.textAlign='center';
  const lab=i=>{const k=String(rows[i].key||'');return k.length>7?k.slice(5):k};
  [0,Math.floor(rows.length/2),last].filter((v,i,a)=>a.indexOf(v)===i).forEach(i=>x.fillText(lab(i),pad.l+i*step+step/2,H-12));
}
let EQ={pts:[],pad:null,geom:null};
function drawEquity(curve,floors,hover){
  const g=setup('eqchart',$('eqchart')&&$('eqchart').clientHeight>0?$('eqchart').clientHeight:240);if(!g)return;const {x,W,H}=g;
  const pts=(curve||[]).filter(p=>isNum(p.equity));
  EQ.pts=[];
  if(pts.length<2){emptyMsg(g,'Pas encore de trade fermé dans learning.db (ou solde initial inconnu)');return}
  const pad={l:64,r:12,t:12,b:26};const iw=W-pad.l-pad.r,ih=H-pad.t-pad.b;
  const fl=[floors&&floors.daily_floor,floors&&floors.overall_floor].filter(isNum);
  const eqs=pts.map(p=>p.equity);
  let lo=Math.min(...eqs,...fl),hi=Math.max(...eqs,...fl);if(hi===lo){hi+=1;lo-=1}
  const m=(hi-lo)*0.05;lo-=m;hi+=m;
  const y=v=>pad.t+ih*(1-(v-lo)/(hi-lo));const xs=i=>pad.l+iw*(pts.length>1?i/(pts.length-1):0);
  x.strokeStyle='#334155';x.lineWidth=1;x.fillStyle='#94a3b8';x.textAlign='right';
  [lo+m,(lo+hi)/2,hi-m].forEach(v=>{x.beginPath();x.moveTo(pad.l,y(v));x.lineTo(W-pad.r,y(v));x.stroke();x.fillText(Math.round(v).toLocaleString('fr-FR'),pad.l-6,y(v)+4)});
  const floorLine=(v,color,label)=>{if(!isNum(v))return;x.strokeStyle=color;x.setLineDash([5,4]);x.beginPath();x.moveTo(pad.l,y(v));x.lineTo(W-pad.r,y(v));x.stroke();x.setLineDash([]);
    x.fillStyle=color;x.textAlign='left';x.fillText(label+' '+Math.round(v).toLocaleString('fr-FR'),pad.l+4,y(v)-4)};
  floorLine(floors&&floors.daily_floor,'#fbbf24','plancher jour');
  floorLine(floors&&floors.overall_floor,'#f87171','plancher global');
  x.strokeStyle='#38bdf8';x.lineWidth=2;x.beginPath();
  pts.forEach((p,i)=>{const px=xs(i),py=y(p.equity);EQ.pts.push({px,py,p});i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();
  x.fillStyle='#94a3b8';x.textAlign='left';x.fillText(paris(pts[0].ts,true),pad.l,H-8);x.textAlign='right';x.fillText(paris(pts[pts.length-1].ts,true),W-pad.r,H-8);
  if(hover&&isNum(hover.px)){x.fillStyle='#e2e8f0';x.beginPath();x.arc(hover.px,hover.py,4.5,0,7);x.fill();
    x.strokeStyle='#e2e8f0';x.setLineDash([2,3]);x.beginPath();x.moveTo(hover.px,pad.t);x.lineTo(hover.px,pad.t+ih);x.stroke();x.setLineDash([])}
}
function drawDD(curve,limitPct){
  const g=setup('ddchart',$('ddchart')&&$('ddchart').clientHeight>0?$('ddchart').clientHeight:130);if(!g)return;const {x,W,H}=g;
  const pts=(curve||[]).filter(p=>isNum(p.percent));
  if(pts.length<2){emptyMsg(g,'Drawdown : pas encore de données');return}
  const pad={l:64,r:12,t:10,b:8};const iw=W-pad.l-pad.r,ih=H-pad.t-pad.b;
  const lim=isNum(limitPct)?-Math.abs(limitPct):null;
  const vals=pts.map(p=>p.percent);
  let lo=Math.min(0,...vals,lim==null?0:lim),hi=Math.max(0,...vals);if(hi===lo){hi+=1}
  const m=(hi-lo)*0.08;lo-=m;hi+=m;
  const y=v=>pad.t+ih*(1-(v-lo)/(hi-lo));const xs=i=>pad.l+iw*(i/(pts.length-1));
  x.strokeStyle='#334155';x.beginPath();x.moveTo(pad.l,y(0));x.lineTo(W-pad.r,y(0));x.stroke();
  x.fillStyle='#94a3b8';x.textAlign='right';x.fillText('0 %',pad.l-6,y(0)+4);
  if(lim!=null){x.strokeStyle='#f87171';x.setLineDash([5,4]);x.beginPath();x.moveTo(pad.l,y(lim));x.lineTo(W-pad.r,y(lim));x.stroke();x.setLineDash([]);
    x.fillStyle='#f87171';x.fillText(lim.toLocaleString('fr-FR')+' %',pad.l-6,y(lim)+4)}
  x.beginPath();pts.forEach((p,i)=>{const px=xs(i),py=y(p.percent);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.strokeStyle='#a78bfa';x.lineWidth=2;x.stroke();
  const last=vals[vals.length-1];x.fillStyle='#e2e8f0';x.textAlign='left';x.fillText('actuel '+(last>=0?'+':'')+last.toLocaleString('fr-FR',{maximumFractionDigits:2})+' %',pad.l+4,pad.t+12);
}
function eqHover(ev){
  const c=$('eqchart'),tip=$('eqtip');if(!c||!tip||!EQ.pts.length)return;
  const r=c.getBoundingClientRect();const cx=(ev.touches&&ev.touches[0]?ev.touches[0].clientX:ev.clientX)-r.left;
  let best=null;EQ.pts.forEach(q=>{if(!best||Math.abs(q.px-cx)<Math.abs(best.px-cx))best=q});
  if(!best)return;
  drawEquity(DATA.equity_curve,DATA.floors,best);
  const p=best.p;tip.hidden=false;
  tip.innerHTML=esc(paris(p.ts,true))+' · equity '+esc(money(p.equity))+(p.symbol?'<br>'+esc(p.symbol)+' '+esc(fmt(p.pnl)):'<br>solde initial');
  const box=c.parentElement.getBoundingClientRect();
  let left=best.px+12;if(left+180>box.width)left=Math.max(0,best.px-190);
  tip.style.left=left+'px';tip.style.top=Math.max(0,best.py-10)+'px';
}
function eqLeave(){const tip=$('eqtip');if(tip)tip.hidden=true;if(DATA)drawEquity(DATA.equity_curve,DATA.floors,null)}
function drawCons(id,txtId,cons){
  const g=setup(id,$(id)&&$(id).clientHeight>0?$(id).clientHeight:240);if(!g)return;const {x,W,H}=g;
  const txt=$(txtId);
  if(!cons||!(cons.ideas||[]).length||!(cons.total_profit>0)){emptyMsg(g,cons&&cons.detail?cons.detail:'Pas encore de données');if(txt)txt.textContent='';return}
  const rows=cons.ideas.filter(i=>i.pnl>0).slice(0,12);const max=isNum(cons.max_share_percent)?cons.max_share_percent:25;
  if(!rows.length){emptyMsg(g,'Aucune idée gagnante sur le cycle');if(txt)txt.textContent='';return}
  const pad={l:110,r:16,t:10,b:8};const iw=W-pad.l-pad.r;const rh=Math.min(22,(H-pad.t-pad.b)/rows.length);
  const lim=Math.max(max*2,...rows.map(r=>isNum(r.share_percent)?r.share_percent:0),100/rows.length);
  const xs=v=>pad.l+iw*Math.min(1,v/lim);
  rows.forEach((r,i)=>{const y=pad.t+i*rh;const sp=isNum(r.share_percent)?r.share_percent:0;const w=xs(sp)-pad.l;
    x.fillStyle=sp>max?'#f87171':'#34d399';x.fillRect(pad.l,y+3,Math.max(2,w),rh-6);
    x.fillStyle='#e2e8f0';x.textAlign='right';x.fillText(`${r.symbol} ${r.side==='BUY'?'▲':'▼'}`,pad.l-8,y+rh/2+4);
    x.textAlign='left';x.fillText(`${sp.toFixed(1)} %  (${fmt(r.pnl)})`,pad.l+Math.max(2,w)+6,y+rh/2+4)});
  x.strokeStyle='#fbbf24';x.setLineDash([4,3]);x.beginPath();x.moveTo(xs(max),pad.t);x.lineTo(xs(max),pad.t+rows.length*rh);x.stroke();x.setLineDash([]);
  x.fillStyle='#fbbf24';x.textAlign='left';x.fillText(`limite ${max} %`,xs(max)+4,pad.t+rows.length*rh+10>H?H-4:pad.t+rows.length*rh+10);
  if(txt)txt.textContent=`${cons.detail||''} · ${cons.n_ideas} idée(s) · ${cons.within_limit?'dans la limite ✅':'au-dessus de la limite ⚠️ : continuer à trader jusqu\'au retour sous '+max+' %'}`;
}
/* ---------- cycle de paiement raconté ---------- */
function drawPay(){
  const el=$('paybox');if(!el)return;const p=DATA.payout_cycle;
  if(!p){el.innerHTML='<p class="mut">Cycle de paiement indisponible (état non lisible).</p>';return}
  const done=isNum(p.trading_days_done)?p.trading_days_done:0,req=isNum(p.trading_days_required)?p.trading_days_required:0;
  const rem=isNum(p.days_remaining)?p.days_remaining:Math.max(0,req-done);
  const lim=DATA.limits||{};const share=isNum(lim.consistency_max_share_percent)?lim.consistency_max_share_percent:25;
  const capPct=isNum(lim.max_withdrawal_percent_per_cycle)?lim.max_withdrawal_percent_per_cycle:15;
  const reasons=p.blocking_reasons||[];
  const violation=reasons.some(r=>/limite dure|violation/i.test(String(r)));
  const ok=b=>b?'✅':'❌';
  const conds=[
    [done>=req&&req>0,`Jours de trading : ${done} / ${req}`+(rem>0?` — reste ${rem} jour${rem>1?'s':''}`:'')],
    [isNum(p.cycle_profit)&&p.cycle_profit>0,`Solde > solde initial : profit du cycle ${fmt(p.cycle_profit)}`],
    [p.consistency_ok!==false,`Cohérence ≤ ${share} % : meilleure idée ${pct(p.consistency_share_percent)} du profit du cycle`],
    [!(p.open_positions>0),`Aucune position ouverte (${isNum(p.open_positions)?p.open_positions:'?'})`],
    [!violation,'Pas de violation de la limite de perte totale']];
  const amount=p.eligible?p.withdrawable:Math.max(0,Math.min(isNum(p.cycle_profit)?p.cycle_profit:0,isNum(p.withdrawal_cap)?p.withdrawal_cap:0));
  const net=(isNum(amount)&&isNum(p.profit_split_percent))?amount*p.profit_split_percent/100:null;
  const pays=DATA.payouts||[];
  const hist=pays.length?'<div class="tablewrap"><table><thead><tr><th>Date</th><th>Montant</th><th>Type</th><th>Jours</th></tr></thead><tbody>'+
    pays.slice().reverse().map(q=>`<tr><td>${esc(paris(q.ts,true))}</td><td>${esc(money(q.amount))}</td><td>${q.simulated?'simulé (démo)':'réel'}</td><td>${esc(q.trading_days)}</td></tr>`).join('')+'</tbody></table></div>'
    :'<p class="mut">Historique : aucun paiement encore.</p>';
  el.innerHTML=`<p><b>${p.window_open?'Fenêtre de paiement ouverte (positions fermées)':p.ready?'Paiement prêt':p.eligible?'Éligible — en attente de la fermeture des positions':'Cycle en cours'}</b>`+
    ` · cycle n°${isNum(p.payout_index)?p.payout_index+1:'?'}${p.cycle_started_at&&p.cycle_started_at!=='origine'?' depuis le '+esc(paris(p.cycle_started_at,true)):''}</p>`+
    `<div class="progress"><div style="width:${req>0?Math.min(100,100*done/req).toFixed(0):0}%"></div></div>`+
    `<p>Jour ${done} / ${req} requis${rem>0?` — reste ${rem} jour${rem>1?'s':''} de trading`:' — jours de trading atteints'}</p>`+
    '<ul class="conds">'+conds.map(([b,t])=>`<li>${ok(b)} ${esc(t)}</li>`).join('')+'</ul>'+
    `<p>Montant à retirer si tout est vert : min(profit ${fmt(p.cycle_profit)}, plafond ${capPct} % = ${money(p.withdrawal_cap)}) = <b>${money(amount)}</b></p>`+
    `<p>Part trader ${isNum(p.profit_split_percent)?p.profit_split_percent:'?'} % → net ≈ <b>${money(net)}</b></p>`+
    (p.demo_as_funded?'<p class="warn">Retrait simulé automatiquement (compte démo traité comme financé).</p>':'')+
    (isNum(DATA.simulated_withdrawn_total)&&DATA.simulated_withdrawn_total>0?`<p class="mut">Retraits simulés cumulés : ${esc(money(DATA.simulated_withdrawn_total))}</p>`:'')+
    hist;
}
/* ---------- classes d'actifs ---------- */
function renderClasses(){
  const el=$('classes');if(!el)return;const rows=DATA.by_asset_class||[];
  if(!rows.length){el.innerHTML='<p class="mut">Pas encore de trade fermé.</p>';return}
  const mx=Math.max(1,...rows.map(r=>Math.abs(isNum(r.pnl)?r.pnl:0)));
  el.innerHTML=rows.map(r=>{const v=isNum(r.pnl)?r.pnl:0;const w=Math.max(2,100*Math.abs(v)/mx);
    return `<div class="row"><span>${esc(r.asset_class)}</span><div class="bar"><div style="width:${w.toFixed(0)}%;background:${v>=0?'#34d399':'#f87171'}"></div></div>`+
      `<span class="${cls(v)}">${esc(fmt(v))} <span class="mut">· ${esc(r.trades)} trade${r.trades>1?'s':''} · ${esc(pct(r.win_rate))}</span></span></div>`}).join('');
}
/* ---------- vue maître ---------- */
function rowHtml(label,r){return `<tr><td>${esc(label)}</td><td>${esc(r.n)}</td><td>${esc(pct(r.win_rate))}</td><td class="hide-sm ${cls(r.r)}">${esc(rfmt(r.r))}</td><td class="${cls(r.pnl)}">${esc(fmt(r.pnl))}</td></tr>`}
function releve(st,cur){
  if(!st)return '';
  const m=v=>esc(money(v));
  return `<h2>Relevé MT5 (historique réel du compte)</h2><div class="cards">`+
    `<div class="card"><small>Bénéfice</small><b class="${cls(st.profit)}">${esc(fmt(st.profit))}</b></div>`+
    `<div class="card"><small>Dépôt</small><b>${m(st.deposits)}</b>${st.withdrawals?`<span class="sub">retraits ${m(st.withdrawals)}</span>`:''}</div>`+
    `<div class="card"><small>Swap</small><b class="${cls(st.swap)}">${esc(fmt(st.swap))}</b></div>`+
    `<div class="card"><small>Commission</small><b class="${cls(st.commission)}">${esc(fmt(st.commission))}</b></div>`+
    `<div class="card"><small>Résultat net</small><b class="${cls(st.net)}">${esc(fmt(st.net))}</b><span class="sub">bénéfice + swap + commission</span></div>`+
    `<div class="card"><small>Solde</small><b>${m(st.balance)}</b>${isNum(st.equity)?`<span class="sub">equity ${m(st.equity)}</span>`:''}</div></div>`;
}
function render(){
  const t=DATA.total||{};
  $('releve').innerHTML=releve(DATA.statement);
  $('cards').innerHTML=
    `<div class="card"><small>P&L total fermé</small><b class="${cls(t.pnl)}">${esc(fmt(t.pnl))}</b></div>`+
    `<div class="card"><small>Trades</small><b>${esc(isNum(t.trades)?t.trades:'—')}</b></div>`+
    `<div class="card"><small>Taux de réussite</small><b>${esc(pct(t.win_rate))}</b></div>`+
    `<div class="card"><small>R total</small><b class="${cls(t.r)}">${esc(rfmt(t.r))}</b></div>`+
    `<div class="card"><small>Meilleur trade</small><b class="pos">${esc(fmt(t.best))}</b></div>`+
    `<div class="card"><small>Pire trade</small><b class="neg">${esc(fmt(t.worst))}</b></div>`;
  const rows=DATA[period]||[];
  document.querySelector('#ptable tbody').innerHTML=rows.slice().reverse().map(r=>rowHtml(r.key,r)).join('')||'<tr><td colspan="5" class="mut">aucun trade fermé</td></tr>';
  document.querySelector('#atable tbody').innerHTML=(DATA.agents||[]).map(a=>rowHtml(a.agent,a)).join('')||'<tr><td colspan="5" class="mut">aucun trade fermé</td></tr>';
  document.querySelector('#stable tbody').innerHTML=(DATA.symbols||[]).map(a=>rowHtml(a.symbol,a)).join('')||'<tr><td colspan="5" class="mut">aucun trade fermé</td></tr>';
  const f=DATA.floors||{};
  $('eqnote').textContent='Solde initial '+money(DATA.initial_balance)+' + cumul des trades fermés (learning.db) · plancher jour '+money(f.daily_floor)+' · plancher global '+money(f.overall_floor)+' · touchez la courbe pour le détail';
  draw();
}
function draw(){drawBars('chart',DATA[period]);drawEquity(DATA.equity_curve,DATA.floors,null);drawDD(DATA.drawdown_curve,(DATA.limits||{}).max_overall_loss_hard_percent);
  drawCons('cons','constxt',DATA.consistency);drawPay();renderClasses()}
function selectTab(container,attr,value){container.querySelectorAll('button').forEach(b=>{const on=b.dataset[attr]===value;b.classList.toggle('on',on);b.setAttribute('aria-selected',on?'true':'false')})}
$('tabs').addEventListener('click',e=>{const b=e.target.closest?e.target.closest('button'):e.target;if(b&&b.dataset&&b.dataset.p){period=b.dataset.p;selectTab($('tabs'),'p',period);render()}});
/* ---------- comptes suiveurs ---------- */
function renderAccounts(){
  const bar=$('atabs');const accs=DATA.accounts||[];
  if(!accs.length){bar.hidden=true;acct='master';return}
  if(acct!=='master'&&!accs.some(a=>a.env_prefix===acct))acct='master';   // l'onglet actif a disparu → repli Maître
  bar.hidden=false;
  bar.innerHTML=tabsHtml([['master','🏦 Maître']].concat(accs.map(a=>[a.env_prefix,'👤 '+esc(a.name)])),'a',acct);
}
function groupActions(actions){
  const out=[];(actions||[]).forEach(x=>{const prev=out[out.length-1];
    if(prev&&prev.kind===x.kind&&prev.symbol===x.symbol&&prev.reason===x.reason){prev.n++;prev.last=x.ts;prev.volume=x.volume;return}
    out.push(Object.assign({n:1,last:x.ts},x))});
  return out;
}
function renderFollower(a){
  const fv=$('fview');const s=a&&a.status;
  if(!s){fv.innerHTML=`<div class="chartbox"><p class="mut">Le copieur de « ${esc(a?a.name:acct)} » n'a pas encore démarré
    (il se lance avec le bot). État de la configuration : ${a&&a.creds_ok?'identifiants ✅':'identifiants ⚠️'} · ${a&&a.terminal_ok?'terminal ✅':'terminal ⚠️'}</p></div>`;return}
  const tsync=normTs(s.ts_utc);const age=Number.isFinite(tsync)?Math.max(0,Math.round((Date.now()-tsync)/1000)):null;
  const fs=a.stats||{total:{trades:0,pnl:0,win_rate:0,best:0,worst:0},daily:[],symbols:[],consistency:null};
  const ft=fs.total||{};
  const floating=(isNum(s.equity)&&isNum(s.balance))?s.equity-s.balance:null;
  const lastDay=(fs.daily||[])[fs.daily.length-1];
  const todayKey=DATA.trading_day;const todayPnl=(lastDay&&lastDay.key===todayKey)?lastDay.pnl:0;
  const todayNote=(lastDay&&lastDay.key===todayKey)?`${lastDay.n} trade${lastDay.n>1?'s':''} fermé${lastDay.n>1?'s':''}`:'aucun trade fermé aujourd\'hui';
  const mt=(DATA.total||{}).pnl;const factor=isNum(s.size_factor)?s.size_factor:(isNum(a.size_factor)?a.size_factor:null);
  const ecart=(isNum(ft.pnl)&&isNum(mt)&&isNum(factor)&&mt*factor!==0)?100*ft.pnl/(mt*factor):null;
  const failed=a.failed_copies||[];
  const failedHtml=failed.length?`<h2 class="neg">⛔ Réplications en échec (journal du jour, ${failed.length} ligne${failed.length>1?'s':''})</h2>
  <div class="tablewrap fail"><table><thead><tr><th>Heure</th><th>Paire</th><th>Sens</th><th>Volume</th><th>Erreur</th></tr></thead><tbody>
  ${failed.slice().reverse().map(x=>`<tr><td>${esc(paris(x.last_ts||x.ts))}${x.n>1?' <span class="mut">×'+esc(x.n)+'</span>':''}</td><td>${esc(x.symbol||(x.ticket!=null?'ticket '+x.ticket:''))}${x.master_symbol&&x.master_symbol!==x.symbol?' <span class="mut">('+esc(x.master_symbol)+')</span>':''}</td><td>${esc(x.side||'')}</td><td>${esc(isNum(x.volume)?x.volume:'')}</td><td>${esc(x.message||'')}${x.detail?' — '+esc(x.detail):''}${isNum(x.retcode)?' <span class="mut">(code '+esc(x.retcode)+')</span>':''}${isNum(x.retry_in_sec)?' <span class="mut">· nouvel essai dans '+esc(Math.round(x.retry_in_sec))+' s</span>':''}</td></tr>`).join('')}
  </tbody></table></div>`:'<p class="mut">Aucune réplication en échec dans les derniers événements du journal du jour.</p>';
  const acts=groupActions(s.actions||[]).reverse();
  fv.innerHTML=failedHtml+releve(s.statement)+`<div class="cards">
    <div class="card"><small>Equity</small><b>${esc(money(s.equity))}</b></div>
    <div class="card"><small>Solde</small><b>${esc(money(s.balance))}</b></div>
    <div class="card"><small>Flottant</small><b class="${cls(floating)}">${esc(fmt(floating))}</b><span class="sub">equity − solde</span></div>
    <div class="card"><small>P&L fermé aujourd'hui</small><b class="${cls(todayPnl)}">${esc(fmt(todayPnl))}</b><span class="sub">${esc(todayNote)}</span></div>
    <div class="card"><small>P&L fermé (90 j)</small><b class="${cls(ft.pnl)}">${esc(fmt(ft.pnl))}</b></div>
    <div class="card"><small>Trades fermés</small><b>${esc(isNum(ft.trades)?ft.trades:'—')}</b></div>
    <div class="card"><small>Taux de réussite</small><b>${esc(pct(ft.win_rate))}</b></div>
    <div class="card"><small>Écart vs maître</small><b>${esc(isNum(ecart)?pct(ecart,0):'—')}</b><span class="sub">P&L fermé du compte / (P&L fermé maître × facteur${isNum(factor)?' '+factor:''}) — périodes différentes</span></div>
    <div class="card"><small>Meilleur / pire</small><b><span class="pos">${esc(fmt(ft.best))}</span> / <span class="neg">${esc(fmt(ft.worst))}</span></b></div>
    <div class="card"><small>Positions copiées</small><b>${(s.positions||[]).length}</b></div>
    <div class="card"><small>Facteur</small><b>×${esc(isNum(factor)?factor:'?')}</b></div>
    <div class="card"><small>Dernière synchro</small><b class="${age==null?'mut':(age>120?'neg':'pos')}">${age==null?'inconnue':age+' s'}</b><span class="sub">${esc(paris(s.ts_utc))}</span></div></div>
  <div class="tabs" id="ftabs" role="tablist" aria-label="Période du compte">${tabsHtml(Object.entries(PERIODS),'fp',fperiod)}</div>
  <div class="chartbox"><canvas id="fchart" aria-label="P&L fermé du compte par période"></canvas></div>
  <h2>Cohérence 25 % — part de chaque idée de trade dans le profit total du compte</h2>
  <div class="chartbox"><canvas id="fcons" aria-label="Part de chaque idée de trade dans le profit du compte"></canvas><p id="fconstxt" class="note"></p></div>
  <h2>Par période</h2>
  <div class="tablewrap"><table><thead><tr><th>Période</th><th>Trades</th><th>Taux de réussite</th><th>P&L</th></tr></thead><tbody>
  ${(fs[fperiod]||[]).slice().reverse().map(r=>`<tr><td>${esc(r.key)}</td><td>${esc(r.n)}</td><td>${esc(pct(r.win_rate))}</td><td class="${cls(r.pnl)}">${esc(fmt(r.pnl))}</td></tr>`).join('')||'<tr><td colspan="4" class="mut">aucun trade fermé</td></tr>'}
  </tbody></table></div>
  <h2>Par paire</h2>
  <div class="tablewrap"><table><thead><tr><th>Paire</th><th>Trades</th><th>Taux de réussite</th><th>P&L</th></tr></thead><tbody>
  ${(fs.symbols||[]).map(r=>`<tr><td>${esc(r.symbol)}</td><td>${esc(r.n)}</td><td>${esc(pct(r.win_rate))}</td><td class="${cls(r.pnl)}">${esc(fmt(r.pnl))}</td></tr>`).join('')||'<tr><td colspan="4" class="mut">aucun trade fermé</td></tr>'}
  </tbody></table></div>
  <h2>Positions répliquées</h2>
  <div class="tablewrap"><table><thead><tr><th>Paire</th><th>Sens</th><th>Volume</th><th>Stop</th><th>P&L flottant</th></tr></thead><tbody>
  ${(s.positions||[]).map(p=>`<tr><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td><td>${esc(p.volume)}</td><td>${esc(p.sl)}</td><td class="${cls(p.profit)}">${esc(fmt(p.profit))}</td></tr>`).join('')||'<tr><td colspan="5" class="mut">aucune position copiée</td></tr>'}
  </tbody></table></div>
  <h2>Dernières réplications</h2>
  <div class="tablewrap"><table><thead><tr><th>Heure</th><th>Action</th><th>Paire</th><th>Volume</th><th>Motif</th></tr></thead><tbody>
  ${acts.map(x=>`<tr><td>${esc(paris(x.last))}${x.n>1?' <span class="mut">×'+esc(x.n)+' (depuis '+esc(paris(x.ts))+')</span>':''}</td><td>${esc(x.kind)}</td><td>${esc(x.symbol||'')}</td><td>${esc(isNum(x.volume)&&x.volume>0?x.volume:'')}</td><td>${esc(x.reason||'')}</td></tr>`).join('')||'<tr><td colspan="5" class="mut">rien encore</td></tr>'}
  </tbody></table></div>`;
  drawBars('fchart',fs[fperiod]);drawCons('fcons','fconstxt',fs.consistency);
  const ft2=$('ftabs');if(ft2)ft2.addEventListener('click',ev=>{const b=ev.target.closest?ev.target.closest('button'):ev.target;if(b&&b.dataset&&b.dataset.fp){fperiod=b.dataset.fp;showAccount()}});
}
function showAccount(){
  const master=acct==='master';
  $('mview').hidden=!master;$('fview').hidden=master;
  if(master){render();return}
  renderFollower((DATA.accounts||[]).find(x=>x.env_prefix===acct));
}
$('atabs').addEventListener('click',e=>{const b=e.target.closest?e.target.closest('button'):e.target;if(!b||!b.dataset||!b.dataset.a)return;acct=b.dataset.a;renderAccounts();showAccount()});
window.addEventListener('resize',()=>{if(!DATA)return;if(acct==='master')draw();else showAccount()});
(function(){const c=$('eqchart');if(!c||!c.addEventListener)return;c.addEventListener('pointermove',eqHover);c.addEventListener('pointerdown',eqHover);c.addEventListener('touchstart',eqHover,{passive:true});c.addEventListener('touchmove',eqHover,{passive:true});c.addEventListener('pointerleave',eqLeave)})();
function refreshAll(){
  fetch('/api/stats',{cache:'no-store'}).then(r=>{
    if(r.status===401){showErr('Statistiques indisponibles (HTTP 401) — <a href="/login">se reconnecter</a>');throw new Error('401')}
    if(!r.ok){showErr('Statistiques indisponibles (HTTP '+r.status+')');throw new Error('HTTP '+r.status)}
    return r.json()}).then(d=>{DATA=d;CUR=d.currency||'';showErr('');renderAccounts();showAccount()})
  .catch(e=>{if(!$('err')||$('err').hidden)showErr('Statistiques indisponibles : '+esc(e.message||e))});
}
refreshAll();setInterval(refreshAll,60000);
</script></body></html>"""


SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
)
API_POST_ROUTES = ("/api/command", "/api/copy/follower", "/api/restart", "/api/copy/factor", "/api/copy/remove",
                   "/api/shadow/live")


class DashboardHandler(BaseHTTPRequestHandler):
    # aucune version divulguée (ni du serveur, ni de Python)
    server_version = "TradingLabDashboard"
    sys_version = ""
    server: DashboardServer  # type: ignore[assignment]

    # -- utilitaires --
    def _client_ip(self) -> str:
        try:
            return str(self.client_address[0])
        except (TypeError, IndexError):
            return "?"

    def _accepts_gzip(self) -> bool:
        enc = self.headers.get("Accept-Encoding", "")
        return any(part.strip().split(";")[0].strip().lower() == "gzip" for part in enc.split(","))

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        if getattr(self.server, "tls_enabled", False):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if len(body) >= GZIP_MIN_BYTES and self._accepts_gzip():
            body = gzip.compress(body, compresslevel=5)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature imposée
        # Silencieux par défaut : le dashboard ne doit pas polluer les logs du système.
        pass

    # -- routes --
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _too_many_attempts(self) -> bool:
        """429 si l'IP a épuisé ses tentatives d'authentification (seulement quand un jeton est configuré)."""
        if not self.server.auth_token:
            return False
        wait = self.server.auth_wait_sec(self._client_ip())
        if wait <= 0:
            return False
        self._send_json({"error": f"trop de tentatives ; réessayez dans {int(math.ceil(wait))} s"},
                        HTTPStatus.TOO_MANY_REQUESTS, {"Retry-After": str(int(math.ceil(wait)))})
        return True

    def _authorized(self) -> bool:
        token = self.server.auth_token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        user, candidate = "", ""
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self.server.auth_failed(self._client_ip())
                return False
            user, _, candidate = decoded.partition(":")
        elif header.startswith("Bearer "):
            candidate = header[7:].strip()
        if not candidate:
            # cookie de session posé par la page /login (valeur = jeton) : évite la popup Basic,
            # que Chrome n'affiche pas toujours — même niveau de preuve que le Basic/Bearer
            cookies = self.headers.get("Cookie", "")
            for part in cookies.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "tladash":
                    candidate = v.strip()
                    break
            if candidate:
                ok = hmac.compare_digest(candidate, token)
                (self.server.auth_succeeded if ok else self.server.auth_failed)(self._client_ip())
                return ok
        if not candidate:
            return False                      # aucune preuve présentée : pas une « tentative »
        if not hmac.compare_digest(candidate, token):
            self.server.auth_failed(self._client_ip())
            return False
        # nom d'utilisateur exigé seulement s'il est configuré (DASHBOARD_AUTH_USER) et si le client
        # utilise Basic ; un Bearer valide reste accepté (usage script/monitoring)
        expected_user = self.server.auth_user
        if expected_user and header.startswith("Basic ") and not hmac.compare_digest(user, expected_user):
            self.server.auth_failed(self._client_ip())
            return False
        self.server.auth_succeeded(self._client_ip())
        return True

    def _logout(self) -> None:
        """Efface le cookie de session et renvoie vers /login (accessible sans authentification)."""
        self.send_response(HTTPStatus.SEE_OTHER)
        cookie = "tladash=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        if getattr(self.server, "tls_enabled", False):
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Location", "/login" if self.server.auth_token else "/")
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/logout":
            self._logout()
            return
        if self._too_many_attempts():
            return
        if not self._authorized():
            if parts.path in ("/", "/index.html", "/login", "/control", "/stats", "/qualite", "/shadow"):
                self._send_html(LOGIN_HTML.replace("__ERR__", ""))
                return
            self._send_json({"error": "authentification requise"}, HTTPStatus.UNAUTHORIZED,
                            {"WWW-Authenticate": 'Basic realm="TradingLab Dashboard", charset="UTF-8"'})
            return
        query = parse_qs(parts.query)
        try:
            if parts.path in ("/", "/index.html"):
                self._send_html(INDEX_HTML)
            elif parts.path == "/login":
                if self.server.auth_token:
                    self._send_html(LOGIN_HTML.replace("__ERR__", ""))
                else:
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            elif parts.path == "/control":
                self._send_html(CONTROL_HTML)
            elif parts.path == "/stats":
                self._send_html(STATS_HTML)
            elif parts.path == "/api/stats":
                self._send_json(self.server.data.stats())
            elif parts.path == "/qualite":
                self._send_html(QUALITE_HTML)
            elif parts.path == "/shadow":
                self._send_html(SHADOW_HTML)
            elif parts.path == "/api/shadow":
                self._send_json(self.server.data.shadow())
            elif parts.path == "/api/quality":
                self._send_json(self.server.data.quality(gel_only=(query.get("periode") or [""])[0] == "gel"))
            elif parts.path == "/api/copy/status":
                from ..copy.registry import followers_status

                self._send_json({"followers": followers_status(self.server.data.settings.home)})
            elif parts.path == "/api/state":
                kinds = self._kinds(query)
                self._send_json(self.server.data.snapshot(kinds))
            elif parts.path == "/api/journal":
                day = (query.get("day") or [None])[0]
                if day and not _DAY_RE.match(day):
                    self._send_json({"error": "paramètre day invalide (YYYY-MM-DD attendu)"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    limit = int((query.get("limit") or [JOURNAL_API_DEFAULT_LIMIT])[0])
                    offset = int((query.get("offset") or [0])[0])
                except ValueError:
                    self._send_json({"error": "paramètres limit/offset invalides (entiers attendus)"}, HTTPStatus.BAD_REQUEST)
                    return
                events, total = self.server.data.journal(day, self._kinds(query), limit, offset)
                self._send_json(events, extra={"X-Total-Count": str(total)})
            elif parts.path == "/health":
                self._send_json({"ok": True, "read_only": False, "auth": bool(self.server.auth_token),
                                 "tls": bool(getattr(self.server, "tls_enabled", False)),
                                 "server_time_utc": utcnow().isoformat()})
            else:
                self._send_json({"error": "route inconnue"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - défense : jamais de trace de pile vers le client
            self._send_json({"error": f"erreur interne: {type(exc).__name__}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    @staticmethod
    def _kinds(query: dict) -> set[str] | None:
        kinds_raw = ",".join(query.get("kinds", []))
        return {k.strip() for k in kinds_raw.split(",") if k.strip()} or None

    def _refuse(self) -> None:
        self._send_json({"error": "dashboard en lecture seule : méthode refusée"}, HTTPStatus.METHOD_NOT_ALLOWED)

    def do_POST(self) -> None:  # noqa: N802 - POST acceptés : /login (cookie) et les routes /api/* du panneau
        path = urlsplit(self.path).path
        if self._too_many_attempts():
            return
        if path in API_POST_ROUTES:
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send_json({"ok": False, "error": "Content-Type: application/json requis"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            {"/api/command": self._post_command, "/api/copy/follower": self._post_follower,
             "/api/restart": self._post_restart, "/api/copy/factor": self._post_factor,
             "/api/copy/remove": self._post_remove_follower, "/api/shadow/live": self._post_shadow_live}[path]()
            return
        if path != "/login" or self.server.auth_token is None:
            self._refuse()
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            form = {}
        user = (form.get("user") or [""])[0]
        password = (form.get("password") or [""])[0]
        expected_user = self.server.auth_user
        ok = bool(password) and hmac.compare_digest(password, self.server.auth_token)
        if ok and expected_user:
            ok = hmac.compare_digest(user, expected_user)
        if not ok:
            self.server.auth_failed(self._client_ip())
            self._send_html(LOGIN_HTML.replace("__ERR__", '<div class="err">Identifiants incorrects.</div>'), HTTPStatus.UNAUTHORIZED)
            return
        self.server.auth_succeeded(self._client_ip())
        self.send_response(HTTPStatus.SEE_OTHER)
        cookie = f"tladash={self.server.auth_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
        if getattr(self.server, "tls_enabled", False):
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _post_command(self) -> None:
        """Panneau de contrôle (2026-09-22, demande utilisateur) : mêmes commandes d'exploitation que la CLI,
        même file `state/commands.jsonl`, même journalisation — aucune écriture directe sur l'état ni le
        broker (sauf le chemin d'urgence PANIC/CLOSE* si l'orchestrateur est mort, identique à la CLI)."""
        if not self._authorized():
            self._send_json({"ok": False, "error": "authentification requise"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            command = str(payload.get("command", "")).upper().strip()
            arg = payload.get("arg")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "corps JSON invalide"}, HTTPStatus.BAD_REQUEST)
            return
        if command not in PANEL_COMMANDS:
            self._send_json({"ok": False, "error": f"commande non autorisée depuis le panneau: {command or '(vide)'}"},
                            HTTPStatus.BAD_REQUEST)
            return
        if command in PANEL_AMOUNT_COMMANDS:
            try:
                amount = float(str(arg if arg is not None else "").replace(",", ".").strip())
                if not math.isfinite(amount) or amount < 0:
                    raise ValueError
            except ValueError:
                self._send_json({"ok": False, "error": f"{command} : montant numérique ≥ 0 requis (0 = renoncer)"}, HTTPStatus.BAD_REQUEST)
                return
            arg = f"{amount:.2f}"
        try:
            from ..api.commands import CommandHandler

            out = CommandHandler(self.server.data.settings, source="dashboard").run(command, str(arg) if arg else None)
        except Exception as exc:  # noqa: BLE001 - jamais de trace de pile vers le client
            self._send_json({"ok": False, "error": f"erreur interne: {type(exc).__name__}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(out)

    def _post_follower(self) -> None:
        """Inscription d'un compte suiveur depuis le panneau (2026-09-22). Les identifiants transitent une
        seule fois (HTTPS + session) vers le .env local ; ils ne sont jamais renvoyés ni journalisés."""
        if not self._authorized():
            self._send_json({"ok": False, "error": "authentification requise"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 8192)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "corps JSON invalide"}, HTTPStatus.BAD_REQUEST)
            return
        from ..copy.registry import register_follower

        try:
            out = register_follower(self.server.data.settings.home,
                                    payload.get("name"), payload.get("login"), payload.get("password"),
                                    payload.get("server"), payload.get("terminal_path"),
                                    payload.get("size_factor", 1.0))
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"erreur interne: {type(exc).__name__}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "follower": out,
                         "note": "Compte enregistré. Le copieur démarrera au prochain redémarrage du bot — demande à l'assistant, ou relance scripts/start_all.ps1."})

    def _post_restart(self) -> None:
        """Redémarre tout le lab dans le mode courant (AUTO reste AUTO : c'est l'utilisateur qui clique)."""
        if not self._authorized():
            self._send_json({"ok": False, "error": "authentification requise"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            snap = self.server.data.snapshot(None)
            mode = "AUTO" if str(snap.get("mode", "")).upper() == "AUTO" else "SAFE"
        except Exception:  # noqa: BLE001
            mode = "SAFE"
        try:
            RESTART_LAUNCHER(self.server.data.settings.home, mode)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"lancement impossible: {type(exc).__name__}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "mode": mode,
                         "note": f"Redémarrage lancé en {mode} — le dashboard revient dans ~60 à 90 s."})

    def _post_factor(self) -> None:
        """Facteur de taille d'un suiveur, modifiable depuis le panneau (2026-09-22) — appliqué à chaud."""
        if not self._authorized():
            self._send_json({"ok": False, "error": "authentification requise"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "corps JSON invalide"}, HTTPStatus.BAD_REQUEST)
            return
        from ..copy.registry import set_size_factor

        try:
            out = set_size_factor(self.server.data.settings.home, str(payload.get("name", "")), payload.get("size_factor"))
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, **out, "note": "Facteur mis à jour — appliqué aux prochaines ouvertures (les positions déjà copiées gardent leur taille)."})

    def _post_shadow_live(self) -> None:
        """Validation, depuis la page Shadow, du passage en LIVE d'un agent prêt (2026-10-01, demande utilisateur).
        Le verdict est recalculé côté serveur ; la demande passe par `state/agent_status_requests.jsonl`, appliquée par
        l'orchestrateur (seul écrivain du registre) à son cycle suivant. Jamais d'ordre, jamais de réglage modifié."""
        if not self._authorized():
            self._send_json({"ok": False, "error": "authentification requise"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            agent_id = str(payload.get("agent_id", ""))
        except (ValueError, UnicodeDecodeError, AttributeError):
            self._send_json({"ok": False, "error": "corps JSON invalide"}, HTTPStatus.BAD_REQUEST)
            return
        from ..learning.shadow_board import demander_passage_live

        data = self.server.data
        try:
            out = demander_passage_live(data.home, dict(data.settings.learning or {}), agent_id)
        except Exception as exc:  # noqa: BLE001 - jamais de trace de pile vers le client
            self._send_json({"ok": False, "error": f"erreur interne: {type(exc).__name__}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if out.get("ok"):
            data._shadow_cache = None                     # la page relue affiche « demandé » tout de suite
            try:
                from ..core.journal import Journal

                Journal(data.home / "logs", component="dashboard").event("agent_live_demande", agent_id=out["agent_id"],
                                                                         reason=out.get("raison", ""))
            except Exception:  # noqa: BLE001 - la demande est écrite ; le journal ne doit pas la faire échouer
                pass
        self._send_json(out, HTTPStatus.OK if out.get("ok") else HTTPStatus.BAD_REQUEST)

    def _post_remove_follower(self) -> None:
        """Suppression complète d'un compte suiveur depuis le panneau (2026-09-24), après confirmation côté page :
        config, fichiers d'état et identifiants .env du suiveur. Les positions déjà copiées ne sont pas fermées."""
        if not self._authorized():
            self._send_json({"ok": False, "error": "authentification requise"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "corps JSON invalide"}, HTTPStatus.BAD_REQUEST)
            return
        if payload.get("confirm") is not True:
            self._send_json({"ok": False, "error": "confirmation requise"}, HTTPStatus.BAD_REQUEST)
            return
        from ..copy.registry import remove_follower

        try:
            out = remove_follower(self.server.data.settings.home, str(payload.get("name", "")))
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, **out, "note": "Compte supprimé — le bot redémarre pour arrêter son copieur. "
                                                  "Les positions déjà copiées restent ouvertes sur ce compte (stop et TP en place)."})

    do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815


def create_server(home: Path | None = None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  auth_token: str | None = None) -> ThreadingHTTPServer:
    """Construit le serveur (non démarré). ``port=0`` choisit un port libre."""
    home = Path(home) if home else project_home()
    if auth_token is None:
        auth_token = os.environ.get("DASHBOARD_AUTH_TOKEN")
    srv = DashboardServer((host, int(port)), DashboardData(home), auth_token=auth_token)
    _maybe_wrap_tls(srv, home)
    return srv


def _maybe_wrap_tls(srv: "DashboardServer", home: Path) -> None:
    """HTTPS (2026-09-22, exposition volontaire derrière un DynDNS) : certificat auto-signé généré une fois
    dans state/dashboard_cert.pem + dashboard_key.pem et réutilisé ensuite. Sans lui, le mot de passe Basic
    voyagerait EN CLAIR sur Internet. Activé par DASHBOARD_TLS=1 ; le certificat étant auto-signé, le
    navigateur affichera un avertissement à accepter une fois par appareil."""
    if str(os.environ.get("DASHBOARD_TLS", "")).strip() not in ("1", "true", "on", "yes"):
        return
    import ssl

    cert = home / "state" / "dashboard_cert.pem"
    key = home / "state" / "dashboard_key.pem"
    if not (cert.exists() and key.exists()):
        _generate_self_signed(cert, key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.tls_enabled = True


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tradinglab-dashboard")])
    now = utcnow()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboard local en lecture seule (Claude MT5 Trading Lab).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port d'écoute (défaut {DEFAULT_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"hôte d'écoute (défaut {DEFAULT_HOST})")
    parser.add_argument("--home", type=Path, default=None, help="racine du projet (défaut : TRADINGLAB_HOME ou le dépôt)")
    args = parser.parse_args(argv)
    token_configured = bool((os.environ.get("DASHBOARD_AUTH_TOKEN") or "").strip())
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token_configured:
        print(f"AVERTISSEMENT : hôte {args.host} non local et aucun jeton DASHBOARD_AUTH_TOKEN : le dashboard n'est pas authentifié.",
              file=sys.stderr)
    server = create_server(args.home, args.host, args.port)
    host, port = server.server_address[:2]
    scheme = "https" if getattr(server, "tls_enabled", False) else "http"
    print(f"Dashboard : {scheme}://{host}:{port}/  ({'authentifié' if token_configured else 'sans authentification'}) — Ctrl+C pour arrêter",
          file=sys.stderr)
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

# Page Qualité (plan pro du 2026-09-25, point 5) — même feuille de style que la page Statistiques
SHADOW_HTML = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trading Lab — Shadow &amp; recherche</title>
""" + STATS_HTML[STATS_HTML.index("<style>"):STATS_HTML.index("</style>") + len("</style>")] + r"""
<style>.verdict{font-size:.8rem;padding:.12rem .5rem;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.v-ok{color:#0f172a;background:var(--green);border-color:var(--green)}.v-ko{color:#0f172a;background:var(--red);border-color:var(--red)}
.v-val{color:var(--green)}.v-wait{color:var(--amber)}.v-few{color:var(--mut)}
.mini{display:inline-block;width:70px;height:8px;background:#0f172a;border:1px solid var(--line);border-radius:4px;overflow:hidden;vertical-align:middle}
.mini div{height:100%;background:var(--sky)}.filt{display:flex;gap:.5rem;flex-wrap:wrap;margin:.5rem 0}
.filt input,.filt select{background:var(--panel);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:.3rem .5rem}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.golive{background:var(--green);color:#0f172a;border:0;border-radius:6px;padding:.25rem .6rem;font-weight:700;cursor:pointer;min-height:32px;white-space:nowrap}
.golive.sec{background:transparent;color:var(--mut);border:1px solid var(--line);font-weight:400}
.golive:focus-visible{outline:2px solid var(--sky);outline-offset:2px}.golive:disabled{opacity:.6;cursor:wait}
.msg{margin:.5rem 0;padding:.5rem .75rem;border:1px solid var(--line);border-radius:8px;background:var(--panel)}</style></head><body><div class="wrap">
<h1>👻 Shadow &amp; recherche</h1>
<div class="crumb"><a href="/">← Tableau de bord</a> · <a href="/stats">Statistiques</a> · <a href="/qualite">Qualité</a> · <a href="/control">Panneau de contrôle</a> · trades shadow <b>clôturés</b> (positions virtuelles, aucun ordre), mise à jour toutes les 60 s</div>
<div id="err" class="errbox" hidden></div>
<div class="cards" id="cards"></div>
<div id="msg" class="msg" role="status" hidden></div>
<div class="paybox" id="regle"></div>
<h2>Par famille</h2>
<div class="tablewrap"><table><thead><tr><th>Famille</th><th class="num">Agents actifs</th><th class="num">Trades</th><th class="num">Résultat</th><th class="num">PF</th><th class="num hide-sm">Réussite</th><th class="num hide-sm">Ouvertes</th></tr></thead>
<tbody id="fams"><tr><td colspan="7" class="mut">Chargement…</td></tr></tbody></table></div>
<h2>Par agent</h2>
<div class="filt"><input id="q" placeholder="Filtrer (agent, stratégie)…" aria-label="Filtrer">
<select id="fst" aria-label="Statut"><option value="SHADOW">En shadow</option><option value="">Tous</option><option value="LIVE">Live</option><option value="SUSPENDED">Suspendus</option></select>
<select id="fv" aria-label="Verdict"><option value="">Tous les verdicts</option><option>prêt pour revue live</option><option>shadow validé</option><option>en cours</option><option>pas concluant</option><option>perdant</option></select></div>
<div class="tablewrap"><table><thead><tr><th>Agent</th><th class="hide-sm">Stratégie</th><th>Statut</th><th class="num">Trades</th><th class="num">Résultat</th><th class="num">PF</th><th class="num hide-sm">Espérance</th><th class="num hide-sm">Réussite</th><th class="num hide-sm">Ouvertes</th><th>Vers le live</th><th>Verdict</th><th>Décision</th></tr></thead>
<tbody id="agents"></tbody></table></div>
<h2>Recherche GPU</h2>
<div class="cards" id="rcards"></div>
<h3>Derniers passages</h3>
<div class="tablewrap"><table><thead><tr><th>Passage</th><th>Heure</th><th class="num">Testées</th><th class="num hide-sm">Seuil t</th><th class="num">Significatives</th><th class="num">Contrôle OK</th><th class="num">Idées nouvelles</th></tr></thead>
<tbody id="passages"></tbody></table></div>
<h3>Idées envoyées en shadow</h3>
<div class="tablewrap"><table><thead><tr><th>Agent</th><th>Stratégie</th><th>UT</th><th class="hide-sm">Marchés</th><th class="hide-sm">Session</th><th class="num">PF backtest</th><th class="num">PF contrôle</th><th class="hide-sm">Date</th></tr></thead>
<tbody id="idees"></tbody></table></div>
<p class="note">Résultat en R : multiples du risque pris (−1 R = un stop plein). PF = gains ÷ pertes (au-dessus de 1, l'agent gagne).
« Contrôle OK » : configurations qui tiennent aussi sur la période récente jamais vue ; la plupart sont des variantes d'idées déjà en shadow, d'où peu d'idées nouvelles.</p>
</div><script>
const $=id=>document.getElementById(id);let D=null;
const f=(v,d=2)=>v===null||v===undefined?'—':Number(v).toFixed(d);
const sg=(v,d=1)=>v===null||v===undefined?'—':(v>0?'+':'')+Number(v).toFixed(d);
const cls=v=>v>0?'pos':v<0?'neg':'';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nf=v=>v===null||v===undefined?'—':Number(v).toLocaleString('fr-FR');
const hm=s=>{if(!s)return '—';const d=new Date(s);return d.toLocaleString('fr-FR',{timeZone:'Europe/Paris',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})};
function vb(v){const c={'prêt pour revue live':'v-ok','perdant':'v-ko','shadow validé':'v-val','pas concluant':'v-wait'}[v]||'v-few';return `<span class="verdict ${c}">${esc(v)}</span>`}
function liveCell(a){
  if(a.demande_live)return '<span class="mut">live demandé</span>';
  if(a.verdict!=='prêt pour revue live'||!['SHADOW','CANDIDATE'].includes(a.statut))return '';
  return `<button type="button" class="golive" data-act="ask" data-id="${esc(a.agent_id)}">Valider le live</button>`}
function note(t,bad){const m=$('msg');m.textContent=(bad?'❌ ':'✅ ')+t;m.hidden=false}
function renderAgents(){if(!D)return;const q=$('q').value.toLowerCase(),st=$('fst').value,fv=$('fv').value;
  const rows=(D.agents||[]).filter(a=>(!st||a.statut===st)&&(!fv||a.verdict===fv)&&(!q||(a.agent_id+' '+a.strategie).toLowerCase().includes(q)));
  $('agents').innerHTML=rows.length?rows.map(a=>`<tr><td><b>${esc(a.agent_id)}</b></td><td class="hide-sm mut">${esc(a.strategie)}</td><td>${esc(a.statut)}</td>
  <td class="num">${a.n}</td><td class="num ${cls(a.r)}">${sg(a.r)} R</td><td class="num">${a.pf===null?(a.n?'<span class="mut">sans perte</span>':'—'):f(a.pf)}</td>
  <td class="num hide-sm ${cls(a.esperance)}">${sg(a.esperance,2)}</td><td class="num hide-sm">${f(a.reussite,0)} %</td><td class="num hide-sm">${a.ouvertes||''}</td>
  <td><span class="mini"><div style="width:${a.progression}%"></div></span> <span class="mut">${Math.min(a.n,D.criteres.min_shadow)}/${D.criteres.min_shadow}</span></td><td>${vb(a.verdict)}</td><td>${liveCell(a)}</td></tr>`).join('')
   :'<tr><td colspan="12" class="mut">Aucun agent pour ce filtre.</td></tr>'}
async function load(){
  try{const r=await fetch('/api/shadow',{credentials:'same-origin',cache:'no-store'});
    if(r.status===401){$('err').innerHTML='Session expirée — <a href="/login">se reconnecter</a>';$('err').hidden=false;return}
    D=await r.json();if(D.erreur){$('err').textContent='Lecture impossible : '+D.erreur;$('err').hidden=false;return}$('err').hidden=true;
    const s=D.resume,c=D.criteres,R=D.recherche||{};
    const cards=[['Agents en shadow',s.agents_shadow,`${s.agents_live} live · ${s.agents_suspendus} suspendus`],
      ['Résultat shadow actif',sg(s.r_actifs)+' R',`${nf(s.trades_actifs)} trades clôturés`],
      ['Positions ouvertes',s.ouvertes,'virtuelles, en cours'],
      ['Prêts pour revue live',s.prets,`≥ ${c.min_shadow} trades, PF ≥ ${f(c.pf_live,1)}`],
      ['Perdants',s.perdants,`≥ ${c.perdant_n} trades, PF < ${f(c.perdant_pf,1)}`]];
    $('cards').innerHTML=cards.map(([t,v,x])=>`<div class="card"><small>${t}</small><b>${v}</b><span class="sub">${x}</span></div>`).join('');
    $('regle').innerHTML=`<p><b>Passage en live</b> : au moins <b>${c.min_shadow} trades shadow</b> avec espérance positive et PF ≥ 1, puis <b>${c.min_sample} trades au total</b> (backtest + shadow),
      PF ≥ ${f(c.pf_live,1)}, espérance ≥ ${f(c.exp_live,2)} R et drawdown limité. Chaque étape du pipeline est vérifiée ; un agent prêt passe aussi en live sur validation manuelle (bouton « Valider le live », appliquée au cycle suivant du bot).
      Un agent shadow à ${c.perdant_n} trades avec un PF sous ${f(c.perdant_pf,1)} est suspendu automatiquement.</p>`;
    $('fams').innerHTML=(D.familles||[]).filter(x=>x.n||x.ouvertes).map(x=>`<tr><td><b>${esc(x.famille)}</b> <span class="mut">${esc(x.nom)}</span></td><td class="num">${x.actifs}</td><td class="num">${x.n}</td>
      <td class="num ${cls(x.r)}">${sg(x.r)} R</td><td class="num">${f(x.pf)}</td><td class="num hide-sm">${f(x.reussite,0)} %</td><td class="num hide-sm">${x.ouvertes||''}</td></tr>`).join('');
    renderAgents();
    $('rcards').innerHTML=[['Configurations testées',nf(R.configurations),`${nf(R.passages_continus)} passages continus`],
      ['Recherche continue',R.en_marche?'<span class="pos">en marche</span>':'<span class="warn">arrêtée</span>','dernier passage '+hm(R.derniere_date)],
      ['Idées en shadow',(R.idees||[]).length,`${nf(R.rapports)} rapports`]].map(([t,v,x])=>`<div class="card"><small>${t}</small><b>${v}</b><span class="sub">${x}</span></div>`).join('');
    $('passages').innerHTML=(R.passages||[]).map(p=>`<tr><td>${esc(p.rapport.replace(/_\d{4}-\d\d-\d\d_\d{4}$/,''))}</td><td>${hm(p.date)}</td><td class="num">${nf(p.testees)}</td>
      <td class="num hide-sm">${f(p.seuil)}</td><td class="num">${nf(p.significatives)}</td><td class="num">${nf(p.controle)}</td><td class="num ${p.propositions?'pos':''}">${p.propositions}</td></tr>`).join('');
    $('idees').innerHTML=(R.idees||[]).length?R.idees.map(i=>`<tr><td><b>${esc(i.agent_id)}</b></td><td>${esc(i.strategie)}</td><td>${esc(i.ut)}</td><td class="hide-sm">${esc(String(i.classe||'').replace('u_',''))}</td>
      <td class="hide-sm">${esc((i.sessions||[]).join(', ')||'toutes')}</td><td class="num">${f(i.pf_backtest)}</td><td class="num">${f(i.pf_controle)}</td><td class="hide-sm">${hm(i.date)}</td></tr>`).join('')
      :'<tr><td colspan="8" class="mut">Aucune idée nouvelle.</td></tr>';
  }catch(e){$('err').textContent='Erreur de chargement : '+e;$('err').hidden=false}}
['q','fst','fv'].forEach(id=>$(id).addEventListener('input',renderAgents));
$('agents').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-act]');if(!b)return;
  const id=b.dataset.id,td=b.closest('td');
  if(b.dataset.act==='ask'){td.innerHTML=`<button type="button" class="golive" data-act="go" data-id="${esc(id)}">Confirmer ${esc(id)}</button> <button type="button" class="golive sec" data-act="no">Annuler</button>`;td.querySelector('button').focus();return}
  if(b.dataset.act==='no'){renderAgents();return}
  b.disabled=true;
  try{const r=await fetch('/api/shadow/live',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:id})});
    if(r.status===401){note('Session expirée : se reconnecter puis recommencer',true);renderAgents();return}
    const d=await r.json();note(d.ok?d.message:(d.error||'demande refusée'),!d.ok);
    if(d.ok){const a=(D.agents||[]).find(x=>x.agent_id===id);if(a)a.demande_live=true;setTimeout(load,15000)}
    renderAgents();
  }catch(err){note('Erreur réseau : la demande n\'est pas partie',true);renderAgents()}
});
load();setInterval(load,60000);
</script></body></html>"""

QUALITE_HTML = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trading Lab — Qualité</title>
""" + STATS_HTML[STATS_HTML.index("<style>"):STATS_HTML.index("</style>") + len("</style>")] + r"""
<style>.verdict{font-size:.85rem;padding:.15rem .5rem;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.v-ok{color:#0f172a;background:var(--green);border-color:var(--green)}.v-ko{color:#0f172a;background:var(--red);border-color:var(--red)}
.v-wait{color:var(--amber)}.v-few{color:var(--mut)}</style></head><body><div class="wrap">
<h1>🧪 Qualité</h1>
<div class="crumb"><a href="/">← Tableau de bord</a> · <a href="/stats">Statistiques</a> · <a href="/control">Panneau de contrôle</a> · trades live clôturés, mise à jour toutes les 60 s</div>
<div id="err" class="errbox" hidden></div>
<div class="tabs"><button id="t_all" class="on" type="button">Tout l'historique</button><button id="t_gel" type="button">Version gelée</button></div>
<div class="cards" id="cards"></div>
<div class="paybox" id="gel" hidden></div>
<h2>Par agent</h2>
<div class="tablewrap"><table><thead><tr><th>Agent</th><th>Trades</th><th>Espérance (marge 95 %)</th><th>Réussite</th><th>PF</th>
<th class="hide-sm">MFE moy.</th><th class="hide-sm">MAE moy.</th><th>Coût</th><th class="hide-sm">Glissement</th><th>Verdict</th></tr></thead>
<tbody id="rows"><tr><td colspan="10" class="mut">Chargement…</td></tr></tbody></table></div>
<p class="note">L'espérance est le résultat moyen par trade, en R. La marge donne la plage où se trouve probablement la vraie
valeur : un agent n'a un avantage prouvé que si toute la plage est au-dessus de zéro. Aucun verdict avant 10 trades.
Le coût (spread + commission, en % du risque) et le glissement sont mesurés sur les trades ouverts depuis le 25/09.</p>
</div><script>
const $=id=>document.getElementById(id);let periode='';
const f=(v,d=2)=>v===null||v===undefined?'—':Number(v).toFixed(d);
const sg=(v,d=2)=>v===null||v===undefined?'—':(v>0?'+':'')+Number(v).toFixed(d);
const cls=v=>v>0?'pos':v<0?'neg':'';
const has=v=>v!==null&&v!==undefined;
function vbadge(v){const c={'avantage prouvé':'v-ok','perdant prouvé':'v-ko','pas encore concluant':'v-wait'}[v]||'v-few';return `<span class="verdict ${c}">${v||'—'}</span>`}
function esp(a){if(!a.n)return '—';const m=has(a.ic_bas)?` <span class="mut">[${sg(a.ic_bas)} ; ${sg(a.ic_haut)}]</span>`:'';return `<span class="${cls(a.expectancy_r)}">${sg(a.expectancy_r,3)} R</span>${m}`}
async function load(){
  try{
    const r=await fetch('/api/quality'+(periode?'?periode='+periode:''),{credentials:'same-origin'});
    if(r.status===401){$('err').innerHTML='Session expirée — <a href="/login">se reconnecter</a>';$('err').hidden=false;return}
    const q=await r.json();$('err').hidden=true;const g=q.global||{};
    const cards=[['Trades',g.n||0,q.periode||''],
      ['Espérance',g.n?sg(g.expectancy_r,3)+' R':'—',has(g.ic_bas)?`marge [${sg(g.ic_bas)} ; ${sg(g.ic_haut)}]`:''],
      ['Total',g.n?sg(g.total_r)+' R':'—',g.n?`${sg(g.pnl,0)} $`:''],
      ['Réussite',g.n?f(g.win_rate,0)+' %':'—',g.profit_factor?'PF '+f(g.profit_factor):''],
      ["Coût d'entrée",has(g.cout_moy_pct)?f(g.cout_moy_pct,0)+' % du risque':'—','spread + commission'],
      ['Glissement',has(g.glissement_moy_r)?sg(g.glissement_moy_r,3)+' R':'—','prix obtenu vs prix du gate']];
    $('cards').innerHTML=cards.map(([t,v,s])=>`<div class="card"><small>${t}</small><b>${v}</b><span class="sub">${s}</span></div>`).join('');
    const gel=q.gel;$('gel').hidden=!gel;
    if(gel){const pc=Math.min(100,Math.round(100*gel.trades/gel.requis));
      $('gel').innerHTML=`<p><b>Gel des réglages ${gel.version||''}</b> — depuis ${String(gel.depuis).slice(0,16).replace('T',' ')} UTC</p>
      <div class="progress"><div style="width:${pc}%"></div></div><p>${gel.trades} / ${gel.requis} trades · ${gel.termine?'<span class="pos">terminé : décision possible</span>':'encore '+gel.restant+' trades avant de juger'}</p>`}
    const rows=(q.agents||[]).map(a=>`<tr><td><b>${a.agent_id}</b></td><td>${a.n}</td><td>${esp(a)}</td><td>${f(a.win_rate,0)} %</td>
      <td>${a.profit_factor===null?'<span class="mut">sans perte</span>':f(a.profit_factor)}</td><td class="hide-sm">${sg(a.mfe_moy)} R</td><td class="hide-sm">−${f(a.mae_moy)} R</td>
      <td>${has(a.cout_moy_pct)?f(a.cout_moy_pct,0)+' %':'—'}</td><td class="hide-sm">${has(a.glissement_moy_r)?sg(a.glissement_moy_r,3):'—'}</td><td>${vbadge(a.verdict)}</td></tr>`);
    $('rows').innerHTML=rows.join('')||'<tr><td colspan="10" class="mut">Aucun trade clôturé sur cette période.</td></tr>';
  }catch(e){$('err').textContent='Qualité indisponible : '+e;$('err').hidden=false}
}
$('t_all').onclick=()=>{periode='';$('t_all').classList.add('on');$('t_gel').classList.remove('on');load()};
$('t_gel').onclick=()=>{periode='gel';$('t_gel').classList.add('on');$('t_all').classList.remove('on');load()};
load();setInterval(load,60000);
</script></body></html>"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude MT5 Trading Lab — Tableau de bord</title>
<style>
:root{--bg:#0f1216;--card:#171b21;--line:#262c35;--fg:#e6e9ee;--muted:#8a93a3;--ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;--info:#3498db;--purple:#9b59b6}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.top{position:sticky;top:0;z-index:2;background:#12161b;border-bottom:1px solid var(--line)}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:10px 16px}
header h1{font-size:16px;margin:0;font-weight:600}
header .meta{color:var(--muted);font-size:12px}
header a{color:#38bdf8;text-decoration:none}
/* Bandeau « État en un coup d'œil » : 4 tuiles, collé sous l'en-tête, 2 colonnes sur téléphone */
.glance-lbl{padding:6px 16px 0;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.accts{padding:4px 16px 0;display:grid;gap:10px}
.acct .alb{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:4px 0}
.acct .row4{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));font-variant-numeric:tabular-nums}
@media (max-width:640px){.acct .row4{grid-template-columns:1fr 1fr;gap:6px}.accts{padding:4px 12px 0}}
.glance{display:grid;gap:8px;padding:8px 16px 10px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));font-variant-numeric:tabular-nums}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0;font-size:13px}
.tile .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.tile .val{margin-top:2px;word-break:break-word}
.light{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--muted);vertical-align:-1px;margin-right:6px;box-shadow:0 0 6px rgba(0,0,0,.4)}
.light.ok{background:var(--ok)}.light.warn{background:var(--warn)}.light.bad{background:var(--bad)}
.tile.alert{border-color:var(--bad)}
main{padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.card h2 .sub{text-transform:none;letter-spacing:0;font-weight:400}
.card.wide{grid-column:1/-1}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-variant-numeric:tabular-nums}
.kv dt{color:var(--muted)}
.kv dd{margin:0;text-align:right;word-break:break-word}
.big{font-size:22px;font-weight:700}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:12px;color:#000;background:var(--muted)}
.badge.ok{background:var(--ok)}.badge.warn{background:var(--warn)}.badge.bad{background:var(--bad);color:#fff}.badge.info{background:var(--info);color:#fff}.badge.purple{background:var(--purple);color:#fff}
.badge.off{background:#232932;color:var(--muted);border:1px solid var(--line);font-weight:500}
.huge{font-size:28px;padding:6px 14px;letter-spacing:.1em}
.pos{color:var(--ok)}.neg{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
th,td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}
.scroll{overflow:auto;max-height:360px}
.reasons{color:var(--muted);font-size:12px;margin-top:4px}
.journal{font-size:13px;max-height:480px;overflow:auto;font-variant-numeric:tabular-nums}
.journal div{padding:3px 0;border-bottom:1px solid var(--line);white-space:pre-wrap;word-break:break-word}
.journal .t{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.journal .k{color:var(--info);font-weight:700}
.journal .ico{display:inline-block;width:1.4em;text-align:center}
.unknown{color:var(--muted);font-style:italic}
.empty{color:var(--muted);font-style:italic;padding:6px 0}
#error{display:none;background:var(--bad);color:#fff;padding:6px 10px;border-radius:4px}
.chips span{display:inline-block;background:#232932;border:1px solid var(--line);border-radius:4px;padding:2px 6px;margin:2px;font-size:12px}
details{margin-top:6px}
summary{cursor:pointer;color:var(--muted);font-size:12px}
/* Jauges horizontales (risque) */
.gauge{margin:8px 0}
.gauge .gl{display:flex;justify-content:space-between;gap:8px;font-size:12px;flex-wrap:wrap}
.gauge .gl b{font-weight:600}
.gauge .bar{position:relative;height:10px;background:#232932;border:1px solid var(--line);border-radius:5px;overflow:hidden;margin-top:3px}
.gauge .fill{height:100%;background:var(--ok);border-radius:5px;transition:width .4s}
.gauge .fill.warn{background:var(--warn)}.gauge .fill.bad{background:var(--bad)}
.gauge .mark{position:absolute;top:-1px;bottom:-1px;width:2px;background:var(--fg);opacity:.7}
.gauge .sub{color:var(--muted);font-size:11px}
/* Cartes de positions */
.poscard{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0;background:#13171c;font-variant-numeric:tabular-nums}
.poscard .l1{font-weight:600;display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center}
.poscard .l2,.poscard .l3{font-size:12px;color:var(--muted);margin-top:3px;word-break:break-word}
.poscard .badges span{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:700;margin-left:4px;background:#232932;color:var(--muted);border:1px solid var(--line)}
.poscard .badges span.on{background:var(--ok);color:#000;border-color:var(--ok)}
.poscard .badges span.on.tr{background:var(--info);color:#fff;border-color:var(--info)}
.btn{background:#232932;border:1px solid var(--line);color:var(--fg);border-radius:4px;padding:2px 8px;font-size:12px;cursor:pointer}
.btn.on{border-color:var(--info);color:#38bdf8}
.lbrow small{color:var(--muted)}
@media (max-width:640px){main{grid-template-columns:1fr;padding:12px}.huge{font-size:22px}.glance{grid-template-columns:1fr 1fr;padding:6px 12px 8px;gap:6px}.tile{font-size:12px;padding:6px 8px}header{padding:8px 12px}}
</style>
</head>
<body>
<div class="top">
<header>
  <h1>Claude MT5 Trading Lab — Tableau de bord</h1>
  <span class="meta"><a href="/stats">📈 Statistiques</a> &nbsp;·&nbsp; <a href="/qualite">🧪 Qualité</a> &nbsp;·&nbsp; <a href="/shadow">👻 Shadow &amp; recherche</a> &nbsp;·&nbsp; <a href="/control">🎛️ Contrôle</a> &nbsp;·&nbsp; lecture seule, rafraîchie toutes les 5 s. Heure (Paris) : <span id="server_time">—</span> &nbsp;·&nbsp; Session : <span id="session_now">—</span></span>
  <span id="error"></span>
</header>
<div class="glance-lbl">🏦 Compte maître (IC Markets)</div>
<section class="glance" id="glance" aria-label="État en un coup d'œil">
  <div class="tile" id="tile_bot"><div class="lbl">Bot</div><div class="val"><span class="light" id="bot_light"></span><span id="bot_text">—</span></div></div>
  <div class="tile" id="tile_today"><div class="lbl">Aujourd'hui</div><div class="val" id="today_text">—</div></div>
  <div class="tile" id="tile_pos"><div class="lbl">Positions</div><div class="val" id="pos_text">—</div></div>
  <div class="tile" id="tile_cycle"><div class="lbl">Cycle FOXX</div><div class="val" id="cycle_text">—</div></div>
</section>
</div>
<section class="accts" id="accts" aria-label="Chaque compte suiveur" hidden></section>
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
  <section class="card"><h2>Risque</h2>
    <dl class="kv">
      <dt>Equity</dt><dd id="equity" class="big"></dd>
      <dt>Solde</dt><dd id="balance"></dd>
      <dt>P&amp;L du jour</dt><dd id="daily_pnl"></dd>
      <dt>Risque ouvert</dt><dd id="open_risk"></dd>
      <dt>Pertes consécutives</dt><dd id="consec"></dd>
      <dt>Trades clos (jour)</dt><dd id="trades_closed"></dd>
    </dl>
    <div id="gauges"></div>
    <h2 style="margin-top:10px">Risque ouvert par classe</h2>
    <div id="risk_by_class"></div>
  </section>
  <section class="card"><h2>Limites prop</h2>
    <dl class="kv">
      <dt>Profil</dt><dd id="prop_firm"></dd>
      <dt>Objectif profit</dt><dd id="prop_target"></dd>
      <dt>Règles prop</dt><dd id="prop_verified"></dd>
      <dt>AUTONOMOUS_PROP</dt><dd id="prop_auto"></dd>
      <dt>Risque max / idée</dt><dd id="prop_idea"></dd>
      <dt>Cohérence (part max idée)</dt><dd id="prop_consistency"></dd>
      <dt>Cycle de paiement</dt><dd id="prop_cycle"></dd>
    </dl>
    <details><summary>Règles</summary>
      <dl class="kv">
        <dt>Programme</dt><dd id="prop_program"></dd>
        <dt>Perte jour max (hard)</dt><dd id="prop_daily"></dd>
        <dt>Perte globale max (hard)</dt><dd id="prop_overall"></dd>
        <dt>Perte jour interne</dt><dd id="risk_daily"></dd>
        <dt>Risque / trade (interne)</dt><dd id="risk_per_trade"></dd>
        <dt>Positions max</dt><dd id="risk_maxpos"></dd>
        <dt>Journée prop</dt><dd id="prop_day"></dd>
      </dl>
    </details>
  </section>
  <section class="card"><h2>Santé du système</h2>
    <dl class="kv">
      <dt>News</dt><dd id="news"></dd>
      <dt>Calendrier</dt><dd id="calendar"></dd>
      <dt>Battement orchestrateur</dt><dd id="hb_orch"></dd>
      <dt>Battement watchdog</dt><dd id="hb_wd"></dd>
      <dt>Watchdog : SAFE_MODE demandé</dt><dd id="wd_safe"></dd>
      <dt>Watchdog : données fraîches</dt><dd id="wd_fresh"></dd>
      <dt>Watchdog : positions sans SL</dt><dd id="wd_nosl"></dd>
      <dt>Watchdog : raisons</dt><dd id="wd_reasons"></dd>
      <dt>Watchdog : actions</dt><dd id="wd_actions"></dd>
      <dt>Redémarrages</dt><dd id="restarts"></dd>
      <dt>Démarré à</dt><dd id="started_at"></dd>
    </dl>
  </section>
  <section class="card"><h2>Activité</h2>
    <div id="activity_line">—</div>
    <details><summary>Détail : régimes par symbole et agents actifs</summary>
      <div class="chips" id="regimes"></div>
      <div class="chips" id="agents" style="margin-top:6px"></div>
    </details>
    <div id="models_line" style="margin-top:8px">—</div>
  </section>
  <section class="card wide"><h2>Top setups</h2>
    <div class="scroll"><table><thead><tr><th>Symbole</th><th>Sens</th><th>Score</th><th>Agent</th><th>Verdict</th></tr></thead><tbody id="setups"></tbody></table></div>
  </section>
  <section class="card wide"><h2>Positions du bot <span class="sub" id="pos_head"></span></h2>
    <div id="positions"></div>
  </section>
  <section class="card wide"><h2>Meilleurs agents <span class="sub"><a href="/stats" style="color:#38bdf8;text-decoration:none">classement complet →</a></span></h2>
    <div class="scroll"><table><thead><tr><th>Agent</th><th>Trades (G / P)</th><th>Taux de réussite</th><th>Profit factor</th><th>Espérance (R)</th><th>Total (R)</th></tr></thead><tbody id="leaderboard"></tbody></table></div>
  </section>
  <section class="card wide"><h2>Journal du jour <span class="sub"><button class="btn" id="journal_toggle" type="button">tout voir</button></span></h2>
    <div class="journal" id="journal"></div>
  </section>
</main>
<script>
(function(){
  var UNK = 'UNKNOWN / UNAVAILABLE';
  // Événements « humains » affichés par défaut ; le bouton « tout voir » recharge le journal sans filtre.
  var JOURNAL_KINDS = 'position_opened,post_trade_review,partial_tp,early_exit,sl_modified,mode_change,command,watchdog_alert,warning,payout_window,payout_done,payout_ready,consistency_cap_close,copy_trade';
  var journalAll = false;
  var CUR = '';
  function $(id){ return document.getElementById(id); }
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function missing(v){ return v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v)); }
  function isNum(v){ return typeof v === 'number' && !isNaN(v); }
  function txt(v){ return missing(v) ? '<span class="unknown">'+UNK+'</span>' : esc(v); }
  function dash(v){ return missing(v) ? '—' : esc(v); }
  function num(v, d){ d = d === undefined ? 2 : d; return !isNum(v) ? txt(v) : esc(v.toLocaleString('fr-FR', {minimumFractionDigits: d, maximumFractionDigits: d})); }
  function pct(v, d){ return !isNum(v) ? '—' : esc(v.toLocaleString('fr-FR', {minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d}) + ' %'); }
  function bool(v){ return missing(v) ? txt(v) : (v ? 'oui' : 'non'); }
  function set(id, html){ $(id).innerHTML = html; }
  function badge(id, text, cls){ var e = $(id); e.textContent = text; e.className = e.className.replace(/\b(ok|warn|bad|info|purple)\b/g,'').trim() + (cls ? ' ' + cls : ''); }
  // Format monétaire uniforme : arrondi à l'unité, séparateurs fr-FR, symbole de la devise du compte.
  function curSym(){ return {USD:'$', EUR:'€', GBP:'£', CHF:'CHF'}[CUR] || CUR || ''; }
  function money(v){ if (!isNum(v)) return '—'; return esc(Math.round(v).toLocaleString('fr-FR') + (curSym() ? ' ' + curSym() : '')); }
  function smoney(v){ if (!isNum(v)) return '—'; var c = v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); return '<span class="'+c+'">'+(v > 0 ? '+' : '')+money(v)+'</span>'; }
  function spct(v, d){ if (!isNum(v)) return '—'; var c = v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); return '<span class="'+c+'">'+(v > 0 ? '+' : '')+pct(v, d)+'</span>'; }
  function sr(v){ if (!isNum(v)) return '—'; var c = v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); return '<span class="'+c+'">'+esc((v > 0 ? '+' : '')+v.toLocaleString('fr-FR', {minimumFractionDigits: 2, maximumFractionDigits: 2})+' R')+'</span>'; }
  function hb(v, warn){ if (missing(v)) return '<span class="badge bad">'+UNK+'</span>'; var cls = v > warn ? 'bad' : 'ok'; return '<span class="badge '+cls+'">'+esc(v.toFixed(1))+' s</span>'; }
  function rows(id, list, fn, cols, emptyMsg){ if (!list || !list.length){ set(id, '<tr><td colspan="'+cols+'" class="empty">'+esc(emptyMsg || UNK)+'</td></tr>'); return; } set(id, list.map(fn).join('')); }
  function parisTime(iso){ if (!iso) return '—'; var d = new Date(iso); if (isNaN(d.getTime())) return String(iso).slice(11, 16); return d.toLocaleTimeString('fr-FR', {timeZone: 'Europe/Paris', hour: '2-digit', minute: '2-digit'}).replace(':', ' h '); }
  function duration(iso, nowIso){ if (!iso) return ''; var a = new Date(iso), b = nowIso ? new Date(nowIso) : new Date(); if (isNaN(a.getTime()) || isNaN(b.getTime())) return ''; var m = Math.max(0, Math.round((b - a) / 60000)); if (m < 60) return m + ' min'; var h = Math.floor(m / 60), r = m % 60; if (h < 48) return h + ' h ' + (r < 10 ? '0' : '') + r; return Math.floor(h / 24) + ' j ' + (h % 24) + ' h'; }

  // Jauge horizontale : valeur / limite, rouge dès 75 % de la limite, orange dès 50 %, marqueur optionnel.
  function gauge(label, value, limit, opts){
    opts = opts || {};
    if (!isNum(value) || !isNum(limit) || limit <= 0) {
      return '<div class="gauge"><div class="gl"><b>'+esc(label)+'</b><span>'+(isNum(value) ? pct(value) : '—')+' / '+(isNum(limit) ? pct(limit, 1) : '—')+'</span></div><div class="bar"></div><div class="sub">'+esc(opts.sub || 'limite inconnue')+'</div></div>';
    }
    var ratio = Math.max(0, value) / limit, w = Math.min(100, ratio * 100);
    var cls = ratio >= 0.75 ? 'bad' : (ratio >= 0.5 ? 'warn' : '');
    var mark = (isNum(opts.mark) && opts.mark > 0 && opts.mark < limit) ? '<span class="mark" title="'+esc(opts.markLabel || 'limite interne')+'" style="left:'+(opts.mark / limit * 100).toFixed(1)+'%"></span>' : '';
    return '<div class="gauge"><div class="gl"><b>'+esc(label)+'</b><span>'+pct(value)+' / '+pct(limit, 1)+'</span></div>'
      + '<div class="bar"><div class="fill '+cls+'" style="width:'+w.toFixed(1)+'%"></div>'+mark+'</div>'
      + (opts.sub ? '<div class="sub">'+opts.sub+'</div>' : '') + '</div>';
  }

  // Feu du bandeau : dérivé du mode, de la connexion MT5, du battement orchestrateur et du watchdog.
  function botStatus(s){
    var warn = isNum(s.heartbeat_warn_sec) ? s.heartbeat_warn_sec : 45;
    var age = s.orchestrator_heartbeat_age_sec, wd = s.watchdog || {};
    if (missing(age) || age > warn) return {cls: 'bad', text: 'orchestrateur silencieux' + (isNum(age) ? ' depuis ' + Math.round(age) + ' s' : ' (battement inconnu)')};
    if (s.mode === 'PANIC') return {cls: 'bad', text: "arrêt d'urgence (PANIC)"};
    if (s.mt5_connected === false) return {cls: 'bad', text: 'MT5 déconnecté'};
    if (s.mode === 'SAFE_MODE' || wd.safe_mode_request === true) return {cls: 'warn', text: 'mode prudence (SAFE_MODE)'};
    if (s.mode === 'PAUSED') return {cls: 'warn', text: 'en pause'};
    if (s.mode === 'AUTO') return s.new_trades_locked ? {cls: 'warn', text: 'en marche, nouvelles entrées bloquées'} : {cls: 'ok', text: 'en marche'};
    return {cls: '', text: missing(s.mode) ? UNK : 'mode ' + s.mode};
  }

  function exitLabel(r){ return {sl: 'sortie stop', tp: 'objectif atteint', invalidation: 'invalidation', payout: 'fenêtre de paiement', 'coherence-25': 'plafond de cohérence', manual: 'sortie manuelle', panic: "arrêt d'urgence", early_exit: 'sortie anticipée'}[r] || (r ? 'sortie : ' + r : ''); }

  // Une ligne lisible par type d'événement (champs relevés dans logs/journal-2026-09-23.jsonl).
  function humanEvent(e, positions){
    var k = e.kind || '?', sym = e.symbol || (positions[String(e.ticket)] ? positions[String(e.ticket)].symbol : '');
    var parts;
    switch (k) {
      case 'position_opened':
        return {ico: e.side === 'SELL' ? '▼' : '▲', text: esc(sym) + ' ' + esc(e.side || '') + ' par ' + esc(e.agent_id || '?') + (isNum(e.trade_idea_risk_money) ? ', risque ' + money(e.trade_idea_risk_money) : '') + (isNum(e.entry) ? ' à ' + esc(e.entry) : '')};
      case 'post_trade_review':
        var rv = e.review || {}, pnl = isNum(e.pnl) ? e.pnl : rv.pnl, rr = isNum(e.result_r) ? e.result_r : rv.result_r;
        var reason = exitLabel(rv.exit_reason || e.exit_reason), verdict = e.verdict || rv.verdict;
        return {ico: isNum(pnl) ? (pnl >= 0 ? '✔' : '✘') : '•', text: esc(sym) + ' ' + smoney(pnl) + (isNum(rr) ? ' (' + sr(rr) + ')' : '') + (reason ? ', ' + esc(reason) : '') + (verdict ? ' · ' + esc(verdict) : '') + (e.agent_id ? ' · ' + esc(e.agent_id) : '')};
      case 'partial_tp':
        return {ico: '🎯', text: esc(sym) + ' prise partielle' + (isNum(e.r) ? ' à ' + sr(e.r) : '') + (isNum(e.gain_estime) ? ', gain estimé ' + money(e.gain_estime) : '') + (isNum(e.volume_restant) ? ', reste ' + esc(e.volume_restant) + ' lot' : '')};
      case 'early_exit':
        return {ico: '🚪', text: esc(sym) + ' sortie anticipée' + (e.reason ? ' (' + esc(e.reason) + ')' : '') + (isNum(e.r) ? ' à ' + sr(e.r) : '') + (e.rule ? ' — ' + esc(e.rule) : '') + (e.result && e.result.ok === false ? ' ⚠ ordre refusé' : '')};
      case 'sl_modified':
        return {ico: '🛡', text: (sym ? esc(sym) : 'ticket ' + esc(e.ticket)) + ' stop déplacé ' + dash(e.old) + ' → ' + dash(e['new']) + (isNum(e.r) ? ' (' + sr(e.r) + ')' : '')};
      case 'mode_change':
        return {ico: '⚙', text: 'mode → ' + esc(e.mode || '?') + (e.reason ? ' (' + esc(e.reason) + ')' : '')};
      case 'command':
        return {ico: '⌨', text: 'commande ' + esc(e.command || '?') + (e.source ? ' (' + esc(e.source) + ')' : '') + (e.ok === false ? ' — refusée' + (e.error ? ' : ' + esc(e.error) : '') : '')};
      case 'watchdog_alert':
        parts = [];
        if (e.reasons && e.reasons.length) parts.push(e.reasons.map(esc).join(' ; '));
        if (e.actions && e.actions.length) parts.push('actions : ' + e.actions.map(esc).join(' ; '));
        return {ico: '⚠', text: 'watchdog : ' + (parts.length ? parts.join(' — ') : 'alerte')};
      case 'warning':
        return {ico: '⚠', text: esc(e.message || 'avertissement') + (sym ? ' · ' + esc(sym) : (e.ticket ? ' · ticket ' + esc(e.ticket) : ''))};
      case 'payout_window':
        return {ico: '🏦', text: 'fenêtre de paiement ouverte' + (e.closed && e.closed.length ? ', ' + e.closed.length + ' position(s) fermée(s)' : '') + (isNum(e.withdrawable) ? ', retirable ' + money(e.withdrawable) : '')};
      case 'payout_done':
        return {ico: '💰', text: 'paiement ' + (e.simulated ? 'simulé' : 'enregistré') + (isNum(e.amount) ? ' : ' + money(e.amount) : '') + (isNum(e.trader_share) ? ' (part trader ' + money(e.trader_share) + ')' : '') + (e.skipped ? ' — montant nul, ignoré' : '')};
      case 'payout_ready':
        return {ico: '💰', text: 'paiement possible' + (isNum(e.withdrawable) ? ' : ' + money(e.withdrawable) : '') + (e.message ? ' — ' + esc(e.message) : '')};
      case 'consistency_cap_close':
        return {ico: '✂', text: esc(sym) + ' fermée pour la règle de cohérence 25 %' + (isNum(e.gain) ? ' : gain ' + money(e.gain) : '') + (isNum(e.cap) ? ' ≥ plafond ' + money(e.cap) : '') + (e.ok === false ? ' ⚠ fermeture refusée' : '')};
      case 'copy_trade':
        return {ico: '⛔', text: 'copie ' + (sym ? esc(sym) + ' ' + esc(e.side || '') + ' ' : '') + 'échouée' + (e.component ? ' (' + esc(e.component) + ')' : '') + (e.detail ? ' : ' + esc(e.detail) : '')};
      default:
        return null;
    }
  }
  function rawEvent(e){
    var summary = e.message || e.summary || e.reason || '';
    if (!summary) { var o = {}; Object.keys(e).forEach(function(k){ if (['ts_utc','ts_local','component','kind'].indexOf(k) < 0) o[k] = e[k]; }); summary = JSON.stringify(o); }
    if (summary.length > 240) summary = summary.slice(0, 240) + '…';
    return '<span class="k">'+esc(e.kind || '?')+'</span> '+esc(summary);
  }

  // résumé par compte suiveur (2026-09-24) : même lecture que le bandeau du maître, depuis ses propres trades
  function renderAccounts(list, p){
    var el = $('accts'); if (!el) return;
    if (!list.length) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.innerHTML = list.map(function(a){
      var head = '<div class="alb">👤 ' + esc(a.name || a.env_prefix || '?') + '</div>';
      if (!a.status) return '<div class="acct">' + head + '<div class="tile">copieur pas encore démarré : aucune donnée</div></div>';
      var n = a.today_trades || 0, w = a.today_wins || 0, l = a.today_losses || 0;
      var cons = a.consistency_ok === false;
      return '<div class="acct">' + head + '<div class="row4">'
        + '<div class="tile"><div class="lbl">Aujourd’hui (fermé)</div><div class="val">' + smoney(a.today_pnl) + ', ' + n + ' trade' + (n === 1 ? '' : 's') + ', ' + w + ' gagnant' + (w === 1 ? '' : 's') + ' / ' + l + ' perdant' + (l === 1 ? '' : 's') + '</div></div>'
        + '<div class="tile"><div class="lbl">Positions</div><div class="val">' + dash(a.positions) + ' position' + (a.positions === 1 ? '' : 's') + ' · flottant ' + smoney(a.floating) + '</div></div>'
        + '<div class="tile' + (cons ? ' alert' : '') + '"><div class="lbl">Cohérence 25 %</div><div class="val">' + (cons ? '⚠ ' : '') + dash(a.trading_days) + ' jour(s) de trading, meilleure idée ' + pct(a.consistency_share_percent, 1) + ' (limite ' + pct(p.consistency_max_share_percent, 0) + ')</div></div>'
        + '<div class="tile"><div class="lbl">Equity</div><div class="val">' + (isNum(a.equity) ? money(a.equity) : '—') + '</div></div>'
        + '</div></div>';
    }).join('');
  }
  function render(s){
    CUR = s.currency || '';
    $('server_time').textContent = s.server_time_utc ? new Date(s.server_time_utc).toLocaleString('fr-FR', {timeZone: 'Europe/Paris'}) : UNK;
    { const NOMS = {ASIA: 'Asie', LONDON: 'Londres', NEWYORK: 'New York', OVERLAP_LDN_NY: 'Londres + New York', SYDNEY: 'Sydney', OFF: 'hors session'};
      const sm = s.session_marche || {};
      const fx = NOMS[sm.forex] || sm.forex || UNK, cr = NOMS[sm.crypto] || sm.crypto || UNK;
      $('session_now').textContent = fx === cr ? fx : fx + ' (crypto : ' + cr + ')'; }
    var p = s.prop || {}, r = s.risk || {}, daily = s.daily || {}, pc = s.payout_cycle || null, corr = s.correlation || {};
    var bp = s.bot_positions || {}, posList = Object.keys(bp).map(function(k){ return bp[k]; });
    var floating = (isNum(s.equity) && isNum(s.balance)) ? s.equity - s.balance : null;

    // ---- bandeau « État en un coup d'œil » ----
    var st = botStatus(s);
    $('bot_light').className = 'light ' + st.cls;
    $('bot_text').textContent = 'Bot : ' + st.text;
    set('today_text', smoney(s.daily_pnl) + ' (' + spct(s.daily_pnl_percent) + ') dont ' + smoney(daily.realized_pnl) + ' fermés, '
      + dash(daily.trades_closed) + ' trade' + (daily.trades_closed === 1 ? '' : 's') + ', ' + dash(daily.wins) + ' gagnant' + (daily.wins === 1 ? '' : 's') + ' / ' + dash(daily.losses) + ' perdant' + (daily.losses === 1 ? '' : 's'));
    set('pos_text', posList.length + ' position' + (posList.length === 1 ? '' : 's') + ' · risque ouvert ' + pct(s.open_risk_percent) + ' · flottant ' + smoney(floating));
    if (pc) {
      var cons = pc.consistency_ok === false;
      set('cycle_text', (cons ? '⚠ ' : '') + 'jour ' + dash(pc.trading_days_done) + '/' + dash(pc.trading_days_required) + ', meilleure idée ' + pct(pc.consistency_share_percent, 1) + ' (limite ' + pct(p.consistency_max_share_percent, 0) + ')' + (pc.ready ? ' · paiement prêt' : ''));
      $('tile_cycle').className = 'tile' + (cons ? ' alert' : '');
    } else { set('cycle_text', '<span class="unknown">'+UNK+'</span>'); $('tile_cycle').className = 'tile'; }
    renderAccounts(s.accounts_glance || [], p);

    // ---- compte & mode ----
    var tm = s.account_trade_mode || 'UNKNOWN';
    badge('trade_mode', tm, tm === 'DEMO' ? 'ok' : (tm === 'REAL' ? 'bad' : (tm === 'CONTEST' ? 'warn' : '')));
    set('mt5', s.mt5_connected === true ? '<span class="badge ok">CONNECTÉ</span>' : (s.mt5_connected === false ? '<span class="badge bad">NON CONNECTÉ</span>' : txt(null)));
    set('login', s.account_login ? txt(s.account_login) : txt(null));
    set('server', txt(s.account_server));
    set('currency', txt(s.currency));
    var mode = s.mode || 'UNKNOWN';
    badge('mode', mode, {SAFE_MODE:'warn', AUTO:'ok', PAUSED:'info', PANIC:'bad'}[mode] || '');
    set('mode_reasons', (s.mode_reasons && s.mode_reasons.length) ? esc(s.mode_reasons.join(' · ')) : '');
    if (missing(s.new_trades_locked)) badge('locked', 'Nouvelles entrées : ' + UNK, '');
    else badge('locked', s.new_trades_locked ? 'Nouvelles entrées : bloquées' : 'Nouvelles entrées : autorisées', s.new_trades_locked ? 'bad' : 'ok');
    set('lock_reasons', (s.lock_reasons && s.lock_reasons.length) ? 'raison : ' + esc(s.lock_reasons.join(' · ')) : '');

    // ---- carte Risque ----
    set('equity', money(s.equity));
    set('balance', money(s.balance));
    set('daily_pnl', smoney(s.daily_pnl) + ' (' + spct(s.daily_pnl_percent) + ')');
    set('open_risk', pct(s.open_risk_percent));
    set('consec', dash(s.consecutive_losses));
    set('trades_closed', dash(daily.trades_closed));
    var hardDay = p.max_daily_loss_hard_percent, hardAll = p.max_overall_loss_hard_percent, ib = s.initial_balance;
    var floorDay = (isNum(daily.reference_equity) && isNum(ib) && isNum(hardDay)) ? daily.reference_equity - ib * hardDay / 100 : null;
    var floorAll = (isNum(ib) && isNum(hardAll)) ? ib * (1 - hardAll / 100) : null;
    var ideas = s.trade_ideas_open || [], worst = null;   // idées encore ouvertes, projetées par le serveur (symbol, side, risk_money)
    ideas.forEach(function(i){ i = i || {}; if (isNum(i.risk_money) && (!worst || i.risk_money > worst.risk_money)) worst = i; });
    var worstPct = (worst && isNum(ib) && ib > 0) ? 100 * worst.risk_money / ib : (posList.length ? null : 0);
    set('gauges',
      gauge('Perte jour', s.prop_daily_loss_percent, hardDay, {mark: r.max_daily_loss_internal_percent, markLabel: 'limite interne ' + pct(r.max_daily_loss_internal_percent, 1),
        sub: 'limite interne ' + pct(r.max_daily_loss_internal_percent, 1) + ' · plancher ' + money(floorDay) + ' (base ' + money(ib) + ')'})
      + gauge('Perte totale', s.prop_overall_loss_percent, hardAll, {sub: 'plancher ' + money(floorAll) + ' (drawdown statique sur ' + money(ib) + ')'})
      + gauge('Idée la plus risquée', worstPct, p.max_risk_per_trade_idea_percent, {sub: worst ? esc(worst.symbol + ' ' + worst.side) + ' : ' + money(worst.risk_money) + " cumulés sur l'idée" : (posList.length ? 'risque des idées ouvertes inconnu' : 'aucune idée ouverte')}));
    var rbc = s.open_risk_by_class || {}, cap = corr.max_asset_class_risk_percent, classes = Object.keys(rbc).sort();
    if (!classes.length) set('risk_by_class', '<div class="empty">aucune position ouverte</div>');
    else set('risk_by_class', classes.map(function(c){ var row = rbc[c] || {}; return gauge(c + ' (' + dash(row.positions) + ')', row.risk_percent, cap, {sub: money(row.risk_money) + (isNum(cap) ? ' · plafond ' + pct(cap, 2) : ' · plafond inconnu')}); }).join(''));

    // ---- Limites prop ----
    set('prop_firm', txt(p.prop_firm));
    set('prop_program', txt(p.program));
    set('prop_daily', pct(hardDay, 1) + ' du solde initial');
    set('prop_overall', pct(hardAll, 1) + ' du solde initial');
    set('prop_target', pct(p.profit_target_percent, 1));
    set('prop_day', dash(daily.day) + ' (reset ' + dash(p.trading_day_reset_hour) + ':00 ' + dash(p.trading_day_timezone) + ')');
    set('prop_idea', pct(p.max_risk_per_trade_idea_percent, 1) + ' / idée (agrégation ' + dash(p.trade_idea_aggregation_minutes) + ' min)');
    if (pc) {
      set('prop_consistency', '<span class="badge '+(pc.consistency_ok === false ? 'bad' : 'ok')+'">'+pct(pc.consistency_share_percent, 1)+'</span><br><span class="reasons">meilleure idée = '+pct(pc.consistency_share_percent, 1)+' du profit du cycle (limite '+pct(p.consistency_max_share_percent, 0)+', appliquée dès '+pct(p.consistency_enforce_from_profit_percent, 0)+' de profit)</span>');
      set('prop_cycle', 'jour ' + dash(pc.trading_days_done) + '/' + dash(pc.trading_days_required) + ' · profit ' + smoney(pc.cycle_profit) + (pc.ready ? ' · <span class="badge ok">PRÊT</span>' : (pc.eligible ? ' · éligible' : '')) + ((pc.blocking_reasons && pc.blocking_reasons.length) ? '<br><span class="reasons">' + pc.blocking_reasons.map(esc).join(' · ') + '</span>' : ''));
    } else { set('prop_consistency', txt(null)); set('prop_cycle', txt(null)); }
    if (p.rules_verified_at) set('prop_verified', '<span class="badge ok">vérifiées le '+esc(p.rules_verified_at)+'</span>');
    else set('prop_verified', missing(p.prop_rules_verified) ? txt(null) : '<span class="badge '+(p.prop_rules_verified ? 'ok' : 'warn')+'">'+(p.prop_rules_verified ? 'VÉRIFIÉES' : 'NON VÉRIFIÉES')+'</span>');
    set('prop_auto', missing(p.autonomous_prop) ? txt(null) : '<span class="badge '+(p.autonomous_prop ? 'bad' : 'ok')+'">'+(p.autonomous_prop ? 'ACTIF' : 'INACTIF')+'</span>');
    set('risk_per_trade', pct(r.risk_per_trade_percent, 3));
    set('risk_daily', pct(r.max_daily_loss_internal_percent, 1));
    set('risk_maxpos', dash(r.max_open_positions));

    // ---- santé ----
    set('news', missing(s.news_data_degraded) ? txt(null) : '<span class="badge '+(s.news_data_degraded ? 'warn' : 'ok')+'">'+(s.news_data_degraded ? 'DÉGRADÉES' : 'OK')+'</span>');
    set('calendar', missing(s.calendar_data_degraded) ? txt(null) : '<span class="badge '+(s.calendar_data_degraded ? 'warn' : 'ok')+'">'+(s.calendar_data_degraded ? 'DÉGRADÉ' : 'OK')+'</span>');
    var warn = isNum(s.heartbeat_warn_sec) ? s.heartbeat_warn_sec : 45;
    set('hb_orch', hb(s.orchestrator_heartbeat_age_sec, warn));
    set('hb_wd', hb(s.watchdog_heartbeat_age_sec, warn));
    var wd = s.watchdog || {};
    set('wd_safe', missing(wd.safe_mode_request) ? txt(null) : '<span class="badge '+(wd.safe_mode_request ? 'bad' : 'ok')+'">'+(wd.safe_mode_request ? 'OUI' : 'NON')+'</span>');
    set('wd_fresh', missing(wd.data_fresh) ? txt(null) : '<span class="badge '+(wd.data_fresh ? 'ok' : 'bad')+'">'+(wd.data_fresh ? 'OUI' : 'PÉRIMÉES')+'</span>');
    set('wd_nosl', dash(wd.positions_without_sl));
    set('wd_reasons', (wd.reasons && wd.reasons.length) ? wd.reasons.map(esc).join('<br>') : 'aucune');
    set('wd_actions', (wd.actions && wd.actions.length) ? wd.actions.map(esc).join('<br>') : 'aucune');
    set('restarts', dash(s.restarts));
    set('started_at', txt(s.started_at));

    // ---- activité (régimes, agents, dernier cycle) + modèles ----
    var reg = s.regimes || {}, rk = Object.keys(reg), ag = s.active_agents || [], lc = s.last_cycle || {};
    set('activity_line', rk.length + ' symbole' + (rk.length === 1 ? '' : 's') + ' suivi' + (rk.length === 1 ? '' : 's') + ' · ' + ag.length + ' agent' + (ag.length === 1 ? '' : 's') + ' actif' + (ag.length === 1 ? '' : 's')
      + ' · dernier cycle ' + (isNum(lc.duration_sec) ? num(lc.duration_sec, 1) + ' s' : '—') + ', ' + dash(lc.candidates) + ' candidat' + (lc.candidates === 1 ? '' : 's') + ', ' + dash(lc.entries) + ' entrée' + (lc.entries === 1 ? '' : 's'));
    set('regimes', rk.length ? rk.map(function(k){ return '<span>'+esc(k)+' : '+esc(reg[k])+'</span>'; }).join('') : '<span class="empty">aucun symbole suivi</span>');
    set('agents', ag.length ? ag.map(function(a){ return '<span>'+esc(a)+'</span>'; }).join('') : '<span class="empty">aucun agent actif</span>');
    var mu = s.model_usage || {}, td = mu.calls_by_tier_day || {}, calls = 0, anyCalls = false;
    Object.keys(td).forEach(function(k){ if (isNum(td[k])) { calls += td[k]; anyCalls = true; } });
    set('models_line', 'Modèles : ' + (isNum(mu.spent_usd) ? esc(mu.spent_usd.toLocaleString('fr-FR', {minimumFractionDigits: 2, maximumFractionDigits: 2})) + ' $' : '—') + " aujourd'hui, " + (anyCalls ? calls : '—') + ' appel' + (calls === 1 ? '' : 's') + ', ' + dash(mu.cache_hits_day) + ' réponse' + (mu.cache_hits_day === 1 ? '' : 's') + ' en cache');

    rows('setups', s.top_setups, function(x){ x = x || {}; return '<tr><td>'+dash(x.symbol)+'</td><td>'+dash(x.side)+'</td><td>'+dash(x.setup_score)+'</td><td>'+dash(x.agent_id)+'</td><td>'+dash(x.verdict)+'</td></tr>'; }, 5, 'aucun setup ce cycle');

    // ---- positions du bot : une carte par position ----
    set('pos_head', posList.length ? posList.length + ' position' + (posList.length === 1 ? '' : 's') + ' · flottant total ' + smoney(floating) : '');
    if (!posList.length) set('positions', '<div class="empty">aucune position ouverte</div>');
    else set('positions', posList.map(function(x){
      x = x || {};
      var sell = x.side === 'SELL', stop = '';
      if (isNum(x.last_sl) && isNum(x.initial_sl)) {
        if (Math.abs(x.last_sl - x.initial_sl) < 1e-9) stop = 'stop = initial';
        else if ((sell && x.last_sl < x.initial_sl) || (!sell && x.last_sl > x.initial_sl)) stop = 'stop remonté';
        else stop = '<span class="neg">stop élargi ⚠</span>';
      } else stop = 'stop —';
      var badges = '<span class="badges">'
        + '<span class="'+(x.tp1_done ? 'on' : '')+'">TP1</span><span class="'+(x.tp2_done ? 'on' : '')+'">TP2</span>'
        + '<span class="'+(x.break_even_done ? 'on' : '')+'">BE</span><span class="'+(x.trailing_active ? 'on tr' : '')+'">trailing</span></span>';
      var tp = (x.tp_plan && x.tp_plan.length) ? x.tp_plan.map(function(v){ return isNum(v) ? esc(v) : '—'; }).join(' / ') : '—';
      return '<div class="poscard">'
        + '<div class="l1"><span>'+esc(x.symbol || '?')+' '+(sell ? '▼' : '▲')+' '+esc(x.side || '?')+'</span><span>· '+esc(x.agent_id || '?')+'</span><span>· '+esc(x.regime || '—')+'</span>'
        + '<span>· ouverte depuis '+esc(parisTime(x.opened_at))+(x.opened_at ? ' ('+esc(duration(x.opened_at, s.server_time_utc))+')' : '')+'</span>'+badges+'</div>'
        + '<div class="l2">risque '+money(x.initial_risk_money)+' ('+pct(x.risk_percent)+') · meilleur '+sr(x.max_r)+' / pire '+sr(x.min_r)+' · '+stop+'</div>'
        + (x.invalidation ? '<div class="l3">invalidation : '+esc(x.invalidation)+'</div>' : '')
        + '<details><summary>prix</summary><div class="l3">ticket '+dash(x.ticket)+' · entrée '+dash(x.entry)+' · SL initial '+dash(x.initial_sl)+' · SL actuel '+dash(x.last_sl)+' · TP '+tp+' · volume initial '+dash(x.initial_volume)+'</div></details>'
        + '</div>';
    }).join(''));

    // ---- meilleurs agents : top 5 par total R parmi les agents ayant au moins 3 trades ----
    var lb = s.leaderboard; if (!Array.isArray(lb)) lb = [];
    if (!lb.length && s.learning && typeof s.learning === 'object') {
      lb = Object.keys(s.learning).map(function(k){ var v = s.learning[k] || {}; return Object.assign({agent_id: v.agent_id || k}, v); });
    }
    var eligible = lb.filter(function(x){ x = x || {}; var n = isNum(x.trades) ? x.trades : x.sample_size; return isNum(n) && n >= 3; });
    eligible.sort(function(a, b){ var ka = isNum(a.total_r) ? a.total_r : (isNum(a.pnl) ? a.pnl : -Infinity), kb = isNum(b.total_r) ? b.total_r : (isNum(b.pnl) ? b.pnl : -Infinity); return kb - ka; });
    var lbEmpty = !lb.length ? 'pas encore de trade fermé' : 'aucun agent avec au moins 3 trades';
    rows('leaderboard', eligible.slice(0, 5), function(x){ x = x || {};
      var n = isNum(x.trades) ? x.trades : x.sample_size;
      var gp = (isNum(x.wins) && isNum(x.losses)) ? n + ' (' + x.wins + ' / ' + x.losses + ')' : dash(n);
      var wr = (isNum(x.wins) && isNum(x.losses) && (x.wins + x.losses) > 0) ? pct(100 * x.wins / (x.wins + x.losses), 0) : (isNum(x.win_rate) ? pct(x.win_rate <= 1 ? 100 * x.win_rate : x.win_rate, 0) : '—');
      var pf = x.no_losses ? '<span title="aucun trade perdant : profit factor non défini">aucune perte</span>' : (isNum(x.profit_factor) ? num(x.profit_factor) : '—');
      var tags = '';
      if (isNum(n) && n < 40) tags += ' <span class="badge off">en observation (&lt; 40 trades)</span>';
      if (x.status && x.status !== 'LIVE') tags += ' <span class="badge warn">'+esc(x.status)+'</span>';
      return '<tr class="lbrow"><td>'+dash(x.agent_id)+tags+'</td><td>'+gp+'</td><td>'+wr+'</td><td>'+pf+'</td><td>'+sr(x.expectancy_r)+'</td><td>'+sr(x.total_r)+'</td></tr>'; }, 6, lbEmpty);

    // ---- journal humain ----
    var j = s.journal_tail || [], lines = [], prev = null;
    j.slice().reverse().forEach(function(e){
      if (!journalAll && e.kind === 'copy_trade' && e.ok !== false) return;   // copies réussies : bruit
      var t = parisTime(e.ts_utc || e.ts_local);
      var h = journalAll ? null : humanEvent(e, bp);
      var body = (journalAll || !h) ? rawEvent(e) : '<span class="ico">'+h.ico+'</span> '+h.text;
      // même texte que la ligne précédente (ex. échec de copie répété à chaque cycle) : on compte au lieu de répéter
      if (prev && prev.body === body) { prev.n += 1; prev.last = t; return; }
      prev = {t: t, body: body, n: 1, last: t}; lines.push(prev);
    });
    lines = lines.map(function(l){ return '<div><span class="t">'+esc(l.t)+'</span> '+l.body+(l.n > 1 ? ' <span class="reasons">×'+l.n+' (jusqu’à '+esc(l.last)+')</span>' : '')+'</div>'; });
    set('journal', lines.length ? lines.join('') : '<div class="empty">'+(journalAll ? "aucun événement aujourd'hui" : "aucun événement de trading aujourd'hui")+'</div>');
  }

  function refresh(){
    var url = journalAll ? '/api/state' : '/api/state?kinds=' + JOURNAL_KINDS;
    fetch(url, {cache: 'no-store'}).then(function(r){
        if (r.status === 401) { window.location.href = '/login'; throw new Error('session expirée'); }
        if (!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(s){ $('error').style.display = 'none'; render(s); })
      .catch(function(e){ var el = $('error'); el.style.display = 'inline-block'; el.textContent = 'Données indisponibles : ' + e.message; });
  }
  $('journal_toggle').addEventListener('click', function(){
    journalAll = !journalAll;
    this.textContent = journalAll ? 'événements de trading seulement' : 'tout voir';
    this.className = 'btn' + (journalAll ? ' on' : '');
    refresh();
  });
  refresh();
  setInterval(refresh, 5000);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
