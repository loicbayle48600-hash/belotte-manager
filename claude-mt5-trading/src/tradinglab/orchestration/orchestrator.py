"""Orchestrateur autonome principal.

Boucle : santé → compte → positions → données → news → régimes → agents actifs →
screeners Python → revue (LLM optionnelle) → classement → gardes (news,
corrélation, risque, prop, gate) → exécution → gestion des positions →
protections journalières → journal → apprentissage → recherche (hors chemin critique).

Sécurité : démarrage TOUJOURS en SAFE_MODE ; aucune ré-ouverture d'anciens ordres
(clés d'idempotence persistées + adoption des positions existantes) ; le watchdog
peut forcer SAFE_MODE via state/watchdog.json.
"""
from __future__ import annotations

import argparse
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..agents.registry import AgentRegistry
from ..agents.review import AdversarialReview
from ..core.clock import forex_market_open
from ..core.config import Settings, load_settings
from ..core.journal import Journal
from ..core.state import StateStore, SystemMode
from ..core.types import AgentStatus, Side, TradeCandidate, TradeMode, Verdict, utcnow
from ..execution.executor import Executor
from ..execution.gate import ExecutionGate, GateContext, max_spread_points_for
from ..core.version import code_version
from ..execution.invalidation import parse_invalidation
from ..execution.position_manager import MarketContext, PMConfig, PositionManager
from ..learning.post_trade import PostTradeAnalyzer, build_trade_record
from ..learning.store import LearningStore
from ..macro.cot import COTClient
from ..macro.sentiment import risk_sentiment
from ..market_data.feed import MarketDataFeed, MarketSnapshot
from ..market_data.indicators import last_closed, swing_points
from ..models.claude_code_client import claude_code_backend, make_llm_backend
from ..models.client import LLMClient
from ..models.router import ModelRouter
from ..monitoring.watchdog import read_watchdog_report
from ..mt5.adapter import BrokerAdapter
from ..mt5.mock_adapter import make_broker
from ..mt5.symbols import resolve_symbols, root_of
from ..news.hub import NewsHub
from ..news.providers import build_providers
from ..research.pipeline import DegradationManager, ResearchPipeline
from ..research.strategy_author import StrategyAuthor
from ..risk.correlation_guard import CorrelationGuard, CorrelationLimits
from ..risk.daily_guard import DailyGuard
from ..risk.prop_guard import PropGuard, PropProfile
from ..risk.risk_manager import RiskLimits, RiskManager
from ..shadow.shadow import ShadowTrader
from .market_router import MarketRouter
from .scheduler import Scheduler

MAX_ENTRIES_PER_CYCLE = 2
#: pendant un chargement long (snapshots), re-persister le heartbeat à cette cadence (< 45 s du watchdog)
HEARTBEAT_REFRESH_SEC = 15.0
#: durée minimale avant qu'une sortie anticipée (invalidation) puisse être décidée — règle FOXX « > 1 minute »
MIN_HOLD_SEC = 60.0
#: délai maximal de la revue IA d'un cycle (2026-09-25) : un appel `claude -p` bloqué 30 s puis l'arbitre portaient le
#: cycle à 48-52 s, au-delà de la tolérance du watchdog (45 s) — positions non gérées pendant ce temps
LLM_REVIEW_DEADLINE_SEC = 25.0
MAX_LLM_REVIEWS_PER_CYCLE = 3   # revue LLM limitée aux N meilleurs candidats non rejetés (le reste : déterministe)


class Orchestrator:
    def __init__(self, settings: Settings, broker: BrokerAdapter, requested_mode: str = "SAFE", now_fn=None):
        self.s = settings
        self.broker = broker
        self.requested_mode = requested_mode.upper()
        self.now_fn = now_fn or utcnow
        self.journal = Journal(settings.logs_dir, settings.system.get("timezone_local", "UTC"), component="orchestrator")
        self.store = StateStore(settings.state_dir)
        self.state = self.store.state
        self.magic = settings.magic
        # composants déterministes
        self.risk = RiskManager(RiskLimits.from_config(settings.risk))
        self.prop = PropGuard(PropProfile.from_config(settings.prop), settings.autonomous_demo, settings.autonomous_prop,
                              float(settings.risk.get("max_daily_loss_internal_percent", 1.0)))
        self.daily = DailyGuard(settings.risk, settings.daily_profit)
        # `settings.markets` n'expose que les listes de symboles : `.get("asset_class_rules")` y renvoie
        # toujours {} et le guard tournait donc sans aucune règle (constaté le 2026-09-21). Le repli codé
        # dans `asset_class_of` rattrape la majorité des cas, mais 17 symboles sur 109 restaient mal classés :
        # 16 indices comptés en « other » (STOXX50, F40, CHINA50, ES35, IT40, MidDE50…), NETH25 en « crypto »
        # à cause de « ETH », CHINAH en « forex », et XNGUSD en « forex ». Conséquence : quatre indices
        # européens très corrélés pouvaient s'empiler sans que le plafond de classe d'actif ne s'applique.
        self.corr = CorrelationGuard(CorrelationLimits.from_config(settings.correlation),
                                     settings.section("asset_class_rules"))
        self.gate = ExecutionGate(broker, self.risk, self.prop, self.daily, self.corr)
        # calendrier de la journée de trading prop (reset 17:00 America/New_York) : c'est lui qui décide
        # quand la perte quotidienne repart de zéro, pas le calendrier UTC.
        self.trading_day = self.prop.profile.trading_day_calendar()
        self.executor = Executor(broker, self.store, self.journal,
                                 idea_window_minutes=self.prop.profile.trade_idea_aggregation_minutes)
        self.pm = PositionManager(broker, self.store, self.journal, PMConfig.from_config(settings.profit_management), self.magic)
        self.pm.commission_per_lot = self.prop.commission_per_lot   # break-even commission comprise (2026-09-25)
        self.pm.commission_price = self.prop.commission_price       # crypto en % de la valeur (2026-09-26)
        tfs = settings.scheduler.get("timeframes", ["M5", "M15", "H1", "H4", "D1"])
        self.feed = MarketDataFeed(broker, tfs, bars=max(300, int(settings.system.get("min_bars_required", 250)) + 50),
                                   max_tick_age_sec=int(settings.system.get("data_max_age_sec", 30)),
                                   min_bars=int(settings.system.get("min_bars_required", 250)))
        self.registry = AgentRegistry(status_file=settings.data_dir / "agent_status.json", journal=self.journal)
        self.router_ = MarketRouter(self.registry, settings.markets)
        self.model_router = ModelRouter(settings.models)
        self.llm: Optional[LLMClient] = None
        self.review = AdversarialReview(None, float(settings.execution.get("required_setup_score", 65)),
                                        float(settings.execution.get("min_rr_required", 1.5)), int(settings.learning.get("min_sample_size", 40)))
        self.review.market_note = self._market_note
        self.news = NewsHub(build_providers(settings.news), settings.news, cache_dir=settings.data_dir / "cache")
        self.learning = LearningStore(settings.data_dir / "learning.db")
        self.post_trade = PostTradeAnalyzer(self.learning, None, settings.learning)
        self.shadow = ShadowTrader(self.learning, settings.state_dir / "shadow_positions.json",
                                   max_open=int(settings.learning.get("shadow_max_open", 50)),
                                   max_per_agent=int(settings.learning.get("shadow_max_open_per_agent", 0)))
        self.degradation = DegradationManager(self.registry, self.learning, settings.learning, self.journal)
        self.research: Optional[ResearchPipeline] = None
        # filtre de contexte COT (CFTC, hebdomadaire) : décision utilisateur du 2026-09-21 — avertit quand un
        # trade suit un positionnement spéculatif déjà extrême ; jamais bloquant, jamais de donnée inventée
        self.cot = COTClient(settings.data_dir / "cot_cache.json", journal=self.journal)
        sch = settings.scheduler
        self.scheduler = Scheduler({"position_manager": sch.get("position_manager_interval_sec", 15), "fast_scanner": sch.get("fast_scanner_interval_sec", 60),
                                    "news": sch.get("news_interval_sec", 120), "calendar": sch.get("calendar_interval_sec", 900),
                                    "research": sch.get("research_interval_sec", 3600), "models": settings.models.get("refresh_interval_sec", 3600),
                                    "export": 300, "cot": sch.get("cot_interval_sec", 6 * 3600)}, tfs)
        self.universe: dict[str, str] = {}      # racine -> symbole broker
        self.symbol_groups: dict[str, str] = {}  # racine -> groupe (forex_majors...)
        self.snapshots: dict[str, MarketSnapshot] = {}
        self.candidates_cache: dict[str, dict] = {}
        self._running = True
        self._healthy_cycles = 0
        self._research_lock = threading.Lock()
        self._research_thread: Optional[threading.Thread] = None
        self._bar_candidates: dict[str, TradeCandidate] = {}
        self._initialized = False   # initialisation post-connexion (compte, univers, adoption, modèles, recherche)

    # ------------------------------------------------------------------ démarrage
    def startup(self) -> bool:
        st = self.state
        st.restarts += 1
        st.started_at = self.now_fn().isoformat()
        st.set_mode(SystemMode.SAFE_MODE, "démarrage (safe_mode_on_startup)")
        self.store.save()
        self.journal.event("startup", restarts=st.restarts, requested_mode=self.requested_mode, broker=self.broker.name)
        for attempt in range(5):
            if self.broker.connect():
                break
            self.journal.warn("connexion broker échouée", attempt=attempt + 1, error=self.broker.last_error())
            time.sleep(min(30, 2 ** attempt))
        if not self.broker.is_connected():
            self.journal.error("broker injoignable au démarrage : SAFE_MODE, nouvelle tentative à chaque cycle")
            return False
        acc = self.broker.account_info()
        if acc is None:
            self.journal.error("account_info indisponible : SAFE_MODE, initialisation reportée au prochain cycle")
            return False
        self._post_connect_init(acc)
        return True

    def _check_account(self, acc) -> list[str]:
        """Contrôle déterministe du compte connecté par rapport à `account_expected` (login, serveur, DEMO).

        Met à jour l'état, journalise l'événement `account` et pose/retire le verrou ACCOUNT_MISMATCH.
        Renvoie la liste des écarts (vide si le compte est conforme).
        """
        st = self.state
        st.mt5_connected, st.account_trade_mode, st.account_login, st.account_server, st.currency = True, acc.trade_mode.value, acc.login, acc.server, acc.currency
        exp = self.s.get("account_expected", {}) or {}
        mismatch = []
        if exp.get("login") and int(exp["login"]) != acc.login:
            mismatch.append(f"login {acc.login} ≠ attendu {exp['login']}")
        if exp.get("server") and str(exp["server"]) != acc.server:
            mismatch.append(f"serveur {acc.server} ≠ attendu {exp['server']}")
        if acc.trade_mode is not TradeMode.DEMO:
            mismatch.append(f"trade_mode={acc.trade_mode.value} (DEMO requis pour l'automatisation initiale)")
        self.journal.event("account", **acc.public_dict(), mismatch=mismatch)
        if mismatch:
            st.lock_entries("ACCOUNT_MISMATCH")
            self.journal.error("compte inattendu : entrées verrouillées", mismatch=mismatch)
        else:
            st.unlock_entries("ACCOUNT_MISMATCH")
            self.journal.info("DEMO ACCOUNT CONFIRMED", login=acc.login, server=acc.server)
        return mismatch

    def _post_connect_init(self, acc) -> None:
        """Initialisation qui exige un broker connecté : contrôle du compte, univers, adoption des positions,
        modèles et pipeline de recherche. Appelée par `startup()` ou, si la connexion a échoué au démarrage,
        par le premier `cycle()` connecté (sinon l'orchestrateur tournerait sans univers ni adoption)."""
        st = self.state
        st.roll_day_if_needed(acc.equity, acc.balance, self.now_fn(), self.trading_day)
        st.update_equity(acc.equity, acc.balance)
        self._check_account(acc)
        self._build_universe()
        # reprise : adopter les positions existantes du bot, ne jamais ré-ouvrir
        fermees_hors_ligne = self.pm.sync()
        self.journal.event("recovery", bot_positions=list(st.bot_positions), executed_keys=len(st.executed_keys),
                           closed_offline=[p.ticket for p in fermees_hors_ligne])
        self._setup_models()
        self._setup_research()
        # Positions fermées pendant l'arrêt du bot. `sync()` les retire de l'état et les renvoie, mais le
        # démarrage jetait cette liste : le cycle normal appelle `_on_position_closed` (l. 266), pas la reprise.
        # Le 2026-09-21 BTCUSD a été fermée à +1 581 $ pendant une coupure de 74 min ; le gain est bien sur le
        # compte mais le trade n'existait ni dans `learning.db`, ni dans les statistiques de l'agent B08, ni dans
        # le P&L du jour, ni dans le compteur de pertes consécutives. Traité après `_setup_research` pour qu'un
        # challenger puisse réellement être créé si la revue post-trade le demande.
        for plan in fermees_hors_ligne:
            self._on_position_closed(plan, self.now_fn())
        self.scheduler.due("research", self.now_fn())   # pas de recherche au premier cycle (chemin critique)
        self.scheduler.due("models", self.now_fn())
        self._initialized = True
        self.store.save()

    def _build_universe(self) -> None:
        avail = self.broker.symbols()
        wanted: list[str] = []
        for group, syms in self.s.markets.items():
            if group in ("asset_class_rules",):
                continue
            if isinstance(syms, list):
                for s in syms:
                    wanted.append(s)
                    self.symbol_groups[root_of(s)] = group
        res = resolve_symbols(wanted, avail)
        self.universe = {}
        for w, real in res.items():
            if real and self.broker.symbol_select(real):
                self.universe[root_of(w)] = real
        missing = [w for w, r in res.items() if r is None]
        self.journal.event("universe", available=len(avail), resolved=len(self.universe), missing=missing)

    def _setup_models(self) -> None:
        try:
            sdk = make_llm_backend(self.s)      # None => SDK anthropic + ANTHROPIC_API_KEY
        except Exception as e:  # noqa: BLE001 - un backend absent ne doit jamais tuer le démarrage
            self.journal.warn("backend LLM indisponible", error=f"{type(e).__name__}: {e}")
            sdk = None
        self.model_router.refresh_availability(sdk)
        if self.model_router.available_models:
            # secours (2026-09-22, demande utilisateur) : si la clé API n'a plus de crédit, les appels
            # repartent par le terminal Claude (abonnement) sans interruption ni redémarrage. Inutile si
            # ce backend est DÉJÀ celui qui sert.
            fallback = None if sdk is not None else (lambda: claude_code_backend(self.s))
            self.llm = LLMClient(self.model_router, self.state, int(self.s.models.get("cache_ttl_sec", 240)),
                                 sdk_client=sdk, journal=self.journal, fallback_factory=fallback)
            self.review.llm = self.llm
            self.post_trade.llm = self.llm
        self.journal.event("models", **self.model_router.status(self.state))

    def _setup_research(self) -> None:
        specs = {sym: self.broker.symbol_info(sym) for sym in self.universe.values()}
        specs = {k: v for k, v in specs.items() if v}
        self.research = ResearchPipeline(self.registry, self.learning, self.s.learning, self.s.backtest,
                                         lambda sym, tf, n: self.broker.rates(sym, tf, n), specs, self.s.data_dir / "research", self.journal)

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> dict:
        started = time.monotonic()
        try:
            return self._cycle_body(started)
        finally:
            # Durée réelle (horloge monotone, pas `now_fn`). Un cycle plus long que `heartbeat_max_age_sec`
            # fait déclarer l'orchestrateur mort par le watchdog et bloque les entrées (`01_health`) sans
            # qu'aucune trace ne dise pourquoi : on le journalise, exception comprise.
            duration = self._cycle_duration(started)
            limit = float(self.s.system.get("heartbeat_max_age_sec", 45))
            if duration > limit:
                self.journal.warn("cycle plus long que la tolérance du watchdog", duration_sec=duration,
                                  heartbeat_max_age_sec=limit)

    @staticmethod
    def _cycle_duration(started: float) -> float:
        return round(time.monotonic() - started, 3)

    def _cycle_body(self, started: float) -> dict:
        now = self.now_fn()
        st = self.state
        st.orchestrator_heartbeat = now.isoformat()
        summary: dict = {"ts": now.isoformat(), "entries": 0, "candidates": 0}
        self._process_commands()
        self._apply_watchdog()
        # 1. santé + compte
        if not self.broker.is_connected() and not self.broker.connect():
            st.mt5_connected = False
            st.set_mode(SystemMode.SAFE_MODE, "broker déconnecté")
            self._healthy_cycles = 0
            self.store.save()
            summary["error"] = "broker déconnecté"
            return summary
        acc = self.broker.account_info()
        if acc is None:
            st.mt5_connected = False
            self.store.save()
            summary["error"] = "account_info None"
            return summary
        if not self._initialized:
            # connexion refusée au démarrage : on rejoue ici l'initialisation post-connexion
            self._post_connect_init(acc)
        elif acc.login != st.account_login or acc.server != st.account_server or acc.trade_mode.value != st.account_trade_mode:
            # le terminal MT5 a changé de compte en cours d'exécution : verrou + SAFE_MODE jusqu'à décision humaine (RESUME)
            previous = {"login": st.account_login, "server": st.account_server, "trade_mode": st.account_trade_mode}
            st.mt5_connected, st.account_trade_mode, st.account_login, st.account_server, st.currency = True, acc.trade_mode.value, acc.login, acc.server, acc.currency
            st.lock_entries("ACCOUNT_MISMATCH")
            st.set_mode(SystemMode.SAFE_MODE, "compte changé en cours d'exécution")
            self.requested_mode = "SAFE"
            self._healthy_cycles = 0
            self.journal.event("account", level="ERROR", account_changed=True, previous=previous, mismatch=["compte changé en cours d'exécution"],
                               **acc.public_dict())
            self.journal.error("compte changé en cours d'exécution : entrées verrouillées, SAFE_MODE", previous=previous, login=acc.login, server=acc.server)
        st.mt5_connected = True
        st.account_trade_mode = acc.trade_mode.value
        if st.roll_day_if_needed(acc.equity, acc.balance, now, self.trading_day):
            self.journal.event("new_day", trading_day=st.daily.day, starting_equity=acc.equity,
                               reference_equity=st.daily.reference_equity,
                               reset_at=self.trading_day.reset_at(now).isoformat())
        st.update_equity(acc.equity, acc.balance)
        # 2. positions : sync + post-trade sur les fermetures
        closed = self.pm.sync()
        for plan in closed:
            self._on_position_closed(plan, now)
        # 2b. cycle de paiement prop (décision utilisateur 2026-09-23)
        self._maybe_payout(now)
        # 2c. rapport hebdomadaire (lundi 08:00 Paris, 2026-09-24, point 2)
        self._maybe_weekly_report(now)
        # 2d. rapport quotidien à la clôture de la journée FOXX (17 h New York, plan pro du 2026-09-25, point 8)
        self._maybe_daily_report(now)
        self._export_master_positions(acc)
        self._export_statement(acc)
        # 3. données + news
        news_shock = {}
        if self.scheduler.due("news", now):
            self.news.refresh_news(now)
        if self.scheduler.due("calendar", now):
            self.news.refresh_calendar(now)
        if self.scheduler.due("cot", now):
            self.cot.maybe_refresh(now)
        st.news_data_degraded = self.news.state.degraded
        st.calendar_data_degraded = self.news.state.calendar_degraded
        self.snapshots = {}
        # Le premier cycle charge 250 barres × 5 timeframes pour tout l'univers (159 s mesurés le 2026-09-22
        # 20:11) : sans heartbeat pendant ce chargement, le watchdog déclarait l'orchestrateur mort dès
        # 45 s. Le heartbeat est re-persisté toutes les HEARTBEAT_REFRESH_SEC pendant la boucle.
        dernier_hb = time.monotonic()
        for root, sym in self.universe.items():
            spec = self.broker.symbol_info(sym)
            ccys = [spec.currency_base, spec.currency_profit] if spec else []
            shock = self.news.news_shock(ccys, now) if ccys else False
            try:
                self.snapshots[sym] = self.feed.snapshot(sym, now=now, news_shock=shock)
                if ccys:
                    self.snapshots[sym].news_events = self._news_events(ccys, now)
            except Exception as e:  # noqa: BLE001 - dégradation par symbole : absent des snapshots ce cycle, jamais inventé
                self.journal.warn("snapshot impossible", symbol=sym, error=f"{type(e).__name__}: {e}")
            if time.monotonic() - dernier_hb >= HEARTBEAT_REFRESH_SEC:
                st.orchestrator_heartbeat = self.now_fn().isoformat()
                self.store.save()
                dernier_hb = time.monotonic()
        st.regimes = {s: snap.regime.regime.value for s, snap in self.snapshots.items()}
        summary["macro"] = self._macro_context()
        # 4. gestion des positions ouvertes (cadence courte)
        if self.scheduler.due("position_manager", now):
            self._manage_positions()
        # 5. protections journalières (toujours)
        dd = self.daily.evaluate(st)
        if not dd.entries_allowed and st.new_trades_locked:
            summary["locked"] = st.lock_reasons
        # 6. scan / candidats / exécution (cadence : nouvelle barre M5 ou fast scanner)
        new_bars = self.scheduler.new_bars(now)
        if new_bars or self.scheduler.due("fast_scanner", now):
            summary.update(self._scan_and_execute(now, dd, macro=summary["macro"]))
        # 7. shadow
        self.shadow.update(self.snapshots, now)
        # 8. recherche / dégradation / modèles (hors chemin critique)
        if self.scheduler.due("research", now):
            self._start_research_thread()
        if self.scheduler.due("models", now) and self.model_router.needs_refresh():
            self._setup_models()
        if self.scheduler.due("export", now):
            self._export_learning()
        # 9. passage en AUTO après cycles sains si demandé
        self._maybe_go_auto(acc)
        summary["duration_sec"] = self._cycle_duration(started)
        st.last_cycle = summary
        self.store.save()
        return summary

    # ------------------------------------------------------------------ helpers
    def _maybe_go_auto(self, acc) -> None:
        st = self.state
        healthy = self.broker.is_connected() and acc is not None and acc.trade_mode is TradeMode.DEMO and "ACCOUNT_MISMATCH" not in st.lock_reasons
        wd = read_watchdog_report(self.s.state_dir)
        if wd and wd.get("safe_mode_request"):
            healthy = False
        self._healthy_cycles = self._healthy_cycles + 1 if healthy else 0
        if self.requested_mode == "AUTO" and st.mode == SystemMode.SAFE_MODE.value and self._healthy_cycles >= 2 and self.s.autonomous_demo:
            st.set_mode(SystemMode.AUTO, "")
            self.journal.event("mode_change", mode="AUTO", reason="cycles sains après démarrage")

    def _apply_watchdog(self) -> None:
        wd = read_watchdog_report(self.s.state_dir)
        if not wd:
            return
        self.state.watchdog_heartbeat = wd.get("heartbeat", "")
        if wd.get("safe_mode_request") and self.state.mode == SystemMode.AUTO.value:
            self.state.set_mode(SystemMode.SAFE_MODE, "watchdog: " + "; ".join(wd.get("reasons", [])))
            self.journal.warn("SAFE_MODE demandé par le watchdog", reasons=wd.get("reasons"))

    def _macro_context(self) -> dict:
        rets = {}
        for sym, snap in self.snapshots.items():
            df = snap.frames.get("H1")
            if df is not None and len(df) > 6:
                c = df["close"].iloc[-6:-1]
                rets[root_of(sym)] = 100.0 * (float(c.iloc[-1]) / float(c.iloc[0]) - 1) if float(c.iloc[0]) else 0.0
        label, feats = risk_sentiment(rets)
        return {"risk_sentiment": label, **{k: v for k, v in feats.items() if not isinstance(v, dict)}}

    def _symbol_currencies(self, sym: str) -> list[str]:
        spec = self.broker.symbol_info(sym)
        return [spec.currency_base, spec.currency_profit] if spec else []

    def _scan_and_execute(self, now: datetime, dd, macro: Optional[dict] = None) -> dict:
        st = self.state
        out: dict = {}
        self.review.required_score = self._required_score(now)
        active_statuses = (AgentStatus.LIVE.value,)
        cands, rep = self.router_.route(self.snapshots, active_statuses)
        st.active_agents = sorted({a for lst in rep.agents_activated.values() for a in lst})
        out["routing"] = rep.to_dict()
        # candidats shadow (agents SHADOW/CANDIDATE) — jamais exécutés
        shadow_cands, _ = self.router_.route(self.snapshots, (AgentStatus.SHADOW.value, AgentStatus.CANDIDATE.value))
        self.shadow.open_from_candidates(shadow_cands, now)
        # enrichissement + revue — le contexte macro vient de `cycle()` (`summary["macro"]`) ; `out` ne le
        # contient jamais, l'ancien `out.get("macro")` recalculait donc la même chose à chaque scan.
        macro = macro if macro is not None else self._macro_context()
        reviewed: list[TradeCandidate] = []
        entries_ok, entries_reason = st.entries_allowed()
        llm_batch: list[TradeCandidate] = []
        # heartbeat persisté avant la revue (appels LLM potentiellement longs) pour ne pas être déclaré mort par le watchdog
        self.store.save()
        cands = sorted(cands, key=self._priorite, reverse=True)
        # Pré-filtre déterministe (audit 2026-09-23) : un symbole déjà porté par le bot sera refusé par le
        # gate (`14_max_positions_per_symbol` = 1, `17_existing_position`) quoi que dise la revue. Mesuré sur
        # 4 jours : 246 revues LLM (≈ 1 000 appels) consommées pour des candidats refusés à ce seul titre,
        # au détriment des créneaux des autres candidats. Le gate reste seul juge : ceci n'ouvre rien.
        symboles_portes = {p.symbol for p in st.bot_positions.values()}
        for c in cands:
            spec = self.registry.get(c.agent_id)
            nc = self._news_check(c, spec, now)
            c.news_state = nc.state
            c.macro_alignment = macro.get("risk_sentiment", "UNKNOWN")
            self.cot.annotate(c)
            stats = self.learning.agent_stats(c.agent_id)
            c.historical_stats = {"sample_size": stats.sample_size, "expectancy_r": stats.expectancy_r, "profit_factor": stats.profit_factor, "win_rate": stats.win_rate}
            c.sample_size = stats.sample_size
            snap = self.snapshots[c.symbol]
            c.review["similar_situations"] = self.learning.similar_situations(c.symbol, c.regime.value, c.session, snap.regime.features.get("vol_pct"))
            det = self.review.deterministic(c, dd.setup_score_bonus)
            # Un rejet déterministe (ou verdict de sécurité) n'est jamais soumis au LLM et ne consomme pas
            # un des N créneaux : `AdversarialReview.review` applique la même règle.
            deja_porte = c.symbol in symboles_portes
            llm_worthy = (self.review.llm is not None and entries_ok and det.verdict is not Verdict.REJECT
                          and not det.hard and not deja_porte)
            if llm_worthy and len(llm_batch) < MAX_LLM_REVIEWS_PER_CYCLE:
                llm_batch.append(c)
            else:
                # verdict déterministe seul : aucun appel LLM quand le gate refusera de toute façon (mode/verrous/
                # symbole déjà porté) ou au-delà des N meilleurs candidats
                c.verdict, c.review = det.verdict, det.to_dict()
                if self.review.llm is not None and (llm_worthy or not entries_ok or deja_porte):
                    if not entries_ok:
                        c.review["llm_skipped"] = entries_reason
                    elif deja_porte:
                        c.review["llm_skipped"] = f"position déjà ouverte sur {c.symbol} (1 par symbole)"
                    else:
                        c.review["llm_skipped"] = f"au-delà des {MAX_LLM_REVIEWS_PER_CYCLE} meilleurs candidats"
                        # 2026-09-26 (décision utilisateur) : sans l'avis de l'IA, pas d'entrée. Au-delà des N créneaux,
                        # un APPROVE sur le seul score (ADAUSD 18 h 02, arrêté par le gate) devient WAIT.
                        if c.verdict is Verdict.APPROVE:
                            c.verdict = Verdict.WAIT
                            c.review["verdict"] = Verdict.WAIT.value
            reviewed.append(c)
        if llm_batch:
            # Les N revues LLM sont indépendantes : en parallèle. Mesuré le 2026-09-21 en séquentiel :
            # 4 appels × ~9 s × 3 candidats = cycle de 100 s, gestion des positions figée d'autant (boucle à 15 s).
            self._review_with_deadline(llm_batch, dd.setup_score_bonus)
            # Seul un appel LLM justifie de re-persister l'état : on rafraîchit le heartbeat (figé au début
            # du cycle) pour que le watchdog ne déclare pas l'orchestrateur mort.
            st.orchestrator_heartbeat = self.now_fn().isoformat()
            self.store.save()
        self._annotate_extension(reviewed)
        reviewed.sort(key=self._priorite, reverse=True)
        st.top_setups = [{"symbol": c.symbol, "side": c.side.value, "setup_score": c.setup_score, "agent_id": c.agent_id,
                          "verdict": c.verdict.value if c.verdict else None, "rr": c.rr, "news": c.news_state} for c in reviewed[:10]]
        out["candidates"] = len(reviewed)
        for c in reviewed:
            # nature = verdict + raison d'une revue IA sautée (symbole déjà porté, hors délai, au-delà des N meilleurs)
            nature = f"{c.verdict.value if c.verdict else ''}|{(c.review or {}).get('llm_skipped', '')}"
            self._journal_once("candidate", "cand:" + c.idempotency_key, nature, candidate=c.to_dict())
        # exécution (le gate a le dernier mot)
        entries = 0
        corr = None
        try:
            corr = self.feed.rolling_correlation(list(self.snapshots), "H1", int(self.s.correlation.get("lookback_bars", 200)))
        except Exception:  # noqa: BLE001
            corr = None
        for c in reviewed:
            if entries >= MAX_ENTRIES_PER_CYCLE:
                break
            if c.verdict is not Verdict.APPROVE:
                continue
            gate_res, req = self._gate(c, now, corr)
            refus = sorted(ch.name for ch in gate_res.checks if not ch.ok)
            self._journal_once("gate", "gate:" + c.idempotency_key, "OK" if gate_res.approved else "|".join(refus),
                               force=gate_res.approved, candidate_id=c.id, symbol=c.symbol, approved=gate_res.approved,
                               key=c.idempotency_key, agent_id=c.agent_id, side=c.side.value, entry=c.entry, sl=c.sl,
                               tp=(c.tp_plan[-1] if c.tp_plan else None), atr=getattr(c, "atr", None),
                               reason=gate_res.reason, checks=[ch.__dict__ for ch in gate_res.checks])
            if not gate_res.approved or req is None:
                continue
            # Le risque transmis à l'exécuteur est celui du gate (dimensionné sur le tick, `GateResult`) :
            # un second `risk.size()` sur `c.entry` — le prix du scan — donnait un `risk_money` différent
            # du volume réellement envoyé (`req.volume`), et c'est ce chiffre faux qui finissait dans le
            # plan de position, le journal et l'agrégation prop.
            if gate_res.volume_capped:
                # Le broker plafonne le volume : la position risque moins que la cible. Ce n'est
                # pas une erreur (l'ordre part quand même) mais ça doit être visible, sinon on
                # découvre sur un relevé qu'un trade a risqué 5 $ au lieu de 625 $.
                self.journal.warn("volume rabote par le broker", symbol=c.symbol, agent_id=c.agent_id,
                                  volume_voulu=round(gate_res.volume_wanted, 4), volume_envoye=req.volume,
                                  risque_vise_percent=round(dd.risk_percent, 4),
                                  risque_effectif_percent=round(gate_res.risk_percent, 5),
                                  risque_money=round(gate_res.risk_money, 2))
            outcome = self.executor.execute(c, gate_res, req, gate_res.risk_money, gate_res.risk_percent)
            self.journal.event("execution", candidate_id=c.id, executed=outcome.executed, message=outcome.message,
                               ticket=outcome.position.ticket if outcome.position else None)
            if outcome.executed and outcome.position:
                entries += 1
                cd = c.to_dict()
                # version du code, coût d'entrée et glissement mesurés (plan pro du 2026-09-25, points 1 et 5)
                fill = float(getattr(outcome.position, "price_open", 0.0) or 0.0)
                dist = abs(float(gate_res.sizing_price or c.entry) - float(c.sl)) or 0.0
                glisse = ((fill - float(gate_res.sizing_price)) * c.side.sign) if (fill and gate_res.sizing_price) else None
                cd.update({"version": code_version(), "cost_ratio": gate_res.cost_ratio,
                           "slippage_r": round(glisse / dist, 4) if (glisse is not None and dist > 0) else None})
                self.candidates_cache[str(outcome.position.ticket)] = cd
                snap = self.snapshots[c.symbol]
                sid = self.learning.store_snapshot(c.symbol, c.regime.value, c.session, {**snap.regime.features, "setup_score": c.setup_score},
                                                   macro=c.macro_alignment, news_state=c.news_state)
                self.candidates_cache[str(outcome.position.ticket)]["snapshot_id"] = sid
                plan = st.bot_positions.get(str(outcome.position.ticket))
                if plan is not None:
                    plan.candidate = self.candidates_cache[str(outcome.position.ticket)]
        out["entries"] = entries
        return out

    def _gate(self, c: TradeCandidate, now: datetime, corr):
        st = self.state
        snap = self.snapshots.get(c.symbol)
        spec = self.broker.symbol_info(c.symbol)
        tick = self.broker.tick(c.symbol)
        wd = read_watchdog_report(self.s.state_dir)
        if not wd:
            # aucun rapport : le watchdog n'est optionnel qu'avec le broker mock
            wd_alive = self.broker.name == "mock"
        else:
            # fichier écrit par un autre processus : un heartbeat absent/non ISO/naïf vaut « watchdog absent », jamais une exception
            try:
                hb = datetime.fromisoformat(str(wd["heartbeat"]))
                hb = hb if hb.tzinfo else hb.replace(tzinfo=timezone.utc)
                wd_alive = (now - hb).total_seconds() <= float(self.s.system.get("heartbeat_max_age_sec", 45))
            except (ValueError, TypeError, KeyError):
                wd_alive = False
        agent = self.registry.get(c.agent_id)
        nc = self._news_check(c, agent, now)
        bot_pos = list(st.bot_positions.values())
        ex = self.s.execution
        max_sp = max_spread_points_for(spec.asset_class if spec else "", ex.get("max_spread_points", {}))
        ctx = GateContext(candidate=c, state=st, account=self.broker.account_info(), spec=spec, tick=tick,
                          atr=snap.atr_h1 if snap else 0.0, data_quality=snap.data_quality if snap else "NO_DATA",
                          market_open=forex_market_open(now) if (spec and spec.asset_class == "forex") else True,
                          news_check=nc, correlations=corr, now=now, watchdog_alive=wd_alive,
                          open_positions_symbol=sum(1 for p in bot_pos if p.symbol == c.symbol), open_positions_total=len(bot_pos),
                          max_spread_points=int(max_sp), max_spread_atr_ratio=float(ex.get("max_spread_atr_ratio", 0.15)),
                          max_spread_sl_ratio=float(ex.get("max_spread_sl_ratio", 0.35)),
                          rollover_block=self._rollover_block(spec, now),
                          disabled_checks=tuple(str(x) for x in (ex.get("gate_checks_disabled") or [])),
                          min_rr_required=float(ex.get("min_rr_required", 1.5)), required_setup_score=self._required_score(now),
                          min_sl_atr_ratio=float(ex.get("min_sl_atr_ratio", 0.25)), max_sl_atr_ratio=float(ex.get("max_sl_atr_ratio", 4.0)),
                          deviation_points=int(ex.get("slippage_deviation_points", 20)), magic=self.magic,
                          comment_prefix=str(self.s.system.get("order_comment_prefix", "TLAB")))
        return self.gate.evaluate(ctx)

    def _invalidation_timeframe(self, agent_id: str) -> str:
        """Timeframe sur lequel juger l'invalidation d'une position : le tf d'entrée de l'agent.
        Position adoptée (`ADOPTED`) ou agent inconnu → M15 (comportement historique)."""
        spec = self.registry.get(agent_id) if agent_id else None
        tfs = getattr(spec, "timeframes", None) or {}
        return str(tfs.get("entry") or "M15")

    def _invalidation_hit(self, plan, side: Side, snap: MarketSnapshot, df) -> tuple[bool, str]:
        """La règle d'invalidation promise par l'agent est-elle déclenchée sur la dernière barre CLÔTURÉE ?

        Jusqu'au 2026-09-21 seules les règles contenant « EMA50 » étaient lues : les 24 autres formulations
        (« clôture M15 sous 1.6095 », « retour dans le range (a-b) »…) n'étaient jamais appliquées et la position
        courait jusqu'au SL plein. `parse_invalidation` extrait un niveau ou une zone mesurable ; si le texte cite
        un timeframe disponible dans le snapshot on juge sur celui-là, sinon sur le tf d'entrée de l'agent (`df`).
        Sans niveau exploitable, repli historique : clôture au-delà de l'EMA50 si le texte le mentionne.
        """
        # retour au 19/09 (2026-09-25, décision utilisateur) : `invalidation_levels: false` ne garde que la règle
        # EMA50 d'origine ; les niveaux lus dans le texte des agents ne ferment plus les positions
        ex = getattr(getattr(self, "s", None), "execution", None) or {}
        lire_niveaux = bool(ex.get("invalidation_levels", True))
        rule = parse_invalidation(plan.invalidation or "", side, plan.entry) if lire_niveaux else None
        if rule is not None:
            frame = snap.frames.get(rule.timeframe) if rule.timeframe else None
            if frame is None or len(frame) < 3:
                frame = df
            lc = last_closed(frame)
            close = float(lc["close"]) if "close" in lc else float("nan")
            return rule.triggered(side, close), rule.describe()
        lc = last_closed(df)
        if "EMA50" in (plan.invalidation or "") and "ema50" in lc and lc["ema50"] == lc["ema50"]:
            hit = (lc["close"] < lc["ema50"]) if side is Side.BUY else (lc["close"] > lc["ema50"])
            return bool(hit), "clôture au-delà de l'EMA50"
        return False, ""

    def _manage_positions(self) -> None:
        st = self.state
        for ticket, plan in list(st.bot_positions.items()):
            pos = self.broker.position(int(ticket))
            spec = self.broker.symbol_info(plan.symbol)
            if pos is None or spec is None:
                continue
            # Cohérence 25 % (FOXX, décision utilisateur 2026-09-23) : une idée dont le gain (partiels réalisés +
            # flottant) atteint la part autorisée est fermée — au-delà, la règle n'est plus satisfaite au paiement.
            cap = self.prop.consistency_gain_cap(st, plan.symbol, plan.side, self.now_fn())
            if cap is not None:
                idea = st.active_trade_idea(plan.symbol, plan.side, self.now_fn(), self.prop.profile.trade_idea_aggregation_minutes)
                gain = (idea.realized_pnl if idea else 0.0) + float(pos.profit)
                if gain >= cap:
                    ok = self.pm.close(int(ticket), "coherence-25")
                    self.journal.event("consistency_cap_close", ticket=int(ticket), symbol=plan.symbol, gain=round(gain, 2),
                                       cap=round(cap, 2), total_profit=round(self.prop.consistency_total_profit(st), 2), ok=bool(ok))
                    if ok:
                        continue
            # rollover (2026-09-24) : une paire exotique n'est pas portée à travers le reset 17:00 New York
            cfg_ro = self._rollover_cfg()
            if cfg_ro and self._is_exotic(spec):
                reste = (self.trading_day.next_reset(self.now_fn()) - self.now_fn()).total_seconds() / 60.0
                if 0 <= reste <= float(cfg_ro.get("close_before_min", 10)):
                    ok = self.pm.close(int(ticket), "rollover exotique")
                    self.journal.event("rollover_close", ticket=int(ticket), symbol=plan.symbol, minutes_avant_reset=round(reste, 1),
                                       profit=round(float(pos.profit), 2), ok=bool(ok))
                    if ok:
                        continue
            snap = self.snapshots.get(plan.symbol)
            ctx = MarketContext(atr=snap.atr_h1 if snap else abs(plan.entry - plan.initial_sl))
            if snap is not None:
                # L'invalidation des screeners s'exprime sur le tf d'entrée de l'agent (sauf tf explicite dans le
                # texte, cf. `_invalidation_hit`) : on lit donc ce tf (M5/M15/H1 selon la spec), pas M15 en dur. Un agent H1 était
                # invalidé sur une clôture M15 qu'il n'avait jamais promise ; un agent M5 ne l'était jamais à temps.
                df = snap.frames.get(self._invalidation_timeframe(plan.agent_id))
                if df is None:
                    df = snap.frames.get("M15")
                if df is None:
                    df = snap.frames.get("H1")
                if df is not None and len(df) > 20:
                    sh, sl_ = swing_points(df.iloc[:-1])
                    ctx.last_swing_high = sh[-1][1] if sh else None
                    ctx.last_swing_low = sl_[-1][1] if sl_ else None
                    side = Side(plan.side)
                    ctx.structure_ok = (ctx.last_swing_low is not None and ctx.last_swing_low > plan.entry) if side is Side.BUY \
                        else (ctx.last_swing_high is not None and ctx.last_swing_high < plan.entry)
                    # FOXX (FAQ 2026-09-23) : « maintenir les positions ouvertes plus d'une minute » — une sortie
                    # anticipée n'est jamais décidée dans la première minute (le SL broker, lui, reste actif)
                    age_ok = self._position_age_sec(plan) >= MIN_HOLD_SEC
                    if plan.max_r < 0.5 and age_ok:
                        ctx.invalidated, ctx.invalidation_reason = self._invalidation_hit(plan, side, snap, df)
                if snap.regime.regime.value == "NEWS_SHOCK":
                    ctx.invalidated = False  # on gère sans supprimer le SL, pas de sortie forcée
            actions = self.pm.manage(plan, pos, spec, ctx)
            if actions:
                self.journal.event("position_managed", ticket=int(ticket), actions=actions)

    def _maybe_daily_report(self, now: datetime) -> None:
        """Au passage de 17 h New York, rapport de la journée qui vient de se clore. Jamais au premier démarrage :
        la journée courante est seulement mémorisée."""
        key = self.trading_day.day_key(now)
        if not self.state.last_daily_report:
            self.state.last_daily_report = key
            return
        if self.state.last_daily_report == key:
            return
        veille = self.state.last_daily_report
        self.state.last_daily_report = key
        try:
            texte = self._daily_report_text(now, veille)
        except Exception as e:  # noqa: BLE001 - le rapport ne doit jamais casser la boucle
            self.journal.warn("rapport quotidien impossible", error=f"{type(e).__name__}: {e}")
            return
        self.journal.event("report_day", day=veille, text=texte)

    def _daily_report_text(self, now: datetime, day_label: str) -> str:
        import json as _json
        from ..learning.quality import daily_report_text

        fin = self.trading_day.reset_at(now)
        debut = fin - timedelta(days=1)
        comptes = []
        for f in sorted(self.s.state_dir.glob("copy_status_*.json")):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            fermes = [t for t in (d.get("closed") or []) if debut.isoformat()[:19] <= str(t.get("closed_at", ""))[:19] < fin.isoformat()[:19]]
            comptes.append({"nom": str(d.get("name") or f.stem), "trades": len(fermes),
                            "pnl": sum(float(t.get("pnl") or 0.0) for t in fermes)})
        raisons: list[str] = []
        for jour in sorted({debut.date(), fin.date()}):
            fichier = self.journal._file(datetime.combine(jour, datetime.min.time(), tzinfo=timezone.utc))
            if not fichier.exists():
                continue
            with fichier.open(encoding="utf-8") as fh:
                for ligne in fh:
                    if '"watchdog_alert"' not in ligne:
                        continue
                    try:
                        ev = _json.loads(ligne)
                    except ValueError:
                        continue
                    if debut.isoformat() <= str(ev.get("ts_utc", "")) < fin.isoformat():
                        for r in ev.get("reasons") or []:
                            nature = self.__class__._nature(r)
                            if nature not in [self.__class__._nature(x) for x in raisons]:
                                raisons.append(str(r))
        return daily_report_text(self.s.data_dir / "learning.db", debut, fin, comptes, raisons,
                                 self.s.system.get("gel_reglages"), day_label)

    @staticmethod
    def _nature(raison: str) -> str:
        import re
        return re.sub(r"[-+]?\d+(?:[.,]\d+)?", "#", str(raison))

    def _maybe_weekly_report(self, now: datetime) -> None:
        from zoneinfo import ZoneInfo

        try:
            loc = now.astimezone(ZoneInfo("Europe/Paris"))
        except Exception:  # noqa: BLE001 - base de fuseaux absente : UTC
            loc = now
        iso = f"{loc.isocalendar()[0]}-S{loc.isocalendar()[1]:02d}"
        if loc.weekday() != 0 or loc.hour < 8 or self.state.last_weekly_report == iso:
            return
        try:
            texte = self._weekly_report_text(now)
        except Exception as e:  # noqa: BLE001 - le rapport ne doit jamais casser la boucle
            self.journal.warn("rapport hebdomadaire impossible", error=f"{type(e).__name__}: {e}")
            return
        self.state.last_weekly_report = iso
        self.journal.event("report_week", week=iso, text=texte)

    def _weekly_report_text(self, now: datetime) -> str:
        """Résumé des 7 derniers jours : P&L du maître, meilleurs / pires agents, filtre d'extension (mesuré),
        cycle de paiement, comptes suiveurs."""
        import json as _json
        import sqlite3 as _sql

        depuis = (now - timedelta(days=7)).isoformat()
        con = _sql.connect(f"file:{self.s.data_dir / 'learning.db'}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute("SELECT agent_id, pnl, result_r, review FROM trades WHERE mode='live' AND closed_at >= ?",
                               (depuis,)).fetchall()
        finally:
            con.close()
        n = len(rows); pnl = sum(float(r[1] or 0) for r in rows); g = sum(1 for r in rows if float(r[1] or 0) > 0)
        par_agent: dict = {}
        ext_oui, ext_non = [], []
        for aid, p, rr, rev in rows:
            par_agent.setdefault(aid, [0, 0.0]); par_agent[aid][0] += 1; par_agent[aid][1] += float(p or 0)
            try:
                rv = _json.loads(rev) if rev else {}
            except (TypeError, ValueError):
                rv = {}
            if "extension_filter_would_reject" in rv:
                (ext_oui if rv["extension_filter_would_reject"] else ext_non).append(float(rr or 0))
        tri = sorted(par_agent.items(), key=lambda kv: kv[1][1], reverse=True)
        lignes = [f"<b>Semaine</b> : {n} trades, {g} gagnants, P&L {pnl:+,.0f} $".replace(",", " ")]
        if tri:
            lignes.append("🏅 " + ", ".join(f"{a} {v[1]:+,.0f} $ ({v[0]})".replace(",", " ") for a, v in tri[:3]))
            lignes.append("⚠️ " + ", ".join(f"{a} {v[1]:+,.0f} $ ({v[0]})".replace(",", " ") for a, v in tri[-3:][::-1] if v[1] < 0) or "aucun agent en perte")
        if ext_oui or ext_non:
            moy = lambda xs: (sum(xs) / len(xs)) if xs else 0.0  # noqa: E731
            lignes.append(f"🧪 Filtre d'extension (mesuré) : aurait refusé {len(ext_oui)} trades ({moy(ext_oui):+.2f} R en moyenne), "
                          f"gardé {len(ext_non)} ({moy(ext_non):+.2f} R)")
        try:
            pc = self.prop.payout_cycle_status(self.state, now)
            lignes.append(f"🏦 Cycle FOXX : jour {pc['trading_days_done']}/{pc['trading_days_required']}, profit {pc['cycle_profit']:+,.0f} $, "
                          f"meilleure idée {pc['consistency_share_percent']:.0f} %".replace(",", " "))
        except Exception:  # noqa: BLE001
            pass
        for f in sorted(self.s.state_dir.glob("copy_status_*.json")):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
                lignes.append(f"👤 {d.get('name')} : equity {float(d.get('equity') or 0):,.0f} $, {len(d.get('positions') or [])} positions".replace(",", " "))
            except (OSError, ValueError, TypeError):
                continue
        return "\n".join(lignes)

    def _annotate_extension(self, cands) -> None:
        """Point 1 (2026-09-24) : distance entrée / EMA20 du tf d'entrée, en ATR, et verdict du filtre d'extension
        EN MESURE (aucun refus). Stocké dans la revue du candidat → plan de position → learning.db."""
        cfg = dict(self.s.raw.get("entry_extension_filter", {}) or {})
        seuil = float(cfg.get("max_atr", 1.5))
        for c in cands:
            try:
                snap = self.snapshots.get(c.symbol)
                tf = (c.timeframes or ["M15"])[0]
                df = snap.frames.get(tf) if snap else None
                if df is None or len(df) < 3:
                    continue
                row = df.iloc[-2]
                atr = float(row["atr14"])
                if atr > 0:
                    ext = abs(float(c.entry) - float(row["ema20"])) / atr
                    c.review["extension_atr"] = round(ext, 2)
                    c.review["extension_filter_would_reject"] = bool(ext > seuil)
            except (KeyError, IndexError, TypeError, ValueError):
                continue

    def _is_exotic(self, spec) -> bool:
        exo = {str(x).upper() for x in self._rollover_cfg().get("exotic_currencies", [])}
        return bool(spec) and bool({str(spec.currency_base).upper(), str(spec.currency_profit).upper()} & exo)

    def _rollover_cfg(self) -> dict:
        return dict(self.s.raw.get("rollover", {}) or {})

    def _rollover_block(self, spec, now: datetime) -> str:
        """Raison du refus si la paire est exotique et `now` dans la fenêtre du reset 17:00 New York, sinon ""."""
        cfg = self._rollover_cfg()
        if not cfg or not self._is_exotic(spec):
            return ""
        nxt = self.trading_day.next_reset(now)
        prev = self.trading_day.reset_at(now)
        avant = (nxt - now).total_seconds() / 60.0
        apres = (now - prev).total_seconds() / 60.0
        if avant <= float(cfg.get("block_entries_before_min", 30)):
            return f"paire exotique : rollover dans {avant:.0f} min (spread qui s'élargit)"
        if apres <= float(cfg.get("block_entries_after_min", 30)):
            return f"paire exotique : rollover il y a {apres:.0f} min (spread encore large)"
        return ""

    def _news_events(self, ccys: list[str], now: datetime) -> list[dict]:
        """Annonces HIGH des 6 dernières heures pour ces devises (stratégies d'annonces, famille K)."""
        from ..news.hub import is_central_bank_event

        out = []
        try:
            for ev in self.news.recent_events(now, ccys, 360, "HIGH"):
                out.append({"ts": ev.timestamp.isoformat(), "minutes_ago": round((now - ev.timestamp).total_seconds() / 60.0, 1),
                            "title": ev.title, "currency": ev.currency, "central_bank": is_central_bank_event(ev.title),
                            "surprise": ev.surprise})
        except Exception:  # noqa: BLE001 - calendrier indisponible : aucune annonce, jamais inventée
            return []
        return out

    def _position_age_sec(self, plan) -> float:
        try:
            opened = datetime.fromisoformat(str(plan.opened_at))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            return max(0.0, (self.now_fn() - opened).total_seconds())
        except (TypeError, ValueError):
            return float("inf")            # date illisible : on ne bloque pas une sortie de sécurité

    STATEMENT_EVERY_SEC = 60.0

    def _export_statement(self, acc) -> None:
        """Relevé réel du compte maître (historique MT5 : trades, commissions, swaps, dépôts) → state/statement_master.json,
        source des statistiques du maître dans le dashboard (2026-09-24). Toutes les 60 s, jamais bloquant."""
        import json as _json
        import os as _os

        from ..learning.consistency import account_statement

        last = getattr(self, "_statement_at", 0.0)
        if time.monotonic() - last < self.STATEMENT_EVERY_SEC:
            return
        self._statement_at = time.monotonic()
        try:
            now = self.now_fn()
            deals = self.broker.history_deals(now - timedelta(days=400), now + timedelta(hours=1))
            payload = {"ts_utc": now.isoformat(), "login": getattr(acc, "login", None), "server": getattr(acc, "server", None),
                       "currency": getattr(acc, "currency", ""), "balance_broker": float(acc.balance),
                       **account_statement(deals, equity=float(acc.equity),
                                           since=str(self.s.system.get("master_account_since") or "") or None,
                                           balance=float(acc.balance))}
            path = self.s.state_dir / "statement_master.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            _os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 - l'export ne doit jamais tuer la boucle
            self.journal.warn("relevé du compte maître indisponible", error=f"{type(e).__name__}: {e}")

    def _export_master_positions(self, acc) -> None:
        """Export pour le copy trading (2026-09-22, demande utilisateur) : photographie des positions du bot
        (volume COURANT du broker, donc partiels inclus) que les processus copieurs répliquent sur les
        comptes suiveurs. Écriture atomique ; une erreur n'interrompt jamais le cycle."""
        try:
            import json as _json
            import os as _os

            positions = []
            for p in self.broker.positions(magic=self.s.magic):
                sp = self.broker.symbol_info(p.symbol)
                # valeur d'un mouvement de 1,0 du prix pour 1 lot (devise du compte) : les suiveurs dimensionnent sur
                # cette valeur et non sur le nombre de lots — 1 lot d'argent = 1 000 oz chez IC, 5 000 oz chez Admirals
                # (2026-09-24 : copie à 5× le risque du maître, −2 781 $ contre −6 $)
                vpp = (float(sp.tick_value) / float(sp.tick_size)) if (sp and sp.tick_size) else None
                positions.append({"ticket": p.ticket, "symbol": p.symbol, "side": p.side.value, "volume": p.volume,
                                  "sl": p.sl, "tp": p.tp, "price_open": p.price_open, "value_per_price": vpp,
                                  "price_current": float(p.price_current or 0.0) or None,
                                  "contract_size": float(sp.contract_size) if sp else None})
            payload = {"ts_utc": self.now_fn().isoformat(), "equity": float(acc.equity),
                       "magic": self.s.magic, "positions": positions}
            path = self.s.state_dir / "master_positions.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(payload), encoding="utf-8")
            # Windows refuse le remplacement tant qu'un copieur lit le fichier (PermissionError constaté le
            # 2026-09-23 à 03:09). La collision dure quelques millisecondes : on réessaie brièvement plutôt
            # que de sauter l'export, sans jamais bloquer la boucle (3 essais, 0,15 s au pire).
            for essai in range(3):
                try:
                    _os.replace(tmp, path)
                    break
                except PermissionError:
                    if essai == 2:
                        raise
                    time.sleep(0.05)
        except Exception as e:  # noqa: BLE001 - l'export ne doit jamais tuer la boucle
            self.journal.warn("export copy trading échoué", error=type(e).__name__)

    def _on_position_closed(self, plan, now: datetime) -> None:
        deals = self.broker.history_deals(now - timedelta(days=30), now + timedelta(minutes=5))
        # après un redémarrage le cache mémoire est vide : le candidat persisté avec le plan prend le relais
        cand = self.candidates_cache.pop(str(plan.ticket), None) or (getattr(plan, "candidate", None) or None)
        try:
            point = float(getattr(self.broker.symbol_info(plan.symbol), "point", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 - spec indisponible : la revue retombe sur le seuil en points
            point = 0.0
        rec = build_trade_record(plan, deals, cand, now.isoformat(), session=(cand or {}).get("session", ""),
                                 point=point)
        tid = self.learning.record_trade(rec)
        if cand and cand.get("snapshot_id"):
            self.learning.set_snapshot_outcome(cand["snapshot_id"], rec.result_r, tid)
        st = self.state
        st.daily.trades_closed += 1
        if rec.pnl > 0:
            st.daily.wins += 1
            st.consecutive_losses = 0
        else:
            st.daily.losses += 1
            st.consecutive_losses += 1
        st.daily.realized_pnl += rec.pnl
        # P&L rattaché à l'idée de trade : sert la règle de cohérence (part du profit total par idée)
        st.close_trade_idea_position(plan.ticket, rec.pnl, now)
        review = self.post_trade.analyze(rec)
        self.journal.event("post_trade_review", ticket=plan.ticket, symbol=plan.symbol, side=plan.side, agent_id=plan.agent_id,
                           result_r=rec.result_r, pnl=rec.pnl, review=review.to_dict())
        if review.challenger_needed and self.research is not None:
            spec = self.registry.get(plan.agent_id)
            if spec and not any(a.parent_id == spec.agent_id and a.status == AgentStatus.RESEARCH.value for a in self.registry.agents.values()):
                self.research.generate_challengers(spec, 1)

    def _start_research_thread(self) -> None:
        """La recherche (backtests, walk-forward…) tourne hors du chemin critique live."""
        if self._research_thread and self._research_thread.is_alive():
            return

        def _run():
            with self._research_lock:
                try:
                    self._research_cycle()
                except Exception as e:  # noqa: BLE001
                    self.journal.warn("cycle de recherche échoué", error=f"{type(e).__name__}: {e}")
        self._research_thread = threading.Thread(target=_run, name="research", daemon=True)
        self._research_thread.start()

    def _review_with_deadline(self, batch: list, score_bonus: float, deadline: float = None) -> None:
        """Revues IA en parallèle, bornées dans le temps. Chaque revue travaille sur une COPIE du candidat : une
        revue hors délai ne peut pas modifier le verdict après coup (le thread finit en arrière-plan, sans effet).
        Hors délai ou en erreur → verdict déterministe, noté dans la revue et journalisé."""
        import copy
        from concurrent.futures import wait

        deadline = LLM_REVIEW_DEADLINE_SEC if deadline is None else deadline
        ex = ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="llm-review")
        futs = {}
        for c in batch:
            cc = copy.deepcopy(c)
            futs[ex.submit(self.review.review, cc, score_bonus)] = (c, cc)
        done, _ = wait(futs, timeout=deadline)
        ex.shutdown(wait=False, cancel_futures=True)
        for f, (c, cc) in futs.items():
            if f in done and f.exception() is None:
                c.verdict, c.review = cc.verdict, cc.review
                continue
            det = self.review.deterministic(c, score_bonus)
            # 2026-09-26 : le repli déterministe APPROUVAIT (score 63 ≥ 55) un BTCUSD que l'IA venait de mettre en
            # attente 5 min plus tôt (stop jugé fragile) ; le trade a perdu 617 $. Une revue IA qui n'aboutit pas ne
            # valide jamais une entrée : APPROVE devient WAIT, le candidat est revu au cycle suivant.
            verdict = Verdict.WAIT if det.verdict is Verdict.APPROVE else det.verdict
            c.verdict, c.review = verdict, det.to_dict()
            c.review["verdict"] = verdict.value
            raison = f"revue IA hors délai ({deadline:.0f} s)" if f not in done else f"revue IA en erreur ({type(f.exception()).__name__})"
            c.review["llm_skipped"] = raison
            self.journal.warn("revue IA remplacée par la revue déterministe", symbol=c.symbol, agent_id=c.agent_id, raison=raison)

    def _journal_once(self, kind: str, dedup_key: str, signature: str, force: bool = False, **data) -> None:
        """Journalise un candidat / une décision du gate une fois, puis seulement si sa nature change.

        2026-09-25 (plan pro, point 7) : le même candidat (même agent, symbole, sens et barre) était réécrit à chaque
        scan rapide avec la même décision — 12 000 candidats et 4 500 décisions par jour, 35 Mo de journal."""
        vus = self.__dict__.setdefault("_journal_sig", {})
        if not force and vus.get(dedup_key) == signature:
            return
        if len(vus) > 20000:
            vus.clear()
        vus[dedup_key] = signature
        self.journal.event(kind, **data)

    def _required_score(self, now: datetime) -> float:
        """Score minimal du setup. Une baisse TEMPORAIRE (`execution.required_setup_score_temporaire`, avec une date de
        fin) s'applique jusqu'à son échéance puis s'éteint d'elle-même (2026-09-26, demande utilisateur : « juste
        pour aujourd'hui »). Une échéance illisible ou dépassée → valeur normale."""
        ex = self.s.execution
        base = float(ex.get("required_setup_score", 65))
        tmp = ex.get("required_setup_score_temporaire") or {}
        try:
            if tmp and now < datetime.fromisoformat(str(tmp["jusqu_a"])):
                return float(tmp["valeur"])
        except (KeyError, TypeError, ValueError):
            pass
        return base

    #: 2026-09-26 : l'IA refusait des setups crypto du samedi pour « session Londres/New York un samedi : données
    #: incohérentes ». La crypto cote en continu ; le nom de session n'est qu'une plage horaire UTC le week-end.
    NOTE_CRYPTO_WEEKEND = ("Crypto : marché ouvert 24 h/24, 7 j/7. Le week-end, le nom de session (ASIA, LONDON, "
                           "NEWYORK, OVERLAP_LDN_NY) désigne seulement la plage horaire UTC, pas l'ouverture d'une place "
                           "boursière : ce n'est pas une incohérence de données. La liquidité du week-end est plus faible.")

    def _market_note(self, c) -> Optional[str]:
        """Contexte ajouté au dossier de l'IA : crypto le samedi ou le dimanche (UTC)."""
        created = getattr(c, "created_at", None)
        if created is None or created.weekday() < 5:
            return None
        spec = self.broker.symbol_info(c.symbol)
        return self.NOTE_CRYPTO_WEEKEND if spec is not None and spec.asset_class == "crypto" else None

    def _news_check(self, c, spec, now: datetime):
        """Contrôle des annonces pour un candidat. Les agents qui TRADENT les annonces (`news_trader`, décision
        utilisateur du 2026-09-26, option news FOXX) passent la fenêtre de blocage et le choc de news ; jamais un
        calendrier indisponible (DEGRADED reste bloquant pour eux). Tous les autres agents gardent le blocage."""
        from ..news.hub import NewsCheck

        nc = self.news.check(self._symbol_currencies(c.symbol), now, news_sensitive_strategy=bool(spec and spec.news_sensitive))
        if spec is not None and getattr(spec, "news_trader", False) and (nc.state.startswith("BLOCKED") or nc.state == "SHOCK"):
            return NewsCheck(ok=True, state="NEWS_TRADING", reason=f"annonce tradée par un agent spécialisé ({nc.state} : {nc.reason})",
                             events=nc.events)
        return nc

    def _priorite(self, c) -> tuple:
        """Clé de tri des candidats : agents mis en avant par l'utilisateur d'abord (2026-09-25), puis score de setup."""
        prio = {str(x) for x in ((getattr(getattr(self, "s", None), "learning", None) or {}).get("agents_prioritaires") or [])}
        return (c.agent_id in prio, c.setup_score)

    def _research_queue(self, now: datetime) -> list:
        """Agents à faire avancer dans le pipeline, les plus anciennement essayés d'abord.

        Jusqu'au 2026-09-25 la boucle prenait les 2 PREMIERS agents non-LIVE du registre : K01 et K02, qui échouaient au
        backtest à chaque passage, occupaient les deux places pour toujours et aucun autre agent n'avançait. Un agent
        dont l'étape suivante vient d'échouer attend `research_retry_hours` avant un nouvel essai."""
        cfg = self.s.learning or {}
        retry = timedelta(hours=float(cfg.get("research_retry_hours", 24)))
        statuts = (AgentStatus.RESEARCH.value, AgentStatus.BACKTEST.value, AgentStatus.SHADOW.value, AgentStatus.CANDIDATE.value)
        file = []
        for a in list(self.registry.agents.values()):
            if not a.generates_trades or a.status not in statuts:
                continue
            rec = self.research.record(a.agent_id)
            nxt = rec.next_stage()
            if nxt is None:
                continue
            last = (rec.stages.get(nxt.value) or {}).get("ts") or ""
            if last:
                try:
                    if now - datetime.fromisoformat(last) < retry:
                        continue
                except (ValueError, TypeError):
                    pass
            file.append((last, a.agent_id))
        file.sort()
        return [aid for _, aid in file[:int(cfg.get("research_agents_per_cycle", 2))]]

    def _research_cycle(self) -> None:
        changes = self.degradation.run()
        if changes:
            self.journal.event("degradation", changes=changes)
        if self.research is None:
            return
        # faire avancer les agents non-LIVE dans le pipeline, à tour de rôle (voir `_research_queue`)
        for aid in self._research_queue(self.now_fn()):
            try:
                self.research.advance(aid)
            except Exception as e:  # noqa: BLE001
                self.journal.warn("recherche : étape échouée", agent_id=aid, error=f"{type(e).__name__}: {e}")
        # nouveaux challengers pour les champions dégradés
        for a in self.registry.by_status(AgentStatus.DEGRADED):
            if not any(x.parent_id == a.agent_id for x in self.registry.agents.values()):
                self.research.generate_challengers(a, 1)
        # auteur de stratégies (LLM si disponible, sinon déterministe) : au plus 1 parent DEGRADED (ou LIVE en dérive)
        # par cycle, seulement s'il n'a aucun challenger RESEARCH ; les variantes naissent RESEARCH et passent par le pipeline
        try:
            parents = self.registry.by_status(AgentStatus.DEGRADED)
            parents += [a for a in self.registry.by_status(AgentStatus.LIVE) if a.generates_trades
                        and self.learning.agent_stats(a.agent_id).degradation_score >= 0.5]
            for a in parents:
                if not a.generates_trades or any(x.parent_id == a.agent_id and x.status == AgentStatus.RESEARCH.value
                                                 for x in list(self.registry.agents.values())):
                    continue
                created = StrategyAuthor(self.registry, self.learning, self.llm, self.s.learning, self.journal).propose(
                    a, {"status": a.status, "trigger": "research_cycle"})
                self.journal.event("strategy_author_cycle", parent_id=a.agent_id, created=[c.agent_id for c in created])
                break
        except Exception as e:  # noqa: BLE001
            self.journal.warn("strategy_author : proposition échouée", error=f"{type(e).__name__}: {e}")

    def _export_learning(self) -> None:
        try:
            self.learning.export_stats_json(self.s.data_dir / "agent_stats.json")
            import json
            rows = self.learning.leaderboard()
            for row in rows:
                spec = self.registry.get(row["agent_id"])
                row["status"] = spec.status if spec else "UNKNOWN"
            (self.s.reports_dir / "leaderboard.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            self.journal.warn("export learning échoué", error=str(e))

    # ------------------------------------------------------------------ commandes
    def _process_commands(self) -> None:
        for cmd in self.store.pop_commands():
            try:
                self.handle_command(str(cmd.get("command", "")), cmd.get("args", {}) or {}, cmd.get("source", "?"))
            except Exception as e:  # noqa: BLE001 - une commande malformée ne doit ni tuer le cycle ni perdre les suivantes (PANIC…)
                self.journal.error("commande en échec", command=cmd, error=f"{type(e).__name__}: {e}")

    def _maybe_payout(self, now: datetime) -> None:
        """Cycle de paiement automatique (règles FOXX, `docs/prop/foxx_funded_regles.md`).

        Assez de jours de trading dans le cycle et profit > 0 et cohérence du cycle ≤ 25 % → fenêtre de
        paiement : entrées verrouillées (PAYOUT_WINDOW), positions du bot fermées, puis retrait de tout le
        profit (plafond 15 % du solde initial). DEMO traité comme un compte financé : le retrait est simulé
        (`simulated_withdrawal`, le solde vu par le labo baisse d'autant). Compte réel : `payout_ready` et
        attente de la commande PAYOUT_DONE <montant> (la demande de retrait est une démarche humaine).
        Cohérence non respectée ou profit nul : aucune fenêtre, on continue à trader (règle FOXX).
        """
        st = self.state
        try:
            status = self.prop.payout_cycle_status(st, now)
        except Exception as e:  # noqa: BLE001 - le cycle de paiement ne doit jamais casser la boucle
            self.journal.warn("statut du cycle de paiement indisponible", error=f"{type(e).__name__}: {e}")
            return
        if not status["auto_cycle"]:
            return
        if not status["eligible"]:
            if st.payout_window_since and st.payout_window_since != "REAL_PENDING":
                # la fenêtre était ouverte mais une condition est retombée (ex. cohérence) : on rend la main
                st.payout_window_since = ""
                st.unlock_entries("PAYOUT_WINDOW")
                self.journal.event("payout_window_closed", reasons=status["blocking_reasons"])
            return
        if not st.payout_window_since:
            st.payout_window_since = now.isoformat()
            st.lock_entries("PAYOUT_WINDOW")
            closed = self.pm.close_all_bot("payout") if st.bot_positions else []
            self.journal.event("payout_window", level="WARNING", closed=closed, **{k: v for k, v in status.items() if k != "last_payout"})
            return
        if st.bot_positions or self.broker.positions(magic=self.magic):
            return                                   # fermetures en cours : on attend d'être à plat
        amount = float(status["withdrawable"])
        if st.account_trade_mode == TradeMode.DEMO.value and status["demo_as_funded"]:
            rec = st.register_simulated_withdrawal(amount, now)
            self.journal.event("simulated_withdrawal", source="payout_cycle", **rec, broker_balance=st.broker_balance,
                               balance_after=st.balance, simulated_withdrawn_total=st.simulated_withdrawn_total())
            pay = st.record_payout(amount, now, simulated=True, trading_days=status["trading_days_done"])
            st.unlock_entries("PAYOUT_WINDOW")
            self.journal.event("payout_done", level="WARNING", **pay,
                               trader_share=round(amount * status["profit_split_percent"] / 100.0, 2),
                               profit_split_percent=status["profit_split_percent"])
        elif st.payout_window_since != "REAL_PENDING":
            # compte financé : la demande se fait sur le tableau de bord FOXX (24 h) ; le bot reste verrouillé
            # jusqu'à PAYOUT_DONE <montant>
            st.payout_window_since = "REAL_PENDING"
            self.journal.event("payout_ready", level="WARNING", withdrawable=amount,
                               profit_split_percent=status["profit_split_percent"],
                               message="demander le retrait sur le tableau de bord FOXX puis PAYOUT_DONE <montant>")

    def _payout_done(self, target: str, source: str) -> dict:
        """PAYOUT_DONE <montant> : le retrait a été demandé/reçu (compte financé) → nouveau cycle. 0 = renoncer."""
        st = self.state
        try:
            amount = float(str(target or "0").replace(",", "."))
        except ValueError:
            return {"ok": False, "reason": f"montant illisible: {target!r}", "usage": "PAYOUT_DONE <montant|0>"}
        rec = st.record_payout(amount, self.now_fn(), simulated=False, trading_days=len(st.cycle_trading_days())) if amount > 0 else None
        st.payout_window_since = ""
        st.unlock_entries("PAYOUT_WINDOW")
        self.journal.event("payout_done", level="WARNING", simulated=False, source=source, amount=amount, skipped=amount <= 0)
        return {"ok": True, "payout": rec, "skipped": amount <= 0}

    def _simulate_withdrawal(self, target: str, source: str) -> dict:
        """Retrait simulé : retranche un montant du solde vu par le labo (compte DEMO uniquement).

        MT5 n'autorise aucune opération de solde sur un compte démo ; le décalage est donc
        tenu côté labo. `RESET` annule tous les retraits simulés.
        """
        st = self.state
        if st.account_trade_mode != TradeMode.DEMO.value:
            return {"ok": False, "reason": f"refusé : compte {st.account_trade_mode}, retrait simulé réservé au DEMO"}
        if target.upper() in ("RESET", "ANNULER", "0"):
            n = st.clear_simulated_withdrawals()
            self.journal.event("simulated_withdrawal_reset", removed=n, source=source)
            return {"ok": True, "reset": n, "simulated_withdrawn_total": st.simulated_withdrawn_total()}
        try:
            amount = float(target.replace(",", "."))
        except ValueError:
            return {"ok": False, "reason": f"montant illisible: {target!r}", "usage": "SIMULATE_WITHDRAWAL <montant|RESET>"}
        if amount > st.balance:
            return {"ok": False, "reason": f"montant {amount:.2f} > solde disponible {st.balance:.2f}"}
        try:
            rec = st.register_simulated_withdrawal(amount, self.now_fn())
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        self.journal.event("simulated_withdrawal", source=source, **rec,
                           broker_balance=st.broker_balance, balance_after=st.balance,
                           simulated_withdrawn_total=st.simulated_withdrawn_total())
        return {"ok": True, "withdrawal": rec, "broker_balance": st.broker_balance,
                "balance_after": round(st.balance, 2), "equity_after": round(st.equity, 2),
                "simulated_withdrawn_total": st.simulated_withdrawn_total()}

    def handle_command(self, command: str, args: dict, source: str = "cli") -> dict:
        st = self.state
        if not isinstance(args, dict):
            args = {}
        c = command.upper()
        res: dict = {"command": c, "ok": True}
        if c == "PAUSE":
            st.set_mode(SystemMode.PAUSED, f"PAUSE ({source})")
        elif c == "RESUME":
            if self.s.autonomous_demo and st.account_trade_mode == TradeMode.DEMO.value and "ACCOUNT_MISMATCH" not in st.lock_reasons:
                st.set_mode(SystemMode.AUTO, "")
                self.requested_mode = "AUTO"
            else:
                res.update(ok=False, reason="RESUME refusé : compte non DEMO ou automatisation désactivée")
        elif c == "SAFE_MODE":
            # symétrique de RESUME : le mode demandé devient SAFE, sinon _maybe_go_auto repasserait en AUTO au même cycle
            st.set_mode(SystemMode.SAFE_MODE, f"commande ({source})")
            self.requested_mode = "SAFE"
            self._healthy_cycles = 0
        elif c == "SIMULATE_WITHDRAWAL":
            res.update(self._simulate_withdrawal(str(args.get("target", "") or "").strip(), source))
        elif c == "RESET_LOSSES":
            # levée manuelle du verrou « pertes consécutives » (demande utilisateur 2026-09-24) : le compteur est remis
            # à zéro, le Daily Guard lève le verrou au cycle suivant. Les autres verrous (perte jour, giveback, prop)
            # ne sont PAS touchés.
            before = st.consecutive_losses
            st.consecutive_losses = 0
            st.unlock_entries("MAX_CONSECUTIVE_LOSSES")
            self.journal.event("losses_reset", level="WARNING", source=source, consecutive_losses_before=before,
                               lock_reasons_after=list(st.lock_reasons))
            res.update(consecutive_losses_before=before, lock_reasons=list(st.lock_reasons))
        elif c == "PAYOUT_DONE":
            res.update(self._payout_done(str(args.get("target", "") or "").strip(), source))
        elif c == "PANIC":
            st.set_mode(SystemMode.PANIC, f"PANIC ({source})")
            st.lock_entries("PANIC")
            cancelled = self.pm.cancel_all_bot_orders()
            closed = self.pm.close_all_bot("panic") if self.s.get("commands.panic_close_all_bot_positions", True) else []
            res.update(cancelled=cancelled, closed=closed)
        elif c == "CLOSE":
            target = str(args.get("target", ""))
            if target.isdigit():
                res["ok"] = self.pm.close(int(target), "manual")
            else:
                closed = [self.pm.close(p.ticket, "manual") for p in self.broker.positions(magic=self.magic) if p.symbol == target]
                res.update(closed=len(closed))
        elif c == "CLOSE_WINNERS":
            # bouton « Encaisser les gagnantes » (demande utilisateur 2026-09-24) : ferme immédiatement chaque position du
            # bot en profit (profit + swap > 0 chez le broker) et laisse les positions en perte actives
            closed, kept = [], []
            for p_ in self.broker.positions(magic=self.magic):
                gain = float(p_.profit) + float(getattr(p_, "swap", 0.0) or 0.0)
                if gain > 0:
                    ok = self.pm.close(p_.ticket, "encaisser gagnantes")
                    closed.append({"ticket": p_.ticket, "symbol": p_.symbol, "profit": round(gain, 2), "ok": bool(ok)})
                else:
                    kept.append({"ticket": p_.ticket, "symbol": p_.symbol, "profit": round(gain, 2)})
            res.update(ok=True, closed=closed, kept=kept,
                       gain_total=round(sum(x["profit"] for x in closed if x["ok"]), 2),
                       message=f"{sum(1 for x in closed if x['ok'])} position(s) gagnante(s) fermée(s), {len(kept)} en perte laissée(s) actives")
        elif c == "CLOSE_ALL_BOT":
            res["closed"] = self.pm.close_all_bot("manual")
        elif c == "BREAK_EVEN":
            target = str(args.get("target", "") or "").strip()
            if target.upper() in ("", "ALL", "TOUT", "TOUTES"):
                # bouton « Armer partout » du panneau (2026-09-24 : il envoyait BREAK_EVEN sans ticket et le bot
                # répondait « ticket invalide ») : break-even sur chaque position du bot où il est possible, c.-à-d.
                # déjà assez en profit pour que le stop passe à l'entrée ; les autres sont laissées telles quelles
                done, skipped = [], []
                for t, plan in list(st.bot_positions.items()):
                    if plan.break_even_done:
                        skipped.append({"ticket": int(t), "symbol": plan.symbol, "raison": "déjà au break-even"})
                    elif self.pm.move_to_break_even(int(t), self.broker.symbol_info(plan.symbol)):
                        done.append({"ticket": int(t), "symbol": plan.symbol})
                    else:
                        skipped.append({"ticket": int(t), "symbol": plan.symbol, "raison": "pas assez en profit"})
                res.update(ok=True, armed=done, skipped=skipped,
                           message=f"break-even armé sur {len(done)} position(s), {len(skipped)} laissée(s)")
            elif not target.isdigit():
                res.update(ok=False, reason="ticket invalide")
            else:
                t = int(target)
                plan = st.bot_positions.get(str(t))
                res["ok"] = bool(plan) and self.pm.move_to_break_even(t, self.broker.symbol_info(plan.symbol))
        else:
            res.update(ok=False, reason="commande inconnue ou en lecture seule (traitée par la CLI)")
        self.journal.event("command", source=source, **res)
        self.store.save()
        return res

    # ------------------------------------------------------------------ boucle
    def run(self, max_cycles: Optional[int] = None) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        self.startup()
        n = 0
        base = float(self.s.scheduler.get("position_manager_interval_sec", 15))
        while self._running:
            try:
                self.cycle()
            except Exception as e:  # noqa: BLE001 - la boucle ne meurt pas, on journalise et on passe en SAFE_MODE
                self.journal.error("cycle exception", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
                self.state.set_mode(SystemMode.SAFE_MODE, f"exception cycle: {type(e).__name__}")
                # SAFE_MODE conservé jusqu'à une commande RESUME : jamais de retour automatique en AUTO après une exception
                self.requested_mode = "SAFE"
                self._healthy_cycles = 0
                self.store.save()
            n += 1
            if max_cycles and n >= max_cycles:
                break
            time.sleep(self.scheduler.sleep_seconds(base, self.now_fn()))
        self.journal.event("shutdown", cycles=n)

    def stop(self) -> None:
        self._running = False


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Orchestrateur Claude MT5 Trading Lab")
    ap.add_argument("--home", default=None)
    ap.add_argument("--mode", default="SAFE", choices=["SAFE", "AUTO"])
    ap.add_argument("--broker", default=None, help="mt5 | mock")
    ap.add_argument("--cycles", type=int, default=None, help="nombre de cycles (tests)")
    args = ap.parse_args(argv)
    s = load_settings(Path(args.home) if args.home else None)
    broker = make_broker(args.broker or s.broker_kind, s)
    orch = Orchestrator(s, broker, args.mode)
    try:
        orch.run(max_cycles=args.cycles)
    except KeyboardInterrupt:
        orch.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
