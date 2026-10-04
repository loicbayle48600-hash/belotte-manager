"""Validation des agents LIVE par le pipeline (plan pro du 2026-09-25, point 3).

Les 102 agents LIVE du registre ont été mis en LIVE directement, sans jamais passer le backtest ni le hors
échantillon. Ce module leur fait passer BACKTEST → OUT_OF_SAMPLE → WALK_FORWARD → MONTE_CARLO avec le même code que
le pipeline des challengers, dans un PROCESSUS SÉPARÉ (aucune charge sur la boucle de trading).

Sécurité : lecture seule. Les résultats vont dans `data/validation_live/<agent>.json` (pas dans `data/research`), le
registre est chargé sans fichier de statut (aucune écriture) et aucun statut n'est modifié. La décision de garder un
noyau d'agents revient à l'utilisateur, sur la base du rapport `reports/validation_live.json`.

Usage : python -m tradinglab.research.validate_live [--agents E05,B02] [--limite 10]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STAGES = ("BACKTEST", "OUT_OF_SAMPLE", "WALK_FORWARD", "MONTE_CARLO")


class _NullStore:
    """Magasin muet : la validation n'écrit rien dans learning.db."""

    def agent_event(self, *a, **k) -> None:
        return None


def validate(pipeline, agents: list, journal=print) -> list[dict]:
    """Fait passer les 4 étapes à chaque agent (arrêt à la première étape échouée) et résume."""
    from .pipeline import Stage

    fns = {"BACKTEST": pipeline.stage_backtest, "OUT_OF_SAMPLE": pipeline.stage_out_of_sample,
           "WALK_FORWARD": pipeline.stage_walk_forward, "MONTE_CARLO": pipeline.stage_monte_carlo}
    out = []
    for i, spec in enumerate(agents, 1):
        dernier, erreur = None, None
        try:
            for nom in STAGES:
                rec = fns[nom](spec)
                dernier = nom
                if not rec.passed(Stage(nom)):
                    break
        except Exception as e:  # noqa: BLE001 - un agent en erreur ne bloque pas les autres
            erreur = f"{type(e).__name__}: {e}"
        rec = pipeline.record(spec.agent_id)
        bt = rec.stages.get("BACKTEST", {}).get("metrics", {})
        passes = [s for s in STAGES if rec.stages.get(s, {}).get("passed")]
        out.append({"agent_id": spec.agent_id, "nom": spec.name, "etapes_reussies": len(passes),
                    "valide": len(passes) == len(STAGES), "derniere_etape": dernier, "erreur": erreur,
                    "symboles": bt.get("symbols") or bt.get("symbol"), "trades_bt": bt.get("sample_size"),
                    "pf_bt": bt.get("profit_factor"), "esperance_bt": bt.get("expectancy_r"),
                    "dd_bt": bt.get("max_drawdown_r")})
        journal(f"[{i}/{len(agents)}] {spec.agent_id} : {len(passes)}/4 étapes"
                + (f" (erreur {erreur})" if erreur else ""))
    out.sort(key=lambda x: (x["etapes_reussies"], x["esperance_bt"] or -99), reverse=True)
    return out


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - outil en ligne de commande
    from ..agents.registry import AgentRegistry
    from ..core.config import load_dotenv, load_settings
    from ..core.types import AgentStatus
    from ..mt5.mock_adapter import make_broker
    from ..mt5.symbols import resolve_symbols
    from .pipeline import ResearchPipeline

    ap = argparse.ArgumentParser(description="Validation des agents LIVE (lecture seule)")
    ap.add_argument("--agents", default="", help="liste d'agents séparés par des virgules (défaut : tous les LIVE)")
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--barres", type=int, default=3000, help="historique par symbole sur le tf d'entrée (3000 ≈ 1 mois en M15)")
    ap.add_argument("--sortie", default="validation_live", help="nom du dossier de résultats et du rapport")
    args = ap.parse_args(argv)
    s = load_settings()
    load_dotenv(s.home / ".env")
    broker = make_broker(os.environ.get("TRADINGLAB_BROKER", "mt5"), s)
    if not broker.connect():
        print("terminal indisponible")
        return 1
    reg = AgentRegistry(status_file=None)
    statuts = json.loads((s.data_dir / "agent_status.json").read_text(encoding="utf-8")).get("status", {})
    agents = [a for a in reg.agents.values() if a.generates_trades and statuts.get(a.agent_id, a.status) == AgentStatus.LIVE.value]
    if args.agents:
        voulus = {x.strip() for x in args.agents.split(",")}
        agents = [a for a in agents if a.agent_id in voulus]
    if args.limite:
        agents = agents[: args.limite]
    voulus_sym = [x for grp, lst in s.markets.items() if grp != "asset_class_rules" and isinstance(lst, list) for x in lst]
    symbols = [r for r in resolve_symbols(voulus_sym, broker.symbols()).values() if r]
    specs = {sym: broker.symbol_info(sym) for sym in symbols}
    specs = {k: v for k, v in specs.items() if v}
    pipe = ResearchPipeline(reg, _NullStore(), s.learning, {**s.backtest, "management": s.profit_management},
                            lambda sym, tf, n: broker.rates(sym, tf, n),
                            specs, s.data_dir / args.sortie, bars=args.barres)
    res = validate(pipe, agents)
    rapport = {"date": datetime.now(timezone.utc).isoformat(), "agents": res,
               "valides": [r["agent_id"] for r in res if r["valide"]]}
    (s.home / "reports").mkdir(exist_ok=True)
    rapport["barres"] = args.barres
    (s.home / "reports" / f"{args.sortie}.json").write_text(json.dumps(rapport, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rapport['valides'])} agents LIVE valident les 4 étapes sur {len(res)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
