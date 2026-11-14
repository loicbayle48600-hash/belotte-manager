"""2026-10-01, décision utilisateur : les positions semi-longues (agents H4 / D1 / W1) restent ouvertes le week-end,
protégées contre l'écart de réouverture du lundi ; toutes les autres positions hors crypto sont fermées comme avant."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import yaml

from tradinglab.core.types import SymbolSpec
from tradinglab.orchestration.orchestrator import Orchestrator

EURUSD = SymbolSpec(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0, contract_size=100000,
                    volume_min=0.01, volume_step=0.01, volume_max=100.0, stops_level_points=0, trade_allowed=True,
                    currency_base="EUR", currency_profit="USD", currency_margin="EUR", asset_class="forex", spread_points=10)
CFG = {"enabled": True, "timeframes": ["H4", "D1", "W1"], "require_break_even": True, "reduce_percent": 50,
       "gap_budget_percent": 0.25, "gap_p99_atr_d1": {"forex": 1.3, "default": 1.5}}


def _orch(d1=True):
    o = Orchestrator.__new__(Orchestrator)
    o.prop = SimpleNamespace(profile=SimpleNamespace(raw={"weekend_semi_long": dict(CFG)}))
    tfs = {"SW": "D1", "IN": "M15"}
    o.registry = SimpleNamespace(get=lambda aid: SimpleNamespace(timeframes={"entry": tfs[aid]}) if aid in tfs else None)
    frames = {"D1": pd.DataFrame({"atr14": [0.0068, 0.0069, 0.0070, 0.0071]})} if d1 else {}
    o.snapshots = {"EURUSD": SimpleNamespace(frames=frames)}
    o.state = SimpleNamespace(equity=100000.0)
    o.now_fn = lambda: datetime(2026, 10, 2, 20, 50, tzinfo=timezone.utc)          # vendredi
    o.appels = []
    o.broker = SimpleNamespace(close_position=lambda t, v=None, comment="": o.appels.append((t, v)) or SimpleNamespace(ok=True, retcode=10009))
    o.evenements = []
    o.journal = SimpleNamespace(event=lambda kind, **kw: o.evenements.append(kind))
    return o


def _plan(agent="SW"):
    return SimpleNamespace(agent_id=agent, symbol="EURUSD", side="BUY", entry=1.1000, last_sl=1.1005, notes=[])


def _pos(sl=1.1005, prix=1.1100, vol=1.0):
    return SimpleNamespace(ticket=7, sl=sl, has_sl=True, price_current=prix, volume=vol)


def test_intraday_ferme_avant_le_week_end():
    garde, pourquoi = _orch()._garde_week_end(_plan("IN"), _pos(), EURUSD)
    assert not garde and "intraday" in pourquoi


def test_semi_long_sans_profit_protege_ferme():
    garde, pourquoi = _orch()._garde_week_end(_plan(), _pos(sl=1.0950), EURUSD)
    assert not garde and "prix d'entrée" in pourquoi


def test_semi_long_protege_garde_et_moitie_fermee_une_seule_fois():
    o, plan = _orch(), _plan()
    garde, _ = o._garde_week_end(plan, _pos(), EURUSD)
    assert garde and o.appels == [(7, 0.5)] and "weekend_reduce" in o.evenements and "weekend_keep" in o.evenements
    garde2, _ = o._garde_week_end(plan, _pos(vol=0.5), EURUSD)
    assert garde2 and o.appels == [(7, 0.5)], "réduite une seule fois par week-end"


def test_ecart_trop_couteux_au_dela_du_stop_ferme():
    # stop juste au-dessus de l'entrée, prix tout près : un écart p99 (1,3 × 0,0070) traverserait le stop de ≈ 86 pips
    garde, pourquoi = _orch()._garde_week_end(_plan(), _pos(prix=1.1010, vol=10.0), EURUSD)
    assert not garde and "trop coûteux" in pourquoi


def test_sans_atr_journalier_fermeture_prudente():
    garde, pourquoi = _orch(d1=False)._garde_week_end(_plan(), _pos(), EURUSD)
    assert not garde and "ATR journalier" in pourquoi


def test_configuration_foxx():
    prof = yaml.safe_load(open("config/prop_firms.yaml", encoding="utf-8"))
    def cherche(o):
        if isinstance(o, dict):
            if "weekend_semi_long" in o:
                return o["weekend_semi_long"]
            for v in o.values():
                r = cherche(v)
                if r is not None:
                    return r
        return None
    c = cherche(prof)
    assert c and c["enabled"] is True and c["timeframes"] == ["H4", "D1", "W1"] and c["reduce_percent"] == 50
    assert c["require_break_even"] is True and c["gap_budget_percent"] == 0.25 and c["gap_p99_atr_d1"]["energies"] == 2.4
