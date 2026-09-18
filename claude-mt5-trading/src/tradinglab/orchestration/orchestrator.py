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
import threading
import time
import traceback
from datetime import datetime, timedelta
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
from ..execution.gate import ExecutionGate, GateContext
from ..execution.position_manager import MarketContext, PMConfig, PositionManager
from ..learning.post_trade import PostTradeAnalyzer, build_trade_record
from ..learning.store import LearningStore
from ..macro.sentiment import risk_sentiment
from ..market_data.feed import MarketDataFeed, MarketSnapshot
from ..market_data.indicators import last_closed, swing_points
from ..models.client import LLMClient
from ..models.router import ModelRouter
from ..monitoring.watchdog import read_watchdog_report
from ..mt5.adapter import BrokerAdapter
from ..mt5.mock_adapter import make_broker
from ..mt5.symbols import resolve_symbols, root_of
from ..news.hub import NewsHub
from ..news.providers import build_providers
from ..research.pipeline import DegradationManager, ResearchPipeline
from ..risk.correlation_guard import CorrelationGuard, CorrelationLimits
from ..risk.daily_guard import DailyGuard
from ..risk.prop_guard import PropGuard, PropProfile
from ..risk.risk_manager import RiskLimits, RiskManager
from ..shadow.shadow import ShadowTrader
from .market_router import MarketRouter
from .scheduler import Scheduler

MAX_ENTRIES_PER_CYCLE = 2


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
        self.corr = CorrelationGuard(CorrelationLimits.from_config(settings.correlation), settings.markets.get("asset_class_rules", {}))
        self.gate = ExecutionGate(broker, self.risk, self.prop, self.daily, self.corr)
        self.executor = Executor(broker, self.store, self.journal)
        self.pm = PositionManager(broker, self.store, self.journal, PMConfig.from_config(settings.profit_management), self.magic)
        tfs = settings.scheduler.get("timeframes", ["M5", "M15", "H1", "H4", "D1"])
        self.feed = MarketDataFeed(broker, tfs, bars=max(300, int(settings.system.get("min_bars_required", 250)) + 50),
                                   max_tick_age_sec=int(settings.system.get("data_max_age_sec", 30)),
                                   min_bars=int(settings.system.get("min_bars_required", 250)))
        self.registry = AgentRegistry(status_file=settings.data_dir / "agent_status.json")
        self.router_ = MarketRouter(self.registry, settings.markets)
        self.model_router = ModelRouter(settings.models)
        self.llm: Optional[LLMClient] = None
        self.review = AdversarialReview(None, float(settings.execution.get("required_setup_score", 65)),
                                        float(settings.execution.get("min_rr_required", 1.5)), int(settings.learning.get("min_sample_size", 40)))
        self.news = NewsHub(build_providers(settings.news), settings.news, cache_dir=settings.data_dir / "cache")
        self.learning = LearningStore(settings.data_dir / "learning.db")
        self.post_trade = PostTradeAnalyzer(self.learning, None, settings.learning)
        self.shadow = ShadowTrader(self.learning, settings.state_dir / "shadow_positions.json")
        self.degradation = DegradationManager(self.registry, self.learning, settings.learning, self.journal)
        self.research: Optional[ResearchPipeline] = None
        sch = settings.scheduler
        self.scheduler = Scheduler({"position_manager": sch.get("position_manager_interval_sec", 15), "fast_scanner": sch.get("fast_scanner_interval_sec", 60),
                                    "news": sch.get("news_interval_sec", 120), "calendar": sch.get("calendar_interval_sec", 900),
                                    "research": sch.get("research_interval_sec", 3600), "models": settings.models.get("refresh_interval_sec", 3600),
                                    "export": 300}, tfs)
        self.universe: dict[str, str] = {}      # racine -> symbole broker
        self.symbol_groups: dict[str, str] = {}  # racine -> groupe (forex_majors...)
        self.snapshots: dict[str, MarketSnapshot] = {}
        self.candidates_cache: dict[str, dict] = {}
        self._running = True
        self._healthy_cycles = 0
        self._research_lock = threading.Lock()
        self._research_thread: Optional[threading.Thread] = None
        self._bar_candidates: dict[str, TradeCandidate] = {}

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
            self.journal.error("account_info indisponible")
            return False
        st.mt5_connected, st.account_trade_mode, st.account_login, st.account_server, st.currency = True, acc.trade_mode.value, acc.login, acc.server, acc.currency
        st.roll_day_if_needed(acc.equity, acc.balance, self.now_fn().date())
        st.update_equity(acc.equity, acc.balance)
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
        self._build_universe()
        # reprise : adopter les positions existantes du bot, ne jamais ré-ouvrir
        self.pm.sync()
        self.journal.event("recovery", bot_positions=list(st.bot_positions), executed_keys=len(st.executed_keys))
        self._setup_models()
        self._setup_research()
        self.scheduler.due("research", self.now_fn())   # pas de recherche au premier cycle (chemin critique)
        self.scheduler.due("models", self.now_fn())
        self.store.save()
        return True

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
        self.model_router.refresh_availability()
        if self.model_router.available_models:
            self.llm = LLMClient(self.model_router, self.state, int(self.s.models.get("cache_ttl_sec", 240)), journal=self.journal)
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
            self.store.save()
            summary["error"] = "broker déconnecté"
            return summary
        acc = self.broker.account_info()
        if acc is None:
            st.mt5_connected = False
            self.store.save()
            summary["error"] = "account_info None"
            return summary
        st.mt5_connected = True
        st.account_trade_mode = acc.trade_mode.value
        if st.roll_day_if_needed(acc.equity, acc.balance, now.date()):
            self.journal.event("new_day", starting_equity=acc.equity)
        st.update_equity(acc.equity, acc.balance)
        # 2. positions : sync + post-trade sur les fermetures
        closed = self.pm.sync()
        for plan in closed:
            self._on_position_closed(plan, now)
        # 3. données + news
        news_shock = {}
        if self.scheduler.due("news", now):
            self.news.refresh_news(now)
        if self.scheduler.due("calendar", now):
            self.news.refresh_calendar(now)
        st.news_data_degraded = self.news.state.degraded
        st.calendar_data_degraded = self.news.state.calendar_degraded
        self.snapshots = {}
        for root, sym in self.universe.items():
            spec = self.broker.symbol_info(sym)
            ccys = [spec.currency_base, spec.currency_profit] if spec else []
            shock = self.news.news_shock(ccys, now) if ccys else False
            try:
                self.snapshots[sym] = self.feed.snapshot(sym, now=now, news_shock=shock)
            except ValueError as e:
                self.journal.warn("snapshot impossible", symbol=sym, error=str(e))
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
            summary.update(self._scan_and_execute(now, dd))
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

    def _scan_and_execute(self, now: datetime, dd) -> dict:
        st = self.state
        out: dict = {}
        active_statuses = (AgentStatus.LIVE.value,)
        cands, rep = self.router_.route(self.snapshots, active_statuses)
        st.active_agents = sorted({a for lst in rep.agents_activated.values() for a in lst})
        out["routing"] = rep.to_dict()
        # candidats shadow (agents SHADOW/CANDIDATE) — jamais exécutés
        shadow_cands, _ = self.router_.route(self.snapshots, (AgentStatus.SHADOW.value, AgentStatus.CANDIDATE.value))
        self.shadow.open_from_candidates(shadow_cands, now)
        # enrichissement + revue
        macro = out.get("macro") or self._macro_context()
        reviewed: list[TradeCandidate] = []
        for c in cands:
            spec = self.registry.get(c.agent_id)
            nc = self.news.check(self._symbol_currencies(c.symbol), now, news_sensitive_strategy=bool(spec and spec.news_sensitive))
            c.news_state = nc.state
            c.macro_alignment = macro.get("risk_sentiment", "UNKNOWN")
            stats = self.learning.agent_stats(c.agent_id)
            c.historical_stats = {"sample_size": stats.sample_size, "expectancy_r": stats.expectancy_r, "profit_factor": stats.profit_factor, "win_rate": stats.win_rate}
            c.sample_size = stats.sample_size
            snap = self.snapshots[c.symbol]
            c.review["similar_situations"] = self.learning.similar_situations(c.symbol, c.regime.value, c.session, snap.regime.features.get("vol_pct"))
            self.review.review(c, dd.setup_score_bonus)
            reviewed.append(c)
        reviewed.sort(key=lambda x: x.setup_score, reverse=True)
        st.top_setups = [{"symbol": c.symbol, "side": c.side.value, "setup_score": c.setup_score, "agent_id": c.agent_id,
                          "verdict": c.verdict.value if c.verdict else None, "rr": c.rr, "news": c.news_state} for c in reviewed[:10]]
        out["candidates"] = len(reviewed)
        for c in reviewed:
            self.journal.event("candidate", candidate=c.to_dict())
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
            self.journal.event("gate", candidate_id=c.id, symbol=c.symbol, approved=gate_res.approved, reason=gate_res.reason,
                               checks=[ch.__dict__ for ch in gate_res.checks])
            if not gate_res.approved or req is None:
                continue
            sizing = self.risk.size(st.equity, c.entry, c.sl, self.broker.symbol_info(c.symbol), risk_percent=dd.risk_percent)
            outcome = self.executor.execute(c, gate_res, req, sizing.risk_money, sizing.risk_percent_effective)
            self.journal.event("execution", candidate_id=c.id, executed=outcome.executed, message=outcome.message,
                               ticket=outcome.position.ticket if outcome.position else None)
            if outcome.executed and outcome.position:
                entries += 1
                self.candidates_cache[str(outcome.position.ticket)] = c.to_dict()
                snap = self.snapshots[c.symbol]
                sid = self.learning.store_snapshot(c.symbol, c.regime.value, c.session, {**snap.regime.features, "setup_score": c.setup_score},
                                                   macro=c.macro_alignment, news_state=c.news_state)
                self.candidates_cache[str(outcome.position.ticket)]["snapshot_id"] = sid
        out["entries"] = entries
        return out

    def _gate(self, c: TradeCandidate, now: datetime, corr):
        st = self.state
        snap = self.snapshots.get(c.symbol)
        spec = self.broker.symbol_info(c.symbol)
        tick = self.broker.tick(c.symbol)
        wd = read_watchdog_report(self.s.state_dir)
        wd_alive = bool(wd) and (now - datetime.fromisoformat(wd["heartbeat"])).total_seconds() <= float(self.s.system.get("heartbeat_max_age_sec", 45)) \
            if wd and wd.get("heartbeat") else self.broker.name == "mock"
        agent = self.registry.get(c.agent_id)
        nc = self.news.check(self._symbol_currencies(c.symbol), now, news_sensitive_strategy=bool(agent and agent.news_sensitive))
        bot_pos = list(st.bot_positions.values())
        ex = self.s.execution
        sp_cfg = ex.get("max_spread_points", {})
        max_sp = sp_cfg.get("default", 40)
        if spec:
            if spec.asset_class == "metals":
                max_sp = sp_cfg.get("XAU", max_sp)
            elif spec.asset_class == "indices":
                max_sp = sp_cfg.get("indices", max_sp)
        ctx = GateContext(candidate=c, state=st, account=self.broker.account_info(), spec=spec, tick=tick,
                          atr=snap.atr_h1 if snap else 0.0, data_quality=snap.data_quality if snap else "NO_DATA",
                          market_open=forex_market_open(now) if (spec and spec.asset_class == "forex") else True,
                          news_check=nc, correlations=corr, now=now, watchdog_alive=wd_alive,
                          open_positions_symbol=sum(1 for p in bot_pos if p.symbol == c.symbol), open_positions_total=len(bot_pos),
                          max_spread_points=int(max_sp), max_spread_atr_ratio=float(ex.get("max_spread_atr_ratio", 0.15)),
                          min_rr_required=float(ex.get("min_rr_required", 1.5)), required_setup_score=float(ex.get("required_setup_score", 65)),
                          min_sl_atr_ratio=float(ex.get("min_sl_atr_ratio", 0.25)), max_sl_atr_ratio=float(ex.get("max_sl_atr_ratio", 4.0)),
                          deviation_points=int(ex.get("slippage_deviation_points", 20)), magic=self.magic,
                          comment_prefix=str(self.s.system.get("order_comment_prefix", "TLAB")))
        return self.gate.evaluate(ctx)

    def _manage_positions(self) -> None:
        st = self.state
        for ticket, plan in list(st.bot_positions.items()):
            pos = self.broker.position(int(ticket))
            spec = self.broker.symbol_info(plan.symbol)
            if pos is None or spec is None:
                continue
            snap = self.snapshots.get(plan.symbol)
            ctx = MarketContext(atr=snap.atr_h1 if snap else abs(plan.entry - plan.initial_sl))
            if snap is not None:
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
                    lc = last_closed(df)
                    if "ema50" in lc and lc["ema50"] == lc["ema50"]:
                        ctx.invalidated = (lc["close"] < lc["ema50"] and side is Side.BUY and plan.max_r < 0.5) or \
                                          (lc["close"] > lc["ema50"] and side is Side.SELL and plan.max_r < 0.5)
                        ctx.invalidated = ctx.invalidated and "EMA50" in (plan.invalidation or "")
                if snap.regime.regime.value == "NEWS_SHOCK":
                    ctx.invalidated = False  # on gère sans supprimer le SL, pas de sortie forcée
            actions = self.pm.manage(plan, pos, spec, ctx)
            if actions:
                self.journal.event("position_managed", ticket=int(ticket), actions=actions)

    def _on_position_closed(self, plan, now: datetime) -> None:
        deals = self.broker.history_deals(now - timedelta(days=30), now + timedelta(minutes=5))
        cand = self.candidates_cache.pop(str(plan.ticket), None)
        rec = build_trade_record(plan, deals, cand, now.isoformat(), session=(cand or {}).get("session", ""))
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
        review = self.post_trade.analyze(rec)
        self.journal.event("post_trade_review", ticket=plan.ticket, agent_id=plan.agent_id, result_r=rec.result_r, pnl=rec.pnl, review=review.to_dict())
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

    def _research_cycle(self) -> None:
        changes = self.degradation.run()
        if changes:
            self.journal.event("degradation", changes=changes)
        if self.research is None:
            return
        # faire avancer jusqu'à 2 agents non-LIVE dans le pipeline
        n = 0
        for a in list(self.registry.agents.values()):
            if n >= 2:
                break
            if a.generates_trades and a.status in (AgentStatus.RESEARCH.value, AgentStatus.BACKTEST.value, AgentStatus.SHADOW.value, AgentStatus.CANDIDATE.value):
                try:
                    self.research.advance(a.agent_id)
                    n += 1
                except Exception as e:  # noqa: BLE001
                    self.journal.warn("recherche : étape échouée", agent_id=a.agent_id, error=f"{type(e).__name__}: {e}")
        # nouveaux challengers pour les champions dégradés
        for a in self.registry.by_status(AgentStatus.DEGRADED):
            if not any(x.parent_id == a.agent_id for x in self.registry.agents.values()):
                self.research.generate_challengers(a, 1)

    def _export_learning(self) -> None:
        try:
            self.learning.export_stats_json(self.s.data_dir / "agent_stats.json")
            import json
            (self.s.reports_dir / "leaderboard.json").write_text(json.dumps(self.learning.leaderboard(), ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            self.journal.warn("export learning échoué", error=str(e))

    # ------------------------------------------------------------------ commandes
    def _process_commands(self) -> None:
        for cmd in self.store.pop_commands():
            self.handle_command(cmd.get("command", ""), cmd.get("args", {}) or {}, cmd.get("source", "?"))

    def handle_command(self, command: str, args: dict, source: str = "cli") -> dict:
        st = self.state
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
            st.set_mode(SystemMode.SAFE_MODE, f"commande ({source})")
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
        elif c == "CLOSE_ALL_BOT":
            res["closed"] = self.pm.close_all_bot("manual")
        elif c == "BREAK_EVEN":
            t = int(args.get("target", 0) or 0)
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
