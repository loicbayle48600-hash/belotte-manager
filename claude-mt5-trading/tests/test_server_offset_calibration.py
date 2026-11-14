"""Calibration du décalage heure serveur : priorité aux symboles retenus par le labo.

Bug corrigé le 2026-09-20. `_freshest_raw_tick()` prenait les 12 PREMIERS symboles visibles
du broker. Chez IC Markets (7 420 symboles), ce sont des paires forex : le week-end elles
sont gelées depuis vendredi, la mesure dépassait `OFFSET_MAX_ABS_SEC` et la calibration
abandonnait. Conséquence en cascade : `server_time()` restait bloqué au vendredi soir, les
ticks crypto (heure serveur UTC+3) paraissaient dans le futur, et `age_seconds()` les
déclarait infiniment périmés — donc `data_quality=STALE` sur un marché pourtant ouvert.

Le comportement sûr en cas d'échec de calibration (tout en STALE) reste inchangé : ces
tests vérifient qu'on cesse d'échouer à tort, pas qu'on accepte des données douteuses.
"""
from __future__ import annotations

import sys
import types as _types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core import clock  # noqa: E402
from tradinglab.mt5 import mt5_adapter  # noqa: E402

NOW_PC = 1_800_000_000.0          # heure PC fixe (UTC)
SERVEUR_UTC_PLUS_3 = 3 * 3600     # décalage réel d'IC Markets


class _StubMT5:
    """Broker simulé : beaucoup de symboles gelés, quelques-uns vivants."""

    def __init__(self, frais: dict[str, float], gel: float, n_geles: int = 40):
        self.frais = frais
        self.interroges: list[str] = []
        self._syms = [_types.SimpleNamespace(name=f"FX{i:02d}", visible=True) for i in range(n_geles)]
        self._syms += [_types.SimpleNamespace(name=n, visible=True) for n in frais]
        self.gel = gel

    def symbols_get(self, *a, **kw):
        return self._syms

    def symbol_select(self, symbol, flag):
        return True

    def symbol_info_tick(self, name):
        self.interroges.append(name)
        ts = self.frais.get(name, self.gel)
        return _types.SimpleNamespace(time=ts, time_msc=ts * 1000.0,
                                      bid=1.1, ask=1.1001, last=1.1, volume=1)


@pytest.fixture
def adapter(monkeypatch):
    # forex gelé depuis 34 h, crypto à l'heure serveur (UTC+3)
    stub = _StubMT5(frais={"BTCUSD": NOW_PC + SERVEUR_UTC_PLUS_3}, gel=NOW_PC - 34 * 3600)
    monkeypatch.setattr(mt5_adapter, "mt5", stub)
    monkeypatch.setattr(mt5_adapter, "MT5_AVAILABLE", True)
    monkeypatch.setattr(mt5_adapter.time, "time", lambda: NOW_PC)
    a = mt5_adapter.MT5Adapter()
    yield a, stub
    clock.set_server_utc_offset(0)


def test_sans_selection_la_calibration_echoue(adapter):
    """Reproduit le bug : les 12 premiers visibles sont gelés, aucune référence exploitable."""
    a, stub = adapter
    assert a._calibrate_offset(force=True) is None
    assert a.server_offset_sec is None
    assert "BTCUSD" not in stub.interroges


def test_un_symbole_selectionne_fournit_la_reference(adapter):
    """BTCUSD arrive en 41e position chez le broker : sans priorité il ne serait jamais lu."""
    a, stub = adapter
    assert a.symbol_select("BTCUSD") is True
    assert a._calibrate_offset(force=True) == SERVEUR_UTC_PLUS_3
    assert a.server_offset_sec == SERVEUR_UTC_PLUS_3
    assert stub.interroges[0] == "BTCUSD", "le symbole du labo doit être interrogé en premier"


def test_echantillon_sans_doublon_et_labo_en_tete(adapter):
    """Contrat : les symboles du labo d'abord, puis au plus OFFSET_MAX_SYMBOLS du broker.

    Un symbole sélectionné par le labo n'est jamais relu au titre du complément broker.
    """
    a, stub = adapter
    for n in ("BTCUSD", "FX00", "FX01"):
        a.symbol_select(n)
    a._calibrate_offset(force=True)
    assert stub.interroges[:3] == ["BTCUSD", "FX00", "FX01"], "le labo passe en premier"
    assert len(stub.interroges) == len(set(stub.interroges)), "aucun symbole interrogé deux fois"
    assert len(stub.interroges) <= 3 + mt5_adapter.OFFSET_MAX_SYMBOLS


def test_selection_refusee_non_memorisee(adapter, monkeypatch):
    """Un symbole que le broker refuse de sélectionner ne doit pas polluer l'échantillon."""
    a, stub = adapter
    monkeypatch.setattr(stub, "symbol_select", lambda symbol, flag: False)
    assert a.symbol_select("INEXISTANT") is False
    a._calibrate_offset(force=True)
    assert "INEXISTANT" not in stub.interroges


def test_server_time_suit_le_decalage_calibre(adapter):
    a, _ = adapter
    a.symbol_select("BTCUSD")
    a._calibrate_offset(force=True)
    # server_time() ramène le tick de référence (heure serveur) en UTC
    assert a.server_time().timestamp() == pytest.approx(NOW_PC, abs=1.0)


def test_tick_gele_reste_gele(adapter):
    """Garde-fou : calibrer ne doit pas rajeunir un marché réellement fermé."""
    a, _ = adapter
    a.symbol_select("BTCUSD")
    a._calibrate_offset(force=True)
    tick = a.tick("FX00")
    if tick is not None:                      # l'adaptateur peut renvoyer None selon le stub
        assert tick.age_seconds() > 30 * 3600


def test_nouvelle_selection_rouvre_la_calibration(adapter, monkeypatch):
    """Au démarrage l'univers est sélectionné APRÈS la connexion : sans réouverture, le délai de
    recalibration (600 s) laisserait tout en STALE pendant 10 minutes."""
    a, stub = adapter
    assert a._calibrate_offset() is None            # échec initial, mesure horodatée
    assert a._offset_measured_at > 0
    a.symbol_select("BTCUSD")                       # un symbole vivant arrive
    assert a._offset_measured_at == 0.0, "la sélection doit permettre une nouvelle tentative"
    assert a._calibrate_offset() == SERVEUR_UTC_PLUS_3


def test_selection_ne_rouvre_pas_si_deja_calibre(adapter):
    """Une fois le décalage connu, le rythme normal de recalibration reprend ses droits."""
    a, stub = adapter
    a.symbol_select("BTCUSD")
    assert a._calibrate_offset(force=True) == SERVEUR_UTC_PLUS_3
    marque = a._offset_measured_at
    a.symbol_select("FX05")
    assert a._offset_measured_at == marque, "aucune raison de forcer une mesure quand l'offset est connu"


def test_crypto_en_fin_d_univers_fournit_quand_meme_la_reference(adapter):
    """Cas réel du 2026-09-20 : l'univers commence par 12+ paires forex gelées et la crypto — seul
    marché ouvert le week-end — arrive en dernier. Borner l'échantillon aux 12 premiers symboles du
    labo laissait donc tout en STALE malgré des ticks crypto vivants."""
    a, stub = adapter
    for i in range(14):                 # le labo sélectionne d'abord son forex, gelé
        a.symbol_select(f"FX{i:02d}")
    a.symbol_select("BTCUSD")           # puis la crypto, en 15e position
    assert a._calibrate_offset(force=True) == SERVEUR_UTC_PLUS_3
    assert "BTCUSD" in stub.interroges, "un symbole du labo ne doit jamais être écarté de la mesure"


def test_complement_broker_reste_borne(adapter):
    """La borne continue de s'appliquer aux symboles du broker, qui peuvent être des milliers."""
    a, stub = adapter
    a.symbol_select("BTCUSD")
    a._calibrate_offset(force=True)
    hors_labo = [n for n in stub.interroges if n not in a._selected]
    assert len(hors_labo) <= mt5_adapter.OFFSET_MAX_SYMBOLS
