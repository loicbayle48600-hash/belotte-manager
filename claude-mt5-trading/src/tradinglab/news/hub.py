"""Hub News & Macro : agrégation, cache, dédoublonnage, état dégradé et règles de blocage.

FACT vs INTERPRÉTATION
----------------------
- Un ``CalendarEvent`` (horodatage, devise, importance fournie par l'API, actual /
  forecast / previous) est un **FACT** : il vient tel quel d'une API structurée.
- Un ``NewsItem`` brut (horodatage, source, titre) est un **FACT**.
- ``classify_importance`` et ``assets_from_text`` sont des classifications
  déterministes par mots-clés : elles n'inventent aucune donnée mais restent
  une lecture du texte ; ``importance`` d'une news est donc une heuristique
  documentée, pas une donnée officielle.
- ``direction_hint`` est une **interprétation** (``BULLISH/BEARISH/NEUTRAL``)
  à faible confiance : lorsqu'elle est appliquée à un ``NewsItem``, le champ
  ``provenance`` passe à ``MODEL_INTERPRETATION`` pour signaler qu'une couche
  d'interprétation a été ajoutée. Le fait brut (titre/horodatage/source) reste
  exact ; seule la direction est une hypothèse et doit être traitée comme telle.
- Le hub ne renvoie jamais de donnée inventée : sans provider disponible ou
  après ``degraded_after_failures`` échecs consécutifs, il se déclare dégradé
  (``NEWS_DATA_DEGRADED``) et ``check()`` bloque les stratégies news-sensibles.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from tradinglab.core.types import CalendarEvent, NewsItem, Provenance, utcnow
from tradinglab.news.providers import NewsProvider, ProviderError

log = logging.getLogger("tradinglab.news")

IMPORTANCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

DEFAULT_CFG = {
    "max_age_minutes": 90,
    "block_minutes_before_high_impact": 30,
    "block_minutes_after_high_impact": 15,
    "degraded_after_failures": 3,
    "shock_event_minutes": 15,
    "shock_news_minutes": 10,
    "conflict_window_minutes": 30,
    "news_retention_hours": 24,
    "calendar_retention_hours": 24,
    "calendar_days_ahead": 2,
}

CACHE_FILENAME = "news_cache.json"


# ---------------------------------------------------------------------------
# Classification déterministe (mots-clés)
# ---------------------------------------------------------------------------

_HIGH_PATTERNS = [
    r"\bfomc\b", r"\bfed\b.*\b(decision|rate|minutes)\b", r"\bfederal reserve\b",
    r"\becb\b", r"\beuropean central bank\b",
    r"\bboe\b", r"\bbank of england\b",
    r"\bboj\b", r"\bbank of japan\b",
    r"\bsnb\b", r"\brba\b", r"\brbnz\b", r"\bboc\b", r"\bbank of canada\b",
    r"\brate decision\b", r"\binterest rate decision\b", r"\bcash rate\b", r"\bpolicy rate\b",
    r"\bcpi\b", r"\bconsumer price index\b", r"\bcore inflation\b", r"\bpce\b",
    r"\bnfp\b", r"\bnon-?farm\b", r"\bpayrolls?\b",
    r"\bgdp\b", r"\bgross domestic product\b",
    r"\bemergency\b", r"\bwar\b", r"\bwarfare\b", r"\binvasion\b", r"\bmissile\b",
    r"\btariffs?\b", r"\bsanctions?\b",
    r"\bunemployment rate\b",
]
_MEDIUM_PATTERNS = [
    r"\bpmi\b", r"\bism\b", r"\bretail sales\b", r"\bjobless claims\b", r"\binitial claims\b", r"\bclaims\b",
    r"\bspeech\b", r"\bspeaks\b", r"\btestimony\b", r"\bpress conference\b",
    r"\bppi\b", r"\bproducer price\b", r"\btrade balance\b", r"\bhousing starts\b",
    r"\bconsumer confidence\b", r"\bsentiment\b", r"\bindustrial production\b", r"\bdurable goods\b",
]
_HIGH_RE = re.compile("|".join(_HIGH_PATTERNS), re.IGNORECASE)
_MEDIUM_RE = re.compile("|".join(_MEDIUM_PATTERNS), re.IGNORECASE)


def classify_importance(title: str, text: str = "") -> str:
    """Importance LOW|MEDIUM|HIGH par mots-clés (déterministe, sans modèle).

    HIGH : banques centrales (FOMC/ECB/BoE/BoJ...), décisions de taux, CPI, NFP/
    payrolls, GDP, urgence, guerre, tarifs douaniers. MEDIUM : PMI, ventes au
    détail, inscriptions au chômage, discours. Sinon LOW. Le titre pèse autant
    que le texte : un seul mot-clé suffit.
    """
    blob = f"{title or ''}\n{text or ''}"
    if _HIGH_RE.search(blob):
        return "HIGH"
    if _MEDIUM_RE.search(blob):
        return "MEDIUM"
    return "LOW"


# Détection d'actifs : (regex, actif). L'ordre définit l'ordre de sortie (déterministe).
_ASSET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\busd\b|\bdollar\b|\bgreenback\b|\bfed\b|\bfomc\b|\bfederal reserve\b|\bpowell\b", re.I), "USD"),
    (re.compile(r"\beur\b|\beuro\b|\becb\b|\beuropean central bank\b|\beurozone\b|\blagarde\b", re.I), "EUR"),
    (re.compile(r"\bgbp\b|\bpound\b|\bsterling\b|\bcable\b|\bboe\b|\bbank of england\b", re.I), "GBP"),
    (re.compile(r"\bjpy\b|\byen\b|\bboj\b|\bbank of japan\b|\bueda\b", re.I), "JPY"),
    (re.compile(r"\bchf\b|\bswiss franc\b|\bfranc\b|\bsnb\b", re.I), "CHF"),
    (re.compile(r"\baud\b|\baussie\b|\baustralian dollar\b|\brba\b", re.I), "AUD"),
    (re.compile(r"\bnzd\b|\bkiwi\b|\bnew zealand dollar\b|\brbnz\b", re.I), "NZD"),
    (re.compile(r"\bcad\b|\bloonie\b|\bcanadian dollar\b|\bboc\b|\bbank of canada\b", re.I), "CAD"),
    (re.compile(r"\bcny\b|\bcnh\b|\byuan\b|\brenminbi\b|\bpboc\b", re.I), "CNY"),
    (re.compile(r"\bxau\b|\bgold\b|\bbullion\b", re.I), "XAU"),
    (re.compile(r"\bxag\b|\bsilver\b", re.I), "XAG"),
    (re.compile(r"\bwti\b|\bbrent\b|\bcrude\b|\boil prices?\b|\bopec\b", re.I), "OIL"),
    (re.compile(r"\bs&p ?500\b|\bspx\b|\bus500\b|\bsp500\b|\bwall street\b", re.I), "US500"),
    (re.compile(r"\bnasdaq\b|\bnas100\b|\bustec\b|\bndx\b", re.I), "NAS100"),
    (re.compile(r"\bdow\b|\bus30\b|\bdjia\b", re.I), "US30"),
    (re.compile(r"\bdax\b|\bger40\b|\bde40\b", re.I), "GER40"),
    (re.compile(r"\bftse\b|\buk100\b", re.I), "UK100"),
    (re.compile(r"\bnikkei\b|\bjp225\b", re.I), "JP225"),
    (re.compile(r"\bbtc\b|\bbitcoin\b", re.I), "BTC"),
]

# Paires FX explicites (EURUSD, EUR/USD) -> devises composantes
_PAIR_RE = re.compile(r"\b([A-Z]{3})[/\-]?([A-Z]{3})\b")
_ISO_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNY", "CNH", "XAU", "XAG"}


def assets_from_text(title: str, text: str = "") -> list[str]:
    """Actifs détectés dans le texte (devises ISO, XAU pour l'or, indices), ordre déterministe."""
    blob = f"{title or ''}\n{text or ''}"
    found: list[str] = []
    for m in _PAIR_RE.finditer(blob):
        for c in m.groups():
            c = "XAU" if c == "XAU" else ("CNY" if c == "CNH" else c)
            if c in _ISO_CCY and c not in found:
                found.append(c)
    for rx, asset in _ASSET_PATTERNS:
        if asset not in found and rx.search(blob):
            found.append(asset)
    return found


_BULLISH_RE = re.compile(
    r"\bbeats?\b|\bbetter than expected\b|\babove (expectations|forecast|consensus)\b|\bhawkish\b|"
    r"\brate hike\b|\bhikes?\b|\braises? rates\b|\bsurges?\b|\brall(y|ies)\b|\bjumps?\b|\bsoars?\b|"
    r"\bstrong(er)?\b|\bupbeat\b|\brecord high\b|\bupgrades?\b|\brebounds?\b|\bgains?\b",
    re.I,
)
_BEARISH_RE = re.compile(
    r"\bmiss(es)?\b|\bworse than expected\b|\bbelow (expectations|forecast|consensus)\b|\bdovish\b|"
    r"\brate cut\b|\bcuts? rates\b|\bplunges?\b|\bslumps?\b|\bfalls?\b|\btumbles?\b|\bdrops?\b|"
    r"\bweak(er)?\b|\brecession\b|\bdowngrades?\b|\bsell-?off\b|\bslides?\b|\bdeclines?\b",
    re.I,
)
_NEUTRAL_RE = re.compile(
    r"\bunchanged\b|\bas expected\b|\bin line\b|\bholds? (rates|steady)\b|\bsteady\b|\bflat\b|\bmixed\b",
    re.I,
)


def direction_hint(title: str, text: str = "") -> tuple[str, float]:
    """Interprétation faible de la direction : (BULLISH|BEARISH|NEUTRAL|UNKNOWN, confiance).

    ATTENTION : c'est une INTERPRÉTATION par mots-clés (``MODEL_INTERPRETATION``),
    jamais un fait. La confiance est plafonnée à 0.4 et vaut 0.0 pour UNKNOWN.
    La direction s'entend « pour l'actif principal cité » et ne prend pas en
    compte la sémantique fine (ex. « dollar falls » est BEARISH pour USD).
    """
    blob = f"{title or ''}\n{text or ''}"
    bull = len(_BULLISH_RE.findall(blob))
    bear = len(_BEARISH_RE.findall(blob))
    neutral = len(_NEUTRAL_RE.findall(blob))
    if bull == 0 and bear == 0 and neutral == 0:
        return "UNKNOWN", 0.0
    if bull > bear:
        return "BULLISH", min(0.4, 0.15 * (bull - bear))
    if bear > bull:
        return "BEARISH", min(0.4, 0.15 * (bear - bull))
    # Egalité (mots contradictoires) ou uniquement des marqueurs neutres
    return "NEUTRAL", min(0.4, 0.15 * max(1, neutral))


def annotate_direction(item: NewsItem, text: str = "") -> NewsItem:
    """Applique ``direction_hint`` à un item (mutation en place, retourne l'item).

    Si une direction est déduite, ``provenance`` passe à ``MODEL_INTERPRETATION``
    (le titre/horodatage restent factuels, mais l'item porte désormais une
    interprétation). Sans direction, l'item reste ``FACT``.
    """
    direction, conf = direction_hint(item.title, text)
    item.direction = direction
    item.confidence = conf
    if direction != "UNKNOWN":
        item.provenance = Provenance.MODEL_INTERPRETATION
    return item


def detect_conflicts(items: Iterable[NewsItem], window_minutes: int = 30) -> list[NewsItem]:
    """Marque ``conflict_with`` entre items sur les mêmes actifs, dans la fenêtre, à directions opposées.

    Retourne la liste (dédoublonnée, ordre d'apparition) des items en conflit.
    """
    lst = list(items)
    window = timedelta(minutes=window_minutes)
    conflicted: list[NewsItem] = []
    opposite = {("BULLISH", "BEARISH"), ("BEARISH", "BULLISH")}
    for i, a in enumerate(lst):
        for b in lst[i + 1:]:
            if (a.direction, b.direction) not in opposite:
                continue
            if abs((a.timestamp - b.timestamp)) > window:
                continue
            if not (set(x.upper() for x in a.assets) & set(x.upper() for x in b.assets)):
                continue
            if b.id not in a.conflict_with:
                a.conflict_with.append(b.id)
            if a.id not in b.conflict_with:
                b.conflict_with.append(a.id)
            for it in (a, b):
                if it not in conflicted:
                    conflicted.append(it)
    return conflicted


# ---------------------------------------------------------------------------
# État et résultat de contrôle
# ---------------------------------------------------------------------------

@dataclass
class NewsState:
    degraded: bool = True
    calendar_degraded: bool = True
    last_news_at: Optional[datetime] = None
    last_calendar_at: Optional[datetime] = None
    failures: int = 0
    items_count: int = 0
    events_count: int = 0
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["last_news_at"] = self.last_news_at.isoformat() if self.last_news_at else None
        d["last_calendar_at"] = self.last_calendar_at.isoformat() if self.last_calendar_at else None
        d["flag"] = "NEWS_DATA_DEGRADED" if self.degraded else "OK"
        return d


@dataclass
class NewsCheck:
    ok: bool
    state: str                 # OK | BLOCKED_PRE_NEWS | BLOCKED_POST_NEWS | DEGRADED | SHOCK
    reason: str = ""
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _round_minute(dt: datetime) -> datetime:
    return _aware(dt).replace(second=0, microsecond=0)


def _dedup_key(source: str, title: str, ts: datetime) -> str:
    raw = f"{source.strip().lower()}|{' '.join(title.split()).lower()}|{_round_minute(ts).isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class NewsHub:
    """Agrège plusieurs providers, dédoublonne, met en cache et applique les règles de blocage."""

    def __init__(self, providers: list[NewsProvider], cfg: dict, cache_dir: Path | None = None) -> None:
        self.providers = list(providers)
        self.cfg = {**DEFAULT_CFG, **(cfg or {})}
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._news: dict[str, NewsItem] = {}
        self._events: dict[str, CalendarEvent] = {}
        self._news_failures = 0
        self._calendar_failures = 0
        self.state = NewsState()
        if self.cache_dir:
            self._load_cache()
        self._update_state()

    # ----- configuration -----
    @property
    def max_age(self) -> timedelta:
        return timedelta(minutes=float(self.cfg["max_age_minutes"]))

    @property
    def block_before(self) -> timedelta:
        return timedelta(minutes=float(self.cfg["block_minutes_before_high_impact"]))

    @property
    def block_after(self) -> timedelta:
        return timedelta(minutes=float(self.cfg["block_minutes_after_high_impact"]))

    @property
    def degraded_after(self) -> int:
        return int(self.cfg["degraded_after_failures"])

    # ----- providers -----
    def _providers(self, kind: str) -> list[NewsProvider]:
        return [p for p in self.providers if p.kind == kind]

    def _available(self, kind: str) -> list[NewsProvider]:
        return [p for p in self._providers(kind) if p.available()]

    # ----- état -----
    def _update_state(self) -> None:
        any_available = any(p.available() for p in self.providers)
        news_deg = (not self._available("news")) or self._news_failures >= self.degraded_after
        cal_deg = (not self._available("economic_calendar")) or self._calendar_failures >= self.degraded_after
        self.state.calendar_degraded = cal_deg
        # NEWS_DATA_DEGRADED : aucun provider, ou trop d'échecs consécutifs sur l'un des flux
        self.state.degraded = (
            not any_available
            or self._news_failures >= self.degraded_after
            or self._calendar_failures >= self.degraded_after
            or (news_deg and cal_deg)
        )
        self.state.failures = max(self._news_failures, self._calendar_failures)
        self.state.items_count = len(self._news)
        self.state.events_count = len(self._events)
        self.state.sources = sorted({p.name for p in self.providers if p.available()})

    def is_realtime(self, now: Optional[datetime] = None) -> bool:
        """Vrai si le dernier rafraîchissement news réussi date de moins de ``max_age_minutes``."""
        now = _aware(now or utcnow())
        return self.state.last_news_at is not None and (now - self.state.last_news_at) <= self.max_age

    # ----- rafraîchissement -----
    def refresh_news(self, now: Optional[datetime] = None) -> int:
        """Interroge les providers de news. Retourne le nombre d'items nouveaux."""
        now = _aware(now or utcnow())
        avail = self._available("news")
        added = 0
        ok_any = False
        failed_any = False
        for p in avail:
            try:
                items = p.fetch_news(now)
                ok_any = True
            except ProviderError as e:
                failed_any = True
                log.warning("news provider %s en échec: %s", p.name, e)
                continue
            for it in items:
                if self._add_news(it):
                    added += 1
        if avail:
            if ok_any and not failed_any:
                self._news_failures = 0
            elif ok_any:
                # succès partiel : on ne compte pas un échec complet mais on ne remet pas à zéro
                pass
            else:
                self._news_failures += 1
        if ok_any:
            self.state.last_news_at = now
        self._prune(now)
        self._update_state()
        if self.cache_dir:
            self._save_cache(now)
        return added

    def refresh_calendar(self, now: Optional[datetime] = None) -> int:
        """Interroge les providers de calendrier. Retourne le nombre d'événements nouveaux."""
        now = _aware(now or utcnow())
        avail = self._available("economic_calendar")
        added = 0
        ok_any = False
        failed_any = False
        for p in avail:
            try:
                events = p.fetch_calendar(now, days_ahead=int(self.cfg["calendar_days_ahead"]))
                ok_any = True
            except ProviderError as e:
                failed_any = True
                log.warning("calendar provider %s en échec: %s", p.name, e)
                continue
            for ev in events:
                if self._add_event(ev):
                    added += 1
        if avail:
            if ok_any and not failed_any:
                self._calendar_failures = 0
            elif not ok_any:
                self._calendar_failures += 1
        if ok_any:
            self.state.last_calendar_at = now
        self._prune(now)
        self._update_state()
        if self.cache_dir:
            self._save_cache(now)
        return added

    def refresh(self, now: Optional[datetime] = None) -> NewsState:
        self.refresh_news(now)
        self.refresh_calendar(now)
        return self.state

    # ----- insertion / dédoublonnage -----
    def _add_news(self, item: NewsItem) -> bool:
        item.timestamp = _aware(item.timestamp)
        key = _dedup_key(item.source, item.title, item.timestamp)
        if key in self._news:
            return False
        if item.direction == "UNKNOWN" and item.confidence == 0.0:
            annotate_direction(item)
        self._news[key] = item
        return True

    def _add_event(self, ev: CalendarEvent) -> bool:
        ev.timestamp = _aware(ev.timestamp)
        key = _dedup_key(f"{ev.source}|{ev.currency}", ev.title, ev.timestamp)
        existing = self._events.get(key)
        if existing is not None:
            # Même événement : on met à jour actual/forecast/previous si désormais publiés (faits)
            if ev.actual is not None:
                existing.actual = ev.actual
            if ev.forecast is not None:
                existing.forecast = ev.forecast
            if ev.previous is not None:
                existing.previous = ev.previous
            return False
        self._events[key] = ev
        return True

    def add_news(self, items: Iterable[NewsItem]) -> int:
        """Insertion directe (tests, replays). Retourne le nombre d'items nouveaux."""
        n = sum(1 for it in items if self._add_news(it))
        self._update_state()
        return n

    def add_events(self, events: Iterable[CalendarEvent]) -> int:
        n = sum(1 for ev in events if self._add_event(ev))
        self._update_state()
        return n

    def _prune(self, now: datetime) -> None:
        news_limit = now - timedelta(hours=float(self.cfg["news_retention_hours"]))
        cal_limit = now - timedelta(hours=float(self.cfg["calendar_retention_hours"]))
        self._news = {k: v for k, v in self._news.items() if v.timestamp >= news_limit}
        self._events = {k: v for k, v in self._events.items() if v.timestamp >= cal_limit}

    # ----- cache disque -----
    @property
    def cache_path(self) -> Optional[Path]:
        return (self.cache_dir / CACHE_FILENAME) if self.cache_dir else None

    def _save_cache(self, now: datetime) -> None:
        path = self.cache_path
        if path is None:
            return
        payload = {
            "saved_at": now.isoformat(),
            "last_news_at": self.state.last_news_at.isoformat() if self.state.last_news_at else None,
            "last_calendar_at": self.state.last_calendar_at.isoformat() if self.state.last_calendar_at else None,
            "news": [it.to_dict() for it in self._news.values()],
            "calendar": [ev.to_dict() for ev in self._events.values()],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.warning("cache news non sauvegardé: %s", e)

    def _load_cache(self) -> None:
        path = self.cache_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("cache news illisible, ignoré: %s", e)
            return
        for d in payload.get("news", []):
            try:
                it = NewsItem(
                    timestamp=_aware(datetime.fromisoformat(d["timestamp"])),
                    source=d["source"],
                    title=d["title"],
                    assets=list(d.get("assets", [])),
                    importance=d.get("importance", "LOW"),
                    direction=d.get("direction", "UNKNOWN"),
                    confidence=float(d.get("confidence", 0.0)),
                    surprise=d.get("surprise"),
                    impact_minutes=int(d.get("impact_minutes", 60)),
                    quality=d.get("quality", "cache"),
                    provenance=Provenance(d.get("provenance", "FACT")),
                    conflict_with=list(d.get("conflict_with", [])),
                    id=d.get("id") or NewsItem.__dataclass_fields__["id"].default_factory(),  # type: ignore[misc]
                )
            except (KeyError, ValueError, TypeError):
                continue
            self._news[_dedup_key(it.source, it.title, it.timestamp)] = it
        for d in payload.get("calendar", []):
            try:
                ev = CalendarEvent(
                    timestamp=_aware(datetime.fromisoformat(d["timestamp"])),
                    source=d["source"],
                    title=d["title"],
                    currency=d.get("currency", ""),
                    importance=d.get("importance", "LOW"),
                    actual=d.get("actual"),
                    forecast=d.get("forecast"),
                    previous=d.get("previous"),
                    provenance=Provenance(d.get("provenance", "FACT")),
                    id=d.get("id") or CalendarEvent.__dataclass_fields__["id"].default_factory(),  # type: ignore[misc]
                )
            except (KeyError, ValueError, TypeError):
                continue
            self._events[_dedup_key(f"{ev.source}|{ev.currency}", ev.title, ev.timestamp)] = ev
        # Horodatages du cache : conservés tels quels. Un cache plus vieux que
        # max_age_minutes n'est jamais « temps réel » (voir is_realtime / recent_news).
        for attr in ("last_news_at", "last_calendar_at"):
            v = payload.get(attr)
            if v:
                try:
                    setattr(self.state, attr, _aware(datetime.fromisoformat(v)))
                except ValueError:
                    pass

    # ----- requêtes -----
    @staticmethod
    def _norm(values: Optional[Iterable[str]]) -> set[str]:
        return {str(v).strip().upper() for v in (values or []) if str(v).strip()}

    def recent_news(
        self,
        now: Optional[datetime] = None,
        assets: Optional[list[str]] = None,
        max_age_minutes: Optional[int] = None,
    ) -> list[NewsItem]:
        """News publiées dans les ``max_age_minutes`` dernières minutes (jamais plus vieux que le seuil config).

        Une news plus vieille que ``max_age_minutes`` (config) n'est jamais
        retournée : elle n'est pas « temps réel ». Filtre optionnel par actifs.
        """
        now = _aware(now or utcnow())
        max_age = self.max_age if max_age_minutes is None else min(self.max_age, timedelta(minutes=max_age_minutes))
        wanted = self._norm(assets)
        out = []
        for it in self._news.values():
            age = now - it.timestamp
            if age < timedelta(0) or age > max_age:
                continue
            if wanted and not (wanted & self._norm(it.assets)):
                continue
            out.append(it)
        out.sort(key=lambda x: (x.timestamp, x.id), reverse=True)
        return out

    def _events_between(self, start: datetime, end: datetime, currencies: Iterable[str], min_importance: str) -> list[CalendarEvent]:
        wanted = self._norm(currencies)
        rank = IMPORTANCE_RANK.get(str(min_importance).upper(), 2)
        out = [
            ev for ev in self._events.values()
            if start <= ev.timestamp <= end
            and IMPORTANCE_RANK.get(ev.importance.upper(), 0) >= rank
            and (not wanted or ev.currency.upper() in wanted)
        ]
        out.sort(key=lambda x: (x.timestamp, x.id))
        return out

    def upcoming_events(
        self,
        now: Optional[datetime] = None,
        currencies: Optional[list[str]] = None,
        minutes_ahead: int = 120,
        min_importance: str = "HIGH",
    ) -> list[CalendarEvent]:
        """Événements à venir dans ``minutes_ahead`` minutes pour ces devises (importance >= seuil)."""
        now = _aware(now or utcnow())
        return self._events_between(now, now + timedelta(minutes=minutes_ahead), currencies or [], min_importance)

    def recent_events(
        self,
        now: Optional[datetime] = None,
        currencies: Optional[list[str]] = None,
        minutes_back: int = 60,
        min_importance: str = "HIGH",
    ) -> list[CalendarEvent]:
        """Événements passés dans les ``minutes_back`` dernières minutes (importance >= seuil)."""
        now = _aware(now or utcnow())
        return self._events_between(now - timedelta(minutes=minutes_back), now, currencies or [], min_importance)

    # ----- règles -----
    def news_shock(self, symbol_currencies: list[str], now: Optional[datetime] = None) -> bool:
        """Choc de news : événement HIGH publié récemment avec surprise non nulle, ou news HIGH très récente."""
        now = _aware(now or utcnow())
        for ev in self.recent_events(now, symbol_currencies, int(self.cfg["shock_event_minutes"]), "HIGH"):
            s = ev.surprise
            if s is not None and s != 0:
                return True
        for it in self.recent_news(now, symbol_currencies, int(self.cfg["shock_news_minutes"])):
            if it.importance.upper() == "HIGH":
                return True
        return False

    def check(
        self,
        symbol_currencies: list[str],
        now: Optional[datetime] = None,
        news_sensitive_strategy: bool = False,
    ) -> NewsCheck:
        """Contrôle pré-trade.

        - DEGRADED : hub dégradé (aucun provider / échecs consécutifs / calendrier
          indisponible). ``ok = not news_sensitive_strategy`` : les stratégies
          news-sensibles sont bloquées, les autres passent avec l'état marqué.
        - BLOCKED_PRE_NEWS / BLOCKED_POST_NEWS : un événement HIGH sur l'une des
          devises tombe dans [now - block_after, now + block_before].
        - SHOCK : ``news_shock`` vrai (surprise récente ou news HIGH très récente).
        - OK sinon.
        """
        now = _aware(now or utcnow())
        self._update_state()
        if self.state.degraded or self.state.calendar_degraded:
            return NewsCheck(
                ok=not news_sensitive_strategy,
                state="DEGRADED",
                reason="NEWS_DATA_DEGRADED: aucune source news/calendrier fiable disponible",
                events=[],
            )
        start = now - self.block_after
        end = now + self.block_before
        hits = self._events_between(start, end, symbol_currencies, "HIGH")
        if hits:
            upcoming = [ev for ev in hits if ev.timestamp > now]
            past = [ev for ev in hits if ev.timestamp <= now]
            if upcoming:
                ev = upcoming[0]
                mins = int((ev.timestamp - now).total_seconds() // 60)
                return NewsCheck(
                    ok=False,
                    state="BLOCKED_PRE_NEWS",
                    reason=f"{ev.currency} {ev.title} dans {mins} min (fenêtre {self.cfg['block_minutes_before_high_impact']} min)",
                    events=[e.to_dict() for e in hits],
                )
            ev = past[-1]
            mins = int((now - ev.timestamp).total_seconds() // 60)
            return NewsCheck(
                ok=False,
                state="BLOCKED_POST_NEWS",
                reason=f"{ev.currency} {ev.title} il y a {mins} min (fenêtre {self.cfg['block_minutes_after_high_impact']} min)",
                events=[e.to_dict() for e in hits],
            )
        if self.news_shock(symbol_currencies, now):
            shock_events = self.recent_events(now, symbol_currencies, int(self.cfg["shock_event_minutes"]), "HIGH")
            shock_news = [it for it in self.recent_news(now, symbol_currencies, int(self.cfg["shock_news_minutes"])) if it.importance == "HIGH"]
            return NewsCheck(
                ok=False,
                state="SHOCK",
                reason="Choc de news récent (surprise ou news HIGH)",
                events=[e.to_dict() for e in shock_events] + [n.to_dict() for n in shock_news],
            )
        return NewsCheck(ok=True, state="OK", reason="", events=[])

    # ----- divers -----
    def conflicts(self, now: Optional[datetime] = None, assets: Optional[list[str]] = None) -> list[NewsItem]:
        """Détecte les conflits directionnels parmi les news récentes."""
        return detect_conflicts(self.recent_news(now, assets), int(self.cfg["conflict_window_minutes"]))

    def snapshot(self, now: Optional[datetime] = None) -> dict:
        now = _aware(now or utcnow())
        self._update_state()
        return {
            "state": self.state.to_dict(),
            "realtime": self.is_realtime(now),
            "recent_news": [it.to_dict() for it in self.recent_news(now)],
            "upcoming_high": [ev.to_dict() for ev in self.upcoming_events(now, None, 240, "HIGH")],
        }


__all__ = [
    "IMPORTANCE_RANK",
    "NewsCheck",
    "NewsHub",
    "NewsState",
    "annotate_direction",
    "assets_from_text",
    "classify_importance",
    "detect_conflicts",
    "direction_hint",
]
