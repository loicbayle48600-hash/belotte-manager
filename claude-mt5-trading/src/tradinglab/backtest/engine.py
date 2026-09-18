"""Moteur de backtest reproductible, barre par barre, sans lookahead.

Principes :
- À chaque barre i (>= warmup), ``signal_fn`` ne reçoit QUE ``df.iloc[:i+1]`` (barres clôturées jusqu'à i).
- Un signal à la barre i est exécuté à l'open de la barre i+1, avec spread/2 + slippage dans le sens défavorable.
- SL/TP sont vérifiés sur high/low des barres suivantes (barre d'entrée incluse). Si SL et TP sont touchés dans
  la même barre, le SL est prioritaire (hypothèse conservatrice).
- Une seule position à la fois.
- r_multiple = (exit - entry) / (entry - sl) : positif si gain, quel que soit le sens.
- pnl en monnaie = r_multiple * risk_money - commission - swap.
- Les barres sont traitées comme des prix "mid" : le spread est payé à l'entrée et à la sortie discrétionnaire ;
  les fills SL subissent le slippage, les fills TP sont supposés exacts (ordre limite).

Toutes les métriques de type Sharpe/Sortino sont INDICATIVES (calculées sur les R par trade, mises à l'échelle
par sqrt(n)) et ne constituent pas une annualisation rigoureuse.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from ..core.types import Side

REQUIRED_COLUMNS = ("time", "open", "high", "low", "close")

SignalFn = Callable[[pd.DataFrame], Optional["Signal"]]
ExitFn = Callable[[pd.DataFrame, "BTTrade"], bool]
SignalFactory = Callable[[dict], SignalFn]


# ---------------------------------------------------------------------------
# Structures de données
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    """Signal d'entrée : sens, stop-loss et take-profit en prix ABSOLUS."""
    side: Side
    sl: float
    tp: float | None = None
    note: str = ""


@dataclass
class BTTrade:
    """Trade simulé. ``mae_r``/``mfe_r`` sont des magnitudes (>= 0) en R sur les barres tenues.

    ``bars_held`` = nombre de barres de l'entrée (incluse) à la sortie (incluse).
    ``exit_reason`` ∈ {"sl", "tp", "signal_exit", "end"} ; "signal_exit" couvre ``exit_fn`` et ``max_bars_held``.
    Pendant la vie du trade (passage à ``exit_fn``), ``exit_time``/``exit`` valent None et ``exit_reason`` "".
    """
    entry_time: Any
    exit_time: Any
    side: Side
    entry: float
    exit: Optional[float]
    sl: float
    tp: Optional[float]
    r_multiple: float
    pnl: float
    mae_r: float
    mfe_r: float
    bars_held: int
    exit_reason: str
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        for k in ("entry_time", "exit_time"):
            v = d[k]
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        return d


@dataclass
class BTMetrics:
    """Métriques agrégées en R. ``sharpe``/``sortino`` sont indicatifs (moyenne/écart-type des R × sqrt(n))."""
    trades: int = 0
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
    avg_bars_held: float = 0.0
    sample_size: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BTResult:
    trades: list[BTTrade]
    metrics: BTMetrics
    equity_curve_r: list[float]
    params: dict
    rejected_signals: int = 0

    def to_dict(self) -> dict:
        return {
            "trades": [t.to_dict() for t in self.trades],
            "metrics": self.metrics.to_dict(),
            "equity_curve_r": list(self.equity_curve_r),
            "params": dict(self.params),
            "rejected_signals": self.rejected_signals,
        }


@dataclass
class BTCosts:
    """Coûts de transaction. ``swap_per_day_points`` positif = débit par jour calendaire tenu."""
    spread_points: int = 12
    commission_per_lot: float = 0.0     # aller-retour, par lot
    slippage_points: int = 3
    point: float = 1e-5
    tick_value: float = 1.0             # valeur d'un tick pour 1 lot
    tick_size: float = 1e-5
    swap_per_day_points: float = 0.0

    @property
    def spread(self) -> float:
        return self.spread_points * self.point

    @property
    def slippage(self) -> float:
        return self.slippage_points * self.point


@dataclass
class WalkForwardFold:
    fold: int
    params: dict
    is_metrics: BTMetrics
    oos_metrics: BTMetrics
    is_range: tuple[int, int]
    oos_range: tuple[int, int]
    qualified: bool = True   # False si aucun jeu de params n'a atteint min_trades en in-sample

    def to_dict(self) -> dict:
        return {
            "fold": self.fold, "params": dict(self.params), "is_metrics": self.is_metrics.to_dict(),
            "oos_metrics": self.oos_metrics.to_dict(), "is_range": list(self.is_range),
            "oos_range": list(self.oos_range), "qualified": self.qualified,
        }


@dataclass
class WalkForwardResult:
    folds: list[WalkForwardFold]
    oos_trades: list[BTTrade]
    oos_metrics: BTMetrics
    robustness_ratio: float
    is_expectancy_r: float = 0.0
    oos_expectancy_r: float = 0.0

    def to_dict(self) -> dict:
        return {
            "folds": [f.to_dict() for f in self.folds],
            "oos_trades": [t.to_dict() for t in self.oos_trades],
            "oos_metrics": self.oos_metrics.to_dict(),
            "robustness_ratio": self.robustness_ratio,
            "is_expectancy_r": self.is_expectancy_r,
            "oos_expectancy_r": self.oos_expectancy_r,
        }


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def _check_df(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"colonnes manquantes dans df : {missing}")
    return df.reset_index(drop=True)


def _days_between(a: Any, b: Any) -> int:
    """Nombre de jours calendaires entiers entre deux timestamps (0 si non datetime)."""
    try:
        delta = pd.Timestamp(b) - pd.Timestamp(a)
        return max(0, int(delta.total_seconds() // 86400))
    except Exception:  # noqa: BLE001 — timestamps non comparables : pas de swap
        return 0


def _lots_for_risk(risk_money: float, risk_distance: float, costs: BTCosts) -> float:
    """Taille de position (lots) telle que la perte au SL == risk_money."""
    if risk_distance <= 0 or costs.tick_size <= 0 or costs.tick_value <= 0:
        return 0.0
    return risk_money / (risk_distance / costs.tick_size * costs.tick_value)


def _signal_is_valid(sig: Signal, entry: float) -> bool:
    if not isinstance(sig, Signal) or sig.side not in (Side.BUY, Side.SELL):
        return False
    if sig.sl is None or not math.isfinite(sig.sl):
        return False
    if sig.side is Side.BUY:
        if sig.sl >= entry:
            return False
        if sig.tp is not None and sig.tp <= entry:
            return False
    else:
        if sig.sl <= entry:
            return False
        if sig.tp is not None and sig.tp >= entry:
            return False
    return True


# ---------------------------------------------------------------------------
# Moteur
# ---------------------------------------------------------------------------
def run_backtest(df: pd.DataFrame, signal_fn: SignalFn, costs: BTCosts, params: dict | None = None,
                 risk_money: float = 100.0, max_bars_held: int | None = None, exit_fn: ExitFn | None = None,
                 warmup: int = 200) -> BTResult:
    """Exécute le backtest barre par barre (voir docstring du module pour les règles).

    ``exit_fn(df.iloc[:i+1], trade_en_cours)`` retournant True à la clôture de la barre i ferme la position
    à l'open de la barre i+1 (spread/2 + slippage défavorables). ``max_bars_held`` = k ferme de même à l'open
    de la barre suivant la k-ième barre tenue. Une position encore ouverte à la dernière barre est fermée au
    close de celle-ci (``exit_reason`` = "end").
    """
    df = _check_df(df)
    n = len(df)
    params = dict(params or {})
    opens = df["open"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    times = df["time"].tolist()

    trades: list[BTTrade] = []
    equity: list[float] = []
    cum_r = 0.0
    rejected = 0

    pending: Optional[Signal] = None   # signal à exécuter à l'open de la barre courante
    pos: Optional[dict] = None         # position ouverte
    pending_exit = False               # sortie discrétionnaire à l'open de la barre courante

    def _close(exit_idx: int, exit_price: float, reason: str, exit_time: Any) -> None:
        nonlocal pos, cum_r
        p = pos
        entry = p["entry"]
        risk = p["risk"]
        r = (exit_price - entry) / (entry - p["sl"])
        sign = p["side"].sign
        # excursions : barres tenues avant la barre de sortie + prix de sortie
        fav = max(p["mfe"], sign * (exit_price - entry))
        adv = max(p["mae"], -sign * (exit_price - entry))
        lots = _lots_for_risk(risk_money, risk, costs)
        commission = costs.commission_per_lot * lots
        days = _days_between(p["entry_time"], exit_time)
        swap = 0.0
        if costs.swap_per_day_points and days > 0 and costs.tick_size > 0:
            swap = costs.swap_per_day_points * costs.point / costs.tick_size * costs.tick_value * lots * days
        pnl = r * risk_money - commission - swap
        trades.append(BTTrade(
            entry_time=p["entry_time"], exit_time=exit_time, side=p["side"], entry=entry, exit=exit_price,
            sl=p["sl"], tp=p["tp"], r_multiple=float(r), pnl=float(pnl), mae_r=float(max(0.0, adv) / risk),
            mfe_r=float(max(0.0, fav) / risk), bars_held=int(exit_idx - p["entry_idx"] + 1), exit_reason=reason,
            note=p["note"],
        ))
        cum_r += float(r)
        equity.append(cum_r)
        pos = None

    for i in range(n):
        # 1) exécution d'un signal en attente à l'open de la barre i
        if pending is not None:
            sign = pending.side.sign
            entry = opens[i] + sign * (costs.spread / 2.0 + costs.slippage)
            if _signal_is_valid(pending, entry):
                pos = {
                    "side": pending.side, "entry": float(entry), "sl": float(pending.sl),
                    "tp": None if pending.tp is None else float(pending.tp), "entry_idx": i,
                    "entry_time": times[i], "risk": float(abs(entry - pending.sl)), "mae": 0.0, "mfe": 0.0,
                    "note": pending.note,
                }
            else:
                rejected += 1
            pending = None
            pending_exit = False

        # 2) gestion de la position ouverte sur la barre i
        if pos is not None:
            sign = pos["side"].sign
            if pending_exit and i > pos["entry_idx"]:
                exit_price = opens[i] - sign * (costs.spread / 2.0 + costs.slippage)
                _close(i, float(exit_price), "signal_exit", times[i])
                pending_exit = False
            else:
                sl, tp = pos["sl"], pos["tp"]
                hit_sl = lows[i] <= sl if sign > 0 else highs[i] >= sl
                hit_tp = tp is not None and (highs[i] >= tp if sign > 0 else lows[i] <= tp)
                if hit_sl:  # SL prioritaire (conservateur), slippage défavorable
                    _close(i, float(sl - sign * costs.slippage), "sl", times[i])
                elif hit_tp:
                    _close(i, float(tp), "tp", times[i])
                else:
                    pos["mfe"] = max(pos["mfe"], sign * ((highs[i] if sign > 0 else lows[i]) - pos["entry"]))
                    pos["mae"] = max(pos["mae"], -sign * ((lows[i] if sign > 0 else highs[i]) - pos["entry"]))
                    bars_held = i - pos["entry_idx"] + 1
                    if i == n - 1:
                        _close(i, float(closes[i]), "end", times[i])
                    elif max_bars_held is not None and bars_held >= max_bars_held:
                        pending_exit = True
                    elif exit_fn is not None:
                        snapshot = BTTrade(
                            entry_time=pos["entry_time"], exit_time=None, side=pos["side"], entry=pos["entry"],
                            exit=None, sl=sl, tp=tp,
                            r_multiple=float(sign * (closes[i] - pos["entry"]) / pos["risk"]), pnl=0.0,
                            mae_r=float(pos["mae"] / pos["risk"]), mfe_r=float(pos["mfe"] / pos["risk"]),
                            bars_held=bars_held, exit_reason="", note=pos["note"],
                        )
                        if bool(exit_fn(df.iloc[: i + 1], snapshot)):
                            pending_exit = True

        # 3) recherche d'un signal à la clôture de la barre i (uniquement barres <= i visibles)
        if pos is None and i >= warmup and i < n - 1:
            sig = signal_fn(df.iloc[: i + 1])
            if sig is not None:
                if isinstance(sig, Signal):
                    pending = sig
                else:
                    rejected += 1

    return BTResult(trades=trades, metrics=compute_metrics(trades), equity_curve_r=equity, params=params,
                    rejected_signals=rejected)


def _signal_key(sig: Any) -> Any:
    if sig is None:
        return None
    if isinstance(sig, Signal):
        return (sig.side.value, round(float(sig.sl), 12), None if sig.tp is None else round(float(sig.tp), 12), sig.note)
    return repr(sig)


def _fake_future(df: pd.DataFrame, n_bars: int, rng: np.random.Generator) -> pd.DataFrame:
    """Barres futures fictives (choc marqué) pour détecter toute fuite du futur."""
    last = df.iloc[-1]
    px = float(last["close"])
    scale = max(abs(px) * 0.02, 1e-9)
    times = pd.to_datetime(df["time"])
    step = (times.iloc[-1] - times.iloc[-2]) if len(times) > 1 else pd.Timedelta(hours=1)
    rows = []
    for k in range(1, n_bars + 1):
        o = px
        c = px + rng.normal(0, scale) * (1 if k % 2 else -1)
        rows.append({
            "time": times.iloc[-1] + step * k, "open": o, "high": max(o, c) + scale, "low": min(o, c) - scale,
            "close": c, "tick_volume": 1.0, "spread": 10,
        })
        px = c
    fut = pd.DataFrame(rows)
    for col in df.columns:
        if col not in fut.columns:
            fut[col] = df[col].iloc[-1]
    return fut[list(df.columns)]


def assert_no_lookahead(df: pd.DataFrame, signal_fn: SignalFn, samples: int = 20, warmup: int = 200,
                        seed: int = 0) -> bool:
    """Garde-fou anti-lookahead / anti-état caché.

    Pour ``samples`` barres i (ordre aléatoire puis séquentiel), vérifie que ``signal_fn(df.iloc[:i+1])`` est
    identique à l'appel sur ``df.iloc[:i+1]`` recopié après ajout de barres futures fictives (concat puis
    troncature), que deux appels successifs donnent le même résultat, et que l'entrée n'est pas mutée.
    Lève AssertionError en cas d'écart ; retourne True sinon.
    """
    df = _check_df(df)
    n = len(df)
    if n < 3:
        raise ValueError("df trop court pour assert_no_lookahead")
    lo = min(max(warmup, 2), n - 2)
    idx = np.unique(np.linspace(lo, n - 2, num=max(1, min(samples, n - 1 - lo)), dtype=int))
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(idx))
    reference: dict[int, Any] = {}
    for j in order:  # ordre aléatoire : un état caché dépendant de l'ordre d'appel serait détecté au 2e passage
        i = int(idx[j])
        base = df.iloc[: i + 1].copy()
        snapshot = base.copy()
        a = signal_fn(base)
        pd.testing.assert_frame_equal(base, snapshot, check_dtype=False)
        extended = pd.concat([df.iloc[: i + 1], _fake_future(df.iloc[: i + 1], 50, rng)], ignore_index=True)
        b = signal_fn(extended.iloc[: i + 1].copy())
        c = signal_fn(base.copy())
        ka, kb, kc = _signal_key(a), _signal_key(b), _signal_key(c)
        if ka != kb:
            raise AssertionError(f"lookahead suspecté à la barre {i} : {a!r} vs futur fictif {b!r}")
        if ka != kc:
            raise AssertionError(f"signal non déterministe à la barre {i} : {a!r} vs {c!r}")
        reference[i] = ka
    for i in sorted(reference):  # passage séquentiel
        k = _signal_key(signal_fn(df.iloc[: i + 1].copy()))
        if k != reference[i]:
            raise AssertionError(f"signal dépendant de l'ordre d'appel à la barre {i}")
    return True


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def _max_drawdown(equity: np.ndarray) -> float:
    if equity.size == 0:
        return 0.0
    curve = np.concatenate([[0.0], equity])
    peaks = np.maximum.accumulate(curve)
    return float(np.max(peaks - curve))


def compute_metrics(trades: list[BTTrade]) -> BTMetrics:
    """Métriques en R. PF = somme des gains / somme des pertes (inf si aucune perte).

    Sharpe/Sortino = moyenne(R) / écart-type(R ou R négatifs) × sqrt(n) : INDICATIF (échelle « par trade »,
    pas une annualisation calendaire).
    """
    if not trades:
        return BTMetrics()
    r = np.array([t.r_multiple for t in trades], dtype=float)
    n = int(r.size)
    gains = r[r > 0]
    losses = r[r < 0]
    wins = int(gains.size)
    nloss = int(losses.size)
    sum_g = float(gains.sum())
    sum_l = float(-losses.sum())
    if sum_l > 0:
        pf = sum_g / sum_l
    else:
        pf = float("inf") if sum_g > 0 else 0.0
    mean = float(r.mean())
    std = float(r.std(ddof=1)) if n > 1 else 0.0
    sharpe = mean / std * math.sqrt(n) if std > 1e-12 else 0.0
    down = np.minimum(r, 0.0)
    dstd = float(np.sqrt(np.mean(down ** 2)))
    sortino = mean / dstd * math.sqrt(n) if dstd > 1e-12 else 0.0
    return BTMetrics(
        trades=n, wins=wins, losses=nloss, win_rate=wins / n, profit_factor=float(pf), expectancy_r=mean,
        avg_r=mean, median_r=float(np.median(r)), max_drawdown_r=_max_drawdown(np.cumsum(r)),
        sharpe=float(sharpe), sortino=float(sortino), total_r=float(r.sum()),
        avg_bars_held=float(np.mean([t.bars_held for t in trades])), sample_size=n,
    )


# ---------------------------------------------------------------------------
# Validation : IS/OOS, walk-forward, Monte Carlo, sensibilité, stress
# ---------------------------------------------------------------------------
def split_in_out_of_sample(df: pd.DataFrame, oos_fraction: float = 0.3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Coupe chronologique : les premières (1 - oos_fraction) barres en IS, le reste en OOS."""
    if not 0.0 < oos_fraction < 1.0:
        raise ValueError("oos_fraction doit être dans ]0, 1[")
    df = _check_df(df)
    cut = int(round(len(df) * (1.0 - oos_fraction)))
    return df.iloc[:cut].reset_index(drop=True), df.iloc[cut:].reset_index(drop=True)


def _bounded(x: float, lo: float = -5.0, hi: float = 5.0) -> float:
    if not math.isfinite(x):
        return hi if x > 0 else lo
    return max(lo, min(hi, x))


def walk_forward(df: pd.DataFrame, signal_factory: SignalFactory, param_grid: list[dict], costs: BTCosts,
                 folds: int = 4, warmup: int = 200, anchored: bool = True, min_trades: int = 10,
                 risk_money: float = 100.0, max_bars_held: int | None = None) -> WalkForwardResult:
    """Walk-forward : le df est découpé en ``folds + 1`` segments égaux.

    Fold k : IS = segments 0..k (anchored) ou segment k (rolling) ; OOS = segment k+1. Sur l'IS, on retient les
    params à meilleure ``expectancy_r`` parmi ceux ayant ``sample_size >= min_trades`` (sinon meilleure expectancy
    tout court, fold marqué ``qualified=False``). L'OOS reçoit ``warmup`` barres de préchauffage prises AVANT son
    début (aucun trade n'y est pris) pour ne pas perdre de barres testables.
    ``robustness_ratio`` = expectancy OOS globale / expectancy IS (pondérée par le nombre de trades), borné à [-5, 5]
    (0 si l'expectancy IS est <= 0).
    """
    if folds < 1:
        raise ValueError("folds >= 1 requis")
    if not param_grid:
        raise ValueError("param_grid vide")
    df = _check_df(df)
    n = len(df)
    edges = np.linspace(0, n, folds + 2, dtype=int)
    fold_results: list[WalkForwardFold] = []
    oos_trades: list[BTTrade] = []
    is_weighted = 0.0
    is_count = 0
    for k in range(folds):
        is_start = 0 if anchored else int(edges[k])
        is_end = int(edges[k + 1])
        oos_start, oos_end = int(edges[k + 1]), int(edges[k + 2])
        df_is = df.iloc[is_start:is_end].reset_index(drop=True)
        best: Optional[tuple[dict, BTMetrics]] = None
        fallback: Optional[tuple[dict, BTMetrics]] = None
        for params in param_grid:
            m = run_backtest(df_is, signal_factory(params), costs, params, risk_money=risk_money,
                             max_bars_held=max_bars_held, warmup=warmup).metrics
            if fallback is None or m.expectancy_r > fallback[1].expectancy_r:
                fallback = (params, m)
            if m.sample_size >= min_trades and (best is None or m.expectancy_r > best[1].expectancy_r):
                best = (params, m)
        qualified = best is not None
        chosen_params, is_metrics = best if best is not None else fallback  # type: ignore[misc]
        lead = max(0, oos_start - warmup)
        df_oos = df.iloc[lead:oos_end].reset_index(drop=True)
        oos_res = run_backtest(df_oos, signal_factory(chosen_params), costs, chosen_params, risk_money=risk_money,
                               max_bars_held=max_bars_held, warmup=oos_start - lead)
        fold_results.append(WalkForwardFold(fold=k, params=dict(chosen_params), is_metrics=is_metrics,
                                            oos_metrics=oos_res.metrics, is_range=(is_start, is_end),
                                            oos_range=(oos_start, oos_end), qualified=qualified))
        oos_trades.extend(oos_res.trades)
        is_weighted += is_metrics.expectancy_r * is_metrics.sample_size
        is_count += is_metrics.sample_size
    oos_metrics = compute_metrics(oos_trades)
    is_exp = is_weighted / is_count if is_count else 0.0
    oos_exp = oos_metrics.expectancy_r
    ratio = _bounded(oos_exp / is_exp) if is_exp > 1e-12 else 0.0
    return WalkForwardResult(folds=fold_results, oos_trades=oos_trades, oos_metrics=oos_metrics,
                             robustness_ratio=ratio, is_expectancy_r=is_exp, oos_expectancy_r=oos_exp)


def monte_carlo(r_multiples: list[float], runs: int = 500, seed: int = 42) -> dict:
    """Rééchantillonnage avec remise des R (bootstrap) : distribution du total R et du drawdown max."""
    r = np.asarray(list(r_multiples), dtype=float)
    keys = ("median_total_r", "p05_total_r", "p95_total_r", "median_max_dd_r", "p95_max_dd_r", "prob_negative")
    if r.size == 0 or runs <= 0:
        out = {k: 0.0 for k in keys}
        out["runs"] = 0
        return out
    rng = np.random.default_rng(seed)
    samples = rng.choice(r, size=(runs, r.size), replace=True)
    curves = np.cumsum(samples, axis=1)
    totals = curves[:, -1]
    curves0 = np.concatenate([np.zeros((runs, 1)), curves], axis=1)
    peaks = np.maximum.accumulate(curves0, axis=1)
    dds = np.max(peaks - curves0, axis=1)
    return {
        "median_total_r": float(np.median(totals)),
        "p05_total_r": float(np.percentile(totals, 5)),
        "p95_total_r": float(np.percentile(totals, 95)),
        "median_max_dd_r": float(np.median(dds)),
        "p95_max_dd_r": float(np.percentile(dds, 95)),
        "prob_negative": float(np.mean(totals < 0)),
        "runs": int(runs),
    }


def parameter_sensitivity(df: pd.DataFrame, signal_factory: SignalFactory, base_params: dict,
                          jitter_percent: float = 20, costs: BTCosts | None = None, warmup: int = 200,
                          risk_money: float = 100.0) -> dict:
    """Fait varier chaque paramètre numérique (non booléen) de ±jitter_percent et compare l'expectancy.

    ``stable`` = expectancy de base > 0 ET toutes les variantes gardent une expectancy > 0.
    Un paramètre entier est arrondi (déplacé d'au moins ±1 si l'arrondi retombe sur la valeur de base).
    """
    costs = costs or BTCosts()
    df = _check_df(df)
    base_m = run_backtest(df, signal_factory(base_params), costs, base_params, risk_money=risk_money,
                          warmup=warmup).metrics
    variants: dict[str, dict] = {}
    for key, val in base_params.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        for sign, label in ((-1, f"-{jitter_percent:g}%"), (1, f"+{jitter_percent:g}%")):
            new = val * (1.0 + sign * jitter_percent / 100.0)
            if isinstance(val, int):
                new = int(round(new))
                if new == val:
                    new = val + sign
            p = dict(base_params)
            p[key] = new
            m = run_backtest(df, signal_factory(p), costs, p, risk_money=risk_money, warmup=warmup).metrics
            variants[f"{key}{label}"] = {"param": key, "value": new, "expectancy_r": m.expectancy_r,
                                         "sample_size": m.sample_size, "profit_factor": m.profit_factor}
    stable = base_m.expectancy_r > 0 and all(v["expectancy_r"] > 0 for v in variants.values())
    return {
        "base_params": dict(base_params), "base_expectancy_r": base_m.expectancy_r,
        "base_sample_size": base_m.sample_size, "jitter_percent": jitter_percent, "variants": variants,
        "stable": bool(stable),
    }


def stress_test(df: pd.DataFrame, signal_fn: SignalFn, costs: BTCosts, spread_multipliers=(1, 2, 3),
                warmup: int = 200, risk_money: float = 100.0) -> dict:
    """Métriques pour chaque multiplicateur de spread (clé = multiplicateur)."""
    df = _check_df(df)
    out: dict = {}
    for m in spread_multipliers:
        c = replace(costs, spread_points=int(round(costs.spread_points * m)))
        out[m] = run_backtest(df, signal_fn, c, {"spread_multiplier": m}, risk_money=risk_money,
                              warmup=warmup).metrics.to_dict()
    return out


# ---------------------------------------------------------------------------
# Signal d'exemple (tests / démonstration)
# ---------------------------------------------------------------------------
def example_ema_cross_signal(fast: int = 20, slow: int = 50, atr_mult: float = 1.5, rr: float = 2.0,
                             atr_period: int = 14) -> SignalFn:
    """Fabrique un signal croisement EMA rapide/lente avec SL = ATR × atr_mult et TP = rr × distance SL.

    EMA/ATR sont calculés sur les ``lookback`` dernières barres du df reçu (fenêtre fixe → déterministe et
    sans dépendance à la longueur totale de l'historique).
    """
    fast, slow = int(max(2, fast)), int(max(3, slow))
    lookback = max(4 * slow, 200)

    def signal_fn(df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < slow + atr_period + 2:
            return None
        d = df.tail(lookback)
        close = d["close"].astype(float)
        ema_f = close.ewm(span=fast, adjust=False).mean()
        ema_s = close.ewm(span=slow, adjust=False).mean()
        prev_close = close.shift(1)
        tr = pd.concat([d["high"] - d["low"], (d["high"] - prev_close).abs(), (d["low"] - prev_close).abs()],
                       axis=1).max(axis=1)
        atr = float(tr.rolling(atr_period).mean().iloc[-1])
        if not math.isfinite(atr) or atr <= 0:
            return None
        f0, f1 = float(ema_f.iloc[-1]), float(ema_f.iloc[-2])
        s0, s1 = float(ema_s.iloc[-1]), float(ema_s.iloc[-2])
        c = float(close.iloc[-1])
        dist = atr * atr_mult
        if f1 <= s1 and f0 > s0:
            return Signal(side=Side.BUY, sl=c - dist, tp=c + rr * dist, note=f"ema{fast}>{slow}")
        if f1 >= s1 and f0 < s0:
            return Signal(side=Side.SELL, sl=c + dist, tp=c - rr * dist, note=f"ema{fast}<{slow}")
        return None

    return signal_fn


__all__ = [
    "Signal", "BTTrade", "BTMetrics", "BTResult", "BTCosts", "WalkForwardFold", "WalkForwardResult",
    "run_backtest", "assert_no_lookahead", "compute_metrics", "split_in_out_of_sample", "walk_forward",
    "monte_carlo", "parameter_sensitivity", "stress_test", "example_ema_cross_signal",
]
