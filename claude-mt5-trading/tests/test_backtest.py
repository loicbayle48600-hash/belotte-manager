"""Tests du moteur de backtest (déterminisme, absence de lookahead, coûts, métriques, validation)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tradinglab.backtest.engine import (BTCosts, BTMetrics, BTTrade, Signal, assert_no_lookahead, compute_metrics,
                                        example_ema_cross_signal, monte_carlo, parameter_sensitivity, run_backtest,
                                        split_in_out_of_sample, stress_test, walk_forward)
from tradinglab.core.types import Side
from tradinglab.mt5.mock_adapter import MockBroker

ZERO_COSTS = BTCosts(spread_points=0, slippage_points=0, commission_per_lot=0.0)
WARMUP = 200


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def h1_df() -> pd.DataFrame:
    """3000 barres H1 synthétiques (36000 M5 resamplées) — reproductibles."""
    b = MockBroker(seed=7, bars=36000, symbols=["EURUSD"])
    assert b.connect()
    df = b.rates("EURUSD", "H1", 3000)
    assert len(df) == 3000
    return df


@pytest.fixture(scope="module")
def h1_short(h1_df: pd.DataFrame) -> pd.DataFrame:
    return h1_df.iloc[:1500].reset_index(drop=True)


def _synthetic_df(closes: list[float]) -> pd.DataFrame:
    """Série contrôlée : open[i] = close[i-1], high/low = enveloppe open/close (pas de mèche)."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return pd.DataFrame({
        "time": pd.to_datetime([start + timedelta(hours=i) for i in range(len(closes))], utc=True),
        "open": opens, "high": np.maximum(opens, closes), "low": np.minimum(opens, closes), "close": closes,
        "tick_volume": 100.0, "spread": 10,
    })


def _one_shot(side: Side, sl_dist: float, tp_dist: float | None, at_len: int = WARMUP + 1):
    """Signal unique émis quand len(df) == at_len (barre index at_len-1)."""
    def fn(df: pd.DataFrame):
        if len(df) != at_len:
            return None
        c = float(df["close"].iloc[-1])
        s = side.sign
        return Signal(side=side, sl=c - s * sl_dist, tp=None if tp_dist is None else c + s * tp_dist, note="t")
    return fn


def _factory(p: dict):
    return example_ema_cross_signal(**p)


# ---------------------------------------------------------------------------
# Déterminisme et lookahead
# ---------------------------------------------------------------------------
def test_determinism_two_runs_identical(h1_short):
    a = run_backtest(h1_short, example_ema_cross_signal(), BTCosts(), {"x": 1}, warmup=WARMUP)
    b = run_backtest(h1_short, example_ema_cross_signal(), BTCosts(), {"x": 1}, warmup=WARMUP)
    assert a.metrics.sample_size > 0
    assert [t.to_dict() for t in a.trades] == [t.to_dict() for t in b.trades]
    assert a.metrics.to_dict() == b.metrics.to_dict()
    assert a.equity_curve_r == b.equity_curve_r


def test_assert_no_lookahead_passes_for_example_signal(h1_short):
    assert assert_no_lookahead(h1_short, example_ema_cross_signal(), samples=20, warmup=WARMUP) is True


def test_assert_no_lookahead_detects_hidden_state(h1_short):
    calls = {"n": 0}

    def stateful(df: pd.DataFrame):
        calls["n"] += 1
        c = float(df["close"].iloc[-1])
        return Signal(Side.BUY, sl=c - 0.001) if calls["n"] % 2 else None

    with pytest.raises(AssertionError):
        assert_no_lookahead(h1_short, stateful, samples=10, warmup=WARMUP)


def test_assert_no_lookahead_detects_mutation(h1_short):
    def mutating(df: pd.DataFrame):
        df["close"] = df["close"] * 1.0001
        return None

    with pytest.raises(AssertionError):
        assert_no_lookahead(h1_short, mutating, samples=5, warmup=WARMUP)


def test_signal_fn_only_sees_closed_bars(h1_short):
    seen: list[int] = []

    def spy(df: pd.DataFrame):
        seen.append(len(df))
        return None

    run_backtest(h1_short, spy, ZERO_COSTS, warmup=WARMUP)
    assert seen[0] == WARMUP + 1
    assert seen == list(range(WARMUP + 1, len(h1_short)))  # jamais la dernière barre, jamais le futur


# ---------------------------------------------------------------------------
# SL / TP / coûts
# ---------------------------------------------------------------------------
def test_sl_hit_gives_exit_reason_sl_and_r_minus_one():
    closes = [1.1000] * (WARMUP + 1) + [1.0995, 1.0990, 1.0980, 1.0975] + [1.0975] * 10
    df = _synthetic_df(closes)
    fn = _one_shot(Side.BUY, sl_dist=0.0010, tp_dist=0.0020)
    res = run_backtest(df, fn, ZERO_COSTS, warmup=WARMUP)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "sl"
    assert t.side is Side.BUY
    assert t.entry == pytest.approx(1.1000)
    assert t.r_multiple == pytest.approx(-1.0)
    assert t.pnl == pytest.approx(-100.0)
    assert t.mae_r == pytest.approx(1.0)

    costs = BTCosts(spread_points=12, slippage_points=3)
    res2 = run_backtest(df, fn, costs, warmup=WARMUP)
    t2 = res2.trades[0]
    assert t2.exit_reason == "sl"
    assert t2.entry > 1.1000                  # spread/2 + slippage défavorables
    assert -1.10 < t2.r_multiple < -1.0       # ≈ -1 moins les coûts (slippage sur le fill SL)


def test_tp_hit_gives_r_equal_rr():
    closes = [1.1000] * (WARMUP + 1) + [1.1005, 1.1012, 1.1025] + [1.1025] * 10
    df = _synthetic_df(closes)
    fn = _one_shot(Side.BUY, sl_dist=0.0010, tp_dist=0.0020)
    t = run_backtest(df, fn, ZERO_COSTS, warmup=WARMUP).trades[0]
    assert t.exit_reason == "tp"
    assert t.r_multiple == pytest.approx(2.0)
    assert t.exit == pytest.approx(1.1020)
    assert t.pnl == pytest.approx(200.0)

    t2 = run_backtest(df, fn, BTCosts(spread_points=12, slippage_points=3), warmup=WARMUP).trades[0]
    assert t2.exit_reason == "tp"
    assert 1.7 < t2.r_multiple < 2.0          # entrée plus chère → R légèrement < rr


def test_sell_side_symmetry():
    closes = [1.1000] * (WARMUP + 1) + [1.0995, 1.0988, 1.0975] + [1.0975] * 5
    df = _synthetic_df(closes)
    t = run_backtest(df, _one_shot(Side.SELL, 0.0010, 0.0020), ZERO_COSTS, warmup=WARMUP).trades[0]
    assert t.side is Side.SELL and t.exit_reason == "tp"
    assert t.r_multiple == pytest.approx(2.0)


def test_sl_priority_when_both_touched_same_bar():
    df = _synthetic_df([1.1000] * (WARMUP + 1) + [1.1000] * 5)
    df.loc[WARMUP + 2, ["high", "low"]] = [1.1030, 1.0985]   # barre englobant SL et TP
    t = run_backtest(df, _one_shot(Side.BUY, 0.0010, 0.0020), ZERO_COSTS, warmup=WARMUP).trades[0]
    assert t.exit_reason == "sl"


def test_end_and_max_bars_held_exits():
    df = _synthetic_df([1.1000] * (WARMUP + 1) + [1.1001, 1.1002, 1.1003, 1.1004, 1.1005])
    fn = _one_shot(Side.BUY, 0.0050, None)
    t = run_backtest(df, fn, ZERO_COSTS, warmup=WARMUP).trades[0]
    assert t.exit_reason == "end" and t.exit == pytest.approx(1.1005)
    t2 = run_backtest(df, fn, ZERO_COSTS, warmup=WARMUP, max_bars_held=2).trades[0]
    assert t2.exit_reason == "signal_exit" and t2.bars_held == 3


def test_exit_fn_and_single_position():
    df = _synthetic_df([1.1000] * (WARMUP + 1) + [1.1001 + 0.0001 * i for i in range(20)])
    always = lambda d: Signal(Side.BUY, sl=float(d["close"].iloc[-1]) - 0.01)  # noqa: E731
    res = run_backtest(df, always, ZERO_COSTS, warmup=WARMUP, exit_fn=lambda d, tr: tr.bars_held >= 3)
    assert all(t.exit_reason == "signal_exit" for t in res.trades)
    for a, b in zip(res.trades, res.trades[1:]):
        assert b.entry_time > a.exit_time  # jamais deux positions simultanées


def test_commission_and_swap_reduce_pnl():
    closes = [1.1000] * (WARMUP + 1) + [1.1005, 1.1012, 1.1025] + [1.1025] * 3
    df = _synthetic_df(closes)
    fn = _one_shot(Side.BUY, 0.0010, 0.0020)
    base = run_backtest(df, fn, ZERO_COSTS, warmup=WARMUP).trades[0]
    with_comm = run_backtest(df, fn, BTCosts(0, 7.0, 0), warmup=WARMUP).trades[0]
    assert with_comm.r_multiple == pytest.approx(base.r_multiple)
    assert with_comm.pnl == pytest.approx(base.pnl - 7.0)  # 1 lot pour 100 points × 1.0 €/tick = 100 €


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def _trade(r: float, bars: int = 5) -> BTTrade:
    return BTTrade(entry_time=None, exit_time=None, side=Side.BUY, entry=1.0, exit=1.0 + r * 0.01, sl=0.99,
                   tp=None, r_multiple=r, pnl=r * 100, mae_r=0.0, mfe_r=0.0, bars_held=bars, exit_reason="tp")


def test_compute_metrics_consistency():
    m = compute_metrics([_trade(2.0), _trade(-1.0), _trade(1.0), _trade(-1.0), _trade(-0.5)])
    assert isinstance(m, BTMetrics)
    assert m.trades == m.sample_size == 5
    assert m.wins == 2 and m.losses == 3
    assert m.win_rate == pytest.approx(0.4)
    assert m.profit_factor == pytest.approx(3.0 / 2.5)
    assert m.expectancy_r == pytest.approx(0.1) and m.avg_r == pytest.approx(0.1)
    assert m.total_r == pytest.approx(0.5)
    assert m.median_r == pytest.approx(-0.5)
    assert m.max_drawdown_r == pytest.approx(1.5)   # pic à 2, creux à 0.5
    assert m.avg_bars_held == 5.0
    assert set(m.to_dict()) >= {"profit_factor", "sharpe", "sortino", "sample_size"}


def test_compute_metrics_edge_cases():
    assert compute_metrics([]).sample_size == 0
    assert compute_metrics([_trade(1.0), _trade(2.0)]).profit_factor == float("inf")


def test_backtest_metrics_match_trades(h1_short):
    res = run_backtest(h1_short, example_ema_cross_signal(), BTCosts(), warmup=WARMUP)
    m = res.metrics
    assert m.sample_size == len(res.trades) > 0
    r = [t.r_multiple for t in res.trades]
    assert m.win_rate == pytest.approx(sum(1 for x in r if x > 0) / len(r))
    assert m.total_r == pytest.approx(sum(r))
    assert res.equity_curve_r[-1] == pytest.approx(m.total_r)
    assert {t.exit_reason for t in res.trades} <= {"sl", "tp", "signal_exit", "end"}


# ---------------------------------------------------------------------------
# Validation : split, walk-forward, Monte Carlo, sensibilité, stress
# ---------------------------------------------------------------------------
def test_split_in_out_of_sample_proportions(h1_df):
    df_is, df_oos = split_in_out_of_sample(h1_df, oos_fraction=0.3)
    assert len(df_is) == 2100 and len(df_oos) == 900
    assert df_is["time"].iloc[-1] < df_oos["time"].iloc[0]
    assert list(df_oos.index) == list(range(900))
    with pytest.raises(ValueError):
        split_in_out_of_sample(h1_df, oos_fraction=1.5)


def test_walk_forward_returns_folds_and_robustness(h1_df):
    grid = [dict(fast=10, slow=30), dict(fast=20, slow=50)]
    wf = walk_forward(h1_df, _factory, grid, BTCosts(), folds=3, warmup=WARMUP)
    assert len(wf.folds) == 3
    for f in wf.folds:
        assert f.params in grid
        assert isinstance(f.is_metrics, BTMetrics) and isinstance(f.oos_metrics, BTMetrics)
        assert f.is_range[1] == f.oos_range[0]
    assert sum(f.oos_metrics.sample_size for f in wf.folds) == len(wf.oos_trades) == wf.oos_metrics.sample_size
    assert isinstance(wf.robustness_ratio, float) and -5.0 <= wf.robustness_ratio <= 5.0
    assert "robustness_ratio" in wf.to_dict()


def test_monte_carlo_keys_and_sanity():
    rng = np.random.default_rng(1)
    r = list(rng.choice([-1.0, 2.0], size=60))
    mc = monte_carlo(r, runs=300, seed=42)
    for k in ("median_total_r", "p05_total_r", "p95_total_r", "median_max_dd_r", "p95_max_dd_r", "prob_negative"):
        assert k in mc
    assert mc["p05_total_r"] <= mc["median_total_r"] <= mc["p95_total_r"]
    assert mc["median_max_dd_r"] <= mc["p95_max_dd_r"]
    assert 0.0 <= mc["prob_negative"] <= 1.0
    assert mc == monte_carlo(r, runs=300, seed=42)   # reproductible
    assert monte_carlo([])["prob_negative"] == 0.0


def test_parameter_sensitivity_returns_stable_bool(h1_short):
    out = parameter_sensitivity(h1_short, _factory, dict(fast=20, slow=50, atr_mult=1.5), jitter_percent=20,
                                costs=BTCosts(), warmup=WARMUP)
    assert isinstance(out["stable"], bool)
    assert set(out["variants"]) == {"fast-20%", "fast+20%", "slow-20%", "slow+20%", "atr_mult-20%", "atr_mult+20%"}
    assert out["variants"]["fast-20%"]["value"] == 16 and out["variants"]["slow+20%"]["value"] == 60
    assert all("expectancy_r" in v for v in out["variants"].values())


def test_stress_test_spread_multipliers(h1_short):
    out = stress_test(h1_short, example_ema_cross_signal(), BTCosts(), spread_multipliers=(1, 2, 3), warmup=WARMUP)
    assert set(out) == {1, 2, 3}
    assert out[1]["expectancy_r"] >= out[2]["expectancy_r"] >= out[3]["expectancy_r"]
