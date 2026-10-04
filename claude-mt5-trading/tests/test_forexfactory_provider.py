"""Provider calendrier ForexFactory : source publique, sans clé, provenance FACT.

Motivation : les endpoints calendrier de FMP renvoient HTTP 402 sur le plan gratuit.
Cette source fournit elle-même le niveau d'impact, ce qui permet de garder `FACT` sur une
donnée qui alimente un contrôle de sécurité du gate (fenêtre de blocage autour des
publications à fort impact).

Aucun test ne touche le réseau : `_get_json` est remplacé par une charge figée, calquée
sur la réponse réelle relevée le 2026-09-20.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.types import Provenance  # noqa: E402
from tradinglab.news.providers import (  # noqa: E402
    ForexFactoryProvider,
    NullProvider,
    ProviderError,
    build_providers,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

#: extrait fidèle de la réponse réelle (champs : country, date, forecast, impact, previous, title)
PAYLOAD = [
    {"title": "Employment Change", "country": "AUD", "date": "2026-09-24T01:30:00-04:00",
     "impact": "High", "forecast": "20.9K", "previous": "-15.8K"},
    {"title": "CPI y/y", "country": "GBP", "date": "2026-09-22T02:00:00-04:00",
     "impact": "High", "forecast": "3.1%", "previous": "2.9%"},
    {"title": "Consumer Confidence", "country": "EUR", "date": "2026-09-22T10:00:00-04:00",
     "impact": "Medium", "forecast": "", "previous": "-15.4"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-09-22T00:00:00-04:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
    {"title": "BRICS Summit", "country": "All", "date": "2026-09-23T04:15:00-04:00",
     "impact": "Low", "forecast": "", "previous": ""},
    {"title": "Hors fenêtre", "country": "USD", "date": "2026-10-15T12:30:00-04:00",
     "impact": "High", "forecast": "", "previous": ""},
    {"title": "", "country": "USD", "date": "2026-09-22T12:30:00-04:00", "impact": "High"},   # sans titre
    {"title": "Sans date", "country": "USD", "date": "", "impact": "High"},                   # sans date
    {"title": "Date illisible", "country": "USD", "date": "pas-une-date", "impact": "High"},
    "ligne non conforme",
]


@pytest.fixture
def provider(monkeypatch):
    p = ForexFactoryProvider(base_url="https://exemple.invalide/calendar.json")
    monkeypatch.setattr(p, "_get_json", lambda: PAYLOAD)
    return p


# ------------------------------------------------------------------ nombres
@pytest.mark.parametrize("brut,attendu", [
    ("0.5%", 0.5), ("-11.0K", -11_000.0), ("2.5M", 2_500_000.0), ("1.2B", 1_200_000_000.0),
    ("1,234", 1234.0), ("-15.4", -15.4), ("4.5", 4.5),
])
def test_parse_number_formats_reels(brut, attendu):
    assert ForexFactoryProvider.parse_number(brut) == pytest.approx(attendu)


@pytest.mark.parametrize("brut", ["", "   ", "-", "--", None, "n/a", "abc"])
def test_parse_number_absent_ou_illisible_donne_none(brut):
    """Une donnée absente reste absente : jamais de valeur approchée inventée."""
    assert ForexFactoryProvider.parse_number(brut) is None


# ------------------------------------------------------------------ parsing
def test_lignes_invalides_ignorees(provider):
    events = provider.parse_calendar(PAYLOAD)
    titres = {e.title for e in events}
    assert "Sans date" not in titres and "Date illisible" not in titres
    assert "" not in titres
    assert len(events) == 6        # les 4 lignes inexploitables sont écartées


def test_impact_et_provenance(provider):
    par_titre = {e.title: e for e in provider.parse_calendar(PAYLOAD)}
    assert par_titre["Employment Change"].importance == "HIGH"
    assert par_titre["Consumer Confidence"].importance == "MEDIUM"
    assert par_titre["BRICS Summit"].importance == "LOW"
    assert par_titre["Bank Holiday"].importance == "LOW"       # Holiday -> LOW
    assert all(e.provenance is Provenance.FACT for e in par_titre.values())


def test_horodatage_converti_en_utc(provider):
    e = {x.title: x for x in provider.parse_calendar(PAYLOAD)}["Employment Change"]
    assert e.timestamp == datetime(2026, 9, 24, 5, 30, tzinfo=timezone.utc)   # -04:00 -> UTC
    assert e.timestamp.tzinfo is timezone.utc


def test_valeurs_numeriques_et_actual_absent(provider):
    e = {x.title: x for x in provider.parse_calendar(PAYLOAD)}["Employment Change"]
    assert e.previous == pytest.approx(-15_800.0) and e.forecast == pytest.approx(20_900.0)
    assert e.actual is None, "la source ne fournit pas `actual`"
    assert e.surprise is None, "sans `actual`, aucune surprise ne peut être calculée"
    assert e.currency == "AUD"


def test_payload_non_liste_leve_provider_error(provider):
    for mauvais in ({"events": []}, None, "texte"):
        with pytest.raises(ProviderError):
            provider.parse_calendar(mauvais)


# ------------------------------------------------------------------ fenêtre et contrat
def test_fetch_calendar_filtre_la_fenetre(provider):
    events = provider.fetch_calendar(NOW, days_ahead=3)
    titres = {e.title for e in events}
    assert "Hors fenêtre" not in titres, "un événement à trois semaines ne doit pas passer"
    assert "Employment Change" in titres
    assert all(e.timestamp <= NOW.replace(day=24, hour=12) for e in events)


def test_source_calendrier_uniquement(provider):
    """Le provider ne prétend pas fournir de news : il le dit au lieu de renvoyer une liste vide."""
    with pytest.raises(ProviderError):
        provider.fetch_news(NOW)


def test_disponible_sans_cle_api():
    assert ForexFactoryProvider(base_url="https://exemple.invalide/x.json").available() is True
    assert ForexFactoryProvider(base_url="").available() is False


# ------------------------------------------------------------------ fabrique
def test_build_providers_accepte_une_source_sans_cle():
    cfg = {"providers": [
        {"name": "ff", "kind": "economic_calendar", "impl": "forexfactory",
         "base_url": "https://exemple.invalide/x.json"},
    ]}
    (p,) = build_providers(cfg)
    assert isinstance(p, ForexFactoryProvider) and p.name == "ff"


def test_build_providers_refuse_toujours_fmp_sans_cle():
    """La tolérance ne vaut que pour les sources publiques : FMP sans clé reste inerte."""
    cfg = {"providers": [
        {"name": "fmp_x", "kind": "economic_calendar",
         "base_url": "https://financialmodelingprep.com/stable"},
    ]}
    (p,) = build_providers(cfg)
    assert isinstance(p, NullProvider) and p.available() is False


def test_config_du_projet_declare_forexfactory():
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text(encoding="utf-8"))
    provs = cfg["news"]["providers"]
    ff = [p for p in provs if p.get("impl") == "forexfactory"]
    assert ff, "la source gratuite doit rester déclarée"
    assert ff[0]["enabled"] is True
    assert "api_key_env" not in ff[0], "source publique : aucune clé ne doit être exigée"
    noms = [p["name"] for p in provs if p.get("kind") == "economic_calendar"]
    assert noms[0] == ff[0]["name"], "la source sans clé doit être essayée en premier"
