"""Positionnement COT (Commitments of Traders, CFTC) — filtre de CONTEXTE déterministe.

Source : l'API publique Socrata de la CFTC (publicreporting.cftc.gov, rapport « Legacy —
Futures Only », hebdomadaire, sans clé). Décision explicite de l'utilisateur du 2026-09-21
(« tout ce qui peut être utile ») : le positionnement spéculatif net n'est PAS un signal
d'entrée, c'est un avertissement de contexte — acheter quand les spéculateurs sont déjà à un
extrême haussier historique, c'est acheter tard.

Règles strictes du dépôt :
- aucune donnée inventée : marché non couvert, cache vide, flux en panne → ``None`` et
  l'annotation n'ajoute RIEN au candidat (jamais un neutre fabriqué) ;
- l'annotation n'ajoute qu'un ``argument_against`` (protection pure) : l'extrême opposé
  n'est jamais transformé en argument POUR (un extrême peut durer des mois) ;
- le rafraîchissement est borné (une requête HTTP toutes les ``refresh_hours`` au plus,
  timeout court) et ses échecs sont journalisés puis absorbés : le cache disque précédent
  reste la seule vérité, avec sa date.

L'index COT est le classique min-max sur ``lookback_weeks`` semaines : 0 = net spéculatif le
plus vendeur de la fenêtre, 100 = le plus acheteur. Un index >= ``extreme_high`` (ou <=
``extreme_low``) marque un extrême de positionnement.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

SOCRATA_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

#: racine symbole -> (code contrat CFTC, sens du future vu du symbole, libellé)
#: sens +1 : être acheteur du symbole = être acheteur du future (EURUSD BUY ~ long Euro FX).
#: sens -1 : le future cote la devise de CONTREPARTIE (USDJPY BUY ~ short Yen futures).
#: Les croisés (EURJPY…), le Brent (coté ICE, absent de ce flux) et les indices non américains
#: ne sont volontairement PAS couverts : à défaut de mapping propre, pas d'annotation du tout.
MARKETS: dict[str, tuple[str, int, str]] = {
    "EURUSD": ("099741", +1, "Euro FX"),
    "GBPUSD": ("096742", +1, "British Pound"),
    "AUDUSD": ("232741", +1, "Australian Dollar"),
    "NZDUSD": ("112741", +1, "New Zealand Dollar"),
    "USDJPY": ("097741", -1, "Japanese Yen"),
    "USDCHF": ("092741", -1, "Swiss Franc"),
    "USDCAD": ("090741", -1, "Canadian Dollar"),
    "XAUUSD": ("088691", +1, "Gold"),
    "XAGUSD": ("084691", +1, "Silver"),
    "XTIUSD": ("067651", +1, "WTI Crude Oil"),
    "USOIL": ("067651", +1, "WTI Crude Oil"),
}

DEFAULTS = {
    "lookback_weeks": 156,     # ~3 ans de rapports hebdomadaires
    "extreme_high": 90.0,
    "extreme_low": 10.0,
    "refresh_hours": 6.0,      # le rapport est hebdomadaire : inutile d'interroger plus souvent
    "max_report_age_days": 21, # au-delà, le rapport est trop vieux pour commenter quoi que ce soit
    "timeout_sec": 10.0,
    "min_weeks": 52,           # sous un an d'historique, aucun index n'est calculé
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class COTClient:
    """Cache disque + index COT par marché. ``http_get`` est injectable pour les tests."""

    def __init__(self, cache_path: Path, journal=None, http_get=None, **overrides):
        self.cache_path = Path(cache_path)
        self.journal = journal
        self.cfg = {**DEFAULTS, **overrides}
        self._http_get = http_get or self._default_http_get
        self._last_attempt: Optional[datetime] = None
        self.data: dict[str, list[dict]] = {}      # code contrat -> [{date, net}] trié par date croissante
        self.fetched_at: Optional[str] = None
        self._load_cache()

    # ---------- cache disque ----------
    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.data = {str(k): list(v) for k, v in raw.get("data", {}).items()}
            self.fetched_at = raw.get("fetched_at")
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            self.data, self.fetched_at = {}, None

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"fetched_at": self.fetched_at, "data": self.data}),
                                       encoding="utf-8")
        except OSError as e:
            self._warn("cache COT non écrit", error=f"{type(e).__name__}: {e}")

    def _warn(self, msg: str, **kw) -> None:
        if self.journal is not None:
            self.journal.warn(msg, **kw)

    # ---------- rafraîchissement ----------
    def _default_http_get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "tradinglab-cot/1.0"})
        with urllib.request.urlopen(req, timeout=float(self.cfg["timeout_sec"])) as resp:  # noqa: S310 - URL fixe CFTC
            return resp.read()

    def maybe_refresh(self, now: Optional[datetime] = None) -> bool:
        """Une tentative réseau au plus toutes les ``refresh_hours``. Échec → cache précédent conservé."""
        now = now or _utcnow()
        if self._last_attempt is not None and (now - self._last_attempt) < timedelta(hours=float(self.cfg["refresh_hours"])):
            return False
        self._last_attempt = now
        codes = sorted({code for code, _, _ in MARKETS.values()})
        limit = (int(self.cfg["lookback_weeks"]) + 8) * len(codes)
        params = {
            "$select": "cftc_contract_market_code,report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
            "$where": "cftc_contract_market_code in(" + ",".join(f"'{c}'" for c in codes) + ")",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": str(limit),
        }
        url = SOCRATA_URL + "?" + urllib.parse.urlencode(params)
        try:
            rows = json.loads(self._http_get(url).decode("utf-8"))
            if not isinstance(rows, list):
                raise ValueError("réponse Socrata non-liste")
        except Exception as e:  # noqa: BLE001 - le flux public peut tomber : cache conservé, jamais de donnée inventée
            self._warn("rafraîchissement COT échoué (cache conservé)", error=f"{type(e).__name__}: {str(e)[:120]}")
            return False
        parsed: dict[str, dict[str, float]] = {}
        for r in rows:
            try:
                code = str(r["cftc_contract_market_code"])
                date = str(r["report_date_as_yyyy_mm_dd"])[:10]
                net = float(r["noncomm_positions_long_all"]) - float(r["noncomm_positions_short_all"])
            except (KeyError, TypeError, ValueError):
                continue
            parsed.setdefault(code, {})[date] = net       # une seule valeur par date de rapport
        if not parsed:
            self._warn("rafraîchissement COT : aucune ligne exploitable (cache conservé)")
            return False
        keep = int(self.cfg["lookback_weeks"])
        self.data = {code: [{"date": d, "net": n} for d, n in sorted(vals.items())][-keep:]
                     for code, vals in parsed.items()}
        self.fetched_at = now.isoformat()
        self._save_cache()
        return True

    # ---------- lecture ----------
    def context(self, symbol_root: str, now: Optional[datetime] = None) -> Optional[dict]:
        """Index COT du marché sous-jacent au symbole, ou ``None`` (non couvert / données insuffisantes)."""
        m = MARKETS.get(str(symbol_root).upper().split(".")[0].split("_")[0])   # racine sans suffixe broker
        if m is None:
            return None
        code, direction, label = m
        series = self.data.get(code) or []
        if len(series) < int(self.cfg["min_weeks"]):
            return None
        last = series[-1]
        try:
            as_of = datetime.strptime(last["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return None
        if ((now or _utcnow()) - as_of) > timedelta(days=float(self.cfg["max_report_age_days"])):
            return None
        nets = [float(s["net"]) for s in series]
        lo, hi = min(nets), max(nets)
        if hi <= lo:
            return None
        index = (nets[-1] - lo) / (hi - lo) * 100.0
        return {"market": label, "as_of": last["date"], "net": nets[-1], "cot_index": round(index, 1),
                "direction": direction, "weeks": len(nets)}

    def annotate(self, candidate) -> None:
        """Ajoute UN argument contre si le trade suit un positionnement spéculatif déjà extrême. Sinon : rien."""
        ctx = self.context(candidate.symbol, None)
        if ctx is None:
            return
        # sens du candidat exprimé dans le sens du FUTURE : +1 = même sens que la foule acheteuse du future
        future_side = candidate.side.sign * ctx["direction"]
        idx = ctx["cot_index"]
        if future_side > 0 and idx >= float(self.cfg["extreme_high"]):
            candidate.arguments_against.append(
                f"COT: spéculateurs déjà à l'extrême acheteur sur {ctx['market']} "
                f"(index {idx:.0f}/100 sur {ctx['weeks']} sem., rapport du {ctx['as_of']}) : entrée tardive avec la foule")
        elif future_side < 0 and idx <= float(self.cfg["extreme_low"]):
            candidate.arguments_against.append(
                f"COT: spéculateurs déjà à l'extrême vendeur sur {ctx['market']} "
                f"(index {idx:.0f}/100 sur {ctx['weeks']} sem., rapport du {ctx['as_of']}) : entrée tardive avec la foule")
