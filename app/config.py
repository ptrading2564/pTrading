"""Application configuration settings using Pydantic Settings."""

from enum import Enum
from pathlib import Path
from typing import List, Set
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(str, Enum):
    AUTO = "AUTO"
    SEMI_AUTO = "SEMI_AUTO"
    PAUSED = "PAUSED"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server Settings
    ENVIRONMENT: str = "production"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DATA_DIR: Path = Path("data")
    DATABASE_PATH: Path = Path("data/ptrading.db")

    # Domain & Webhook Security
    DOMAIN_NAME: str = "trading.yourdomain.com"
    WEBHOOK_SECRET: str = "change_this_webhook_secret_in_env"
    VERIFY_TRADINGVIEW_IP: bool = True
    TRADINGVIEW_IPS: Set[str] = {
        "52.89.214.238",
        "34.212.75.30",
        "54.218.53.128",
        "52.32.178.7",
    }

    # Dashboard Authentication
    DASHBOARD_USERNAME: str = "admin"
    DASHBOARD_PASSWORD: str = "adminpassword123"
    SESSION_SECRET_KEY: str = "secret-key-32-bytes-minimum-trading-session"

    # Alpaca API Credentials
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_PAPER: bool = True
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"

    # Trading Execution Mode
    EXECUTION_MODE: ExecutionMode = ExecutionMode.SEMI_AUTO

    # Risk Management Parameters
    RISK_PERCENT_PER_TRADE: float = 0.5  # 0.5% equity per trade
    MAX_CONCURRENT_POSITIONS: int = 4
    MAX_EQUITY_PERCENT_PER_STOCK: float = 25.0  # Max 25% single stock allocation
    DAILY_MAX_LOSS_PERCENT: float = 2.0  # -2.0% daily circuit breaker
    MIN_STOP_DISTANCE_DOLLARS: float = 0.10
    BREAKEVEN_TRIGGER_R: float = 1.0  # Move stop at +1.0R
    BREAKEVEN_OFFSET_DOLLARS: float = 0.02  # Entry + $0.02
    STALE_ORDER_TIMEOUT_SECONDS: int = 180  # 3 minutes

    # Market Timings (Eastern Time)
    MARKET_TIMEZONE: str = "America/New_York"
    EOD_FLATTEN_TIME: str = "15:55"
    EARLY_CLOSE_FLATTEN_TIME: str = "12:55"

    # Telegram Bot Settings
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ENABLED: bool = False


settings = Settings()
