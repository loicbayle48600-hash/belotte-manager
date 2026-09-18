"""Rapports : jour, semaine, mois, stratégie, leaderboard, risque, prop, coût modèles, apprentissage.

Chaque rapport inclut le contexte nécessaire et ne masque jamais les pertes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..core.journal import Journal
from ..core.state import SystemState
from ..core.types import utcnow
from .store import LearningStore


def _period_trades(store: LearningStore, days: int, now: Optional[datetime] = None) -> list[dict]:
    now = now or utcnow()
    since = (now - timedelta(days=days)).isoformat()
    return [t for t in store.trades(mode="live") if (t.get("closed_at") or "") >= since]


def _summ(trades: list[dict]) -> dict:
    rs = [float(t["result_r"]) for t in trades]
    pnl = [float(t["pnl"]) for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {"trades": len(rs), "wins": len(wins), "losses": len(losses), "total_r": round(sum(rs), 2), "total_pnl": round(sum(pnl), 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0, "worst_r": round(min(rs), 2) if rs else 0.0, "best_r": round(max(rs), 2) if rs else 0.0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else (99.0 if wins else 0.0)}


def daily_report(store: LearningStore, state: SystemState, journal: Journal, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    trades = _period_trades(store, 1, now)
    events = journal.read_day(now)
    kinds = {}
    for e in events:
        kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1
    gates = [e for e in events if e.get("kind") == "gate"]
    refused = {}
    for g in gates:
        if not g.get("approved"):
            for ch in g.get("checks", []):
                if not ch.get("ok"):
                    refused[ch["name"]] = refused.get(ch["name"], 0) + 1
    return {"type": "daily", "date": now.date().isoformat(), "mode": state.mode, "equity": state.equity, "balance": state.balance,
            "daily_pnl": state.daily_pnl(), "daily_pnl_percent": round(state.daily_pnl_percent(), 3),
            "daily_drawdown_percent": round(state.daily_drawdown_percent(), 3), "overall_drawdown_percent": round(state.overall_drawdown_percent(), 3),
            "trades": _summ(trades), "by_agent": _by(trades, "agent_id"), "by_symbol": _by(trades, "symbol"),
            "gate_refusals": refused, "events": kinds, "locks": state.lock_reasons, "news_degraded": state.news_data_degraded,
            "model_cost_usd": state.model_budget.spent_usd, "open_positions": len(state.bot_positions), "restarts": state.restarts}


def _by(trades: list[dict], key: str) -> dict:
    g: dict[str, list[dict]] = {}
    for t in trades:
        g.setdefault(str(t.get(key)), []).append(t)
    return {k: _summ(v) for k, v in g.items()}


def weekly_report(store: LearningStore, state: SystemState, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    trades = _period_trades(store, 7, now)
    return {"type": "weekly", "until": now.date().isoformat(), "trades": _summ(trades), "by_agent": _by(trades, "agent_id"),
            "by_regime": _by(trades, "regime"), "by_session": _by(trades, "session"), "leaderboard": store.leaderboard(min_sample=5),
            "overall_drawdown_percent": round(state.overall_drawdown_percent(), 3)}


def monthly_report(store: LearningStore, state: SystemState, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    trades = _period_trades(store, 30, now)
    return {"type": "monthly", "until": now.date().isoformat(), "trades": _summ(trades), "by_agent": _by(trades, "agent_id"),
            "leaderboard": store.leaderboard(min_sample=10)}


def strategy_report(store: LearningStore, agent_id: str) -> dict:
    st = store.agent_stats(agent_id)
    return {"type": "strategy", **st.to_dict(), "shadow": store.agent_stats(agent_id, mode="shadow").to_dict(), "events": store.agent_events(agent_id, 20)}


def risk_report(state: SystemState, risk_cfg: dict) -> dict:
    return {"type": "risk", "equity": state.equity, "open_risk_percent": round(state.open_risk_percent(), 3),
            "open_risk_money": state.open_risk_money(), "daily_drawdown_percent": round(state.daily_drawdown_percent(), 3),
            "overall_drawdown_percent": round(state.overall_drawdown_percent(), 3), "consecutive_losses": state.consecutive_losses,
            "limits": risk_cfg, "locks": state.lock_reasons, "positions": {t: p.__dict__ for t, p in state.bot_positions.items()}}


def model_cost_report(state: SystemState, journal: Journal, now: Optional[datetime] = None) -> dict:
    events = journal.read_day(now or utcnow(), kinds={"llm_call", "llm_skipped"})
    by_role: dict = {}
    for e in events:
        r = e.get("role", "?")
        d = by_role.setdefault(r, {"calls": 0, "skipped": 0, "cost_usd": 0.0, "tokens": 0})
        if e.get("kind") == "llm_call":
            d["calls"] += 1
            d["cost_usd"] += float(e.get("cost_usd", 0.0))
            d["tokens"] += int(e.get("input_tokens", 0)) + int(e.get("output_tokens", 0))
        else:
            d["skipped"] += 1
    return {"type": "model_cost", "spent_today_usd": state.model_budget.spent_usd, "calls_by_tier_day": state.model_budget.calls_by_tier_day, "by_role": by_role}


def learning_report(store: LearningStore, registry_summary: dict) -> dict:
    return {"type": "learning", "live_trades": store.trade_count("live"), "shadow_trades": store.trade_count("shadow"),
            "registry": registry_summary, "leaderboard": store.leaderboard(min_sample=1)[:20], "recent_events": store.agent_events(limit=30)}


def write_report(reports_dir: Path, name: str, data: dict) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    p = reports_dir / f"{name}-{utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return p
