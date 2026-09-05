# 🇮🇳 F&O AI Signal Advisor — Nifty 50 & Bank Nifty

A **100% free, real-time AI Options Trading Signal Advisor** for Nifty 50 & Bank Nifty F&O trading. Uses live NSE data, multi-factor technical analysis, and **Google Gemini AI** to generate clear **CALL / PUT / WAIT** signals with exact strike prices, premiums, and budget breakdown for your **₹10,000 capital**.

---

## ✨ Key Features

- 📡 **Live NSE Data**: Fetches real-time index LTP, options chain OI, Put-Call Ratio, and Max Pain directly from NSE's public API
- 🤖 **Google Gemini AI** (free tier, 1,500 req/day): Generates intelligent CALL/PUT signals by analyzing all factors together
- 📊 **Multi-Factor Analysis**:
  - **PCR (Put-Call Ratio)** — Bullish >1.2 | Bearish <0.8
  - **Max Pain** — Where option sellers want expiry
  - **OI Buildup Detection** — Call wall / Put wall resistance-support
  - **RSI (14)** — Overbought / Oversold momentum
  - **MACD** — Trend direction and momentum crossovers
  - **EMA (9, 21)** — Golden/Death cross detection
  - **Supertrend** — Buy/Sell trend signal
  - **Composite Bias Score** — All signals combined into -10 to +10 score
- 💰 **₹10,000 Budget Optimizer**: Tells you exact strike, how many lots, cost, 30% target premium, and 40% stop-loss
- ⏱️ **Auto-Refresh every 60 seconds** during market hours
- 📋 **Signal History Log**: All past CALL/PUT signals saved to SQLite database

---

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install flask flask-cors pandas numpy requests yfinance google-generativeai pandas-ta
```

### 2. Get Your FREE Gemini API Key (Optional but Recommended)
> Visit: **https://aistudio.google.com/app/apikey** (sign in with Google, click "Create API Key")
> Free tier: 1,500 requests/day — completely free!

Open `config.py` and paste your key:
```python
GEMINI_API_KEY = "AIza..."  # Your key here
```

> **Without a key**: The app still works using rule-based signal logic (PCR + RSI + MACD + Supertrend scoring). Still very useful!

### 3. Launch the Application
```bash
python app.py
```

### 4. Open Dashboard
Navigate to: **http://127.0.0.1:5000**

---

## 📁 File Structure

```
Stock Market/
├── config.py              # Settings: API key, budget, risk params, indicators
├── nse_data.py            # Live NSE data fetcher (index LTP, options chain, candles)
├── analysis_engine.py     # Multi-factor analysis: PCR, OI, RSI, MACD, Supertrend, S&R
├── gemini_advisor.py      # Google Gemini AI signal generator with rule-based fallback
├── app.py                 # Flask REST API backend
├── index.html             # Web dashboard UI
├── style.css              # Dark-mode glassmorphism design system
├── app.js                 # Real-time polling, chart rendering, options chain display
├── requirements.txt       # Python dependencies
└── signals.sqlite         # Auto-created: Signal history database
```

---

## 🌐 API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/signal/NIFTY` | AI CALL/PUT signal for Nifty 50 |
| `GET /api/signal/BANKNIFTY` | AI CALL/PUT signal for Bank Nifty |
| `GET /api/options-chain/NIFTY` | Live options chain OI data |
| `GET /api/options-chain/BANKNIFTY` | Live BankNifty options chain |
| `GET /api/market-pulse` | Quick summary of both indices |
| `GET /api/signal-history` | Last 20 saved signals |
| `GET /api/gemini-status` | Gemini API status check |
| `POST /api/refresh/NIFTY` | Force-refresh analysis now |

---

## 📊 Signal Interpretation

| Signal | Meaning | Action on Groww |
|--------|---------|----------------|
| 🟢 **BUY CALL** | Market expected to go UP | Buy CE (Call) option at shown strike |
| 🔴 **BUY PUT** | Market expected to go DOWN | Buy PE (Put) option at shown strike |
| 🟡 **WAIT** | No clear direction | Avoid trading — wait for signal |

### Budget Details Shown Per Signal
- **Strike**: Which CE/PE to buy (ATM or nearby)
- **Expiry**: Nearest weekly expiry date
- **Entry Premium**: Suggested entry price
- **Target Premium**: Exit at +30% on premium (take profit)
- **Stop Loss**: Exit at -40% on premium (protect capital)
- **Lots**: How many lots with ₹5,000 risk allocation

---

## ⚙️ Customization

Edit `config.py` to change:
```python
USER_BUDGET_INR = 10000        # Your total capital
RISK_PER_TRADE_PCT = 0.5       # Max 50% per trade (₹5000)
PROFIT_TARGET_PCT = 0.30       # Target +30% on premium
STOP_LOSS_PCT = 0.40           # Stop at -40% loss on premium
NSE_REFRESH_INTERVAL = 60      # Refresh interval in seconds
```

---

## ⚠️ Disclaimer

> F&O options trading involves **substantial risk of loss**. The signals generated are NOT SEBI-registered investment advice. Always use your own judgment, understand the risks, and never trade more than you can afford to lose.

---

## 🆓 Cost Breakdown

| Component | Cost |
|-----------|------|
| NSE Market Data | Free (public API) |
| Google Gemini AI | Free (1,500 req/day) |
| Python Libraries | Free (open source) |
| Signal History DB | Free (SQLite local) |
| **Total** | **₹0 / month** |
