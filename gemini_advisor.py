"""
gemini_advisor.py — Google Gemini API Integration (Free Tier)
Sends structured market analysis to Gemini and returns a
structured CALL/PUT/WAIT signal with full reasoning.

Get your FREE API key at: https://aistudio.google.com/app/apikey
Free tier: 1,500 requests/day | 15 req/min — more than enough!
"""

import json
import logging
import os
import time

import config

logger = logging.getLogger(__name__)

# Track last Gemini/API error for status reporting and UI
_last_error = None
_last_error_time = 0.0


def _is_market_open() -> bool:
    """Returns True if current IST time is within NSE market hours (9:15–15:30) on a weekday."""
    from datetime import datetime
    now = datetime.now()
    day = now.weekday()  # 0=Mon, 6=Sun
    if day >= 5:  # Weekend
        return False
    h, m = now.hour, now.minute
    mins = h * 60 + m
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)


def _market_closed_signal(analysis: dict) -> dict:
    """Returns a WAIT signal when market is closed — never recommend trades outside market hours."""
    symbol = analysis["symbol"]
    ltp = analysis.get("ltp", 0)
    atm = analysis.get("atm_strike", 0)
    lot_size = config.INDICES[symbol]["lot_size"]
    premium = analysis.get("atm_call_ltp", 0)

    return {
        "signal": "WAIT", "confidence": 0,
        "strike": atm, "option_type": "NONE",
        "entry_premium": premium,
        "target_premium": round(premium * (1 + config.PROFIT_TARGET_PCT), 2) if premium else 0,
        "stop_loss_premium": round(premium * (1 - config.STOP_LOSS_PCT), 2) if premium else 0,
        "lots_recommended": 0, "estimated_cost_inr": 0,
        "max_profit_inr": 0, "max_loss_inr": 0,
        "lot_size": lot_size,
        "reasoning": (
            "MARKET CLOSED — NO TRADING:\n"
            "• NSE market hours are 9:15 AM – 3:30 PM IST (Mon–Fri)\n"
            "• Never enter positions outside market hours\n"
            "• Signals will activate during next trading session"
        ),
        "key_risk": "Market is closed. Wait for next session.",
        "market_bias": "NEUTRAL",
        "trade_tip": "Review today's signals and prepare your watchlist for tomorrow.",
        "holding_time": "0 Mins — Market Closed",
        "action_summary": "MARKET CLOSED — No trades until next session",
        "exit_rule": "No active positions outside market hours",
        "symbol": symbol, "nearest_expiry": analysis.get("nearest_expiry", "N/A"),
        "atm_strike": atm, "ltp": ltp,
        "bias_score": analysis.get("bias_score", 0),
        "data_source": analysis.get("data_source", "unknown"),
        "source": "market_closed",
        "llm_model": "Market Closed",
        "llm_inference_time": 0,
        "trading_window": "Market Closed (NSE: 9:15–15:30 IST)",
        "consecutive_losses": 0,
    }


def get_last_error() -> dict | None:
    """Return the last GEMINI API error (if any) as a dict for status reporting."""
    global _last_error, _last_error_time
    if not _last_error:
        return None
    return {"error": _last_error, "time": _last_error_time}

# ─── Check Gemini availability ───────────────────────────────
def _get_api_key() -> str | None:
    """Returns Gemini API key preferring the environment variable first."""
    # Prefer explicit environment variable to avoid accidental commits of keys
    key = os.environ.get("GEMINI_API_KEY") or config.GEMINI_API_KEY
    if not key:
        return None
    try:
        return key.strip()
    except Exception:
        return None


def is_gemini_available() -> bool:
    key = _get_api_key()
    if not key:
        return False
    try:
        from google import genai as gai  # noqa
        return True
    except ImportError:
        pass
    try:
        import google.generativeai  # noqa
        return True
    except ImportError:
        return False


def get_gemini_model_name() -> str:
    return config.GEMINI_MODEL if is_gemini_available() and _get_api_key() else "N/A"


# ─── Prompt Builder (same logic as ollama_advisor) ───────────
def _build_prompt(analysis: dict) -> str:
    symbol = analysis["display_name"]
    ltp = analysis["ltp"]
    change_pct = analysis["change_pct"]
    pcr = analysis["pcr"]
    pcr_signal = analysis["pcr_signal"].replace("_", " ")
    max_pain = analysis["max_pain"]
    oi = analysis["oi_analysis"]
    ta = analysis["ta"]
    support = analysis["support"]
    resistance = analysis["resistance"]
    budget = analysis["budget_advice"]
    bias_score = analysis["bias_score"]
    bias_factors = analysis["bias_factors"]
    expiry = analysis.get("nearest_expiry", "N/A")
    call_premium = analysis.get("atm_call_ltp", 0)
    put_premium = analysis.get("atm_put_ltp", 0)
    atm = analysis.get("atm_strike", ltp)
    call_budget = budget.get("call", {})
    put_budget = budget.get("put", {})

    factor_lines = "\n".join(
        f"  - {f['factor']}: {f['signal']} (score: {f['points']:+d}) — {f['desc']}"
        for f in bias_factors
    )

    return f"""You are an expert Indian stock market F&O (Futures & Options) trader specializing in Nifty 50 and Bank Nifty options. A trader with ₹10,000 budget wants your expert tip.

LIVE MARKET DATA — {symbol}
═══════════════════════════════════════════
• Spot LTP: ₹{ltp:,} | Today: {change_pct:+.2f}%
• Nearest Expiry: {expiry} | ATM Strike: {atm}

OPTIONS CHAIN:
• PCR: {pcr} → {pcr_signal}
• Max Pain: {max_pain}
• Call Wall (Resistance): {oi['call_wall']}
• Put Wall (Support): {oi['put_wall']}
• OI Range: {oi['oi_range']}
• OI Bias: {oi['oi_bias']}
• Call OI Added: {oi['call_oi_addition']:,} | Put OI Added: {oi['put_oi_addition']:,}

TECHNICAL INDICATORS (5-min):
• RSI(14): {ta['rsi']} → {ta['rsi_signal']}
• MACD: {ta['macd']} / Signal: {ta['macd_signal']} → {ta['macd_bias']}
• EMA(9): {ta['ema_fast']} / EMA(21): {ta['ema_slow']} → {ta['ema_crossover']} CROSS
• Supertrend: {ta['supertrend_signal']} ({ta['supertrend_trend']})

KEY LEVELS: Support ₹{support} | Resistance ₹{resistance}

COMPOSITE SIGNAL (score {bias_score:+}/±10):
{factor_lines}

ATM PREMIUMS ({expiry}):
• {atm} CE: ₹{call_premium} | Lots with ₹5000: {call_budget.get('lots',0)} | Cost: ₹{call_budget.get('cost_inr',0)}
• {atm} PE: ₹{put_premium} | Lots with ₹5000: {put_budget.get('lots',0)} | Cost: ₹{put_budget.get('cost_inr',0)}

Respond ONLY with this JSON (no markdown, no extra text):
{{
  "signal": "BUY_CALL" or "BUY_PUT" or "WAIT",
  "confidence": <integer 1-100>,
  "strike": <recommended strike as integer>,
  "option_type": "CE" or "PE" or "NONE",
  "entry_premium": <entry premium in rupees>,
  "target_premium": <target premium for profit>,
  "stop_loss_premium": <stop loss premium level>,
  "estimated_cost_inr": <total cost for 1 lot>,
  "reasoning": "WHY BUY CALL (or PUT / WAIT):\n• PCR: <exact PCR & interpretation>\n• OI Walls: <Support and Resistance walls>\n• Technicals: <RSI, MACD, Supertrend signals>\n• Risk/Reward: <Target, SL, and Capital Protection>",
  "key_risk": "<1 sentence about the main risk>",
  "market_bias": "BULLISH" or "BEARISH" or "NEUTRAL",
  "trade_tip": "<1 practical tip for Groww F&O>"
}}"""


_signal_cache = {}


def get_signal(analysis: dict) -> dict:
    """
    Generates CALL/PUT signal using Google Gemini API.
    Falls back to rule-based signal if API unavailable or quota exceeded.
    Caches LLM result for 3 minutes to stay within rate limits.
    Forces WAIT when market is closed or outside trading windows.
    """
    symbol = analysis["symbol"]
    now = time.time()

    # ── Gate 0: Market Hours Check ────────────────────────
    # Never return BUY signals when market is closed
    if not _is_market_open():
        return _market_closed_signal(analysis)

    if symbol in _signal_cache and (now - _signal_cache[symbol]["time"]) < 180:
        cached_sig = _signal_cache[symbol]["signal"].copy()
        # Update live spot and ATM
        cached_sig["ltp"] = analysis.get("ltp", cached_sig.get("ltp"))
        cached_sig["atm_strike"] = analysis.get("atm_strike", cached_sig.get("atm_strike"))
        # Override cached BUY to WAIT if now outside trading window
        window_ok, window_reason = _is_good_trading_window()
        if not window_ok and cached_sig.get("signal") in ("BUY_CALL", "BUY_PUT"):
            cached_sig["signal"] = "WAIT"
            cached_sig["reasoning"] = f"Signal downgraded to WAIT: {window_reason}"
            cached_sig["trading_window"] = window_reason
        return cached_sig

    key = _get_api_key()
    if not key:
        logger.warning("No Gemini API key configured — using rule-based fallback.")
        return _rule_based_signal(analysis)

    prompt = _build_prompt(analysis)

    # Try new google.genai SDK first, then fall back to old google.generativeai
    try:
        from google import genai as gai
        client = gai.Client(api_key=key)
        start = time.time()
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=gai.types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
                response_mime_type="application/json",
                system_instruction=(
                    "You are an expert Indian F&O options trader. "
                    "Always respond with valid JSON only — no markdown, no extra text."
                ),
            ),
        )
        elapsed = time.time() - start
        content = response.text.strip()
        logger.info(f"Gemini (genai) response in {elapsed:.1f}s: {content[:80]}...")
        signal = _parse_response(content, analysis)
        signal["llm_model"] = config.GEMINI_MODEL
        signal["llm_inference_time"] = round(elapsed, 2)
        _signal_cache[symbol] = {"signal": signal, "time": time.time()}
        return signal
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"google.genai call failed: {e}. Trying legacy SDK...")
        # record last error for status endpoint
        try:
            import time as _time
            _last_error = str(e)
            _last_error_time = _time.time()
        except Exception:
            pass

    # Fallback: try old google.generativeai
    try:
        import google.generativeai as genai
        import warnings
        warnings.filterwarnings("ignore")
        genai.configure(api_key=key)
        model = genai.GenerativeModel(
            config.GEMINI_MODEL,
            generation_config=genai.GenerationConfig(
                temperature=0.2,
                response_mime_type="application/json",
                max_output_tokens=1024,
            ),
            system_instruction=(
                "You are an expert Indian F&O options trader. "
                "Always respond with valid JSON only — no markdown, no extra text."
            ),
        )
        start = time.time()
        response = model.generate_content(prompt)
        elapsed = time.time() - start
        content = response.text.strip()
        logger.info(f"Gemini (legacy) response in {elapsed:.1f}s: {content[:80]}...")
        signal = _parse_response(content, analysis)
        signal["llm_model"] = config.GEMINI_MODEL
        signal["llm_inference_time"] = round(elapsed, 2)
        _signal_cache[symbol] = {"signal": signal, "time": time.time()}
        return signal
    except Exception as e:
        logger.warning(f"Gemini API call failed: {e}. Using rule-based fallback.")
        try:
            import time as _time
            _last_error = str(e)
            _last_error_time = _time.time()
        except Exception:
            pass

    return _rule_based_signal(analysis)


import re

def _parse_response(content: str, analysis: dict) -> dict:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        raw_json = match.group(0)
        result = None
        try:
            result = json.loads(raw_json)
        except json.JSONDecodeError:
            try:
                # Convert raw literal newlines inside JSON strings to \n escape sequences
                fixed_json = re.sub(r'(?<=: ")(.*?)(?=",\n|"\n\})', lambda m: m.group(1).replace("\n", "\\n").replace("\r", ""), raw_json, flags=re.DOTALL)
                result = json.loads(fixed_json)
            except Exception as e:
                logger.warning(f"JSON decode failed after newline cleanup: {e}")

        if result and isinstance(result, dict) and "signal" in result and "confidence" in result:
            result["source"] = "gemini_api"
            return _enrich_signal(result, analysis)

    logger.warning(f"Could not parse Gemini JSON, using rule-based. Raw: {content[:200]}")
    return _rule_based_signal(analysis)


def _enrich_signal(signal: dict, analysis: dict) -> dict:
    symbol = analysis["symbol"]
    lot_size = config.INDICES[symbol]["lot_size"]
    budget_advice = analysis.get("budget_advice", {})
    otype = signal.get("option_type", "CE")
    if otype not in ("CE", "PE"):
        otype = "CE"

    side = "call" if otype == "CE" else "put"
    budget_side = budget_advice.get(side, {})

    # Ensure accurate premium is displayed even for WAIT
    prem = signal.get("entry_premium", 0)
    if not prem or prem <= 0:
        prem = analysis.get("atm_put_ltp", 0) if otype == "PE" else analysis.get("atm_call_ltp", 0)
        signal["entry_premium"] = round(prem, 2)

    if not signal.get("target_premium"):
        signal["target_premium"] = round(prem * (1 + config.PROFIT_TARGET_PCT), 2)
    if not signal.get("stop_loss_premium"):
        signal["stop_loss_premium"] = round(prem * (1 - config.STOP_LOSS_PCT), 2)

    signal.setdefault("lots_recommended", budget_side.get("lots", 1))
    signal.setdefault("estimated_cost_inr", round(prem * lot_size, 2))
    signal.setdefault("max_profit_inr", round((signal["target_premium"] - prem) * lot_size, 2))
    signal.setdefault("max_loss_inr", round((prem - signal["stop_loss_premium"]) * lot_size, 2))

    # Add Recommended Holding Time & Action Plan
    conf = signal.get("confidence", 50)
    sig_name = signal.get("signal", "WAIT")
    strike = signal.get("strike", analysis.get("atm_strike", 0))

    tgt_pct = int(config.PROFIT_TARGET_PCT * 100)
    sl_pct = int(config.STOP_LOSS_PCT * 100)

    if sig_name == "BUY_CALL":
        signal["holding_time"] = "30 – 60 Minutes (Trend Ride)" if conf >= 75 else "15 – 30 Minutes (Quick Scalp)"
        signal["action_summary"] = f"BUY {symbol} {strike} CE @ ₹{signal['entry_premium']} on Groww"
        signal["exit_rule"] = f"Book profit at ₹{signal['target_premium']} (+{tgt_pct}%) or Exit at SL ₹{signal['stop_loss_premium']} (-{sl_pct}%). Trail SL to breakeven at +10%."
    elif sig_name == "BUY_PUT":
        signal["holding_time"] = "30 – 60 Minutes (Trend Ride)" if conf >= 75 else "15 – 30 Minutes (Quick Scalp)"
        signal["action_summary"] = f"BUY {symbol} {strike} PE @ ₹{signal['entry_premium']} on Groww"
        signal["exit_rule"] = f"Book profit at ₹{signal['target_premium']} (+{tgt_pct}%) or Exit at SL ₹{signal['stop_loss_premium']} (-{sl_pct}%). Trail SL to breakeven at +10%."
    else:
        signal["holding_time"] = "0 Mins — Stay on Sidelines"
        signal["action_summary"] = "HOLD CASH — Wait for technical breakout"
        signal["exit_rule"] = "Do not enter position while market is consolidating"

    signal["lot_size"] = lot_size
    signal["symbol"] = symbol
    signal["nearest_expiry"] = analysis.get("nearest_expiry", "N/A")
    signal["atm_strike"] = analysis.get("atm_strike", 0)
    signal["ltp"] = analysis.get("ltp", 0)
    signal["bias_score"] = analysis.get("bias_score", 0)
    signal["data_source"] = analysis.get("data_source", "unknown")
    return signal


# ─── Trading Window Filter ────────────────────────────────────
def _is_good_trading_window() -> tuple:
    """
    Returns (is_allowed, reason) based on time-of-day.
    Only allows entries during the two best intraday windows:
      - 9:45–11:15 AM  (morning institutional trend)
      - 14:00–14:45 PM (afternoon momentum)
    Blocks entries during chop zones and volatile open/close.
    """
    from datetime import datetime
    now = datetime.now()
    h, m = now.hour, now.minute
    mins = h * 60 + m

    # Pre-market / first 30 min volatility — AVOID
    if mins < 9 * 60 + 45:
        return False, "Avoiding first 30 min — spreads wide, IV crush risk"
    # Morning trend window — BEST
    if mins <= 11 * 60 + 15:
        return True, "Morning trend window (9:45–11:15)"
    # Lunch chop zone — AVOID
    if mins < 14 * 60:
        return False, "Lunch chop zone (11:15–14:00) — theta decay eats premium"
    # Afternoon momentum — GOOD
    if mins <= 14 * 60 + 45:
        return True, "Afternoon momentum window (14:00–14:45)"
    # Last hour square-off pressure — AVOID
    return False, "Avoiding last 45 min — square-off pressure, unpredictable"


# ─── Consecutive Loss Tracker ────────────────────────────────
_consecutive_losses = {"NIFTY": 0, "BANKNIFTY": 0}

def record_trade_result(symbol: str, is_win: bool):
    """Called by app.py when a trade closes. Tracks consecutive losses."""
    global _consecutive_losses
    if is_win:
        _consecutive_losses[symbol] = 0
    else:
        _consecutive_losses[symbol] = _consecutive_losses.get(symbol, 0) + 1


def _get_consecutive_losses(symbol: str) -> int:
    return _consecutive_losses.get(symbol, 0)


# ─── Rule-Based Fallback Signal ───────────────────────────────
def _rule_based_signal(analysis: dict) -> dict:
    symbol = analysis["symbol"]
    bias_score = analysis.get("bias_score", 0)
    ltp = analysis.get("ltp", 0)
    pcr = analysis.get("pcr", 1.0)
    atm = analysis.get("atm_strike", 0)
    lot_size = config.INDICES[symbol]["lot_size"]
    budget_advice = analysis.get("budget_advice", {})
    expiry = analysis.get("nearest_expiry", "N/A")
    ta = analysis.get("ta", {})
    oi = analysis.get("oi_analysis", {})

    # ── Gate 1: Trading Window Filter ──────────────────────
    window_ok, window_reason = _is_good_trading_window()

    # ── Gate 2: Consecutive Loss Protection ────────────────
    consec_losses = _get_consecutive_losses(symbol)
    loss_blocked = consec_losses >= 2  # Pause after 2 consecutive losses

    # ── Gate 3: Strong Signal Confirmation (bias >= 5) ─────
    # Require at least 3-4 indicators agreeing before entry
    ENTRY_THRESHOLD = 5

    if bias_score >= ENTRY_THRESHOLD and window_ok and not loss_blocked:
        signal, otype, confidence = "BUY_CALL", "CE", min(92, 55 + bias_score * 7)
        reasoning = (
            f"WHY BUY CALL (CE) — STRONG CONFIRMATION:\n"
            f"• Bias Score: {bias_score:+}/±10 (threshold ≥{ENTRY_THRESHOLD} met)\n"
            f"• Trading Window: ✅ {window_reason}\n"
            f"• Put-Call Ratio (PCR): {pcr} ({analysis.get('pcr_signal','').replace('_',' ')}) — Bullish put writing\n"
            f"• OI Support/Resistance: Call wall at {oi.get('call_resistance','')}, Put support at {oi.get('put_support','')}\n"
            f"• Technical Indicators: RSI(14)={ta.get('rsi',0):.1f} ({ta.get('rsi_signal','').replace('_',' ')}), MACD {ta.get('macd_bias','')}, Supertrend {ta.get('supertrend_signal','')}\n"
            f"• Risk/Reward: Target +{config.PROFIT_TARGET_PCT*100:.0f}% / SL -{config.STOP_LOSS_PCT*100:.0f}% (1:2 R:R in your favor)\n"
            f"• Groww Strategy: Buy {atm} CE, set limit order within bid-ask spread."
        )
    elif bias_score <= -ENTRY_THRESHOLD and window_ok and not loss_blocked:
        signal, otype, confidence = "BUY_PUT", "PE", min(92, 55 + abs(bias_score) * 7)
        reasoning = (
            f"WHY BUY PUT (PE) — STRONG CONFIRMATION:\n"
            f"• Bias Score: {bias_score:+}/±10 (threshold ≤-{ENTRY_THRESHOLD} met)\n"
            f"• Trading Window: ✅ {window_reason}\n"
            f"• Put-Call Ratio (PCR): {pcr} ({analysis.get('pcr_signal','').replace('_',' ')}) — Bearish call writing\n"
            f"• OI Support/Resistance: Call wall at {oi.get('call_resistance','')}, Put support at {oi.get('put_support','')}\n"
            f"• Technical Indicators: RSI(14)={ta.get('rsi',0):.1f} ({ta.get('rsi_signal','').replace('_',' ')}), MACD {ta.get('macd_bias','')}, Supertrend {ta.get('supertrend_signal','')}\n"
            f"• Risk/Reward: Target +{config.PROFIT_TARGET_PCT*100:.0f}% / SL -{config.STOP_LOSS_PCT*100:.0f}% (1:2 R:R in your favor)\n"
            f"• Groww Strategy: Buy {atm} PE, set limit order within bid-ask spread."
        )
    else:
        signal, otype, confidence = "WAIT", "NONE", 40
        # Build specific WAIT reason
        wait_reasons = []
        if abs(bias_score) < ENTRY_THRESHOLD:
            wait_reasons.append(f"Weak signal (bias {bias_score:+}, need ≥{ENTRY_THRESHOLD} or ≤-{ENTRY_THRESHOLD})")
        if not window_ok:
            wait_reasons.append(f"Bad timing: {window_reason}")
        if loss_blocked:
            wait_reasons.append(f"Capital protection: {consec_losses} consecutive losses — waiting for reset")

        reasoning = (
            f"WHY WAIT (NO TRADE) — CAPITAL PROTECTION:\n"
            f"• {' | '.join(wait_reasons) if wait_reasons else 'No strong directional signal'}\n"
            f"• Put-Call Ratio (PCR): {pcr} — {analysis.get('pcr_signal','NEUTRAL').replace('_',' ')}\n"
            f"• OI Range: Bound between Call Wall ({oi.get('call_resistance','')}) & Put Wall ({oi.get('put_support','')})\n"
            f"• Technical Indicators: RSI(14)={ta.get('rsi',50):.1f}, MACD {ta.get('macd_bias','')}, Supertrend {ta.get('supertrend_signal','')}\n"
            f"• Capital Protection: Preserve ₹{config.USER_BUDGET_INR:,} budget — patience is profitable."
        )

    side = "call" if otype in ("CE", "NONE") else "put"
    bd = budget_advice.get(side, {})
    premium = (
        analysis.get("atm_put_ltp", 0) if otype == "PE"
        else analysis.get("atm_call_ltp", 0)
    )

    # Use config-driven target/SL percentages (not hardcoded)
    target_prem = round(premium * (1 + config.PROFIT_TARGET_PCT), 2) if premium else 0
    sl_prem = round(premium * (1 - config.STOP_LOSS_PCT), 2) if premium else 0

    res = {
        "signal": signal, "confidence": int(confidence),
        "strike": atm, "option_type": otype,
        "entry_premium": premium,
        "target_premium": target_prem,
        "stop_loss_premium": sl_prem,
        "lots_recommended": bd.get("lots", 1),
        "estimated_cost_inr": bd.get("cost_inr", 0),
        "max_profit_inr": bd.get("max_profit_inr", 0),
        "max_loss_inr": bd.get("max_loss_inr", 0),
        "lot_size": lot_size,
        "reasoning": reasoning,
        "key_risk": "Market can reverse quickly — always use a stop loss. Never risk more than 50% of capital.",
        "market_bias": (
            "BULLISH" if bias_score >= 3 else
            "BEARISH" if bias_score <= -3 else "NEUTRAL"
        ),
        "trade_tip": (
            "Set a LIMIT order within the bid-ask spread on Groww F&O. "
            "Move SL to breakeven once trade is +10% in profit (trailing stop)."
        ),
        "symbol": symbol, "nearest_expiry": expiry,
        "atm_strike": atm, "ltp": ltp,
        "bias_score": bias_score,
        "data_source": analysis.get("data_source", "unknown"),
        "source": "rule_based",
        "llm_model": "Rule-Based (Enhanced v2)",
        "llm_inference_time": 0,
        "trading_window": window_reason,
        "consecutive_losses": consec_losses,
    }
    return _enrich_signal(res, analysis)

