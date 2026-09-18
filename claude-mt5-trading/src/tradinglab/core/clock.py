"""Horloge, sessions de marché et détection de clôture de barre."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .types import Session, utcnow

TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}


# Décalage heure serveur broker − UTC (secondes). Les barres H4/D1/W1 de MT5 sont alignées sur minuit
# **serveur** (MetaQuotes-Demo : UTC+2/UTC+3), pas sur minuit UTC. Réglé par `set_server_utc_offset()`
# (calibration `MT5Adapter.connect()` ou clé `system.server_utc_offset_hours`). 0 = alignement UTC (mock).
SERVER_UTC_OFFSET_SEC: int = 0

# L'epoch 0 (1970-01-01) est un jeudi ; les barres W1 de MT5 ouvrent le dimanche 00:00 serveur.
# Décalage à retrancher pour ancrer le modulo hebdomadaire sur un dimanche : jeudi → dimanche = 3 jours.
_W1_ANCHOR_SHIFT_SEC = 3 * 86400


def set_server_utc_offset(seconds: float | int | None) -> None:
    """Fixe le décalage serveur−UTC utilisé par `bar_open_time()` (None → 0)."""
    global SERVER_UTC_OFFSET_SEC
    SERVER_UTC_OFFSET_SEC = int(round(seconds or 0))


def tf_seconds(tf: str) -> int:
    return TF_SECONDS[tf.upper()]


def bar_open_time(ts: datetime, tf: str, server_offset_sec: int | None = None) -> datetime:
    """Début (UTC) de la barre contenant ts, alignée comme le fait le serveur MT5.

    - M1…H1 : alignement identique quel que soit le décalage (diviseurs de l'heure).
    - H4/D1 : alignement sur minuit **serveur** (`server_offset_sec`, défaut `SERVER_UTC_OFFSET_SEC`).
    - W1 : ancrage sur le dimanche 00:00 serveur (l'epoch 0 est un jeudi).
    """
    s = tf_seconds(tf)
    off = SERVER_UTC_OFFSET_SEC if server_offset_sec is None else int(server_offset_sec)
    e = int(ts.timestamp()) + off
    if tf.upper() == "W1":
        e -= _W1_ANCHOR_SHIFT_SEC
        return datetime.fromtimestamp(e - e % s + _W1_ANCHOR_SHIFT_SEC - off, tz=timezone.utc)
    return datetime.fromtimestamp(e - e % s - off, tz=timezone.utc)


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
