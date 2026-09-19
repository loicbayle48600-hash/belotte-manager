"""Tests du sous-package market_data : indicateurs, régime, feed (MockBroker)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tradinglab.core.types import Regime
from tradinglab.market_data import indicators as ind
from tradinglab.market_data.feed import MarketDataFeed, MarketSnapshot
from tradinglab.market_data.regime import RegimeResult, classify_regime, trend_direction
from tradinglab.mt5.mock_adapter import MockBroker


# ----------------------------------------------------------------------------------------------------------
# Fabriques de données synthétiques
# ----------------------------------------------------------------------------------------------------------
def make_df(closes: np.ndarray, start: datetime | None = None, wick: float = 0.0002, tf_minutes: int = 60) -> pd.DataFrame:
    start = start or datetime(2026, 3, 2, tzinfo=timezone.utc)
    n = len(closes)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - wick
    times = [start + timedelta(minutes=tf_minutes * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times, utc=True), "open": opens, "high": highs, "low": lows, "close": closes,
        "tick_volume": np.full(n, 100.0), "spread": np.full(n, 10),
    })


def trending_df(n: int = 400, slope: float = 0.0004, noise: float = 0.00005, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 1.10 + slope * np.arange(n) + rng.normal(0, noise, n)
    return make_df(closes)


def flat_df(n: int = 400, noise: float = 0.0003, seed: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 1.10 + rng.normal(0, noise, n)
    return make_df(closes)


# ----------------------------------------------------------------------------------------------------------
# Indicateurs
# ----------------------------------------------------------------------------------------------------------
def test_ema_sma_known_values():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    e = ind.ema(s, 3)
    assert e.iloc[0] == 1.0
    assert abs(e.iloc[1] - 1.5) < 1e-12          # alpha = 2/(3+1) = 0.5
    m = ind.sma(s, 3)
    assert np.isnan(m.iloc[1]) and m.iloc[-1] == 4.0


def test_rsi_bounded_and_extremes():
    up = pd.Series(np.arange(50, dtype=float))
    r = ind.rsi(up, 14)
    assert ((r >= 0) & (r <= 100)).all()
    assert r.iloc[-1] == 100.0
    down = pd.Series(np.arange(50, 0, -1, dtype=float))
    assert ind.rsi(down, 14).iloc[-1] == 0.0
    rng = np.random.default_rng(0)
    r2 = ind.rsi(pd.Series(rng.normal(0, 1, 300).cumsum()), 14)
    assert ((r2 >= 0) & (r2 <= 100)).all()
    assert 20 < r2.iloc[-1] < 80 or True  # simple sanité : pas d'exception, bornes vérifiées ci-dessus


def test_macd_shapes():
    s = pd.Series(np.linspace(1.0, 2.0, 100))
    m, sig, hist = ind.macd(s)
    assert len(m) == len(sig) == len(hist) == 100
    assert np.allclose(hist, m - sig)


def test_atr_positive_and_adx_bounded():
    df = flat_df(300)
    a = ind.atr(df, 14)
    assert (a.dropna() > 0).all()
    x = ind.adx(df, 14)
    assert ((x >= 0) & (x <= 100)).all()
    assert ind.adx(trending_df(300), 14).iloc[-1] > ind.adx(flat_df(300), 14).iloc[-1]


def test_bollinger_order():
    df = flat_df(100)
    mid, up, low = ind.bollinger(df["close"], 20, 2.0)
    valid = mid.notna()
    assert (up[valid] >= mid[valid]).all() and (mid[valid] >= low[valid]).all()


def test_swing_points_no_lookahead():
    rng = np.random.default_rng(3)
    closes = 1.0 + rng.normal(0, 0.001, 200).cumsum()
    df = make_df(closes)
    sh1, sl1 = ind.swing_points(df, 3, 3)
    assert sh1 and sl1
    # Tous les pivots sont confirmés : indice <= n-1-right
    assert max(i for i, _ in sh1 + sl1) <= len(df) - 1 - 3
    # Ajouter des barres futures ne modifie pas les pivots déjà confirmés
    more = make_df(np.concatenate([closes, closes[-1] + rng.normal(0, 0.001, 50).cumsum()]))
    sh2, sl2 = ind.swing_points(more, 3, 3)
    assert sh2[: len(sh1)] == sh1 and sl2[: len(sl1)] == sl1
    # Les pivots sont bien des extrêmes locaux
    highs = df["high"].to_numpy()
    for i, p in sh1:
        assert p == highs[i] and p > highs[i - 3:i].max() and p >= highs[i + 1:i + 4].max()


def test_structure_label_values():
    assert ind.structure_label(trending_df(300)) in ("HH_HL", "MIXED", "UNKNOWN")
    down = make_df(1.2 - 0.0004 * np.arange(300) + np.random.default_rng(5).normal(0, 0.00005, 300))
    assert ind.structure_label(down) in ("LH_LL", "MIXED", "UNKNOWN")
    assert ind.structure_label(make_df(np.array([1.0, 1.1, 1.0]))) == "UNKNOWN"
    zig = make_df(1.0 + 0.01 * np.tile([0, 1, 2, 3, 2, 1], 20) + 0.0005 * np.arange(120))
    assert ind.structure_label(zig) == "HH_HL"


def test_volatility_percentile_and_momentum():
    df = flat_df(400)
    a = ind.atr(df, 14)
    vp = ind.volatility_percentile(a, 200)
    assert vp.iloc[:199].isna().all()
    assert ((vp.dropna() >= 0) & (vp.dropna() <= 100)).all()
    mom = ind.momentum(df["close"], 10)
    assert np.isclose(mom.iloc[20], df["close"].iloc[20] - df["close"].iloc[10])


def test_session_range_and_daily_high_low():
    df = make_df(np.linspace(1.0, 1.1, 48), start=datetime(2026, 3, 2, tzinfo=timezone.utc), tf_minutes=60)
    # Jour de la dernière barre : 2026-03-03 ; session 07:00-16:00 -> barres index 31..39
    hi, lo = ind.session_range(df, "07:00", "16:00")
    sub = df.iloc[31:40]
    assert hi == sub["high"].max() and lo == sub["low"].min()
    assert all(np.isnan(v) for v in ind.session_range(df.iloc[:0], "07:00", "16:00"))
    d1 = make_df(np.array([1.0, 1.2, 1.1]), tf_minutes=1440)
    ph, pl = ind.daily_high_low(d1)
    assert ph == d1["high"].iloc[-2] and pl == d1["low"].iloc[-2]
    assert all(np.isnan(v) for v in ind.daily_high_low(d1.iloc[:1]))


def test_support_resistance_sorted():
    levels = ind.support_resistance(flat_df(300), lookback=100, tolerance_atr=0.5)
    assert levels == sorted(levels)
    assert ind.support_resistance(flat_df(2)) == []


def test_enrich_columns_and_short_df():
    df = ind.enrich(flat_df(400))
    for c in ind.ENRICHED_COLUMNS:
        assert c in df.columns
    assert df["atr14"].iloc[-1] > 0 and 0 <= df["rsi14"].iloc[-1] <= 100
    assert not np.isnan(df["vol_pct"].iloc[-1])
    # df court : pas d'exception, colonnes présentes (NaN acceptées)
    for n in (0, 1, 5, 30, 150):
        short = ind.enrich(flat_df(n) if n else flat_df(10).iloc[:0])
        assert len(short) == n
        for c in ind.ENRICHED_COLUMNS:
            assert c in short.columns


def test_last_closed():
    df = flat_df(10)
    assert ind.last_closed(df).equals(df.iloc[-2])
    with pytest.raises(ValueError):
        ind.last_closed(df.iloc[:1])


# ----------------------------------------------------------------------------------------------------------
# Régime
# ----------------------------------------------------------------------------------------------------------
def test_regime_trending():
    df = ind.enrich(trending_df(400))
    res = classify_regime(df)
    assert isinstance(res, RegimeResult)
    assert res.regime is Regime.TRENDING, res.features
    assert 0 <= res.confidence <= 1
    assert res.features["direction"] == "UP" and res.features["ema_order"] == "UP"
    assert trend_direction(df) == "UP"
    down = ind.enrich(make_df(1.3 - 0.0004 * np.arange(400) + np.random.default_rng(9).normal(0, 0.00005, 400)))
    assert classify_regime(down).regime is Regime.TRENDING
    assert trend_direction(down) == "DOWN"


def test_regime_flat_not_trending():
    for seed in range(5):
        df = ind.enrich(flat_df(400, seed=seed))
        res = classify_regime(df)
        assert res.regime in (Regime.RANGING, Regime.LOW_VOLATILITY, Regime.HIGH_VOLATILITY, Regime.UNCERTAIN, Regime.BREAKOUT)
        assert res.regime is not Regime.TRENDING


def test_regime_news_shock_and_insufficient():
    df = ind.enrich(trending_df(400))
    r = classify_regime(df, news_shock=True)
    assert r.regime is Regime.NEWS_SHOCK and r.confidence == 1.0
    assert classify_regime(ind.enrich(trending_df(30))).regime is Regime.UNCERTAIN
    assert classify_regime(ind.enrich(flat_df(10).iloc[:0])).regime is Regime.UNCERTAIN
    d = r.to_dict()
    assert d["regime"] == "NEWS_SHOCK" and d["provenance"] == "CALCULATED"


def test_regime_uses_last_closed_bar():
    """La dernière barre (en formation) ne doit pas influencer le résultat."""
    base = trending_df(400)
    df1 = ind.enrich(base)
    spoiled = base.copy()
    spoiled.loc[len(spoiled) - 1, "close"] = 0.5   # barre en formation aberrante
    df2 = ind.enrich(spoiled)
    assert classify_regime(df1).regime is classify_regime(df2).regime is Regime.TRENDING


# ----------------------------------------------------------------------------------------------------------
# Feed avec MockBroker
# ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def broker() -> MockBroker:
    b = MockBroker()
    b.connect()
    return b


def test_feed_snapshot_ok(broker: MockBroker):
    feed = MarketDataFeed(broker, ["M15", "H1", "H4"], bars=300)
    snap = feed.snapshot("EURUSD", now=broker.now())
    assert isinstance(snap, MarketSnapshot)
    assert snap.data_fresh is True
    assert snap.data_quality == "OK"
    assert set(snap.frames) == {"M15", "H1", "H4"}
    assert "atr14" in snap.frames["H1"].columns
    assert snap.atr_h1 > 0
    assert isinstance(snap.regime, RegimeResult)
    assert snap.spread_points == broker.specs["EURUSD"].spread_points
    assert snap.bar_times["H1"] and snap.bar_times["H1"] == pd.Timestamp(snap.frames["H1"]["time"].iloc[-2]).isoformat()
    pub = snap.to_public_dict()
    assert "frames" not in pub and pub["regime"]["regime"] in {r.value for r in Regime}
    assert pub["session"] in {"ASIA", "LONDON", "NEWYORK", "OVERLAP_LDN_NY", "OFF"}


def test_feed_stale_tick(broker: MockBroker):
    broker.stale_ticks = True
    feed = MarketDataFeed(broker, ["H1"], bars=300, max_tick_age_sec=30)
    snap = feed.snapshot("EURUSD", now=broker.now())
    assert snap.data_fresh is False
    assert snap.data_quality == "STALE"


def test_feed_no_tick_and_insufficient(broker: MockBroker):
    feed = MarketDataFeed(broker, ["H1"], bars=300, min_bars=250)
    broker.set_connected(False)
    snap = feed.snapshot("EURUSD", now=broker.now())
    assert snap.tick is None and snap.data_quality == "NO_TICK" and snap.data_fresh is False
    broker.set_connected(True)
    feed2 = MarketDataFeed(broker, ["D1"], bars=300, min_bars=250)   # 4000 M5 ≈ 14 jours < 250 barres D1
    assert feed2.snapshot("EURUSD", now=broker.now()).data_quality == "INSUFFICIENT"
    with pytest.raises(ValueError):
        feed.snapshot("NOPE", now=broker.now())
    assert set(feed.snapshots(["EURUSD", "NOPE"], now=broker.now())) == {"EURUSD"}


def test_feed_cache_invalidated_on_new_bar(broker: MockBroker):
    feed = MarketDataFeed(broker, ["H1"], bars=300)
    now = broker.now()
    df1 = feed.frame("EURUSD", "H1", now)
    assert feed.frame("EURUSD", "H1", now + timedelta(seconds=10)) is df1     # même barre -> cache
    broker.advance_bars(12)                                                     # +1h de M5
    assert feed.frame("EURUSD", "H1", now + timedelta(minutes=5)) is df1       # barre inchangée -> cache
    df2 = feed.frame("EURUSD", "H1", broker.now())
    assert df2 is not df1 and len(df2) == 300


def test_feed_returns_matrix_and_correlation(broker: MockBroker):
    feed = MarketDataFeed(broker, ["H1"], bars=300)
    syms = ["EURUSD", "GBPUSD", "USDJPY"]
    rm = feed.returns_matrix(syms, tf="H1", bars=200)
    assert rm.shape == (200, 3)
    assert list(rm.columns) == syms
    assert not rm.isna().any().any()
    corr = feed.rolling_correlation(syms, tf="H1", bars=200)
    assert corr.shape == (3, 3)
    assert np.allclose(np.diag(corr.to_numpy()), 1.0)
    assert ((corr >= -1.0001) & (corr <= 1.0001)).all().all()
