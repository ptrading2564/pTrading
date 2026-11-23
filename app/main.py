"""pTrading Main FastAPI Application Entrypoint."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.brokers.alpaca_adapter import AlpacaAdapter
from app.config import settings
from app.db import init_db
from app.routers.dashboard import router as dashboard_router
from app.routers.webhook import router as webhook_router
from app.services.lifecycle_worker import LifecycleWorker
from app.services.market_scanner import get_market_scanner

# Configure logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ptrading")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages application startup and shutdown lifecycle."""
    logger.info("Initializing pTrading system...")
    # Initialize SQLite database and tables
    await init_db()
    logger.info("Database initialized with WAL mode.")

    # Initialize and launch LifecycleWorker background task
    broker = AlpacaAdapter()
    lifecycle_worker = LifecycleWorker(broker)
    worker_task = asyncio.create_task(lifecycle_worker.start(poll_interval_seconds=5.0))
    logger.info("LifecycleWorker background monitoring loop launched.")

    # Initialize and launch MarketScanner background task (Option 3)
    scanner_task = None
    if settings.SCANNER_ENABLED:
        market_scanner = get_market_scanner()
        scanner_task = asyncio.create_task(market_scanner.start())
        logger.info("MarketScanner background monitoring loop launched (Option 3 server-side scanning).")

    yield

    logger.info("Shutting down pTrading system...")
    if scanner_task:
        get_market_scanner().stop()
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass

    lifecycle_worker.stop()
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutdown complete.")


app = FastAPI(
    title="pTrading",
    description="Automated Intraday Momentum Breakout Trading System for US Equities",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(dashboard_router)
app.include_router(webhook_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
