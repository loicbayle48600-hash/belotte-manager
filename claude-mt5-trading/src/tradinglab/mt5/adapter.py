"""Interface broker abstraite. Tout accès MT5 passe par cette interface.

IMPORTANT : aucun agent LLM n'a accès à cet objet. Seuls l'Execution Gate,
le Position Manager et le Watchdog l'utilisent, dans du code déterministe.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd

from ..core.types import AccountInfo, Deal, OrderRequest, OrderResult, PendingOrder, Position, SymbolSpec, Tick

RATES_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread"]


class BrokerAdapter(ABC):
    name: str = "abstract"

    # --- connexion ---
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def last_error(self) -> str: ...

    # --- compte / symboles ---
    @abstractmethod
    def account_info(self) -> Optional[AccountInfo]: ...

    @abstractmethod
    def symbols(self) -> list[str]: ...

    @abstractmethod
    def symbol_info(self, symbol: str) -> Optional[SymbolSpec]: ...

    @abstractmethod
    def symbol_select(self, symbol: str) -> bool: ...

    # --- données ---
    @abstractmethod
    def tick(self, symbol: str) -> Optional[Tick]: ...

    @abstractmethod
    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...

    # --- positions / ordres ---
    @abstractmethod
    def positions(self, magic: Optional[int] = None) -> list[Position]: ...

    @abstractmethod
    def pending_orders(self, magic: Optional[int] = None) -> list[PendingOrder]: ...

    @abstractmethod
    def history_deals(self, start: datetime, end: datetime) -> list[Deal]: ...

    @abstractmethod
    def order_check(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def order_send(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def modify_position(self, ticket: int, sl: float, tp: float) -> OrderResult:
        """Modifie SL/TP d'une position. Règle « never_remove_stop » : `sl` doit être > 0 ; une
        implémentation DOIT refuser (`OrderResult(ok=False, retcode=-4)`) toute demande qui retirerait le SL.
        Ne lève jamais : toute erreur est rendue sous forme d'`OrderResult(ok=False)`."""
        ...

    @abstractmethod
    def close_position(self, ticket: int, volume: Optional[float] = None, comment: str = "") -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, ticket: int) -> OrderResult: ...

    @abstractmethod
    def server_time(self) -> datetime: ...

    # --- utilitaire ---
    def position(self, ticket: int) -> Optional[Position]:
        for p in self.positions():
            if p.ticket == ticket:
                return p
        return None


def empty_rates() -> pd.DataFrame:
    return pd.DataFrame(columns=RATES_COLUMNS)
