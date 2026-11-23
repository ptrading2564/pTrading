# ChartMill Momentum Breakout Screener Configuration Guide

This guide details how to configure and save the ChartMill screener preset and custom CSV export view for pTrading.

---

## 1. Screener Filter Criteria

In ChartMill Screener (`https://www.chartmill.com/stock-screener`):

| Filter Category | Parameter | Setting | Rationale |
| :--- | :--- | :--- | :--- |
| **General / Liquidity** | Exchange | US Only (`NASDAQ`, `NYSE`, `AMEX`) | Focus on primary liquid US equity markets. |
| | Price | `>= $10.00` | Eliminates penny stocks and irrational spread slippage. |
| | Average Volume (50d) | `>= 1,000,000 shares` | Guarantees institutional liquidity for instant limit fills. |
| **Volatility** | ATR% (14d) | `>= 2.5%` | Ensures sufficient intraday range for +2.0R expansions. |
| **Technical Health** | Technical Rating | `>= 7` | Ensures stock is in an established upward trend. |
| | Moving Averages | `Price > SMA20` and `SMA20 > SMA50` | Classic trend stack alignment. |
| **Momentum & Strength**| Relative Strength (RS) | `>= 75` | Outperforming 75%+ of the broad market. |
| **Setup Quality** | Setup Rating | `>= 7` | Proprietary consolidation base near potential breakout. |

---

## 2. Custom Table & CSV Export View Columns

To ensure the CSV export contains all fields needed by `pTrading`:

1. Click on the **Custom Table** / **Views** dropdown.
2. Ensure the following columns are selected in your view:
   - `Ticker` (Symbol)
   - `Company Name`
   - `Price` (Last Close)
   - `Average Volume`
   - `ATR %`
   - `Technical Rating`
   - `Setup Rating`
   - `Relative Strength`
   - `Next Earnings Date`
3. Save this view preset as **"pTrading Morning View"**.

---

## 3. Daily Workflow

1. **Pre-market (08:30–09:15 ET)**:
   - Open ChartMill saved screen.
   - Click **Export to CSV**.
2. **Import into pTrading**:
   - Navigate to your pTrading Web Dashboard (`https://<your-trading-domain>/`).
   - In the **ChartMill Pre-Market CSV Importer** box, upload the exported `.csv`.
   - The importer automatically strips out any tickers reporting earnings today or tomorrow, ranks the candidates by composite score, and displays the top 38 basket.
3. **Sync to TradingView**:
   - Click **Copy Basket to Clipboard**.
   - Open your TradingView chart with `ptrading_basket_scanner.pine`.
   - Paste the tickers into the scanner basket inputs (or update default settings).
