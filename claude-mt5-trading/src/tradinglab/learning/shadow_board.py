"""Tableau « Shadow & recherche » du dashboard (2026-09-30, demande utilisateur : « les tableaux sur le dashboard
pour que je puisse suivre »).

Lecture seule : `data/learning.db` (trades shadow clôturés), `data/agent_status.json` (statuts), `state/shadow_positions.json`
(positions virtuelles ouvertes), `state/recherche_continue.json` et `reports/massive_*.json` (recherche en masse).
Les critères affichés sont ceux du pipeline (`research/pipeline.py`) : étape SHADOW = au moins `min_shadow` trades
shadow avec espérance > 0 et PF ≥ 1 ; puis revue statistique (échantillon total ≥ min_sample_size, PF ≥ min_profit_factor,
espérance ≥ min_expectancy_r), revue de risque, promotion.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def famille(agent_id: str) -> str:
    m = re.match(r"[A-Z]+", agent_id or "")
    return m.group(0) if m else "?"


FAMILLES = {"A": "scanners", "B": "tendance", "C": "cassure", "D": "pullback", "E": "retournement", "F": "structure",
            "G": "volatilité", "K": "annonces", "L": "par classe", "M": "par symbole", "N": "saisonnalité",
            "O": "cycle long", "P": "optimiseur", "Q": "ordres en attente", "R": "optimiseur 2", "S": "micro M1",
            "X": "recherche GPU", "CH": "challengers", "OR": "or (algorithme dédié)"}


def _infos_agents(home: Path) -> dict[str, str]:
    """« stratégie UT » de chaque agent du registre (vide si le registre est illisible : l'affichage ne tombe jamais)."""
    try:
        from ..agents.registry import AgentRegistry

        reg = AgentRegistry(status_file=home / "data" / "agent_status.json")
        return {k: f"{a.base_strategy or a.strategy} {a.timeframes.get('entry', '')}".strip() for k, a in reg.agents.items()}
    except Exception:  # noqa: BLE001
        return {}


def verdict(n: int, pf: Optional[float], exp: float, min_shadow: int, pf_live: float, exp_live: float,
            perdant_n: int = 50, perdant_pf: float = 0.8) -> str:
    if n >= perdant_n and pf is not None and pf < perdant_pf:
        return "perdant"
    if n < min_shadow:
        return "en cours"
    if pf is not None and pf >= pf_live and exp >= exp_live:
        return "prêt pour revue live"
    if (pf is None or pf >= 1.0) and exp > 0:
        return "shadow validé"
    return "pas concluant"


def shadow_board(home: Path, strategies_cfg: Optional[dict] = None) -> dict:
    cfg = strategies_cfg or {}
    n_req = int(cfg.get("min_sample_size", 40))
    min_shadow = int(cfg.get("min_shadow_trades", max(20, n_req // 2)))
    pf_live = float(cfg.get("min_profit_factor", 1.2))
    exp_live = float(cfg.get("min_expectancy_r", 0.10))
    deg = cfg.get("degradation") or {}
    perdant_n = int(deg.get("shadow_suspend_min_trades", 50) or 50)
    perdant_pf = float(deg.get("shadow_suspend_max_profit_factor", 0.8) or 0.8)
    status = (_read_json(home / "data" / "agent_status.json", {}) or {}).get("status", {}) or {}

    ouvertes: dict[str, int] = {}
    pos = _read_json(home / "state" / "shadow_positions.json", {})
    pos = pos.get("positions", pos) if isinstance(pos, dict) else pos
    for p in (pos.values() if isinstance(pos, dict) else pos or []):
        if isinstance(p, dict):
            a = str(p.get("agent_id") or "?")
            ouvertes[a] = ouvertes.get(a, 0) + 1

    infos = _infos_agents(home)
    agents: list[dict] = []
    db = home / "data" / "learning.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select agent_id, max(strategy), count(*), sum(result_r), "
                "sum(case when result_r > 0 then result_r else 0 end), "
                "-sum(case when result_r < 0 then result_r else 0 end), sum(result_r > 0), max(closed_at) "
                "from trades where mode = 'shadow' and result_r is not null group by agent_id").fetchall()
        finally:
            con.close()
        for a, strat, n, r, g, l, w, last in rows:
            pf = (g / l) if l else None
            exp = (r / n) if n else 0.0
            agents.append({"agent_id": a, "famille": famille(a), "strategie": infos.get(a, strat or ""), "statut": status.get(a, "?"),
                           "n": int(n), "r": round(r or 0.0, 2), "pf": round(pf, 2) if pf is not None else None,
                           "esperance": round(exp, 3), "reussite": round(100.0 * (w or 0) / n, 1) if n else 0.0,
                           "ouvertes": ouvertes.get(a, 0), "dernier": last,
                           "progression": min(100, round(100 * n / min_shadow)),
                           "verdict": verdict(int(n), pf, exp, min_shadow, pf_live, exp_live, perdant_n, perdant_pf),
                           "_g": float(g or 0.0), "_l": float(l or 0.0), "_w": int(w or 0)})
    vus = {x["agent_id"] for x in agents}
    for a, k in ouvertes.items():                          # agents qui n'ont encore que des positions ouvertes
        if a not in vus:
            agents.append({"agent_id": a, "famille": famille(a), "strategie": infos.get(a, ""), "statut": status.get(a, "?"), "n": 0,
                           "r": 0.0, "pf": None, "esperance": 0.0, "reussite": 0.0, "ouvertes": k, "dernier": None,
                           "progression": 0, "verdict": "en cours", "_g": 0.0, "_l": 0.0, "_w": 0})
    agents.sort(key=lambda x: -x["r"])

    fams: dict[str, dict] = {}
    for x in agents:
        f = fams.setdefault(x["famille"], {"famille": x["famille"], "nom": FAMILLES.get(x["famille"], ""), "agents": 0,
                                           "actifs": 0, "n": 0, "r": 0.0, "g": 0.0, "l": 0.0, "w": 0, "ouvertes": 0})
        f["agents"] += 1
        f["actifs"] += x["statut"] == "SHADOW"
        f["n"] += x["n"]
        f["r"] += x["r"]
        f["ouvertes"] += x["ouvertes"]
        f["g"] += x["_g"]
        f["l"] += x["_l"]
        f["w"] += x["_w"]
    familles = []
    for f in fams.values():
        familles.append({"famille": f["famille"], "nom": f["nom"], "agents": f["agents"], "actifs": f["actifs"],
                         "n": f["n"], "r": round(f["r"], 1), "pf": round(f["g"] / f["l"], 2) if f["l"] > 0 else None,
                         "reussite": round(100.0 * f["w"] / f["n"], 0) if f["n"] else 0.0, "ouvertes": f["ouvertes"]})
    familles.sort(key=lambda x: -x["r"])

    for x in agents:
        for k in ("_g", "_l", "_w"):
            x.pop(k, None)
    actifs = [x for x in agents if x["statut"] == "SHADOW"]
    resume = {"agents_shadow": sum(1 for s in status.values() if s == "SHADOW"),
              "agents_live": sum(1 for s in status.values() if s == "LIVE"),
              "agents_suspendus": sum(1 for s in status.values() if s == "SUSPENDED"),
              "trades": sum(x["n"] for x in agents), "r": round(sum(x["r"] for x in agents), 1),
              "trades_actifs": sum(x["n"] for x in actifs), "r_actifs": round(sum(x["r"] for x in actifs), 1),
              "ouvertes": sum(ouvertes.values()),
              "prets": sum(1 for x in actifs if x["verdict"] == "prêt pour revue live"),
              "perdants": sum(1 for x in actifs if x["verdict"] == "perdant")}
    return {"resume": resume, "familles": familles, "agents": agents, "recherche": recherche(home),
            "criteres": {"min_shadow": min_shadow, "min_sample": n_req, "pf_live": pf_live, "exp_live": exp_live,
                         "perdant_n": perdant_n, "perdant_pf": perdant_pf}}


def recherche(home: Path, derniers: int = 15) -> dict:
    etat = _read_json(home / "state" / "recherche_continue.json", {}) or {}
    rapports = sorted((home / "reports").glob("massive_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    passages, idees = [], []
    for f in rapports[:200]:
        d = _read_json(f, {}) or {}
        et = d.get("etapes") or {}
        ids = [x for x in (d.get("propositions") or []) if isinstance(x, str)]
        ret = [x for x in (d.get("retenues") or []) if isinstance(x, dict)]
        if len(passages) < derniers:
            passages.append({"rapport": f.stem.replace("massive_", ""), "date": d.get("date"), "duree_sec": d.get("duree_sec"),
                             "testees": et.get("testees"), "seuil": et.get("t_min"), "significatives": et.get("significatives"),
                             "controle": et.get("controle"), "propositions": len(ids)})
        for i, agent_id in enumerate(ids):
            x = ret[i] if i < len(ret) else {}
            c = x.get("config") or {}
            ap, ct = x.get("apprentissage") or {}, x.get("controle") or {}
            idees.append({"date": d.get("date"), "agent_id": agent_id, "strategie": c.get("strategy"), "ut": c.get("entry_tf"),
                          "classe": c.get("asset_class"), "sessions": c.get("sessions"),
                          "pf_backtest": round(ap["pf"], 2) if ap.get("pf") else None,
                          "pf_controle": round(ct["pf"], 2) if ct.get("pf") else None, "n_controle": ct.get("n")})
    derniere = etat.get("derniere") or {}
    return {"passages_continus": etat.get("passages"), "configurations": etat.get("configurations"),
            "derniere_date": derniere.get("date"), "derniere_duree_sec": derniere.get("duree_sec"),
            "en_marche": not (home / "state" / "recherche_continue.stop").exists(),
            "passages": passages, "idees": idees[:30], "rapports": len(rapports)}


def lignes_rapport_agents(home: Path, strategies_cfg: Optional[dict] = None, max_n: int = 6) -> list[str]:
    """Lignes « agents » du rapport de 17 h New York (2026-10-01, décision utilisateur) :
    - prêts pour le live : agents SHADOW au verdict « prêt pour revue live » (≥ `min_shadow` trades, PF ≥ seuil, espérance
      ≥ seuil), meilleurs d'abord ;
    - à remettre en shadow : agents LIVE avec au moins 10 trades RÉELS et un facteur de profit sous 0,8."""
    d = shadow_board(home, strategies_cfg)
    prets = sorted([a for a in d["agents"] if a["statut"] == "SHADOW" and a["verdict"] == "prêt pour revue live"],
                   key=lambda a: -a["r"])
    lignes = []
    if prets:
        lignes.append("Prêts pour le live (shadow) : " + ", ".join(
            f"{a['agent_id']} {a['n']} trades {a['r']:+.1f} R PF {a['pf']:.2f}" if a["pf"] is not None
            else f"{a['agent_id']} {a['n']} trades {a['r']:+.1f} R" for a in prets[:max_n]))
    else:
        lignes.append("Prêts pour le live (shadow) : aucun")
    status = (_read_json(home / "data" / "agent_status.json", {}) or {}).get("status", {}) or {}
    perdants = []
    db = home / "data" / "learning.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select agent_id, count(*), sum(result_r), sum(case when result_r > 0 then result_r else 0 end), "
                "-sum(case when result_r < 0 then result_r else 0 end) from trades "
                "where mode = 'live' and result_r is not null group by agent_id").fetchall()
        finally:
            con.close()
        for a, n, r, g, l in rows:
            if status.get(a) == "LIVE" and n >= 10 and l and (g or 0) / l < 0.8:
                perdants.append((a, int(n), float(r or 0), (g or 0) / l))
    perdants.sort(key=lambda x: x[2])
    if perdants:
        lignes.append("À remettre en shadow (réel) : " + ", ".join(
            f"{a} {n} trades {r:+.1f} R PF {pf:.2f}" for a, n, r, pf in perdants[:max_n]))
    else:
        lignes.append("À remettre en shadow (réel) : aucun")
    return lignes
