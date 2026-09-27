"""Watchdog : ancrage de l'horloge sur un marché ouvert avant tout contrôle de fraîcheur.

Défaut mesuré le 2026-09-20 sur le compte réel. Le watchdog construit son propre adaptateur
et n'appelait jamais `symbol_select`. La calibration du décalage serveur n'interrogeait donc
que les symboles déjà visibles chez le broker — tous gelés le week-end. `server_time()`
restait figé sur le dernier tick de vendredi, si bien qu'un tick tout aussi figé paraissait
**vieux d'une seconde** :

    EURUSD   âge vu par le watchdog =      1 s   |   âge réel = 139 051 s (38 h)
    BTCUSD   âge vu par le watchdog =    inf     |   marché pourtant ouvert

Le troisième contrôle du watchdog — détecter un flux figé et demander SAFE_MODE — ne pouvait
alors jamais se déclencher, et la crypto vivante était jugée périmée. Ces tests verrouillent
la correction : sélectionner des symboles d'un marché ouvert (la crypto sert d'ancre, c'est
le seul continu) avant de mesurer quoi que ce soit.
"""
from __future__ import annotations

import sys
from pathlib import Path

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.journal import Journal  # noqa: E402
from tradinglab.core.state import StateStore  # noqa: E402
from tradinglab.monitoring.watchdog import Watchdog  # noqa: E402

MAINTENANT = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class _BrokerEspion:
    """Broker minimal qui enregistre les symboles sélectionnés."""

    def __init__(self, symboles: list[str]):
        self._symboles = list(symboles)
        self.selectionnes: list[str] = []

    def symbols(self) -> list[str]:
        return list(self._symboles)

    def symbol_select(self, symbol: str) -> bool:
        self.selectionnes.append(symbol)
        return True

    def is_connected(self) -> bool:
        return True


@pytest.fixture
def watchdog(settings, home, monkeypatch):
    monkeypatch.setitem(settings.raw["markets"], "crypto",
                        ["BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD", "LTCUSD", "ADAUSD", "DOGUSD"])
    broker = _BrokerEspion(["EURUSD", "BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD", "LTCUSD", "ADAUSD", "DOGUSD"])
    wd = Watchdog(settings, broker, StateStore(settings.state_dir),
                  Journal(home / "logs", component="wd_test"), reference_symbol="EURUSD")
    return wd, broker


def test_ancrage_selectionne_la_crypto(watchdog):
    """La crypto est le seul marché continu : sans elle, aucune référence vivante le week-end."""
    wd, broker = watchdog
    wd._ancrer_horloge()
    assert "BTCUSD" in broker.selectionnes
    assert "EURUSD" in broker.selectionnes, "le symbole de référence reste suivi"


def test_ancrage_borne_le_nombre_de_symboles(watchdog):
    """Le watchdog tourne toutes les 3 s : il ne doit pas balayer tout l'univers."""
    wd, broker = watchdog
    wd._ancrer_horloge()
    assert len(broker.selectionnes) <= 7, "référence + 6 cryptos au maximum"


def test_ancrage_fait_une_seule_fois(watchdog):
    """Trois appels par seconde : la sélection ne doit pas être refaite à chaque cycle."""
    wd, broker = watchdog
    wd._ancrer_horloge()
    combien = len(broker.selectionnes)
    for _ in range(5):
        wd._ancrer_horloge()
    assert len(broker.selectionnes) == combien


def test_broker_indisponible_ne_leve_pas(settings, home):
    """Un broker qui renvoie une erreur ne doit jamais arrêter le watchdog."""

    class _Casse:
        def symbols(self):
            raise RuntimeError("broker indisponible")

        def symbol_select(self, symbol):
            return False

    wd = Watchdog(settings, _Casse(), StateStore(settings.state_dir),
                  Journal(home / "logs", component="wd_test"), reference_symbol="EURUSD")
    wd._ancrer_horloge()                      # ne lève pas
    assert wd._ancrage_fait is False, "un échec doit laisser une nouvelle tentative possible"


def test_symbole_refuse_n_arrete_pas_l_ancrage(settings, home, monkeypatch):
    """Un symbole que le broker refuse ne doit pas empêcher les autres d'être sélectionnés."""
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD", "ETHUSD"])

    class _Capricieux(_BrokerEspion):
        def symbol_select(self, symbol):
            if symbol == "BTCUSD":
                raise RuntimeError("refusé")
            return super().symbol_select(symbol)

    broker = _Capricieux(["EURUSD", "BTCUSD", "ETHUSD"])
    wd = Watchdog(settings, broker, StateStore(settings.state_dir),
                  Journal(home / "logs", component="wd_test"), reference_symbol="EURUSD")
    wd._ancrer_horloge()
    assert "ETHUSD" in broker.selectionnes
    assert wd._ancrage_fait is True


def test_sans_symbole_de_reference_la_crypto_suffit(settings, home, monkeypatch):
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD"])
    broker = _BrokerEspion(["BTCUSD"])
    wd = Watchdog(settings, broker, StateStore(settings.state_dir),
                  Journal(home / "logs", component="wd_test"), reference_symbol=None)
    wd._ancrer_horloge()
    assert broker.selectionnes == ["BTCUSD"]


# ------------------------------------------------------------------ fraîcheur multi-marchés
class _BrokerTicks(_BrokerEspion):
    """Broker qui renvoie des ticks d'âges choisis (secondes avant `maintenant`)."""

    def __init__(self, symboles, ages: dict):
        super().__init__(symboles)
        self.ages = ages

    def server_time(self):
        return MAINTENANT

    def tick(self, symbol):
        age = self.ages.get(symbol)
        if age is None:
            return None
        return SimpleNamespace(age_seconds=lambda now=None, _a=age: _a)

    def positions(self, magic=None):
        return []

    def account_info(self):
        return None


def _wd(settings, home, broker, ref="EURUSD"):
    wd = Watchdog(settings, broker, StateStore(settings.state_dir),
                  Journal(home / "logs", component="wd_test"), reference_symbol=ref)
    wd._ancrer_horloge()
    return wd


def test_forex_gele_mais_crypto_vivante_reste_frais(settings, home, monkeypatch):
    """Cœur du correctif : un week-end normal ne doit PAS déclencher SAFE_MODE."""
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD"])
    broker = _BrokerTicks(["EURUSD", "BTCUSD"], {"EURUSD": 150_000.0, "BTCUSD": 2.0})
    wd = _wd(settings, home, broker)
    rep = wd.check_once()
    assert rep.data_fresh is True
    assert not any("données périmées" in r for r in rep.reasons)
    assert rep.safe_mode_request is False


def test_tous_les_marches_geles_declenche_safe_mode(settings, home, monkeypatch):
    """Un flux réellement mort doit toujours être détecté."""
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD"])
    broker = _BrokerTicks(["EURUSD", "BTCUSD"], {"EURUSD": 150_000.0, "BTCUSD": 90_000.0})
    wd = _wd(settings, home, broker)
    rep = wd.check_once()
    assert rep.data_fresh is False
    assert any("données périmées" in r for r in rep.reasons)
    assert rep.safe_mode_request is True


def test_tick_indisponible_compte_comme_gele(settings, home, monkeypatch):
    monkeypatch.setitem(settings.raw["markets"], "crypto", ["BTCUSD"])
    broker = _BrokerTicks(["EURUSD", "BTCUSD"], {})        # aucun tick
    wd = _wd(settings, home, broker)
    rep = wd.check_once()
    assert rep.data_fresh is None and rep.safe_mode_request is False   # tolérance de démarrage : inconnu (2026-09-27)
    wd._started_mono -= wd.STARTUP_GRACE_SEC + 1                        # tolérance écoulée : flux figé
    rep = wd.check_once()
    assert rep.data_fresh is False and rep.safe_mode_request is True


def test_ancrage_reessaie_tant_que_le_terminal_ne_liste_aucun_symbole(watchdog):
    """2026-09-26 : l'ancrage passait avant la connexion ; liste de symboles vide → aucune crypto retenue, ancrage
    déclaré fait pour toujours, seul EURUSD surveillé → SAFE_MODE tout le samedi, aucun trade crypto."""
    wd, broker = watchdog
    vrais = broker._symboles
    broker._symboles = []                       # terminal pas encore connecté
    wd._ancrer_horloge()
    assert not wd._ancrage_fait and wd._surveilles == []
    broker._symboles = vrais                    # connecté au contrôle suivant
    wd._ancrer_horloge()
    assert wd._ancrage_fait and "BTCUSD" in wd._surveilles



def test_tolerance_de_demarrage_symbole_sans_tick(watchdog):
    """2026-09-27 : après un redémarrage le week-end, les cryptos fraîchement sélectionnées n'avaient pas encore de tick
    et le watchdog demandait SAFE_MODE (~10 min). Pendant la tolérance, « pas de tick » = inconnu, pas périmé."""
    import time as _t
    wd, broker = watchdog
    assert wd.STARTUP_GRACE_SEC >= 600
    assert (_t.monotonic() - wd._started_mono) < wd.STARTUP_GRACE_SEC
