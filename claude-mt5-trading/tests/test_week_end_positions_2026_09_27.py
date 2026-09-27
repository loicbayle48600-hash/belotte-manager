"""Tenue des positions hors crypto le week-end : mécanisme disponible (`weekend_holding_allowed: CRYPTO_ONLY`) mais
DÉSACTIVÉ dans la config FOXX le 2026-09-27 (décision utilisateur : la règle interdit de trader, pas de garder)."""
from __future__ import annotations

from datetime import datetime, timezone

from tradinglab.risk.prop_guard import PropGuard, PropProfile


def _guard():
    return PropGuard(PropProfile.from_config({"weekend_holding_allowed": "CRYPTO_ONLY", "weekend_no_entry_minutes_before": 60,
                                              "weekend_close_minutes_before": 15}), True, False, 1.0)


VEN_16H30_NY = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)      # vendredi 16:30 New York (EDT)
VEN_12H_NY = datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc)
JEU_16H30_NY = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)


def test_minutes_avant_le_week_end():
    g = _guard()
    assert round(g.minutes_before_weekend(VEN_16H30_NY)) == 30
    assert g.minutes_before_weekend(JEU_16H30_NY) is None


def test_pas_d_entree_hors_crypto_dans_l_heure_qui_precede():
    g = _guard()
    assert g.weekend_holding_check("forex", VEN_16H30_NY).ok is False
    assert g.weekend_holding_check("indices", VEN_16H30_NY).ok is False
    assert g.weekend_holding_check("crypto", VEN_16H30_NY).ok is True
    assert g.weekend_holding_check("forex", VEN_12H_NY).ok is True
    assert g.weekend_holding_check("forex", JEU_16H30_NY).ok is True



def test_config_reelle_garde_les_positions_le_week_end():
    import yaml
    cfg = yaml.safe_load(open("config/prop_firms.yaml", encoding="utf-8"))["prop"]
    g = PropGuard(PropProfile.from_config(cfg), True, False, 1.0)
    assert g.weekend_holding_check("forex", VEN_16H30_NY).ok is True
