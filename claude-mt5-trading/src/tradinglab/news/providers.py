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

# ---------------------------------------------------------------------------
# Finnhub (titres forex et crypto, clé gratuite)
# ---------------------------------------------------------------------------

class FinnhubProvider(NewsProvider):
    """Finnhub : titres de marché. ``GET {base_url}/news?category=...&token=...``

    Choisi pour une raison simple et mesurable : la cadence news du labo est de 120 s, soit
    **720 appels par jour**. Marketaux (~100/jour) et Alpha Vantage (~25/jour) ne tiennent pas,
    et l'offre gratuite de NewsAPI.org interdit l'usage en production. Finnhub annonce 60 appels
    par minute sur sa clé gratuite, ce qui laisse une marge confortable.

    Ce provider ne fournit **que** des titres : le calendrier économique de Finnhub est réservé
    aux plans payants, donc ``fetch_calendar`` lève ``ProviderError`` au lieu de renvoyer une liste
    vide — un calendrier vide serait indiscernable d'un calendrier sans événement, et le hub doit
    pouvoir se replier en connaissance de cause. Le calendrier reste servi par ForexFactory.

    Réponse (``datetime`` est un epoch en secondes) :

        {"category": "forex", "datetime": 1596589501, "headline": "...",
         "id": 5085164, "related": "", "source": "Reuters", "summary": "...", "url": "..."}

    L'importance et les actifs ne sont pas fournis : ils sont dérivés en aval par le hub, comme
    pour FMP. La provenance reste ``FACT`` (le titre et son horodatage sont rapportés tels quels).
    """

    #: catégories interrogées, dans l'ordre. "general" couvre la macro, absente de "forex".
    CATEGORIES = ("forex", "crypto", "general")

    def __init__(self, base_url: str, api_key_env: str, name: str = "finnhub", kind: str = "news",
                 quality: str = "structured_api", timeout: float = DEFAULT_TIMEOUT_S,
                 categories: Optional[list[str]] = None, **_ignored: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.name = name
        self.kind = kind
        self.quality = quality
        self.timeout = float(timeout)
        self.categories = tuple(categories) if categories else self.CATEGORIES

    def _api_key(self) -> Optional[str]:
        v = os.environ.get(self.api_key_env, "").strip()
        return v or None

    def available(self) -> bool:
        return self._api_key() is not None

    def _get_json(self, category: str) -> Any:
        """GET JSON pour une catégorie. Lève ``ProviderError`` (clé masquée) sur toute erreur."""
        key = self._api_key()
        if key is None:
            raise ProviderError(f"{self.name}: clé API absente ({self.api_key_env})")
        url = f"{self.base_url}/news?{urllib.parse.urlencode({'category': category, 'token': key})}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read()
        except urllib.error.HTTPError as e:
            # 401/403 = clé invalide, 429 = quota dépassé : tous des échecs francs, jamais un repli silencieux
            raise ProviderError(f"{self.name}: HTTP {e.code} sur /news?category={category}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            raise ProviderError(mask_secret(
                f"{self.name}: erreur réseau sur /news?category={category}: {type(e).__name__}: {e}", key)) from None
        if status != 200:
            raise ProviderError(f"{self.name}: HTTP {status} sur /news?category={category}")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProviderError(mask_secret(f"{self.name}: JSON invalide sur /news?category={category}: {e}", key)) from None

    def fetch_news(self, now: datetime) -> list[NewsItem]:
        """Agrège les catégories. Une seule doit suffire : si toutes échouent, on lève."""
        items: list[NewsItem] = []
        vus: set[int] = set()
        echecs: list[str] = []
        for cat in self.categories:
            try:
                payload = self._get_json(cat)
            except ProviderError as e:
                echecs.append(str(e))
                continue
            for it in self.parse_news(payload, vus):
                items.append(it)
        if not items and echecs:
            raise ProviderError("; ".join(echecs[:3]))
        return items

    def parse_news(self, payload: Any, vus: Optional[set[int]] = None) -> list[NewsItem]:
        """Parse une réponse. ``vus`` déduplique par identifiant Finnhub entre catégories."""
        if isinstance(payload, dict):
            if "error" in payload:
                raise ProviderError(f"{self.name}: réponse d'erreur de l'API news")
            payload = payload.get("data") or payload.get("results") or []
        if not isinstance(payload, list):
            raise ProviderError(f"{self.name}: format news inattendu ({type(payload).__name__})")
        from tradinglab.news.hub import assets_from_text, classify_importance

        vus = vus if vus is not None else set()
        out: list[NewsItem] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            ident = row.get("id")
            if isinstance(ident, int):
                if ident in vus:
                    continue
                vus.add(ident)
            ts = parse_timestamp(_first(row, "datetime", "publishedDate", "date"))
            title = _first(row, "headline", "title")
            if ts is None or not title:
                continue                      # pas d'horodatage inventé, pas de titre reconstruit
            text = str(_first(row, "summary", "text") or "")
            assets: list[str] = []
            related = _first(row, "related", "symbol")
            if isinstance(related, str):
                assets = [s.strip().upper() for s in related.replace(";", ",").split(",") if s.strip()]
            for a in assets_from_text(str(title), text):
                if a not in assets:
                    assets.append(a)
            site = str(_first(row, "source", "publisher") or self.name)
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

    def fetch_calendar(self, now: datetime, days_ahead: int = 2) -> list[CalendarEvent]:
        raise ProviderError(f"{self.name}: calendrier économique réservé aux plans payants")


# ---------------------------------------------------------------------------
# ForexFactory (calendrier hebdomadaire public, sans clé)
# ---------------------------------------------------------------------------

class ForexFactoryProvider(NewsProvider):
    """Calendrier économique ForexFactory : ``GET {base_url}`` (JSON, aucune clé).

    Pourquoi ce provider : les endpoints calendrier de FMP sont réservés aux plans
    payants (HTTP 402). Cette source est gratuite et surtout elle **fournit** le niveau
    d'impact (`impact`) au lieu de le faire déduire, ce qui permet de conserver la
    provenance ``FACT`` sur un contrôle de sécurité du gate.

    Limites assumées, vérifiées le 2026-09-20 :
    - seule la **semaine en cours** est exposée (``ff_calendar_nextweek.json`` → 404) ;
    - le débit est bridé (HTTP 429 après quelques appels rapprochés) : le cache du hub
      est indispensable, la cadence `calendar_interval_sec` ne doit pas être réduite ;
    - le champ ``actual`` n'existe pas : il reste ``None``, donc ``surprise`` aussi.

    C'est une source communautaire, pas une API contractuelle : toute erreur lève
    ``ProviderError`` et le hub bascule en mode dégradé plutôt que d'inventer.
    """

    #: ForexFactory -> importance interne (LOW | MEDIUM | HIGH)
    IMPACTS = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "holiday": "LOW"}

    def __init__(self, base_url: str, name: str = "forexfactory", kind: str = "economic_calendar",
                 quality: str = "structured_api", timeout: float = DEFAULT_TIMEOUT_S, **_ignored: Any) -> None:
        self.base_url = base_url
        self.name = name
        self.kind = kind
        self.quality = quality
        self.timeout = float(timeout)

    def available(self) -> bool:
        return bool(self.base_url)

    # ----- HTTP -----
    def _get_json(self) -> Any:
        """GET JSON. Méthode isolée pour être remplacée dans les tests."""
        req = urllib.request.Request(self.base_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise ProviderError(f"{self.name}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            raise ProviderError(f"{self.name}: erreur réseau: {type(e).__name__}: {e}") from None
        if status != 200:
            raise ProviderError(f"{self.name}: HTTP {status}")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProviderError(f"{self.name}: JSON invalide: {e}") from None

    # ----- parsing -----
    @staticmethod
    def parse_number(raw: Any) -> Optional[float]:
        """'0.5%' -> 0.5 ; '-11.0K' -> -11000 ; '2.5M' -> 2500000 ; '' ou illisible -> None.

        Renvoie ``None`` plutôt qu'une valeur approchée : une donnée absente reste absente.
        """
        if raw is None:
            return None
        t = str(raw).strip().replace(",", "").replace("%", "")
        if not t or t in ("-", "--"):
            return None
        mult = 1.0
        if t[-1:].upper() in ("K", "M", "B", "T"):
            mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[t[-1].upper()]
            t = t[:-1]
        try:
            return float(t) * mult
        except ValueError:
            return None

    def parse_calendar(self, payload: Any) -> list[CalendarEvent]:
        """Parse la réponse. Une ligne sans date ou sans titre exploitable est ignorée."""
        if not isinstance(payload, list):
            raise ProviderError(f"{self.name}: format inattendu ({type(payload).__name__})")
        out: list[CalendarEvent] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            raw_date = str(row.get("date") or "").strip()
            if not title or not raw_date:
                continue
            try:
                ts = datetime.fromisoformat(raw_date)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            out.append(CalendarEvent(
                timestamp=ts.astimezone(timezone.utc),
                source=self.name,
                title=title,
                currency=str(row.get("country") or "").strip().upper(),
                importance=self.IMPACTS.get(str(row.get("impact") or "").strip().lower(), "LOW"),
                actual=None,                                   # non fourni par la source
                forecast=self.parse_number(row.get("forecast")),
                previous=self.parse_number(row.get("previous")),
                provenance=Provenance.FACT,
            ))
        return out

    def fetch_calendar(self, now: datetime, days_ahead: int = 2) -> list[CalendarEvent]:
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        debut = now - timedelta(days=1)
        fin = now + timedelta(days=int(days_ahead))
        return [e for e in self.parse_calendar(self._get_json()) if debut <= e.timestamp <= fin]

    def fetch_news(self, now: datetime) -> list[NewsItem]:
        raise ProviderError(f"{self.name}: source calendrier uniquement, aucune news")


_PROVIDER_CLASSES = {
    "fmp": FMPProvider,
    "finnhub": FinnhubProvider,
    "forexfactory": ForexFactoryProvider,
}

#: implémentations qui n'utilisent aucune clé API (source publique)
_KEYLESS_IMPLS = {"forexfactory"}


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
        sans_cle = impl in _KEYLESS_IMPLS
        # une source publique n'a pas de `api_key_env` : l'exiger la rendrait inutilisable
        if cls is None or not base_url or (not sans_cle and not p.get("api_key_env")):
            providers.append(NullProvider(name=name, kind=kind))
            continue
        kwargs = dict(
            base_url=base_url,
            name=name,
            kind=kind,
            quality=str(p.get("quality", "structured_api")),
            timeout=float(p.get("timeout_seconds", DEFAULT_TIMEOUT_S)),
        )
        if not sans_cle:
            kwargs["api_key_env"] = str(p["api_key_env"])
        providers.append(cls(**kwargs))
    return providers


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FMPProvider",
    "FinnhubProvider",
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
