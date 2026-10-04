"""Simulation de backtest en masse sur la carte graphique (2026-09-29, demande utilisateur : « tester toutes les stratégies
possibles, on lance sur le GPU »).

Une configuration = un fil CUDA qui rejoue, bougie par bougie, EXACTEMENT la boucle de `engine.run_backtest` pour des
entrées au marché : exécution à l'ouverture suivante (spread/2 + glissement), stop prioritaire sur la cible, gestion du bot
(TP partiels, break-even, stop suiveur ATR), clôture à la dernière bougie. Le signal de chaque bougie vient du jumeau
vectorisé (`fastsig`). Sorties : les métriques en R de `compute_metrics` utilisées pour trier (nombre de trades, gains,
pertes, somme des gains / pertes, somme et somme des carrés des R, drawdown maximal).

La même fonction est compilée pour le processeur (`simulate_cpu`, Numba) : c'est la référence des tests et le repli si
CUDA n'est pas disponible. Équivalence avec `run_backtest` : tests/test_gpu_sim_2026_09_29.py.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from numba import njit

N_OUT = 8   # trades, gains, pertes, somme gains, somme pertes, somme R, somme R², drawdown max


def _sim_one(opens, highs, lows, closes, atr, side, sl_a, tp_a, warmup, start, end,
             half_spread, slip, mg_on, tp1_r, tp1_p, tp2_r, tp2_p, be_on, be_r, be_off, tr_on, tr_start, tr_mult, out):
    n_tr = 0.0
    wins = 0.0
    losses = 0.0
    sum_g = 0.0
    sum_l = 0.0
    sum_r = 0.0
    sum_r2 = 0.0
    cum = 0.0
    peak = 0.0
    maxdd = 0.0
    has_pend = False
    p_side = 0
    p_sl = 0.0
    p_tp = 0.0
    in_pos = False
    sgn = 0
    entry = 0.0
    risk = 0.0
    psl = 0.0
    ptp = 0.0
    mfe = 0.0
    realized = 0.0
    remaining = 1.0
    tp1 = False
    tp2 = False
    for i in range(start, end):
        # 1) signal en attente exécuté à l'ouverture de la bougie i
        if has_pend:
            e = opens[i] + p_side * (half_spread + slip)
            ok = True
            if not (p_sl == p_sl):
                ok = False
            elif p_side > 0:
                if p_sl >= e or (p_tp == p_tp and p_tp <= e):
                    ok = False
            else:
                if p_sl <= e or (p_tp == p_tp and p_tp >= e):
                    ok = False
            if ok:
                in_pos = True
                sgn = p_side
                entry = e
                psl = p_sl
                ptp = p_tp
                risk = abs(e - p_sl)
                mfe = 0.0
                realized = 0.0
                remaining = 1.0
                tp1 = False
                tp2 = False
            has_pend = False
        # 2) position ouverte
        if in_pos:
            if sgn > 0:
                hit_sl = lows[i] <= psl
                hit_tp = (ptp == ptp) and highs[i] >= ptp
            else:
                hit_sl = highs[i] >= psl
                hit_tp = (ptp == ptp) and lows[i] <= ptp
            closed = False
            px = 0.0
            if hit_sl:
                px = psl - sgn * slip
                closed = True
            elif hit_tp:
                px = ptp
                closed = True
            else:
                fav = sgn * ((highs[i] if sgn > 0 else lows[i]) - entry)
                if fav > mfe:
                    mfe = fav
                if mg_on:
                    mfe_r = mfe / risk
                    if (not tp1) and mfe_r >= tp1_r:
                        realized += tp1_p * tp1_r
                        remaining -= tp1_p
                        tp1 = True
                    if tp1 and (not tp2) and mfe_r >= tp2_r:
                        realized += tp2_p * tp2_r
                        remaining -= tp2_p
                        tp2 = True
                    nouveau = psl
                    if be_on and mfe_r >= be_r:
                        be = entry + sgn * be_off * risk
                        nouveau = max(nouveau, be) if sgn > 0 else min(nouveau, be)
                    if tr_on and mfe_r >= tr_start:
                        a = atr[i]
                        if a == a and a > 0:
                            trail = (entry + sgn * mfe) - sgn * tr_mult * a
                            nouveau = max(nouveau, trail) if sgn > 0 else min(nouveau, trail)
                    psl = nouveau
                if i == end - 1:
                    px = closes[i]
                    closed = True
            if closed:
                r = realized + remaining * (sgn * (px - entry) / risk)
                n_tr += 1.0
                if r > 0:
                    wins += 1.0
                    sum_g += r
                elif r < 0:
                    losses += 1.0
                    sum_l -= r
                sum_r += r
                sum_r2 += r * r
                cum += r
                if cum > peak:
                    peak = cum
                if peak - cum > maxdd:
                    maxdd = peak - cum
                in_pos = False
        # 3) signal à la clôture de la bougie i
        if (not in_pos) and i >= start + warmup and i < end - 1:
            s = side[i]
            if s != 0:
                has_pend = True
                p_side = s
                p_sl = sl_a[i]
                p_tp = tp_a[i]
    out[0] = n_tr
    out[1] = wins
    out[2] = losses
    out[3] = sum_g
    out[4] = sum_l
    out[5] = sum_r
    out[6] = sum_r2
    out[7] = maxdd


_sim_cpu = njit(cache=True)(_sim_one)


@njit(cache=True)
def _batch_cpu(opens, highs, lows, closes, atr, side, sl, tp, warmup, start, end, half_spread, slip, mg, out):
    for c in range(side.shape[0]):
        _sim_cpu(opens, highs, lows, closes, atr, side[c], sl[c], tp[c], warmup, start, end, half_spread, slip,
                 mg[0] > 0, mg[1], mg[2], mg[3], mg[4], mg[5] > 0, mg[6], mg[7], mg[8] > 0, mg[9], mg[10], out[c])


_KERNEL = None


def _kernel():
    """Noyau CUDA compilé à la première utilisation (None si CUDA indisponible)."""
    global _KERNEL
    if _KERNEL is not None:
        return _KERNEL or None
    try:
        from numba import cuda
        if not cuda.is_available():
            _KERNEL = False
            return None
        dev = cuda.jit(device=True)(_sim_one)

        @cuda.jit
        def k(opens, highs, lows, closes, atr, side, sl, tp, warmup, start, end, half_spread, slip, mg, out):
            c = cuda.grid(1)
            if c < side.shape[0]:
                dev(opens, highs, lows, closes, atr, side[c], sl[c], tp[c], warmup, start, end, half_spread, slip,
                    mg[0] > 0, mg[1], mg[2], mg[3], mg[4], mg[5] > 0, mg[6], mg[7], mg[8] > 0, mg[9], mg[10], out[c])
        _KERNEL = k
    except Exception:  # noqa: BLE001 - pas de CUDA : repli processeur
        _KERNEL = False
    return _KERNEL or None


def mgmt_vector(mgmt: Optional[dict]) -> np.ndarray:
    """Paramètres de gestion (mêmes défauts que engine._manage) ; tout à 0 = pas de gestion."""
    if not mgmt:
        return np.zeros(11, dtype=np.float64)
    g = mgmt.get
    return np.array([1.0, float(g("tp1_r", 1.5)), float(g("tp1_close_percent", 30)) / 100.0, float(g("tp2_r", 2.5)),
                     float(g("tp2_close_percent", 40)) / 100.0, 1.0 if bool(g("break_even_enabled", True)) else 0.0,
                     float(g("break_even_r", 1.0)), float(g("break_even_offset_r", 0.05)),
                     1.0 if bool(g("trailing_enabled", True)) else 0.0, float(g("trailing_start_r", 2.0)),
                     float(g("trailing_atr_multiplier", 1.5))], dtype=np.float64)


def simulate(opens, highs, lows, closes, atr, side, sl, tp, spread: float, slip: float, mgmt: Optional[dict],
             warmup: int = 200, start: int = 0, end: Optional[int] = None, device: str = "auto") -> np.ndarray:
    """Métriques (C × N_OUT) pour C configurations dont les signaux sont `side`/`sl`/`tp` (C × n).
    `device` : "gpu", "cpu" ou "auto" (GPU si disponible)."""
    f = lambda a: np.ascontiguousarray(a, dtype=np.float64)  # noqa: E731
    opens, highs, lows, closes, atr = f(opens), f(highs), f(lows), f(closes), f(atr)
    side = np.ascontiguousarray(side, dtype=np.int8)
    sl, tp = f(sl), f(tp)
    end = len(opens) if end is None else int(end)
    mg = mgmt_vector(mgmt)
    out = np.zeros((side.shape[0], N_OUT), dtype=np.float64)
    k = _kernel() if device in ("gpu", "auto") else None
    if device == "gpu" and k is None:
        raise RuntimeError("CUDA indisponible")
    if k is None:
        _batch_cpu(opens, highs, lows, closes, atr, side, sl, tp, int(warmup), int(start), end, spread / 2.0, slip, mg, out)
        return out
    from numba import cuda
    d = cuda.to_device
    dout = cuda.to_device(out)
    k[(side.shape[0] + 127) // 128, 128](d(opens), d(highs), d(lows), d(closes), d(atr), d(side), d(sl), d(tp),
                                          int(warmup), int(start), end, spread / 2.0, slip, d(mg), dout)
    return dout.copy_to_host()


def metrics_from(out_row) -> dict:
    """Métriques au format de `compute_metrics` (celles qui servent au tri) à partir d'une ligne de sortie."""
    n = int(out_row[0])
    if n == 0:
        return {"sample_size": 0, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy_r": 0.0, "total_r": 0.0, "max_drawdown_r": 0.0, "sharpe": 0.0}
    wins, losses, sg, sl_, sr, sr2 = out_row[1], out_row[2], out_row[3], out_row[4], out_row[5], out_row[6]
    pf = sg / sl_ if sl_ > 0 else (float("inf") if sg > 0 else 0.0)
    mean = sr / n
    var = (sr2 - n * mean * mean) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var) if var > 0 else 0.0
    return {"sample_size": n, "trades": n, "wins": int(wins), "losses": int(losses), "win_rate": wins / n,
            "profit_factor": float(pf), "expectancy_r": float(mean), "total_r": float(sr),
            "max_drawdown_r": float(out_row[7]), "sharpe": float(mean / std * math.sqrt(n)) if std > 1e-12 else 0.0}


def simulate_device(opens, highs, lows, closes, atr, side, sl, tp, spread: float, slip: float, mgmt: Optional[dict],
                    warmup: int = 200, start: int = 0, end: Optional[int] = None) -> np.ndarray:
    """Comme `simulate`, mais les tableaux sont DÉJÀ sur la carte (cupy) : aucun aller-retour, seules les métriques
    (P × N_OUT) reviennent au processeur. (2026-09-29, recherche en masse sur la 3090)"""
    import cupy as cp
    k = _kernel()
    if k is None:
        raise RuntimeError("CUDA indisponible")
    side = cp.ascontiguousarray(side, dtype=cp.int8)
    sl = cp.ascontiguousarray(sl, dtype=cp.float64)
    tp = cp.ascontiguousarray(tp, dtype=cp.float64)
    end = int(opens.shape[0]) if end is None else int(end)
    out = cp.zeros((side.shape[0], N_OUT), dtype=cp.float64)
    mg = cp.asarray(mgmt_vector(mgmt))
    k[(side.shape[0] + 127) // 128, 128](opens, highs, lows, closes, atr, side, sl, tp, int(warmup), int(start), end,
                                          spread / 2.0, slip, mg, out)
    return cp.asnumpy(out)
