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
        self.magic = settings.magic
        self.ref_symbol = reference_symbol
        self._ref_resolved: Optional[str] = None  # symbole broker réel (suffixe résolu), calculé une fois connecté
        self.report_path = settings.state_dir / "watchdog.json"
        self.hard_daily = float(settings.prop.get("max_daily_loss_hard_percent", 4.0))
        self.hard_overall = float(settings.prop.get("max_overall_loss_hard_percent", 8.0))
        self.internal_daily = float(settings.risk.get("max_daily_loss_internal_percent", 1.0))
        self.max_hb_age = float(settings.system.get("heartbeat_max_age_sec", 45))
        self.max_tick_age = float(settings.system.get("data_max_age_sec", 30))
        self._running = True

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

    def check_once(self) -> WatchdogReport:
        rep = WatchdogReport(ts=utcnow().isoformat(), heartbeat=utcnow().isoformat())
        state = self.store.reload()
        # 1. connexion
        if not self.broker.is_connected():
            ok = self.broker.connect()
            if not ok:
                rep.reasons.append(f"MT5 déconnecté: {self.broker.last_error()}")
        rep.mt5_connected = self.broker.is_connected()
        if rep.mt5_connected:
            acc = self.broker.account_info()
            rep.account_ok = acc is not None and acc.equity > 0
            if acc:
                rep.trade_mode = acc.trade_mode.value
                if state.daily.starting_equity:
                    rep.daily_dd_percent = max(0.0, 100 * (state.daily.starting_equity - acc.equity) / state.daily.starting_equity)
                if state.overall_peak_equity:
                    rep.overall_dd_percent = max(0.0, 100 * (state.overall_peak_equity - acc.equity) / state.overall_peak_equity)
                if rep.daily_dd_percent >= self.internal_daily:
                    rep.reasons.append(f"DD jour {rep.daily_dd_percent:.2f}% >= limite interne {self.internal_daily}%")
                if rep.daily_dd_percent >= self.hard_daily * 0.75 or rep.overall_dd_percent >= self.hard_overall * 0.75:
                    rep.reasons.append("drawdown proche des hard limits")
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
            sym = self._reference_symbol() or (positions[0].symbol if positions else None)
            if sym:
                t = self.broker.tick(sym)
                age = t.age_seconds(self.broker.server_time()) if t else float("inf")
                rep.data_fresh = age <= self.max_tick_age
                if rep.data_fresh is False:
                    # Flux figé / tick indisponible : donnée périmée → SAFE_MODE demandé (jamais silencieux).
                    rep.reasons.append(f"données périmées ({sym}: tick vieux de {age:.0f}s > {self.max_tick_age:.0f}s)"
                                       if age != float("inf") else f"données périmées ({sym}: tick indisponible)")
        # 4. heartbeat orchestrateur
        age = self.store.heartbeat_age("orchestrator")
        rep.orchestrator_heartbeat_age = age if age != float("inf") else -1
        rep.orchestrator_alive = age <= self.max_hb_age
        if not rep.orchestrator_alive and state.bot_positions:
            rep.reasons.append(f"orchestrateur silencieux depuis {age:.0f}s avec positions ouvertes")
        rep.safe_mode_request = bool(rep.reasons)
        self._write(rep)
        if rep.reasons or rep.actions:
            self.journal.event("watchdog_alert", level="WARNING", reasons=rep.reasons, actions=rep.actions)
        return rep

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
