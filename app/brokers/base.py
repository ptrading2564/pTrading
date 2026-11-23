"""Abstract base class for Broker Adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class PositionInfo:
    symbol: str
    qty: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    market_value: float


@dataclass
class OrderInfo:
    order_id: str
    client_order_id: str
    symbol: str
    status: str
    side: str
    qty: int
    filled_qty: int
    limit_price: Optional[float]
    stop_price: Optional[float]
    created_at: datetime
    filled_at: Optional[datetime]
    legs: Optional[List["OrderInfo"]] = None


class BrokerAdapter(ABC):
    """Abstract interface for multi-broker abstraction (Alpaca, IBKR, etc.)."""

    @abstractmethod
    async def get_account_equity(self) -> float:
        """Fetch live account equity."""
        pass

    @abstractmethod
    async def get_positions(self) -> List[PositionInfo]:
        """Fetch all open positions."""
        pass

    @abstractmethod
    async def get_open_orders(self) -> List[OrderInfo]:
        """Fetch all pending open orders."""
        pass

    @abstractmethod
    async def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        client_order_id: str,
    ) -> Dict[str, Any]:
        """Submits a marketable limit entry order with attached bracket Stop-Loss and Take-Profit legs."""
        pass

    @abstractmethod
    async def replace_stop_loss(self, order_id: str, new_stop_price: float) -> bool:
        """Modifies existing stop-loss leg to a new price (e.g. breakeven)."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancels a pending order by ID."""
        pass

    @abstractmethod
    async def close_all_positions(self) -> None:
        """Emergency or EOD flatten: closes all open positions and cancels all open orders."""
        pass

    @abstractmethod
    async def is_market_open(self) -> bool:
        """Checks if US equity market is currently open for regular trading hours."""
        pass

    @abstractmethod
    async def get_market_close_time(self) -> datetime:
        """Returns the market close time for today in Eastern Time (handles early closes)."""
        pass
