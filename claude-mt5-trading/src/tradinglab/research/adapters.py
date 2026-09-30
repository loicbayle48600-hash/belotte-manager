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

import numpy as np
import pandas as pd

from ..agents.registry import AgentSpec
from ..agents.screeners import run_screener
from ..backtest.engine import Signal
from ..core.clock import current_session
from ..core.types import Regime, Session, Side, SymbolSpec
from ..market_data.indicators import enrich
from ..market_data.regime import MIN_BARS as MIN_TREND_BARS, classify_regime, regime_series

# 2026-09-30 : H2, W1 (semaine du dimanche, comme MT5) et MN1 (mois calendaire). MN1 ne sert que de tendance :
# jamais assez d'historique mensuel pour entrer (MIN_ENTRY_BARS = 260 mois).
RESAMPLE = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H2": "2h", "H4": "4h", "D1": "1D",
            "W1": "W-SUN", "MN1": "MS"}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H2": 120, "H4": 240, "D1": 1440, "W1": 10080,
              "MN1": 43200}
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


# 2026-09-29 : caches par processus (optimiseur : des dizaines de configurations sur le même historique). Les indicateurs
# et le régime ne dépendent que des données : clé = empreinte des données (taille, dates, octets des prix).
_ENRICH_CACHE: dict = {}
_REGIME_CACHE: dict = {}
_SESSION_CACHE: dict = {}
_CTX_CACHE: dict = {}
_CACHE_MAX = 48


def _data_key(df: pd.DataFrame, tag: str):
    if len(df) == 0:
        return None
    arr = np.ascontiguousarray(df[["open", "high", "low", "close"]].to_numpy(dtype=float))
    return (tag, len(df), str(df["time"].iloc[0]), str(df["time"].iloc[-1]), hash(arr.tobytes()))


def _enrich_cached(df: pd.DataFrame, tag: str) -> pd.DataFrame:
    key = _data_key(df, tag)
    if key is None:
        return enrich(df)
    hit = _ENRICH_CACHE.get(key)
    if hit is None:
        if len(_ENRICH_CACHE) >= _CACHE_MAX:
            _ENRICH_CACHE.pop(next(iter(_ENRICH_CACHE)))
        hit = _ENRICH_CACHE[key] = enrich(df)
    return hit


def _regime_value(t_full: pd.DataFrame, nc: int, tkey=None) -> str:
    """Régime vu par le screener quand `nc` bougies de tendance sont clôturées (= classify_regime sur ce préfixe).
    2026-09-30 : calculé pour TOUTES les bougies en une passe vectorisée (regime_series), mis en cache par données."""
    return _regime_serie(t_full, tkey)[nc - 1]


def _regime_serie(t_full: pd.DataFrame, tkey=None) -> np.ndarray:
    key = tkey if tkey is not None else _data_key(t_full, "regime")
    v = _REGIME_CACHE.get(key)
    if v is None:
        if len(_REGIME_CACHE) >= _CACHE_MAX:
            _REGIME_CACHE.pop(next(iter(_REGIME_CACHE)))
        v = _REGIME_CACHE[key] = regime_series(t_full)
    return v


def _with_forming_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Le screener ignore la dernière ligne (barre « en formation ») : on duplique la dernière barre clôturée."""
    return pd.concat([df, df.iloc[[-1]]], ignore_index=True)


def make_signal_fn(spec: AgentSpec, symbol_spec: SymbolSpec, entry_tf: str = "M15",
                   check_regime: bool = True, params_override: Optional[dict] = None,
                   fast: Optional[bool] = None) -> Callable[[pd.DataFrame], Optional[Signal]]:
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
    # 2026-09-29 : jumeau vectorisé (backtest/fastsig.py) si la stratégie en a un et que l'équivalence est prouvée
    from ..backtest import fastsig as _fs
    fast_name = spec.strategy if spec.strategy in _fs.FAST else (spec.base_strategy if getattr(spec, "base_strategy", None) in _fs.FAST else None)
    use_fast = (fast if fast is not None else True) and fast_name is not None         and str((spec.params or {}).get("entry_kind", "MARKET") or "MARKET").upper() == "MARKET"

    def _trend_frame(df: pd.DataFrame) -> pd.DataFrame:
        return resample(df, trend_tf) if trend_tf != entry_tf else df

    def _trend_closes(t_times: pd.DatetimeIndex) -> pd.DatetimeIndex:
        """Clôture de chaque barre de tendance. Durée fixe, sauf MN1 (2026-09-30) : un mois calendaire (28 à 31 jours),
        une durée fixe de 30 jours ferait lire la clôture d'un mois de 31 jours un jour trop tôt (lookahead)."""
        if str(trend_tf).upper() == "MN1":
            return t_times + pd.offsets.MonthBegin(1)
        return t_times + trend_delta

    def _n_closed(t_times: pd.DatetimeIndex, last_time, closes=None) -> int:
        # une barre de tendance est clôturée si sa clôture <= clôture de la barre d'entrée (L + entry_delta)
        closes = _trend_closes(t_times) if closes is None else closes
        return int(closes.searchsorted(pd.Timestamp(last_time) + entry_delta, side="right"))

    def prepare(df_full: pd.DataFrame) -> None:
        """Pré-calcule les indicateurs une seule fois (indicateurs causaux → découpage par préfixe sans lookahead)."""
        df_full = df_full.reset_index(drop=True)
        e_full = _enrich_cached(df_full, "e")
        t_full = _enrich_cached(_trend_frame(df_full), "t_" + trend_tf) if len(df_full) else e_full
        cache["times"] = pd.DatetimeIndex(df_full["time"])
        cache["e"] = e_full
        cache["t"] = t_full
        cache["t_times"] = pd.DatetimeIndex(t_full["time"])
        cache["t_closes"] = _trend_closes(cache["t_times"])
        cache["gen"] = cache.get("gen", 0) + 1          # nouveau jeu de données : le cache de tendance repart à zéro
        cache["fast"] = _prepare_fast(e_full, t_full) if (use_fast and len(df_full)) else None
        fn.data_error = None
        if len(df_full) < need:
            fn.data_error = f"données insuffisantes : {len(df_full)} barres {entry_tf} < {need} requises pour la tendance {trend_tf}"
        elif len(df_full):
            n_closed = _n_closed(cache["t_times"], df_full["time"].iloc[-1], cache["t_closes"])
            if n_closed < MIN_TREND_BARS:
                fn.data_error = f"données insuffisantes : {n_closed} barres {trend_tf} clôturées < {MIN_TREND_BARS}"

    def _frames(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(entrée enrichie, tendance enrichie clôturée) pour le préfixe ``df`` — depuis le cache si ``df`` en est un préfixe."""
        k = len(df)
        times = cache["times"]
        last_time = df["time"].iloc[-1]
        if times is not None and k <= len(times) and df["time"].iloc[0] == times[0] and last_time == times[k - 1]:
            e_en = cache["e"].iloc[:k]
            t_all, t_times, closes = cache["t"], cache["t_times"], cache.get("t_closes")
        else:
            e_en = enrich(df.reset_index(drop=True))
            t_all = enrich(_trend_frame(df.reset_index(drop=True)))
            t_times, closes = pd.DatetimeIndex(t_all["time"]), None
        n_closed = _n_closed(t_times, last_time, closes)
        return e_en, t_all.iloc[:n_closed]

    def _build_ctx(e_full: pd.DataFrame, t_full: pd.DataFrame):

        times = cache["times"]
        n = len(e_full)
        closes = cache.get("t_closes")
        if closes is None:
            closes = _trend_closes(cache["t_times"])
        nc = np.asarray(closes.searchsorted(pd.DatetimeIndex(times) + entry_delta, side="right"), dtype=np.int64)
        i = np.arange(n)
        skey = _data_key(e_full, "sess")
        vals = _SESSION_CACHE.get(skey)
        if vals is None:                           # session de chaque bougie : même calcul pour toutes les configurations
            vals = np.array([current_session(ts.to_pydatetime()).value for ts in times], dtype=object)
            if len(_SESSION_CACHE) >= _CACHE_MAX:
                _SESSION_CACHE.pop(next(iter(_SESSION_CACHE)))
            _SESSION_CACHE[skey] = vals
        sess_ok = np.isin(vals, list(spec.sessions)) | (vals == Session.OFF.value)
        base = (i + 1 >= MIN_ENTRY_BARS) & sess_ok & (nc >= MIN_TREND_BARS) & (nc + 1 >= 60) & (i + 2 >= 60)
        if check_regime:
            serie = _regime_serie(t_full, _data_key(t_full, "regime"))
            ok_t = np.isin(serie, list(spec.regimes))
            base &= (nc >= 1) & ok_t[np.clip(nc - 1, 0, len(ok_t) - 1)]
        ctx = _fs.FastCtx(e=e_full, t=t_full, lt_idx=nc - 1, base=base)
        for c in _fs.LE_COLS:
            base &= ~np.isnan(ctx.col(c))
        for c in _fs.LT_COLS:
            base &= ~np.isnan(ctx.tcol(c))
        base &= ctx.col("atr14") > 0
        ctx.base = base
        return ctx

    def _prepare_fast(e_full: pd.DataFrame, t_full: pd.DataFrame):
        """Signaux de toutes les bougies en une fois — mêmes filtres, dans le même ordre, que `fn` ci-dessous.
        Le contexte (sessions, régime, colonnes alignées, pivots) ne dépend que des données et des filtres de l'agent :
        il est partagé entre toutes les configurations d'un même marché (2026-09-29, simulation en masse)."""
        ckey = (_data_key(e_full, "ctx"), entry_tf, trend_tf, tuple(sorted(spec.sessions)), tuple(sorted(spec.regimes)), check_regime)
        ctx = _CTX_CACHE.get(ckey)
        if ctx is None:
            ctx = _build_ctx(e_full, t_full)
            if len(_CTX_CACHE) >= _CACHE_MAX:
                _CTX_CACHE.pop(next(iter(_CTX_CACHE)))
            _CTX_CACHE[ckey] = ctx
        cache["ctx"] = ctx                               # exposé pour le calcul par paquets sur la carte (massive)
        side, sl, tp = _fs.FAST[fast_name](ctx, dict(spec.params or {}))
        return side, sl, tp

    def fn(df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < MIN_ENTRY_BARS:
            return None
        fa = cache.get("fast")
        if fa is not None:
            k = len(df)
            times = cache["times"]
            if k <= len(times) and df["time"].iloc[0] == times[0] and df["time"].iloc[-1] == times[k - 1]:
                sd = int(fa[0][k - 1])
                if sd == 0:
                    return None
                return Signal(side=Side.BUY if sd > 0 else Side.SELL, sl=float(fa[1][k - 1]), tp=float(fa[2][k - 1]), note=spec.agent_id)
        # 2026-09-29 (accélération, résultat identique) : la session ne dépend que de l'heure → testée en premier ;
        # le cadre de tendance et son régime ne changent qu'à la clôture d'une barre de tendance → mis en cache
        last_time = df["time"].iloc[-1]
        ts = last_time.to_pydatetime() if hasattr(last_time, "to_pydatetime") else datetime.now(timezone.utc)
        sess = current_session(ts)
        if sess.value not in spec.sessions and sess is not Session.OFF:
            return None
        e_en, t_closed = _frames(df)
        if len(t_closed) < MIN_TREND_BARS:
            return None
        cle = (cache.get("gen"), len(t_closed), t_closed["time"].iloc[-1])
        if cache.get("t_key") == cle:
            t_en, reg = cache["t_en"], cache["t_reg"]
        else:
            t_en = _with_forming_bar(t_closed)
            reg = classify_regime(t_en) if check_regime else None
            cache["t_key"], cache["t_en"], cache["t_reg"] = cle, t_en, reg
        if check_regime and reg.regime.value not in spec.regimes:
            return None
        e_en = _with_forming_bar(e_en)
        atr = float(e_en["atr14"].iloc[-2]) if pd.notna(e_en["atr14"].iloc[-2]) else 0.0
        snap = SimpleNamespace(symbol=symbol_spec.name, spec=symbol_spec, frames={entry_tf: e_en, trend_tf: t_en},
                               regime=reg or SimpleNamespace(regime=Regime.UNCERTAIN), session=sess, spread_points=symbol_spec.spread_points,
                               atr_h1=atr, data_quality="OK", tick=None)
        c = run_screener(spec, snap)
        if c is None:
            return None
        return Signal(side=c.side, sl=c.sl, tp=c.tp_plan[-1] if c.tp_plan else None, note=c.agent_id,
                      entry_kind=str(getattr(c, "entry_kind", "MARKET") or "MARKET"),
                      order_price=(float(c.order_price) if getattr(c, "order_price", 0.0) else None),
                      expiry_bars=int(getattr(c, "expiry_bars", 0) or 3))

    def signal_at(i: int):
        """Signal précalculé de la bougie i du jeu préparé, ou `engine._NO_FAST` s'il n'y en a pas."""
        from ..backtest.engine import _NO_FAST
        fa = cache.get("fast")
        if fa is None or i < MIN_ENTRY_BARS - 1:
            return _NO_FAST if fa is None else None
        sd = int(fa[0][i])
        if sd == 0:
            return None
        return Signal(side=Side.BUY if sd > 0 else Side.SELL, sl=float(fa[1][i]), tp=float(fa[2][i]), note=spec.agent_id)

    fn.signal_at = signal_at
    fn.fast_arrays = lambda: cache.get("fast")
    fn.fast_ctx = lambda: cache.get("ctx")      # (side, sl, tp) de chaque bougie, pour la simulation en masse
    fn.prepare = prepare
    fn.data_error = None
    fn.required_bars = need
    return fn


def make_signal_factory(spec: AgentSpec, symbol_spec: SymbolSpec, entry_tf: str = "M15"):
    return lambda params: make_signal_fn(spec, symbol_spec, entry_tf, params_override=params)
