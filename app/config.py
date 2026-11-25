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
    DASHBOARD_USERNAME: str = "p.trading2564@gmail.com"
    DASHBOARD_PASSWORD: str = "adminpassword123"
    SESSION_SECRET_KEY: str = "secret-key-32-bytes-minimum-trading-session"

    # Google OAuth 2.0 Settings
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    ALLOWED_GOOGLE_EMAILS: str = "p.trading2564@gmail.com"
    GOOGLE_OAUTH_ENABLED: bool = True

    @property
    def allowed_google_emails_set(self) -> Set[str]:
        return {
            email.strip().lower()
            for email in self.ALLOWED_GOOGLE_EMAILS.split(",")
            if email.strip()
        }

    # Alpaca API Credentials
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_PAPER: bool = True
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"

    # Trading Execution Mode
    EXECUTION_MODE: ExecutionMode = ExecutionMode.SEMI_AUTO

    # Risk Management Parameters
    RISK_PERCENT_PER_TRADE: float = 0.5  # 0.5% equity per trade
    MAX_CONCURRENT_POSITIONS: int = 4  # Max at-risk positions (2.0% max open heat)
    MAX_TOTAL_POSITIONS_WITH_BREAKEVEN: int = 6  # Hard cap when extra slots are freed by +1R breakeven trades
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

    # Server-Side Market Scanner (Option 3)
    SCANNER_ENABLED: bool = True
    SCANNER_POLL_INTERVAL_SECONDS: int = 30
    ALPACA_DATA_FEED: str = "iex"  # "iex" (free) or "sip" (paid)
    SCANNER_MIN_RVOL: float = 1.5
    SCANNER_MAX_ATR_EXTENSION: float = 0.75
    DEFAULT_BASKET: str = (
        "NVDA,TSLA,AMD,PLTR,SMCI,ARM,AVGO,META,AMZN,MSFT,AAPL,GOOGL,NFLX,COIN,HOOD,MSTR,"
        "APP,RDDT,CAVA,DKNG,UBER,CRWD,PANW,SNOW,DDOG,NET,ZS,CELH,ONON,DECK,ANET,VRT,GEV,"
        "CEG,VST,DUOL,HIMS,BROS"
    )

    # Telegram Bot Settings
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ENABLED: bool = False

    # Daily Analytics & Strategy Review Settings
    DAILY_REPORT_DELAY_MINUTES: int = 5  # Minutes after market close to generate EOD report
    MONTE_CARLO_SIMULATIONS: int = 5000
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.8-flash"


settings = Settings()
