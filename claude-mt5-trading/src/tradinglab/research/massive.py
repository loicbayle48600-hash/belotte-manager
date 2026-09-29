"""Recherche en masse de stratégies (2026-09-29, demande utilisateur : « tester toutes les stratégies possibles et voir ce
qui en sort »).

Chaque stratégie vectorisée (backtest/fastsig) est déclinée sur une grille FINE de paramètres, sur toutes les classes et
unités de temps ; les signaux sont calculés en une fois par configuration et la simulation est compilée
(backtest/gpu_sim, résultats identiques à run_backtest).

Garde-fous contre les gagnants dus au hasard (plus on teste, plus on en trouve) :
1. **Période de contrôle** : les 30 % les plus récents de chaque historique ne servent JAMAIS au tri ; seules les
   configurations retenues y sont jugées.
2. **Exigence qui croît avec le nombre d'essais** : sur la période d'apprentissage, le t-stat de l'espérance
   (moyenne / écart-type × √n) doit dépasser √(2 ln M), M = nombre de configurations testées (≈ 4,5 pour 30 000).
3. **Plateau** : les voisines (même stratégie, mêmes autres paramètres, stop juste au-dessus et au-dessous) doivent
   aussi être gagnantes — un réglage isolé est écarté.
4. Contrôle : espérance > 0 et PF ≥ 1,1 sur la période jamais vue, avec au moins 15 trades.

Les retenues deviennent des propositions d'agents SHADOW (famille X), comme l'optimiseur.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from . import optimizer as opt

SL_ATR = [0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]
RR = [1.5, 2.0, 3.0]
#: paramètres propres à chaque stratégie (produit cartésien) ; {} = rien de plus que stop et cible
KEY_PARAMS: dict[str, dict[str, list]] = {
    "ema_trend": {"adx_min": [15, 20, 25, 30]},
    "mtf_trend_pullback": {"rsi": [(30, 70), (38, 62), (45, 55)]},
    "ema_pullback": {},
    "macd_momentum": {},
    "atr_expansion": {"atr_ratio": [1.1, 1.3, 1.5, 1.8]},
    "compression_expansion": {},
    "bollinger_mr": {"rsi": [(20, 80), (30, 70), (40, 60)]},
    "breakout_retest": {},
    "failed_breakout": {},
    "liquidity_sweep": {"with_trend": [False, True]},
    "structure_bos": {"require_mtf": [False, True]},
    "choch": {},
    "fib_pullback": {"tol_atr": [0.3, 0.5]},
    "rsi_divergence": {},
    "exhaustion": {"rsi_ext": [25, 30, 35]},
    "sr_rejection": {"tol_atr": [0.2, 0.3, 0.5], "with_trend": [False, True]},
    "donchian_breakout": {"lookback": [10, 20, 40, 55]},
    "rsi2_reversion": {"rsi2": [(5, 95), (10, 90), (20, 80)]},
}
TIMEFRAMES = [("M5", "H1"), ("M15", "H1"), ("H1", "H4"), ("H4", "D1"), ("D1", "D1")]
BARS = {"M5": 20000, "M15": 20000, "H1": 20000, "H4": 8000, "D1": 3000}
HOLDOUT = 0.30
N_OUT = 8


def expand(strategy: str) -> list[dict]:
    """Toutes les combinaisons de paramètres d'une stratégie."""
    keys = list(KEY_PARAMS.get(strategy, {}))
    vals = [KEY_PARAMS[strategy][k] for k in keys]
    out = []
    for combo in itertools.product(*vals) if keys else [()]:
        base = {}
        for k, v in zip(keys, combo):
            if k == "rsi":
                base["rsi_lo"], base["rsi_hi"] = v
            elif k == "rsi2":
                base["rsi_lo"], base["rsi_hi"] = v
            else:
                base[k] = v
        for sl in SL_ATR:
            for rr in RR:
                out.append({**base, "sl_atr": sl, "rr": rr})
    return out


def grid(strategies: Optional[list] = None, classes: Optional[list] = None, timeframes: Optional[list] = None) -> list[dict]:
    """Liste des configurations : {strategy, entry_tf, trend_tf, asset_class, params}."""
    from ..backtest import fastsig
    out = []
    for st in (strategies or sorted(fastsig.FAST)):
        for (e, t) in (timeframes or TIMEFRAMES):
            for c in (classes or list(opt.CLASSES)):
                for prm in expand(st):
                    out.append({"strategy": st, "entry_tf": e, "trend_tf": t, "asset_class": c, "params": prm})
    return out


def seuil_multiple(m: int) -> float:
    """t-stat minimal pour M essais : √(2 ln M) (borne de l'espérance du maximum de M gaussiennes)."""
    return math.sqrt(2.0 * math.log(max(m, 2)))


def aggregate(rows: np.ndarray) -> dict:
    """Métriques d'une configuration à partir des sorties (une ligne par symbole) : sommes additionnées, drawdown max."""
    n = float(rows[:, 0].sum())
    if n <= 0:
        return {"n": 0, "exp": 0.0, "pf": 0.0, "t": 0.0, "dd": 0.0, "total": 0.0}
    sg, sl, sr, sr2 = rows[:, 3].sum(), rows[:, 4].sum(), rows[:, 5].sum(), rows[:, 6].sum()
    mean = sr / n
    var = (sr2 - n * mean * mean) / (n - 1) if n > 1 else 0.0
    t = mean / math.sqrt(var) * math.sqrt(n) if var > 1e-18 else 0.0
    pf = sg / sl if sl > 0 else (float("inf") if sg > 0 else 0.0)
    return {"n": int(n), "exp": float(mean), "pf": float(pf), "t": float(t), "dd": float(rows[:, 7].max()), "total": float(sr)}


def plateau_ok(cfg: dict, index: dict, min_voisins: int = 2) -> bool:
    """Voisines de stop (juste au-dessus / au-dessous, mêmes autres paramètres) gagnantes sur l'apprentissage."""
    i = SL_ATR.index(cfg["params"]["sl_atr"])
    voisines = [SL_ATR[j] for j in (i - 1, i + 1) if 0 <= j < len(SL_ATR)]
    reste = {k: v for k, v in cfg["params"].items() if k != "sl_atr"}
    gagnantes = 0
    for sl in voisines:
        m = index.get(_key(cfg, {**reste, "sl_atr": sl}))
        if m is not None and m["exp"] > 0 and m["pf"] > 1.0:
            gagnantes += 1
    return gagnantes >= min(min_voisins, len(voisines))


def _key(cfg: dict, params: dict) -> str:
    return json.dumps([cfg["strategy"], cfg["entry_tf"], cfg["trend_tf"], cfg["asset_class"], sorted(params.items())], default=str)


# ------------------------------------------------------------------------------------------------ calcul (processus fils)
_DATA: dict = {}
_SPECS: dict = {}
_COSTS: dict = {}
_MGMT: Optional[dict] = None


def _init(data, specs, costs, mgmt):
    global _DATA, _SPECS, _COSTS, _MGMT
    _DATA, _SPECS, _COSTS, _MGMT = data, specs, costs, mgmt


def _task(args) -> list:
    """(symbole, cadre, stratégie, liste de configurations) → sorties apprentissage et contrôle par configuration."""
    from ..agents.registry import AgentSpec
    from ..backtest import gpu_sim
    from ..backtest.engine import _atr_causal
    from .adapters import make_signal_fn
    sym, entry_tf, trend_tf, strategy, cfgs = args
    df = _DATA.get((sym, entry_tf))
    ss = _SPECS.get(sym)
    if df is None or ss is None or len(df) < 600:
        return [None] * len(cfgs)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = _atr_causal(h, l, c)
    n = len(df)
    cut = int(n * (1 - HOLDOUT))
    sides, sls, tps = [], [], []
    for cfg in cfgs:
        spec = AgentSpec(agent_id="MASS", family="X", name="mass", strategy=strategy, markets=[sym],
                         sessions=list(opt.SESSIONS), timeframes={"entry": entry_tf, "trend": trend_tf},
                         regimes=list(opt.REGIMES.get(strategy, opt.ALL)), params=dict(cfg["params"]), base_strategy=strategy)
        f = make_signal_fn(spec, ss, entry_tf)
        f.prepare(df)
        fa = f.fast_arrays()
        if fa is None:
            sides.append(np.zeros(n, dtype=np.int8)); sls.append(np.full(n, np.nan)); tps.append(np.full(n, np.nan))
        else:
            sides.append(fa[0]); sls.append(fa[1]); tps.append(fa[2])
    side, sl, tp = np.stack(sides), np.stack(sls), np.stack(tps)
    cost = _COSTS[sym]
    appr = gpu_sim.simulate(o, h, l, c, atr, side, sl, tp, cost.spread, cost.slippage, _MGMT, warmup=200, start=0, end=cut, device="cpu")
    ctrl = gpu_sim.simulate(o, h, l, c, atr, side, sl, tp, cost.spread, cost.slippage, _MGMT, warmup=0, start=cut, end=n, device="cpu")
    return [(appr[k], ctrl[k]) for k in range(len(cfgs))]


# ------------------------------------------------------------------------------------------------ pilotage
def select(configs: list[dict], results: dict, min_trades: int = 60) -> tuple[list, dict]:
    """Applique les garde-fous 2 à 4. `results[i]` = (lignes apprentissage, lignes contrôle) par symbole."""
    index, rows = {}, []
    for i, cfg in enumerate(configs):
        r = results.get(i)
        if not r:
            continue
        a = aggregate(np.array([x[0] for x in r])) if r else None
        k = aggregate(np.array([x[1] for x in r])) if r else None
        index[_key(cfg, cfg["params"])] = a
        rows.append((cfg, a, k))
    t_min = seuil_multiple(len(rows))
    etapes = {"testees": len(rows), "t_min": round(t_min, 2)}
    s1 = [x for x in rows if x[1]["n"] >= min_trades and x[1]["exp"] > 0 and x[1]["pf"] >= 1.2 and x[1]["t"] >= t_min]
    etapes["significatives"] = len(s1)
    s2 = [x for x in s1 if plateau_ok(x[0], index)]
    etapes["plateau"] = len(s2)
    s3 = [x for x in s2 if x[2]["n"] >= 15 and x[2]["exp"] > 0 and x[2]["pf"] >= 1.1]
    etapes["controle"] = len(s3)
    s3.sort(key=lambda x: -(x[2]["exp"] * math.sqrt(min(x[2]["n"], 200)) + x[1]["t"] * 0.1))
    return s3, etapes


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - processus de calcul
    import multiprocessing as mp

    from ..agents.registry import AgentRegistry
    from ..backtest.engine import BTCosts
    from ..core.config import load_dotenv, load_settings
    from ..core.journal import Journal
    from ..mt5.mock_adapter import make_broker
    from ..mt5.symbols import resolve_symbols
    from .rates_cache import TerminalOccupe, load_rates

    ap = argparse.ArgumentParser(description="Recherche en masse de stratégies")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--classes", default="")
    ap.add_argument("--strategies", default="")
    ap.add_argument("--max-configs", type=int, default=0)
    args = ap.parse_args(argv)
    s = load_settings()
    load_dotenv(s.home / ".env")
    journal = Journal(s.logs_dir, s.system.get("timezone_local", "UTC"), component="massive")
    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), s)
    if not broker.connect():
        print("terminal indisponible")
        return 1
    t0 = time.time()
    classes = [c for c in args.classes.split(",") if c] or list(opt.CLASSES)
    strategies = [x for x in args.strategies.split(",") if x] or None
    configs = grid(strategies, classes)
    if args.max_configs:
        configs = configs[: args.max_configs]
    voulus = sorted({sym for c in classes for sym in opt.CLASSES_ALL[c]})
    reels = resolve_symbols(voulus, broker.symbols())
    data, specs, costs = {}, {}, {}
    bt = s.backtest
    tfs = sorted({c["entry_tf"] for c in configs})
    for sym in voulus:
        reel = reels.get(sym)
        ss = broker.symbol_info(reel) if reel else None
        if ss is None:
            continue
        specs[sym] = ss
        costs[sym] = BTCosts(spread_points=int(bt.get("default_spread_points", ss.spread_points or 12)),
                             commission_per_lot=float(bt.get("commission_per_lot", 0.0)), slippage_points=int(bt.get("slippage_points", 3)),
                             point=ss.point, tick_value=ss.tick_value, tick_size=ss.tick_size)
        for tf in tfs:
            try:
                df = load_rates(broker, reel, tf, BARS[tf], s.data_dir / "cache" / "rates", pause_sec=2.0,
                                state_file=s.state_dir / "system_state.json")
            except TerminalOccupe as e:
                journal.warn("recherche en masse arrêtée : terminal occupé", error=str(e))
                return 2
            if df is not None and len(df):
                data[(sym, tf)] = df
    journal.event("massive_start", configurations=len(configs), jeux=len(data), workers=args.workers)
    # tâches : (symbole, cadre, stratégie) avec toutes ses configurations
    groupes: dict = {}
    for i, cfg in enumerate(configs):
        for sym in opt.CLASSES_ALL[cfg["asset_class"]]:
            groupes.setdefault((sym, cfg["entry_tf"], cfg["trend_tf"], cfg["strategy"]), []).append(i)
    taches = [(k[0], k[1], k[2], k[3], [configs[i] for i in idx]) for k, idx in groupes.items()]
    cles = list(groupes.values())
    mgmt = dict(s.profit_management) if bool(bt.get("use_position_management", True)) else None
    results: dict = {}
    with mp.Pool(processes=args.workers, initializer=_init, initargs=(data, specs, costs, mgmt), maxtasksperchild=20) as pool:
        for idx, res in zip(cles, pool.imap(_task, taches, chunksize=1)):
            for i, r in zip(idx, res):
                if r is not None:
                    results.setdefault(i, []).append(r)
    retenues, etapes = select(configs, results)
    retenues = retenues[: args.top]
    reg = AgentRegistry(status_file=s.data_dir / "agent_status.json")
    ids = opt.next_ids(set(reg.agents), len(retenues))
    props = []
    for (cfg, a, k), aid in zip(retenues, ids):
        c = opt.Config(cfg["strategy"], cfg["entry_tf"], cfg["trend_tf"], cfg["params"]["sl_atr"], cfg["params"]["rr"], cfg["asset_class"])
        p = opt.proposal_spec(c, aid, {"profit_factor": a["pf"], "sample_size": a["n"], "expectancy_r": a["exp"]},
                              {"robustness_ratio": 0.0, "oos_expectancy_r": k["exp"]}, "SHADOW")
        p["params"] = {**p["params"], **cfg["params"]}
        p["description"] = (f"Recherche en masse {datetime.now(timezone.utc).date()} : apprentissage {a['n']} trades, "
                            f"{a['exp']:+.2f} R, PF {a['pf']:.2f}, t {a['t']:.1f} ; contrôle jamais vu {k['n']} trades, "
                            f"{k['exp']:+.2f} R, PF {k['pf']:.2f}")
        props.append(p)
    with (s.state_dir / "agent_proposals.jsonl").open("a", encoding="utf-8") as fh:
        for p in props:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    rapport = {"date": datetime.now(timezone.utc).isoformat(), "duree_sec": round(time.time() - t0), "etapes": etapes,
               "retenues": [{"config": c, "apprentissage": a, "controle": k} for c, a, k in retenues],
               "propositions": [p["agent_id"] for p in props]}
    out = s.home / "reports" / f"massive_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rapport, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    journal.event("massive_done", **etapes, retenues=len(props), rapport=str(out), duree_sec=rapport["duree_sec"])
    print(f"{etapes} → {len(props)} propositions ({out})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
