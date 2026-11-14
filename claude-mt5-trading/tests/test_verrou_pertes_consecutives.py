"""Le verrou de pertes consécutives se lève quand la série est cassée.

Constaté sur le compte réel le 2026-09-21. Trois pertes d'affilée posent le verrou
`MAX_CONSECUTIVE_LOSSES`, c'est voulu. Mais `_on_position_closed` remet
`consecutive_losses` à 0 dès qu'une clôture est gagnante, **sans jamais retirer le verrou** :

    06:00:56  C06  ticket 1946049383   -697,98 $   (-1,06 R)   -> 3e perte, verrou posé
    10:01:58  B03  ticket 1945891496  +1 390,44 $  (+2,18 R)   -> compteur remis à 0

    lock_reasons    = ['MAX_CONSECUTIVE_LOSSES']
    consecutive     = 0

Le compteur disait « série terminée », le verrou disait « bloqué ». `unlock_entries`
n'était appelé que dans `roll_day_if_needed`, soit au reset 17:00 New York : la remise à
zéro du compteur était donc du code mort une fois le verrou posé, et le seul gain capable
de casser la série ne pouvait venir que d'une position déjà ouverte — plus aucune entrée
n'étant possible.

Le verrou perte-jour n'est délibérément pas symétrique : voir `test_perte_jour_ne_se_leve_pas_seule`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.state import SystemState  # noqa: E402
from tradinglab.risk.daily_guard import DailyGuard  # noqa: E402

RISK = {"risk_per_trade_percent": 0.125, "max_daily_loss_internal_percent": 1.0, "max_consecutive_losses": 3}


def _guard() -> DailyGuard:
    return DailyGuard(RISK, {})


def _etat(pertes: int) -> SystemState:
    st = SystemState()
    st.consecutive_losses = pertes
    st.equity = 500_000.0
    st.daily.starting_equity = 500_000.0
    return st


def test_trois_pertes_posent_le_verrou():
    """Comportement d'origine, inchangé."""
    st = _etat(3)
    d = _guard().evaluate(st)
    assert d.entries_allowed is False
    assert "MAX_CONSECUTIVE_LOSSES" in st.lock_reasons
    assert st.new_trades_locked is True


def test_un_gain_leve_le_verrou():
    """Cœur du correctif : le gain casse la série, le verrou tombe au cycle suivant."""
    guard, st = _guard(), _etat(3)
    guard.evaluate(st)
    assert "MAX_CONSECUTIVE_LOSSES" in st.lock_reasons

    st.consecutive_losses = 0                      # ce que fait `_on_position_closed` sur un gain
    d = guard.evaluate(st)
    assert "MAX_CONSECUTIVE_LOSSES" not in st.lock_reasons
    assert st.new_trades_locked is False
    assert d.entries_allowed is True


def test_deux_pertes_ne_verrouillent_pas():
    """Borne : sous le seuil, rien ne doit être posé ni laissé traîner."""
    st = _etat(2)
    _guard().evaluate(st)
    assert st.lock_reasons == []


def test_la_levee_ne_touche_pas_aux_autres_verrous():
    """Un seul motif retiré : les autres raisons de blocage survivent au gain."""
    guard, st = _guard(), _etat(3)
    st.lock_entries("ACCOUNT_MISMATCH")
    guard.evaluate(st)

    st.consecutive_losses = 0
    guard.evaluate(st)
    assert st.lock_reasons == ["ACCOUNT_MISMATCH"]
    assert st.new_trades_locked is True, "le compte non conforme bloque toujours"


def test_perte_jour_ne_se_leve_pas_seule():
    """Asymétrie voulue : la perte du jour se mesure sur l'equity courante.

    La rendre symétrique ferait tomber le coupe-circuit au premier rebond du flottant, et
    le ferait se réarmer en boucle autour du seuil. Ce verrou-là ne tombe qu'au reset.
    """
    guard, st = _guard(), _etat(0)
    st.equity = 494_000.0                          # -1,2 %, au-delà de la limite interne de 1 %
    guard.evaluate(st)
    assert "DAILY_LOSS_LIMIT" in st.lock_reasons

    st.equity = 500_000.0                          # le flottant revient
    guard.evaluate(st)
    assert "DAILY_LOSS_LIMIT" in st.lock_reasons, "le verrou perte-jour tient jusqu'au reset"
