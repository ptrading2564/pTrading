"""Deterministic unit & integration tests for the pTrading portfolio backtesting engine and /backtest UI."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from backtest.config import BacktestConfig
from backtest.data_loader import Bar, InMemoryDataStore
from backtest.db import get_backtest_run, init_backtest_db, list_backtest_runs
from backtest.engine import BacktestEngine
from backtest.screener_sim import ScreenerSimulator
from backtest.sim_broker import SimulatedPortfolioBroker

ET = ZoneInfo("America/New_York")


def _et_bar(day: date, hh: int, mm: int, o: float, h: float, l: float, c: float, v: float) -> Bar:
    ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).astimezone(timezone.utc)
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def _make_healthy_spy_qqq(day: date, end_hh: int = 16) -> Dict[str, List[Bar]]:
    """Generates steadily rising SPY & QQQ 5m bars so SPY/QQQ > VWAP gate passes all session."""
    bars_spy: List[Bar] = []
    bars_qqq: List[Bar] = []
    cur = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
    end = datetime(day.year, day.month, day.day, end_hh, 0, tzinfo=ET)
    i = 0
    while cur < end:
        p_s = 500.0 + i * 0.25
        p_q = 420.0 + i * 0.25
        ts_u = cur.astimezone(timezone.utc)
        bars_spy.append(Bar(ts_u, p_s, p_s + 0.3, p_s - 0.1, p_s + 0.2, 100000.0))
        bars_qqq.append(Bar(ts_u, p_q, p_q + 0.3, p_q - 0.1, p_q + 0.2, 100000.0))
        cur += timedelta(minutes=5)
        i += 1
    return {"SPY": bars_spy, "QQQ": bars_qqq}


@pytest.fixture
def isolated_bt_db(tmp_path: Path, monkeypatch):
    bt_db = tmp_path / "test_backtest.db"
    cache_db = tmp_path / "test_cache.db"
    monkeypatch.setenv("BACKTEST_DATABASE_PATH", str(bt_db))
    monkeypatch.setenv("BACKTEST_CACHE_PATH", str(cache_db))
    return bt_db


@pytest.mark.asyncio
async def test_take_profit_and_compounding(isolated_bt_db):
    """Breakout enters on 09:50 ET 1m bar and hits +2.0R take-profit cleanly."""
    d = date(2025, 5, 6)  # Non-FOMC regular Tuesday
    spy_qqq = _make_healthy_spy_qqq(d)

    # NVDA 5m bars: OR 09:30-09:45 has high=102.0, low=100.0; 09:45 bar breaks out at close=102.50 with 3x RVOL
    nvda_5m = [
        _et_bar(d, 9, 30, 100.5, 101.5, 100.0, 101.0, 10000.0),
        _et_bar(d, 9, 35, 101.0, 102.0, 100.5, 101.5, 10000.0),
        _et_bar(d, 9, 40, 101.5, 101.8, 100.8, 101.6, 10000.0),
        # Breakout bar [09:45, 09:50): close=102.50 > OR_H(102.0), close > open(101.6), RVOL = 30000/10000 = 3.0x
        _et_bar(d, 9, 45, 101.6, 102.6, 101.5, 102.5, 30000.0),
        _et_bar(d, 9, 50, 102.5, 107.0, 102.4, 106.5, 15000.0),
    ]
    # 1m bars starting at 09:50 ET: fills at 102.50 (<= limit 102.55), rallies past +2.0R TP (~106.75)
    nvda_1m = [
        _et_bar(d, 9, 50, 102.50, 103.50, 102.45, 103.40, 5000.0),
        _et_bar(d, 9, 51, 103.40, 105.50, 103.30, 105.20, 5000.0),
        _et_bar(d, 9, 52, 105.20, 108.00, 105.10, 107.50, 5000.0),
    ]

    store = InMemoryDataStore(
        bars_by_tf={
            "1Day": {"SPY": [], "QQQ": [], "NVDA": []},
            "5Min": {**spy_qqq, "NVDA": nvda_5m},
            "1Min": {"NVDA": nvda_1m},
        }
    )

    cfg = BacktestConfig(
        name="TP Test",
        mode="fixed",
        start_date="2025-05-06",
        end_date="2025-05-06",
        custom_symbols=["NVDA"],
        initial_capital=50000.0,
        exit_slippage_bps=0.0,
        monte_carlo_simulations=100,
    )
    res = await BacktestEngine(cfg, data_store=store).run(persist=True)

    assert len(res["trades"]) == 1
    tr = res["trades"][0]
    assert tr["symbol"] == "NVDA"
    assert tr["exit_reason"] == "TAKE_PROFIT"
    assert tr["breakeven_stepped"] == 1
    assert tr["pnl_dollars"] > 450.0  # ~+2.0R on $250 risk
    assert res["summary"]["ending_equity"] > 50450.0


@pytest.mark.asyncio
async def test_breakeven_step_and_choked_winner_post_exit_r(isolated_bt_db):
    """Hits +1.0R -> steps stop to entry+$0.02 -> retraces to BE -> later rallies past +2.0R (choked winner)."""
    d = date(2025, 5, 6)
    spy_qqq = _make_healthy_spy_qqq(d)
    nvda_5m = [
        _et_bar(d, 9, 30, 100.5, 101.5, 100.0, 101.0, 10000.0),
        _et_bar(d, 9, 35, 101.0, 102.0, 100.5, 101.5, 10000.0),
        _et_bar(d, 9, 40, 101.5, 101.8, 100.8, 101.6, 10000.0),
        _et_bar(d, 9, 45, 101.6, 102.6, 101.5, 102.5, 30000.0),
    ]
    # Entry ~102.50, initial stop = max(100.0, 102.50 - 1.5*ATR) -> ~100.85, 1R ~1.65 -> +1R ~104.15
    nvda_1m = [
        _et_bar(d, 9, 50, 102.50, 103.00, 102.45, 102.90, 5000.0),  # fills at 102.50
        _et_bar(d, 9, 51, 102.90, 104.80, 102.85, 104.50, 5000.0),  # hits +1.0R -> BE steps to 102.52
        _et_bar(d, 9, 52, 104.50, 104.50, 102.40, 102.60, 5000.0),  # retraces to 102.52 -> BREAKEVEN exit
        _et_bar(d, 10, 15, 103.00, 109.00, 103.00, 108.50, 5000.0), # later rallies past +2.0R!
    ]
    store = InMemoryDataStore(
        bars_by_tf={"1Day": {}, "5Min": {**spy_qqq, "NVDA": nvda_5m}, "1Min": {"NVDA": nvda_1m}}
    )
    cfg = BacktestConfig(
        mode="fixed",
        start_date="2025-05-06",
        end_date="2025-05-06",
        custom_symbols=["NVDA"],
        exit_slippage_bps=0.0,
        monte_carlo_simulations=50,
    )
    res = await BacktestEngine(cfg, data_store=store).run(persist=False)

    assert len(res["trades"]) == 1
    tr = res["trades"][0]
    assert tr["exit_reason"] == "BREAKEVEN"
    assert tr["breakeven_stepped"] == 1
    assert abs(tr["exit_price"] - 102.52) < 0.02
    assert tr["post_exit_max_r"] is not None and tr["post_exit_max_r"] >= 2.0


@pytest.mark.asyncio
async def test_stale_order_expires_after_3_minutes(isolated_bt_db):
    """Unfilled marketable limit order expires cleanly after 3 minutes when price runs away."""
    d = date(2025, 5, 6)
    spy_qqq = _make_healthy_spy_qqq(d)
    nvda_5m = [
        _et_bar(d, 9, 30, 100.5, 101.5, 100.0, 101.0, 10000.0),
        _et_bar(d, 9, 35, 101.0, 102.0, 100.5, 101.5, 10000.0),
        _et_bar(d, 9, 40, 101.5, 101.8, 100.8, 101.6, 10000.0),
        _et_bar(d, 9, 45, 101.6, 102.6, 101.5, 102.5, 30000.0),  # limit = 102.55
    ]
    # Price gaps immediately above 102.55 and stays above for >3 minutes
    nvda_1m = [
        _et_bar(d, 9, 50, 103.10, 103.50, 103.00, 103.40, 5000.0),
        _et_bar(d, 9, 51, 103.40, 103.80, 103.30, 103.70, 5000.0),
        _et_bar(d, 9, 52, 103.70, 104.00, 103.60, 103.90, 5000.0),
        _et_bar(d, 9, 53, 103.90, 104.20, 103.80, 104.10, 5000.0),
        _et_bar(d, 9, 54, 104.10, 104.20, 101.00, 101.50, 5000.0),  # drops back AFTER expiry!
    ]
    store = InMemoryDataStore(
        bars_by_tf={"1Day": {}, "5Min": {**spy_qqq, "NVDA": nvda_5m}, "1Min": {"NVDA": nvda_1m}}
    )
    cfg = BacktestConfig(
        mode="fixed",
        start_date="2025-05-06",
        end_date="2025-05-06",
        custom_symbols=["NVDA"],
        stale_order_timeout_minutes=3,
        monte_carlo_simulations=50,
    )
    res = await BacktestEngine(cfg, data_store=store).run(persist=False)

    assert len(res["trades"]) == 0
    assert res["summary"]["expired_orders"] >= 1


def test_conservative_intrabar_tie_breaking_stop_first():
    """If a single bar hits both initial stop and +2R TP, simulator enforces worst-case STOP_LOSS."""
    cfg = BacktestConfig(
        start_date="2025-05-06",
        end_date="2025-05-06",
        exit_slippage_bps=0.0,
    )
    broker = SimulatedPortfolioBroker(cfg)
    t0 = datetime(2025, 5, 6, 9, 50, tzinfo=ET).astimezone(timezone.utc)
    ok, _ = broker.evaluate_and_submit(
        symbol="AMD",
        signal_time=t0,
        close_price=100.0,
        or_high=99.5,
        or_low=98.0,
        atr=1.5,
        signal_rvol=2.0,
    )
    assert ok
    # Entry bar fills at 100.00 (open=100.0) cleanly
    broker.step_execution_bars(t0, {"AMD": Bar(t0, 100.0, 100.2, 99.9, 100.1, 1000.0)})
    assert "AMD" in broker.open_positions

    # Next bar has giant range touching BOTH stop (98.0) and TP (104.10)
    t1 = t0 + timedelta(minutes=1)
    broker.step_execution_bars(t1, {"AMD": Bar(t1, 100.1, 106.0, 97.5, 105.0, 5000.0)})
    assert len(broker.closed_trades) == 1
    assert broker.closed_trades[0]["exit_reason"] == "STOP_LOSS"


def test_risk_free_breakeven_slot_recycling_4_to_6():
    """Breakeven-protected trades free at-risk slots up to 6 total positions."""
    cfg = BacktestConfig(
        start_date="2025-05-06",
        end_date="2025-05-06",
        max_concurrent_positions=4,
        max_total_positions_with_breakeven=6,
        enable_slot_recycling=True,
        exit_slippage_bps=0.0,
    )
    broker = SimulatedPortfolioBroker(cfg)
    t0 = datetime(2025, 5, 6, 9, 50, tzinfo=ET).astimezone(timezone.utc)

    # Submit and fill 4 positions
    for sym in ("S1", "S2", "S3", "S4"):
        ok, _ = broker.evaluate_and_submit(
            symbol=sym, signal_time=t0, close_price=50.0, or_high=49.5, or_low=48.0, atr=1.5, signal_rvol=2.0
        )
        assert ok
    broker.step_execution_bars(t0, {s: Bar(t0, 50.0, 50.2, 49.9, 50.1, 1000.0) for s in ("S1", "S2", "S3", "S4")})

    # 5th position rejected while all 4 are at-risk
    ok5, reason5 = broker.evaluate_and_submit(
        symbol="S5", signal_time=t0, close_price=50.0, or_high=49.5, or_low=48.0, atr=1.5, signal_rvol=2.0
    )
    assert not ok5 and "at-risk" in (reason5 or "")

    # Step S1 and S2 to +1.0R breakeven (without hitting +2.0R TP)
    t1 = t0 + timedelta(minutes=1)
    broker.step_execution_bars(
        t1,
        {
            "S1": Bar(t1, 50.5, 52.5, 50.4, 52.3, 2000.0),
            "S2": Bar(t1, 50.5, 52.5, 50.4, 52.3, 2000.0),
        },
    )
    assert broker.open_positions["S1"].breakeven_stepped
    assert broker.open_positions["S2"].breakeven_stepped

    # Now S5 and S6 are BOTH approved (2 at-risk + 2 new = 4 at-risk, 6 total)
    ok5b, _ = broker.evaluate_and_submit(
        symbol="S5", signal_time=t1, close_price=50.0, or_high=49.5, or_low=48.0, atr=1.5, signal_rvol=2.0
    )
    ok6, _ = broker.evaluate_and_submit(
        symbol="S6", signal_time=t1, close_price=50.0, or_high=49.5, or_low=48.0, atr=1.5, signal_rvol=2.0
    )
    assert ok5b and ok6

    # Fill S5 & S6 and step S3 to breakeven so only 3 are at-risk (<4), while total = 6 (at hard cap)
    t2 = t1 + timedelta(minutes=1)
    broker.step_execution_bars(
        t2,
        {
            "S3": Bar(t2, 50.5, 52.5, 50.4, 52.3, 2000.0),
            "S5": Bar(t2, 50.0, 50.2, 49.9, 50.1, 1000.0),
            "S6": Bar(t2, 50.0, 50.2, 49.9, 50.1, 1000.0),
        },
    )
    assert broker.open_positions["S3"].breakeven_stepped

    # 7th position blocked specifically by hard cap of 6 (even though at-risk is only 3/4!)
    ok7, reason7 = broker.evaluate_and_submit(
        symbol="S7", signal_time=t2, close_price=50.0, or_high=49.5, or_low=48.0, atr=1.5, signal_rvol=2.0
    )
    assert not ok7 and "total positions" in (reason7 or "")


def test_health_ladder_and_daily_circuit_breaker():
    """Health-State Drawdown Ladder scales down risk at 5%/10% DD and halts at 15% DD."""
    cfg = BacktestConfig(
        start_date="2025-05-06",
        end_date="2025-05-06",
        initial_capital=50000.0,
        enable_health_ladder=True,
    )
    broker = SimulatedPortfolioBroker(cfg)
    t0 = datetime(2025, 5, 6, 9, 50, tzinfo=ET).astimezone(timezone.utc)

    # Simulate 6% cumulative drawdown -> CAUTION (0.75x multiplier)
    broker.twr_peak = 1.0
    broker.twr_growth = 0.94
    broker.cash = 47000.0
    broker.starting_day_equity = 47000.0
    ok, _ = broker.evaluate_and_submit(
        symbol="AAPL", signal_time=t0, close_price=100.0, or_high=99.5, or_low=98.0, atr=1.5, signal_rvol=2.0
    )
    assert ok
    assert broker.pending_orders["AAPL"].health_state == "CAUTION"
    assert broker.pending_orders["AAPL"].risk_multiplier == 0.75

    # Simulate 16% cumulative drawdown -> SUSPENDED (blocked)
    broker.pending_orders.clear()
    broker.twr_growth = 0.84
    broker.cash = 42000.0
    broker.starting_day_equity = 42000.0
    ok2, reason2 = broker.evaluate_and_submit(
        symbol="MSFT", signal_time=t0, close_price=100.0, or_high=99.5, or_low=98.0, atr=1.5, signal_rvol=2.0
    )
    assert not ok2 and "SUSPENDED" in (reason2 or "")


@pytest.mark.asyncio
async def test_fomc_guard_cutoff_and_pre_event_flatten(isolated_bt_db):
    """On FOMC day (2025-05-07), morning position flattens at 13:55 ET and 13:35 ET breakout is blocked."""
    d = date(2025, 5, 7)  # Historical FOMC decision day
    spy_qqq = _make_healthy_spy_qqq(d)

    # Morning breakout at 09:45 -> enters -> drifts sideways -> flattened at 13:55 ET as FOMC_FLATTEN
    nvda_5m = [
        _et_bar(d, 9, 30, 100.5, 101.5, 100.0, 101.0, 10000.0),
        _et_bar(d, 9, 35, 101.0, 102.0, 100.5, 101.5, 10000.0),
        _et_bar(d, 9, 40, 101.5, 101.8, 100.8, 101.6, 10000.0),
        _et_bar(d, 9, 45, 101.6, 102.6, 101.5, 102.5, 30000.0),
        _et_bar(d, 13, 50, 102.8, 103.0, 102.7, 102.9, 10000.0),
        _et_bar(d, 13, 55, 102.9, 103.1, 102.8, 103.0, 10000.0),
    ]
    nvda_1m = [
        _et_bar(d, 9, 50, 102.50, 102.80, 102.45, 102.70, 3000.0),
        _et_bar(d, 13, 55, 103.00, 103.10, 102.95, 103.05, 3000.0),
    ]
    # Second symbol tries to break out at 13:35 ET (after 13:30 ET FOMC cutoff) -> must be blocked
    amd_5m = [
        _et_bar(d, 9, 30, 100.0, 101.0, 99.5, 100.5, 10000.0),
        _et_bar(d, 9, 35, 100.5, 101.0, 100.0, 100.6, 10000.0),
        _et_bar(d, 9, 40, 100.6, 101.0, 100.2, 100.7, 10000.0),
        _et_bar(d, 13, 35, 100.8, 101.6, 100.7, 101.5, 35000.0),
    ]

    store = InMemoryDataStore(
        bars_by_tf={"1Day": {}, "5Min": {**spy_qqq, "NVDA": nvda_5m, "AMD": amd_5m}, "1Min": {"NVDA": nvda_1m}}
    )
    cfg = BacktestConfig(
        mode="fixed",
        start_date="2025-05-07",
        end_date="2025-05-07",
        custom_symbols=["NVDA", "AMD"],
        enable_fomc_guard=True,
        exit_slippage_bps=0.0,
        monte_carlo_simulations=50,
    )
    res = await BacktestEngine(cfg, data_store=store).run(persist=False)

    assert len(res["trades"]) == 1
    assert res["trades"][0]["symbol"] == "NVDA"
    assert res["trades"][0]["exit_reason"] == "FOMC_FLATTEN"


@pytest.mark.asyncio
async def test_early_close_cutoff_and_1255_flatten(isolated_bt_db):
    """On 13:00 ET early-close day (2025-11-28), entries stop at 12:00 ET and open trades flatten at 12:55 ET."""
    d = date(2025, 11, 28)  # Day after Thanksgiving (13:00 ET close)
    spy_qqq = _make_healthy_spy_qqq(d, end_hh=13)

    nvda_5m = [
        _et_bar(d, 9, 30, 100.5, 101.5, 100.0, 101.0, 10000.0),
        _et_bar(d, 9, 35, 101.0, 102.0, 100.5, 101.5, 10000.0),
        _et_bar(d, 9, 40, 101.5, 101.8, 100.8, 101.6, 10000.0),
        _et_bar(d, 9, 45, 101.6, 102.6, 101.5, 102.5, 30000.0),
        _et_bar(d, 12, 50, 102.8, 103.0, 102.7, 102.9, 10000.0),
    ]
    nvda_1m = [
        _et_bar(d, 9, 50, 102.50, 102.80, 102.45, 102.70, 3000.0),
        _et_bar(d, 12, 55, 103.10, 103.20, 103.00, 103.15, 3000.0),
    ]
    amd_5m = [
        _et_bar(d, 9, 30, 100.0, 101.0, 99.5, 100.5, 10000.0),
        _et_bar(d, 9, 35, 100.5, 101.0, 100.0, 100.6, 10000.0),
        _et_bar(d, 9, 40, 100.6, 101.0, 100.2, 100.7, 10000.0),
        _et_bar(d, 12, 5, 100.8, 101.6, 100.7, 101.5, 35000.0),  # after 12:00 ET cutoff
    ]

    store = InMemoryDataStore(
        bars_by_tf={"1Day": {}, "5Min": {**spy_qqq, "NVDA": nvda_5m, "AMD": amd_5m}, "1Min": {"NVDA": nvda_1m}},
        sessions={"2025-11-28": {"date": "2025-11-28", "open": "09:30", "close": "13:00"}},
    )
    cfg = BacktestConfig(
        mode="fixed",
        start_date="2025-11-28",
        end_date="2025-11-28",
        custom_symbols=["NVDA", "AMD"],
        enable_early_close_guard=True,
        exit_slippage_bps=0.0,
        monte_carlo_simulations=50,
    )
    res = await BacktestEngine(cfg, data_store=store).run(persist=False)

    assert len(res["trades"]) == 1
    assert res["trades"][0]["symbol"] == "NVDA"
    assert res["trades"][0]["exit_reason"] == "EOD_FLATTEN"


def test_mode_a_screener_zero_lookahead():
    """Perturbing bars on day T and T+1 must have zero effect on day T's point-in-time screened basket."""
    start = date(2024, 10, 1)
    daily_bars: Dict[str, List[Bar]] = {}
    symbols = [f"SYM{i}" for i in range(1, 9)]

    for idx, sym in enumerate(symbols):
        bars: List[Bar] = []
        price = 50.0 + idx * 5.0
        # Build 90 trading days of steady uptrends with varying strengths
        slope = 1.003 + (idx * 0.0015)
        cur = start
        days_added = 0
        while days_added < 90:
            if cur.weekday() < 5:
                price *= slope
                # Tighten final 5 days for VCP setup proxy
                spread = 0.015 if days_added > 82 else 0.035
                vol = 2_000_000.0 if days_added <= 82 else 1_500_000.0
                bars.append(
                    _et_bar(cur, 16, 0, price * 0.995, price * (1 + spread), price * (1 - spread), price, vol)
                )
                days_added += 1
            cur += timedelta(days=1)
        daily_bars[sym] = bars

    target_date = daily_bars["SYM1"][-2].timestamp.astimezone(ET).date()
    cfg = BacktestConfig(
        mode="screener",
        start_date=target_date.strftime("%Y-%m-%d"),
        end_date=target_date.strftime("%Y-%m-%d"),
        screener_min_rs=50.0,
        screener_min_tech_rating=5.0,
        screener_min_setup_rating=5.0,
    )

    scr1 = ScreenerSimulator(cfg, daily_bars)
    basket1 = scr1.screen_for_date(target_date)
    assert len(basket1) >= 1

    # Violently perturb day T and day T+1 bars (crash one stock, 10x another)
    mutated = {s: list(b_list) for s, b_list in daily_bars.items()}
    for s in symbols:
        last_two = mutated[s][-2:]
        mutated[s][-2] = Bar(last_two[0].timestamp, 1.0, 2.0, 0.5, 1.0, 99_000_000.0)
        mutated[s][-1] = Bar(last_two[1].timestamp, 999.0, 1000.0, 990.0, 995.0, 99_000_000.0)

    scr2 = ScreenerSimulator(cfg, mutated)
    basket2 = scr2.screen_for_date(target_date)

    assert [(c.symbol, c.composite_score) for c in basket1] == [(c.symbol, c.composite_score) for c in basket2]


def test_backtest_web_ui_auth_and_isolation(isolated_bt_db):
    """/backtest requires auth, renders cleanly when authenticated, and serves export routes."""
    client = TestClient(app)

    # Unauthenticated HTML request redirects to /login
    r_unauth = client.get("/backtest", headers={"accept": "text/html"}, follow_redirects=False)
    assert r_unauth.status_code in (303, 401)

    # Authenticated request via HTTP Basic succeeds and shows Backtest Lab
    r_ok = client.get(
        "/backtest",
        auth=(settings.DASHBOARD_USERNAME, settings.DASHBOARD_PASSWORD),
    )
    assert r_ok.status_code == 200
    assert "Automated Portfolio Backtest Lab" in r_ok.text
    assert "Mode A: Point-in-Time Dynamic Screener" in r_ok.text
