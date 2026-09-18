"""Fournisseurs de news et de calendrier économique (API structurées uniquement).

Principes :
- aucune donnée inventée : toute erreur réseau ou de parsing lève ``ProviderError``
  et ne renvoie jamais une valeur par défaut « plausible » ;
- les clés API sont lues dans l'environnement (``api_key_env``), jamais dans les
  YAML, et jamais écrites dans les journaux ni dans les messages d'erreur ;
- réseau uniquement via ``urllib.request`` avec timeout explicite ;
- les horodatages sont systématiquement convertis en UTC « aware ».
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from tradinglab.core.types import CalendarEvent, NewsItem, Provenance, utcnow

DEFAULT_TIMEOUT_S = 10.0
USER_AGENT = "tradinglab/1.0 (+urllib)"


class ProviderError(RuntimeError):
    """Erreur réseau / HTTP / parsing d'un provider. Jamais masquée par une valeur inventée."""


# ---------------------------------------------------------------------------
# Utilitaires de parsing tolérant (mais jamais inventif)
# ---------------------------------------------------------------------------

def mask_secret(text: str, secret: Optional[str]) -> str:
    """Remplace toute occurrence de la clé API par ``***`` dans un message."""
    if not secret:
        return text
    return text.replace(secret, "***")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Convertit une valeur (ISO 8601, 'YYYY-MM-DD HH:MM:SS', epoch) en datetime UTC aware.

    Retourne ``None`` si la valeur est absente ou inexploitable : l'appelant
    décide alors d'ignorer l'enregistrement (il n'y a pas d'horodatage inventé).
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            # FMP renvoie parfois des epochs en millisecondes
            ts = float(value)
            if ts > 1e11:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    dt = datetime.strptime(s, "%Y-%m-%d")
                except ValueError:
                    return None
    else:
        return None
    if dt.tzinfo is None:
        # FMP documente ses horodatages en UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_number(value: Any) -> Optional[float]:
    """Convertit ``'3.2%'``, ``'-0,5'``, ``'1.2K'``, ``None`` ... en float (ou None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().replace("%", "").replace("$", "").replace(" ", "")
    if re.fullmatch(r"-?\d+,\d{1,2}[KkMmBb]?", s) and "." not in s:
        s = s.replace(",", ".")      # virgule décimale ('-0,5' → -0.5)
    else:
        s = s.replace(",", "")       # séparateur de milliers ('1,200' → 1200)
    if s in ("", "-", "--", "n/a", "N/A", "null"):
        return None
    mult = 1.0
    if s and s[-1] in "KkMmBb":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1].lower()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def normalize_importance(value: Any) -> str:
    """Normalise ``Low/Medium/High``, ``1/2/3``, ``★★★`` ... en LOW|MEDIUM|HIGH.

    Une valeur inconnue est classée LOW (jamais HIGH par défaut, pour ne pas
    inventer un événement bloquant ; le blocage repose sur des faits explicites).
    """
    if value is None:
        return "LOW"
    if isinstance(value, (int, float)):
        return {1: "LOW", 2: "MEDIUM", 3: "HIGH"}.get(int(value), "LOW")
    s = str(value).strip().upper()
    if s in ("HIGH", "H", "3", "RED", "★★★"):
        return "HIGH"
    if s in ("MEDIUM", "MED", "M", "MODERATE", "2", "ORANGE", "★★"):
        return "MEDIUM"
    return "LOW"


# Correspondance pays -> devise lorsque l'API ne fournit que le pays (fait tabulaire, pas une inférence).
COUNTRY_TO_CURRENCY = {
    "US": "USD", "USA": "USD", "UNITED STATES": "USD",
    "EU": "EUR", "EA": "EUR", "EMU": "EUR", "EUROZONE": "EUR", "EURO AREA": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "GB": "GBP", "UK": "GBP", "UNITED KINGDOM": "GBP",
    "JP": "JPY", "JAPAN": "JPY",
    "CH": "CHF", "SWITZERLAND": "CHF",
    "AU": "AUD", "AUSTRALIA": "AUD",
    "NZ": "NZD", "NEW ZEALAND": "NZD",
    "CA": "CAD", "CANADA": "CAD",
    "CN": "CNY", "CHINA": "CNY",
}


def _first(d: dict, *keys: str) -> Any:
    """Première clé présente et non vide dans le dict."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class NewsProvider(ABC):
    """Interface commune à tous les fournisseurs (news ou calendrier)."""

    name: str = "abstract"
    kind: str = "news"              # "news" | "economic_calendar"
    quality: str = "structured_api"

    @abstractmethod
    def available(self) -> bool:
        """Vrai si le provider est utilisable (ex. clé API présente)."""

    @abstractmethod
    def fetch_news(self, now: datetime) -> list[NewsItem]:
        """Retourne les news récentes. Lève ``ProviderError`` en cas d'échec."""

    @abstractmethod
    def fetch_calendar(self, now: datetime, days_ahead: int = 2) -> list[CalendarEvent]:
        """Retourne les événements du calendrier. Lève ``ProviderError`` en cas d'échec."""


class NullProvider(NewsProvider):
    """Provider inerte : jamais disponible, ne renvoie rien (aucune donnée inventée)."""

    def __init__(self, name: str = "null", kind: str = "news") -> None:
        self.name = name
        self.kind = kind
        self.quality = "none"

    def available(self) -> bool:
        return False

    def fetch_news(self, now: datetime) -> list[NewsItem]:
        return []

    def fetch_calendar(self, now: datetime, days_ahead: int = 2) -> list[CalendarEvent]:
        return []


# ---------------------------------------------------------------------------
# Financial Modeling Prep (endpoints "stable")
# ---------------------------------------------------------------------------

class FMPProvider(NewsProvider):
    """Financial Modeling Prep : calendrier économique et news forex.

    Endpoints :
    - ``GET {base_url}/economic-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&apikey=...``
    - ``GET {base_url}/news/forex-latest?limit=50&apikey=...`` ; si 404 :
      ``GET {base_url}/fmp-articles?limit=50&apikey=...``
    """

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        name: str = "fmp",
        kind: str = "economic_calendar",
        quality: str = "structured_api",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.name = name
        self.kind = kind
        self.quality = quality
        self.timeout = float(timeout)

    # ----- accès clé -----
    def _api_key(self) -> Optional[str]:
        v = os.environ.get(self.api_key_env)
        return v if v else None

    def available(self) -> bool:
        return self._api_key() is not None

    # ----- HTTP -----
    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        """GET JSON. Lève ``ProviderError`` (clé masquée) sur toute erreur.

        Méthode volontairement isolée pour être remplacée dans les tests.
        """
        key = self._api_key()
        if key is None:
            raise ProviderError(f"{self.name}: clé API absente ({self.api_key_env})")
        query = dict(params)
        query["apikey"] = key
        url = f"{self.base_url}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise ProviderError(f"{self.name}: HTTP {e.code} sur /{path.lstrip('/')}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            # http.client.HTTPException (IncompleteRead, BadStatusLine…) n'hérite pas d'OSError ; ValueError = URL mal formée
            raise ProviderError(mask_secret(f"{self.name}: erreur réseau sur /{path.lstrip('/')}: {type(e).__name__}: {e}", key)) from None
        if status != 200:
            raise ProviderError(f"{self.name}: HTTP {status} sur /{path.lstrip('/')}")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProviderError(mask_secret(f"{self.name}: JSON invalide sur /{path.lstrip('/')}: {e}", key)) from None

    @staticmethod
    def _is_404(err: ProviderError) -> bool:
        return "HTTP 404" in str(err)

    # ----- calendrier -----
    def fetch_calendar(self, now: datetime, days_ahead: int = 2) -> list[CalendarEvent]:
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        d_from: date = (now - timedelta(days=1)).date()
        d_to: date = (now + timedelta(days=int(days_ahead))).date()
        payload = self._get_json("economic-calendar", {"from": d_from.isoformat(), "to": d_to.isoformat()})
        return self.parse_calendar(payload)

    def parse_calendar(self, payload: Any) -> list[CalendarEvent]:
        """Parse la réponse du calendrier. Les lignes sans date/titre sont ignorées."""
        if isinstance(payload, dict):
            if "Error Message" in payload or "error" in payload:
                raise ProviderError(f"{self.name}: réponse d'erreur de l'API calendrier")
            payload = payload.get("data") or payload.get("results") or []
        if not isinstance(payload, list):
            raise ProviderError(f"{self.name}: format calendrier inattendu ({type(payload).__name__})")
        out: list[CalendarEvent] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            ts = parse_timestamp(_first(row, "date", "datetime", "timestamp"))
            title = _first(row, "event", "title", "name")
            if ts is None or not title:
                continue
            currency = str(_first(row, "currency") or "").strip().upper()
            if not currency:
                country = str(_first(row, "country") or "").strip().upper()
                currency = COUNTRY_TO_CURRENCY.get(country, country)
            importance = normalize_importance(_first(row, "impact", "importance"))
            out.append(
                CalendarEvent(
                    timestamp=ts,
                    source=self.name,
                    title=str(title).strip(),
                    currency=currency,
                    importance=importance,
                    actual=parse_number(_first(row, "actual")),
                    forecast=parse_number(_first(row, "estimate", "forecast", "consensus")),
                    previous=parse_number(_first(row, "previous")),
                    provenance=Provenance.FACT,
                )
            )
        return out

    # ----- news -----
    def fetch_news(self, now: datetime) -> list[NewsItem]:
        try:
            payload = self._get_json("news/forex-latest", {"limit": 50})
        except ProviderError as e:
            if not self._is_404(e):
                raise
            payload = self._get_json("fmp-articles", {"limit": 50})
        return self.parse_news(payload)

    def parse_news(self, payload: Any) -> list[NewsItem]:
        """Parse la réponse news. Importance/actifs/direction sont dérivés en aval (hub)."""
        if isinstance(payload, dict):
            if "Error Message" in payload or "error" in payload:
                raise ProviderError(f"{self.name}: réponse d'erreur de l'API news")
            payload = payload.get("content") or payload.get("data") or payload.get("results") or []
        if not isinstance(payload, list):
            raise ProviderError(f"{self.name}: format news inattendu ({type(payload).__name__})")
        # Import local pour éviter un cycle providers <-> hub
        from tradinglab.news.hub import assets_from_text, classify_importance

        out: list[NewsItem] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            ts = parse_timestamp(_first(row, "publishedDate", "date", "published_at"))
            title = _first(row, "title", "headline")
            if ts is None or not title:
                continue
            text = str(_first(row, "text", "content", "summary") or "")
            symbols = _first(row, "symbol", "tickers")
            assets: list[str] = []
            if isinstance(symbols, str):
                assets = [s.strip().upper() for s in symbols.replace(";", ",").split(",") if s.strip()]
            elif isinstance(symbols, list):
                assets = [str(s).strip().upper() for s in symbols if str(s).strip()]
            for a in assets_from_text(str(title), text):
                if a not in assets:
                    assets.append(a)
            site = str(_first(row, "site", "publisher") or self.name)
            out.append(
                NewsItem(
                    timestamp=ts,
                    source=f"{self.name}:{site}" if site != self.name else self.name,
                    title=str(title).strip(),
                    assets=assets,
                    importance=classify_importance(str(title), text),
                    quality=self.quality,
                    provenance=Provenance.FACT,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Fabrique
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES = {
    "fmp": FMPProvider,
}


def build_providers(news_cfg: dict) -> list[NewsProvider]:
    """Construit les providers depuis ``news_cfg["providers"]`` (voir news_sources.yaml).

    Un provider désactivé est ignoré ; un provider de type inconnu devient un
    ``NullProvider`` (jamais disponible) afin que le hub passe en mode dégradé
    plutôt que d'inventer des données.
    """
    providers: list[NewsProvider] = []
    for p in (news_cfg or {}).get("providers", []) or []:
        if not isinstance(p, dict) or not p.get("enabled", True):
            continue
        name = str(p.get("name", "unknown"))
        kind = str(p.get("kind", "news"))
        base_url = str(p.get("base_url", ""))
        impl = str(p.get("impl") or ("fmp" if name.startswith("fmp") or "financialmodelingprep" in base_url else ""))
        cls = _PROVIDER_CLASSES.get(impl)
        if cls is None or not base_url or not p.get("api_key_env"):
            providers.append(NullProvider(name=name, kind=kind))
            continue
        providers.append(
            cls(
                base_url=base_url,
                api_key_env=str(p["api_key_env"]),
                name=name,
                kind=kind,
                quality=str(p.get("quality", "structured_api")),
                timeout=float(p.get("timeout_seconds", DEFAULT_TIMEOUT_S)),
            )
        )
    return providers


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FMPProvider",
    "NewsProvider",
    "NullProvider",
    "ProviderError",
    "build_providers",
    "mask_secret",
    "normalize_importance",
    "parse_number",
    "parse_timestamp",
    "utcnow",
]
