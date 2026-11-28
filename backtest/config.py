"""Backtest configuration & isolated credential resolution.

Reads strategy parameter defaults live from app.config.settings (read-only)
so parameter updates made to live pTrading stay automatically synchronized.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services.financing_service import RISK_PROFILES


def _read_dotenv_values(dotenv_path: Path = Path(".env")) -> Dict[str, str]:
    """Lightweight .env parser for backtest-only keys without mutating app/config.py."""
    values: Dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    try:
        for raw_line in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                values[k] = v
    except Exception:
        pass
    return values


def resolve_backtest_alpaca_credentials() -> Tuple[str, str, str]:
    """Resolves Alpaca API credentials for read-only historical market data.

    Priority:
      1. BACKTEST_ALPACA_API_KEY / BACKTEST_ALPACA_SECRET_KEY (env or .env) — dedicated backtest paper account
      2. app.config.settings.ALPACA_API_KEY / ALPACA_SECRET_KEY — live paper account fallback
    Returns (api_key, secret_key, source_label).
    """
    dotenv = _read_dotenv_values()
    bt_key = (os.getenv("BACKTEST_ALPACA_API_KEY") or dotenv.get("BACKTEST_ALPACA_API_KEY") or "").strip()
    bt_sec = (os.getenv("BACKTEST_ALPACA_SECRET_KEY") or dotenv.get("BACKTEST_ALPACA_SECRET_KEY") or "").strip()
    if bt_key and bt_sec:
        return bt_key, bt_sec, "BACKTEST_ALPACA_KEY"

    live_key = (settings.ALPACA_API_KEY or os.getenv("ALPACA_API_KEY") or dotenv.get("ALPACA_API_KEY") or "").strip()
    live_sec = (settings.ALPACA_SECRET_KEY or os.getenv("ALPACA_SECRET_KEY") or dotenv.get("ALPACA_SECRET_KEY") or "").strip()
    if live_key and live_sec:
        return live_key, live_sec, "LIVE_ALPACA_KEY_FALLBACK"

    return "", "", "MISSING"


def get_backtest_db_path() -> Path:
    """Dedicated SQLite DB path for backtest runs/trades (isolated from live ptrading.db)."""
    raw = os.getenv("BACKTEST_DATABASE_PATH")
    path = Path(raw) if raw else (settings.DATA_DIR / "backtest.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_backtest_cache_db_path() -> Path:
    """Dedicated SQLite bar-cache DB path for historical 1d/5m/1m bars."""
    raw = os.getenv("BACKTEST_CACHE_PATH")
    path = Path(raw) if raw else (settings.DATA_DIR / "backtest_cache.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class BacktestConfig:
    """Complete parameter specification for a deterministic portfolio backtest run."""

    start_date: str
    end_date: str
    mode: str = "screener"  # "screener" (Mode A), "fixed" (Mode B), "chartmill_history" (Mode C)
    name: str = ""
    feed: str = "sip"  # "sip" (full consolidated historical) or "iex" (matches live free real-time)
    initial_capital: float = 50000.0

    # Risk & Capital Governance (defaults pulled from live app.config.settings)
    risk_profile: str = "P2_BALANCED"  # P1_DEFENSIVE, P2_BALANCED, P3_GROWTH, CUSTOM
    risk_percent_per_trade: float = field(default_factory=lambda: float(settings.RISK_PERCENT_PER_TRADE))
    max_concurrent_positions: int = field(default_factory=lambda: int(settings.MAX_CONCURRENT_POSITIONS))
    max_total_positions_with_breakeven: int = field(
        default_factory=lambda: int(settings.MAX_TOTAL_POSITIONS_WITH_BREAKEVEN)
    )
    max_equity_percent_per_stock: float = field(default_factory=lambda: float(settings.MAX_EQUITY_PERCENT_PER_STOCK))
    daily_max_loss_percent: float = field(default_factory=lambda: float(settings.DAILY_MAX_LOSS_PERCENT))
    min_stop_distance_dollars: float = field(default_factory=lambda: float(settings.MIN_STOP_DISTANCE_DOLLARS))

    # Feature Toggles (mirror live guards & allow A/B testing)
    enable_slot_recycling: bool = True
    enable_health_ladder: bool = True
    enable_fomc_guard: bool = field(default_factory=lambda: bool(settings.FOMC_GUARD_ENABLED))
    enable_early_close_guard: bool = True
    enable_spy_qqq_gate: bool = True
    lock_triggered_on_reject: bool = True  # Matches live MarketScanner.triggered_symbols behavior

    # Entry & Exit Parameters
    scanner_min_rvol: float = field(default_factory=lambda: float(settings.SCANNER_MIN_RVOL))
    scanner_max_atr_extension: float = field(default_factory=lambda: float(settings.SCANNER_MAX_ATR_EXTENSION))
    entry_limit_buffer_pct: float = 0.05  # 0.05% marketable limit allowance (close * 1.0005)
    breakeven_trigger_r: float = field(default_factory=lambda: float(settings.BREAKEVEN_TRIGGER_R))
    breakeven_offset_dollars: float = field(default_factory=lambda: float(settings.BREAKEVEN_OFFSET_DOLLARS))
    take_profit_r: float = 2.0
    stale_order_timeout_minutes: int = field(
        default_factory=lambda: max(1, int(round(settings.STALE_ORDER_TIMEOUT_SECONDS / 60)))
    )
    fomc_entry_cutoff_time: str = field(default_factory=lambda: str(settings.FOMC_ENTRY_CUTOFF_TIME))
    fomc_flatten_time: str = field(default_factory=lambda: str(settings.FOMC_PRE_EVENT_FLATTEN_TIME))

    # Optional Experiment / Optimization Filters (all OFF by default for 1:1 live baseline parity)
    custom_entry_cutoff_et: Optional[str] = None  # e.g. "11:30" or "12:00" for morning-only tests
    midday_blackout_start_et: Optional[str] = None  # e.g. "12:00"
    midday_blackout_end_et: Optional[str] = None  # e.g. "14:00"
    require_spy_above_daily_ema20: bool = False  # Higher-timeframe SPY daily trend filter (T-1 close > EMA20)
    stop_atr_multiplier: float = 1.5  # Live default = 1.5x 5m ATR; test 2.5-4.0x to avoid micro-noise stops
    use_pure_or_low_stop: bool = False  # Test structural OR_Low stop instead of max(OR_Low, close - k*ATR)

    # Realistic Friction Modeling
    exit_slippage_bps: float = 3.0  # 3 bps (0.03%) conservative slippage on stop-outs & EOD market flattens
    commission_per_share: float = 0.0  # Alpaca equities are $0 commission
    use_1m_execution: bool = True  # Step trades across 1m bars for exact fill/BE/SL/TP ordering

    # Basket & Screener Parameters
    max_basket_size: Optional[int] = 38  # None = uncapped dynamic basket (Option B); 38 = top 38
    custom_symbols: List[str] = field(default_factory=list)  # Used in Mode B; defaults to settings.DEFAULT_BASKET
    screener_min_price: float = 10.0
    screener_min_avg_vol: float = 1_000_000.0
    screener_min_atr_pct: float = 2.5
    screener_min_rs: float = 75.0
    screener_min_tech_rating: float = 7.0
    screener_min_setup_rating: float = 7.0
    enable_edgar_fundamentals: bool = False
    screener_min_fundamental_rating: float = 5.0

    # Analytics Options
    monte_carlo_simulations: int = 2000
    run_gemini_review: bool = False  # Keep deterministic by default for fast/free runs; opt-in via UI

    def resolved_base_risk_pct(self) -> float:
        profile = RISK_PROFILES.get(self.risk_profile)
        if profile and profile.get("risk_pct") is not None:
            return float(profile["risk_pct"])
        return float(self.risk_percent_per_trade)

    def resolved_fixed_symbols(self) -> List[str]:
        raw = self.custom_symbols if self.custom_symbols else settings.DEFAULT_BASKET.split(",")
        seen = set()
        out: List[str] = []
        for s in raw:
            sym = str(s).strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BacktestConfig":
        valid_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)
