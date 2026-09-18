"""Broker simulé déterministe pour tests, backtests rapides et fonctionnement hors Windows.

- Séries M5 synthétiques (marche aléatoire à graine fixe, régimes alternés) resamplées vers M15/H1/H4/D1.
- Règles broker : volume min/max/step, stops_level, trade_allowed, symboles inconnus.
- Fill immédiat au ask/bid, SL/TP déclenchés sur les barres suivantes (advance_bars).
- Hooks de test : fail_next_order, drop_sl_on_fill, reject_modify, set_connected(False), trade_mode.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from ..core.types import (AccountInfo, Deal, OrderKind, OrderRequest, OrderResult, PendingOrder, Position, Side,
                          SymbolSpec, Tick, TradeMode)
from .adapter import RATES_COLUMNS, BrokerAdapter, empty_rates
from .symbols import asset_class_of, currencies_of, root_of

DEFAULT_SPECS: dict[str, dict] = {
    "EURUSD": dict(digits=5, point=1e-5, tick_value=1.0, contract=100000, price=1.0850, vol=0.00035, spread=10),
    "GBPUSD": dict(digits=5, point=1e-5, tick_value=1.0, contract=100000, price=1.2650, vol=0.00045, spread=14),
    "USDJPY": dict(digits=3, point=1e-3, tick_value=0.65, contract=100000, price=151.20, vol=0.045, spread=12),
    "USDCHF": dict(digits=5, point=1e-5, tick_value=1.1, contract=100000, price=0.8900, vol=0.00030, spread=14),
    "AUDUSD": dict(digits=5, point=1e-5, tick_value=1.0, contract=100000, price=0.6550, vol=0.00030, spread=12),
    "USDCAD": dict(digits=5, point=1e-5, tick_value=0.73, contract=100000, price=1.3600, vol=0.00032, spread=15),
    "NZDUSD": dict(digits=5, point=1e-5, tick_value=1.0, contract=100000, price=0.6050, vol=0.00030, spread=16),
    "EURGBP": dict(digits=5, point=1e-5, tick_value=1.26, contract=100000, price=0.8560, vol=0.00022, spread=14),
    "EURJPY": dict(digits=3, point=1e-3, tick_value=0.65, contract=100000, price=164.10, vol=0.055, spread=16),
    "GBPJPY": dict(digits=3, point=1e-3, tick_value=0.65, contract=100000, price=191.30, vol=0.075, spread=22),
    "XAUUSD": dict(digits=2, point=0.01, tick_value=1.0, contract=100, price=2320.0, vol=1.6, spread=25),
    "XAGUSD": dict(digits=3, point=0.001, tick_value=5.0, contract=5000, price=27.50, vol=0.035, spread=30),
    "US500": dict(digits=1, point=0.1, tick_value=1.0, contract=10, price=5200.0, vol=3.5, spread=6),
    "NAS100": dict(digits=1, point=0.1, tick_value=1.0, contract=10, price=18200.0, vol=14.0, spread=15),
    "GER40": dict(digits=1, point=0.1, tick_value=1.0, contract=10, price=18000.0, vol=11.0, spread=12),
    "USOIL": dict(digits=2, point=0.01, tick_value=1.0, contract=100, price=80.50, vol=0.09, spread=30),
}


def _make_series(seed: int, n: int, price: float, vol: float, start: datetime) -> pd.DataFrame:
    """Marche aléatoire M5 avec alternance de régimes (tendance / range / expansion) — reproductible."""
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    p = price
    drift = 0.0
    regime_len = 0
    vol_mult = 1.0
    for i in range(n):
        if regime_len <= 0:
            regime_len = int(rng.integers(120, 400))
            kind = rng.integers(0, 3)
            drift = float(rng.choice([-1, 1])) * vol * 0.12 if kind == 0 else 0.0
            vol_mult = 1.8 if kind == 2 else (0.6 if kind == 1 else 1.0)
        regime_len -= 1
        p += drift + rng.normal(0, vol * vol_mult)
        closes[i] = p
    opens = np.concatenate([[price], closes[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.6, n))
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, vol * 0.6, n))
    times = [start + timedelta(minutes=5 * i) for i in range(n)]
    df = pd.DataFrame({
        "time": pd.to_datetime(times, utc=True), "open": opens, "high": highs, "low": lows, "close": closes,
        "tick_volume": rng.integers(50, 500, n).astype(float), "spread": np.full(n, 10),
    })
    return df


class MockBroker(BrokerAdapter):
    name = "mock"

    def __init__(self, seed: int = 7, symbols: Optional[list[str]] = None, bars: int = 4000, balance: float = 100000.0,
                 currency: str = "EUR", trade_mode: TradeMode = TradeMode.DEMO, start: Optional[datetime] = None,
                 suffix: str = "", login: int = 5056132326, server: str = "MetaQuotes-Demo", live: bool = False):
        self.seed = seed
        self.live = live
        if live and start is None:
            # mode "paper temps réel" : la série se termine maintenant et avance toutes les 5 minutes
            from ..core.clock import bar_open_time
            from ..core.types import utcnow as _utcnow
            start = bar_open_time(_utcnow(), "M5") - timedelta(minutes=5 * (bars - 1))
        self.suffix = suffix
        self.symbol_names = symbols or list(DEFAULT_SPECS)
        self._start = start or (datetime(2026, 1, 5, tzinfo=timezone.utc))
        self.series: dict[str, pd.DataFrame] = {}
        self.specs: dict[str, SymbolSpec] = {}
        self._cursor: dict[str, int] = {}
        for i, s in enumerate(self.symbol_names):
            root = root_of(s)
            d = DEFAULT_SPECS.get(root, DEFAULT_SPECS["EURUSD"])
            name = root + suffix
            self.series[name] = _make_series(seed * 100 + i, bars, d["price"], d["vol"], self._start)
            self._cursor[name] = bars - 1
            base, quote = currencies_of(root)
            self.specs[name] = SymbolSpec(
                name=name, digits=d["digits"], point=d["point"], tick_size=d["point"], tick_value=d["tick_value"],
                contract_size=d["contract"], volume_min=0.01, volume_max=100.0, volume_step=0.01,
                stops_level_points=20, trade_allowed=True, currency_base=base, currency_profit=quote,
                currency_margin=base, asset_class=asset_class_of(root), spread_points=d["spread"], root=root,
            )
        self.balance = balance
        self.currency = currency
        self.trade_mode = trade_mode
        self.login = login
        self.server = server
        self._connected = False
        self._err = ""
        self._positions: dict[int, Position] = {}
        self._orders: dict[int, PendingOrder] = {}
        self._deals: list[Deal] = []
        self._ticket = 100000
        self._now_override: Optional[datetime] = None
        # hooks de test
        self.fail_next_order: Optional[int] = None
        self.drop_sl_on_fill = False
        self.reject_modify = False
        self.reject_close = False
        self.stale_ticks = False

    # ---------- horloge simulée ----------
    def now(self) -> datetime:
        if self._now_override:
            return self._now_override
        if self.live:
            from ..core.types import utcnow as _utcnow
            return _utcnow()
        first = next(iter(self.series))
        idx = self._cursor[first]
        return self.series[first]["time"].iloc[idx].to_pydatetime() + timedelta(minutes=5)

    def set_now(self, dt: Optional[datetime]) -> None:
        self._now_override = dt

    def server_time(self) -> datetime:
        return self.now()

    def advance_bars(self, n: int = 1) -> None:
        """Ajoute n barres M5 (génération à la volée) et déclenche SL/TP."""
        for name, df in self.series.items():
            spec = self.specs[name]
            d = DEFAULT_SPECS.get(spec.root, DEFAULT_SPECS["EURUSD"])
            last = df.iloc[-1]
            extra = _make_series(self.seed * 7919 + len(df), n, float(last["close"]), d["vol"],
                                 last["time"].to_pydatetime() + timedelta(minutes=5))
            self.series[name] = pd.concat([df, extra], ignore_index=True)
            self._cursor[name] = len(self.series[name]) - 1
            for bar in extra.itertuples():
                self._trigger_stops(name, float(bar.high), float(bar.low), bar.time.to_pydatetime())

    def set_price(self, symbol: str, price: float) -> None:
        """Force le dernier close (tests)."""
        df = self.series[symbol]
        i = self._cursor[symbol]
        df.loc[i, ["close"]] = price
        df.loc[i, "high"] = max(float(df.loc[i, "high"]), price)
        df.loc[i, "low"] = min(float(df.loc[i, "low"]), price)
        self._trigger_stops(symbol, price, price, self.now())

    # ---------- connexion ----------
    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def set_connected(self, v: bool) -> None:
        self._connected = v

    def is_connected(self) -> bool:
        return self._connected

    def last_error(self) -> str:
        return self._err

    # ---------- compte ----------
    def floating_pnl(self) -> float:
        # mode live : avancer les barres (et déclencher SL/TP) AVANT d'itérer, puis itérer sur une copie,
        # sinon `_close_at` supprime une entrée pendant l'itération → RuntimeError « dictionary changed size »
        self._maybe_advance()
        return sum(self._pnl(p) for p in list(self._positions.values()))

    def _pnl(self, p: Position) -> float:
        spec = self.specs[p.symbol]
        t = self.tick(p.symbol, force=True)
        px = t.bid if p.side is Side.BUY else t.ask
        ticks = (px - p.price_open) / spec.tick_size * p.side.sign
        return ticks * spec.tick_value * p.volume

    def account_info(self) -> Optional[AccountInfo]:
        if not self._connected:
            return None
        self._maybe_advance()
        eq = self.balance + self.floating_pnl()
        return AccountInfo(login=self.login, server=self.server, trade_mode=self.trade_mode, balance=self.balance,
                           equity=eq, margin=0.0, margin_free=eq, currency=self.currency, leverage=100,
                           name="Mock", hedging=True, connected=True)

    # ---------- symboles ----------
    def symbols(self) -> list[str]:
        return list(self.specs)

    def symbol_info(self, symbol: str) -> Optional[SymbolSpec]:
        return self.specs.get(symbol)

    def symbol_select(self, symbol: str) -> bool:
        return symbol in self.specs

    # ---------- données ----------
    def _maybe_advance(self) -> None:
        """Mode live : génère les barres M5 manquantes jusqu'à l'heure réelle."""
        if not self.live:
            return
        first = next(iter(self.series))
        last = self.series[first]["time"].iloc[-1].to_pydatetime()
        missing = int((self.now() - last).total_seconds() // 300)
        if missing >= 1:
            self.advance_bars(min(missing, 500))

    def tick(self, symbol: str, force: bool = False) -> Optional[Tick]:
        if not self._connected and not force:
            return None
        self._maybe_advance()
        df = self.series.get(symbol)
        if df is None:
            return None
        spec = self.specs[symbol]
        close = float(df["close"].iloc[self._cursor[symbol]])
        half = spec.spread_points * spec.point / 2
        t = self.now() - (timedelta(minutes=30) if self.stale_ticks else timedelta(seconds=1))
        return Tick(symbol=symbol, time=t, bid=round(close - half, spec.digits), ask=round(close + half, spec.digits))

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if not self._connected:
            return empty_rates()
        self._maybe_advance()
        df = self.series.get(symbol)
        if df is None:
            return empty_rates()
        df = df.iloc[: self._cursor[symbol] + 1]
        tf = timeframe.upper()
        if tf in ("M5", "M1"):
            return df.tail(count)[RATES_COLUMNS].reset_index(drop=True)
        rule = {"M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D", "W1": "1W"}[tf]
        g = df.set_index("time").resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum", "spread": "last"}
        ).dropna().reset_index()
        return g.tail(count)[RATES_COLUMNS].reset_index(drop=True)

    # ---------- positions ----------
    def positions(self, magic: Optional[int] = None) -> list[Position]:
        self._maybe_advance()
        out = []
        for p in list(self._positions.values()):
            if p.ticket not in self._positions:  # fermée par un stop déclenché pendant la mise à jour
                continue
            if magic is not None and p.magic != magic:
                continue
            p.price_current = self.tick(p.symbol, force=True).bid
            p.profit = self._pnl(p)
            out.append(p)
        return out

    def pending_orders(self, magic: Optional[int] = None) -> list[PendingOrder]:
        return [o for o in self._orders.values() if magic is None or o.magic == magic]

    def history_deals(self, start: datetime, end: datetime) -> list[Deal]:
        return [d for d in self._deals if start <= d.time <= end]

    # ---------- validation broker ----------
    def _validate(self, req: OrderRequest) -> Optional[str]:
        spec = self.specs.get(req.symbol)
        if spec is None:
            return "symbole inconnu"
        if not spec.trade_allowed:
            return "trade non autorisé"
        if req.volume < spec.volume_min or req.volume > spec.volume_max:
            return "volume hors bornes"
        steps = req.volume / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            return "volume non multiple du step"
        t = self.tick(req.symbol, force=True)
        ref = t.ask if req.side is Side.BUY else t.bid
        if req.kind is not OrderKind.MARKET:
            ref = req.price
        if req.sl and req.sl > 0:
            if req.side is Side.BUY and req.sl >= ref:
                return "SL invalide (côté)"
            if req.side is Side.SELL and req.sl <= ref:
                return "SL invalide (côté)"
            if abs(ref - req.sl) < spec.min_stop_distance:
                return "SL trop proche (stops_level)"
        if req.tp and req.tp > 0:
            if req.side is Side.BUY and req.tp <= ref:
                return "TP invalide (côté)"
            if req.side is Side.SELL and req.tp >= ref:
                return "TP invalide (côté)"
        return None

    def order_check(self, req: OrderRequest) -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, retcode=-1, comment="déconnecté", request=req.to_dict())
        err = self._validate(req)
        if err:
            return OrderResult(ok=False, retcode=10016, comment=err, request=req.to_dict())
        return OrderResult(ok=True, retcode=0, comment="check ok", request=req.to_dict())

    def order_send(self, req: OrderRequest) -> OrderResult:
        chk = self.order_check(req)
        if not chk.ok:
            return chk
        if self.fail_next_order is not None:
            code, self.fail_next_order = self.fail_next_order, None
            return OrderResult(ok=False, retcode=code, comment="rejet simulé", request=req.to_dict())
        self._ticket += 1
        t = self.tick(req.symbol, force=True)
        if req.kind is not OrderKind.MARKET:
            self._orders[self._ticket] = PendingOrder(ticket=self._ticket, symbol=req.symbol, side=req.side,
                                                      kind=req.kind, volume=req.volume, price=req.price, sl=req.sl,
                                                      tp=req.tp, magic=req.magic, comment=req.comment)
            return OrderResult(ok=True, retcode=10008, ticket=self._ticket, price=req.price, volume=req.volume,
                               comment="placed", request=req.to_dict())
        price = t.ask if req.side is Side.BUY else t.bid
        sl = 0.0 if self.drop_sl_on_fill else req.sl
        pos = Position(ticket=self._ticket, symbol=req.symbol, side=req.side, volume=req.volume, price_open=price,
                       sl=sl, tp=req.tp, profit=0.0, time_open=self.now(), magic=req.magic, comment=req.comment,
                       price_current=price)
        self._positions[self._ticket] = pos
        self._deals.append(Deal(ticket=self._ticket, order=self._ticket, position_id=self._ticket, symbol=req.symbol,
                                side=req.side, volume=req.volume, price=price, profit=0.0, commission=0.0, swap=0.0,
                                time=self.now(), magic=req.magic, entry="IN", comment=req.comment))
        return OrderResult(ok=True, retcode=10009, ticket=self._ticket, price=price, volume=req.volume, comment="done",
                           request=req.to_dict())

    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, retcode=-1, comment="déconnecté")
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(ok=False, retcode=-2, comment="position introuvable")
        if self.reject_modify:
            return OrderResult(ok=False, retcode=10016, comment="modify rejeté (simulé)")
        if not sl or sl <= 0:
            # défense en profondeur : l'adaptateur lui-même refuse de retirer un SL (never_remove_stop)
            return OrderResult(ok=False, retcode=-4, comment="refus : SL absent (never_remove_stop)")
        spec = self.specs[p.symbol]
        t = self.tick(p.symbol, force=True)
        ref = t.bid if p.side is Side.BUY else t.ask
        if sl and sl > 0:
            if (p.side is Side.BUY and sl >= ref) or (p.side is Side.SELL and sl <= ref):
                return OrderResult(ok=False, retcode=10016, comment="SL invalide (côté)")
            if abs(ref - sl) < spec.min_stop_distance:
                return OrderResult(ok=False, retcode=10016, comment="SL trop proche")
        p.sl = float(sl)
        p.tp = float(tp or 0.0)
        return OrderResult(ok=True, retcode=10009, ticket=ticket, comment="modified")

    def _close_at(self, p: Position, price: float, volume: float, reason: str) -> OrderResult:
        spec = self.specs[p.symbol]
        ticks = (price - p.price_open) / spec.tick_size * p.side.sign
        pnl = ticks * spec.tick_value * volume
        self.balance += pnl
        self._deals.append(Deal(ticket=self._ticket + 1, order=self._ticket + 1, position_id=p.ticket, symbol=p.symbol,
                                side=Side.SELL if p.side is Side.BUY else Side.BUY, volume=volume, price=price,
                                profit=pnl, commission=0.0, swap=0.0, time=self.now(), magic=p.magic, entry="OUT",
                                comment=reason))
        self._ticket += 1
        if volume >= p.volume - 1e-9:
            del self._positions[p.ticket]
        else:
            p.volume = round(p.volume - volume, 2)
        return OrderResult(ok=True, retcode=10009, ticket=p.ticket, price=price, volume=volume, comment=reason)

    def close_position(self, ticket: int, volume: Optional[float] = None, comment: str = "") -> OrderResult:
        if not self._connected:
            return OrderResult(ok=False, retcode=-1, comment="déconnecté")
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(ok=False, retcode=-2, comment="position introuvable")
        if self.reject_close:
            return OrderResult(ok=False, retcode=10016, comment="close rejeté (simulé)")
        vol = min(volume or p.volume, p.volume)
        t = self.tick(p.symbol, force=True)
        price = t.bid if p.side is Side.BUY else t.ask
        return self._close_at(p, price, vol, comment or "close")

    def cancel_order(self, ticket: int) -> OrderResult:
        if ticket in self._orders:
            del self._orders[ticket]
            return OrderResult(ok=True, retcode=10009, ticket=ticket, comment="cancelled")
        return OrderResult(ok=False, retcode=-2, comment="ordre introuvable")

    def _trigger_stops(self, symbol: str, high: float, low: float, when: datetime) -> None:
        for p in list(self._positions.values()):
            if p.symbol != symbol:
                continue
            if p.side is Side.BUY:
                if p.sl and low <= p.sl:
                    self._close_at(p, p.sl, p.volume, "sl")
                elif p.tp and high >= p.tp:
                    self._close_at(p, p.tp, p.volume, "tp")
            else:
                if p.sl and high >= p.sl:
                    self._close_at(p, p.sl, p.volume, "sl")
                elif p.tp and low <= p.tp:
                    self._close_at(p, p.tp, p.volume, "tp")


def make_broker(kind: str, settings=None, **kw) -> BrokerAdapter:
    """Fabrique : 'mt5' (Windows) ou 'mock'."""
    if kind == "mt5":
        from .mt5_adapter import MT5Adapter, MT5_AVAILABLE
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 indisponible sur cette plateforme : utiliser TRADINGLAB_BROKER=mock")
        rules = settings.markets.get("asset_class_rules", {}) if settings else {}
        return MT5Adapter(asset_rules=rules, deviation=int(settings.execution.get("slippage_deviation_points", 20)) if settings else 20)
    if settings is not None:
        # décalage serveur explicite (heures) pour l'alignement H4/D1/W1 de `clock.bar_open_time`
        off_h = (settings.system or {}).get("server_utc_offset_hours")
        if off_h is not None:
            from ..core.clock import set_server_utc_offset
            set_server_utc_offset(float(off_h) * 3600)
        # le broker simulé se présente comme le compte DEMO attendu par la configuration :
        # une session « papier » n'est ainsi pas bloquée par la garde ACCOUNT_MISMATCH
        exp = settings.get("account_expected", {}) or {}
        if exp.get("login"):
            kw.setdefault("login", int(exp["login"]))
        if exp.get("server"):
            kw.setdefault("server", str(exp["server"]))
    kw.setdefault("live", True)
    return MockBroker(**kw)
