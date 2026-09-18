"""Base d'apprentissage persistante (SQLite) : trades, statistiques par agent, dégradation.

Le système apprend par données persistantes, pas par intuition. Aucune promotion
sur petit échantillon (MIN_SAMPLE_SIZE).
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.types import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket INTEGER, mode TEXT DEFAULT 'live', agent_id TEXT, agent_version TEXT, strategy TEXT, symbol TEXT, side TEXT,
  session TEXT, regime TEXT, timeframes TEXT, features TEXT, entry REAL, sl REAL, tp TEXT, risk_money REAL,
  risk_percent REAL, spread_points INTEGER, news_context TEXT, macro_context TEXT, reasoning_summary TEXT,
  result_r REAL, pnl REAL, mae_r REAL, mfe_r REAL, duration_sec REAL, exit_reason TEXT, mistakes TEXT,
  rule_compliance TEXT, opened_at TEXT, closed_at TEXT, review TEXT, candidate_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_agent ON trades(agent_id, mode);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent_id TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, regime TEXT, session TEXT, vol_pct REAL,
  structure TEXT, adx REAL, rsi REAL, spread_atr REAL, macro TEXT, news_state TEXT, features TEXT, outcome_r REAL, trade_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap ON market_snapshots(symbol, regime, session);
"""


@dataclass
class TradeRecord:
    ticket: int
    agent_id: str
    symbol: str
    side: str
    entry: float
    sl: float
    risk_money: float
    risk_percent: float
    result_r: float
    pnl: float
    opened_at: str
    closed_at: str
    mode: str = "live"                 # live | shadow | backtest
    agent_version: str = "1.0"
    strategy: str = ""
    session: str = ""
    regime: str = ""
    timeframes: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    tp: list[float] = field(default_factory=list)
    spread_points: int = 0
    news_context: str = ""
    macro_context: str = ""
    reasoning_summary: str = ""
    mae_r: float = 0.0
    mfe_r: float = 0.0
    duration_sec: float = 0.0
    exit_reason: str = ""
    mistakes: list[str] = field(default_factory=list)
    rule_compliance: dict = field(default_factory=dict)
    review: dict = field(default_factory=dict)
    candidate_id: str = ""


@dataclass
class AgentStats:
    agent_id: str
    sample_size: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    avg_r: float = 0.0
    median_r: float = 0.0
    max_drawdown_r: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    total_r: float = 0.0
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0
    by_regime: dict = field(default_factory=dict)
    by_session: dict = field(default_factory=dict)
    by_symbol: dict = field(default_factory=dict)
    by_side: dict = field(default_factory=dict)
    by_vol_bucket: dict = field(default_factory=dict)
    recent_expectancy_r: dict = field(default_factory=dict)   # window -> expectancy
    degradation_score: float = 0.0
    confidence_calibration: float = 0.0   # corrélation score/résultat (indicatif)

    def to_dict(self) -> dict:
        return asdict(self)


def _metrics(rs: list[float]) -> dict:
    if not rs:
        return {"sample_size": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "profit_factor": 0.0, "expectancy_r": 0.0,
                "avg_r": 0.0, "median_r": 0.0, "max_drawdown_r": 0.0, "sharpe": 0.0, "sortino": 0.0, "total_r": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp, gl = sum(wins), -sum(losses)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    n = len(rs)
    mean = sum(rs) / n
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    dsd = statistics.pstdev([min(0.0, r) for r in rs]) if n > 1 else 0.0
    return {"sample_size": n, "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / n,
            "profit_factor": pf if math.isfinite(pf) else 99.0, "expectancy_r": mean, "avg_r": mean,
            "median_r": statistics.median(rs), "max_drawdown_r": dd, "sharpe": (mean / sd * math.sqrt(n)) if sd > 0 else 0.0,
            "sortino": (mean / dsd * math.sqrt(n)) if dsd > 0 else 0.0, "total_r": sum(rs)}


class LearningStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- trades ----------
    def record_trade(self, t: TradeRecord) -> int:
        d = asdict(t)
        for k in ("timeframes", "features", "tp", "mistakes", "rule_compliance", "review"):
            d[k] = json.dumps(d[k], ensure_ascii=False, default=str)
        cols = ", ".join(d.keys())
        q = ", ".join("?" for _ in d)
        cur = self.conn.execute(f"INSERT INTO trades ({cols}) VALUES ({q})", list(d.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def trades(self, agent_id: Optional[str] = None, mode: str = "live", limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE mode=?"
        args: list = [mode]
        if agent_id:
            sql += " AND agent_id=?"
            args.append(agent_id)
        sql += " ORDER BY closed_at ASC, id ASC"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        if limit:
            rows = rows[-limit:]
        for r in rows:
            for k in ("timeframes", "features", "tp", "mistakes", "rule_compliance", "review"):
                try:
                    r[k] = json.loads(r[k]) if r[k] else None
                except (json.JSONDecodeError, TypeError):
                    pass
        return rows

    def trade_count(self, mode: str = "live") -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trades WHERE mode=?", (mode,)).fetchone()[0])

    # ---------- statistiques ----------
    def agent_stats(self, agent_id: str, mode: str = "live", windows: tuple[int, ...] = (20, 30, 50),
                    min_history: int = 60, pf_drop_ratio: float = 0.6, expectancy_drop_r: float = 0.15) -> AgentStats:
        rows = self.trades(agent_id, mode)
        rs = [float(r["result_r"]) for r in rows if r["result_r"] is not None]
        st = AgentStats(agent_id=agent_id)
        m = _metrics(rs)
        for k, v in m.items():
            setattr(st, k, v)
        if rows:
            st.avg_mae_r = sum(float(r["mae_r"] or 0) for r in rows) / len(rows)
            st.avg_mfe_r = sum(float(r["mfe_r"] or 0) for r in rows) / len(rows)
        for key, attr in (("regime", "by_regime"), ("session", "by_session"), ("symbol", "by_symbol"), ("side", "by_side")):
            groups: dict[str, list[float]] = {}
            for r in rows:
                groups.setdefault(str(r[key]), []).append(float(r["result_r"]))
            setattr(st, attr, {g: {"n": len(v), "expectancy_r": sum(v) / len(v), "win_rate": sum(1 for x in v if x > 0) / len(v)} for g, v in groups.items()})
        buckets: dict[str, list[float]] = {}
        for r in rows:
            vp = (r.get("features") or {}).get("vol_pct") if isinstance(r.get("features"), dict) else None
            b = "UNKNOWN" if vp is None else ("LOW" if vp < 33 else "MID" if vp < 66 else "HIGH")
            buckets.setdefault(b, []).append(float(r["result_r"]))
        st.by_vol_bucket = {b: {"n": len(v), "expectancy_r": sum(v) / len(v)} for b, v in buckets.items()}
        for w in windows:
            if len(rs) >= w:
                st.recent_expectancy_r[str(w)] = sum(rs[-w:]) / w
        # calibration : corrélation score de setup / résultat (indicatif, jamais une probabilité)
        pairs = [(float((r.get("features") or {}).get("setup_score", 0)), float(r["result_r"])) for r in rows
                 if isinstance(r.get("features"), dict) and "setup_score" in r["features"]]
        if len(pairs) >= 10:
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            cov = sum((x - mx) * (y - my) for x, y in pairs)
            vx = math.sqrt(sum((x - mx) ** 2 for x in xs)) * math.sqrt(sum((y - my) ** 2 for y in ys))
            st.confidence_calibration = cov / vx if vx > 0 else 0.0
        st.degradation_score = self.degradation_score(rs, windows, min_history, pf_drop_ratio, expectancy_drop_r)
        return st

    @staticmethod
    def degradation_score(rs: list[float], windows=(20, 30, 50), min_history: int = 60, pf_drop_ratio: float = 0.6,
                          expectancy_drop_r: float = 0.15) -> float:
        """0 = pas de dérive ; 1 = dégradation nette (PF récent < ratio du PF long terme ET/OU chute d'expectancy)."""
        if len(rs) < min_history:
            return 0.0
        long = _metrics(rs)
        score = 0.0
        for w in windows:
            if len(rs) < w:
                continue
            rec = _metrics(rs[-w:])
            if long["profit_factor"] > 0 and rec["profit_factor"] < pf_drop_ratio * long["profit_factor"]:
                score += 0.5 / len(windows)
            if rec["expectancy_r"] < long["expectancy_r"] - expectancy_drop_r:
                score += 0.5 / len(windows)
        return min(1.0, score)

    def all_agent_ids(self, mode: str = "live") -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT agent_id FROM trades WHERE mode=?", (mode,))]

    def leaderboard(self, mode: str = "live", min_sample: int = 1) -> list[dict]:
        out = []
        for aid in self.all_agent_ids(mode):
            st = self.agent_stats(aid, mode)
            if st.sample_size >= min_sample:
                out.append({"agent_id": aid, "sample_size": st.sample_size, "profit_factor": round(st.profit_factor, 2),
                            "expectancy_r": round(st.expectancy_r, 3), "win_rate": round(st.win_rate, 3),
                            "max_drawdown_r": round(st.max_drawdown_r, 2), "degradation_score": st.degradation_score})
        return sorted(out, key=lambda x: (x["expectancy_r"], x["sample_size"]), reverse=True)

    # ---------- événements agents ----------
    def agent_event(self, agent_id: str, event: str, detail: dict | str = "") -> None:
        self.conn.execute("INSERT INTO agent_events (ts, agent_id, event, detail) VALUES (?,?,?,?)",
                          (utcnow().isoformat(), agent_id, event, json.dumps(detail, ensure_ascii=False, default=str)))
        self.conn.commit()

    def agent_events(self, agent_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM agent_events" + (" WHERE agent_id=?" if agent_id else "") + " ORDER BY id DESC LIMIT ?"
        args = ([agent_id] if agent_id else []) + [limit]
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ---------- mémoire de marché ----------
    def store_snapshot(self, symbol: str, regime: str, session: str, features: dict, macro: str = "UNKNOWN",
                       news_state: str = "UNKNOWN", outcome_r: Optional[float] = None, trade_id: Optional[int] = None,
                       ts: Optional[datetime] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO market_snapshots (ts, symbol, regime, session, vol_pct, structure, adx, rsi, spread_atr, macro, news_state, features, outcome_r, trade_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((ts or utcnow()).isoformat(), symbol, regime, session, features.get("vol_pct"), features.get("structure"),
             features.get("adx"), features.get("rsi"), features.get("spread_atr"), macro, news_state,
             json.dumps(features, ensure_ascii=False, default=str), outcome_r, trade_id))
        self.conn.commit()
        return int(cur.lastrowid)

    def set_snapshot_outcome(self, snapshot_id: int, outcome_r: float, trade_id: int) -> None:
        self.conn.execute("UPDATE market_snapshots SET outcome_r=?, trade_id=? WHERE id=?", (outcome_r, trade_id, snapshot_id))
        self.conn.commit()

    def similar_situations(self, symbol: str, regime: str, session: Optional[str] = None, vol_pct: Optional[float] = None,
                           structure: Optional[str] = None, tol_vol: float = 20.0, limit: int = 200) -> dict:
        """Cas historiques comparables avec résultat connu. Jamais présenté comme une garantie."""
        sql = "SELECT * FROM market_snapshots WHERE symbol=? AND regime=? AND outcome_r IS NOT NULL"
        args: list = [symbol, regime]
        if session:
            sql += " AND session=?"
            args.append(session)
        if structure:
            sql += " AND structure=?"
            args.append(structure)
        if vol_pct is not None:
            sql += " AND vol_pct BETWEEN ? AND ?"
            args += [vol_pct - tol_vol, vol_pct + tol_vol]
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        rs = [float(r["outcome_r"]) for r in rows]
        if not rs:
            return {"n": 0, "note": "aucun cas comparable (UNKNOWN)"}
        m = _metrics(rs)
        return {"n": len(rs), "median_r": m["median_r"], "expectancy_r": m["expectancy_r"], "win_rate": m["win_rate"],
                "distribution": {"p10": sorted(rs)[int(0.1 * (len(rs) - 1))], "p90": sorted(rs)[int(0.9 * (len(rs) - 1))]},
                "note": "statistique descriptive sur cas passés — pas une garantie"}

    # ---------- export ----------
    def export_stats_json(self, path: Path, mode: str = "live") -> None:
        data = {aid: self.agent_stats(aid, mode).to_dict() for aid in self.all_agent_ids(mode)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
