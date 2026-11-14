"""Tests du pipeline News & Macro (sans réseau : FakeProvider et monkeypatch)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradinglab.core.types import CalendarEvent, NewsItem, Provenance
from tradinglab.macro.sentiment import risk_sentiment
from tradinglab.news.hub import (
    NewsCheck,
    NewsHub,
    NewsState,
    assets_from_text,
    classify_importance,
    detect_conflicts,
    direction_hint,
)
from tradinglab.news.providers import (
    FMPProvider,
    NewsProvider,
    NullProvider,
    ProviderError,
    build_providers,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

CFG = {
    "max_age_minutes": 90,
    "block_minutes_before_high_impact": 30,
    "block_minutes_after_high_impact": 15,
    "degraded_after_failures": 3,
}


class FakeProvider(NewsProvider):
    """Provider de test : renvoie des listes construites, ou lève ProviderError."""

    def __init__(self, kind: str = "economic_calendar", news=None, events=None, fail: bool = False, available: bool = True, name: str = "fake"):
        self.name = name
        self.kind = kind
        self.quality = "structured_api"
        self._news = list(news or [])
        self._events = list(events or [])
        self._fail = fail
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def fetch_news(self, now):
        self.calls += 1
        if self._fail:
            raise ProviderError("fake: boom")
        return list(self._news)

    def fetch_calendar(self, now, days_ahead: int = 2):
        self.calls += 1
        if self._fail:
            raise ProviderError("fake: boom")
        return list(self._events)


def ev(minutes: int, currency: str, importance: str = "HIGH", title: str = "Event", actual=None, forecast=None) -> CalendarEvent:
    return CalendarEvent(
        timestamp=NOW + timedelta(minutes=minutes),
        source="fake",
        title=title,
        currency=currency,
        importance=importance,
        actual=actual,
        forecast=forecast,
    )


def news(minutes: int, title: str, assets: list[str], importance: str = "MEDIUM", direction: str = "UNKNOWN", source: str = "fake") -> NewsItem:
    return NewsItem(
        timestamp=NOW + timedelta(minutes=minutes),
        source=source,
        title=title,
        assets=assets,
        importance=importance,
        direction=direction,
        confidence=0.3 if direction != "UNKNOWN" else 0.0,
    )


def make_hub(events=None, items=None, cache_dir=None) -> NewsHub:
    providers = [
        FakeProvider(kind="economic_calendar", events=events, name="cal"),
        FakeProvider(kind="news", news=items, name="news"),
    ]
    hub = NewsHub(providers, CFG, cache_dir=cache_dir)
    hub.refresh_calendar(NOW)
    hub.refresh_news(NOW)
    return hub


# ---------------------------------------------------------------------------
# État dégradé
# ---------------------------------------------------------------------------

def test_no_provider_available_is_degraded():
    hub = NewsHub([NullProvider("n1", "news"), NullProvider("n2", "economic_calendar")], CFG)
    assert hub.state.degraded is True
    assert hub.state.calendar_degraded is True
    assert hub.state.to_dict()["flag"] == "NEWS_DATA_DEGRADED"

    sensitive = hub.check(["EUR", "USD"], NOW, news_sensitive_strategy=True)
    assert sensitive.state == "DEGRADED" and sensitive.ok is False

    other = hub.check(["EUR", "USD"], NOW, news_sensitive_strategy=False)
    assert other.state == "DEGRADED" and other.ok is True


def test_empty_provider_list_is_degraded():
    hub = NewsHub([], CFG)
    assert hub.state.degraded is True
    assert hub.check(["EUR", "USD"], NOW).state == "DEGRADED"


def test_provider_failing_three_times_becomes_degraded():
    cal = FakeProvider(kind="economic_calendar", fail=True)
    hub = NewsHub([cal], CFG)
    assert hub.state.degraded is False
    hub.refresh_calendar(NOW)
    hub.refresh_calendar(NOW + timedelta(minutes=1))
    assert hub.state.degraded is False
    assert hub.state.failures == 2
    hub.refresh_calendar(NOW + timedelta(minutes=2))
    assert hub.state.failures == 3
    assert hub.state.degraded is True
    assert hub.state.calendar_degraded is True
    assert hub.check(["USD"], NOW + timedelta(minutes=3), news_sensitive_strategy=True).ok is False


def test_failure_counter_resets_after_success():
    cal = FakeProvider(kind="economic_calendar", fail=True)
    hub = NewsHub([cal], CFG)
    hub.refresh_calendar(NOW)
    hub.refresh_calendar(NOW)
    cal._fail = False
    hub.refresh_calendar(NOW)
    assert hub.state.failures == 0
    assert hub.state.degraded is False
    assert hub.state.last_calendar_at == NOW


# ---------------------------------------------------------------------------
# Règles de blocage
# ---------------------------------------------------------------------------

def test_high_usd_event_in_10_minutes_blocks_eurusd():
    hub = make_hub(events=[ev(10, "USD", "HIGH", "FOMC Rate Decision")])
    res = hub.check(["EUR", "USD"], NOW)
    assert isinstance(res, NewsCheck)
    assert res.ok is False
    assert res.state == "BLOCKED_PRE_NEWS"
    assert res.events and res.events[0]["currency"] == "USD"
    # Une paire sans USD n'est pas concernée
    assert hub.check(["EUR", "JPY"], NOW).state == "OK"


def test_high_gbp_event_5_minutes_ago_blocks_gbpusd_not_eurjpy():
    hub = make_hub(events=[ev(-5, "GBP", "HIGH", "BoE Rate Decision")])
    assert hub.check(["GBP", "USD"], NOW).state == "BLOCKED_POST_NEWS"
    assert hub.check(["GBP", "USD"], NOW).ok is False
    res = hub.check(["EUR", "JPY"], NOW)
    assert res.state == "OK" and res.ok is True


def test_medium_event_does_not_block():
    hub = make_hub(events=[ev(5, "USD", "MEDIUM", "ISM PMI")])
    res = hub.check(["EUR", "USD"], NOW)
    assert res.ok is True and res.state == "OK"


def test_event_outside_windows_does_not_block():
    hub = make_hub(events=[ev(45, "USD", "HIGH"), ev(-20, "USD", "HIGH")])
    assert hub.check(["EUR", "USD"], NOW).state == "OK"
    # Bornes incluses
    hub2 = make_hub(events=[ev(30, "USD", "HIGH")])
    assert hub2.check(["USD"], NOW).state == "BLOCKED_PRE_NEWS"
    hub3 = make_hub(events=[ev(-15, "USD", "HIGH")])
    assert hub3.check(["USD"], NOW).state == "BLOCKED_POST_NEWS"


def test_news_shock_on_surprise_and_high_news():
    # Événement HIGH publié il y a 20 min (hors fenêtre post 15 min) mais pas de surprise → pas de choc
    hub = make_hub(events=[ev(-20, "USD", "HIGH", "CPI", actual=3.0, forecast=3.0)])
    assert hub.news_shock(["USD"], NOW) is False
    # Événement HIGH il y a 10 min avec surprise → choc (et bloqué POST par la fenêtre)
    hub = make_hub(events=[ev(-10, "USD", "HIGH", "CPI", actual=3.4, forecast=3.0)])
    assert hub.news_shock(["USD"], NOW) is True
    assert hub.check(["USD"], NOW).state == "BLOCKED_POST_NEWS"
    # News HIGH il y a 5 min sans événement calendrier → SHOCK
    hub = make_hub(items=[news(-5, "Emergency rate decision", ["USD"], importance="HIGH")])
    assert hub.news_shock(["USD"], NOW) is True
    res = hub.check(["EUR", "USD"], NOW)
    assert res.state == "SHOCK" and res.ok is False
    assert hub.check(["EUR", "JPY"], NOW).state == "OK"


# ---------------------------------------------------------------------------
# Dédoublonnage, âge, requêtes
# ---------------------------------------------------------------------------

def test_dedup_news_and_events():
    a = news(-5, "ECB holds rates", ["EUR"])
    b = news(-5, "ECB  holds rates ", ["EUR"])          # même minute, espaces différents
    b.timestamp = a.timestamp + timedelta(seconds=30)
    c = news(-6, "ECB holds rates", ["EUR"])            # minute différente → item distinct
    hub = make_hub(items=[a, b, c], events=[ev(10, "USD"), ev(10, "USD")])
    assert hub.state.items_count == 2
    assert hub.state.events_count == 1
    # Un second refresh ne rajoute rien
    assert hub.refresh_news(NOW) == 0
    assert hub.refresh_calendar(NOW) == 0


def test_old_news_excluded_from_recent():
    hub = make_hub(items=[news(-180, "Old GDP print", ["USD"]), news(-30, "Fresh GDP print", ["USD"])])
    titles = [n.title for n in hub.recent_news(NOW, ["USD"])]
    assert titles == ["Fresh GDP print"]
    # max_age_minutes explicite plus court
    assert hub.recent_news(NOW, ["USD"], max_age_minutes=10) == []
    # filtre par actif
    assert hub.recent_news(NOW, ["JPY"]) == []
    assert len(hub.recent_news(NOW, None)) == 1


def test_upcoming_and_recent_events():
    hub = make_hub(events=[ev(60, "USD", "HIGH"), ev(200, "USD", "HIGH"), ev(30, "EUR", "MEDIUM"), ev(-30, "GBP", "HIGH")])
    up = hub.upcoming_events(NOW, ["USD", "EUR"], minutes_ahead=120, min_importance="HIGH")
    assert [e.currency for e in up] == ["USD"]
    up_med = hub.upcoming_events(NOW, ["USD", "EUR"], minutes_ahead=120, min_importance="MEDIUM")
    assert [e.currency for e in up_med] == ["EUR", "USD"]
    rec = hub.recent_events(NOW, ["GBP"], minutes_back=60)
    assert len(rec) == 1 and rec[0].currency == "GBP"
    assert hub.recent_events(NOW, ["GBP"], minutes_back=20) == []


def test_cache_roundtrip(tmp_path: Path):
    hub = make_hub(items=[news(-30, "Fresh GDP print", ["USD"])], events=[ev(10, "USD")], cache_dir=tmp_path)
    cache = tmp_path / "news_cache.json"
    assert cache.exists()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["saved_at"] == NOW.isoformat()

    # Nouveau hub sans provider disponible : recharge le cache mais reste dégradé
    hub2 = NewsHub([NullProvider("n", "economic_calendar")], CFG, cache_dir=tmp_path)
    assert hub2.state.items_count == 1 and hub2.state.events_count == 1
    assert hub2.state.last_news_at == NOW
    assert hub2.is_realtime(NOW + timedelta(minutes=10)) is True
    assert hub2.is_realtime(NOW + timedelta(hours=3)) is False   # plus vieux que max_age → pas temps réel
    assert hub2.recent_news(NOW + timedelta(hours=3), ["USD"]) == []
    assert hub2.state.degraded is True


# ---------------------------------------------------------------------------
# Classification déterministe
# ---------------------------------------------------------------------------

def test_classify_importance():
    assert classify_importance("FOMC rate decision") == "HIGH"
    assert classify_importance("US CPI rises 0.3% m/m", "") == "HIGH"
    assert classify_importance("Nonfarm payrolls beat estimates") == "HIGH"
    assert classify_importance("New tariffs announced on EU goods") == "HIGH"
    assert classify_importance("Manufacturing PMI slips") == "MEDIUM"
    assert classify_importance("Fed's Waller speech at 14:00") == "MEDIUM"
    assert classify_importance("Company X launches new product") == "LOW"
    assert classify_importance("Forward guidance warning") == "LOW"     # 'war' n'est pas dans 'warning'


def test_assets_from_text():
    assert assets_from_text("EUR/USD steady ahead of ECB") == ["EUR", "USD"]
    assert "XAU" in assets_from_text("Gold jumps as dollar falls")
    assert "USD" in assets_from_text("Gold jumps as dollar falls")
    assert assets_from_text("Nasdaq and S&P 500 close higher") == ["US500", "NAS100"]
    assert assets_from_text("BoJ holds policy") == ["JPY"]
    assert assets_from_text("Nothing here") == []


def test_direction_hint_and_provenance():
    assert direction_hint("Dollar surges after strong payrolls")[0] == "BULLISH"
    assert direction_hint("Euro slumps on weak data")[0] == "BEARISH"
    assert direction_hint("Fed holds rates steady as expected")[0] == "NEUTRAL"
    assert direction_hint("Meeting scheduled") == ("UNKNOWN", 0.0)
    d, conf = direction_hint("Dollar surges after strong payrolls")
    assert 0 < conf <= 0.4

    hub = make_hub(items=[news(-5, "Dollar surges after strong payrolls", ["USD"]), news(-6, "Meeting scheduled", ["USD"])])
    items = {n.title: n for n in hub.recent_news(NOW, ["USD"])}
    assert items["Dollar surges after strong payrolls"].direction == "BULLISH"
    assert items["Dollar surges after strong payrolls"].provenance == Provenance.MODEL_INTERPRETATION
    assert items["Meeting scheduled"].direction == "UNKNOWN"
    assert items["Meeting scheduled"].provenance == Provenance.FACT


def test_detect_conflicts():
    a = news(0, "Dollar surges", ["USD"], direction="BULLISH")
    b = news(-10, "Dollar slumps", ["USD", "EUR"], direction="BEARISH")
    c = news(-50, "Dollar slumps early", ["USD"], direction="BEARISH")     # hors fenêtre 30 min
    d = news(-5, "Yen falls", ["JPY"], direction="BEARISH")               # autre actif
    conflicted = detect_conflicts([a, b, c, d])
    assert {x.id for x in conflicted} == {a.id, b.id}
    assert a.conflict_with == [b.id]
    assert b.conflict_with == [a.id]
    assert c.conflict_with == [] and d.conflict_with == []


# ---------------------------------------------------------------------------
# Sentiment macro
# ---------------------------------------------------------------------------

def test_risk_sentiment():
    label, feats = risk_sentiment({"US500": 0.5, "NAS100": 0.8, "AUDJPY": 0.2, "XAUUSD": -0.1})
    assert label == "RISK_ON" and feats["rule"] == "indices_up_audjpy_up"

    label, feats = risk_sentiment({"US500": -0.6, "NAS100": -0.9, "AUDJPY": -0.3})
    assert label == "RISK_OFF"

    label, _ = risk_sentiment({"US500": -0.1, "XAUUSD": 0.8, "AUDJPY": 0.1})
    assert label == "RISK_OFF"

    label, _ = risk_sentiment({"US500": 0.1, "NAS100": 0.0, "AUDJPY": 0.1})
    assert label == "NEUTRAL"

    label, feats = risk_sentiment({})
    assert label == "UNKNOWN" and feats["rule"] == "missing_indices"
    assert risk_sentiment({"XAUUSD": 0.2, "AUDJPY": 0.1})[0] == "UNKNOWN"
    assert risk_sentiment({"US500": float("nan")})[0] == "UNKNOWN"
    # RISK_ON non confirmable sans AUDJPY
    assert risk_sentiment({"US500": 0.9})[0] == "UNKNOWN"
    # Suffixe broker toléré
    assert risk_sentiment({"US500.cash": 0.5, "AUDJPY.m": 0.1})[0] == "RISK_ON"


# ---------------------------------------------------------------------------
# Provider FMP (sans réseau)
# ---------------------------------------------------------------------------

FMP_CALENDAR_JSON = [
    {"date": "2026-09-17 12:30:00", "country": "US", "event": "CPI (MoM)", "currency": "USD",
     "impact": "High", "actual": "0.3%", "estimate": "0.2%", "previous": "0.1%"},
    {"date": "2026-09-17T14:00:00Z", "country": "GB", "event": "BoE Gov Bailey Speaks",
     "importance": "Medium", "actual": None, "consensus": None, "previous": None},
    {"date": "2026-09-18", "country": "JP", "event": "Trade Balance", "impact": "Low", "actual": "1.2K"},
    {"event": "Sans date", "country": "US"},
    "garbage",
]

FMP_NEWS_JSON = [
    {"publishedDate": "2026-09-17 11:55:00", "title": "EUR/USD falls after ECB rate decision",
     "text": "The euro dropped as the ECB cut rates.", "symbol": "EURUSD", "site": "fxstreet"},
    {"publishedDate": "2026-09-17T11:50:00.000Z", "title": "Gold steady", "text": "", "tickers": ["XAUUSD"], "publisher": "reuters"},
    {"title": "Sans date"},
]


def test_fmp_available_requires_env(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    p = FMPProvider(base_url="https://financialmodelingprep.com/stable", api_key_env="FMP_API_KEY", name="fmp", kind="news")
    assert p.available() is False
    with pytest.raises(ProviderError):
        p.fetch_news(NOW)
    monkeypatch.setenv("FMP_API_KEY", "secret-key")
    assert p.available() is True


def test_fmp_parsing_calendar_and_news(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "secret-key")
    p = FMPProvider(base_url="https://financialmodelingprep.com/stable/", api_key_env="FMP_API_KEY", name="fmp", kind="economic_calendar")
    calls: list[tuple[str, dict]] = []

    def fake_get_json(path, params):
        calls.append((path, dict(params)))
        if path == "economic-calendar":
            return FMP_CALENDAR_JSON
        if path == "news/forex-latest":
            return FMP_NEWS_JSON
        raise AssertionError(path)

    monkeypatch.setattr(p, "_get_json", fake_get_json)

    events = p.fetch_calendar(NOW, days_ahead=2)
    assert calls[0][0] == "economic-calendar"
    assert calls[0][1] == {"from": "2026-09-16", "to": "2026-09-19"}
    assert len(events) == 3
    cpi = events[0]
    assert cpi.timestamp == datetime(2026, 9, 17, 12, 30, tzinfo=timezone.utc)
    assert cpi.currency == "USD" and cpi.importance == "HIGH"
    assert cpi.actual == pytest.approx(0.3) and cpi.forecast == pytest.approx(0.2) and cpi.previous == pytest.approx(0.1)
    assert cpi.surprise == pytest.approx(0.1)
    assert cpi.provenance == Provenance.FACT
    boe = events[1]
    assert boe.currency == "GBP" and boe.importance == "MEDIUM" and boe.actual is None and boe.surprise is None
    assert boe.timestamp.tzinfo is not None
    jp = events[2]
    assert jp.currency == "JPY" and jp.importance == "LOW" and jp.actual == pytest.approx(1200.0)

    items = p.fetch_news(NOW)
    assert len(items) == 2
    first = items[0]
    assert first.timestamp == datetime(2026, 9, 17, 11, 55, tzinfo=timezone.utc)
    assert first.source == "fmp:fxstreet"
    assert "EURUSD" in first.assets and "EUR" in first.assets and "USD" in first.assets
    assert first.importance == "HIGH"
    assert first.provenance == Provenance.FACT
    second = items[1]
    assert second.assets[:1] == ["XAUUSD"] and "XAU" in second.assets
    assert second.source == "fmp:reuters"


def test_fmp_news_fallback_on_404_and_error_masking(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "secret-key")
    p = FMPProvider(base_url="https://financialmodelingprep.com/stable", api_key_env="FMP_API_KEY", name="fmp", kind="news")
    calls: list[str] = []

    def fake_get_json(path, params):
        calls.append(path)
        if path == "news/forex-latest":
            raise ProviderError("fmp: HTTP 404 sur /news/forex-latest")
        return FMP_NEWS_JSON

    monkeypatch.setattr(p, "_get_json", fake_get_json)
    items = p.fetch_news(NOW)
    assert calls == ["news/forex-latest", "fmp-articles"]
    assert len(items) == 2

    # Erreur non-404 : propagée telle quelle
    def fake_500(path, params):
        raise ProviderError("fmp: HTTP 500 sur /news/forex-latest")

    monkeypatch.setattr(p, "_get_json", fake_500)
    with pytest.raises(ProviderError):
        p.fetch_news(NOW)

    # Format inattendu → ProviderError (pas de valeur inventée)
    with pytest.raises(ProviderError):
        p.parse_calendar({"Error Message": "Invalid API key"})
    with pytest.raises(ProviderError):
        p.parse_news("not a list")


def test_fmp_network_error_never_leaks_key(monkeypatch):
    import urllib.request
    from urllib.error import URLError

    monkeypatch.setenv("FMP_API_KEY", "super-secret-key")
    p = FMPProvider(base_url="https://financialmodelingprep.com/stable", api_key_env="FMP_API_KEY", name="fmp", kind="news")

    def fake_urlopen(req, timeout=None):
        raise URLError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProviderError) as exc:
        p.fetch_news(NOW)
    assert "super-secret-key" not in str(exc.value)


def test_build_providers_from_config(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    cfg = {
        "providers": [
            {"name": "fmp_economic_calendar", "kind": "economic_calendar", "enabled": True,
             "api_key_env": "FMP_API_KEY", "base_url": "https://financialmodelingprep.com/stable", "quality": "structured_api"},
            {"name": "fmp_news", "kind": "news", "enabled": True,
             "api_key_env": "FMP_API_KEY", "base_url": "https://financialmodelingprep.com/stable"},
            {"name": "disabled", "kind": "news", "enabled": False, "api_key_env": "X", "base_url": "https://x"},
            {"name": "unknown_vendor", "kind": "news", "enabled": True, "api_key_env": "X", "base_url": "https://x"},
        ]
    }
    providers = build_providers(cfg)
    assert [p.name for p in providers] == ["fmp_economic_calendar", "fmp_news", "unknown_vendor"]
    assert isinstance(providers[0], FMPProvider) and providers[0].kind == "economic_calendar"
    assert isinstance(providers[1], FMPProvider) and providers[1].kind == "news"
    assert isinstance(providers[2], NullProvider)
    assert all(p.available() is False for p in providers)

    hub = NewsHub(providers, CFG)
    assert hub.state.degraded is True
    monkeypatch.setenv("FMP_API_KEY", "k")
    hub._update_state()
    assert hub.state.degraded is False
    assert hub.state.sources == ["fmp_economic_calendar", "fmp_news"]


def test_news_state_to_dict():
    st = NewsState(degraded=False, calendar_degraded=False, last_news_at=NOW, last_calendar_at=None, failures=0, items_count=1, events_count=2, sources=["a"])
    d = st.to_dict()
    assert d["last_news_at"] == NOW.isoformat() and d["last_calendar_at"] is None and d["flag"] == "OK"
