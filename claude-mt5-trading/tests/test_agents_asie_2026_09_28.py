"""Famille R : agents dédiés à la session Asie (2026-09-28, décision utilisateur, LIVE directement)."""
from __future__ import annotations

from tradinglab.agents.registry import default_agents
from tradinglab.agents.screeners import resolve_screener


def test_douze_agents_asie_live_sur_les_bons_marches():
    fam = {a.agent_id: a for a in default_agents() if a.family == "R"}
    assert sorted(fam) == [f"R{i:02d}" for i in range(1, 13)]
    for a in fam.values():
        attendu = "SHADOW" if a.agent_id == "R07" else "LIVE"      # R07 : PF 0,68 en backtest (28/09)
        assert a.status == attendu and a.sessions == ["ASIA"] and resolve_screener(a) is not None
        assert a.timeframes["entry"] in ("M15", "H1")
        # stop >= 0,75 ATR H1 (minimum hors crypto) : 1,6 ATR M15 ≈ 0,8 ATR H1 ; 1,3 ATR H1
        assert a.params["sl_atr"] >= (1.6 if a.timeframes["entry"] == "M15" else 1.3)
    marches = {m for a in fam.values() for m in a.markets}
    assert {"USDJPY", "AUDUSD", "JP225", "XAUUSD"} <= marches and "crypto" not in marches
