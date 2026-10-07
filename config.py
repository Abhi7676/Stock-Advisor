"""
Configuration for Indian F&O AI Signal Advisor
Nifty50 & BankNifty Options Trading Signal System
AI Engine: Google Gemini API (free tier — 1500 req/day)
"""
import os
from datetime import time
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DB_PATH env var lets Render (or any host) point to a persistent disk.
# On Render: set DB_PATH=/data/signals.sqlite (with 1GB persistent disk mounted at /data)
# Locally: falls back to signals.sqlite next to this file.
DB_FILE = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "signals.sqlite"))

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    _env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(_env_path):
        try:
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _k, _v = _k.strip(), _v.strip().strip("\"'")
                        if _k and _k not in os.environ:
                            os.environ[_k] = _v
        except Exception:
            pass

# ─── NSE Indices ──────────────────────────────────────────
INDICES = {
    "NIFTY": {
        "symbol": "NIFTY",
        "display_name": "Nifty 50",
        "lot_size": 65,
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
MARKET_CLOSE = time(15, 0)     # 3:00 PM IST — trading window closes & open trades square off
PRE_OPEN = time(9, 0)

# ─── User Budget Settings ─────────────────────────────────
USER_BUDGET_INR = 10000
RISK_PER_TRADE_PCT = 0.5    # Max 50% of budget in one trade = ₹5000
PROFIT_TARGET_PCT = 0.13    # Target 13% on premium — closes trade when 13% profit is made
STOP_LOSS_PCT = 0.08        # Stop at 8% loss on premium (tight capital protection)
ENTRY_THRESHOLD = 3         # Default composite bias score
ENTRY_THRESHOLDS = {
    "NIFTY": 3,             # Nifty 50: ±3 score fires high-conviction trades (proven 71.4% win rate)
    "BANKNIFTY": 5,         # Bank Nifty: ±5 required — high-beta index whipsaws hard, need all 5 factors aligned
}

# ─── Google Gemini API Settings (Free Tier) ──────────────────
# Get your free API key from: https://aistudio.google.com/app/apikey
# Standard production models have 1,500 requests/day & 15 req/min on free tier
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", None)
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", None)
GEMINI_API_KEY_3 = os.environ.get("GEMINI_API_KEY_3", None)

def _normalize_gemini_model(model_name: str | None) -> str:
    """Normalize model string to ensure valid, active Gemini model ID."""
    if not model_name:
        return "gemini-flash-latest"
    m = model_name.strip()
    m_clean = m.lower().replace(" ", "-").replace("_", "-")
    if any(k in m_clean for k in ("flash-lite-latest", "flash-lite")):
        return "gemini-flash-lite-latest"
    if any(k in m_clean for k in ("3.5-flash-lite", "3.5flash-lite")):
        return "gemini-3.5-flash-lite"
    if any(k in m_clean for k in ("3.5flash", "3.5-flash", "3.5")):
        return "gemini-3.5-flash"
    if any(k in m_clean for k in ("3.8flash", "3.8-flash", "3.8")):
        return "gemini-3.8-flash"
    if any(k in m_clean for k in ("3.6flash", "3.6-flash", "3.6")):
        return "gemini-3.6-flash"
    if any(k in m_clean for k in ("1.5-flash", "2.0-flash", "flash-latest", "flash")):
        return "gemini-flash-latest"
    return m

_raw_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_MODEL = _normalize_gemini_model(_raw_model)
GEMINI_FALLBACK_MODELS = [
    "gemini-3.5-flash",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.8-flash",
]
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
