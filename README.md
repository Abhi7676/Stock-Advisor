# 🇮🇳 F&O AI Signal Advisor — Nifty 50 & Bank Nifty

A **100% free, real-time AI Options Trading Signal Advisor** for Nifty 50 & Bank Nifty F&O trading. Uses live NSE data, multi-factor technical analysis, and **Google Gemini AI** to generate clear **CALL / PUT / WAIT** signals with exact strike prices, premiums, and budget breakdown for your **₹10,000 capital**.

---

## ✨ Key Features

- 📡 **Live NSE Data**: Fetches real-time index LTP, options chain OI, Put-Call Ratio, and Max Pain directly from NSE's public API
- 🤖 **Google Gemini 3.8 Flash AI** (free tier, 1,500 req/day): Generates intelligent CALL/PUT signals by analyzing all factors together
- 📊 **Multi-Factor Analysis**:
  - **PCR (Put-Call Ratio)** — Bullish >1.2 | Bearish <0.8
  - **Max Pain** — Where option sellers want expiry
  - **OI Buildup Detection** — Call wall / Put wall resistance-support
  - **RSI (14)** — Overbought / Oversold momentum
  - **MACD** — Trend direction and momentum crossovers
  - **EMA (9, 21)** — Golden/Death cross detection
  - **Supertrend** — Buy/Sell trend signal
  - **Composite Bias Score** — All signals combined into -10 to +10 score
- 💰 **₹10,000 Budget Optimizer**: Tells you exact strike, how many lots, cost, 13% target premium, and 8% stop-loss (auto square-off at 3:00 PM IST close)
- ⏱️ **Auto-Refresh every 15 seconds** during market hours
- 📋 **Signal History Log**: All past CALL/PUT signals saved to Neon PostgreSQL / SQLite database
- 📲 **Telegram Signal Alerts**: Instant mobile notifications when BUY CALL / BUY PUT triggers with entry, +13% target, and stop loss!

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

### 3. Setup Telegram Alerts (100% Free)
1. Message **@BotFather** on Telegram &rarr; send `/newbot` &rarr; name your bot &rarr; copy the **Bot Token**.
2. Message **@userinfobot** on Telegram &rarr; copy your numeric **Chat ID**.
3. Open your new bot in Telegram and click **Start** (or send `/start`) so it can message you.
4. Set them in `.env` (or Render Dashboard &rarr; Environment):
   ```bash
   TELEGRAM_BOT_TOKEN="your_token_here"
   TELEGRAM_CHAT_ID="your_chat_id_here"
   ```

### 4. Launch the Application
```bash
python app.py
```

### 5. Open Dashboard & Test
Navigate to: **http://127.0.0.1:5000**
Click the **✈ Telegram: Active** pill in the top header to send a test alert directly to your phone!

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
- **Target Premium**: Exit when +13% profit on premium is made
- **Stop Loss**: Exit at -8% on premium (protect capital)
- **Market Close**: Auto square-off remaining open positions at 3:00 PM IST close
- **Lots**: How many lots with ₹5,000 risk allocation

---

## ⚙️ Customization

Edit `config.py` to change:
```python
USER_BUDGET_INR = 10000        # Your total capital
RISK_PER_TRADE_PCT = 0.5       # Max 50% per trade (₹5000)
PROFIT_TARGET_PCT = 0.13       # Target +13% on premium (stops immediately on hit)
STOP_LOSS_PCT = 0.08           # Stop at -8% loss on premium
MARKET_CLOSE = time(15, 0)     # 3:00 PM IST market close / auto square-off
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
