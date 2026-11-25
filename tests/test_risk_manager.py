"""Tests for dynamic compounding risk engine, sizing, position limits, and circuit breaker."""

from unittest.mock import AsyncMock
import pytest
from app.brokers.base import BrokerAdapter, OrderInfo, PositionInfo
from app.config import settings
from app.db import init_db
from app.services.risk_manager import RiskManager


class MockBroker(BrokerAdapter):
    def __init__(self, equity: float = 50000.0) -> None:
        self.equity = equity
        self.positions = []
        self.orders = []

    async def get_account_equity(self) -> float:
        return self.equity

    async def get_positions(self):
        return self.positions

    async def get_open_orders(self):
        return self.orders

    async def submit_bracket_order(self, *args, **kwargs):
        return {"order_id": "mock_id", "status": "accepted"}

    async def replace_stop_loss(self, *args, **kwargs):
        return True

    async def cancel_order(self, *args, **kwargs):
        return True

    async def close_all_positions(self):
        self.positions.clear()
        self.orders.clear()

    async def is_market_open(self):
        return True

    async def get_market_close_time(self):
        from datetime import datetime, time
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.MARKET_TIMEZONE)
        return datetime.combine(datetime.now(tz).date(), time(16, 0), tzinfo=tz)


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


@pytest.mark.asyncio
async def test_compounding_position_sizing_standard():
    mock_broker = MockBroker(equity=50000.0)
    risk_manager = RiskManager(mock_broker)

    # Signal: Close = 100.0, OR_High = 99.5, OR_Low = 98.0, ATR = 1.0
    # Stop = max(98.0, 100 - 1.5*1.0 = 98.5) -> 98.50
    # Entry limit = 100.0 * 1.0005 = 100.05
    # Stop distance = 100.05 - 98.50 = 1.55
    # Risk = 50000 * 0.005 = $250
    # Shares = floor(250 / 1.55) = 161 shares
    eval_res = await risk_manager.evaluate_signal(
        symbol="TEST",
        close_price=100.0,
        or_high=99.5,
        or_low=98.0,
        atr=1.0,
    )

    assert eval_res.approved is True
    assert eval_res.qty == 161
    assert eval_res.stop_loss_price == 98.50
    assert eval_res.take_profit_price == round(100.05 + 2.0 * 1.55, 2)
    assert eval_res.risk_dollars == 250.0


@pytest.mark.asyncio
async def test_max_equity_allocation_cap():
    mock_broker = MockBroker(equity=50000.0)
    risk_manager = RiskManager(mock_broker)

    # Tight stop on high-priced stock:
    # Close = 500.0, OR_Low = 499.0, ATR = 0.50
    # Entry limit = 500.25, Stop = 499.50 -> stop distance = 0.75
    # Raw shares = floor(250 / 0.75) = 333 shares ($166,582 capital -> 333% of equity!)
    # Max 25% single stock cap = 50000 * 0.25 = $12,500
    # Max shares = floor(12500 / 500.25) = 24 shares
    eval_res = await risk_manager.evaluate_signal(
        symbol="EXPENSIVE",
        close_price=500.0,
        or_high=499.8,
        or_low=499.0,
        atr=0.50,
    )

    assert eval_res.approved is True
    assert eval_res.qty == 24  # Capped at 25% single-stock capital limit


@pytest.mark.asyncio
async def test_max_concurrent_positions_cap():
    mock_broker = MockBroker(equity=50000.0)
    # Simulate 4 existing positions
    mock_broker.positions = [
        PositionInfo("SYM1", 10, 100.0, 101.0, 10.0, 1010.0),
        PositionInfo("SYM2", 10, 100.0, 101.0, 10.0, 1010.0),
        PositionInfo("SYM3", 10, 100.0, 101.0, 10.0, 1010.0),
        PositionInfo("SYM4", 10, 100.0, 101.0, 10.0, 1010.0),
    ]
    risk_manager = RiskManager(mock_broker)

    eval_res = await risk_manager.evaluate_signal(
        symbol="NEW_STOCK",
        close_price=50.0,
        or_high=49.0,
        or_low=48.0,
        atr=1.0,
    )

    assert eval_res.approved is False
    assert "Max concurrent positions cap reached" in eval_res.rejection_reason


@pytest.mark.asyncio
async def test_daily_circuit_breaker():
    mock_broker = MockBroker(equity=50000.0)
    risk_manager = RiskManager(mock_broker)

    # Initialize daily starting equity at $50,000
    await risk_manager.get_or_initialize_daily_stats(50000.0)

    # Now simulate catastrophic intraday drawdown: equity drops to $48,900 (-2.2% drawdown)
    mock_broker.equity = 48900.0

    eval_res = await risk_manager.evaluate_signal(
        symbol="NEW_STOCK",
        close_price=50.0,
        or_high=49.0,
        or_low=48.0,
        atr=1.0,
    )

    assert eval_res.approved is False
    assert "Daily circuit breaker tripped" in eval_res.rejection_reason


@pytest.mark.asyncio
async def test_breakeven_slot_recycling_allows_fifth_position():
    from app.db import get_connection

    mock_broker = MockBroker(equity=100000.0)
    mock_broker.positions = [
        PositionInfo("SYM1", 10, 100.0, 103.0, 30.0, 1030.0),
        PositionInfo("SYM2", 10, 100.0, 101.0, 10.0, 1010.0),
        PositionInfo("SYM3", 10, 100.0, 101.0, 10.0, 1010.0),
        PositionInfo("SYM4", 10, 100.0, 101.0, 10.0, 1010.0),
    ]
    # Mark SYM1 as already stepped to Breakeven (+1.0R risk-free)
    async with get_connection() as conn:
        await conn.execute("DELETE FROM trades;")
        await conn.execute(
            """
            INSERT INTO trades (client_order_id, symbol, qty, entry_price, stop_loss_price, take_profit_price, is_open, breakeven_stepped)
            VALUES ('pt_be_1', 'SYM1', 10, 100.0, 100.02, 104.0, 1, 1);
            """
        )
        await conn.commit()

    risk_manager = RiskManager(mock_broker)
    eval_res = await risk_manager.evaluate_signal(
        symbol="NEW_FIFTH",
        close_price=50.0,
        or_high=49.0,
        or_low=48.0,
        atr=1.0,
    )
    assert eval_res.approved is True
    assert eval_res.qty > 0


@pytest.mark.asyncio
async def test_breakeven_slot_recycling_respects_hard_cap_6():
    from app.db import get_connection

    mock_broker = MockBroker(equity=100000.0)
    mock_broker.positions = [
        PositionInfo(f"SYM{i}", 10, 100.0, 103.0, 30.0, 1030.0) for i in range(1, 7)
    ]
    async with get_connection() as conn:
        await conn.execute("DELETE FROM trades;")
        for i in range(1, 5):
            await conn.execute(
                """
                INSERT INTO trades (client_order_id, symbol, qty, entry_price, stop_loss_price, take_profit_price, is_open, breakeven_stepped)
                VALUES (?, ?, 10, 100.0, 100.02, 104.0, 1, 1);
                """,
                (f"pt_be_{i}", f"SYM{i}"),
            )
        await conn.commit()

    risk_manager = RiskManager(mock_broker)
    eval_res = await risk_manager.evaluate_signal(
        symbol="NEW_SEVENTH",
        close_price=50.0,
        or_high=49.0,
        or_low=48.0,
        atr=1.0,
    )
    assert eval_res.approved is False
    assert "Max concurrent total positions cap reached" in eval_res.rejection_reason
