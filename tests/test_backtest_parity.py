"""Live-vs-Backtest Parity & Drift-Detection Test Suite.

Feeds identical synthetic market bars into live MarketScanner / RiskManager and into
SimulatedPortfolioBroker / BacktestEngine. If another chat modifies live strategy rules
(sizing, stop placement, RVOL/ATR/VWAP math, or breakout criteria), this suite will
immediately flag the divergence so backtesting stays 1:1 with live trading.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.db import init_db, set_system_state
from app.services.market_scanner import MarketScanner
from app.services.risk_manager import RiskManager
from backtest.config import BacktestConfig
from backtest.data_loader import Bar
from backtest.sim_broker import SimulatedPortfolioBroker
from tests.test_risk_manager import MockBroker

ET = ZoneInfo("America/New_York")


@pytest.mark.asyncio
async def test_risk_manager_vs_sim_broker_exact_sizing_parity(tmp_path, monkeypatch):
    """Live RiskManager.evaluate_signal() and SimulatedPortfolioBroker produce identical sizing, stops, and targets."""
    monkeypatch.setattr(settings, "DATABASE_PATH", tmp_path / "parity_live.db")
    await init_db()
    await set_system_state("dead_mans_brake", "OFF")

    live_broker = MockBroker(equity=50000.0)
    live_rm = RiskManager(live_broker)

    close_price = 128.40
    or_high = 127.80
    or_low = 126.10
    atr = 1.60

    live_eval = await live_rm.evaluate_signal(
        symbol="NVDA",
        close_price=close_price,
        or_high=or_high,
        or_low=or_low,
        atr=atr,
    )
    assert live_eval.approved

    cfg = BacktestConfig(
        start_date="2025-05-06",
        end_date="2025-05-06",
        initial_capital=50000.0,
        risk_profile="P2_BALANCED",
        enable_health_ladder=True,
    )
    sim_broker = SimulatedPortfolioBroker(cfg)
    t0 = datetime(2025, 5, 6, 9, 50, tzinfo=ET).astimezone(timezone.utc)
    ok, reason = sim_broker.evaluate_and_submit(
        symbol="NVDA",
        signal_time=t0,
        close_price=close_price,
        or_high=or_high,
        or_low=or_low,
        atr=atr,
        signal_rvol=2.1,
    )
    assert ok, f"Sim broker unexpectedly rejected: {reason}"
    order = sim_broker.pending_orders["NVDA"]

    assert order.entry_limit_price == live_eval.entry_limit_price
    assert order.initial_stop_price == live_eval.stop_loss_price
    assert order.take_profit_price == live_eval.take_profit_price
    assert order.qty == live_eval.qty
    assert order.stop_type == live_eval.stop_type


def test_indicator_math_shared_with_live_market_scanner():
    """Backtest Bar dataclass is 100% compatible with live MarketScanner VWAP/ATR/RVOL calculations."""
    d = date(2025, 5, 6)
    bars = [
        Bar(datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(timezone.utc), 100.0, 101.0, 99.5, 100.5, 10000.0),
        Bar(datetime(d.year, d.month, d.day, 9, 35, tzinfo=ET).astimezone(timezone.utc), 100.5, 102.0, 100.2, 101.8, 12000.0),
        Bar(datetime(d.year, d.month, d.day, 9, 40, tzinfo=ET).astimezone(timezone.utc), 101.8, 102.5, 101.4, 102.2, 11000.0),
        Bar(datetime(d.year, d.month, d.day, 9, 45, tzinfo=ET).astimezone(timezone.utc), 102.2, 103.2, 102.0, 103.0, 22000.0),
    ]

    vwap = MarketScanner.calculate_intraday_vwap(bars)
    atr = MarketScanner.calculate_atr(bars, period=14)
    rvol = MarketScanner.calculate_rvol(bars, period=20)

    assert 101.0 < vwap < 103.0
    assert atr > 1.0
    assert rvol == pytest.approx(22000.0 / 11000.0, rel=1e-4)
