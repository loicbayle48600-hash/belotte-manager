"""Journée de trading « prop » : le compteur de perte quotidienne ne suit pas le calendrier UTC.

FOXX Funded réinitialise la journée à **17:00 America/New_York** (relevé du 2026-09-19, voir
`docs/prop/foxx_funded_regles.md`). Une position ouverte à 22:00 UTC un mardi appartient donc à la
journée prop du mercredi : compter en dates UTC ferait démarrer le plancher journalier au mauvais
moment, et une perte de fin de session serait imputée au mauvais jour.

Le fuseau est résolu par `zoneinfo` (base système sous Linux, paquet `tzdata` sous Windows). Si la
base de fuseaux est absente, on ne devine pas : `TradingDayCalendar.degraded` passe à vrai et le
repli explicite est l'heure d'hiver (UTC−5), le choix le plus prudent puisqu'il fait basculer la
journée une heure plus tôt en été.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

try:  # pragma: no cover - dépend de la base de fuseaux de la machine
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


FALLBACK_OFFSET = timedelta(hours=-5)  # EST, repli si la base de fuseaux est indisponible


@dataclass
class TradingDayCalendar:
    """Convertit un instant absolu en journée de trading prop."""

    reset_hour: int = 17
    reset_minute: int = 0
    tz_name: str = "America/New_York"
    degraded: bool = False

    def __post_init__(self) -> None:
        self.reset_hour = max(0, min(23, int(self.reset_hour)))
        self.reset_minute = max(0, min(59, int(self.reset_minute)))
        self._tz = self._resolve_tz(self.tz_name)

    def _resolve_tz(self, name: str):
        if ZoneInfo is not None and name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                pass
        self.degraded = True
        return timezone(FALLBACK_OFFSET, "EST(repli)")

    @classmethod
    def from_config(cls, prop_cfg: dict | None) -> "TradingDayCalendar":
        cfg = prop_cfg or {}
        return cls(reset_hour=int(cfg.get("trading_day_reset_hour", 17) or 0),
                   reset_minute=int(cfg.get("trading_day_reset_minute", 0) or 0),
                   tz_name=str(cfg.get("trading_day_timezone") or "America/New_York"))

    def local(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(self._tz)

    def day(self, now: datetime) -> date:
        """Journée prop contenant `now` : à partir du reset, on est déjà dans la journée suivante."""
        loc = self.local(now)
        if (loc.hour, loc.minute) >= (self.reset_hour, self.reset_minute):
            return loc.date() + timedelta(days=1)
        return loc.date()

    def day_key(self, now: datetime) -> str:
        return self.day(now).isoformat()

    def reset_at(self, now: datetime) -> datetime:
        """Instant UTC du reset qui a ouvert la journée prop contenant `now`."""
        opened_on = self.day(now) - timedelta(days=1)
        local_reset = datetime.combine(opened_on, time(self.reset_hour, self.reset_minute), tzinfo=self._tz)
        return local_reset.astimezone(timezone.utc)

    def next_reset(self, now: datetime) -> datetime:
        return self.reset_at(now) + timedelta(days=1)


DEFAULT_CALENDAR = TradingDayCalendar()


def prop_trading_day(now: datetime, calendar: Optional[TradingDayCalendar] = None) -> date:
    return (calendar or DEFAULT_CALENDAR).day(now)
