"""Écrêtage du volume par le broker : détecté et expliqué, jamais silencieux.

Constaté le 2026-09-20 sur le compte IC Markets. `volume_max` plafonne 16 cryptos sur 18 :
XLMUSD autorise 0,6 % de la taille visée, XRPUSD 4,5 %, LTCUSD 1,9 %. Une position XRPUSD
a risqué **5,30 $ au lieu de 625 $** sans qu'aucune trace ne le signale — `round_volume_down`
rabote à `volume_max` et `risk_percent_effective` tombe à 0,001 % en silence.

L'ordre part quand même, c'est volontaire : un trade plus petit que prévu reste un trade
valide, et les statistiques en R restent exploitables. Mais ça doit être visible.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.types import SymbolSpec  # noqa: E402
from tradinglab.risk.risk_manager import compute_volume  # noqa: E402


def _spec(volume_max: float, volume_min: float = 1.0, step: float = 0.01,
          name: str = "XRPUSD", digits: int = 4, tick: float = 0.0001) -> SymbolSpec:
    """Symbole type XRPUSD : tick 0,0001, contrat 1 unité (spécifications réelles IC Markets)."""
    return SymbolSpec(name=name, digits=digits, point=tick, tick_size=tick, tick_value=tick,
                      contract_size=1.0, volume_min=volume_min, volume_max=volume_max,
                      volume_step=step, stops_level_points=0, trade_allowed=True,
                      currency_base=name[:3], currency_profit="USD", currency_margin="USD",
                      asset_class="crypto", spread_points=3, root=name)


EQUITY = 500_000.0
RISK_PCT = 0.125          # 625 $ visés
ENTRY, SL = 1.4019, 1.3966


def test_ecretage_detecte_et_chiffre():
    """Le cas réel : 22 000 unités voulues, 1 000 autorisées."""
    r = compute_volume(EQUITY, RISK_PCT, ENTRY, SL, _spec(volume_max=1000.0), max_risk_percent=0.35)
    assert r.ok, "l'ordre reste valide : un trade plus petit n'est pas une erreur"
    assert r.volume_capped is True
    assert r.volume == pytest.approx(1000.0)
    assert r.volume_wanted > 20_000, "le volume voulu est conservé pour le diagnostic"
    assert r.risk_percent_effective < RISK_PCT / 10, "le risque effectif est très inférieur à la cible"


def test_message_explique_le_rabotage():
    r = compute_volume(EQUITY, RISK_PCT, ENTRY, SL, _spec(volume_max=1000.0), max_risk_percent=0.35)
    assert "rabote" in r.reason
    assert "1000" in r.reason and "0.125" in r.reason, "le message doit porter les deux chiffres"


def test_sans_ecretage_le_message_reste_ok():
    """Un plafond large ne doit rien signaler : pas de bruit quand tout va bien."""
    r = compute_volume(EQUITY, RISK_PCT, ENTRY, SL, _spec(volume_max=10_000_000.0), max_risk_percent=0.35)
    assert r.ok and r.volume_capped is False
    assert r.reason == "ok" and r.volume_wanted == 0.0
    assert r.risk_percent_effective == pytest.approx(RISK_PCT, abs=0.005), "taille pleine atteinte"


def test_plafond_pile_au_volume_voulu_n_est_pas_un_ecretage():
    """Borne : `volume_max` exactement égal au besoin ne doit pas déclencher l'avertissement."""
    voulu = compute_volume(EQUITY, RISK_PCT, ENTRY, SL, _spec(volume_max=1e9), max_risk_percent=0.35).volume
    r = compute_volume(EQUITY, RISK_PCT, ENTRY, SL, _spec(volume_max=voulu), max_risk_percent=0.35)
    assert r.volume_capped is False and r.volume == pytest.approx(voulu)


def test_volume_minimum_trop_risque_reste_un_refus():
    """L'écrêtage ne doit pas masquer le refus opposé : un minimum trop gros reste refusé."""
    r = compute_volume(1_000.0, RISK_PCT, ENTRY, SL, _spec(volume_max=1e6, volume_min=100_000.0),
                       max_risk_percent=0.35)
    assert r.ok is False and "REFUS" in r.reason


def test_btc_non_ecrete():
    """Contre-exemple mesuré : sur BTCUSD le plafond de 10 lots ne mord jamais."""
    btc = _spec(volume_max=10.0, volume_min=0.01, step=0.01, name="BTCUSD", digits=2, tick=0.01)
    r = compute_volume(EQUITY, RISK_PCT, 81_000.0, 79_380.0, btc, max_risk_percent=0.35)
    assert r.ok and r.volume_capped is False
    assert r.risk_percent_effective == pytest.approx(RISK_PCT, abs=0.01)
