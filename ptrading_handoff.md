# pTrading — Standalone Project Handoff & Kickoff Pack

> [!IMPORTANT]
> **How to transfer this to your new Antigravity account:** Copy or save this single file (`ptrading_handoff.md`) into your new project folder before switching accounts, then paste the **Kickoff Prompt** (at the bottom of this document) into the new Antigravity chat. All references to your existing SaaS VPS and domain have been removed.

---

## 1. Locked-In Trading & Architecture Decisions

| Area | Agreed Specification |
| :--- | :--- |
| **Target Market** | US Equities (`NASDAQ` / `NYSE`), Regular Trading Hours (`09:30–16:00 ET` / `14:30–21:00 UK`). |
| **Broker** | **Alpaca** first via `alpaca-py` (starting with **\$50,000 Paper Account**; clean `BrokerAdapter` abstraction so Interactive Brokers `IBKR` can be added later). |
| **Holding Horizon** | **Pure Intraday Day Trading** — flat before every close (`15:55 ET` / `12:55 ET` on early-close days) for zero overnight gap risk. |
| **Core Edge** | **Long-Only Momentum Breakouts** gated by a `SPY` / `QQQ` > `VWAP` market health filter. |
| **Candidate Screening** | **ChartMill** EOD / pre-market screen (`Price >= \$10`, `Avg Vol >= 1M`, `ATR% >= 2.5%`, `Tech Rating >= 7`, `Setup Rating >= 7`, `RS >= 75`, `Price > SMA20 > SMA50`). Python CSV importer strips stocks with earnings today/tomorrow, ranks setups, and caps the daily basket at **38 stocks**. |
| **TradingView Layer** | **Pine Script v6** (`ptrading_basket_scanner.pine`) using `input.text_area()` + dynamic `request.security()` loop (38 basket stocks + `AMEX:SPY` + `NASDAQ:QQQ` = 40 max security calls) on a `5m` chart. Plus a companion single-symbol `strategy()` (`ptrading_single_backtest.pine`) for visual backtesting. |
| **Intraday Entry Rule** | Measure **15-minute Opening Range (`09:30–09:45 ET`)**. From `09:45–15:00 ET`, trigger on `5m` bar close when `close > OR_High`, `close > VWAP`, `RVOL >= 1.5x`, bullish bar close, and extension `<= 0.75 * ATR(14)`. Fires a **single consolidated JSON array webhook** per bar close (`alert.freq_once_per_bar_close`). |
| **Exit & Stop Lifecycle** | **Atomic Alpaca Marketable-Limit Bracket Order** on entry (`Stop-Loss = max(OR_Low, Entry - 1.5*ATR)` + `Take-Profit = +2.0R`). Python steps server-side Stop-Loss leg to **Breakeven (`Entry + \$0.02`) at `+1.0R`** via `replace_order_by_id`, cancels unfilled parent entries after `3 minutes`, and flattens all remaining open positions at **`15:55 ET`**. |
| **Compounding & Risk** | Dynamic **`0.5%` of live equity** risked per trade (\$250 initial `1R` on \$50k bank, auto-compounding profits), max **`4` concurrent positions**, max **`25%` equity** per single stock, and **`-2.0%` daily max loss circuit breaker**. |
| **Control & Mobility** | Password-protected responsive **Web Dashboard** (`Auto-Execute` \| `Semi-Auto Confirm` \| `Paused` + **Emergency Flatten Kill-Switch**) + **Telegram Bot** mobile push alerts & inline confirm/flatten commands. |
| **Browser Automation** | Use Antigravity `/browser` on logged-in Chrome sessions to configure/save the ChartMill screen & custom CSV view, compile/save both Pine v6 scripts in TradingView, and optionally automate the morning basket sync. |

---

## 2. Dedicated Infrastructure Blueprint (Decoupled VPS & Domain)

Because `pTrading` will run on its **own dedicated DigitalOcean Droplet** and **standalone domain**:

1. **Droplet Sizing:** Ubuntu `24.04 LTS` in `lon1` or `nyc1`/`nyc3` (`nyc` shaves ~70 ms off round-trip latency to Alpaca's US-East servers, though either works great for 5m bar trading) — Basic Regular SSD **1 vCPU / 1 GB RAM (\$6/mo)** or **1 vCPU / 2 GB RAM (\$12/mo)**.
2. **Firewall (`ufw`):** Allow only `22/tcp` (SSH key-only), `80/tcp`, and `443/tcp`.
3. **Reverse Proxy & Automatic TLS:** Host **Caddy** (zero-config Let's Encrypt HTTPS) routing `https://<your-trading-domain>` -> `http://127.0.0.1:8000`.
4. **DNS:** Point `<your-trading-domain>` (or `app.<your-trading-domain>`) DNS `A` record to the new Droplet's IP (DNS-only / unproxied recommended so TradingView's webhook source IPs `52.89.214.238`, `34.212.75.30`, `54.218.53.128`, `52.32.178.7` reach Caddy/FastAPI directly for IP allowlisting alongside the cryptographic `WEBHOOK_SECRET`).
5. **Container Layout (`/opt/ptrading`):**
   - `FastAPI + Uvicorn + APScheduler + alpaca-py` in Docker Compose (`127.0.0.1:8000:8000`).
   - Embedded **SQLite (`WAL` mode)** persisted at `/opt/ptrading/data/ptrading.db` (with optional daily automated backup snapshot).

---

## 3. Repository Structure to Build in the New Account

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
│   ├── db.py                             # Async SQLite (WAL)
│   ├── brokers/
│   │   ├── base.py                       # Abstract BrokerAdapter (Alpaca now, IBKR later)
│   │   └── alpaca_adapter.py             # Bracket orders, replace_order_by_id, clock/calendar
│   ├── services/
│   │   ├── chartmill_importer.py         # CSV cleaner, earnings filter, composite ranker (top 38)
│   │   ├── risk_manager.py               # 0.5% compounding sizing, 4-pos cap, -2% daily breaker
│   │   ├── lifecycle_worker.py           # +1R breakeven step, 3m stale order expiry, 15:55 ET flatten
│   │   └── telegram_bot.py               # Push alerts + inline Semi-Auto & /flatten commands
│   ├── routers/
│   │   ├── webhook.py                    # POST /webhook/tradingview (secret + IP allowlist + idempotency)
│   │   └── dashboard.py                  # Auth, CSV upload, mode toggle, kill-switch, analytics
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

## 4. Pre-Flight Accounts & Keys Checklist (For Your New Setup)

Before going live in Paper Trading on the new setup, have these ready:
- [ ] **Standalone Domain** registered + DNS `A` record pointed at your new dedicated DigitalOcean Droplet IP.
- [ ] **Dedicated DigitalOcean Droplet** (Ubuntu 24.04 LTS; consider `nyc1`/`nyc3` for closest proximity to US exchanges/Alpaca, or `lon1` for snappiest UK dashboard UI—both are sub-100ms).
- [ ] **Alpaca Account** -> Paper Trading account initialized/reset to **\$50,000** (`ALPACA_API_KEY` & `ALPACA_SECRET_KEY`).
- [ ] **TradingView Account** on **Essential** tier or higher (required for outgoing Webhook URLs).
- [ ] **ChartMill Account** ready to log in via Chrome (`/browser`).
- [ ] **Telegram Bot** created via `@BotFather` (`TELEGRAM_BOT_TOKEN` + your personal `TELEGRAM_CHAT_ID`).

---

## 5. Ready-to-Paste Kickoff Prompt (For Your New Antigravity Account)

Once you open your new project workspace in your separate Antigravity account and drop `ptrading_handoff.md` into that workspace root, send this prompt:

```text
Please read @ptrading_handoff.md in the workspace root. We have already completed the full architecture and requirements discovery for pTrading.

Let's execute this in three phases:
1. Build and verify the full Python/FastAPI application, Pine Script v6 files (`pine/`), Docker/Caddy deployment configs (`deploy/`), and automated `pytest` suite locally.
2. Guide/assist deploying the stack to my dedicated DigitalOcean Ubuntu VPS and verifying HTTPS + `/health` + Telegram notifications.
3. Use `/browser` with my logged-in Chrome session to set up and save the ChartMill Momentum screen/CSV view and load/compile both Pine Script v6 scripts + webhook alert in TradingView.

Start with `/plan` briefly if you want to confirm my domain name and VPS IP first, or proceed straight to Phase 1 once confirmed.
```
