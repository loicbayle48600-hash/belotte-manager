"""Watchdog indépendant (processus séparé, aucune dépendance LLM).

Surveille : MT5 vivant, compte, positions du bot, présence des SL, fraîcheur des
données, drawdown journalier/global vs hard limits, heartbeat de l'orchestrateur.
Anomalie → demande SAFE_MODE (state/watchdog.json) et, pour un SL manquant,
agit DIRECTEMENT sur le broker : remise du SL attendu, sinon fermeture.

Il n'écrit jamais dans system_state.json (seul l'orchestrateur le fait) : il
publie ses constats dans state/watchdog.json, lu par l'orchestrateur, la CLI et le dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from ..core.config import Settings, load_settings
from ..core.journal import Journal
from ..core.state import StateStore
from ..core.types import TradeMode, utcnow
from ..mt5.adapter import BrokerAdapter
from ..mt5.mock_adapter import make_broker
from ..mt5.symbols import resolve_symbols


#: contrôles consécutifs (intervalle 3 s) sans connexion avant de demander SAFE_MODE
DISCONNECT_CHECKS = 2


@dataclass
class WatchdogReport:
    ts: str = ""
    heartbeat: str = ""
    mt5_connected: bool = False
    account_ok: bool = False
    trade_mode: str = "UNKNOWN"
    positions_bot: int = 0
    positions_without_sl: int = 0
    data_fresh: Optional[bool] = None
    orchestrator_alive: bool = False
    orchestrator_heartbeat_age: float = 0.0
    daily_dd_percent: float = 0.0
    overall_dd_percent: float = 0.0
    # pertes exprimées dans la base de la prop firm (% du solde initial) : ce sont elles que la prop
    # firm mesure, les deux champs ci-dessus restent les mesures internes en equity.
    prop_daily_loss_percent: float = 0.0
    prop_overall_loss_percent: float = 0.0
    safe_mode_request: bool = False
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


class Watchdog:
    def __init__(self, settings: Settings, broker: BrokerAdapter, store: StateStore, journal: Journal,
                 interval: float = 3.0, reference_symbol: Optional[str] = None):
        self.s = settings
        self.broker = broker
        self.store = store
        self.journal = journal
        self.interval = interval
        # alertes dédupliquées (2026-09-25, plan pro point 7) : la même alerte « DD jour ≥ 1 % » était journalisée
        # (et poussée sur Telegram) toutes les 3 s — 1 491 fois en deux heures
        self._alert_key: Optional[str] = None
        self._alert_ts: float = 0.0
        # 2026-09-27 : après un redémarrage le week-end, les cryptos fraîchement sélectionnées n'ont pas encore de tick
        # (« indisponible ») pendant ~10 min et le watchdog demandait SAFE_MODE. Pendant STARTUP_GRACE_SEC, un symbole
        # sans tick est « inconnu », pas « périmé » ; sans aucun âge mesurable, la fraîcheur n'est pas jugée.
        self._started_mono = time.monotonic()
        self.magic = settings.magic
        self.ref_symbol = reference_symbol
        self._ref_resolved: Optional[str] = None  # symbole broker réel (suffixe résolu), calculé une fois connecté
        self._ancrage_fait = False                # symboles sélectionnés pour calibrer l'heure serveur
        self._surveilles: list[str] = []          # symboles réels servant au contrôle de fraîcheur
        self.report_path = settings.state_dir / "watchdog.json"
        self.hard_daily = float(settings.prop.get("max_daily_loss_hard_percent", 4.0))
        self.hard_overall = float(settings.prop.get("max_overall_loss_hard_percent", 8.0))
        self.internal_daily = float(settings.risk.get("max_daily_loss_internal_percent", 1.0))
        self.max_hb_age = float(settings.system.get("heartbeat_max_age_sec", 45))
        self.max_tick_age = float(settings.system.get("data_max_age_sec", 30))
        self._running = True
        self._disconnect_streak = 0               # contrôles consécutifs sans connexion (débounce SAFE_MODE)
        # 2026-09-29 (demande utilisateur) : le 28/09 à 23h11, UN contrôle a vu toutes les cotations figées depuis 62 s
        # (cryptos comprises) puis tout est revenu en 15 s : gel passager du terminal → SAFE_MODE 41 s pour rien. Le
        # flux n'est déclaré figé qu'après `stale_confirm_sec` de contrôles périmés consécutifs.
        self.stale_confirm_sec = float(settings.system.get("stale_confirm_sec", 30))
        self._stale_since: Optional[float] = None

    def stop(self) -> None:
        self._running = False

    def _reference_symbol(self) -> Optional[str]:
        """Symbole broker réel du symbole de référence (racine → suffixe broker), résolu une fois.

        Sans résolution, `tick("EURUSD")` renverrait None chez un broker à suffixes (EURUSD.m)
        et le watchdog demanderait SAFE_MODE en permanence pour « données périmées ».
        """
        if not self.ref_symbol:
            return None
        if self._ref_resolved is None:
            try:
                self._ref_resolved = resolve_symbols([self.ref_symbol], self.broker.symbols()).get(self.ref_symbol) or ""
            except Exception:  # noqa: BLE001 - broker indisponible : on réessaiera au prochain cycle
                return None
        return self._ref_resolved or None

    def _ancrer_horloge(self) -> None:
        """Sélectionne des symboles d'un marché ouvert pour que l'heure serveur soit calibrable.

        Sans cela, la calibration n'interroge que les symboles déjà visibles chez le broker.
        Le week-end ils sont tous gelés : `server_time()` reste figé sur le dernier tick de
        vendredi, et **un flux mort paraît frais** — mesuré le 2026-09-20, le watchdog voyait
        un tick « vieux d'une seconde » sur un flux arrêté depuis 38 h. Le contrôle de
        fraîcheur, dont c'est toute la raison d'être, ne pouvait alors jamais se déclencher.

        La crypto sert d'ancre : c'est le seul marché ouvert en continu.
        """
        if self._ancrage_fait:
            return
        symboles: list[str] = []
        if self.ref_symbol:
            symboles.append(self.ref_symbol)
        cryptos = [str(x) for x in (self.s.markets.get("crypto") or [])][:6]
        symboles += cryptos
        try:
            reels = resolve_symbols(symboles, self.broker.symbols())
        except Exception:  # noqa: BLE001 - broker indisponible : on réessaiera au prochain cycle
            return
        # 2026-09-26 : l'ancrage passait AVANT la connexion au terminal ; la liste des symboles du broker était vide,
        # aucune crypto n'était retenue et l'ancrage était déclaré fait pour toujours. Seul EURUSD restait surveillé :
        # le samedi, forex gelé → « aucun marché suivi ne cote » → SAFE_MODE, aucun trade crypto du week-end.
        # On réessaie à chaque contrôle tant qu'aucune crypto n'est résolue.
        if cryptos and not any(reels.get(c) for c in cryptos):
            return
        for reel in reels.values():
            if reel:
                try:
                    self.broker.symbol_select(reel)
                except Exception:  # noqa: BLE001 - un symbole refusé ne doit pas arrêter le watchdog
                    continue
                if reel not in self._surveilles:
                    self._surveilles.append(reel)
        self._ancrage_fait = True

    def check_once(self) -> WatchdogReport:
        rep = WatchdogReport(ts=utcnow().isoformat(), heartbeat=utcnow().isoformat())
        state = self.store.reload()
        self._ancrer_horloge()
        # 1. connexion
        if not self.broker.is_connected():
            ok = self.broker.connect()
            if not ok:
                # Débounce (2026-09-23) : « Authorization failed (-6) » / « IPC timeout (-10005) » durent
                # 1 à 3 contrôles (≤ 10 s) et provoquaient une demande de SAFE_MODE immédiate — mesuré
                # 6 bascules AUTO→SAFE→AUTO en 4 jours pour des coupures IPC sans conséquence. Une
                # coupure n'est signalée qu'après DISCONNECT_CHECKS contrôles consécutifs (≈ 6 s) ;
                # `mt5_connected` reste vrai à chaque contrôle (le gate 01_health, lui, lit l'état réel).
                self._disconnect_streak += 1
                if self._disconnect_streak >= DISCONNECT_CHECKS:
                    rep.reasons.append(f"MT5 déconnecté: {self.broker.last_error()}")
                else:
                    self.journal.event("watchdog_notice", message="connexion MT5 en échec, nouvel essai au prochain contrôle",
                                       error=self.broker.last_error(), streak=self._disconnect_streak)
        if self.broker.is_connected():
            self._disconnect_streak = 0
        rep.mt5_connected = self.broker.is_connected()
        if rep.mt5_connected:
            acc = self.broker.account_info()
            rep.account_ok = acc is not None and acc.equity > 0
            if acc:
                rep.trade_mode = acc.trade_mode.value
                # `starting_equity` / `overall_peak_equity` / `reference_equity` sont nets des retraits
                # simulés (unités du labo) : on compare à une equity nette, pas à l'equity brute du broker.
                equity = acc.equity - state.simulated_withdrawn_total()
                if state.daily.starting_equity:
                    rep.daily_dd_percent = max(0.0, 100 * (state.daily.starting_equity - equity) / state.daily.starting_equity)
                if state.overall_peak_equity:
                    rep.overall_dd_percent = max(0.0, 100 * (state.overall_peak_equity - equity) / state.overall_peak_equity)
                # bases prop : plancher du jour assis sur max(solde, equity) au reset, perte totale
                # statique sur le solde initial — jamais sur un pic d'equity.
                base = state.initial_balance or acc.balance or acc.equity
                ref = state.daily.reference_equity or max(state.daily.starting_equity, state.daily.starting_balance)
                if base > 0:
                    if ref > 0:
                        rep.prop_daily_loss_percent = max(0.0, 100 * (ref - equity) / base)
                    rep.prop_overall_loss_percent = max(0.0, 100 * (base - equity) / base)
                if rep.daily_dd_percent >= self.internal_daily:
                    rep.reasons.append(f"DD jour {rep.daily_dd_percent:.2f}% >= limite interne {self.internal_daily}%")
                if (rep.prop_daily_loss_percent >= self.hard_daily * 0.75
                        or rep.prop_overall_loss_percent >= self.hard_overall * 0.75):
                    rep.reasons.append(f"drawdown proche des hard limits prop "
                                       f"(jour {rep.prop_daily_loss_percent:.2f}%/{self.hard_daily}%, "
                                       f"total {rep.prop_overall_loss_percent:.2f}%/{self.hard_overall}%)")
            # 2. positions du bot sans SL → action directe
            positions = self.broker.positions(magic=self.magic)
            rep.positions_bot = len(positions)
            for p in positions:
                if not p.has_sl:
                    rep.positions_without_sl += 1
                    plan = state.bot_positions.get(str(p.ticket))
                    target = plan.last_sl or plan.initial_sl if plan else 0.0
                    fixed = False
                    if target and target > 0:
                        res = self.broker.modify_position(p.ticket, target, p.tp)
                        fixed = res.ok
                        rep.actions.append(f"ticket {p.ticket}: SL remis à {target} → {'ok' if fixed else res.comment}")
                    if not fixed:
                        res = self.broker.close_position(p.ticket, comment="WATCHDOG no-SL")
                        rep.actions.append(f"ticket {p.ticket}: fermé (SL impossible) → {'ok' if res.ok else res.comment}")
                    rep.reasons.append(f"position {p.ticket} sans SL")
            # 3. fraîcheur des données
            # Le flux n'est pas mort parce qu'UN marché est fermé : le week-end, le forex est
            # gelé alors que la crypto cote. On exige donc qu'AUCUN symbole suivi ne cote avant
            # de conclure au flux figé — sinon le watchdog demanderait SAFE_MODE tous les
            # week-ends et empêcherait le trading crypto qu'il est censé protéger.
            surveilles = list(self._surveilles)
            ref = self._reference_symbol()
            if ref and ref not in surveilles:
                surveilles.append(ref)
            if not surveilles and positions:
                surveilles = [positions[0].symbol]
            if surveilles:
                ages: dict[str, float] = {}
                en_grace = (time.monotonic() - self._started_mono) < self.STARTUP_GRACE_SEC
                for sym in surveilles:
                    t = self.broker.tick(sym)
                    if t is None and en_grace:
                        continue                      # pas encore de tick après sélection : inconnu, pas périmé
                    ages[sym] = t.age_seconds(self.broker.server_time()) if t else float("inf")
                plus_frais = min(ages.values()) if ages else None
                rep.data_fresh = (plus_frais <= self.max_tick_age) if plus_frais is not None else None
                if rep.data_fresh is False:
                    if self._stale_since is None:
                        self._stale_since = time.monotonic()
                    confirme = (time.monotonic() - self._stale_since) >= self.stale_confirm_sec
                else:
                    self._stale_since = None
                    confirme = False
                if rep.data_fresh is False and confirme:
                    # Aucun marché suivi ne cote : flux réellement figé → SAFE_MODE demandé.
                    detail = ", ".join(f"{s}: {a:.0f}s" if a != float("inf") else f"{s}: indisponible"
                                       for s, a in sorted(ages.items(), key=lambda kv: kv[1]))
                    rep.reasons.append(f"données périmées — aucun marché suivi ne cote ({detail})")
        # 4. heartbeat orchestrateur
        age = self.store.heartbeat_age("orchestrator")
        rep.orchestrator_heartbeat_age = age if age != float("inf") else -1
        rep.orchestrator_alive = age <= self.max_hb_age
        if not rep.orchestrator_alive and state.bot_positions:
            rep.reasons.append(f"orchestrateur silencieux depuis {age:.0f}s avec positions ouvertes")
        rep.safe_mode_request = bool(rep.reasons)
        self._write(rep)
        self._emit_alert(rep)
        return rep

    #: tolérance après le démarrage : un symbole sans tick n'est pas compté comme périmé
    STARTUP_GRACE_SEC = 900.0

    #: rappel d'une alerte inchangée qui persiste
    ALERT_REMINDER_SEC = 900.0

    @staticmethod
    def _alert_signature(reasons: list) -> str:
        """Nature de l'alerte sans ses chiffres : « DD jour 1.13% » et « DD jour 1.14% » sont la même alerte."""
        import re
        return "|".join(sorted(re.sub(r"[-+]?\d+(?:[.,]\d+)?", "#", str(r)) for r in reasons))

    def _emit_alert(self, rep, now: Optional[float] = None) -> None:
        """Journalise une alerte quand elle apparaît, quand sa nature change, toutes les 15 min si elle persiste, et
        toujours quand le watchdog a AGI. Le retour à la normale est journalisé une fois (`watchdog_ok`)."""
        now = time.monotonic() if now is None else now
        if not rep.reasons and not rep.actions:
            if self._alert_key is not None:
                self.journal.event("watchdog_ok", message="plus aucune alerte")
                self._alert_key = None
            return
        key = self._alert_signature(rep.reasons)
        if rep.actions or key != self._alert_key or now - self._alert_ts >= self.ALERT_REMINDER_SEC:
            self.journal.event("watchdog_alert", level="WARNING", reasons=rep.reasons, actions=rep.actions,
                               **({"rappel": True} if key == self._alert_key and not rep.actions else {}))
            self._alert_key, self._alert_ts = key, now

    def _write(self, rep: WatchdogReport) -> None:
        tmp = self.report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(rep), ensure_ascii=False, indent=1), encoding="utf-8")
        # Windows : os.replace échoue (PermissionError) si un lecteur tient watchdog.json ouvert
        # (CLI, dashboard, orchestrateur) → quelques tentatives brèves avant d'abandonner l'itération.
        for attempt in range(3):
            try:
                os.replace(tmp, self.report_path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)

    def run(self) -> None:
        self.journal.info("watchdog démarré", interval=self.interval, magic=self.magic)
        while self._running:
            try:
                self.check_once()
            except Exception as e:  # noqa: BLE001 - le watchdog ne doit jamais mourir
                self.journal.error("watchdog exception", error=f"{type(e).__name__}: {e}")
            time.sleep(self.interval)


def read_watchdog_report(state_dir: Path) -> Optional[dict]:
    """Lit state/watchdog.json ; None si absent, illisible (E/S, sharing violation Windows) ou invalide.

    Appelé dans le chemin critique de l'orchestrateur et par la CLI : ne lève jamais.
    """
    p = Path(state_dir) / "watchdog.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Watchdog indépendant Claude MT5 Trading Lab")
    ap.add_argument("--home", default=None)
    ap.add_argument("--interval", type=float, default=None)
    ap.add_argument("--broker", default=None, help="mt5 | mock")
    args = ap.parse_args(argv)
    s = load_settings(Path(args.home) if args.home else None)
    kind = args.broker or s.broker_kind
    broker = make_broker(kind, s)
    journal = Journal(s.logs_dir, s.system.get("timezone_local", "UTC"), component="watchdog")
    store = StateStore(s.state_dir)
    # Symbole de référence pour la fraîcheur des données même sans position ouverte
    # (premier symbole de l'univers de marchés), sinon data_fresh resterait toujours None.
    ref_symbol = next((sym for group in s.markets.values() if isinstance(group, list) for sym in group), None)
    wd = Watchdog(s, broker, store, journal, interval=args.interval or float(s.scheduler.get("watchdog_interval_sec", 3)),
                  reference_symbol=ref_symbol)
    try:
        wd.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
