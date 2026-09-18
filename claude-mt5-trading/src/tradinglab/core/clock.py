"""Horloge, sessions de marché et détection de clôture de barre."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .types import Session, utcnow

TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}


def tf_seconds(tf: str) -> int:
    return TF_SECONDS[tf.upper()]


def bar_open_time(ts: datetime, tf: str) -> datetime:
    """Début de la barre contenant ts (UTC)."""
    s = tf_seconds(tf)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % s, tz=timezone.utc)


def is_new_bar(tf: str, last_seen: datetime | None, now: datetime | None = None) -> tuple[bool, datetime]:
    now = now or utcnow()
    cur = bar_open_time(now, tf)
    if last_seen is None or cur > last_seen:
        return True, cur
    return False, cur


def current_session(now: datetime | None = None) -> Session:
    now = now or utcnow()
    h = now.hour + now.minute / 60.0
    if now.weekday() >= 5:
        return Session.OFF
    in_ldn = 7 <= h < 16
    in_ny = 12 <= h < 21
    if in_ldn and in_ny:
        return Session.OVERLAP_LDN_NY
    if in_ldn:
        return Session.LONDON
    if in_ny:
        return Session.NEWYORK
    if 0 <= h < 8:
        return Session.ASIA
    return Session.OFF


def forex_market_open(now: datetime | None = None) -> bool:
    """Marché forex ouvert : du dimanche 22:00 UTC au vendredi 21:00 UTC (approximation, à recouper avec MT5)."""
    now = now or utcnow()
    wd = now.weekday()
    if wd == 5:
        return False
    if wd == 6:
        return now.hour >= 22
    if wd == 4:
        return now.hour < 21
    return True


def seconds_until_next_bar(tf: str, now: datetime | None = None) -> float:
    now = now or utcnow()
    s = tf_seconds(tf)
    nxt = bar_open_time(now, tf) + timedelta(seconds=s)
    return (nxt - now).total_seconds()
