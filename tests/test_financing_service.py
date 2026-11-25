"""Tests for FinancingService, Health-State Drawdown Ladder, Dead Man's Brake, Qualification, Custody & Routes."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.brokers.base import BrokerAdapter
from app.config import settings
from app.db import (
    add_capital_ledger_entry,
    get_connection,
    init_db,
    set_validation_check,
    update_financing_config,
)
from app.main import app
from app.services.financing_service import FinancingService, financing_service
from app.services.risk_manager import RiskManager


class DummyBroker(BrokerAdapter):
    def __init__(self, equity: float = 50000.0, buying_power: float = 100000.0) -> None:
        self.equity = equity
        self.buying_power = buying_power
        self.positions = []
        self.orders = []

    async def get_account_equity(self) -> float:
        return self.equity

    async def get_buying_power(self) -> float:
        return self.buying_power

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
async def isolated_db(tmp_path, monkeypatch):
    """Uses a fresh temporary SQLite DB for every test."""
    db_file = tmp_path / "test_financing.db"
    monkeypatch.setattr(settings, "DATABASE_PATH", db_file)
    await init_db()
    yield


def test_health_state_ladder_and_hysteresis():
    # Normal step-downs
    assert FinancingService.compute_health_state(2.0) == ("HEALTHY", False, 1.0)
    assert FinancingService.compute_health_state(5.0) == ("CAUTION", False, 0.75)
    assert FinancingService.compute_health_state(10.0) == ("REDUCED_RISK", False, 0.50)
    assert FinancingService.compute_health_state(15.0) == ("SUSPENDED", True, 0.0)
    assert FinancingService.compute_health_state(20.0) == ("CAPITAL_PROTECTION", True, 0.0)

    # Hysteresis: staying in CAUTION until DD < 3.5%
    assert FinancingService.compute_health_state(4.2, previous_state="CAUTION") == ("CAUTION", False, 0.75)
    assert FinancingService.compute_health_state(3.4, previous_state="CAUTION") == ("HEALTHY", False, 1.0)

    # Hysteresis: staying in REDUCED_RISK until DD < 8.5%
    assert FinancingService.compute_health_state(9.0, previous_state="REDUCED_RISK") == ("REDUCED_RISK", False, 0.50)
    assert FinancingService.compute_health_state(8.0, previous_state="REDUCED_RISK") == ("CAUTION", False, 0.75)

    # Latched SUSPENDED stays latched even if DD recovers until manual reset
    assert FinancingService.compute_health_state(2.0, previous_state="SUSPENDED", latched=True) == ("SUSPENDED", True, 0.0)

    # Dead Man's Brake always forces CAPITAL_PROTECTION
    assert FinancingService.compute_health_state(0.0, brake_latched=True) == ("CAPITAL_PROTECTION", True, 0.0)


@pytest.mark.asyncio
async def test_risk_manager_active_enforcement_caution_pilot_ceiling_and_brake():
    # Seed a 6% cumulative drawdown in daily_stats -> CAUTION (0.75x multiplier)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO daily_stats (date, starting_equity, current_equity, circuit_breaker_tripped) VALUES ('2026-11-20', 50000.0, 47000.0, 0);"
        )
        await conn.commit()

    # Configure LIVE_PILOT (0.5x) + Maximum Live Accessible Capital ceiling ($20,000)
    await update_financing_config(
        {
            "lifecycle_stage": "LIVE_PILOT",
            "risk_profile": "P2_BALANCED",  # 0.50% base
            "pilot_risk_multiplier": 0.5,
            "max_live_accessible_capital": 20000.0,
            "enforcement_enabled": 1,
        }
    )

    broker = DummyBroker(equity=47000.0, buying_power=94000.0)
    rm = RiskManager(broker)

    # Effective equity = min(47000, 20000) = $20,000
    # Effective risk % = 0.50% * 0.75 (CAUTION) * 0.50 (LIVE_PILOT) = 0.1875%
    # Risk dollars = 20000 * 0.001875 = $37.50
    eval_res = await rm.evaluate_signal(
        symbol="AAPL",
        close_price=100.0,
        or_high=99.5,
        or_low=98.0,
        atr=1.0,
    )
    assert eval_res.approved is True
    assert eval_res.health_state == "CAUTION"
    assert eval_res.effective_equity == 20000.0
    assert eval_res.risk_multiplier == pytest.approx(0.375, rel=1e-3)
    assert eval_res.risk_dollars == pytest.approx(37.50, abs=0.05)

    # Now latch Dead Man's Brake -> must fail closed for new entries
    await financing_service.latch_dead_mans_brake("Test integrity fault")
    blocked = await rm.evaluate_signal(
        symbol="MSFT",
        close_price=100.0,
        or_high=99.5,
        or_low=98.0,
        atr=1.0,
    )
    assert blocked.approved is False
    assert "Dead Man's Brake" in (blocked.rejection_reason or "")

    # Reset brake -> entries allowed again
    await financing_service.reset_dead_mans_brake()
    rearmed = await rm.evaluate_signal(
        symbol="MSFT",
        close_price=100.0,
        or_high=99.5,
        or_low=98.0,
        atr=1.0,
    )
    assert rearmed.approved is True


@pytest.mark.asyncio
async def test_qualification_verdicts():
    cfg = {
        "qual_min_trades": 30,
        "qual_min_trading_days": 10,
        "qual_min_expectancy_r": 0.15,
        "qual_min_profit_factor": 1.30,
        "qual_max_drawdown_pct": 10.0,
        "qual_max_avg_slippage_r": 0.10,
        "qual_max_ror_20pct": 10.0,
    }

    # 1. Collecting evidence (small sample)
    early_report = {
        "cumulative_trades_metrics": {"total_trades": 8, "expectancy_r": 0.30, "profit_factor": 1.8, "slippage": {"avg_slippage_r": 0.02}},
        "roi_equity": {"trading_days": 4, "max_drawdown_pct": 2.1},
        "risk_of_ruin": {"analytical": {"sizing_status": "CONSERVATIVE", "drawdown_probabilities": {"20pct": 1.2}}, "monte_carlo": {"20pct": 1.5}},
    }
    res = financing_service.evaluate_qualification(cfg, early_report, {}, open_trades_count=0)
    assert res["verdict"] == "COLLECTING_EVIDENCE"

    # 2. Not viable (negative edge across full sample)
    bad_report = {
        "cumulative_trades_metrics": {"total_trades": 32, "expectancy_r": -0.15, "profit_factor": 0.80, "slippage": {"avg_slippage_r": 0.03}},
        "roi_equity": {"trading_days": 12, "max_drawdown_pct": 9.5},
        "risk_of_ruin": {"analytical": {"sizing_status": "NEGATIVE_EDGE", "drawdown_probabilities": {"20pct": 100.0}}, "monte_carlo": {"20pct": 99.0}},
    }
    res_bad = financing_service.evaluate_qualification(cfg, bad_report, {}, open_trades_count=0)
    assert res_bad["verdict"] == "NOT_VIABLE"

    # 3. Ready for Live Pilot (all 6 auto + 4 manual passed)
    good_report = {
        "cumulative_trades_metrics": {"total_trades": 35, "expectancy_r": 0.35, "profit_factor": 1.85, "slippage": {"avg_slippage_r": 0.03}},
        "roi_equity": {"trading_days": 14, "max_drawdown_pct": 4.2},
        "risk_of_ruin": {"analytical": {"sizing_status": "CONSERVATIVE", "drawdown_probabilities": {"20pct": 0.5}}, "monte_carlo": {"20pct": 0.8}},
    }
    all_manual = {
        "KILL_SWITCH_TESTED": {"status": "PASSED"},
        "BREAKEVEN_STEP_VERIFIED": {"status": "PASSED"},
        "LIVE_CUSTODY_SET": {"status": "PASSED"},
        "BROKER_LIVE_READY": {"status": "PASSED"},
    }
    res_good = financing_service.evaluate_qualification(cfg, good_report, all_manual, open_trades_count=0)
    assert res_good["verdict"] == "READY_FOR_LIVE_PILOT"
    assert res_good["passed_count"] == 10


@pytest.mark.asyncio
async def test_wealth_custody_ledger_and_tax_csv():
    await update_financing_config(
        {
            "seed_capital": 10000.0,
            "target_capital_goal": 50000.0,
            "reserve_pct": 20.0,
            "max_live_accessible_capital": 15000.0,
        }
    )
    await add_capital_ledger_entry("2026-11-20", "INITIAL_SEED", 10000.0, "IN_TO_BROKER", "Seed")
    await add_capital_ledger_entry("2026-11-22", "VAULT_SWEEP", 3000.0, "OUT_TO_VAULT", "Profit sweep")

    # Insert one closed trade to verify tax CSV strictly contains trading P&L and excludes ledger flows
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO trades (
                client_order_id, symbol, qty, entry_price, stop_loss_price,
                take_profit_price, trade_date, exit_price, pnl_dollars, pnl_r, exit_reason, is_open
            ) VALUES ('pt_nvda_1', 'NVDA', 10, 100.0, 98.0, 104.0, '2026-11-24', 104.0, 40.0, 2.0, 'TAKE_PROFIT', 0);
            """
        )
        await conn.commit()

    summary = await financing_service.build_financing_summary(broker_equity=17000.0)
    wealth = summary["wealth"]
    assert wealth["external_vault_balance"] == 3000.0
    assert wealth["total_programme_wealth"] == 20000.0
    assert wealth["progress_pct"] == 25.0  # (20k - 10k) / (50k - 10k)
    assert wealth["ceiling_exceeded"] is True
    assert wealth["surplus_sweep_suggestion"] == 2000.0  # 17k broker - 15k cap

    csv_text = await financing_service.build_tax_pnl_csv()
    assert "NVDA" in csv_text
    assert "40.0" in csv_text
    assert "VAULT_SWEEP" not in csv_text
    assert "INITIAL_SEED" not in csv_text


@pytest.mark.asyncio
async def test_financing_routes_auth_and_endpoints():
    auth = (settings.DASHBOARD_USERNAME, settings.DASHBOARD_PASSWORD)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /api/financing/summary
        res = await client.get("/api/financing/summary", auth=auth)
        assert res.status_code == 200
        data = res.json()
        assert "qualification" in data
        assert "wealth" in data
        assert "controls" in data

        # POST /api/financing/checks/KILL_SWITCH_TESTED
        chk = await client.post(
            "/api/financing/checks/KILL_SWITCH_TESTED",
            data={"status": "PASSED", "notes": "Verified"},
            auth=auth,
            follow_redirects=False,
        )
        assert chk.status_code == 303

        # GET /api/financing/tax-pnl.csv
        csv_res = await client.get("/api/financing/tax-pnl.csv", auth=auth)
        assert csv_res.status_code == 200
        assert "realised_pnl_usd" in csv_res.text
