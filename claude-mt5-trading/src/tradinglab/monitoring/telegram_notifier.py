"""Notifications Telegram : processus séparé qui suit le journal et pousse les événements.

Pourquoi un processus à part, et non un appel dans la boucle : un envoi réseau coûte une à
trois secondes d'incertitude à chaque fois, or le heartbeat de l'orchestrateur est à 45 s.
Si Telegram rame ou tombe, le watchdog déclarerait l'orchestrateur mort et basculerait en
SAFE_MODE. Découplé, le pire qui puisse arriver est un message perdu.

Lancement :
    python -m tradinglab.monitoring.telegram_notifier [--home DIR] [--since-now]

Configuration (dans ``.env``, jamais ailleurs) :
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...
    TELEGRAM_KINDS=position_opened,order_send,...   (facultatif, remplace la liste par défaut)

Le processus reprend là où il s'était arrêté (``state/telegram_notifier.json``) : un
redémarrage ne rejoue pas la journée. Aucune exception ne remonte : une panne de réseau
fait attendre, jamais planter.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.config import load_dotenv, load_settings

API = "https://api.telegram.org"
POLL_SEC = 2.0                  # relecture du journal
RETRY_SEC = 15.0                # attente après un échec réseau
MAX_MESSAGE_CHARS = 3500        # limite Telegram : 4096, on garde de la marge
MAX_BATCH = 20                  # événements regroupés dans un même envoi

#: événements poussés par défaut — l'exploitation courante, pas le bruit de diagnostic
DEFAULT_KINDS = (
    "position_opened", "position_managed", "position_closed", "post_trade_review",
    "order_send", "execution", "mode_change", "watchdog_alert", "error", "emergency_action",
    "simulated_withdrawal", "report_day", "payout_window", "payout_done", "payout_ready", "report_week",
)
#: toujours envoyés, même si TELEGRAM_KINDS restreint la liste (rapport demandé par l'utilisateur le 2026-09-24)
ALWAYS_KINDS = ("report_week", "report_day")    # report_day : rapport de 17 h New York (2026-09-25)

ICONES = {
    "position_opened": "🟢", "position_managed": "🔧", "position_closed": "⚪",
    "partial_tp": "🎯", "early_exit": "🚪", "sl_modified": "🛡",
    "post_trade_review": "📑", "order_send": "📨", "execution": "📨",
    "mode_change": "🔁", "watchdog_alert": "⚠️", "error": "❌",
    "emergency_action": "🚨", "simulated_withdrawal": "💸", "report_day": "📊",
    "payout_window": "🏦", "payout_done": "💰", "payout_ready": "💰", "report_week": "📊",
}


def _fmt_nombre(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.2f}".replace(",", " ") if abs(f) >= 1000 else f"{f:g}"


def format_event(ev: dict) -> str:
    """Rend un événement du journal en une ligne lisible. Ne lève jamais."""
    kind = str(ev.get("kind", "?"))
    icone = ICONES.get(kind, "•")
    heure = str(ev.get("ts_local") or ev.get("ts_utc") or "")[11:19]
    try:
        if kind == "position_opened":
            p = ev.get("position") or ev
            return (f"{icone} <b>Position ouverte</b> {heure}\n"
                    f"{p.get('symbol')} {p.get('side')} {_fmt_nombre(p.get('volume'))} lot\n"
                    f"entrée {_fmt_nombre(p.get('price') or p.get('entry'))} · SL {_fmt_nombre(p.get('sl'))}\n"
                    f"agent {p.get('agent_id', '?')} · ticket {p.get('ticket', '?')}")
        if kind == "partial_tp":
            # `gain_estime` est calculé côté position_manager (R x risque initial x part fermée),
            # pas lu chez le broker : le montant exact arrive à la clôture complète.
            gain = ev.get("gain_estime")
            txt = (f"{icone} <b>{ev.get('level', 'TP')} atteint</b> {heure} · {ev.get('symbol', '?')}\n"
                   f"fermé {_fmt_nombre(ev.get('volume'))} à {_fmt_nombre(ev.get('r'))} R")
            if gain is not None:
                txt += f" · ~{_fmt_nombre(gain)} $"
            return txt + f"\nreste {_fmt_nombre(ev.get('volume_restant'))} en position"
        if kind == "early_exit":
            return (f"{icone} <b>Sortie anticipée</b> {heure} · <b>{ev.get('symbol', '?')}</b> · ticket {ev.get('ticket', '?')}\n"
                    f"{ev.get('reason', '—')} à {_fmt_nombre(ev.get('r'))} R")
        if kind in ("position_closed", "post_trade_review"):
            rev = ev.get("review") or {}
            txt = (f"{icone} <b>Position fermée</b> {heure} · <b>{ev.get('symbol') or rev.get('symbol') or '?'}</b>"
                   f"{(' ' + str(ev.get('side'))) if ev.get('side') else ''}\n"
                   f"agent {ev.get('agent_id', '?')}")
            if ev.get("volume"):
                txt += f" · {_fmt_nombre(ev.get('volume'))} lot"
            if ev.get("exit_reason"):
                txt += f" · sortie {ev.get('exit_reason')}"
            # 2026-09-28, demande utilisateur : commission, spread et bénéfice NET sur chaque récap
            if ev.get("brut") is not None or ev.get("commission") is not None:
                txt += f"\nbrut {_fmt_nombre(ev.get('brut', 0))} $ · commission {_fmt_nombre(ev.get('commission', 0))} $"
                if ev.get("swap"):
                    txt += f" · swap {_fmt_nombre(ev.get('swap'))} $"
                if ev.get("spread_cost") is not None:
                    txt += f"\nspread à l'entrée ≈ {_fmt_nombre(ev.get('spread_cost'))} $ (compris dans le brut)"
            return (txt + f"\nnet <b>{_fmt_nombre(ev.get('pnl', ev.get('profit', 0)))} $</b> "
                    f"({_fmt_nombre(ev.get('result_r', ev.get('r_multiple', 0)))} R)\n"
                    f"verdict {rev.get('verdict') or ev.get('verdict', '—')}")
        if kind == "position_managed":
            return (f"{icone} <b>Gestion</b> {heure} · {ev.get('symbol', '?')} · "
                    f"{ev.get('action', ev.get('reason', '—'))}")
        if kind in ("order_send", "execution"):
            ok = ev.get("ok", ev.get("approved"))
            statut = "accepté" if ok else "refusé"
            return (f"{icone} <b>Ordre {statut}</b> {heure} · {ev.get('symbol', '?')}\n"
                    f"{ev.get('comment') or ev.get('reason') or ''}".rstrip())
        if kind == "mode_change":
            return f"{icone} <b>Mode → {ev.get('mode', '?')}</b> {heure}\n{ev.get('reason', '')}".rstrip()
        if kind == "watchdog_alert":
            raisons = ev.get("reasons") or []
            return f"{icone} <b>Watchdog</b> {heure}\n" + "\n".join(f"· {r}" for r in raisons[:5])
        if kind == "emergency_action":
            return (f"{icone} <b>Action d'urgence</b> {heure} · {ev.get('command', '?')}\n"
                    f"fermées {ev.get('closed')} · annulées {ev.get('cancelled')}")
        if kind == "simulated_withdrawal":
            return (f"{icone} <b>Retrait simulé</b> {heure} · {_fmt_nombre(ev.get('amount'))}\n"
                    f"solde labo {_fmt_nombre(ev.get('balance_after'))} "
                    f"(broker {_fmt_nombre(ev.get('broker_balance'))})")
        if kind == "report_day":
            # texte brut protégé (2026-10-01) : un « < » ou un « & » dans le rapport faisait refuser tout le message
            return f"📊 <b>Rapport du jour</b>\n{html.escape(str(ev.get('text', '')), quote=False)}"
        if kind == "report_week":
            return (f"📊 <b>Rapport hebdomadaire</b> {html.escape(str(ev.get('week', '')), quote=False)}\n"
                    f"{html.escape(str(ev.get('text', '')), quote=False)}")
        if kind == "error":
            return f"{icone} <b>Erreur</b> {heure}\n{ev.get('message', '')}"
    except Exception:  # noqa: BLE001 - un événement mal formé ne doit pas arrêter le notifieur
        pass
    reste = {k: v for k, v in ev.items() if k not in ("kind", "ts_utc", "ts_local", "component")}
    return f"{icone} <b>{kind}</b> {heure}\n{json.dumps(reste, ensure_ascii=False)[:400]}"


class TelegramSender:
    """Envoi HTTP minimal. Renvoie True/False, ne lève jamais, ne journalise aucun secret."""

    def __init__(self, token: str, chat_id: str, timeout: float = 15.0, state_file: Optional[Path] = None) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = float(timeout)
        # 2026-10-01 : le groupe de l'utilisateur a été converti en SUPERGROUPE (nouvel identifiant) ; chaque envoi était
        # refusé (« group chat was upgraded to a supergroup chat ») sans aucune trace. L'identifiant de migration renvoyé
        # par Telegram est suivi automatiquement et mémorisé ici (jamais dans .env, que seul l'utilisateur modifie).
        self.state_file = Path(state_file) if state_file else None
        migre = self._migrations().get(self.chat_id)
        if migre:
            self.chat_id = str(migre)

    def _migrations(self) -> dict:
        if self.state_file is None or not self.state_file.exists():
            return {}
        try:
            return dict(json.loads(self.state_file.read_text(encoding="utf-8")).get("chat_migre") or {})
        except (OSError, ValueError, TypeError):
            return {}

    def _memoriser_migration(self, ancien: str, nouveau: str) -> None:
        if self.state_file is None:
            return
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        except (OSError, ValueError):
            d = {}
        d.setdefault("chat_migre", {})[ancien] = nouveau
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass

    def send(self, text: str, _relance: bool = True) -> bool:
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(f"{API}/bot{self.token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8")).get("ok", False)
        except urllib.error.HTTPError as e:
            # corps JSON de Telegram : jamais l'URL (elle contient le token)
            try:
                corps = json.loads(e.read().decode("utf-8"))
            except (OSError, ValueError):
                corps = {}
            nouveau = (corps.get("parameters") or {}).get("migrate_to_chat_id")
            if nouveau and _relance:
                ancien, self.chat_id = self.chat_id, str(nouveau)
                self._memoriser_migration(ancien, self.chat_id)
                print(f"conversation Telegram convertie en supergroupe : nouvel identifiant {self.chat_id} "
                      f"(à reporter dans .env, TELEGRAM_CHAT_ID)", file=sys.stderr, flush=True)
                return self.send(text, _relance=False)
            print(f"envoi Telegram refusé : HTTP {e.code} {corps.get('description', '')}", file=sys.stderr, flush=True)
            return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            print(f"envoi Telegram impossible : {type(e).__name__}", file=sys.stderr, flush=True)
            return False        # le token ne doit jamais fuir dans un message d'erreur


class JournalTail:
    """Suit le journal du jour et bascule automatiquement au changement de date."""

    def __init__(self, logs_dir: Path, state_file: Path) -> None:
        self.logs_dir = Path(logs_dir)
        self.state_file = Path(state_file)
        self.fichier: str = ""
        self.offset: int = 0
        self._charger()

    def _charger(self) -> None:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.fichier, self.offset = str(d.get("fichier", "")), int(d.get("offset", 0))
        except (OSError, ValueError, TypeError):
            self.fichier, self.offset = "", 0

    def _sauver(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"fichier": self.fichier, "offset": self.offset}),
                                       encoding="utf-8")
        except OSError:
            pass

    def chemin_du_jour(self, now: Optional[datetime] = None) -> Path:
        j = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        return self.logs_dir / f"journal-{j}.jsonl"

    def aller_a_la_fin(self) -> None:
        p = self.chemin_du_jour()
        self.fichier = p.name
        self.offset = p.stat().st_size if p.exists() else 0
        self._sauver()

    def nouvelles_lignes(self) -> list[dict]:
        p = self.chemin_du_jour()
        if p.name != self.fichier:          # changement de jour : on repart du début du nouveau fichier
            self.fichier, self.offset = p.name, 0
        if not p.exists():
            return []
        try:
            taille = p.stat().st_size
            if taille < self.offset:        # fichier tronqué ou remplacé
                self.offset = 0
            if taille == self.offset:
                return []
            with p.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                brut = f.read()
                self.offset = f.tell()
        except OSError:
            return []
        self._sauver()
        out: list[dict] = []
        for ligne in brut.splitlines():
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                d = json.loads(ligne)
            except ValueError:
                continue                    # ligne partielle : elle reviendra au prochain tour
            if isinstance(d, dict):
                out.append(d)
        return out


def run(home: Optional[Path] = None, since_now: bool = True, max_tours: Optional[int] = None) -> int:
    load_dotenv((home or Path.cwd()) / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID absent de .env : notifieur non démarré")
        return 2
    s = load_settings(home)
    kinds = tuple(k.strip() for k in os.environ.get("TELEGRAM_KINDS", "").split(",") if k.strip()) or DEFAULT_KINDS
    kinds = tuple(dict.fromkeys(kinds + ALWAYS_KINDS))
    sender = TelegramSender(token, chat, state_file=s.state_dir / "telegram_chat.json")
    tail = JournalTail(s.logs_dir, s.state_dir / "telegram_notifier.json")
    if since_now:
        tail.aller_a_la_fin()               # un démarrage ne rejoue pas la journée
    sender.send(f"🤖 <b>Notifieur démarré</b>\nsuivi : {', '.join(kinds[:6])}…")
    tours = 0
    while max_tours is None or tours < max_tours:
        tours += 1
        evs = [e for e in tail.nouvelles_lignes() if e.get("kind") in kinds]
        if evs:
            for i in range(0, len(evs), MAX_BATCH):
                paquet = evs[i:i + MAX_BATCH]
                if not sender.send("\n\n".join(format_event(e) for e in paquet)):
                    time.sleep(RETRY_SEC)
        time.sleep(POLL_SEC)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Notifieur Telegram du Claude MT5 Trading Lab")
    ap.add_argument("--home", default=None)
    ap.add_argument("--replay-today", action="store_true",
                    help="rejoue le journal du jour depuis le début (par défaut : seulement la suite)")
    ap.add_argument("--tours", type=int, default=None, help="nombre de tours de boucle (tests)")
    a = ap.parse_args(argv)
    try:
        return run(Path(a.home) if a.home else None, since_now=not a.replay_today, max_tours=a.tours)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
