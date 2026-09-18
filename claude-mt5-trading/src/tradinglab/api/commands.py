"""Commandes utilisateur (CLI / MCP) : lecture directe de l'état + file de commandes vers l'orchestrateur.

Les commandes d'action (PAUSE, RESUME, SAFE_MODE, PANIC, CLOSE…) sont déposées
dans state/commands.jsonl et exécutées par l'orchestrateur. Si l'orchestrateur
est silencieux (heartbeat périmé), PANIC / CLOSE / CLOSE_ALL_BOT sont exécutés
directement via le broker (sécurité), sans jamais ouvrir de position.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..agents.registry import AgentRegistry
from ..core.config import Settings
from ..core.journal import Journal
from ..core.state import StateStore
from ..core.types import utcnow
from ..learning import reports
from ..learning.store import LearningStore
from ..monitoring.watchdog import read_watchdog_report
from ..mt5.mock_adapter import make_broker
from ..risk.prop_guard import PropGuard, PropProfile

READ_COMMANDS = {"STATUS", "POSITIONS", "RISK", "TODAY", "PERFORMANCE", "AGENTS", "TOP_AGENTS", "DEGRADED_AGENTS", "NEWS", "CALENDAR",
                 "WHY", "REPORT_DAY", "REPORT_WEEK", "RESEARCH_STATUS", "HELP"}
ACTION_COMMANDS = {"PAUSE", "RESUME", "SAFE_MODE", "PANIC", "CLOSE", "CLOSE_ALL_BOT", "BREAK_EVEN"}


class CommandHandler:
    def __init__(self, settings: Settings, source: str = "cli"):
        self.s = settings
        self.source = source
        self.store = StateStore(settings.state_dir)
        self.journal = Journal(settings.logs_dir, settings.system.get("timezone_local", "UTC"), component=source)
        self.learning = LearningStore(settings.data_dir / "learning.db")
        self.registry = AgentRegistry(status_file=settings.data_dir / "agent_status.json")

    # ---------- dispatch ----------
    def run(self, command: str, arg: Optional[str] = None) -> dict:
        c = command.upper().strip()
        st = self.store.reload()
        if c in READ_COMMANDS:
            return getattr(self, "cmd_" + c.lower())(st, arg)
        if c in ACTION_COMMANDS:
            return self._action(c, arg)
        return {"ok": False, "error": f"commande inconnue: {command}", "help": sorted(READ_COMMANDS | ACTION_COMMANDS)}

    # ---------- lecture ----------
    def cmd_help(self, st, arg) -> dict:
        return {"read": sorted(READ_COMMANDS), "actions": sorted(ACTION_COMMANDS)}

    def cmd_status(self, st, arg) -> dict:
        wd = read_watchdog_report(self.s.state_dir) or {}
        return {"mode": st.mode, "mode_reasons": st.mode_reasons, "new_trades_locked": st.new_trades_locked, "lock_reasons": st.lock_reasons,
                "mt5_connected": st.mt5_connected, "account": {"login": st.account_login, "server": st.account_server, "trade_mode": st.account_trade_mode, "currency": st.currency},
                "equity": st.equity, "balance": st.balance, "daily_pnl": st.daily_pnl(), "daily_pnl_percent": round(st.daily_pnl_percent(), 3),
                "daily_drawdown_percent": round(st.daily_drawdown_percent(), 3), "overall_drawdown_percent": round(st.overall_drawdown_percent(), 3),
                "open_positions": len(st.bot_positions), "open_risk_percent": round(st.open_risk_percent(), 3),
                "orchestrator_heartbeat_age_sec": self.store.heartbeat_age("orchestrator"), "watchdog": {k: wd.get(k) for k in ("heartbeat", "safe_mode_request", "reasons", "orchestrator_alive")},
                "news_data_degraded": st.news_data_degraded, "calendar_data_degraded": st.calendar_data_degraded,
                "active_agents": len(st.active_agents), "regimes": st.regimes, "model_budget": st.model_budget.__dict__, "restarts": st.restarts, "last_cycle": st.last_cycle}

    def cmd_positions(self, st, arg) -> dict:
        return {"positions": [p.__dict__ for p in st.bot_positions.values()]}

    def cmd_risk(self, st, arg) -> dict:
        pg = PropGuard(PropProfile.from_config(self.s.prop), self.s.autonomous_demo, self.s.autonomous_prop, float(self.s.risk.get("max_daily_loss_internal_percent", 1.0)))
        return {**reports.risk_report(st, self.s.risk), "prop": pg.compliance_report(st)}

    def cmd_today(self, st, arg) -> dict:
        return reports.daily_report(self.learning, st, self.journal)

    def cmd_performance(self, st, arg) -> dict:
        return {"weekly": reports.weekly_report(self.learning, st), "monthly": reports.monthly_report(self.learning, st)}

    def cmd_agents(self, st, arg) -> dict:
        return {"summary": self.registry.summary(), "active_now": st.active_agents,
                "agents": [{"agent_id": a.agent_id, "name": a.name, "family": a.family, "status": a.status, "strategy": a.strategy} for a in self.registry.agents.values()]}

    def cmd_top_agents(self, st, arg) -> dict:
        return {"leaderboard": self.learning.leaderboard(min_sample=1)[:20]}

    def cmd_degraded_agents(self, st, arg) -> dict:
        return {"degraded": [a.agent_id for a in self.registry.by_status("DEGRADED")], "suspended": [a.agent_id for a in self.registry.by_status("SUSPENDED")]}

    def cmd_news(self, st, arg) -> dict:
        cache = self.s.data_dir / "cache" / "news_cache.json"
        data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        return {"degraded": st.news_data_degraded, "cached_items": len(data.get("news", [])), "last_news_at": data.get("last_news_at"), "latest": data.get("news", [])[:15]}

    def cmd_calendar(self, st, arg) -> dict:
        cache = self.s.data_dir / "cache" / "news_cache.json"
        data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        now = utcnow().isoformat()
        upcoming = [e for e in data.get("calendar", []) if str(e.get("timestamp", "")) >= now][:20]
        return {"degraded": st.calendar_data_degraded, "upcoming": upcoming}

    def cmd_why(self, st, arg) -> dict:
        """WHY <ticket|symbol> : reconstitue la décision depuis le journal du jour et l'état."""
        target = str(arg or "")
        events = self.journal.read_day()
        # le journal de l'orchestrateur est dans logs/journal-*.jsonl (partagé par composant)
        hits = []
        for e in events:
            blob = json.dumps(e, ensure_ascii=False)
            if target and target in blob and e.get("kind") in ("candidate", "gate", "execution", "position_opened", "position_managed", "post_trade_review", "watchdog_alert"):
                hits.append({k: e[k] for k in e if k not in ("checks",)} | ({"failed_checks": [c for c in e.get("checks", []) if not c.get("ok")]} if "checks" in e else {}))
        plan = st.bot_positions.get(target)
        return {"target": target, "plan": plan.__dict__ if plan else None, "events": hits[-20:]}

    def cmd_report_day(self, st, arg) -> dict:
        r = reports.daily_report(self.learning, st, self.journal)
        p = reports.write_report(self.s.reports_dir, "daily", r)
        return {"file": str(p), **r}

    def cmd_report_week(self, st, arg) -> dict:
        r = reports.weekly_report(self.learning, st)
        p = reports.write_report(self.s.reports_dir, "weekly", r)
        return {"file": str(p), **r}

    def cmd_research_status(self, st, arg) -> dict:
        rdir = self.s.data_dir / "research"
        recs = []
        if rdir.exists():
            for f in sorted(rdir.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    recs.append({"agent_id": d.get("agent_id"), "stages": {k: v.get("passed") for k, v in d.get("stages", {}).items()}})
                except json.JSONDecodeError:
                    continue
        return {"registry": self.registry.summary(), "challengers": [a.agent_id for a in self.registry.agents.values() if a.created_by != "registry"],
                "shadow_trades": self.learning.trade_count("shadow"), "validation": recs}

    # ---------- actions ----------
    def _action(self, c: str, arg: Optional[str]) -> dict:
        args = {"target": arg} if arg else {}
        rec = self.store.push_command(c, args, self.source)
        self.journal.event("command_queued", command=c, args=args)
        out = {"ok": True, "queued": rec}
        hb = self.store.heartbeat_age("orchestrator")
        if c in ("PANIC", "CLOSE", "CLOSE_ALL_BOT") and hb > float(self.s.system.get("heartbeat_max_age_sec", 45)):
            out["emergency"] = self._emergency(c, arg)
        return out

    def _emergency(self, c: str, arg: Optional[str]) -> dict:
        """Orchestrateur silencieux : agir directement (fermeture / annulation uniquement, jamais d'ouverture)."""
        try:
            broker = make_broker(self.s.broker_kind, self.s)
            if not broker.connect():
                return {"ok": False, "error": broker.last_error()}
            magic = self.s.magic
            closed, cancelled = [], []
            for o in broker.pending_orders(magic=magic):
                if broker.cancel_order(o.ticket).ok:
                    cancelled.append(o.ticket)
            for p in broker.positions(magic=magic):
                if c == "CLOSE" and arg and not (str(p.ticket) == arg or p.symbol == arg):
                    continue
                if c == "PANIC" and not self.s.get("commands.panic_close_all_bot_positions", True):
                    continue
                if broker.close_position(p.ticket, comment=f"TLAB {c.lower()}").ok:
                    closed.append(p.ticket)
            self.journal.event("emergency_action", command=c, closed=closed, cancelled=cancelled)
            return {"ok": True, "closed": closed, "cancelled": cancelled}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
