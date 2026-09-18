"""Adaptateur MetaTrader5 réel (Windows x64 uniquement).

Import paresseux du package MetaTrader5 : sur Linux/macOS l'adaptateur signale
clairement l'indisponibilité au lieu de planter à l'import.

Credentials : lus UNIQUEMENT depuis l'environnement (MT5_LOGIN / MT5_PASSWORD /
MT5_SERVER / MT5_TERMINAL_PATH). Si le terminal est déjà connecté, initialize()
sans credentials suffit et aucun mot de passe n'est nécessaire.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from ..core.clock import set_server_utc_offset
from ..core.types import (AccountInfo, Deal, OrderKind, OrderRequest, OrderResult, PendingOrder, Position, Side,
                          SymbolSpec, Tick, TradeMode)
from .adapter import RATES_COLUMNS, BrokerAdapter, empty_rates
from .symbols import asset_class_of, root_of

log = logging.getLogger("tradinglab.mt5")

try:  # pragma: no cover - dépend de la plateforme
    import MetaTrader5 as mt5  # type: ignore
    MT5_AVAILABLE = True
except Exception:  # noqa: BLE001
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False

TF_MAP = {}
if MT5_AVAILABLE:  # pragma: no cover
    TF_MAP = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
    }

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_NO_TICK = -3        # tick indisponible (symbole non sélectionné / terminal déconnecté)
RETCODE_SL_REFUSED = -4     # refus adaptateur : SL absent (never_remove_stop)
RETCODE_EXCEPTION = -9      # exception interne convertie en OrderResult (l'interface ne lève jamais)

# Calibration du décalage heure serveur − UTC (voir `_calibrate_offset`)
OFFSET_ROUND_SEC = 1800             # les décalages broker sont des multiples de la demi-heure
OFFSET_MAX_ABS_SEC = 14 * 3600      # au-delà : tick trop ancien (marché fermé), pas de calibration
OFFSET_RECALIBRATE_SEC = 600        # nouvelle mesure toutes les 10 min (changement d'heure serveur)
OFFSET_FUTURE_TOLERANCE_SEC = 60    # tick « dans le futur » au-delà de cette marge → décalage trop grand
OFFSET_STABLE_TOLERANCE_SEC = 120   # une baisse n'est acceptée que si la mesure brute est stable (≠ flux gelé)
OFFSET_STABLE_MEASURES = 2          # ... sur ce nombre de mesures consécutives
OFFSET_MAX_SYMBOLS = 12             # nombre de symboles visibles interrogés pour la mesure


class _NoTick(Exception):
    """Tick indisponible pour un symbole (interne à l'adaptateur)."""


class MT5Adapter(BrokerAdapter):  # pragma: no cover - nécessite Windows + terminal
    name = "mt5"

    def __init__(self, asset_rules: dict | None = None, deviation: int = 20):
        self.asset_rules = asset_rules or {}
        self.deviation = deviation
        self._connected = False
        self._err = ""
        # Décalage heure serveur − UTC (secondes), None tant qu'il n'a pas pu être mesuré.
        # Les epochs `time` du package MetaTrader5 sont exprimés dans l'heure du serveur, pas en UTC.
        self._server_offset_sec: Optional[int] = None
        self._offset_measured_at: float = 0.0
        self._last_raw_tick_ts: float = 0.0   # dernier tick de référence brut (heure serveur), pour server_time()
        self._pending_raw: Optional[float] = None   # mesure brute « en baisse » en attente de confirmation
        self._pending_count: int = 0

    # ---------- connexion ----------
    def connect(self) -> bool:
        if not MT5_AVAILABLE:
            self._err = "Package MetaTrader5 indisponible (Windows x64 requis)"
            return False
        kwargs = {}
        path = os.environ.get("MT5_TERMINAL_PATH")
        if path and os.path.exists(path):
            kwargs["path"] = path
        login = os.environ.get("MT5_LOGIN")
        password = os.environ.get("MT5_PASSWORD")
        server = os.environ.get("MT5_SERVER")
        if login and password and server:
            try:
                login_i = int(str(login).strip().replace("\u00a0", ""))
            except ValueError:
                # .env mal formé (commentaire collé, BOM, espace insécable) : erreur claire, pas de crash
                self._err = "MT5_LOGIN invalide (doit être un entier)"
                self._connected = False
                return False
            kwargs.update(login=login_i, password=password, server=server)
        try:
            ok = mt5.initialize(**kwargs)
        except Exception as e:  # noqa: BLE001 - l'orchestrateur doit survivre à un terminal absent/instable
            self._err = f"initialize: {type(e).__name__}: {e}"
            self._connected = False
            return False
        if not ok:
            self._err = f"initialize() a échoué: {mt5.last_error()}"
            self._connected = False
            return False
        try:
            info = mt5.account_info()
        except Exception as e:  # noqa: BLE001
            self._err = f"account_info: {type(e).__name__}: {e}"
            self._connected = False
            return False
        if info is None:
            self._err = f"account_info() vide: {mt5.last_error()}"
            mt5.shutdown()
            self._connected = False
            return False
        self._connected = True
        self._err = ""
        self._calibrate_offset(force=True)
        return True

    # ---------- décalage heure serveur ----------
    @property
    def server_offset_sec(self) -> Optional[int]:
        """Décalage heure serveur − UTC calibré (secondes), None si non mesuré (marché fermé…)."""
        return self._server_offset_sec

    def _freshest_raw_tick(self) -> Optional[float]:
        """Epoch (heure serveur, secondes) du tick le plus récent parmi quelques symboles visibles."""
        try:
            syms = mt5.symbols_get() or []
        except Exception:  # noqa: BLE001
            syms = []
        names = [x.name for x in syms if getattr(x, "visible", True)][:OFFSET_MAX_SYMBOLS]
        best: Optional[float] = None
        for n in names:
            try:
                t = mt5.symbol_info_tick(n)
            except Exception:  # noqa: BLE001
                continue
            if t is None:
                continue
            ts = (float(getattr(t, "time_msc", 0) or 0) / 1000.0) or float(getattr(t, "time", 0) or 0)
            if ts and (best is None or ts > best):
                best = ts
        return best

    def _calibrate_offset(self, force: bool = False) -> Optional[int]:
        """Mesure `offset = tick_serveur − horloge PC`, arrondi à la demi-heure.

        Règles déterministes :
        - tick de référence plus vieux que 14 h (marché fermé) → pas de mesure ;
        - première mesure, mesure **plus grande** (avance d'heure serveur : les ticks apparaissent dans le
          futur avec l'ancien offset) → acceptée immédiatement ;
        - mesure **plus petite** : indiscernable a priori d'un flux gelé (les ticks vieillissent dans les
          deux cas). Un flux gelé donne une mesure brute qui **dérive** (−600 s par mesure) ; un recul
          d'heure serveur donne une mesure brute **stable**. On n'accepte donc la baisse qu'après
          `OFFSET_STABLE_MEASURES` mesures consécutives stables (± `OFFSET_STABLE_TOLERANCE_SEC`).
          Entre-temps les ticks paraissent vieux → données NON fraîches (fail-safe), jamais l'inverse.
        """
        now_pc = time.time()
        if not force and now_pc - self._offset_measured_at < OFFSET_RECALIBRATE_SEC:
            return self._server_offset_sec
        self._offset_measured_at = now_pc
        ref = self._freshest_raw_tick()
        if ref is None:
            return self._server_offset_sec
        self._last_raw_tick_ts = ref
        raw = ref - now_pc
        if abs(raw) > OFFSET_MAX_ABS_SEC:
            if self._server_offset_sec is None:
                log.warning("décalage serveur non calibrable (tick de référence vieux de %.0f h)", abs(raw) / 3600)
            return self._server_offset_sec
        measured = int(round(raw / OFFSET_ROUND_SEC) * OFFSET_ROUND_SEC)
        cur = self._server_offset_sec
        accept = cur is None or measured > cur or (raw - cur) > OFFSET_FUTURE_TOLERANCE_SEC
        if not accept and measured < cur:
            if self._pending_raw is not None and abs(raw - self._pending_raw) <= OFFSET_STABLE_TOLERANCE_SEC:
                self._pending_count += 1
            else:
                self._pending_raw, self._pending_count = raw, 1
            accept = self._pending_count >= OFFSET_STABLE_MEASURES
            if not accept:
                log.warning("décalage serveur mesuré en baisse (%+d s vs %+d s) : en attente de confirmation "
                            "(flux gelé ?)", measured, cur)
        if accept:
            self._pending_raw, self._pending_count = None, 0
            if measured != cur:
                log.info("décalage heure serveur − UTC calibré : %+d s (brut %+.0f s)", measured, raw)
            self._server_offset_sec = measured
            set_server_utc_offset(measured)
        elif measured == cur:
            self._pending_raw, self._pending_count = None, 0
        return self._server_offset_sec

    def _to_utc(self, ts: float | int) -> datetime:
        """Epoch MT5 (heure serveur) → datetime UTC réel."""
        self._calibrate_offset()
        return datetime.fromtimestamp(float(ts) - (self._server_offset_sec or 0), tz=timezone.utc)

    def _from_utc(self, dt: datetime) -> datetime:
        """Datetime UTC → datetime « heure serveur » attendu par les requêtes d'historique MT5."""
        self._calibrate_offset()
        return dt + timedelta(seconds=self._server_offset_sec or 0)

    def disconnect(self) -> None:
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        if not (MT5_AVAILABLE and self._connected):
            return False
        t = mt5.terminal_info()
        return bool(t and t.connected)

    def last_error(self) -> str:
        if MT5_AVAILABLE:
            return f"{self._err} {mt5.last_error()}".strip()
        return self._err

    # ---------- compte ----------
    def account_info(self) -> Optional[AccountInfo]:
        a = mt5.account_info()
        if a is None:
            return None
        mode = {0: TradeMode.DEMO, 1: TradeMode.CONTEST, 2: TradeMode.REAL}.get(a.trade_mode, TradeMode.UNKNOWN)
        return AccountInfo(
            login=a.login, server=a.server, trade_mode=mode, balance=a.balance, equity=a.equity,
            margin=a.margin, margin_free=a.margin_free, currency=a.currency, leverage=a.leverage,
            name=a.name, hedging=(a.margin_mode == 2), connected=self.is_connected(),
        )

    # ---------- symboles ----------
    def symbols(self) -> list[str]:
        syms = mt5.symbols_get()
        return [s.name for s in syms] if syms else []

    def symbol_select(self, symbol: str) -> bool:
        return bool(mt5.symbol_select(symbol, True))

    def symbol_info(self, symbol: str) -> Optional[SymbolSpec]:
        """Contrat `Optional[SymbolSpec]` : None si le symbole est indisponible, jamais d'exception
        (un seul symbole retiré du Market Watch ne doit pas faire basculer tout le cycle en SAFE_MODE)."""
        try:
            s = mt5.symbol_info(symbol)
            if s is None:
                return None
            if not s.visible:
                if not mt5.symbol_select(symbol, True):
                    return None
                s = mt5.symbol_info(symbol)
                if s is None:
                    return None
            return SymbolSpec(
                name=s.name, digits=s.digits, point=s.point, tick_size=s.trade_tick_size or s.point,
                tick_value=s.trade_tick_value, contract_size=s.trade_contract_size,
                volume_min=s.volume_min, volume_max=s.volume_max, volume_step=s.volume_step,
                stops_level_points=int(s.trade_stops_level), trade_allowed=(s.trade_mode == 4),
                currency_base=s.currency_base, currency_profit=s.currency_profit, currency_margin=s.currency_margin,
                asset_class=asset_class_of(s.name, self.asset_rules), spread_points=int(s.spread), root=root_of(s.name),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("symbol_info(%s) indisponible: %s: %s", symbol, type(e).__name__, e)
            return None

    # ---------- données ----------
    def tick(self, symbol: str) -> Optional[Tick]:
        t = mt5.symbol_info_tick(symbol)
        if t is None or t.bid == 0:
            return None
        ts = (float(getattr(t, "time_msc", 0) or 0) / 1000.0) or float(t.time)
        return Tick(symbol=symbol, time=self._to_utc(ts), bid=t.bid, ask=t.ask, last=t.last, volume=t.volume)

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = TF_MAP.get(timeframe.upper())
        if tf is None:
            return empty_rates()
        arr = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if arr is None or len(arr) == 0:
            return empty_rates()
        df = pd.DataFrame(arr)
        self._calibrate_offset()
        # epochs en heure serveur → UTC réel (les barres H4/D1 restent alignées sur minuit serveur)
        df["time"] = pd.to_datetime(df["time"] - (self._server_offset_sec or 0), unit="s", utc=True)
        return df[RATES_COLUMNS].copy()

    # ---------- positions ----------
    def positions(self, magic: Optional[int] = None) -> list[Position]:
        ps = mt5.positions_get() or []
        out = []
        for p in ps:
            if magic is not None and p.magic != magic:
                continue
            out.append(Position(
                ticket=p.ticket, symbol=p.symbol, side=Side.BUY if p.type == 0 else Side.SELL, volume=p.volume,
                price_open=p.price_open, sl=p.sl, tp=p.tp, profit=p.profit,
                time_open=self._to_utc(p.time), magic=p.magic, comment=p.comment,
                price_current=p.price_current, swap=p.swap,
            ))
        return out

    def pending_orders(self, magic: Optional[int] = None) -> list[PendingOrder]:
        os_ = mt5.orders_get() or []
        out = []
        for o in os_:
            if magic is not None and o.magic != magic:
                continue
            side = Side.BUY if o.type in (0, 2, 4, 6) else Side.SELL
            kind = OrderKind.LIMIT if o.type in (2, 3) else OrderKind.STOP if o.type in (4, 5, 6, 7) else OrderKind.MARKET
            out.append(PendingOrder(ticket=o.ticket, symbol=o.symbol, side=side, kind=kind, volume=o.volume_current,
                                    price=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic, comment=o.comment))
        return out

    def history_deals(self, start: datetime, end: datetime) -> list[Deal]:
        ds = mt5.history_deals_get(self._from_utc(start), self._from_utc(end)) or []
        out = []
        for d in ds:
            entry = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}.get(d.entry, "UNKNOWN")
            out.append(Deal(ticket=d.ticket, order=d.order, position_id=d.position_id, symbol=d.symbol,
                            side=Side.BUY if d.type == 0 else Side.SELL, volume=d.volume, price=d.price,
                            profit=d.profit, commission=d.commission, swap=d.swap,
                            time=self._to_utc(d.time), magic=d.magic, entry=entry,
                            comment=d.comment))
        return out

    # ---------- ordres ----------
    def _tick_or_raise(self, symbol: str):
        """Tick brut MT5 ; tente une resélection du symbole dans le Market Watch, sinon lève `_NoTick`."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not getattr(tick, "bid", 0):
            try:
                mt5.symbol_select(symbol, True)
            except Exception:  # noqa: BLE001
                pass
            tick = mt5.symbol_info_tick(symbol)
        if tick is None or not getattr(tick, "bid", 0):
            raise _NoTick(f"tick indisponible: {mt5.last_error()}")
        return tick

    def _build_request(self, req: OrderRequest) -> dict:
        tick = self._tick_or_raise(req.symbol)
        if req.kind is OrderKind.MARKET:
            action = mt5.TRADE_ACTION_DEAL
            otype = mt5.ORDER_TYPE_BUY if req.side is Side.BUY else mt5.ORDER_TYPE_SELL
            price = tick.ask if req.side is Side.BUY else tick.bid
        else:
            action = mt5.TRADE_ACTION_PENDING
            if req.kind is OrderKind.LIMIT:
                otype = mt5.ORDER_TYPE_BUY_LIMIT if req.side is Side.BUY else mt5.ORDER_TYPE_SELL_LIMIT
            else:
                otype = mt5.ORDER_TYPE_BUY_STOP if req.side is Side.BUY else mt5.ORDER_TYPE_SELL_STOP
            price = req.price
        r = {
            "action": action, "symbol": req.symbol, "volume": float(req.volume), "type": otype, "price": float(price),
            "sl": float(req.sl), "tp": float(req.tp or 0.0), "deviation": int(req.deviation_points or self.deviation),
            "magic": int(req.magic), "comment": req.comment[:31], "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(req.symbol),
        }
        return r

    def _filling(self, symbol: str) -> int:
        s = mt5.symbol_info(symbol)
        fm = s.filling_mode if s else 0
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _result(res, req: dict) -> OrderResult:
        if res is None:
            return OrderResult(ok=False, retcode=-1, comment=f"order result None: {mt5.last_error()}", request=req)
        ok = res.retcode in (RETCODE_DONE, RETCODE_PLACED, 0)
        return OrderResult(ok=ok, retcode=res.retcode, ticket=getattr(res, "order", 0) or getattr(res, "deal", 0),
                           price=getattr(res, "price", 0.0), volume=getattr(res, "volume", 0.0),
                           comment=getattr(res, "comment", ""), request=req)

    @staticmethod
    def _failed(e: Exception, req: dict | None = None) -> OrderResult:
        """Exception interne → `OrderResult(ok=False)` : l'interface promet un résultat, jamais une exception
        (les fermetures d'urgence du watchdog / position manager / commandes doivent pouvoir journaliser)."""
        if isinstance(e, _NoTick):
            return OrderResult(ok=False, retcode=RETCODE_NO_TICK, comment=str(e), request=req or {})
        return OrderResult(ok=False, retcode=RETCODE_EXCEPTION, comment=f"{type(e).__name__}: {e}", request=req or {})

    def order_check(self, req: OrderRequest) -> OrderResult:
        try:
            r = self._build_request(req)
            return self._result(mt5.order_check(r), r)
        except Exception as e:  # noqa: BLE001
            return self._failed(e, req.to_dict())

    def order_send(self, req: OrderRequest) -> OrderResult:
        try:
            r = self._build_request(req)
        except Exception as e:  # noqa: BLE001 - rien n'a été envoyé
            return self._failed(e, req.to_dict())
        try:
            res = self._result(mt5.order_send(r), r)
        except Exception as e:  # noqa: BLE001
            return self._failed(e, r)
        try:
            if res.ok and req.kind is OrderKind.MARKET:
                # retrouver le ticket de position (deal -> position_id)
                pos = [p for p in mt5.positions_get(symbol=req.symbol) or [] if p.magic == req.magic]
                if pos:
                    res.ticket = sorted(pos, key=lambda p: p.time)[-1].ticket
        except Exception as e:  # noqa: BLE001 - l'ordre est passé : on garde le résultat, ticket du deal
            log.warning("order_send: ticket de position introuvable après fill: %s: %s", type(e).__name__, e)
        return res

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        if not sl or sl <= 0:
            # défense en profondeur : l'adaptateur refuse lui-même de retirer un SL (never_remove_stop)
            return OrderResult(ok=False, retcode=RETCODE_SL_REFUSED, comment="refus : SL absent (never_remove_stop)")
        try:
            p = mt5.positions_get(ticket=ticket)
            if not p:
                return OrderResult(ok=False, retcode=-2, comment="position introuvable")
            p = p[0]
            r = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": p.symbol, "sl": float(sl),
                 "tp": float(tp or 0.0), "magic": p.magic}
            return self._result(mt5.order_send(r), r)
        except Exception as e:  # noqa: BLE001
            return self._failed(e)

    def close_position(self, ticket: int, volume: Optional[float] = None, comment: str = "") -> OrderResult:
        try:
            p = mt5.positions_get(ticket=ticket)
            if not p:
                return OrderResult(ok=False, retcode=-2, comment="position introuvable")
            p = p[0]
            tick = self._tick_or_raise(p.symbol)
            closing_buy = p.type == 1  # fermer un SELL = BUY
            r = {
                "action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": p.symbol,
                "volume": float(volume or p.volume), "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,
                "price": tick.ask if closing_buy else tick.bid, "deviation": self.deviation, "magic": p.magic,
                "comment": (comment or "TLAB close")[:31], "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(p.symbol),
            }
            return self._result(mt5.order_send(r), r)
        except Exception as e:  # noqa: BLE001
            return self._failed(e)

    def cancel_order(self, ticket: int) -> OrderResult:
        r = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        try:
            return self._result(mt5.order_send(r), r)
        except Exception as e:  # noqa: BLE001
            return self._failed(e, r)

    def server_time(self) -> datetime:
        """Heure serveur exprimée en UTC réel.

        Offset calibré → l'horloge PC (UTC) est la bonne référence puisque tous les horodatages MT5 sont
        ramenés en UTC réel. Sinon (marché fermé, aucune mesure possible) → dernier tick de référence
        brut, non corrigé : un tick est alors comparé à un autre tick (même repère), jamais à une horloge
        PC décalée de plusieurs heures.
        """
        self._calibrate_offset()
        if self._server_offset_sec is not None:
            return datetime.fromtimestamp(time.time(), tz=timezone.utc)
        ref = self._last_raw_tick_ts or self._freshest_raw_tick()
        if ref:
            self._last_raw_tick_ts = ref
            return datetime.fromtimestamp(ref, tz=timezone.utc)
        return datetime.fromtimestamp(time.time(), tz=timezone.utc)
