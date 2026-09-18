"""Flux de données de marché : snapshots multi-timeframes enrichis, avec cache par barre et contrôle de fraîcheur.

Le feed est du code déterministe : il lit le broker (`BrokerAdapter`), applique `indicators.enrich` et
`regime.classify_regime`, et produit un `MarketSnapshot` sérialisable (`to_public_dict`, sans DataFrames)
destiné aux agents.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from ..core.clock import bar_open_time, current_session
from ..core.types import Session, SymbolSpec, Tick, utcnow
from ..mt5.adapter import BrokerAdapter
from .indicators import enrich, last_closed
from .regime import RegimeResult, classify_regime

log = logging.getLogger(__name__)

PRIMARY_TF = "H1"
DATA_QUALITY_VALUES = ("OK", "STALE", "INSUFFICIENT", "NO_TICK")


@dataclass
class MarketSnapshot:
    symbol: str
    spec: SymbolSpec
    tick: Optional[Tick]
    frames: dict[str, pd.DataFrame]          # tf -> DataFrame enrichi (dernière ligne = barre en formation)
    regime: RegimeResult
    session: Session
    spread_points: int
    atr_h1: float
    data_fresh: bool
    data_quality: str                        # "OK" | "STALE" | "INSUFFICIENT" | "NO_TICK"
    fetched_at: datetime
    bar_times: dict[str, str]                # tf -> ISO de la dernière barre CLÔTURÉE
    bar_counts: dict[str, int] = field(default_factory=dict)   # tf -> nombre de barres disponibles

    def to_public_dict(self) -> dict:
        """Représentation sérialisable sans les DataFrames (pour les agents / le journal)."""
        return {
            "symbol": self.symbol,
            "spec": {
                "name": self.spec.name, "root": self.spec.root, "digits": self.spec.digits, "point": self.spec.point,
                "asset_class": self.spec.asset_class, "stops_level_points": self.spec.stops_level_points,
                "trade_allowed": self.spec.trade_allowed, "volume_min": self.spec.volume_min,
                "volume_step": self.spec.volume_step,
            },
            "tick": None if self.tick is None else {
                "time": self.tick.time.isoformat(), "bid": self.tick.bid, "ask": self.tick.ask,
                "provenance": self.tick.provenance.value,
            },
            "regime": self.regime.to_dict(),
            "session": self.session.value,
            "spread_points": self.spread_points,
            "atr_h1": self.atr_h1,
            "data_fresh": self.data_fresh,
            "data_quality": self.data_quality,
            "fetched_at": self.fetched_at.isoformat(),
            "bar_times": dict(self.bar_times),
            "bar_counts": dict(self.bar_counts),
            "timeframes": list(self.frames),
        }


class MarketDataFeed:
    """Fournit des snapshots enrichis par symbole avec cache par (symbole, timeframe).

    Le cache d'un (symbole, tf) est invalidé dès qu'une nouvelle barre s'ouvre (`clock.bar_open_time(now, tf)`).
    """

    def __init__(self, broker: BrokerAdapter, timeframes: list[str], bars: int = 300, max_tick_age_sec: int = 30,
                 min_bars: int = 250):
        self.broker = broker
        self.timeframes = [tf.upper() for tf in timeframes]
        self.bars = int(bars)
        self.max_tick_age_sec = int(max_tick_age_sec)
        self.min_bars = int(min_bars)
        self._cache: dict[tuple[str, str], tuple[datetime, pd.DataFrame]] = {}

    # ---------- cache ----------
    def invalidate(self, symbol: Optional[str] = None) -> None:
        """Vide le cache (d'un symbole, ou tout)."""
        if symbol is None:
            self._cache.clear()
        else:
            for k in [k for k in self._cache if k[0] == symbol]:
                del self._cache[k]

    def frame(self, symbol: str, tf: str, now: Optional[datetime] = None) -> pd.DataFrame:
        """DataFrame enrichi (`self.bars` barres) pour (symbol, tf), servi depuis le cache tant que la barre
        courante (`bar_open_time(now, tf)`) est la même que lors du dernier chargement."""
        tf = tf.upper()
        now = now or utcnow()
        cur_bar = bar_open_time(now, tf)
        key = (symbol, tf)
        hit = self._cache.get(key)
        if hit is not None and hit[0] == cur_bar:
            return hit[1]
        raw = self.broker.rates(symbol, tf, self.bars)
        df = enrich(raw.reset_index(drop=True)) if raw is not None else enrich(pd.DataFrame())
        self._cache[key] = (cur_bar, df)
        return df

    # ---------- snapshot ----------
    def _primary_tf(self) -> str:
        return PRIMARY_TF if PRIMARY_TF in self.timeframes else self.timeframes[0]

    def snapshot(self, symbol: str, now: Optional[datetime] = None, news_shock: bool = False) -> MarketSnapshot:
        """Snapshot complet d'un symbole. `now` sert au calcul de l'âge du tick et du cache (défaut : utcnow)."""
        now = now or utcnow()
        spec = self.broker.symbol_info(symbol)
        if spec is None:
            raise ValueError(f"symbole inconnu du broker : {symbol}")
        tick = self.broker.tick(symbol)
        frames: dict[str, pd.DataFrame] = {tf: self.frame(symbol, tf, now) for tf in self.timeframes}
        bar_times: dict[str, str] = {}
        bar_counts: dict[str, int] = {}
        for tf, df in frames.items():
            bar_counts[tf] = int(len(df))
            bar_times[tf] = pd.Timestamp(last_closed(df)["time"]).isoformat() if len(df) >= 2 else ""

        spread_points = tick.spread_points(spec) if tick is not None else int(spec.spread_points)
        primary = self._primary_tf()
        pdf = frames.get(primary, pd.DataFrame())
        atr_h1 = 0.0
        src = frames.get("H1", pdf)
        if len(src) >= 2:
            v = float(last_closed(src)["atr14"])
            atr_h1 = 0.0 if math.isnan(v) else v
        regime = classify_regime(pdf, spread_points=spread_points, point=spec.point, news_shock=news_shock)

        data_fresh = tick is not None and tick.age_seconds(now) <= self.max_tick_age_sec
        if tick is None:
            quality = "NO_TICK"
        elif len(pdf) < self.min_bars:
            quality = "INSUFFICIENT"
        elif not data_fresh:
            quality = "STALE"
        else:
            quality = "OK"
        return MarketSnapshot(
            symbol=symbol, spec=spec, tick=tick, frames=frames, regime=regime, session=current_session(now),
            spread_points=int(spread_points), atr_h1=atr_h1, data_fresh=bool(data_fresh), data_quality=quality,
            fetched_at=now, bar_times=bar_times, bar_counts=bar_counts,
        )

    def snapshots(self, symbols: list[str], now: Optional[datetime] = None, news_shock: bool = False) -> dict[str, MarketSnapshot]:
        """Snapshots pour plusieurs symboles ; un symbole inconnu est journalisé et ignoré."""
        out: dict[str, MarketSnapshot] = {}
        for s in symbols:
            try:
                out[s] = self.snapshot(s, now=now, news_shock=news_shock)
            except ValueError as e:
                log.warning("snapshot ignoré pour %s : %s", s, e)
        return out

    # ---------- corrélations ----------
    def returns_matrix(self, symbols: list[str], tf: str = "H1", bars: int = 200) -> pd.DataFrame:
        """Rendements (close/close_prev - 1) des barres CLÔTURÉES, alignés sur l'horodatage commun.

        Colonnes = symboles, index = time (UTC). La barre en formation est exclue ; jointure interne.
        """
        cols: dict[str, pd.Series] = {}
        for s in symbols:
            raw = self.broker.rates(s, tf, bars + 2)
            if raw is None or len(raw) < 3:
                log.warning("returns_matrix : données insuffisantes pour %s", s)
                continue
            closed = raw.iloc[:-1]
            r = closed["close"].astype(float).pct_change()
            r.index = pd.to_datetime(closed["time"], utc=True)
            cols[s] = r.dropna().tail(bars)
        if not cols:
            return pd.DataFrame(columns=list(symbols))
        return pd.concat(cols, axis=1, join="inner").dropna()

    def rolling_correlation(self, symbols: list[str], tf: str = "H1", bars: int = 200) -> pd.DataFrame:
        """Matrice de corrélation de Pearson des rendements sur la fenêtre des `bars` dernières barres clôturées."""
        rm = self.returns_matrix(symbols, tf=tf, bars=bars)
        if rm.empty:
            return pd.DataFrame(index=list(symbols), columns=list(symbols), dtype=float)
        return rm.corr()
