"""Copieur de positions : réplique le maître sur UN compte suiveur.

Principes (mêmes règles de sécurité que le reste du dépôt) :
- la SOURCE est l'export atomique du maître (`state/master_positions.json`), jamais une lecture
  directe du terminal maître (une connexion MT5 par processus) ;
- chaque position copiée porte le magic du copieur et le ticket maître dans son commentaire
  (``TLABCOPY <ticket>``) : c'est la clé d'appariement, robuste aux redémarrages ;
- volume suiveur = volume maître × ``size_factor`` × (equity suiveur / equity maître), arrondi VERS
  LE BAS au pas de volume ; sous le volume minimal → on n'ouvre pas (jamais de sur-risque) ;
- le SL est TOUJOURS répliqué (une copie sans stop n'existe pas : l'ouverture porte le SL du maître) ;
  le TP du maître est répliqué tel quel ;
- export plus vieux que ``stale_after_sec`` → aucune OUVERTURE (les fermetures restent permises :
  en cas de doute on réduit l'exposition, on ne l'augmente jamais) ;
- toute action est journalisée ; une erreur sur une action n'empêche pas les suivantes.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..core.types import OrderKind, OrderRequest, Side

COPY_COMMENT_PREFIX = "TLABCOPY "
DEFAULT_MAGIC = 52000
DEFAULT_STALE_SEC = 120.0
# Temporisation des actions refusées par le broker suiveur (2026-09-23) : 60 s puis doublement à chaque
# nouvel échec, plafonné à 15 min. Mesuré sur 4 jours avec le délai fixe de 60 s : 1 858 tentatives
# d'ouverture refusées (« Trade disabled », « No money », « AutoTrading disabled ») et 170 modifications
# SL/TP refusées toutes les 5 s sur un même ticket — des refus permanents martelés sans information.
RETRY_BASE_SEC = 60.0
#: MT5 TRADE_RETCODE_NO_CHANGES : la modification demandée est déjà en place chez le broker
RETCODE_NO_CHANGES = 10025
RETRY_MAX_SEC = 900.0


@dataclass
class CopyAction:
    kind: str              # "open" | "close" | "reduce" | "modify" | "adopt"
    symbol: str = ""       # symbole tel qu'il s'appelle CHEZ LE SUIVEUR (c'est lui qu'on exécute)
    master_symbol: str = ""  # nom côté maître, pour le journal (US500 chez le maître = SPX500 chez le suiveur)
    master_price: float = 0.0  # prix courant du maître : sert à décaler SL/TP si le suiveur cote un autre contrat
    shift: float = 0.0         # décalage de prix appliqué à SL/TP (mémorisé à l'ouverture dans la table maître → suiveur)
    side: Optional[Side] = None
    volume: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    follower_ticket: int = 0
    master_ticket: int = 0
    reason: str = ""


def _master_ticket_of(comment: str) -> Optional[int]:
    c = str(comment or "")
    if not c.startswith(COPY_COMMENT_PREFIX):
        return None
    try:
        return int(c[len(COPY_COMMENT_PREFIX):].split()[0])
    except (ValueError, IndexError):
        return None


def _round_volume(vol: float, step: float, vmin: float, vmax: float) -> float:
    if step <= 0:
        return 0.0
    v = math.floor(vol / step + 1e-9) * step
    v = round(v, 8)
    if v < vmin:
        return 0.0
    return min(v, vmax if vmax > 0 else v)


def _contract_ratio(mp: dict, spec) -> Optional[float]:
    """Rapport (valeur d'un mouvement de prix d'1 lot chez le maître) / (idem chez le suiveur). 1.0 si le maître n'exporte
    pas encore cette valeur (ancien export) ; None si le rapport est aberrant (> ×20 ou < ×1/20 : instrument différent)."""
    m_vpp = mp.get("value_per_price")
    ts, tv = float(getattr(spec, "tick_size", 0.0) or 0.0), float(getattr(spec, "tick_value", 0.0) or 0.0)
    if not m_vpp or ts <= 0 or tv <= 0:
        return 1.0
    r = float(m_vpp) / (tv / ts)
    return r if 0.05 <= r <= 20.0 else None


def plan_sync(master: dict, follower_positions: list, follower_equity: float, size_factor: float,
              specs: dict, allow_open: bool = True, mapping: Optional[dict] = None,
              sym_map: Optional[dict] = None) -> list[CopyAction]:
    """Actions pour amener le compte suiveur à l'image du maître. Fonction PURE (testable sans broker).

    ``master`` : contenu de master_positions.json ; ``follower_positions`` : liste de ``Position`` du
    magic copieur ; ``specs`` : symbol -> SymbolSpec (volume_min/step/max) — symbole absent = pas copiable ;
    ``mapping`` : ticket maître -> ticket suiveur, TABLE PERSISTANTE (2026-09-22 : le commentaire d'ordre
    ne suffit pas — une fermeture partielle l'écrase chez certains brokers, ce qui a fait ouvrir 16 doublons
    sur un compte démo). Ordre d'appariement : table, puis commentaire, puis ADOPTION d'une position
    orpheline de même symbole/sens (jamais d'ouverture tant qu'une orpheline compatible existe).
    """
    actions: list[CopyAction] = []
    m_eq = float(master.get("equity") or 0.0)
    ratio = (follower_equity / m_eq * float(size_factor)) if m_eq > 0 else 0.0
    m_by_ticket = {int(p["ticket"]): p for p in master.get("positions", [])}
    entries: dict[int, dict] = {}
    for k, v in (mapping or {}).items():
        entries[int(k)] = dict(v) if isinstance(v, dict) else {"ticket": int(v)}
    f_by_ticket = {fp.ticket: fp for fp in follower_positions}
    inverse = {int(e["ticket"]): m for m, e in entries.items() if int(e.get("ticket", 0)) in f_by_ticket}
    f_by_master: dict[int, object] = {}
    orphans: list = []
    for fp in follower_positions:
        mt = inverse.get(fp.ticket)
        if mt is None:
            mt = _master_ticket_of(getattr(fp, "comment", ""))
        if mt is not None and mt not in f_by_master:
            f_by_master[mt] = fp
        else:
            orphans.append(fp)
    # adoption : un maître sans copie connue récupère une orpheline de même symbole et sens
    for mt, mp in m_by_ticket.items():
        if mt in f_by_master:
            continue
        cible_adopt = mp["symbol"] if sym_map is None else sym_map.get(mp["symbol"], mp["symbol"])
        for fp in orphans:
            if fp.symbol == cible_adopt and getattr(fp.side, "value", fp.side) == mp["side"]:
                f_by_master[mt] = fp
                orphans.remove(fp)
                actions.append(CopyAction("adopt", symbol=fp.symbol, follower_ticket=fp.ticket, master_ticket=mt,
                                          reason="position orpheline adoptée (commentaire perdu)"))
                break
    # orphelines restantes : elles ne correspondent à rien chez le maître → fermées comme les copies obsolètes
    for fp in orphans:
        actions.append(CopyAction("close", symbol=fp.symbol, follower_ticket=fp.ticket, master_ticket=0,
                                  reason="copie sans correspondance maître"))
    # 1. fermer ce que le maître n'a plus (toujours permis)
    for mt, fp in f_by_master.items():
        if mt not in m_by_ticket:
            actions.append(CopyAction("close", symbol=fp.symbol, follower_ticket=fp.ticket, master_ticket=mt,
                                      reason="position maître clôturée"))
    for mt, mp in m_by_ticket.items():
        # `sym_map` traduit le nom du maître vers celui du suiveur ; sans entrée, l'instrument n'existe
        # pas chez ce broker et la position n'est simplement pas répliquée (jamais de symbole approchant).
        # sym_map absent = pas de traduction demandée (mêmes noms des deux côtés) ; sym_map fourni mais
        # sans entrée = instrument introuvable chez ce broker, donc pas de copie.
        cible = mp["symbol"] if sym_map is None else sym_map.get(mp["symbol"])
        spec = specs.get(cible) if cible else None
        fp = f_by_master.get(mt)
        if fp is None:
            # 2. ouvrir ce qui manque (si autorisé, si le symbole existe, si le volume est viable)
            if not allow_open or spec is None or ratio <= 0:
                continue
            # même EXPOSITION que le maître (× ratio d'equity × facteur), pas le même nombre de lots : la valeur d'un
            # lot diffère d'un broker à l'autre (argent : 1 000 oz chez IC, 5 000 oz chez Admirals, 2026-09-24)
            contrat = _contract_ratio(mp, spec)
            if contrat is None:
                continue                                  # tailles de contrat incompatibles/inconnues : pas de copie
            vol = _round_volume(float(mp["volume"]) * ratio * contrat, spec.volume_step, spec.volume_min, spec.volume_max)
            if vol <= 0:
                continue
            actions.append(CopyAction("open", symbol=cible, master_symbol=mp["symbol"], side=Side(mp["side"]),
                                      master_price=float(mp.get("price_current") or 0.0),
                                      volume=vol, sl=float(mp["sl"] or 0.0), tp=float(mp["tp"] or 0.0),
                                      master_ticket=mt,
                                      reason=f"réplication ×{ratio:.4f}" + (f" · contrat ×{contrat:.3f}" if abs(contrat - 1.0) > 1e-9 else "")
                                             + (f" ({mp['symbol']}→{cible})" if cible != mp["symbol"] else "")))
            continue
        # 3. suivre les réductions partielles du maître (jamais d'augmentation après coup).
        #    PROPORTIONNEL aux volumes mémorisés à l'ouverture (2026-09-22) : recalculer la cible depuis
        #    l'equity courante faisait réduire de 0,01 lot à chaque frémissement d'equity du suiveur.
        if spec is not None:
            e = entries.get(mt) or {}
            mv0, fv0 = float(e.get("mv") or 0.0), float(e.get("fv") or 0.0)
            if mv0 > 0 and fv0 > 0:
                target = _round_volume(fv0 * float(mp["volume"]) / mv0, spec.volume_step, spec.volume_min, spec.volume_max)
            elif ratio > 0:
                target = _round_volume(float(mp["volume"]) * ratio, spec.volume_step, spec.volume_min, spec.volume_max)
                if fp.volume - target < 0.10 * fp.volume:      # sans repère : tolérance 10 % (le bruit d'equity)
                    target = fp.volume
            else:
                target = fp.volume
            excess = round(fp.volume - target, 8)
            if target > 0 and excess >= spec.volume_step - 1e-9:
                cut = _round_volume(excess, spec.volume_step, spec.volume_step, fp.volume)
                if cut > 0:
                    actions.append(CopyAction("reduce", symbol=fp.symbol, volume=cut, follower_ticket=fp.ticket,
                                              master_ticket=mt, reason="partiel maître suivi"))
        # 4. répliquer les stops/TP du maître quand ils changent
        m_sl, m_tp = float(mp["sl"] or 0.0), float(mp["tp"] or 0.0)
        # décalage de prix mémorisé pour cette copie (contrat coté autrement chez le suiveur, 2026-09-25) : les
        # niveaux du maître sont comparés APRÈS décalage, sinon l'écart ne se résorbe jamais et une modification
        # repart toutes les 5 s en dérivant avec le prix
        shift = float(entries.get(mt, {}).get("shift", 0.0) or 0.0)
        if shift:
            m_sl, m_tp = (m_sl + shift if m_sl else 0.0), (m_tp + shift if m_tp else 0.0)
        # Prix du maître ramenés à la précision du SUIVEUR (2026-09-24) : Blue Guardian cote DE40 avec moins de
        # décimales qu'IC Markets ; le SL maître 25 595,14 devenait 25 595,1 chez le suiveur, l'écart ne se
        # résorbait jamais et le copieur redemandait la même modification (« No changes », code 10025) sans fin.
        fspec = specs.get(fp.symbol)
        digits = getattr(fspec, "digits", None)
        if isinstance(digits, int) and digits >= 0:
            m_sl, m_tp = round(m_sl, digits), round(m_tp, digits)
        # tolérance d'un point : l'arrondi binaire (24867,35 → ,3 ou ,4) ne doit pas relancer une modification
        tol = float(getattr(fspec, "point", 0.0) or 0.0) * 1.0001 or 1e-9
        if (m_sl and abs(m_sl - fp.sl) > tol) or (m_tp and abs(m_tp - fp.tp) > tol):
            actions.append(CopyAction("modify", symbol=fp.symbol, sl=m_sl or fp.sl, tp=m_tp or fp.tp,
                                      follower_ticket=fp.ticket, master_ticket=mt, reason="SL/TP maître mis à jour",
                                      master_price=0.0 if shift else float(mp.get("price_current") or 0.0)))
    return actions


class CopyTrader:
    """Applique le plan sur le broker suiveur. Le broker est injecté (MockBroker dans les tests)."""

    def __init__(self, broker, master_file: Path, journal=None, size_factor: float = 1.0,
                 magic: int = DEFAULT_MAGIC, stale_after_sec: float = DEFAULT_STALE_SEC,
                 name: str = "", status_file: Path | None = None, factor_source=None):
        self.broker = broker
        self.master_file = Path(master_file)
        self.journal = journal
        self.size_factor = float(size_factor)
        self.magic = int(magic)
        self.stale_after_sec = float(stale_after_sec)
        self.name = name
        self.status_file = Path(status_file) if status_file else None
        self._recent: list[dict] = []
        self.factor_source = factor_source           # callable → facteur courant (config relue à chaque cycle)
        # 2026-09-27, décision utilisateur (« copier que les positions qui s'ouvrent en même temps ») : une position du
        # maître ouverte plus de MAX_OPEN_AGE_SEC avant le démarrage du suiveur n'est jamais copiée (elle serait
        # reprise à un prix sans rapport, parfois des jours plus tard — cas Moneta / positions du vendredi)
        self.started_at = datetime.now(timezone.utc)
        self.stats_since: Optional[str] = None       # début des statistiques du compte (remise à zéro, 2026-09-25)
        self._retry_after: dict[int, datetime] = {}  # ticket maître → pas de nouvelle tentative d'ouverture avant
        self._modify_after: dict[int, datetime] = {}  # ticket suiveur → pas de nouvelle modification SL/TP avant
        self._failures: dict[tuple, int] = {}         # (kind, ticket) → échecs consécutifs (0 après un succès)
        self._trade_disabled_seen: set[str] = set()   # symboles suiveurs non négociables déjà signalés
        self.map_file = (self.status_file.with_name(self.status_file.stem.replace("copy_status", "copy_map") + ".json")
                         if self.status_file else None)
        self.mapping: dict[int, int] = self._load_map()
        # tickets maîtres déjà copiés au moins une fois (persistés) : une copie fermée chez le suiveur — son propre
        # stop touché sur des prix légèrement différents — n'est JAMAIS rouverte (2026-09-24 : 9 réouvertures en un
        # jour, dont AUDJPY rouvert 12 min après son stop, à un moins bon prix)
        self.done_file = (self.status_file.with_name(self.status_file.stem.replace("copy_status", "copy_done") + ".json")
                          if self.status_file else None)
        self.done: set[int] = self._load_done()
        self._done_logged: set[int] = set()

    def _log(self, msg: str, **kw) -> None:
        if self.journal is not None:
            self.journal.event("copy_trade", message=msg, **kw)

    def _load_map(self) -> dict[int, dict]:
        """{ticket maître: {"ticket": ticket suiveur, "mv": volume maître à l'appariement, "fv": volume suiveur}}."""
        if self.map_file is None or not self.map_file.exists():
            return {}
        try:
            raw = json.loads(self.map_file.read_text(encoding="utf-8"))
            return {int(k): (dict(v) if isinstance(v, dict) else {"ticket": int(v)}) for k, v in raw.items()}
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            return {}

    def _load_done(self) -> set[int]:
        if self.done_file is None or not self.done_file.exists():
            return set()
        try:
            return {int(x) for x in json.loads(self.done_file.read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return set()

    def _save_done(self) -> None:
        if self.done_file is None:
            return
        try:
            import os as _os

            tmp = self.done_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self.done)), encoding="utf-8")
            _os.replace(tmp, self.done_file)
        except OSError:
            pass

    def _save_map(self) -> None:
        if self.map_file is None:
            return
        try:
            import os as _os

            tmp = self.map_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({str(k): v for k, v in self.mapping.items()}), encoding="utf-8")
            _os.replace(tmp, self.map_file)
        except OSError:
            pass

    def _symbol_map(self, master_symbols: list) -> dict:
        """Traduit les symboles du maître vers ceux du broker suiveur (cache : la liste des symboles du
        broker ne change pas en cours de session). Les absents sont signalés UNE fois."""
        from .symbol_map import build_map

        manquants_avant = set(getattr(self, "_sym_missing", set()))
        if not hasattr(self, "_sym_cache"):
            self._sym_cache: dict = {}
            self._sym_missing: set = set()
        a_resoudre = [s for s in master_symbols if s not in self._sym_cache and s not in self._sym_missing]
        if a_resoudre:
            dispo = list(self.broker.symbols() or [])
            trouve = build_map(a_resoudre, dispo)
            self._sym_cache.update(trouve)
            self._sym_missing.update(s for s in a_resoudre if s not in trouve)
        nouveaux = self._sym_missing - manquants_avant
        if nouveaux:
            self._log("instruments absents chez le suiveur : non répliqués",
                      symbols=sorted(nouveaux), follower=self.name)
        return {s: self._sym_cache[s] for s in master_symbols if s in self._sym_cache}

    def read_master(self) -> Optional[dict]:
        try:
            return json.loads(self.master_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def sync(self, now: Optional[datetime] = None) -> list[CopyAction]:
        master = self.read_master()
        if master is None:
            return []
        now = now or datetime.now(timezone.utc)
        try:
            ts = datetime.fromisoformat(str(master.get("ts_utc")))
            fresh = (now - ts) <= timedelta(seconds=self.stale_after_sec)
        except (TypeError, ValueError):
            fresh = False
        acc = self.broker.account_info()
        if acc is None:
            return []
        follower = self.broker.positions(magic=self.magic)
        sym_map = self._symbol_map([p["symbol"] for p in master.get("positions", [])])
        specs = {}
        held = {p.symbol for p in follower}
        for sym in set(sym_map.values()) | held:
            sp = self.broker.symbol_info(sym)
            if sp is None:
                continue
            if not getattr(sp, "trade_allowed", True) and sym not in held:
                # instrument présent mais non négociable chez ce broker (« Trade disabled ») : aucune
                # ouverture tentée, signalé une fois — les positions déjà ouvertes restent gérées
                if sym not in self._trade_disabled_seen:
                    self._trade_disabled_seen.add(sym)
                    self._log("instrument non négociable chez le suiveur : non répliqué", symbol=sym, follower=self.name)
                continue
            specs[sym] = sp
        if self.factor_source is not None:
            try:
                self.size_factor = float(self.factor_source())
            except Exception:  # noqa: BLE001 - config illisible : on garde le facteur courant
                pass
        open_tickets = {fp.ticket for fp in follower}
        self.mapping = {m: e for m, e in self.mapping.items() if int(e.get("ticket", 0)) in open_tickets}
        # une copie encore ouverte (appariée) compte comme « déjà copiée » ; on oublie les tickets que le maître a fermés
        master_tickets = {int(p["ticket"]) for p in master.get("positions", [])}
        avant = set(self.done)
        self.done |= set(self.mapping)
        self.done &= master_tickets
        if self.done != avant:
            self._save_done()
        self._skip_old_positions(master)
        self._master_vol = {int(p["ticket"]): float(p["volume"]) for p in master.get("positions", [])}
        self._follower_vol = {fp.ticket: float(fp.volume) for fp in follower}
        actions = plan_sync(master, follower, float(acc.equity), self.size_factor, specs, allow_open=fresh,
                            mapping=self.mapping, sym_map=sym_map)
        # une ouverture ou une modification SL/TP refusée n'est retentée qu'après une temporisation
        # croissante (`_backoff`) : ni spam du journal ni martèlement du broker — les fermetures et
        # réductions (qui diminuent l'exposition) ne sont jamais temporisées
        actions = [a for a in actions
                   if not (a.kind == "open" and self._retry_after.get(a.master_ticket, now) > now)
                   and not (a.kind == "open" and a.master_ticket in self.done)
                   and not (a.kind == "modify" and self._modify_after.get(a.follower_ticket, now) > now)]
        for mt in sorted(self.done - set(self.mapping) - self._done_logged):
            self._done_logged.add(mt)
            self._log("copie fermée chez le suiveur : pas de réouverture", master_ticket=mt, follower=self.name)
        self._now = now
        for a in actions:
            try:
                self._apply(a)
            except Exception as e:  # noqa: BLE001 - une action ratée n'empêche pas les autres
                self._log("action copy échouée", kind=a.kind, symbol=a.symbol, error=type(e).__name__)
        self._save_map()
        self._write_status(now, actions)
        return actions

    def _write_status(self, now: datetime, actions: list[CopyAction]) -> None:
        """État du compte suiveur pour l'onglet par compte du dashboard (2026-09-22) : écriture atomique,
        jamais bloquante — une erreur d'écriture n'affecte pas la réplication."""
        if self.status_file is None:
            return
        try:
            import os as _os

            acc = self.broker.account_info()
            poss = self.broker.positions(magic=self.magic)
            self._recent = (self._recent + [{"ts": now.isoformat(), "kind": a.kind, "symbol": a.symbol,
                                             "volume": round(a.volume, 4), "reason": a.reason}
                                            for a in actions])[-20:]
            payload = {"name": self.name, "ts_utc": now.isoformat(), "size_factor": self.size_factor,
                       "equity": float(acc.equity) if acc else None,
                       "balance": float(acc.balance) if acc else None,
                       "positions": [{"symbol": p.symbol, "side": p.side.value, "volume": p.volume,
                                      "sl": p.sl, "profit": round(p.profit, 2)} for p in poss],
                       "actions": self._recent,
                       # trades fermés et relevé RÉEL du compte (historique MT5, 2026-09-24)
                       "closed": self._closed_trades(now),
                       "statement": getattr(self, "_statement", None)}
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            _os.replace(tmp, self.status_file)
        except Exception:  # noqa: BLE001
            pass

    CLOSED_REFRESH_SEC = 60.0
    CLOSED_HISTORY_DAYS = 90

    def _closed_trades(self, now: datetime) -> list[dict]:
        """Trades fermés du compte suiveur (deals MT5 au magic du copieur, 90 jours), rafraîchis toutes les 60 s :
        l'historique ne bouge qu'au rythme des clôtures et `history_deals` coûte un aller-retour terminal."""
        from ..learning.consistency import trades_from_deals

        cache = getattr(self, "_closed_cache", None)
        if cache is not None and (now - cache[0]).total_seconds() < self.CLOSED_REFRESH_SEC:
            return cache[1]
        try:
            from ..learning.consistency import account_statement

            deals = self.broker.history_deals(now - timedelta(days=self.CLOSED_HISTORY_DAYS), now + timedelta(minutes=5))
            acc = self.broker.account_info()
            # « les vrais stats de chaque compte » (2026-09-24) : TOUS les trades du compte, commissions et swaps compris
            st_ = account_statement(deals, equity=float(acc.equity) if acc else None, since=self.stats_since,
                                    balance=float(acc.balance) if acc else None)
            self._statement = {k: v for k, v in st_.items() if k != "trades"}
            trades = st_["trades"]
        except Exception:  # noqa: BLE001 - historique indisponible : on garde le dernier connu
            trades = cache[1] if cache else []
        self._closed_cache = (now, trades)
        return trades

    #: écart relatif de prix maître / suiveur au-delà duquel SL/TP sont recopiés en DISTANCE et non en niveau
    PRICE_GAP_RATIO = 0.002

    #: une position du maître plus ancienne que ceci AU DÉMARRAGE du suiveur n'est pas copiée
    MAX_OPEN_AGE_SEC = 600.0

    def _skip_old_positions(self, master: dict) -> None:
        """Marque « déjà traitée » toute position du maître ouverte bien avant le démarrage de ce suiveur."""
        limite = self.started_at - timedelta(seconds=self.MAX_OPEN_AGE_SEC)
        nouveaux = []
        for p in master.get("positions", []):
            t = int(p["ticket"])
            if t in self.done or t in self.mapping or not p.get("time_open"):
                continue
            try:
                ouverte = datetime.fromisoformat(str(p["time_open"]))
            except ValueError:
                continue
            if ouverte.tzinfo is None:
                ouverte = ouverte.replace(tzinfo=timezone.utc)
            if ouverte < limite:
                self.done.add(t)
                nouveaux.append((t, p.get("symbol")))
        for t, sym in nouveaux:
            self._log("position antérieure au démarrage du suiveur : non copiée", master_ticket=t, symbol=sym)
        if nouveaux:
            self._save_done()

    def _shift_to_follower_price(self, a) -> None:
        """Un suiveur peut coter un autre contrat que le maître (2026-09-25 : Brent 104,90 chez IC, 98,21 chez Admirals).
        Recopier le niveau exact du stop le plaçait du mauvais côté du prix (« stop trop proche : copie reportée » ×8).
        Au-delà de 0,2 % d'écart, SL et TP sont décalés de l'écart de prix : même DISTANCE au prix que chez le maître."""
        if not a.master_price:
            return
        tick = self.broker.tick(a.symbol)
        spec = self.broker.symbol_info(a.symbol)
        if tick is None or spec is None:
            return
        mid = (float(tick.bid) + float(tick.ask)) / 2.0
        ecart = mid - float(a.master_price)
        if abs(ecart) <= self.PRICE_GAP_RATIO * float(a.master_price):
            return
        digits = int(getattr(spec, "digits", 5) or 5)
        ecart = round(ecart, digits)
        a.shift = ecart
        if a.sl:
            a.sl = round(a.sl + ecart, digits)
        if a.tp:
            a.tp = round(a.tp + ecart, digits)
        entree = self.mapping.get(a.master_ticket) if a.kind == "modify" else None
        if isinstance(entree, dict) and not entree.get("shift"):
            entree["shift"] = ecart           # copie ouverte avant ce correctif : le décalage est figé une fois pour toutes
        if a.kind == "open":                  # une modification est réévaluée à chaque synchro : pas de journal en boucle
            self._log("SL/TP décalés au prix du suiveur", symbol=a.symbol, master_ticket=a.master_ticket,
                      ecart=round(ecart, digits), sl=a.sl, tp=a.tp)

    #: élargissement maximal accepté d'un stop pour respecter la distance minimale du broker suiveur
    MAX_SL_WIDEN_RATIO = 2.0

    def _fit_stops(self, symbol: str, side, sl: float, tp: float, current_sl: float | None = None,
                   current_tp: float | None = None):
        """Adapte SL/TP du maître à la distance minimale du broker SUIVEUR (2026-09-24 : Admirals impose 46 points la
        nuit sur AUDCHF ; stop du maître à 44 points → « Invalid stops » en boucle sur 4 comptes).
        - TP trop proche → retiré (la copie est fermée quand le maître ferme) ou TP actuel conservé ;
        - SL trop proche → porté à la distance minimale si l'élargissement reste ≤ ×2 (ouverture) ; en modification,
          jamais plus large que le stop actuel (never_widen_stop).
        Renvoie (sl, tp, note) ou None si aucun stop acceptable n'existe."""
        spec = self.broker.symbol_info(symbol)
        tick = self.broker.tick(symbol)
        if spec is None or tick is None or not sl:
            return float(sl or 0.0), float(tp or 0.0), ""
        pt = float(getattr(spec, "point", 0.0) or 0.0)
        mini = (int(getattr(spec, "stops_level_points", 0) or 0) + 2) * pt
        digits = int(getattr(spec, "digits", 5) or 5)
        sell = getattr(side, "value", side) == "SELL"
        ref_sl = tick.ask if sell else tick.bid          # un SELL se ferme à l'ask, un BUY au bid
        note = []
        dist = (sl - ref_sl) if sell else (ref_sl - sl)
        if dist < mini:
            new_sl = round(ref_sl + mini if sell else ref_sl - mini, digits)
            if current_sl:                                # modification : jamais plus large que le stop actuel
                plus_large = new_sl > current_sl if sell else new_sl < current_sl
                if plus_large:
                    return None
            elif dist <= 0 or abs(new_sl - ref_sl) > self.MAX_SL_WIDEN_RATIO * max(dist, pt):
                return None
            note.append(f"stop {sl} → {new_sl} (distance minimale {mini / pt:.0f} pts)")
            sl = new_sl
        if tp:
            tdist = (ref_sl - tp) if sell else (tp - ref_sl)
            if tdist < mini:
                tp = float(current_tp or 0.0)
                note.append("TP trop proche : " + ("TP actuel conservé" if current_tp else "retiré (fermeture avec le maître)"))
        return float(sl), float(tp or 0.0), "; ".join(note)

    def _backoff(self, table: dict, kind: str, key: int) -> float:
        """Prochaine tentative pour (kind, key) : 60 s, 120 s, 240 s… plafonné à RETRY_MAX_SEC. Renvoie le délai."""
        n = self._failures.get((kind, key), 0) + 1
        self._failures[(kind, key)] = n
        delay = min(RETRY_MAX_SEC, RETRY_BASE_SEC * (2 ** (n - 1)))
        table[key] = getattr(self, "_now", datetime.now(timezone.utc)) + timedelta(seconds=delay)
        return delay

    @staticmethod
    def _detail(res) -> dict:
        """Ce que le broker a répondu, pour que le journal explique un refus (retcode + commentaire)."""
        return {"retcode": getattr(res, "retcode", None), "detail": str(getattr(res, "comment", ""))[:80]}

    def _apply(self, a: CopyAction) -> None:
        if a.kind in ("open", "modify"):
            self._shift_to_follower_price(a)
        if a.kind == "open":
            fit = self._fit_stops(a.symbol, a.side, a.sl, a.tp)
            if fit is None:
                # délai croissant (60 s, 120 s, 240 s… 15 min) : le 26/09 un stop BTC trop serré pour les suiveurs a été
                # retenté (et journalisé) chaque minute pendant 50 min, soit ~200 lignes identiques
                retry_in = self._backoff(self._retry_after, "open", a.master_ticket)
                self._log("ouverture copiée", symbol=a.symbol, master_symbol=a.master_symbol or a.symbol, side=a.side.value,
                          volume=a.volume, master_ticket=a.master_ticket, ok=False, retcode=None, retry_in_sec=retry_in,
                          detail="stop du maître trop proche pour ce broker : copie reportée")
                return
            sl_ok, tp_ok, note = fit
            if note:
                self._log("stops ajustés au broker suiveur", symbol=a.symbol, master_ticket=a.master_ticket,
                          sl_maitre=a.sl, sl=sl_ok, tp_maitre=a.tp, tp=tp_ok, detail=note)
            req = OrderRequest(symbol=a.symbol, side=a.side, volume=a.volume, sl=sl_ok, tp=tp_ok,
                               kind=OrderKind.MARKET, magic=self.magic,
                               comment=f"{COPY_COMMENT_PREFIX}{a.master_ticket}")
            res = self.broker.order_send(req)
            if res.ok:
                self.done.add(int(a.master_ticket))
                self._save_done()
            if res.ok and getattr(res, "ticket", 0):
                self.mapping[a.master_ticket] = {"ticket": int(res.ticket),
                                                 "mv": self._master_vol.get(a.master_ticket, 0.0), "fv": float(a.volume)}
                if a.shift:
                    self.mapping[a.master_ticket]["shift"] = a.shift
            retry_in = None
            if res.ok:
                self._failures.pop(("open", a.master_ticket), None)
            else:
                retry_in = self._backoff(self._retry_after, "open", a.master_ticket)
            self._log("ouverture copiée", symbol=a.symbol, master_symbol=a.master_symbol or a.symbol,
                      side=a.side.value, volume=a.volume, master_ticket=a.master_ticket, ok=bool(res.ok),
                      retry_in_sec=retry_in, **self._detail(res))
        elif a.kind == "adopt":
            self.mapping[a.master_ticket] = {"ticket": int(a.follower_ticket),
                                             "mv": self._master_vol.get(a.master_ticket, 0.0),
                                             "fv": self._follower_vol.get(a.follower_ticket, 0.0)}
            self._log("copie adoptée", ticket=a.follower_ticket, master_ticket=a.master_ticket)
        elif a.kind == "close":
            res = self.broker.close_position(a.follower_ticket, comment="copy: maître clôturé")
            if res.ok:
                self.mapping.pop(a.master_ticket, None)
            self._log("fermeture copiée", ticket=a.follower_ticket, master_ticket=a.master_ticket, ok=bool(res.ok),
                      **({} if res.ok else self._detail(res)))
        elif a.kind == "reduce":
            res = self.broker.close_position(a.follower_ticket, volume=a.volume, comment="copy: partiel maître")
            if res.ok and a.master_ticket in self.mapping:
                # repère mis à jour : la prochaine réduction se mesure depuis ce nouveau couple de volumes
                self.mapping[a.master_ticket]["mv"] = self._master_vol.get(a.master_ticket, 0.0)
                self.mapping[a.master_ticket]["fv"] = max(0.0, self._follower_vol.get(a.follower_ticket, 0.0) - float(a.volume))
            self._log("réduction copiée", ticket=a.follower_ticket, volume=a.volume, ok=bool(res.ok),
                      **({} if res.ok else self._detail(res)))
        elif a.kind == "modify":
            pos = next((p for p in (self.broker.positions(magic=self.magic) or []) if p.ticket == a.follower_ticket), None)
            if pos is not None:
                fit = self._fit_stops(pos.symbol, pos.side, a.sl, a.tp, current_sl=pos.sl, current_tp=pos.tp)
                if fit is None:
                    return                         # impossible sans élargir le stop : on garde le stop actuel, sans boucle
                a.sl, a.tp, _ = fit
                if abs(a.sl - pos.sl) < 1e-12 and abs(a.tp - pos.tp) < 1e-12:
                    return                         # rien de plaçable de plus que l'existant chez ce broker
            res = self.broker.modify_position(a.follower_ticket, a.sl, a.tp)
            # « No changes » (10025) : le broker confirme que SL/TP sont déjà en place — un succès, pas un échec
            ok = bool(getattr(res, "ok", False)) or getattr(res, "retcode", None) == RETCODE_NO_CHANGES
            retry_in = None
            if ok:
                self._failures.pop(("modify", a.follower_ticket), None)
            else:
                retry_in = self._backoff(self._modify_after, "modify", a.follower_ticket)
            self._log("SL/TP copiés", ticket=a.follower_ticket, sl=a.sl, tp=a.tp, ok=ok,
                      retry_in_sec=retry_in, **({} if ok else self._detail(res)))

    def run_forever(self, poll_sec: float = 5.0) -> None:  # pragma: no cover - boucle process
        while True:
            self.sync()
            time.sleep(poll_sec)
