"""Qualité mesurée du trading (plan pro du 2026-09-25, points 2, 5 et 8).

- `agent_quality` : par agent, espérance en R avec sa marge d'incertitude (intervalle à 95 %), taux de réussite,
  profit factor, excursions favorable / défavorable moyennes (MFE / MAE), coût d'entrée et glissement mesurés.
- `freeze_status` : avancement du gel des réglages (N trades live sur la version en cours / trades requis).
- `daily_report_text` : le rapport envoyé sur Telegram à la clôture de la journée FOXX (17 h New York).

Lecture seule de `learning.db` : rien ici ne modifie une décision de trading.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

#: intervalle de confiance à 95 % (loi normale ; prudent tant que n est petit, on ne conclut qu'à n >= 10)
Z95 = 1.96
MIN_N_CONCLUSION = 10


def _rows(db: Path, since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    try:
        q = ("SELECT agent_id, symbol, result_r, pnl, mfe_r, mae_r, features, closed_at, opened_at FROM trades "
             "WHERE mode='live' AND exit_reason != 'UNKNOWN'")
        args: list = []
        if since:
            q += " AND closed_at >= ?"; args.append(since)
        if until:
            q += " AND closed_at < ?"; args.append(until)
        out = []
        for aid, sym, r, pnl, mfe, mae, feats, closed, opened in con.execute(q, args):
            try:
                f = json.loads(feats) if feats else {}
            except (TypeError, ValueError):
                f = {}
            out.append({"agent_id": aid, "symbol": sym, "r": float(r or 0.0), "pnl": float(pnl or 0.0),
                        "mfe": float(mfe or 0.0), "mae": float(mae or 0.0), "features": f if isinstance(f, dict) else {},
                        "closed_at": closed or "", "opened_at": opened or ""})
        return out
    finally:
        con.close()


def summarize(trades: Iterable[dict]) -> dict:
    t = list(trades)
    n = len(t)
    if n == 0:
        return {"n": 0}
    rs = [x["r"] for x in t]
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    marge = Z95 * sd / math.sqrt(n) if n > 1 else float("inf")
    gains = sum(r for r in rs if r > 0)
    pertes = -sum(r for r in rs if r < 0)
    couts = [float(x["features"]["cost_ratio"]) for x in t if x["features"].get("cost_ratio") is not None]
    gliss = [float(x["features"]["slippage_r"]) for x in t if x["features"].get("slippage_r") is not None]
    bas, haut = mean - marge, mean + marge
    if n < MIN_N_CONCLUSION:
        verdict = "trop peu de trades"
    elif bas > 0:
        verdict = "avantage prouvé"
    elif haut < 0:
        verdict = "perdant prouvé"
    else:
        verdict = "pas encore concluant"
    return {"n": n, "total_r": round(sum(rs), 2), "pnl": round(sum(x["pnl"] for x in t), 2),
            "expectancy_r": round(mean, 3), "ic_bas": round(bas, 3) if n > 1 else None,
            "ic_haut": round(haut, 3) if n > 1 else None, "win_rate": round(100.0 * sum(1 for r in rs if r > 0) / n, 1),
            "profit_factor": round(gains / pertes, 2) if pertes > 0 else None,
            "mfe_moy": round(sum(x["mfe"] for x in t) / n, 2), "mae_moy": round(sum(x["mae"] for x in t) / n, 2),
            "cout_moy_pct": round(100.0 * sum(couts) / len(couts), 1) if couts else None,
            "glissement_moy_r": round(sum(gliss) / len(gliss), 3) if gliss else None, "verdict": verdict}


def agent_quality(db: Path, since: Optional[str] = None) -> dict:
    """{"global": résumé, "agents": [{agent_id, ...résumé}] triés par espérance décroissante}."""
    rows = _rows(db, since)
    par: dict[str, list] = {}
    for r in rows:
        par.setdefault(r["agent_id"], []).append(r)
    agents = [{"agent_id": a, **summarize(t)} for a, t in par.items()]
    agents.sort(key=lambda x: (x.get("expectancy_r") or 0.0), reverse=True)
    return {"global": summarize(rows), "agents": agents}


def freeze_status(db: Path, gel: Optional[dict]) -> Optional[dict]:
    """Avancement du gel des réglages : trades live clôturés depuis le début de la version gelée."""
    if not gel or not gel.get("depuis"):
        return None
    requis = int(gel.get("trades_requis", 100))
    n = len(_rows(db, since=str(gel["depuis"])))
    return {"version": str(gel.get("version", "")), "depuis": str(gel["depuis"]), "trades": n, "requis": requis,
            "restant": max(0, requis - n), "termine": n >= requis}


def daily_report_text(db: Path, start: datetime, end: datetime, accounts: list[dict], alerts: list[str],
                      gel: Optional[dict], day_label: str) -> str:
    """Rapport de fin de journée FOXX. `accounts` : [{"nom", "pnl", "trades"}] ; `alerts` : raisons distinctes."""
    rows = _rows(db, since=start.isoformat(), until=end.isoformat())
    s = summarize(rows)
    lignes = [f"Journée {day_label} (clôture 17 h New York)"]
    if s["n"] == 0:
        lignes.append("Maître : aucun trade clôturé.")
    else:
        lignes.append(f"Maître : {s['n']} trades · {s['total_r']:+.2f} R · {s['pnl']:+,.0f} $ · réussite {s['win_rate']:.0f} %"
                      .replace(",", " "))
        if s.get("cout_moy_pct") is not None:
            lignes.append(f"Coût d'entrée moyen : {s['cout_moy_pct']:.0f} % du risque")
        if s.get("glissement_moy_r") is not None:
            lignes.append(f"Glissement moyen : {s['glissement_moy_r']:+.3f} R")
    for a in accounts:
        lignes.append(f"{a['nom']} : {a.get('trades', 0)} trades · {float(a.get('pnl', 0.0)):+,.0f} $".replace(",", " "))
        for ecart in (a.get("ecarts") or [])[:4]:
            lignes.append(f"  écart : {ecart}")
    par: dict[str, float] = {}
    for r in rows:
        par[r["agent_id"]] = par.get(r["agent_id"], 0.0) + r["r"]
    if par:
        tri = sorted(par.items(), key=lambda kv: kv[1], reverse=True)
        lignes.append("Meilleurs : " + ", ".join(f"{a} {v:+.2f} R" for a, v in tri[:3]))
        pires = [kv for kv in tri[::-1] if kv[1] < 0][:3]
        if pires:
            lignes.append("Pires : " + ", ".join(f"{a} {v:+.2f} R" for a, v in pires))
    lignes.append("Alertes : " + ("; ".join(alerts[:5]) if alerts else "aucune"))
    fs = freeze_status(db, gel)
    if fs:
        etat = "terminé, décision possible" if fs["termine"] else f"encore {fs['restant']} trades"
        lignes.append(f"Gel des réglages {fs['version']} : {fs['trades']}/{fs['requis']} trades ({etat})")
    return "\n".join(lignes)
