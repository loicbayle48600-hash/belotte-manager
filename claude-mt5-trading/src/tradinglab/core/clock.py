"""Horloge, sessions de marché et détection de clôture de barre."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .types import Session, utcnow

TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H2": 7200, "H4": 14400, "D1": 86400, "W1": 604800,
    # 2026-09-30 : MN1 = durée NOMINALE (30 jours) pour les calculs approchés (expirations) ; l'ouverture et la clôture
    # d'une barre mensuelle suivent le calendrier (`bar_open_time`, `seconds_until_next_bar`)
    "MN1": 2592000,
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
    if tf.upper() == "MN1":
        # 1er du mois 00:00 heure SERVEUR (comme MT5), exprimé en UTC
        loc = datetime.fromtimestamp(e, tz=timezone.utc)
        return datetime(loc.year, loc.month, 1, tzinfo=timezone.utc) - timedelta(seconds=off)
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


def current_session(now: datetime | None = None, round_the_clock: bool = False) -> Session:
    """Session de marché courante.

    `round_the_clock=False` (défaut) : hypothèse forex. `OFF` le samedi et le dimanche, et
    `OFF` entre 21:00 et 00:00 UTC. C'est le comportement de tout instrument qui ferme.

    `round_the_clock=True` : marché réellement ouvert en continu (crypto). Deux règles sautent,
    car `OFF` n'est listé dans les sessions d'aucun agent — `active_for()` y renvoie donc
    toujours 0 agent, ce qui rend l'instrument intradable malgré des cotations vivantes :

    - la règle de **jour** : plus de `OFF` le week-end (constaté 2026-09-20) ;
    - le creux de **21:00-00:00 UTC**, rattaché à `ASIA` : la session asiatique s'ouvre
      effectivement vers 21:00-22:00 UTC (Sydney), le creux n'existe que pour le forex.

    Les plages ASIA / LONDON / NEWYORK / OVERLAP restent identiques dans les deux cas.
    """
    now = now or utcnow()
    h = now.hour + now.minute / 60.0
    if now.weekday() >= 5 and not round_the_clock:
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
    return Session.ASIA if round_the_clock else Session.OFF


#: horaires forex d'IC Markets en HEURE SERVEUR : ouverture lundi 00:05, fermeture vendredi 23:55
FOREX_OPEN_MON = (0, 5)
FOREX_CLOSE_FRI = (23, 55)


def _server_time(now: datetime) -> datetime:
    """Heure serveur du broker : décalage mesuré par l'adaptateur MT5 si connu, sinon Europe/Athens (EET/EEST :
    IC Markets et MetaQuotes suivent l'heure d'Europe de l'Est, UTC+2 l'hiver, UTC+3 l'été)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if SERVER_UTC_OFFSET_SEC:
        return (now.astimezone(timezone.utc) + timedelta(seconds=SERVER_UTC_OFFSET_SEC)).replace(tzinfo=None)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("Europe/Athens")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - base de fuseaux absente : repli UTC+2
        return (now.astimezone(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None)


def forex_market_open(now: datetime | None = None) -> bool:
    """Marché forex ouvert, en HEURE SERVEUR du broker : du lundi 00:05 au vendredi 23:55.

    2026-09-27 : l'ancienne règle « dimanche 22:00 → vendredi 21:00 UTC » ratait l'ouverture réelle d'une heure en
    été (IC Markets rouvre le dimanche à 21:00 UTC : les stops des positions du vendredi ont sauté à 21:01 UTC)."""
    srv = _server_time(now or utcnow())
    wd = srv.weekday()
    if wd in (5, 6):
        return False
    hm = (srv.hour, srv.minute)
    if wd == 0 and hm < FOREX_OPEN_MON:
        return False
    if wd == 4 and hm >= FOREX_CLOSE_FRI:
        return False
    return True


def seconds_until_next_bar(tf: str, now: datetime | None = None) -> float:
    now = now or utcnow()
    if tf.upper() == "MN1":
        return (next_month_open(bar_open_time(now, tf)) - now).total_seconds()
    s = tf_seconds(tf)
    nxt = bar_open_time(now, tf) + timedelta(seconds=s)
    return (nxt - now).total_seconds()


def next_month_open(open_utc: datetime, server_offset_sec: int | None = None) -> datetime:
    """Ouverture (UTC) de la barre mensuelle qui suit celle ouverte en `open_utc` (1er du mois suivant, heure serveur)."""
    off = SERVER_UTC_OFFSET_SEC if server_offset_sec is None else int(server_offset_sec)
    loc = open_utc + timedelta(seconds=off)
    y, m = (loc.year + 1, 1) if loc.month == 12 else (loc.year, loc.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc) - timedelta(seconds=off)
