"""Horaires forex en heure serveur (2026-09-27) : IC Markets rouvre le dimanche à 21:00 UTC l'été, pas 22:00.
Les positions du vendredi ont sauté leurs stops à 21:01 UTC ; l'ancienne règle disait encore « marché fermé »."""
from __future__ import annotations

from datetime import datetime, timezone

from tradinglab.core import clock


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_ouverture_dimanche_21h_utc_en_ete_et_22h_en_hiver(monkeypatch):
    monkeypatch.setattr(clock, "SERVER_UTC_OFFSET_SEC", 0)          # repli Europe/Athens
    assert clock.forex_market_open(_utc(2026, 9, 27, 20, 50)) is False   # dimanche 23:50 serveur (été)
    assert clock.forex_market_open(_utc(2026, 9, 27, 21, 10)) is True    # lundi 00:10 serveur
    assert clock.forex_market_open(_utc(2026, 12, 6, 21, 30)) is False   # hiver : dimanche 23:30 serveur
    assert clock.forex_market_open(_utc(2026, 12, 6, 22, 10)) is True    # lundi 00:10 serveur


def test_fermeture_vendredi_et_week_end(monkeypatch):
    monkeypatch.setattr(clock, "SERVER_UTC_OFFSET_SEC", 0)
    assert clock.forex_market_open(_utc(2026, 10, 2, 20, 50)) is True    # vendredi 23:50 serveur (été)
    assert clock.forex_market_open(_utc(2026, 10, 2, 20, 56)) is False   # vendredi 23:56 serveur
    assert clock.forex_market_open(_utc(2026, 10, 3, 12, 0)) is False    # samedi
    assert clock.forex_market_open(_utc(2026, 9, 30, 12, 0)) is True     # mercredi


def test_decalage_mesure_prioritaire(monkeypatch):
    monkeypatch.setattr(clock, "SERVER_UTC_OFFSET_SEC", 3 * 3600)
    assert clock.forex_market_open(_utc(2026, 9, 27, 21, 10)) is True
    monkeypatch.setattr(clock, "SERVER_UTC_OFFSET_SEC", 2 * 3600)
    assert clock.forex_market_open(_utc(2026, 9, 27, 21, 10)) is False
