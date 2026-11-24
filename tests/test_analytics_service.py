"""Unit tests for AnalyticsService: P&L, ROI, R:R, Analytical RoR, Monte Carlo, and Projections."""

import pytest

from app.db import get_connection, init_db
from app.services.analytics_service import (
    AnalyticsService,
    calculate_analytical_ror,
    run_monte_carlo_and_projections,
)


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()
    async with get_connection() as conn:
        await conn.execute("DELETE FROM trades;")
        await conn.execute("DELETE FROM daily_stats;")
        await conn.execute("DELETE FROM daily_reports;")
        await conn.execute("DELETE FROM signals;")
        await conn.execute("DELETE FROM orders;")
        await conn.commit()


def test_analytical_ror_positive_edge_asymmetric():
    # 50% win rate, 2:1 payoff => strong positive edge (E = +0.5R), low RoR at 0.5% risk
    res = calculate_analytical_ror(
        win_rate=0.5,
        loss_rate=0.5,
        payoff_ratio=2.0,
        risk_per_trade_pct=0.5,
    )
    assert res["edge_per_decisive_r"] == pytest.approx(0.5, rel=1e-3)
    assert res["kelly_pct"] == pytest.approx(25.0, rel=1e-2)
    assert res["half_kelly_pct"] == pytest.approx(12.5, rel=1e-2)
    assert res["sizing_status"] == "CONSERVATIVE"
    assert res["drawdown_probabilities"]["20pct"] < 1.0
    assert res["drawdown_probabilities"]["50pct"] < 0.01


def test_analytical_ror_negative_edge():
    # 25% win rate, 2:1 payoff => E = 0.25*2 - 0.75 = -0.25R => 100% RoR
    res = calculate_analytical_ror(
        win_rate=0.25,
        loss_rate=0.75,
        payoff_ratio=2.0,
        risk_per_trade_pct=0.5,
    )
    assert res["sizing_status"] == "NEGATIVE_EDGE"
    assert res["drawdown_probabilities"]["10pct"] == 100.0
    assert res["drawdown_probabilities"]["20pct"] == 100.0
    assert res["drawdown_probabilities"]["50pct"] == 100.0


def test_analytical_ror_zero_losses():
    res = calculate_analytical_ror(
        win_rate=1.0,
        loss_rate=0.0,
        payoff_ratio=2.0,
        risk_per_trade_pct=0.5,
    )
    assert res["drawdown_probabilities"]["20pct"] == 0.0


def test_monte_carlo_deterministic_and_monotonic_percentiles():
    r_series = [2.0, 2.0, -1.0, 0.02, -1.0, 2.0, 2.0, -1.0, 0.0, 2.0] * 4  # 40 trades
    mc = run_monte_carlo_and_projections(
        r_multiples=r_series,
        current_equity=50000.0,
        risk_per_trade_pct=0.5,
        avg_trades_per_day=2.0,
        num_simulations=1000,
        seed=42,
    )
    assert mc["confidence"] == "MEANINGFUL"
    assert len(mc["horizons"]) == 4
    for h in mc["horizons"]:
        assert h["p10_equity"] <= h["p25_equity"] <= h["p50_equity"] <= h["p75_equity"] <= h["p90_equity"]
    # Positive expectancy series should project median growth over 1Y
    assert mc["horizons"][-1]["p50_equity"] > 50000.0


def test_monte_carlo_empty_series():
    mc = run_monte_carlo_and_projections(r_multiples=[], current_equity=50000.0)
    assert mc["confidence"] == "INSUFFICIENT"
    assert mc["horizons"] == []
    assert mc["empirical_ror"]["20pct"] is None


def test_trade_metrics_and_attribution():
    svc = AnalyticsService()
    trades = [
        {
            "symbol": "NVDA",
            "qty": 100,
            "pnl_dollars": 500.0,
            "pnl_r": 2.0,
            "initial_risk_dollars": 250.0,
            "exit_reason": "TAKE_PROFIT",
            "breakeven_stepped": 1,
            "stop_type": "OR_LOW",
            "signal_rvol": 2.5,
            "signal_atr_ext": 0.3,
            "slippage_dollars": 0.05,
            "slippage_r": 0.02,
            "mfe_r": 2.4,
            "mae_r": 0.3,
            "entry_time": "2026-11-24T15:00:00+00:00",  # 10:00 ET
            "exit_time": "2026-11-24T16:00:00+00:00",
        },
        {
            "symbol": "AMD",
            "qty": 100,
            "pnl_dollars": -250.0,
            "pnl_r": -1.0,
            "initial_risk_dollars": 250.0,
            "exit_reason": "STOP_LOSS",
            "breakeven_stepped": 0,
            "stop_type": "ATR_1_5",
            "signal_rvol": 1.7,
            "signal_atr_ext": 0.6,
            "slippage_dollars": 0.10,
            "slippage_r": 0.04,
            "mfe_r": 0.4,
            "mae_r": 1.0,
            "entry_time": "2026-11-24T17:30:00+00:00",  # 12:30 ET
            "exit_time": "2026-11-24T18:00:00+00:00",
        },
        {
            "symbol": "TSLA",
            "qty": 100,
            "pnl_dollars": 2.0,
            "pnl_r": 0.01,
            "initial_risk_dollars": 250.0,
            "exit_reason": "BREAKEVEN",
            "breakeven_stepped": 1,
            "stop_type": "OR_LOW",
            "signal_rvol": 3.2,
            "signal_atr_ext": 0.2,
            "slippage_dollars": 0.0,
            "slippage_r": 0.0,
            "mfe_r": 1.2,
            "mae_r": 0.1,
            "post_exit_max_r": 2.5,
            "entry_time": "2026-11-24T15:10:00+00:00",  # 10:10 ET
            "exit_time": "2026-11-24T16:30:00+00:00",
        },
    ]

    m = svc.compute_trade_metrics(trades)
    assert m["total_trades"] == 3
    assert m["wins"] == 1
    assert m["losses"] == 1
    assert m["breakevens"] == 1
    assert m["net_pnl"] == 252.0
    assert m["profit_factor"] == pytest.approx(502.0 / 250.0, rel=1e-2)
    assert m["realized_rr_r"] == pytest.approx(2.0, rel=1e-2)
    assert m["breakeven_audit"]["choked_winners_count"] == 1
    assert "09:45-10:30" in m["time_of_day_buckets"]
    assert "12:00-14:00" in m["time_of_day_buckets"]


@pytest.mark.asyncio
async def test_build_comprehensive_report_from_db():
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO daily_stats (date, starting_equity, current_equity, realized_pnl, trade_count, spy_return_pct, market_regime)
            VALUES ('2026-11-23', 50000.0, 50500.0, 500.0, 2, 0.4, 'TREND_UP'),
                   ('2026-11-24', 50500.0, 50750.0, 250.0, 1, 0.2, 'CHOPPY');
            """
        )
        await conn.execute(
            """
            INSERT INTO trades (client_order_id, symbol, qty, entry_price, stop_loss_price, initial_stop_price,
                                initial_risk_dollars, take_profit_price, exit_price, pnl_dollars, pnl_r,
                                exit_reason, trade_date, is_open)
            VALUES ('c1', 'NVDA', 100, 100.0, 97.5, 97.5, 250.0, 105.0, 105.0, 500.0, 2.0, 'TAKE_PROFIT', '2026-11-23', 0),
                   ('c2', 'AMD', 100, 100.0, 97.5, 97.5, 250.0, 105.0, 102.5, 250.0, 1.0, 'EOD_FLATTEN', '2026-11-24', 0);
            """
        )
        await conn.commit()

    svc = AnalyticsService()
    report = await svc.build_comprehensive_report(report_date="2026-11-24", num_simulations=500)
    assert report["report_date"] == "2026-11-24"
    assert report["roi_equity"]["daily_pnl"] == 250.0
    assert report["roi_equity"]["cumulative_roi_pct"] == pytest.approx(1.5, rel=1e-2)
    assert report["daily_trades_metrics"]["total_trades"] == 1
    assert report["cumulative_trades_metrics"]["total_trades"] == 2
    assert len(report["projections"]["horizons"]) == 4
