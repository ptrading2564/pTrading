"""Unit tests for the Two-Layer StrategyReviewer (diagnostics + deterministic/Gemini review)."""

from unittest.mock import AsyncMock, patch
import pytest

from app.config import settings
from app.services.strategy_reviewer import StrategyReviewer


def _sample_report_with_leaks() -> dict:
    return {
        "report_date": "2026-11-24",
        "strategy_params": {
            "risk_percent_per_trade": 0.5,
            "breakeven_trigger_r": 1.0,
            "stale_order_timeout_seconds": 180,
        },
        "roi_equity": {
            "trading_days": 15,
            "daily_starting_equity": 50000.0,
            "daily_ending_equity": 49500.0,
            "daily_pnl": -500.0,
            "daily_roi_pct": -1.0,
            "cumulative_roi_pct": 2.5,
            "daily_spy_return_pct": 0.35,
            "daily_qqq_return_pct": 0.45,
            "daily_alpha_vs_spy_pct": -1.35,
            "market_regime": "CHOPPY",
            "circuit_breaker_tripped_today": False,
        },
        "daily_trades_metrics": {
            "total_trades": 3,
            "wins": 1,
            "losses": 2,
            "breakevens": 0,
            "total_r": -1.0,
            "realized_rr_r": 2.0,
            "expectancy_r": -0.33,
            "expectancy_dollars": -166.67,
            "symbol_breakdown": [{"symbol": "NVDA", "net_pnl": 500.0, "total_r": 2.0}],
            "breakeven_audit": {"stepped_count": 1, "reached_tp_after_be": 1, "stopped_at_be": 0},
            "cluster_stop_events": [{"timestamp": "2026-11-24T15:10:00", "count": 2, "symbols": ["AMD", "SMCI"]}],
        },
        "cumulative_trades_metrics": {
            "total_trades": 32,
            "win_rate_pct": 43.8,
            "realized_rr_r": 1.95,
            "expectancy_r": 0.28,
            "profit_factor": 1.65,
            "sqn": 1.8,
            "breakeven_audit": {
                "stepped_count": 16,
                "stopped_at_be": 10,
                "choked_winners_count": 6,
                "choked_winners_pct": 60.0,
            },
            "mfe_mae": {
                "sample_count": 20,
                "avg_mfe_r": 1.9,
                "avg_mae_r": 0.55,
                "avg_winner_mfe_r": 2.8,
            },
            "slippage": {
                "sample_count": 15,
                "avg_slippage_dollars": 42.0,
                "avg_slippage_r": 0.14,
                "total_slippage_dollars": 630.0,
            },
            "time_of_day_buckets": {
                "09:45-10:30": {"trades": 14, "win_rate_pct": 57.1, "avg_r": 0.65, "total_r": 9.1},
                "12:00-14:00": {"trades": 8, "win_rate_pct": 12.5, "avg_r": -0.55, "total_r": -4.4},
            },
            "stop_type_buckets": {
                "OR_LOW": {"trades": 16, "win_rate_pct": 56.0, "avg_r": 0.60, "total_r": 9.6},
                "ATR_1_5": {"trades": 16, "win_rate_pct": 31.0, "avg_r": -0.05, "total_r": -0.8},
            },
        },
        "signal_funnel": {"total_signals": 6, "expired_stale_orders": 2},
        "risk_of_ruin": {
            "analytical": {
                "drawdown_probabilities": {"10pct": 3.5, "20pct": 0.12, "50pct": 0.0},
                "half_kelly_pct": 7.5,
                "current_risk_pct": 0.5,
                "sizing_status": "CONSERVATIVE",
            },
            "monte_carlo": {"10pct": 4.2, "20pct": 0.2, "50pct": 0.0},
        },
        "projections": {
            "confidence": "MEANINGFUL",
            "avg_trades_per_day": 2.1,
            "horizons": [
                {"label": "30D (~1.5M)", "p10_equity": 48000.0, "p50_equity": 54000.0, "p90_equity": 61000.0, "p50_roi_pct": 8.0},
                {"label": "1 Year (252D)", "p10_equity": 58000.0, "p50_equity": 88000.0, "p90_equity": 135000.0, "p50_roi_pct": 76.0},
            ],
        },
    }


def test_diagnostics_detect_all_strategy_leaks():
    report = _sample_report_with_leaks()
    findings = StrategyReviewer.run_diagnostics(report)
    ids = {f["id"] for f in findings}

    assert "BREAKEVEN_CHOKE" in ids
    assert "MFE_RUNNERS_CAPPED" in ids
    assert "LUNCH_HOUR_DRAG" in ids
    assert "STOP_TYPE_DIVERGENCE" in ids
    assert "SLIPPAGE_LEAK" in ids
    assert "STALE_ORDER_EXPIRATIONS" in ids
    assert "CLUSTER_STOP_OUT" in ids


@pytest.mark.asyncio
async def test_deterministic_review_generation():
    report = _sample_report_with_leaks()
    reviewer = StrategyReviewer()
    with patch.object(settings, "GEMINI_API_KEY", ""):
        md, source, findings = await reviewer.generate_review(report)

    assert source == "deterministic"
    assert "# Daily Strategy Review — 2026-11-24" in md
    assert "## 1. Executive Summary" in md
    assert "## 5. Suggested Next Steps (Anti-Overfitting Gate)" in md
    assert "BREAKEVEN_TRIGGER_R" in md
    assert len(findings) >= 5


@pytest.mark.asyncio
async def test_gemini_review_fallback_on_error():
    report = _sample_report_with_leaks()
    reviewer = StrategyReviewer()
    with patch.object(settings, "GEMINI_API_KEY", "fake_key"), patch(
        "httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=RuntimeError("network down")
    ):
        md, source, _ = await reviewer.generate_review(report)

    assert source == "deterministic"
    assert "## 1. Executive Summary" in md
