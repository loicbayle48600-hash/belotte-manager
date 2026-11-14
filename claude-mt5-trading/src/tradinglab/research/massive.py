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
#: 2026-10-01 : fenêtres d'ouverture (UTC) de la famille « cassure de range » (session_breakout) : Asie complète, Sydney,
#: Tokyo, Francfort, Londres, avant New York, ouverture de New York. Horaires d'été (Londres UTC+1, New York UTC-4).
FENETRES = [("00:00", "07:00"), ("22:00", "00:00"), ("00:00", "02:00"), ("06:00", "07:00"), ("07:00", "08:00"),
            ("07:00", "09:00"), ("12:00", "13:30"), ("13:30", "14:00"), ("13:30", "14:30"), ("14:30", "15:00")]

KEY_PARAMS: dict[str, dict[str, list]] = {
    "session_breakout": {"fenetre": [("00:00", "07:00"), ("07:00", "08:00"), ("13:30", "14:00")], "trend_only": [False, True]},
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
#: grille FINE (2026-09-30, recherches de nuit) : stops et cibles plus serrés, réglages clés élargis
SL_ATR_FIN = [0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.3, 2.6, 3.0]
RR_FIN = [1.2, 1.5, 2.0, 2.5, 3.0, 4.0]
KEY_PARAMS_FIN: dict[str, dict[str, list]] = {
    "ema_trend": {"adx_min": [12, 15, 18, 20, 23, 25, 28, 32]},
    "mtf_trend_pullback": {"rsi": [(25, 75), (30, 70), (35, 65), (38, 62), (42, 58), (45, 55)]},
    "atr_expansion": {"atr_ratio": [1.05, 1.1, 1.2, 1.3, 1.4, 1.5, 1.7, 2.0]},
    "bollinger_mr": {"rsi": [(15, 85), (20, 80), (25, 75), (30, 70), (35, 65), (40, 60)]},
    "liquidity_sweep": {"with_trend": [False, True]},
    "structure_bos": {"require_mtf": [False, True]},
    "fib_pullback": {"tol_atr": [0.2, 0.3, 0.4, 0.5, 0.7]},
    "exhaustion": {"rsi_ext": [15, 18, 20, 22, 25, 28, 30, 35]},
    "sr_rejection": {"tol_atr": [0.15, 0.2, 0.3, 0.4, 0.5], "with_trend": [False, True]},
    "donchian_breakout": {"lookback": [8, 10, 15, 20, 30, 40, 55, 80]},
    "rsi2_reversion": {"rsi2": [(3, 97), (5, 95), (10, 90), (15, 85), (20, 80)]},
    "session_breakout": {"fenetre": FENETRES, "trend_only": [False, True]},
}
# 2026-09-30 (demande utilisateur : « or et tous les autres, sur tous les temps possibles ») : M1, H2 et W1 en plus.
# MT5 refuse une demande égale à sa limite « Max bars » (100 000 : « Invalid params », constaté le 30/09) : 90 000 M1 ≈ 3 mois ; MN1 n'est jamais une unité d'ENTRÉE (trop peu de mois).
BARS = {"M1": 90000, "M5": 20000, "M15": 20000, "H1": 20000, "H2": 12000, "H4": 8000, "D1": 3000, "W1": 1500}
HOLDOUT = 0.30
#: 2026-09-30 : H2 et W1 CONSTRUITS à partir des historiques H1 / D1 déjà en cache (regroupement UTC, comme les unités
#: de tendance) : aucun téléchargement lourd par le terminal MT5 partagé avec le bot. Les premiers téléchargements réels
#: H2 / W1 des 73 marchés ont allongé un cycle du bot à 74 s le 30/09 au soir. `--ut-reelles` pour les vraies barres
#: (passage dédié, marché calme).
DERIVEES = {"H2": "H1", "W1": "D1"}


def charger_ut(broker, reel: str, tf: str, cache_dir, state_file=None, max_cache_age_sec=None, reelles: bool = False):
    """Historique de `tf` pour la recherche : téléchargé (cache), ou construit depuis l'unité source de `DERIVEES`."""
    from .adapters import resample
    from .rates_cache import load_rates

    src = None if reelles else DERIVEES.get(tf)
    df = load_rates(broker, reel, src or tf, BARS[src or tf], cache_dir, pause_sec=2.0, max_cache_age_sec=max_cache_age_sec,
                    state_file=state_file)
    if src and df is not None and len(df):
        df = resample(df, tf)
    return df
#: 2026-09-29, demande utilisateur (« des agents spécialisés par marché : Londres, Asie, US… ») : chaque configuration est
#: aussi déclinée par session. Le filtre de session est le PREMIER du screener et ne dépend que de l'heure : la variante
#: se déduit exactement des signaux « toutes sessions » en effaçant ceux hors session (voir adapters.masque_sessions).
SESSION_VARIANTS = [None, ["ASIA"], ["LONDON"], ["NEWYORK"], ["OVERLAP_LDN_NY"], ["SYDNEY"]]
#: 2026-10-01 : signaux de base calculés sur TOUTES les sessions (Sydney comprise), puis chaque configuration est filtrée
#: par SES sessions (« toutes » = les 4 sessions de jour, sans Sydney), exactement comme le bot en live

N_OUT = 8


def expand(strategy: str, fin: bool = False) -> list[dict]:
    """Toutes les combinaisons de paramètres d'une stratégie (`fin` : grille fine de nuit)."""
    table = {**KEY_PARAMS, **KEY_PARAMS_FIN} if fin else KEY_PARAMS
    keys = list(table.get(strategy, {}))
    vals = [table[strategy][k] for k in keys]
    out = []
    for combo in itertools.product(*vals) if keys else [()]:
        base = {}
        for k, v in zip(keys, combo):
            if k == "rsi":
                base["rsi_lo"], base["rsi_hi"] = v
            elif k == "rsi2":
                base["rsi_lo"], base["rsi_hi"] = v
            elif k == "fenetre":
                base["start"], base["end"] = v
            else:
                base[k] = v
        for sl in (SL_ATR_FIN if fin else SL_ATR):
            for rr in (RR_FIN if fin else RR):
                out.append({**base, "sl_atr": sl, "rr": rr})
    return out


#: espaces LARGES des réglages clés (2026-09-30, recherche continue : « des milliards de tests ») — tirage aléatoire
ESPACES: dict[str, dict] = {
    "session_breakout": {"fenetre": ("fenetre", FENETRES), "trend_only": ("bool",)},
    "ema_trend": {"adx_min": ("int", 8, 40)},
    "mtf_trend_pullback": {"rsi_lo": ("sym", 20, 48)},
    "atr_expansion": {"atr_ratio": ("float", 1.0, 2.5)},
    "bollinger_mr": {"rsi_lo": ("sym", 8, 45)},
    "fib_pullback": {"tol_atr": ("float", 0.1, 1.0)},
    "exhaustion": {"rsi_ext": ("int", 8, 40)},
    "sr_rejection": {"tol_atr": ("float", 0.1, 0.8), "with_trend": ("bool",)},
    "donchian_breakout": {"lookback": ("int", 5, 120)},
    "rsi2_reversion": {"rsi_lo": ("sym", 2, 30)},
    "liquidity_sweep": {"with_trend": ("bool",)},
    "structure_bos": {"require_mtf": ("bool",)},
}
TF_ALEATOIRES = [("M5", "M15"), ("M5", "H1"), ("M15", "H1"), ("M15", "H4"), ("H1", "H4"), ("H1", "D1"), ("H4", "D1"),
                 ("H4", "H4"), ("D1", "D1"),
                 # 2026-09-30 : H2 et W1 (MN1 en tendance seulement). PAS de M1 dans la recherche continue : télécharger
                 # 90 000 barres M1 sur 73 marchés par le terminal partagé a allongé les cycles du bot à 54-74 s le
                 # 30/09 au soir. Le M1 passe par des passages dédiés (--toutes-ut), à lancer marché calme (week-end).
                 ("H2", "H4"), ("H2", "D1"), ("H4", "W1"), ("D1", "W1"), ("D1", "MN1"), ("W1", "MN1"), ("W1", "W1")]
#: toutes les paires (entrée, tendance) utiles, pour `--toutes-ut`
TF_TOUTES = [("M1", "M5"), ("M1", "M15"), ("M5", "M15"), ("M5", "H1"), ("M15", "H1"), ("M15", "H4"), ("H1", "H4"),
             ("H1", "D1"), ("H2", "H4"), ("H2", "D1"), ("H4", "D1"), ("H4", "W1"), ("H4", "H4"), ("D1", "W1"),
             ("D1", "MN1"), ("D1", "D1"), ("W1", "MN1"), ("W1", "W1")]


def _cle_idee(strategie, ut, marches, sessions, params=None) -> tuple:
    """Identité d'une idée (pas de doublon d'un passage à l'autre). Cassures de range : la fenêtre en fait partie, une
    cassure d'ouverture de New York n'est pas la même idée qu'une cassure du range asiatique."""
    cle = (strategie, ut, tuple(sorted(marches)), str(sorted(sessions)))
    if strategie == "session_breakout":
        p = params or {}
        cle += (str(p.get("start", "00:00")), str(p.get("end", "07:00")))
    return cle


def tirage(n_reglages: int, seed: int, strategies: Optional[list] = None, classes: Optional[list] = None,
           sessions: Optional[list] = None) -> list[dict]:
    """Configurations tirées au hasard : pour chaque tirage (stratégie, couple d'unités de temps, réglages clés), toute la
    grille fine stop × cible (le plateau se juge sur le stop), toutes les classes et variantes de session."""
    import random
    from ..backtest import fastsig
    rng = random.Random(seed)
    strats = [x for x in (strategies or sorted(fastsig.FAST)) if x in ESPACES]
    out = []
    for _ in range(n_reglages):
        st = rng.choice(strats)
        paires = [x for x in TF_ALEATOIRES if x[0] in ("M1", "M5", "M15", "H1")] if st == "session_breakout" else TF_ALEATOIRES
        e, t = rng.choice(paires)
        base = {}
        for k, spec in ESPACES[st].items():
            if spec[0] == "int":
                base[k] = rng.randint(spec[1], spec[2])
            elif spec[0] == "float":
                base[k] = round(rng.uniform(spec[1], spec[2]), 2)
            elif spec[0] == "bool":
                base[k] = rng.random() < 0.5
            elif spec[0] == "sym":                       # seuil bas / haut symétriques (RSI)
                lo = rng.randint(spec[1], spec[2])
                base["rsi_lo"], base["rsi_hi"] = lo, 100 - lo
            elif spec[0] == "fenetre":                   # fenêtre d'ouverture (début, fin) en UTC
                base["start"], base["end"] = rng.choice(spec[1])
        for c in (classes or list(opt.CLASSES)):
            for sl in SL_ATR_FIN:
                for rr in RR_FIN:
                    for ses in (sessions if (sessions is not None and e in ("M1", "M5", "M15", "M30", "H1")) else [None]):
                        out.append({"strategy": st, "entry_tf": e, "trend_tf": t, "asset_class": c,
                                    "params": {**base, "sl_atr": sl, "rr": rr}, "sessions": ses, "fin": True})
    return out


def grid(strategies: Optional[list] = None, classes: Optional[list] = None, timeframes: Optional[list] = None,
         sessions: Optional[list] = None, fin: bool = False) -> list[dict]:
    """Liste des configurations : {strategy, entry_tf, trend_tf, asset_class, params}."""
    from ..backtest import fastsig
    out = []
    for st in (strategies or sorted(fastsig.FAST)):
        for (e, t) in (timeframes or TIMEFRAMES):
            for c in (classes or list(opt.CLASSES)):
                for prm in expand(st, fin):
                    # une bougie H4 / D1 n'a qu'une heure d'ouverture : les variantes de session n'y ont pas de sens (30/09)
                    for ses in (sessions if (sessions is not None and e in ("M1", "M5", "M15", "M30", "H1")) else [None]):
                        out.append({"strategy": st, "entry_tf": e, "trend_tf": t, "asset_class": c, "params": prm, "sessions": ses})
    return out


def univers_classes(markets: dict) -> dict:
    """Classes « univers » : chaque groupe de config/markets.yaml avec TOUS ses symboles (forex majeures + mineures)."""
    out = {"u_forex": list(markets.get("forex_majors", [])) + list(markets.get("forex_minors", []))}
    for g in ("indices", "metals", "energies", "crypto"):
        if markets.get(g):
            out["u_" + g] = list(markets[g])
    return {k: v for k, v in out.items() if v}


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
    grille = SL_ATR if cfg["params"]["sl_atr"] in SL_ATR and not cfg.get("fin") else SL_ATR_FIN
    i = grille.index(cfg["params"]["sl_atr"])
    voisines = [grille[j] for j in (i - 1, i + 1) if 0 <= j < len(grille)]
    reste = {k: v for k, v in cfg["params"].items() if k != "sl_atr"}
    gagnantes = 0
    for sl in voisines:
        m = index.get(_key(cfg, {**reste, "sl_atr": sl}))
        if m is not None and m["exp"] > 0 and m["pf"] > 1.0:
            gagnantes += 1
    return gagnantes >= min(min_voisins, len(voisines))


def _key(cfg: dict, params: dict) -> str:
    return json.dumps([cfg["strategy"], cfg["entry_tf"], cfg["trend_tf"], cfg["asset_class"], cfg.get("sessions"),
                       sorted(params.items())], default=str)


# ------------------------------------------------------------------------------------------------ calcul (processus fils)
_DATA: dict = {}
_SPECS: dict = {}
_COSTS: dict = {}
_MGMT: Optional[dict] = None
_SESS: dict = {}


def _init(data, specs, costs, mgmt):
    global _DATA, _SPECS, _COSTS, _MGMT
    _DATA, _SPECS, _COSTS, _MGMT = data, specs, costs, mgmt
    _SESS.clear()                                          # sessions par bougie : propres à ces données


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
    from .adapters import masque_sessions, sessions_bougies
    skey = (sym, entry_tf)
    if skey not in _SESS:
        _SESS[skey] = sessions_bougies(df["time"], entry_tf, str(ss.asset_class) == "crypto")
    sess = _SESS[skey]
    sides, sls, tps = [], [], []
    calcules: dict = {}
    for cfg in cfgs:
        pk = json.dumps(sorted(cfg["params"].items()), default=str)
        if pk in calcules:                                  # même paramètres, autre session : signaux déjà calculés
            s0, a0, b0 = calcules[pk]
            ses = cfg.get("sessions") or opt.SESSIONS
            s0 = np.where(masque_sessions(sess, ses, entry_tf), s0, 0).astype(np.int8)
            sides.append(s0); sls.append(a0); tps.append(b0)
            continue
        spec = AgentSpec(agent_id="MASS", family="X", name="mass", strategy=strategy, markets=[sym],
                         sessions=list(opt.SESSIONS) + ["SYDNEY"], timeframes={"entry": entry_tf, "trend": trend_tf},
                         regimes=list(opt.REGIMES.get(strategy, opt.ALL)), params=dict(cfg["params"]), base_strategy=strategy)
        f = make_signal_fn(spec, ss, entry_tf)
        f.prepare(df)
        fa = f.fast_arrays()
        if fa is None:
            fa = (np.zeros(n, dtype=np.int8), np.full(n, np.nan), np.full(n, np.nan))
        calcules[pk] = fa
        s0 = fa[0]
        ses = cfg.get("sessions") or opt.SESSIONS
        s0 = np.where(masque_sessions(sess, ses, entry_tf), s0, 0).astype(np.int8)
        sides.append(s0); sls.append(fa[1]); tps.append(fa[2])
    side, sl, tp = np.stack(sides), np.stack(sls), np.stack(tps)
    cost = _COSTS[sym]
    appr = gpu_sim.simulate(o, h, l, c, atr, side, sl, tp, cost.spread, cost.slippage, _MGMT, warmup=200, start=0, end=cut, device="cpu")
    ctrl = gpu_sim.simulate(o, h, l, c, atr, side, sl, tp, cost.spread, cost.slippage, _MGMT, warmup=0, start=cut, end=n, device="cpu")
    return [(appr[k], ctrl[k]) for k in range(len(cfgs))]


def _task_gpu(args) -> list:
    """Même résultat que `_task`, calculé sur la 3090 par PAQUETS : pour chaque réglage clé (paramètres hors stop et
    cible), tous les couples (sl_atr, rr) en un appel de la stratégie, variantes de session par masque, simulation sur la
    carte. Repli automatique sur `_task` si la stratégie n'est pas encore portée sur la carte."""
    import cupy as cp
    from ..agents.registry import AgentSpec
    from ..backtest import fastsig, gpu_sim
    from ..backtest.engine import _atr_causal
    from .adapters import make_signal_fn, masque_sessions, sessions_bougies
    sym, entry_tf, trend_tf, strategy, cfgs = args
    df = _DATA.get((sym, entry_tf))
    ss = _SPECS.get(sym)
    if df is None or ss is None or len(df) < 600:
        return [None] * len(cfgs)
    try:
        spec = AgentSpec(agent_id="MASS", family="X", name="mass", strategy=strategy, markets=[sym],
                         sessions=list(opt.SESSIONS) + ["SYDNEY"], timeframes={"entry": entry_tf, "trend": trend_tf},
                         regimes=list(opt.REGIMES.get(strategy, opt.ALL)), params=dict(cfgs[0]["params"]), base_strategy=strategy)
        f = make_signal_fn(spec, ss, entry_tf)
        f.prepare(df)
        ctx = f.fast_ctx()
        if ctx is None:
            return _task(args)
        g = ctx.on(cp)
        n = len(df)
        cut = int(n * (1 - HOLDOUT))
        h_np, l_np, c_np = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
        o, h, l, c = (cp.asarray(df[k].to_numpy(dtype=float)) for k in ("open", "high", "low", "close"))
        atr = cp.asarray(_atr_causal(h_np, l_np, c_np))
        skey = (sym, entry_tf)
        if skey not in _SESS:
            _SESS[skey] = sessions_bougies(df["time"], entry_tf, str(ss.asset_class) == "crypto")
        sess = _SESS[skey]
        masques: dict = {}
        cost = _COSTS[sym]
        groupes: dict = {}
        for j, cfg in enumerate(cfgs):
            cle = json.dumps(sorted((k, v) for k, v in cfg["params"].items() if k not in ("sl_atr", "rr")), default=str)
            groupes.setdefault(cle, []).append(j)
        out: list = [None] * len(cfgs)
        for cle, idx in groupes.items():
            base = {k: v for k, v in cfgs[idx[0]]["params"].items() if k not in ("sl_atr", "rr")}
            paires = sorted({(cfgs[j]["params"]["sl_atr"], cfgs[j]["params"]["rr"]) for j in idx})
            col = {pr: k for k, pr in enumerate(paires)}
            sl_c = cp.asarray([pr[0] for pr in paires], dtype=cp.float64)[:, None]
            rr_c = cp.asarray([pr[1] for pr in paires], dtype=cp.float64)[:, None]
            s_, sl_, tp_ = fastsig.FAST[strategy](g, {**base, "sl_atr": sl_c, "rr": rr_c})
            P = len(paires)
            s_ = cp.broadcast_to(s_, (P, n))
            sl_ = cp.broadcast_to(sl_, (P, n))
            tp_ = cp.broadcast_to(tp_, (P, n))
            lignes_s, lignes_sl, lignes_tp = [], [], []
            for j in idx:
                k = col[(cfgs[j]["params"]["sl_atr"], cfgs[j]["params"]["rr"])]
                ses = cfgs[j].get("sessions") or opt.SESSIONS
                sd = s_[k]
                mk = tuple(ses)
                if mk not in masques:
                    masques[mk] = cp.asarray(masque_sessions(sess, ses, entry_tf))
                sd = cp.where(masques[mk], sd, 0).astype(cp.int8)
                lignes_s.append(sd)
                lignes_sl.append(sl_[k])
                lignes_tp.append(tp_[k])
            S, SL, TP = cp.stack(lignes_s), cp.stack(lignes_sl), cp.stack(lignes_tp)
            appr = gpu_sim.simulate_device(o, h, l, c, atr, S, SL, TP, cost.spread, cost.slippage, _MGMT, warmup=200, start=0, end=cut)
            ctrl = gpu_sim.simulate_device(o, h, l, c, atr, S, SL, TP, cost.spread, cost.slippage, _MGMT, warmup=0, start=cut, end=n)
            for r, j in enumerate(idx):
                out[j] = (appr[r], ctrl[r])
        return out
    except Exception:  # noqa: BLE001 - stratégie pas encore portée sur la carte : calcul processeur, même résultat
        return _task(args)


def _task_marche(args) -> list:
    """Un marché (symbole, unité de temps) avec TOUTES ses stratégies : le processus ne reçoit que cet historique
    (2026-09-30 : chaque processus gardait les 365 historiques en mémoire, 5,6 Go pour 12 processus) et vide ses caches
    ensuite. Renvoie une liste de sorties par sous-tâche (stratégie)."""
    sym, entry_tf, df, ss, cost, device, sous = args[:7]
    global HOLDOUT
    if len(args) > 7:
        HOLDOUT = float(args[7])
    _DATA.clear()
    _SPECS.clear()
    _COSTS.clear()
    _SESS.clear()
    _DATA[(sym, entry_tf)] = df
    _SPECS[sym] = ss
    _COSTS[sym] = cost
    fn = _task_gpu if device == "gpu" else _task
    try:
        return [fn((sym, entry_tf, trend_tf, strategy, cfgs)) for trend_tf, strategy, cfgs in sous]
    finally:
        from . import adapters as _ad
        for cache in (_ad._ENRICH_CACHE, _ad._REGIME_CACHE, _ad._SESSION_CACHE, _ad._CTX_CACHE):
            cache.clear()
        _DATA.clear()


# ------------------------------------------------------------------------------------------------ pilotage
def select(configs: list[dict], results: dict, min_trades: int = 60, m_cumul: int = 0) -> tuple[list, dict]:
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
    t_min = seuil_multiple(len(rows) + int(m_cumul))
    etapes = {"testees": len(rows), "t_min": round(t_min, 2), "m_cumul": int(m_cumul) + len(rows)}
    s1 = [x for x in rows if x[1]["n"] >= min_trades and x[1]["exp"] > 0 and x[1]["pf"] >= 1.2 and x[1]["t"] >= t_min]
    etapes["significatives"] = len(s1)
    s2 = [x for x in s1 if plateau_ok(x[0], index)]
    etapes["plateau"] = len(s2)
    s3 = [x for x in s2 if x[2]["n"] >= 15 and x[2]["exp"] > 0 and x[2]["pf"] >= 1.1]
    etapes["controle"] = len(s3)
    s3.sort(key=lambda x: -(x[2]["exp"] * math.sqrt(min(x[2]["n"], 200)) + x[1]["t"] * 0.1))
    # les 50 meilleures sur l'apprentissage, retenues ou non (où est-on passé près ?)
    etapes["top50"] = [{"config": c, "apprentissage": a, "controle": k}
                       for c, a, k in sorted([x for x in rows if x[1]["n"] >= min_trades], key=lambda x: -x[1]["t"])[:50]]
    return s3, etapes


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - processus de calcul
    import multiprocessing as mp

    from ..agents.registry import AgentRegistry
    from ..backtest.engine import BTCosts
    from ..core.config import load_dotenv, load_settings
    from ..core.journal import Journal
    from ..mt5.mock_adapter import make_broker
    from ..mt5.symbols import resolve_symbols
    from .rates_cache import TerminalOccupe

    ap = argparse.ArgumentParser(description="Recherche en masse de stratégies")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--classes", default="")
    ap.add_argument("--strategies", default="")
    ap.add_argument("--max-configs", type=int, default=0)
    # 2026-09-29, demande utilisateur : « toutes les paires et tous les marchés disponibles »
    ap.add_argument("--univers", action="store_true", help="toutes les classes et tous les symboles de config/markets.yaml")
    ap.add_argument("--sessions", action="store_true", help="décliner chaque configuration par session (Asie, Londres, NY, chevauchement)")
    ap.add_argument("--device", default="cpu", choices=["cpu", "gpu"], help="gpu : signaux et simulation sur la carte (3090)")
    ap.add_argument("--fin", action="store_true", help="grille fine (stops, cibles, réglages clés)")
    ap.add_argument("--timeframes", default="", help="ex. M5:M15,M15:H4,H1:D1")
    ap.add_argument("--toutes-ut", action="store_true", help="toutes les unités de temps (M1 … W1, MN1 en tendance)")
    ap.add_argument("--ut-reelles", action="store_true", help="vraies barres H2 / W1 du terminal (sinon construites depuis H1 / D1)")
    ap.add_argument("--cache-age-h", type=float, default=0.0, help="réutiliser les historiques en cache de moins de N heures")
    ap.add_argument("--etiquette", default="", help="nom du passage (rapport, journal)")
    ap.add_argument("--tirage", type=int, default=0, help="nombre de réglages clés tirés au hasard (recherche continue)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--m-cumul", type=int, default=0, help="configurations déjà testées (seuil cumulatif)")
    ap.add_argument("--holdout", type=float, default=0.30, help="part la plus récente de chaque historique réservée au contrôle")
    args = ap.parse_args(argv)
    globals()["HOLDOUT"] = float(args.holdout)
    s = load_settings()
    load_dotenv(s.home / ".env")
    journal = Journal(s.logs_dir, s.system.get("timezone_local", "UTC"), component="massive")
    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), s)
    if not broker.connect():
        print("terminal indisponible")
        return 1
    t0 = time.time()
    if args.univers:
        for nom, syms in univers_classes(s.markets).items():
            opt.CLASSES_ALL[nom] = syms
        classes = list(univers_classes(s.markets))
    else:
        classes = [c for c in args.classes.split(",") if c] or list(opt.CLASSES)
    strategies = [x for x in args.strategies.split(",") if x] or None
    tfs_args = [tuple(x.split(":")) for x in args.timeframes.split(",") if x] or None
    if args.toutes_ut:
        tfs_args = list(TF_TOUTES)
    if args.tirage:
        configs = tirage(args.tirage, args.seed, strategies, classes, SESSION_VARIANTS if args.sessions else None)
    else:
        configs = grid(strategies, classes, tfs_args, sessions=SESSION_VARIANTS if args.sessions else None, fin=args.fin)
    for cfg in configs:
        cfg["fin"] = bool(args.fin)
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
                df = charger_ut(broker, reel, tf, s.data_dir / "cache" / "rates", state_file=s.state_dir / "system_state.json",
                                max_cache_age_sec=(args.cache_age_h * 3600) if args.cache_age_h else None,
                                reelles=args.ut_reelles)
            except TerminalOccupe as e:
                journal.warn("recherche en masse arrêtée : terminal occupé", error=str(e))
                return 2
            if df is not None and len(df):
                data[(sym, tf)] = df
            else:
                # 30/09 : un historique vide (ex. demande au-delà de la limite du terminal) ne doit plus passer inaperçu
                journal.warn("historique vide : unité de temps ignorée pour ce marché", symbol=sym, timeframe=tf, bars=BARS[tf])
    journal.event("massive_start", configurations=len(configs), jeux=len(data), workers=args.workers)
    # tâches : (symbole, cadre, stratégie) avec toutes ses configurations
    groupes: dict = {}
    for i, cfg in enumerate(configs):
        for sym in opt.CLASSES_ALL[cfg["asset_class"]]:
            groupes.setdefault((sym, cfg["entry_tf"], cfg["trend_tf"], cfg["strategy"]), []).append(i)
    # une tâche par marché (symbole, unité de temps) : l'historique n'est envoyé qu'au processus qui le traite
    par_marche: dict = {}
    for k, idx in groupes.items():
        par_marche.setdefault((k[0], k[1]), []).append((k[2], k[3], idx))
    taches, cles = [], []
    for (sym, tf), lst in par_marche.items():
        df = data.get((sym, tf))
        if df is None or sym not in specs:
            continue
        taches.append((sym, tf, df, specs[sym], costs[sym], args.device, [(t, st, [configs[i] for i in idx]) for t, st, idx in lst], HOLDOUT))
        cles.append([idx for _, _, idx in lst])
    mgmt = dict(s.profit_management) if bool(bt.get("use_position_management", True)) else None
    results: dict = {}
    del data                                        # les historiques partent avec les tâches, un par processus
    with mp.Pool(processes=args.workers, initializer=_init, initargs=({}, {}, {}, mgmt), maxtasksperchild=20) as pool:
        for idx_marche, res_marche in zip(cles, pool.imap(_task_marche, taches, chunksize=1)):
            for idx, res in zip(idx_marche, res_marche):
                for i, r in zip(idx, res):
                    if r is not None:
                        results.setdefault(i, []).append(r)
    retenues, etapes = select(configs, results, m_cumul=args.m_cumul)
    reg0 = AgentRegistry(status_file=s.data_dir / "agent_status.json")
    vus = set()
    for a in reg0.agents.values():
        if a.agent_id.startswith("X"):
            vus.add(_cle_idee(a.base_strategy or a.strategy, a.timeframes.get("entry"), a.markets, a.sessions, a.params))
    fp = s.state_dir / "agent_proposals.jsonl"
    if fp.exists():
        for ligne in fp.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(ligne)
                vus.add(_cle_idee(d.get("base_strategy") or d["strategy"], d["timeframes"]["entry"], d["markets"], d["sessions"],
                                  d.get("params")))
            except (ValueError, KeyError):
                pass
    distinctes = []
    for x in retenues:
        c0 = x[0]
        ses = sorted(c0.get("sessions") or opt.SESSIONS)
        cle_reg = _cle_idee(c0["strategy"], c0["entry_tf"], opt.CLASSES_ALL[c0["asset_class"]], ses, c0.get("params"))
        if cle_reg in vus:
            continue
        vus.add(cle_reg)
        distinctes.append(x)
    retenues = distinctes[: args.top]
    reg = AgentRegistry(status_file=s.data_dir / "agent_status.json")
    ids = opt.next_ids(set(reg.agents), len(retenues))
    props = []
    for (cfg, a, k), aid in zip(retenues, ids):
        c = opt.Config(cfg["strategy"], cfg["entry_tf"], cfg["trend_tf"], cfg["params"]["sl_atr"], cfg["params"]["rr"], cfg["asset_class"])
        p = opt.proposal_spec(c, aid, {"profit_factor": a["pf"], "sample_size": a["n"], "expectancy_r": a["exp"]},
                              {"robustness_ratio": 0.0, "oos_expectancy_r": k["exp"]}, "SHADOW")
        p["params"] = {**p["params"], **cfg["params"]}
        if cfg.get("sessions"):
            p["sessions"] = list(cfg["sessions"])
            p["name"] = p["name"] + "_" + "_".join(x.lower() for x in cfg["sessions"])
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
    out = s.home / "reports" / f"massive_{args.etiquette + '_' if args.etiquette else ''}{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rapport, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    resume = {k: v for k, v in etapes.items() if k != "top50"}
    journal.event("massive_done", **resume, retenues=len(props), rapport=str(out), duree_sec=rapport["duree_sec"])
    print(f"{resume} → {len(props)} propositions ({out})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
