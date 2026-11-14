"""Session de marché le week-end : `OFF` par défaut, plages horaires pour la crypto.

Constaté le 2026-09-20 : `current_session()` renvoyait `OFF` le samedi et le dimanche pour
**tout** instrument. Or aucun agent ne liste `OFF` dans ses sessions, donc `active_for()`
renvoyait 0 agent sur chaque symbole — la crypto restait intradable le week-end malgré des
cotations vivantes et des régimes calculés.

Le changement est volontairement étroit : seule la classe `crypto` y échappe, et uniquement
parce que ces marchés cotent réellement 24/7. Tout le reste conserve l'hypothèse forex.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.agents.registry import AgentRegistry  # noqa: E402
from tradinglab.core.clock import current_session  # noqa: E402
from tradinglab.core.types import Session  # noqa: E402

SAMEDI = datetime(2026, 9, 19, 11, 50, tzinfo=timezone.utc)
DIMANCHE = datetime(2026, 9, 20, 11, 50, tzinfo=timezone.utc)
VENDREDI = datetime(2026, 9, 18, 11, 50, tzinfo=timezone.utc)


def test_defaut_inchange_le_week_end():
    """Le comportement historique reste celui de tous les instruments qui ferment le week-end."""
    assert current_session(SAMEDI) is Session.OFF
    assert current_session(DIMANCHE) is Session.OFF


def test_semaine_inchangee():
    assert current_session(VENDREDI) is Session.LONDON
    assert current_session(VENDREDI, round_the_clock=True) is Session.LONDON


def test_week_end_autorise_applique_les_plages_horaires():
    assert current_session(DIMANCHE, round_the_clock=True) is Session.LONDON
    assert current_session(SAMEDI, round_the_clock=True) is Session.LONDON


@pytest.mark.parametrize("heure,attendue", [
    (3, Session.ASIA),
    (9, Session.LONDON),
    (14, Session.OVERLAP_LDN_NY),
    (18, Session.NEWYORK),
    (22, Session.ASIA),       # creux forex rattaché à ASIA pour un marché continu
])
def test_plages_horaires_respectees_le_dimanche(heure, attendue):
    ts = DIMANCHE.replace(hour=heure, minute=0)
    assert current_session(ts, round_the_clock=True) is attendue


def test_creux_du_soir_comble_pour_un_marche_continu():
    """21:00-00:00 UTC est creux pour le forex, pas pour la crypto : Sydney ouvre vers 21-22 h."""
    for heure in (21, 22, 23):
        ts = DIMANCHE.replace(hour=heure, minute=0)
        assert current_session(ts) is Session.OFF, "le forex garde son creux"
        assert current_session(ts, round_the_clock=True) is Session.ASIA


def test_aucune_heure_sans_agent_en_crypto():
    """Le but du correctif : une couverture continue, sans trou quotidien."""
    from datetime import timedelta
    reg = AgentRegistry()
    creux = []
    for h in range(24):
        ts = DIMANCHE.replace(hour=0, minute=0) + timedelta(hours=h)
        sess = current_session(ts, round_the_clock=True).value
        if not reg.active_for("UNCERTAIN", sess, "crypto", "BTCUSD"):
            creux.append(h)
    assert not creux, f"heures sans aucun agent crypto : {creux}"


# ------------------------------------------------------------------ conséquence réelle
def test_des_agents_s_activent_en_session_ouverte_pas_en_off():
    """La raison d'être du correctif : `OFF` ne correspond à aucun agent."""
    reg = AgentRegistry()
    en_off = reg.active_for("UNCERTAIN", Session.OFF.value, "crypto", "BTCUSD")
    en_londres = reg.active_for("UNCERTAIN", Session.LONDON.value, "crypto", "BTCUSD")
    assert not en_off, "aucun agent ne déclare la session OFF"
    assert en_londres, "des agents doivent exister pour la crypto en session ouverte"


def test_aucun_agent_ne_declare_la_session_off():
    """Garde-fou : si un agent venait à déclarer OFF, ce test signalerait le changement."""
    reg = AgentRegistry()
    fautifs = [a.agent_id for a in reg.agents.values() if Session.OFF.value in (a.sessions or [])]
    assert not fautifs, f"agents déclarant OFF : {fautifs}"
