"""Rejeu de la semaine écoulée avec d'autres réglages (plan pro du 2026-09-25, point 6).

Avant toute modification de trading, on rejoue les candidats RÉELS qui ont atteint le gate pendant la période, on
reprend leur décision avec les réglages proposés, puis on simule chaque trade retenu barre par barre avec la gestion
du bot (break-even, TP1/TP2 partiels, stop suiveur, TP broker). Les deux configurations — actuelle et proposée — sont
simulées de la même façon : c'est leur ÉCART qui compte, pas le niveau absolu.

Limites assumées (écrites dans le rapport) : la revue IA n'est pas rejouée (seuls les candidats qu'elle a approuvés
atteignaient le gate) ; le coût d'entrée mesuré (spread + commission) est déduit de chaque trade ; dans une barre qui touche à la fois le stop et l'objectif,
le stop est supposé touché d'abord (prudent) ; une seule position simulée par symbole à la fois.

Usage (labo en marche, lecture seule des prix du terminal) :
    python -m tradinglab.research.replay --jours 7 --reglages proposition.yaml
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

_COUT_RE = re.compile(r"=\s*(\d+(?:[.,]\d+)?)\s*%\s*du risque")


@dataclass
class ReplayCandidate:
    ts: str
    key: str
    symbol: str
    side: int                     # +1 achat, -1 vente
    agent_id: str
    entry: float
    sl: float
    tp: Optional[float]
    atr: float
    checks: dict = field(default_factory=dict)      # nom -> (ok, détail)
    cost_ratio: Optional[float] = None               # (spread + commission) / risque, lu dans le contrôle 07b

    @property
    def failed(self) -> list[str]:
        return [n for n, (ok, _) in self.checks.items() if not ok]


def _cout(detail: str) -> Optional[float]:
    m = _COUT_RE.search(detail or "")
    return float(m.group(1).replace(",", ".")) / 100.0 if m else None


def load_candidates(journal_dir: Path, since: datetime, until: datetime) -> list[ReplayCandidate]:
    """Première décision du gate pour chaque candidat distinct (agent, symbole, sens, barre) de la période."""
    cands_by_id: dict[str, dict] = {}
    gates: list[dict] = []
    jour = since.date()
    while jour <= until.date():
        f = journal_dir / f"journal-{jour.isoformat()}.jsonl"
        if f.exists():
            with f.open(encoding="utf-8") as fh:
                for ligne in fh:
                    est_gate = '"kind": "gate"' in ligne
                    if not est_gate and '"kind": "candidate"' not in ligne:
                        continue
                    try:
                        ev = json.loads(ligne)
                    except ValueError:
                        continue
                    ts = str(ev.get("ts_utc", ""))
                    if not (since.isoformat() <= ts < until.isoformat()):
                        continue
                    if est_gate:
                        gates.append(ev)
                    else:
                        c = ev.get("candidate") or {}
                        if c.get("id"):
                            cands_by_id[str(c["id"])] = c
        jour += timedelta(days=1)
    out: dict[str, ReplayCandidate] = {}
    for g in gates:
        c = cands_by_id.get(str(g.get("candidate_id")), {})
        agent = g.get("agent_id") or c.get("agent_id")
        side = g.get("side") or c.get("side")
        entry = g.get("entry") if g.get("entry") is not None else c.get("entry")
        sl = g.get("sl") if g.get("sl") is not None else c.get("sl")
        tp = g.get("tp") if g.get("tp") is not None else ((c.get("tp_plan") or [None])[-1])
        atr = g.get("atr") if g.get("atr") is not None else c.get("atr")
        if not (agent and side and entry and sl):
            continue
        key = g.get("key") or f"{g.get('symbol')}|{side}|{agent}|{c.get('bar_time')}"
        if key in out:
            continue
        checks = {str(ch.get("name")): (bool(ch.get("ok")), str(ch.get("detail", ""))) for ch in g.get("checks") or []}
        out[key] = ReplayCandidate(ts=str(g.get("ts_utc")), key=key, symbol=str(g.get("symbol")),
                                   side=1 if str(side).upper() == "BUY" else -1, agent_id=str(agent),
                                   entry=float(entry), sl=float(sl), tp=float(tp) if tp else None,
                                   atr=float(atr or 0.0), checks=checks,
                                   cost_ratio=_cout(checks.get("07b_spread_vs_sl", (True, ""))[1]))
    return sorted(out.values(), key=lambda x: x.ts)


def decide(c: ReplayCandidate, reglages: dict) -> bool:
    """Le candidat serait-il passé avec ces réglages ? Seuls les contrôles réglables sont réévalués ; tout autre
    refus réel (news, perte du jour, corrélation…) reste un refus."""
    if c.agent_id in set(reglages.get("agents_exclus") or []):
        return False
    desactives = set(reglages.get("gate_checks_disabled") or [])
    seuil = reglages.get("max_spread_sl_ratio")
    for nom in c.failed:
        if nom in desactives:
            continue
        if nom == "07b_spread_vs_sl" and seuil is not None and c.cost_ratio is not None and c.cost_ratio <= float(seuil):
            continue
        return False
    if ("07b_spread_vs_sl" not in desactives and seuil is not None and c.cost_ratio is not None
            and c.cost_ratio > float(seuil)):
        return False                       # seuil plus strict que celui du jour : le contrôle aurait refusé
    return True


def simulate(c: ReplayCandidate, bars, pm: dict) -> tuple[float, Optional[object]]:
    """Résultat en R d'un trade simulé sur les barres qui suivent son entrée, et l'instant de sortie."""
    dist = abs(c.entry - c.sl)
    if dist <= 0:
        return 0.0, None
    d = c.side
    sl = c.sl
    reste, realise = 1.0, 0.0
    tp1_r, tp1_p = float(pm.get("tp1_r", 1.5)), float(pm.get("tp1_close_percent", 30)) / 100.0
    tp2_r, tp2_p = float(pm.get("tp2_r", 2.5)), float(pm.get("tp2_close_percent", 40)) / 100.0
    be_r, off = float(pm.get("break_even_r", 1.0)), float(pm.get("break_even_offset_r", 0.05))
    tr_r, tr_m = float(pm.get("trailing_start_r", 2.0)), float(pm.get("trailing_atr_multiplier", 1.5))
    tp1 = tp2 = False
    meilleur = c.entry
    r_de = lambda px: (px - c.entry) * d / dist  # noqa: E731
    for row in bars.itertuples(index=False):
        haut, bas, t = float(row.high), float(row.low), row.time
        defavorable, favorable = (bas, haut) if d > 0 else (haut, bas)
        if (defavorable - sl) * d <= 0:                                   # stop touché (supposé d'abord)
            return realise + reste * r_de(sl), t
        if c.tp and (favorable - c.tp) * d >= 0:                          # TP broker : tout le reste
            return realise + reste * r_de(c.tp), t
        r_max = r_de(favorable)
        if not tp1 and r_max >= tp1_r:
            realise += tp1_p * tp1_r; reste -= tp1_p; tp1 = True
        if tp1 and not tp2 and r_max >= tp2_r:
            realise += tp2_p * tp2_r; reste -= tp2_p; tp2 = True
        meilleur = max(meilleur, favorable) if d > 0 else min(meilleur, favorable)
        r_best = r_de(meilleur)
        plancher = c.entry + d * off * dist
        if r_best >= be_r and (plancher - sl) * d > 0:
            sl = plancher
        if r_best >= tr_r and c.atr > 0:
            suivi = meilleur - d * tr_m * c.atr
            suivi = max(suivi, plancher) if d > 0 else min(suivi, plancher)
            if (suivi - sl) * d > 0:
                sl = suivi
    if len(bars):
        return realise + reste * r_de(float(bars.iloc[-1]["close"])), None
    return 0.0, None


def run(cands: list[ReplayCandidate], reglages: dict, pm: dict, bars_for: Callable[[str], object]) -> dict:
    """Décide et simule tous les candidats ; une seule position simulée par symbole à la fois."""
    import pandas as pd

    occupe: dict[str, object] = {}
    trades = []
    for c in cands:
        if not decide(c, reglages):
            continue
        t0 = pd.Timestamp(c.ts)
        libre = occupe.get(c.symbol)
        if libre is not None and (libre is True or t0 < libre):
            continue
        df = bars_for(c.symbol)
        if df is None or len(df) == 0:
            continue
        suite = df[df["time"] > t0]
        if len(suite) == 0:
            continue
        r, fin = simulate(c, suite, pm)
        # coût d'entrée (spread + commission, en part du risque) déduit : sans lui, les trades chers que laisse passer
        # un seuil plus large paraissaient gratuits (premier rejeu du 2026-09-25)
        r -= float(c.cost_ratio or 0.0)
        occupe[c.symbol] = fin if fin is not None else True
        trades.append({"agent_id": c.agent_id, "symbol": c.symbol, "r": round(r, 3), "ts": c.ts})
    rs = [t["r"] for t in trades]
    courbe, pic, dd = 0.0, 0.0, 0.0
    for r in rs:
        courbe += r; pic = max(pic, courbe); dd = max(dd, pic - courbe)
    return {"trades": len(rs), "total_r": round(sum(rs), 2),
            "win_rate": round(100.0 * sum(1 for r in rs if r > 0) / len(rs), 1) if rs else 0.0,
            "max_dd_r": round(dd, 2), "detail": trades}


def compare(cands, actuels: dict, proposes: dict, pm_actuel: dict, pm_propose: dict, bars_for) -> dict:
    return {"actuel": run(cands, actuels, pm_actuel, bars_for), "propose": run(cands, proposes, pm_propose, bars_for)}


def _texte(res: dict, jours: int, n: int) -> str:
    a, p = res["actuel"], res["propose"]
    lignes = [f"Rejeu des {jours} derniers jours — {n} candidats distincts passés par le gate",
              f"{'':12}{'trades':>8}{'total R':>10}{'réussite':>10}{'DD max R':>10}"]
    for nom, x in (("actuel", a), ("proposé", p)):
        lignes.append(f"{nom:12}{x['trades']:>8}{x['total_r']:>10.2f}{x['win_rate']:>9.0f}%{x['max_dd_r']:>10.2f}")
    lignes.append(f"écart proposé − actuel : {p['total_r'] - a['total_r']:+.2f} R")
    lignes.append("Coût d'entrée (spread + commission) déduit. Limites : revue IA non rejouée, stop supposé touché avant l'objectif "
                  "dans une même barre, une position simulée par symbole.")
    return "\n".join(lignes)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - outil en ligne de commande
    import yaml

    from ..core.config import load_dotenv, load_settings
    from ..mt5.mock_adapter import make_broker
    import os

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jours", type=int, default=7)
    ap.add_argument("--reglages", type=Path, help="YAML : gate_checks_disabled, max_spread_sl_ratio, agents_exclus, "
                                                   "profit_management (surcharges)")
    args = ap.parse_args(argv)
    s = load_settings()
    load_dotenv(s.home / ".env")
    ex = s.execution
    actuels = {"gate_checks_disabled": list(ex.get("gate_checks_disabled") or []),
               "max_spread_sl_ratio": ex.get("max_spread_sl_ratio"), "agents_exclus": []}
    pm_actuel = dict(s.profit_management)
    surcharge = yaml.safe_load(args.reglages.read_text(encoding="utf-8")) if args.reglages else {}
    proposes = {**actuels, **{k: v for k, v in (surcharge or {}).items() if k != "profit_management"}}
    pm_propose = {**pm_actuel, **((surcharge or {}).get("profit_management") or {})}
    now = datetime.now(timezone.utc)
    cands = load_candidates(s.home / "logs", now - timedelta(days=args.jours), now)
    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), s)
    if not broker.connect():
        print("terminal indisponible")
        return 1
    cache: dict = {}

    def bars_for(sym: str):
        if sym not in cache:
            cache[sym] = broker.rates(sym, "M5", 12 * 24 * (args.jours + 1))
        return cache[sym]

    res = compare(cands, actuels, proposes, pm_actuel, pm_propose, bars_for)
    print(_texte(res, args.jours, len(cands)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
