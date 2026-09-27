"""Optimiseur systématique d'agents (2026-09-28, feu vert de l'utilisateur : « créer les meilleurs agents »).

Personne ne sait à l'avance quelle configuration tiendra : on laisse les données choisir. Pour chaque stratégie de
base, l'optimiseur teste une grille (unité de temps, largeur du stop, objectif, classe d'actif), chaque configuration
étant jugée sur plusieurs symboles à la fois, sur un long historique, avec la GESTION RÉELLE du bot (break-even, TP
partiels, stop suiveur — `backtest/engine.py`). Deux étages :

1. tri large : backtest complet ; survivent PF >= 1,2, >= 40 trades, espérance >= 0,10 R, drawdown <= 15 R ;
2. walk-forward anchoré (4 plis) sur les survivants : survivent robustesse >= 0,5 et espérance hors échantillon > 0.

Les configurations retenues deviennent des PROPOSITIONS d'agents (`state/agent_proposals.jsonl`) que l'orchestrateur,
seul écrivain du registre, ajoute à son cycle de recherche (statut SHADOW par défaut, LIVE si demandé).

Calcul : processus séparé, `multiprocessing` sur les cœurs disponibles (24 sur la machine de l'utilisateur), données
lues une fois dans le processus parent (MT5, lecture seule). Un GPU n'aide pas : le moteur avance barre par barre.

Usage : python -m tradinglab.research.optimizer [--workers N] [--max-configs N] [--top K] [--status SHADOW|LIVE]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..core.types import Regime

# ---------------------------------------------------------------- grille
STRATEGIES: dict[str, dict] = {
    "ema_trend": {"adx_min": 20}, "mtf_trend_pullback": {"rsi_lo": 38, "rsi_hi": 62}, "bollinger_mr": {"rsi_lo": 30, "rsi_hi": 70},
    "atr_expansion": {"atr_ratio": 1.3}, "breakout_retest": {}, "liquidity_sweep": {"with_trend": True},
    "sr_rejection": {"tol_atr": 0.3, "with_trend": True}, "structure_bos": {}, "macd_momentum": {},
    "failed_breakout": {}, "rsi_divergence": {}, "donchian_breakout": {"lookback": 20},
}
TREND = [Regime.TRENDING.value, Regime.RISK_ON.value, Regime.RISK_OFF.value, Regime.BREAKOUT.value]
RANGE = [Regime.RANGING.value, Regime.LOW_VOLATILITY.value]
BREAK = [Regime.BREAKOUT.value, Regime.LOW_VOLATILITY.value, Regime.HIGH_VOLATILITY.value, Regime.TRENDING.value]
ALL = [r.value for r in Regime if r is not Regime.NEWS_SHOCK]
REGIMES = {"ema_trend": TREND, "mtf_trend_pullback": TREND, "bollinger_mr": RANGE, "atr_expansion": BREAK,
           "breakout_retest": BREAK, "liquidity_sweep": ALL, "sr_rejection": TREND + RANGE, "structure_bos": TREND + [Regime.UNCERTAIN.value],
           "macd_momentum": TREND, "failed_breakout": RANGE + [Regime.UNCERTAIN.value], "rsi_divergence": RANGE + [Regime.UNCERTAIN.value],
           "donchian_breakout": ALL}
TIMEFRAMES = [("M15", "H1"), ("H1", "H4"), ("H4", "D1"), ("D1", "D1")]
SL_ATR = [1.0, 1.5, 2.0]
RR = [1.5, 2.0, 2.5, 3.0]
CLASSES = {"forex": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"], "indices": ["US500", "USTEC", "DE40"],
           "metals": ["XAUUSD", "XAGUSD"], "crypto": ["BTCUSD", "ETHUSD", "SOLUSD"]}
BARS = {"M15": 10000, "H1": 10000, "H4": 10000, "D1": 5000}
SESSIONS = ["ASIA", "LONDON", "NEWYORK", "OVERLAP_LDN_NY"]


@dataclass(frozen=True)
class Config:
    strategy: str
    entry_tf: str
    trend_tf: str
    sl_atr: float
    rr: float
    asset_class: str

    @property
    def name(self) -> str:
        return f"opt_{self.strategy}_{self.entry_tf.lower()}_{self.asset_class}_sl{self.sl_atr:g}_rr{self.rr:g}"

    def params(self) -> dict:
        return {**STRATEGIES[self.strategy], "sl_atr": self.sl_atr, "rr": self.rr}


def grid(max_configs: int = 0) -> list[Config]:
    out = [Config(s, e, t, sl, rr, c) for s in STRATEGIES for (e, t) in TIMEFRAMES for sl in SL_ATR for rr in RR for c in CLASSES]
    return out[:max_configs] if max_configs else out


def survives_stage1(m: dict, seuils: dict) -> bool:
    return (m.get("sample_size", 0) >= int(seuils.get("min_sample_size", 40)) and m.get("profit_factor", 0.0) >= float(seuils.get("min_profit_factor", 1.2))
            and m.get("expectancy_r", 0.0) >= float(seuils.get("min_expectancy_r", 0.10)) and m.get("max_drawdown_r", 99.0) <= float(seuils.get("max_drawdown_r", 15.0)))


def score(m: dict) -> float:
    """Espérance pondérée par la taille d'échantillon (plafonnée à 200 trades) : un 2,5 R sur 10 trades ne bat pas un
    0,3 R sur 200."""
    n = max(int(m.get("sample_size", 0)), 0)
    retrecie = float(m.get("expectancy_r", 0.0)) * n / (n + 30.0)      # rétrécissement vers 0 pour les petits échantillons
    return retrecie * math.sqrt(min(n, 200))


def proposal_spec(cfg: Config, agent_id: str, metrics: dict, wf: dict, status: str) -> dict:
    """Spécification d'agent (AgentSpec.to_dict) prête à être ajoutée au registre par l'orchestrateur."""
    return {"agent_id": agent_id, "family": "X", "name": cfg.name, "strategy": cfg.strategy, "markets": list(CLASSES[cfg.asset_class]),
            "sessions": list(SESSIONS), "timeframes": {"entry": cfg.entry_tf, "trend": cfg.trend_tf}, "regimes": list(REGIMES[cfg.strategy]),
            "params": cfg.params(), "status": status, "version": "1.0", "model_tier_role": "technical_analysis",
            "news_sensitive": False, "cost_budget_usd": 0.5,
            "description": (f"Optimiseur {datetime.now(timezone.utc).date().isoformat()} : PF {metrics.get('profit_factor', 0):.2f}, "
                            f"{metrics.get('sample_size', 0)} trades, {metrics.get('expectancy_r', 0):+.2f} R, walk-forward "
                            f"robustesse {wf.get('robustness_ratio', 0):.2f}, hors échantillon {wf.get('oos_expectancy_r', 0):+.2f} R"),
            "entry_rules": "configuration sélectionnée par l'optimiseur (walk-forward multi-symboles)",
            "invalidation_rules": "", "sl_logic": "structure/ATR", "tp_logic": "ADAPTIVE_R_MANAGEMENT", "filters": [],
            "parent_id": None, "created_by": "optimizer", "base_strategy": cfg.strategy}


# ---------------------------------------------------------------- calcul (processus fils)
_DATA: dict = {}
_SPECS: dict = {}
_COSTS: dict = {}
_MGMT: Optional[dict] = None


def _init_worker(data: dict, specs: dict, costs: dict, mgmt: Optional[dict]) -> None:
    global _DATA, _SPECS, _COSTS, _MGMT
    _DATA, _SPECS, _COSTS, _MGMT = data, specs, costs, mgmt
    from ..backtest.engine import set_default_management
    set_default_management(mgmt)


def _spec_for(cfg: Config, agent_id: str = "OPT"):
    from ..agents.registry import AgentSpec
    return AgentSpec(agent_id=agent_id, family="X", name=cfg.name, strategy=cfg.strategy, markets=list(CLASSES[cfg.asset_class]),
                     sessions=list(SESSIONS), timeframes={"entry": cfg.entry_tf, "trend": cfg.trend_tf},
                     regimes=list(REGIMES[cfg.strategy]), params=cfg.params(), base_strategy=cfg.strategy)


def eval_stage1(cfg: Config) -> dict:
    """Backtest complet, trades cumulés sur les symboles de la classe. Tourne dans un processus fils."""
    from ..backtest.engine import compute_metrics, run_backtest
    from .adapters import make_signal_fn

    trades = []
    spec = _spec_for(cfg)
    for sym in CLASSES[cfg.asset_class]:
        df = _DATA.get((sym, cfg.entry_tf))
        ss = _SPECS.get(sym)
        if df is None or ss is None or len(df) < 300:
            continue
        try:
            trades += run_backtest(df, make_signal_fn(spec, ss, cfg.entry_tf), _COSTS[sym]).trades
        except Exception as e:  # noqa: BLE001 - une configuration en erreur est simplement écartée
            return {"config": asdict(cfg), "error": f"{type(e).__name__}: {e}"}
    trades.sort(key=lambda t: str(t.entry_time))
    return {"config": asdict(cfg), "metrics": compute_metrics(trades).to_dict()}


def eval_stage2(cfg: Config) -> dict:
    """Walk-forward anchoré (4 plis) par symbole, hors échantillon cumulé ; grille = paramètres ± 20 %."""
    from ..backtest.engine import compute_metrics, walk_forward
    from .adapters import make_signal_factory

    spec = _spec_for(cfg)
    base = cfg.params()
    grille = [dict(base)]
    for k, v in base.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            grille.append({**base, k: v * 1.2})
            grille.append({**base, k: v * 0.8})
    oos, ratios = [], []
    for sym in CLASSES[cfg.asset_class]:
        df = _DATA.get((sym, cfg.entry_tf))
        ss = _SPECS.get(sym)
        if df is None or ss is None or len(df) < 600:
            continue
        try:
            wf = walk_forward(df, make_signal_factory(spec, ss, cfg.entry_tf), grille, _COSTS[sym], folds=4)
        except Exception as e:  # noqa: BLE001
            return {"config": asdict(cfg), "error": f"{type(e).__name__}: {e}"}
        oos += wf.oos_trades
        if wf.folds:
            ratios.append(wf.robustness_ratio)
    oos.sort(key=lambda t: str(t.entry_time))
    m = compute_metrics(oos).to_dict()
    return {"config": asdict(cfg), "wf": {"robustness_ratio": (sum(ratios) / len(ratios)) if ratios else 0.0,
                                          "oos_expectancy_r": m.get("expectancy_r", 0.0), "oos_trades": m.get("sample_size", 0)}}


def survives_stage2(wf: dict) -> bool:
    return wf.get("robustness_ratio", 0.0) >= 0.5 and wf.get("oos_expectancy_r", 0.0) > 0 and wf.get("oos_trades", 0) >= 20


# ---------------------------------------------------------------- pilotage (processus parent)
def next_ids(registry_agents: set, n: int) -> list[str]:
    out, k = [], 1
    while len(out) < n:
        aid = f"X{k:02d}"
        if aid not in registry_agents:
            out.append(aid)
        k += 1
    return out


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - processus de calcul
    import multiprocessing as mp

    from ..agents.registry import AgentRegistry
    from ..backtest.engine import BTCosts
    from ..core.config import load_dotenv, load_settings
    from ..core.journal import Journal
    from ..mt5.mock_adapter import make_broker
    from ..mt5.symbols import resolve_symbols

    ap = argparse.ArgumentParser(description="Optimiseur systématique d'agents")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument("--max-configs", type=int, default=0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--status", default="SHADOW", choices=["SHADOW", "LIVE"])
    args = ap.parse_args(argv)
    s = load_settings()
    load_dotenv(s.home / ".env")
    journal = Journal(s.logs_dir, s.system.get("timezone_local", "UTC"), component="optimizer")
    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), s)
    if not broker.connect():
        print("terminal indisponible")
        return 1
    t0 = time.time()
    voulus = sorted({sym for syms in CLASSES.values() for sym in syms})
    reels = resolve_symbols(voulus, broker.symbols())
    data, specs, costs = {}, {}, {}
    bt = s.backtest
    for sym in voulus:
        reel = reels.get(sym)
        if not reel:
            continue
        ss = broker.symbol_info(reel)
        if ss is None:
            continue
        broker.symbol_select(reel)
        specs[sym] = ss
        costs[sym] = BTCosts(spread_points=int(bt.get("default_spread_points", ss.spread_points or 12)),
                             commission_per_lot=float(bt.get("commission_per_lot", 0.0)), slippage_points=int(bt.get("slippage_points", 3)),
                             point=ss.point, tick_value=ss.tick_value, tick_size=ss.tick_size)
        for tf, n in BARS.items():
            df = broker.rates(reel, tf, n)
            if df is not None and len(df):
                data[(sym, tf)] = df
    journal.event("optimizer_start", symboles=sorted(specs), jeux_de_donnees=len(data), workers=args.workers)
    configs = grid(args.max_configs)
    seuils = s.learning
    mgmt = dict(s.profit_management) if bool(bt.get("use_position_management", True)) else None
    with mp.Pool(processes=args.workers, initializer=_init_worker, initargs=(data, specs, costs, mgmt), maxtasksperchild=50) as pool:
        r1 = list(pool.imap_unordered(eval_stage1, configs, chunksize=4))
        survivants = [r for r in r1 if "metrics" in r and survives_stage1(r["metrics"], seuils)]
        survivants.sort(key=lambda r: -score(r["metrics"]))
        survivants = survivants[: max(args.top * 8, 60)]
        journal.event("optimizer_stage1", configurations=len(configs), erreurs=sum(1 for r in r1 if "error" in r), survivants=len(survivants))
        cfgs2 = [Config(**r["config"]) for r in survivants]
        r2 = {tuple(sorted(r["config"].items())): r for r in pool.imap_unordered(eval_stage2, cfgs2, chunksize=1)}
    finals = []
    for r in survivants:
        w = r2.get(tuple(sorted(r["config"].items())), {}).get("wf")
        if w and survives_stage2(w):
            finals.append({**r, "wf": w})
    finals.sort(key=lambda r: -(score(r["metrics"]) * (0.5 + r["wf"]["robustness_ratio"])))
    finals = finals[: args.top]
    reg = AgentRegistry(status_file=s.data_dir / "agent_status.json")
    ids = next_ids(set(reg.agents), len(finals))
    props = [proposal_spec(Config(**r["config"]), aid, r["metrics"], r["wf"], args.status) for r, aid in zip(finals, ids)]
    (s.state_dir / "agent_proposals.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with (s.state_dir / "agent_proposals.jsonl").open("a", encoding="utf-8") as fh:
        for p in props:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    rapport = {"date": datetime.now(timezone.utc).isoformat(), "duree_sec": round(time.time() - t0), "configurations": len(configs),
               "survivants_etape_1": len(survivants), "retenus": len(finals), "propositions": props,
               "classement": [{"config": r["config"], "metrics": r["metrics"], "wf": r["wf"]} for r in finals],
               "etape_1_top": [{"config": r["config"], "metrics": r["metrics"]} for r in survivants[:40]]}
    (s.home / "reports").mkdir(exist_ok=True)
    out = s.home / "reports" / f"optimizer_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')}.json"
    out.write_text(json.dumps(rapport, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    journal.event("optimizer_done", retenus=len(finals), rapport=str(out), duree_sec=rapport["duree_sec"], propositions=[p["agent_id"] for p in props])
    print(f"{len(finals)} configurations retenues sur {len(configs)} en {rapport['duree_sec']} s → {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
