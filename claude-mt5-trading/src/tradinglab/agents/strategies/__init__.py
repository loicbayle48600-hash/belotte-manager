"""Stratégies propres à chaque agent (une fonction par agent, clé = agent_id).

Chaque module de famille enregistre ses stratégies via `screeners.register("<agent_id>")`.
Un module absent ou en erreur d'import n'empêche pas les autres de se charger : les agents
concernés retombent sur leur screener générique (`AgentSpec.base_strategy`).
"""
from __future__ import annotations

import importlib

FAMILY_MODULES = ["b_trend", "c_breakout", "d_pullback", "e_reversal", "f_structure", "g_volatility",
                  "k_news", "l_assets", "m_symbols"]
LOADED: list[str] = []
FAILED: dict[str, str] = {}

for _name in FAMILY_MODULES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
        LOADED.append(_name)
    except ImportError as e:  # module pas encore écrit
        FAILED[_name] = f"{type(e).__name__}: {e}"
