"""Serveur MCP local (stdio) pour Claude Code / agents.

Il n'expose AUCUNE route brute vers order_send / modify SL : les outils sont
en lecture (état, positions, compte, ticks, OHLC, news, agents) ou des commandes
de sécurité (PAUSE, SAFE_MODE, PANIC, CLOSE…) qui passent par la file de
commandes de l'orchestrateur. `propose_trade` dépose une idée qui suivra le
même chemin que les agents internes (revue + Execution Gate déterministe).

Compatible mcp 1.x (FastMCP) et 2.x (MCPServer).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..core.config import load_settings
from ..core.types import utcnow
from ..mt5.mock_adapter import make_broker
from ..api.commands import CommandHandler

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server  # type: ignore
except Exception:  # noqa: BLE001
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server  # type: ignore
    except Exception:  # noqa: BLE001
        _Server = None  # type: ignore


def build_server(home: Optional[Path] = None):
    if _Server is None:
        raise RuntimeError("package mcp indisponible : pip install mcp")
    s = load_settings(home)
    handler = CommandHandler(s, source="mcp")
    srv = _Server("mt5-trading-lab", instructions="Lab de trading MT5 (DEMO). Aucun outil ne permet d'envoyer un ordre brut : "
                                                  "toute exécution passe par l'Execution Gate déterministe de l'orchestrateur.")
    _broker_cache: dict[str, Any] = {}

    def broker():
        if "b" not in _broker_cache:
            b = make_broker(s.broker_kind, s)
            b.connect()
            _broker_cache["b"] = b
        return _broker_cache["b"]

    @srv.tool(name="status", description="État système, mode, compte, verrous, régimes, budget modèles.")
    def status() -> dict:
        return handler.run("STATUS")

    @srv.tool(name="positions", description="Positions ouvertes gérées par le bot avec leur plan (SL initial, TP partiels, R max).")
    def positions() -> dict:
        return handler.run("POSITIONS")

    @srv.tool(name="risk", description="Rapport de risque et conformité prop (limites internes < hard limits).")
    def risk() -> dict:
        return handler.run("RISK")

    @srv.tool(name="account", description="Informations de compte MT5 (jamais de mot de passe).")
    def account() -> dict:
        a = broker().account_info()
        return a.public_dict() if a else {"error": "compte indisponible", "last_error": broker().last_error()}

    @srv.tool(name="tick", description="Dernier tick bid/ask d'un symbole.")
    def tick(symbol: str) -> dict:
        t = broker().tick(symbol)
        return {"symbol": symbol, "time": t.time.isoformat(), "bid": t.bid, "ask": t.ask, "provenance": "FACT"} if t else {"error": "UNAVAILABLE"}

    @srv.tool(name="rates", description="Dernières barres OHLC d'un symbole (timeframe M5/M15/H1/H4/D1).")
    def rates(symbol: str, timeframe: str = "H1", count: int = 100) -> dict:
        df = broker().rates(symbol, timeframe, min(int(count), 500))
        if df.empty:
            return {"error": "UNAVAILABLE"}
        d = df.copy()
        d["time"] = d["time"].astype(str)
        return {"symbol": symbol, "timeframe": timeframe, "bars": d.to_dict(orient="records"), "provenance": "FACT"}

    @srv.tool(name="agents", description="Registre des agents (statuts, familles, stratégies) et agents actifs.")
    def agents() -> dict:
        return handler.run("AGENTS")

    @srv.tool(name="top_agents", description="Classement des agents par expectancy (échantillon indiqué).")
    def top_agents() -> dict:
        return handler.run("TOP_AGENTS")

    @srv.tool(name="news", description="État du pipeline news (dégradé ou non) et dernières infos en cache.")
    def news() -> dict:
        return handler.run("NEWS")

    @srv.tool(name="calendar", description="Événements macro à venir (ou état dégradé).")
    def calendar() -> dict:
        return handler.run("CALENDAR")

    @srv.tool(name="why", description="Explique une décision : WHY <ticket ou symbole> à partir du journal d'audit.")
    def why(target: str) -> dict:
        return handler.run("WHY", target)

    @srv.tool(name="report_day", description="Rapport journalier (pertes incluses).")
    def report_day() -> dict:
        return handler.run("REPORT_DAY")

    @srv.tool(name="research_status", description="État du pipeline champion/challenger et du shadow trading.")
    def research_status() -> dict:
        return handler.run("RESEARCH_STATUS")

    @srv.tool(name="command", description="Commande de contrôle : PAUSE | RESUME | SAFE_MODE | PANIC | CLOSE <ticket|symbole> | CLOSE_ALL_BOT | BREAK_EVEN <ticket>. "
                                          "Jamais d'ouverture de position par cet outil.")
    def command(name: str, arg: Optional[str] = None) -> dict:
        n = name.upper()
        if n not in {"PAUSE", "RESUME", "SAFE_MODE", "PANIC", "CLOSE", "CLOSE_ALL_BOT", "BREAK_EVEN"}:
            return {"ok": False, "error": "commande non autorisée via MCP"}
        return handler.run(n, arg)

    @srv.tool(name="propose_trade", description="Dépose une idée de trade (symbole, sens, entrée, SL, TP, justification). Elle est journalisée et "
                                                "soumise au même chemin que les agents internes : revue adversariale puis Execution Gate déterministe. "
                                                "Aucune exécution directe.")
    def propose_trade(symbol: str, side: str, entry: float, sl: float, tp: float, rationale: str, agent_id: str = "MCP_EXTERNAL") -> dict:
        if sl is None or sl <= 0:
            return {"ok": False, "error": "SL obligatoire"}
        rec = {"ts": utcnow().isoformat(), "symbol": symbol, "side": side.upper(), "entry": entry, "sl": sl, "tp": tp, "rationale": rationale, "agent_id": agent_id}
        p = s.state_dir / "proposals.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        handler.journal.event("trade_proposal", **rec)
        return {"ok": True, "queued": rec, "note": "la proposition sera évaluée par le gate ; aucune garantie d'exécution"}

    return srv


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
