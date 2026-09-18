"""Pont screeners ↔ moteur de backtest : transforme un AgentSpec en signal_fn sans lookahead."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable, Optional

import pandas as pd

from ..agents.registry import AgentSpec
from ..agents.screeners import run_screener
from ..backtest.engine import Signal
from ..core.clock import current_session
from ..core.types import Regime, Session, SymbolSpec
from ..market_data.indicators import enrich
from ..market_data.regime import classify_regime

RESAMPLE = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D"}


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = RESAMPLE[tf]
    g = df.set_index("time").resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum", "spread": "last"}).dropna().reset_index()
    return g


def make_signal_fn(spec: AgentSpec, symbol_spec: SymbolSpec, entry_tf: str = "M15", lookback: int = 400,
                   check_regime: bool = True, params_override: Optional[dict] = None) -> Callable[[pd.DataFrame], Optional[Signal]]:
    """signal_fn(df_slice) : df_slice = barres du tf d'entrée clôturées jusqu'à i. Aucune barre future n'est visible."""
    spec = AgentSpec(**{**spec.to_dict(), "params": {**spec.params, **(params_override or {})}})
    trend_tf = spec.timeframes.get("trend", "H1")
    spec.timeframes = {"entry": entry_tf, "trend": trend_tf}

    def fn(df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < 260:
            return None
        sub = df.iloc[-lookback:].reset_index(drop=True)
        # le screener ignore la dernière ligne (barre "en formation") : on duplique la dernière barre clôturée
        e = pd.concat([sub, sub.iloc[[-1]]], ignore_index=True)
        e_en = enrich(e)
        t = resample(sub, trend_tf) if trend_tf != entry_tf else sub
        if len(t) < 60:
            return None
        t = pd.concat([t, t.iloc[[-1]]], ignore_index=True)
        t_en = enrich(t)
        reg = classify_regime(t_en) if check_regime else None
        if check_regime and reg.regime.value not in spec.regimes:
            return None
        last_time = sub["time"].iloc[-1]
        ts = last_time.to_pydatetime() if hasattr(last_time, "to_pydatetime") else datetime.now(timezone.utc)
        sess = current_session(ts)
        if sess.value not in spec.sessions and sess is not Session.OFF:
            return None
        atr = float(e_en["atr14"].iloc[-2]) if pd.notna(e_en["atr14"].iloc[-2]) else 0.0
        snap = SimpleNamespace(symbol=symbol_spec.name, spec=symbol_spec, frames={entry_tf: e_en, trend_tf: t_en},
                               regime=reg or SimpleNamespace(regime=Regime.UNCERTAIN), session=sess, spread_points=symbol_spec.spread_points,
                               atr_h1=atr, data_quality="OK", tick=None)
        c = run_screener(spec, snap)
        if c is None:
            return None
        return Signal(side=c.side, sl=c.sl, tp=c.tp_plan[-1] if c.tp_plan else None, note=c.agent_id)
    return fn


def make_signal_factory(spec: AgentSpec, symbol_spec: SymbolSpec, entry_tf: str = "M15", lookback: int = 400):
    return lambda params: make_signal_fn(spec, symbol_spec, entry_tf, lookback, params_override=params)
