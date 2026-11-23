# pTrading 🚀

**pTrading** is an automated, high-probability intraday momentum breakout trading system for US Equities (`NASDAQ` / `NYSE`).

Designed with strict capital preservation and compounding principles:
- **Broker Integration:** Alpaca via `alpaca-py` (Paper & Live) with extensible `BrokerAdapter`.
- **Trading Hours:** Regular US Trading Hours (`09:30–16:00 ET`).
- **Holding Horizon:** Pure Intraday — all open positions flattened at `15:55 ET` (or `12:55 ET` on early-close days) for **zero overnight gap risk**.
- **Edge:** 15-minute Opening Range breakouts gated by SPY/QQQ VWAP trend filters and 1.5x RVOL volume confirmation.
- **Order Structure:** Atomic Marketable-Limit Bracket Orders (`Stop-Loss = max(OR_Low, Entry - 1.5*ATR)` and `Take-Profit = +2.0R`).
- **Stop Stepping:** Server-side trailing stop moves to **Breakeven (`Entry + $0.02`) at `+1.0R`**.
- **Risk & Sizing:** Dynamic **`0.5%` live equity compounding** per trade, max **`4` concurrent positions**, max **`25%` single-stock capital limit**, and **`-2.0%` daily max loss circuit breaker**.
- **Scanning Layer:** Pine Script v6 (`ptrading_basket_scanner.pine`) scanning top 38 stocks from ChartMill screener + SPY & QQQ (40 security calls maximum).
- **Control:** Password-protected responsive Web Dashboard (`Auto` | `Semi-Auto` | `Paused` + **Emergency Kill-Switch**) and Telegram Bot push notifications.

---

## Repository Structure

```text
pTrading/
├── pyproject.toml
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── deploy/
│   ├── Caddyfile
│   └── VPS_SETUP.md
├── docs/
│   └── chartmill_screener_guide.md
├── pine/
│   ├── ptrading_basket_scanner.pine      # Pine v6 38-symbol scanner + JSON array webhook
│   └── ptrading_single_backtest.pine     # Pine v6 companion visual backtester
├── app/
│   ├── main.py
│   ├── config.py
│   ├── db.py                             # Async SQLite (WAL mode)
│   ├── brokers/
│   │   ├── base.py                       # Abstract BrokerAdapter (Alpaca, IBKR)
│   │   └── alpaca_adapter.py             # alpaca-py bracket orders & calendar
│   ├── services/
│   │   ├── chartmill_importer.py         # CSV cleaner, earnings filter, top 38 ranker
│   │   ├── risk_manager.py               # 0.5% compounding sizing, 4-pos cap, -2% breaker
│   │   ├── lifecycle_worker.py           # +1R breakeven step, 3m expiry, 15:55 ET flatten
│   │   └── telegram_bot.py               # Push alerts & inline Semi-Auto confirm
│   ├── routers/
│   │   ├── webhook.py                    # POST /webhook/tradingview (secret + IP filter)
│   │   └── dashboard.py                  # Web UI, CSV upload, mode toggle, kill-switch
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       └── analytics.html
└── tests/
    ├── test_chartmill_importer.py
    ├── test_risk_manager.py
    └── test_webhook_and_lifecycle.py
```

---

## Local Development & Testing

1. **Install Dependencies**:
   ```bash
   pip install -e ".[dev]"
   ```

2. **Run Pytest Suite**:
   ```bash
   pytest -v
   ```

3. **Start Application Locally**:
   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   ```

4. **Access Dashboard**:
   Open `http://127.0.0.1:8000/` (default credentials in `.env.example`: `admin` / `adminpassword123`).

---

## VPS Cloud Deployment

Refer to [`deploy/VPS_SETUP.md`](deploy/VPS_SETUP.md) for full instructions on deploying to a dedicated Ubuntu 24.04 VPS with Docker Compose and Caddy automatic Let's Encrypt HTTPS.
