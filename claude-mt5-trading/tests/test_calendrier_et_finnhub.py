"""Le calendrier protège même quand les titres sont en panne, et provider Finnhub.

**Le défaut.** `NewsHub.check()` sortait en première ligne sur
``self.state.degraded or self.state.calendar_degraded``. Or ce sont deux flux indépendants :
le calendrier (événements datés à l'avance) et les titres. Constaté en production le 2026-09-21 :

    CALENDAR.degraded : false     <- ForexFactory répond, événements en provenance FACT
    NEWS.degraded     : true      <- titres indisponibles (plan FMP gratuit)

Le repli sortait donc immédiatement et la fenêtre de blocage 30 min avant / 15 min après un
événement HIGH **n'était jamais évaluée**. Le bot pouvait ouvrir une position en pleine BCE ou
en plein NFP. Une panne des titres ne doit pas désarmer la protection calendrier, qui est la
plus stricte des deux.

**Finnhub.** Ajouté pour les titres, choisi sur le quota : la cadence news est de 120 s, soit
720 appels/jour. Finnhub annonce 60 appels/min ; Marketaux (~100/jour) et Alpha Vantage
(~25/jour) ne tiennent pas, et le palier gratuit de NewsAPI.org interdit la production. Son
calendrier étant payant, `fetch_calendar` lève au lieu de renvoyer une liste vide : un
calendrier vide serait indiscernable d'un calendrier sans événement.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradinglab.core.types import CalendarEvent, Provenance  # noqa: E402
from tradinglab.news.providers import FinnhubProvider, ProviderError, build_providers  # noqa: E402

MAINTENANT = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------- 1. calendrier vs titres
def _hub_avec_evenement(settings, minutes_avant: int):
    """Hub dont le calendrier fonctionne et porte un événement HIGH à venir."""
    from tradinglab.news.hub import NewsHub

    hub = NewsHub([], settings.news)
    hub.state.degraded = True                 # titres en panne
    hub.state.calendar_degraded = False       # calendrier opérationnel
    ev = CalendarEvent(
        timestamp=MAINTENANT + timedelta(minutes=minutes_avant), source="forexfactory_calendar",
        title="ECB Main Refinancing Rate", currency="EUR", importance="HIGH",
        provenance=Provenance.FACT,
    )
    hub._events = {ev.id: ev}
    hub._update_state = lambda: None          # l'état est posé à la main pour le test
    return hub


def test_titres_en_panne_le_calendrier_bloque_quand_meme(settings):
    """Cœur du correctif : BCE dans 10 min, titres HS → l'entrée doit être refusée."""
    hub = _hub_avec_evenement(settings, minutes_avant=10)
    chk = hub.check(["EUR", "USD"], now=MAINTENANT)
    assert chk.ok is False
    assert chk.state == "BLOCKED_PRE_NEWS"
    assert "ECB" in chk.reason


def test_titres_en_panne_hors_fenetre_reste_degrade(settings):
    """Hors fenêtre, le repli normal reprend : les stratégies news-sensibles restent bloquées."""
    hub = _hub_avec_evenement(settings, minutes_avant=600)
    assert hub.check(["EUR"], now=MAINTENANT, news_sensitive_strategy=True).ok is False
    chk = hub.check(["EUR"], now=MAINTENANT, news_sensitive_strategy=False)
    assert chk.ok is True and chk.state == "DEGRADED"


def test_calendrier_en_panne_le_repli_reste_total(settings):
    """Sans calendrier, aucune fenêtre n'est calculable : on se replie, comme avant."""
    hub = _hub_avec_evenement(settings, minutes_avant=10)
    hub.state.calendar_degraded = True
    chk = hub.check(["EUR"], now=MAINTENANT)
    assert chk.state == "DEGRADED" and "calendrier" in chk.reason


def test_une_autre_devise_n_est_pas_bloquee(settings):
    """Le blocage suit la devise de l'événement : un événement EUR ne gèle pas l'AUD/JPY."""
    hub = _hub_avec_evenement(settings, minutes_avant=10)
    chk = hub.check(["AUD", "JPY"], now=MAINTENANT)
    assert chk.state == "DEGRADED", "pas de fenêtre EUR sur une paire sans EUR"


# ------------------------------------------------------- 2. provider Finnhub
REPONSE = [
    {"category": "forex", "datetime": 1758456000, "headline": "Fed holds rates steady",
     "id": 1, "related": "EURUSD", "source": "Reuters", "summary": "The Federal Reserve..."},
    {"category": "forex", "datetime": 1758452400, "headline": "ECB signals caution",
     "id": 2, "related": "", "source": "Bloomberg", "summary": "Policymakers..."},
]


def _provider(monkeypatch, payload=REPONSE):
    monkeypatch.setenv("FINNHUB_API_KEY", "cle-de-test")
    p = FinnhubProvider("https://finnhub.io/api/v1", "FINNHUB_API_KEY", categories=["forex"])
    monkeypatch.setattr(p, "_get_json", lambda cat: payload)
    return p


def test_finnhub_parse_les_titres(monkeypatch):
    items = _provider(monkeypatch).fetch_news(MAINTENANT)
    assert len(items) == 2
    assert items[0].title == "Fed holds rates steady"
    assert items[0].timestamp.tzinfo is not None, "horodatage converti en UTC aware"
    assert items[0].provenance is Provenance.FACT
    assert "Reuters" in items[0].source


def test_finnhub_sans_cle_est_indisponible(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    p = FinnhubProvider("https://finnhub.io/api/v1", "FINNHUB_API_KEY")
    assert p.available() is False
    with pytest.raises(ProviderError):
        p.fetch_news(MAINTENANT)


def test_finnhub_ne_pretend_pas_avoir_un_calendrier(monkeypatch):
    """Le calendrier est payant : lever, jamais renvoyer une liste vide trompeuse."""
    with pytest.raises(ProviderError, match="payants"):
        _provider(monkeypatch).fetch_calendar(MAINTENANT)


def test_finnhub_ignore_les_lignes_sans_horodatage(monkeypatch):
    """Aucune donnée inventée : une ligne sans date ou sans titre est écartée."""
    p = _provider(monkeypatch, [{"id": 3, "headline": "sans date"}, {"id": 4, "datetime": 1758456000}])
    assert p.fetch_news(MAINTENANT) == []


def test_finnhub_dedoublonne_entre_categories(monkeypatch):
    """Le même article revient dans « forex » et « general » : une seule entrée attendue."""
    monkeypatch.setenv("FINNHUB_API_KEY", "cle-de-test")
    p = FinnhubProvider("https://finnhub.io/api/v1", "FINNHUB_API_KEY", categories=["forex", "general"])
    monkeypatch.setattr(p, "_get_json", lambda cat: REPONSE)
    assert len(p.fetch_news(MAINTENANT)) == 2, "les id identiques ne doivent pas être comptés deux fois"


def test_finnhub_la_cle_ne_fuit_jamais(monkeypatch):
    """Une erreur réseau ne doit jamais recracher le token dans le message."""
    monkeypatch.setenv("FINNHUB_API_KEY", "SECRET-ABC")
    p = FinnhubProvider("http://127.0.0.1:1/api", "FINNHUB_API_KEY", categories=["forex"], timeout=0.2)
    with pytest.raises(ProviderError) as e:
        p.fetch_news(MAINTENANT)
    assert "SECRET-ABC" not in str(e.value)


def test_finnhub_est_construit_par_la_fabrique(settings, monkeypatch):
    """La config doit produire un vrai FinnhubProvider, pas un NullProvider silencieux."""
    monkeypatch.setenv("FINNHUB_API_KEY", "cle-de-test")
    noms = {p.name: p for p in build_providers(settings.news)}
    assert "finnhub_news" in noms, "source absente de news_sources.yaml"
    assert isinstance(noms["finnhub_news"], FinnhubProvider)
    assert noms["finnhub_news"].available() is True
