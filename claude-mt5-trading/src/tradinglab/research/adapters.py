"""Pont screeners ↔ moteur de backtest : transforme un AgentSpec en signal_fn sans lookahead.

Performance : les indicateurs (``enrich``) sont causaux (la valeur à la barre i ne dépend que des barres ≤ i,
vérifié par test). Ils sont donc calculés UNE fois par backtest sur le frame complet (``fn.prepare(df_full)``,
appelé par ``run_backtest``) puis découpés par préfixe à chaque barre ; sans ``prepare`` (appel isolé, tests),
``fn`` recalcule sur le préfixe reçu et donne exactement le même résultat.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Optional

import pandas as pd

from ..agents.registry import AgentSpec
from ..agents.screeners import run_screener
from ..backtest.engine import Signal
from ..core.clock import current_session
from ..core.types import Regime, Session, SymbolSpec
from ..market_data.indicators import enrich
from ..market_data.regime import MIN_BARS as MIN_TREND_BARS, classify_regime

RESAMPLE = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D"}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
MIN_ENTRY_BARS = 260   # barres du tf d'entrée nécessaires aux indicateurs (ema200, vol_pct sur 200)


def tf_minutes(tf: str) -> int:
    return TF_MINUTES.get(str(tf).upper(), 60)


def required_bars(entry_tf: str, trend_tf: str) -> int:
    """Barres minimales du tf d'entrée pour disposer de MIN_TREND_BARS barres de tendance clôturées.

    Ex. M15/H1 → 260 ; M5/H1 → 732 ; H4/D1 → 366. En dessous, le régime de tendance est structurellement
    UNCERTAIN et l'agent ne peut jamais produire de signal : l'appelant doit le signaler, pas l'ignorer.
    """
    ratio = max(1, tf_minutes(trend_tf) // tf_minutes(entry_tf))
    return max(MIN_ENTRY_BARS, MIN_TREND_BARS * ratio + ratio)


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = RESAMPLE[tf]
    g = df.set_index("time").resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum", "spread": "last"}).dropna().reset_index()
    return g


def _with_forming_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Le screener ignore la dernière ligne (barre « en formation ») : on duplique la dernière barre clôturée."""
    return pd.concat([df, df.iloc[[-1]]], ignore_index=True)


def make_signal_fn(spec: AgentSpec, symbol_spec: SymbolSpec, entry_tf: str = "M15",
                   check_regime: bool = True, params_override: Optional[dict] = None) -> Callable[[pd.DataFrame], Optional[Signal]]:
    """signal_fn(df_slice) : df_slice = barres du tf d'entrée clôturées jusqu'à i. Aucune barre future n'est visible.

    La fonction renvoyée expose ``prepare(df_full)`` (pré-calcul, appelé par ``run_backtest``), ``data_error``
    (message si les données sont insuffisantes pour le tf de tendance, sinon None) et ``required_bars``.
    """
    spec = AgentSpec(**{**spec.to_dict(), "params": {**spec.params, **(params_override or {})}})
    trend_tf = spec.timeframes.get("trend", "H1")
    spec.timeframes = {"entry": entry_tf, "trend": trend_tf}
    entry_delta = timedelta(minutes=tf_minutes(entry_tf))
    trend_delta = timedelta(minutes=tf_minutes(trend_tf))
    need = required_bars(entry_tf, trend_tf)
    cache: dict = {"times": None, "e": None, "t": None, "t_times": None}

    def _trend_frame(df: pd.DataFrame) -> pd.DataFrame:
        return resample(df, trend_tf) if trend_tf != entry_tf else df

    def _closed_trend_cutoff(last_time) -> pd.Timestamp:
        # une barre de tendance ouverte en T est clôturée si T + trend_delta <= clôture de la barre d'entrée (L + entry_delta)
        return pd.Timestamp(last_time) + entry_delta - trend_delta

    def prepare(df_full: pd.DataFrame) -> None:
        """Pré-calcule les indicateurs une seule fois (indicateurs causaux → découpage par préfixe sans lookahead)."""
        df_full = df_full.reset_index(drop=True)
        e_full = enrich(df_full)
        t_full = enrich(_trend_frame(df_full)) if len(df_full) else e_full
        cache["times"] = pd.DatetimeIndex(df_full["time"])
        cache["e"] = e_full
        cache["t"] = t_full
        cache["t_times"] = pd.DatetimeIndex(t_full["time"])
        fn.data_error = None
        if len(df_full) < need:
            fn.data_error = f"données insuffisantes : {len(df_full)} barres {entry_tf} < {need} requises pour la tendance {trend_tf}"
        elif len(df_full):
            n_closed = int(cache["t_times"].searchsorted(_closed_trend_cutoff(df_full["time"].iloc[-1]), side="right"))
            if n_closed < MIN_TREND_BARS:
                fn.data_error = f"données insuffisantes : {n_closed} barres {trend_tf} clôturées < {MIN_TREND_BARS}"

    def _frames(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(entrée enrichie, tendance enrichie clôturée) pour le préfixe ``df`` — depuis le cache si ``df`` en est un préfixe."""
        k = len(df)
        times = cache["times"]
        last_time = df["time"].iloc[-1]
        if times is not None and k <= len(times) and df["time"].iloc[0] == times[0] and last_time == times[k - 1]:
            e_en = cache["e"].iloc[:k]
            t_all, t_times = cache["t"], cache["t_times"]
        else:
            e_en = enrich(df.reset_index(drop=True))
            t_all = enrich(_trend_frame(df.reset_index(drop=True)))
            t_times = pd.DatetimeIndex(t_all["time"])
        n_closed = int(t_times.searchsorted(_closed_trend_cutoff(last_time), side="right"))
        return e_en, t_all.iloc[:n_closed]

    def fn(df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < MIN_ENTRY_BARS:
            return None
        e_en, t_closed = _frames(df)
        if len(t_closed) < MIN_TREND_BARS:
            return None
        e_en = _with_forming_bar(e_en)
        t_en = _with_forming_bar(t_closed)
        reg = classify_regime(t_en) if check_regime else None
        if check_regime and reg.regime.value not in spec.regimes:
            return None
        last_time = df["time"].iloc[-1]
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

    fn.prepare = prepare
    fn.data_error = None
    fn.required_bars = need
    return fn


def make_signal_factory(spec: AgentSpec, symbol_spec: SymbolSpec, entry_tf: str = "M15"):
    return lambda params: make_signal_fn(spec, symbol_spec, entry_tf, params_override=params)
