"""
Configuration for Indian F&O AI Signal Advisor
Nifty50 & BankNifty Options Trading Signal System
AI Engine: Google Gemini API (free tier — 1500 req/day)
"""
import os
from datetime import time
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "signals.sqlite")

# Load environment variables from .env file
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ─── NSE Indices ──────────────────────────────────────────
INDICES = {
    "NIFTY": {
        "symbol": "NIFTY",
        "display_name": "Nifty 50",
        "lot_size": 75,
        "yf_symbol": "^NSEI",
    },
    "BANKNIFTY": {
        "symbol": "BANKNIFTY",
        "display_name": "Bank Nifty",
        "lot_size": 30,
        "yf_symbol": "^NSEBANK",
    },
}

# ─── NSE API Base URLs ─────────────────────────────────────
NSE_BASE_URL = "https://www.nseindia.com"
NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NSE_CONTRACT_INFO_URL = "https://www.nseindia.com/api/option-chain-contract-info"
NSE_OPTION_CHAIN_V3_URL = "https://www.nseindia.com/api/option-chain-v3"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
}

# ─── Technical Indicator Settings ─────────────────────────
TA_PARAMS = {
    "RSI_PERIOD": 14,
    "EMA_FAST": 9,
    "EMA_SLOW": 21,
    "MACD_FAST": 12,
    "MACD_SLOW": 26,
    "MACD_SIGNAL": 9,
    "SUPERTREND_PERIOD": 10,
    "SUPERTREND_MULT": 3.0,
    "ATR_PERIOD": 14,
    "LOOKBACK_CANDLES": 60,   # How many 5-min candles to analyze
}

# ─── PCR Signal Thresholds ─────────────────────────────────
PCR_THRESHOLDS = {
    "STRONG_BULLISH": 1.4,
    "BULLISH": 1.1,
    "NEUTRAL_HIGH": 1.05,
    "NEUTRAL_LOW": 0.95,
    "BEARISH": 0.9,
    "STRONG_BEARISH": 0.7,
}

# ─── Market Hours (IST) ────────────────────────────────────
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_OPEN = time(9, 0)

# ─── User Budget Settings ─────────────────────────────────
USER_BUDGET_INR = 10000
RISK_PER_TRADE_PCT = 0.5    # Max 50% of budget in one trade = ₹5000
PROFIT_TARGET_PCT = 0.15    # Target 15% on premium — realistic in the 9:30–11:45 morning window
STOP_LOSS_PCT = 0.08        # Stop at 8% loss on premium (tight capital protection)
ENTRY_THRESHOLD = 3         # Minimum composite bias score (3 = balanced: signals fire regularly)
                            # 3 → fires when 3+ indicators align (PCR + OI + 1 technical)
                            # 4 → more selective, fewer signals but higher conviction
                            # 5 → ultra-selective, rarely fires (not recommended for testing)

# ─── Google Gemini API Settings (Free Tier) ──────────────────
# Get your free API key from: https://aistudio.google.com/app/apikey
# Standard production models have 1,500 requests/day & 15 req/min on free tier
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", None)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")    # Active Gemini model
GEMINI_FALLBACK_MODELS = ["gemini-1.5-flash", "gemini-2.0-flash-exp"]
GEMINI_TIMEOUT = 30   # seconds

# ─── Execution Costs / Slippage (for backtesting realism) ────
# Estimated round-trip execution fees as a fraction of trade value (e.g. 0.01 = 1%)
TRADING_FEE_PCT = float(os.environ.get("TRADING_FEE_PCT", 0.005))
# Estimated one-way slippage fraction applied to premiums (e.g. 0.01 = 1%)
SLIPPAGE_PCT = float(os.environ.get("SLIPPAGE_PCT", 0.01))

# ─── NSE API & Refresh Settings ──────────────────────────────
NSE_REFRESH_INTERVAL = 15   # Refresh live market data every 15 seconds!

# ─── Flask Server ──────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))
DEBUG = False

# ─── Data Refresh ─────────────────────────────────────────
# How often the backend re-fetches NSE data (seconds)
NSE_REFRESH_INTERVAL = 60
# How often the frontend polls for new signals (seconds)
FRONTEND_POLL_INTERVAL = 60

# ─── Number of strikes to display around ATM ──────────────
STRIKES_AROUND_ATM = 8
