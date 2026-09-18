"""Adaptateur MetaTrader5 réel (Windows x64 uniquement).

Import paresseux du package MetaTrader5 : sur Linux/macOS l'adaptateur signale
clairement l'indisponibilité au lieu de planter à l'import.

Credentials : lus UNIQUEMENT depuis l'environnement (MT5_LOGIN / MT5_PASSWORD /
MT5_SERVER / MT5_TERMINAL_PATH). Si le terminal est déjà connecté, initialize()
sans credentials suffit et aucun mot de passe n'est nécessaire.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from ..core.types import (AccountInfo, Deal, OrderKind, OrderRequest, OrderResult, PendingOrder, Position, Side,
                          SymbolSpec, Tick, TradeMode)
from .adapter import RATES_COLUMNS, BrokerAdapter, empty_rates
from .symbols import asset_class_of, root_of

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


class MT5Adapter(BrokerAdapter):  # pragma: no cover - nécessite Windows + terminal
    name = "mt5"

    def __init__(self, asset_rules: dict | None = None, deviation: int = 20):
        self.asset_rules = asset_rules or {}
        self.deviation = deviation
        self._connected = False
        self._err = ""

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
            kwargs.update(login=int(login), password=password, server=server)
        ok = mt5.initialize(**kwargs)
        if not ok:
            self._err = f"initialize() a échoué: {mt5.last_error()}"
            self._connected = False
            return False
        info = mt5.account_info()
        if info is None:
            self._err = f"account_info() vide: {mt5.last_error()}"
            mt5.shutdown()
            self._connected = False
            return False
        self._connected = True
        self._err = ""
        return True

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
        s = mt5.symbol_info(symbol)
        if s is None:
            return None
        if not s.visible:
            mt5.symbol_select(symbol, True)
            s = mt5.symbol_info(symbol)
        return SymbolSpec(
            name=s.name, digits=s.digits, point=s.point, tick_size=s.trade_tick_size or s.point,
            tick_value=s.trade_tick_value, contract_size=s.trade_contract_size,
            volume_min=s.volume_min, volume_max=s.volume_max, volume_step=s.volume_step,
            stops_level_points=int(s.trade_stops_level), trade_allowed=(s.trade_mode == 4),
            currency_base=s.currency_base, currency_profit=s.currency_profit, currency_margin=s.currency_margin,
            asset_class=asset_class_of(s.name, self.asset_rules), spread_points=int(s.spread), root=root_of(s.name),
        )

    # ---------- données ----------
    def tick(self, symbol: str) -> Optional[Tick]:
        t = mt5.symbol_info_tick(symbol)
        if t is None or t.bid == 0:
            return None
        return Tick(symbol=symbol, time=datetime.fromtimestamp(t.time, tz=timezone.utc), bid=t.bid, ask=t.ask,
                    last=t.last, volume=t.volume)

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        tf = TF_MAP.get(timeframe.upper())
        if tf is None:
            return empty_rates()
        arr = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if arr is None or len(arr) == 0:
            return empty_rates()
        df = pd.DataFrame(arr)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
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
                time_open=datetime.fromtimestamp(p.time, tz=timezone.utc), magic=p.magic, comment=p.comment,
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
        ds = mt5.history_deals_get(start, end) or []
        out = []
        for d in ds:
            entry = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}.get(d.entry, "UNKNOWN")
            out.append(Deal(ticket=d.ticket, order=d.order, position_id=d.position_id, symbol=d.symbol,
                            side=Side.BUY if d.type == 0 else Side.SELL, volume=d.volume, price=d.price,
                            profit=d.profit, commission=d.commission, swap=d.swap,
                            time=datetime.fromtimestamp(d.time, tz=timezone.utc), magic=d.magic, entry=entry,
                            comment=d.comment))
        return out

    # ---------- ordres ----------
    def _build_request(self, req: OrderRequest) -> dict:
        tick = mt5.symbol_info_tick(req.symbol)
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

    def order_check(self, req: OrderRequest) -> OrderResult:
        r = self._build_request(req)
        return self._result(mt5.order_check(r), r)

    def order_send(self, req: OrderRequest) -> OrderResult:
        r = self._build_request(req)
        res = self._result(mt5.order_send(r), r)
        if res.ok and req.kind is OrderKind.MARKET:
            # retrouver le ticket de position (deal -> position_id)
            pos = [p for p in mt5.positions_get(symbol=req.symbol) or [] if p.magic == req.magic]
            if pos:
                res.ticket = sorted(pos, key=lambda p: p.time)[-1].ticket
        return res

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        p = mt5.positions_get(ticket=ticket)
        if not p:
            return OrderResult(ok=False, retcode=-2, comment="position introuvable")
        p = p[0]
        r = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": p.symbol, "sl": float(sl),
             "tp": float(tp or 0.0), "magic": p.magic}
        return self._result(mt5.order_send(r), r)

    def close_position(self, ticket: int, volume: Optional[float] = None, comment: str = "") -> OrderResult:
        p = mt5.positions_get(ticket=ticket)
        if not p:
            return OrderResult(ok=False, retcode=-2, comment="position introuvable")
        p = p[0]
        tick = mt5.symbol_info_tick(p.symbol)
        closing_buy = p.type == 1  # fermer un SELL = BUY
        r = {
            "action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": p.symbol,
            "volume": float(volume or p.volume), "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if closing_buy else tick.bid, "deviation": self.deviation, "magic": p.magic,
            "comment": (comment or "TLAB close")[:31], "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(p.symbol),
        }
        return self._result(mt5.order_send(r), r)

    def cancel_order(self, ticket: int) -> OrderResult:
        r = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        return self._result(mt5.order_send(r), r)

    def server_time(self) -> datetime:
        return datetime.now(timezone.utc)
