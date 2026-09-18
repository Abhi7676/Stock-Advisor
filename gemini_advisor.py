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
import threading
import time

import config

logger = logging.getLogger(__name__)

# Track last Gemini/API error for status reporting and UI
_last_error = None
_last_error_time = 0.0

# ─── Gemini Pause Toggle ─────────────────────────────────────
# When True, get_signal() skips the Gemini API call entirely
# and falls back to the rule-based signal — saving tokens.
_gemini_paused = False
_gemini_paused_lock = threading.Lock()

def set_gemini_paused(paused: bool):
    """Enable or disable sending requests to the Gemini API."""
    global _gemini_paused
    with _gemini_paused_lock:
        _gemini_paused = bool(paused)

def is_gemini_paused() -> bool:
    """Returns True if Gemini API calls are currently paused by the user."""
    with _gemini_paused_lock:
        return _gemini_paused

# ─── Gemini Call Inspector / Log Storage ─────────────────────
_call_logs = []
_call_logs_lock = threading.Lock()
_call_counter = 0

def _next_log_id() -> int:
    global _call_counter
    with _call_logs_lock:
        _call_counter += 1
        return _call_counter

def _log_call(entry: dict):
    with _call_logs_lock:
        _call_logs.insert(0, entry)
        if len(_call_logs) > 200:
            _call_logs.pop()

def get_gemini_call_logs(limit: int = 50) -> list:
    with _call_logs_lock:
        return list(_call_logs[:limit])


def _is_market_open() -> bool:
    """Returns True if current IST time is within NSE market hours (9:15–15:30) on a weekday."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
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
        pass
    try:
        import requests  # noqa
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
  "target_premium": <target premium for 15%+ profit>,
  "stop_loss_premium": <stop loss premium level>,
  "estimated_cost_inr": <total cost for 1 lot>,
  "holding_minutes": <integer: recommended minutes to hold before booking profit, e.g. 20 or 45>,
  "reasoning": "WHY BUY CALL (or PUT / WAIT):\n• PCR: <exact PCR & interpretation>\n• OI Walls: <Support and Resistance walls>\n• Technicals: <RSI, MACD, Supertrend signals>\n• Risk/Reward: <Target +15%, SL -8%, Capital Protection>",
  "key_risk": "<1 sentence about the main risk>",
  "market_bias": "BULLISH" or "BEARISH" or "NEUTRAL",
  "trade_tip": "<1 practical tip for Groww F&O — include exact time to exit>"
}}"""


_signal_cache = {}
_cooldown_until = 0.0
CACHE_TTL = 120  # 2 minutes cache for responsive signal updates

# Per-cache-key locks to prevent concurrent threads from making duplicate Gemini API calls.
# Only the first thread to acquire a lock calls Gemini; the rest wait and hit the warm cache.
_gemini_call_locks: dict = {}
_gemini_locks_meta = threading.Lock()

def _get_call_lock(key: str) -> threading.Lock:
    """Returns a reusable lock for a given cache_key, creating it if needed."""
    with _gemini_locks_meta:
        if key not in _gemini_call_locks:
            _gemini_call_locks[key] = threading.Lock()
        return _gemini_call_locks[key]


def get_signal(analysis: dict) -> dict:
    """
    Two-Stage Smart Signal Engine (Max API Quota Preservation):
      Stage 1: Fast Rule-Based Pre-Filter.
               Checks PCR, RSI, MACD, Supertrend, OI walls, and intraday trading windows.
               If the market is choppy, sideways, or outside trading windows (WAIT),
               returns the rule-based result immediately — SAVING 90%+ of API calls!
      Stage 2: AI Validation & Trade Advisory (Gemini API).
               When a confirmed setup PASSES (BUY_CALL or BUY_PUT candidate), queries
               Google Gemini API for expert trade validation, reasoning, risk tips, and confidence.
    """
    global _cooldown_until, _last_error, _last_error_time
    symbol = analysis["symbol"]
    now = time.time()

    # ── Gate 0: Market Hours Check ────────────────────────
    if not _is_market_open():
        return _market_closed_signal(analysis)

    # ── Stage 1: Fast Rule-Based Filter ───────────────────
    rule_sig = _rule_based_signal(analysis)

    # If the setup is WAIT (choppy, sideways, low momentum, bad window), do NOT call Gemini
    if rule_sig.get("signal") == "WAIT":
        return rule_sig

    # ── Stage 2: Confirmed Trade Candidate -> Gemini AI ───
    # Check Cache (2 minutes) for the active trade setup
    setup_type = rule_sig.get("signal", "NONE")
    cache_key = f"{symbol}_{setup_type}"
    if cache_key in _signal_cache and (now - _signal_cache[cache_key]["time"]) < CACHE_TTL:
        cached_sig = _signal_cache[cache_key]["signal"].copy()
        window_ok, window_reason = _is_good_trading_window()
        if not window_ok and cached_sig.get("signal") in ("BUY_CALL", "BUY_PUT"):
            cached_sig["signal"] = "WAIT"
            cached_sig["reasoning"] = f"Signal downgraded to WAIT: {window_reason}"
            cached_sig["trading_window"] = window_reason
        return _enrich_signal(cached_sig, analysis)

    # If in rate-limit cooldown, return confirmed rule signal
    if now < _cooldown_until:
        return rule_sig

    key = _get_api_key()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")

    if not key:
        logger.info("No Gemini API key — using confirmed rule-based trade signal.")
        return rule_sig

    # ── User-requested Gemini pause ────────────────────────────
    if is_gemini_paused():
        logger.info(f"Gemini API is PAUSED by user — returning rule-based signal for {symbol}.")
        rule_sig["source"] = "rule_based (Gemini paused)"
        rule_sig["llm_model"] = "Paused (token save mode)"
        return rule_sig

    # ── Deduplication Lock ─────────────────────────────────
    # Acquire a per-(symbol+direction) lock before calling Gemini.
    # If another thread is already in flight for this exact setup,
    # we WAIT for it to finish, then return the freshly cached result.
    call_lock = _get_call_lock(cache_key)
    if not call_lock.acquire(blocking=True, timeout=35):
        # Lock timed out — return rule signal safely
        return rule_sig

    try:
        # Re-check cache: another thread may have populated it while we waited
        if cache_key in _signal_cache and (time.time() - _signal_cache[cache_key]["time"]) < CACHE_TTL:
            logger.info(f"Cache hit after lock wait for {cache_key} — skipping Gemini call.")
            return _enrich_signal(_signal_cache[cache_key]["signal"].copy(), analysis)

        prompt = _build_prompt(analysis)
        candidate_models = [config.GEMINI_MODEL] + [
            m for m in getattr(config, "GEMINI_FALLBACK_MODELS", ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash"])
            if m != config.GEMINI_MODEL
        ]

        sys_instruction = (
            "You are an expert Indian F&O options trader. "
            "Always respond with valid JSON only — no markdown, no extra text."
        )

        # Try models in order (primary -> fallbacks)
        last_err = None
        for model_name in candidate_models:
            try:
                start = time.time()
                content, sdk_used = _call_gemini_model(
                    model_name=model_name,
                    key=key,
                    prompt=prompt,
                    system_instruction=sys_instruction,
                    json_mode=True,
                )
                elapsed = round(time.time() - start, 2)
                logger.info(f"Gemini ({model_name} via {sdk_used}) response in {elapsed:.1f}s: {content[:80]}...")
                signal = _parse_response(content, analysis)
                signal["llm_model"] = model_name
                signal["llm_inference_time"] = elapsed
                _signal_cache[cache_key] = {"signal": signal, "time": time.time()}
                _log_call({
                    "id": _next_log_id(), "timestamp": ist_time, "symbol": symbol,
                    "model": model_name, "status": "SUCCESS", "prompt": prompt,
                    "response_raw": content, "inference_time_sec": elapsed,
                    "error": None, "parsed_signal": signal, "sdk_used": sdk_used,
                })
                return signal
            except Exception as e:
                last_err = str(e)
                logger.warning(f"Gemini call with {model_name} failed: {e}. Trying next option...")

        # All models exhausted — set 60s cooldown to protect quota
        _last_error = last_err
        _last_error_time = time.time()
        _cooldown_until = time.time() + 60.0
        _log_call({
            "id": _next_log_id(), "timestamp": ist_time, "symbol": symbol,
            "model": config.GEMINI_MODEL, "status": "ERROR", "prompt": prompt,
            "response_raw": None, "inference_time_sec": 0,
            "error": f"API Error — Falling back to rule-based analysis ({last_err})",
            "parsed_signal": None, "sdk_used": "failed_all",
        })
        return _rule_based_signal(analysis)

    finally:
        call_lock.release()


def _call_gemini_model(
    model_name: str,
    key: str,
    prompt: str,
    system_instruction: str | None = None,
    json_mode: bool = True,
) -> tuple[str, str]:
    """
    Executes a prompt against a specific Gemini model using a 3-tier strategy:
      1. Direct REST API (fastest, guaranteed compatibility with gemini-3.8-flash, zero SDK version constraints)
      2. google.genai SDK
      3. google.generativeai legacy SDK
    Returns (raw_text, sdk_used).
    """
    errors = []

    # 1. Direct REST API (guaranteed to work across all environments including Render free host)
    try:
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 2048,
            },
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        timeout_sec = getattr(config, "GEMINI_TIMEOUT", 30)
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout_sec)
        if resp.status_code == 200:
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    return parts[0]["text"].strip(), "google.rest"

        # Try to extract clean error message
        err_msg = ""
        try:
            err_data = resp.json()
            err_msg = err_data.get("error", {}).get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200]
        # If server returned an HTTP error code (e.g. 503 high demand, 404 expired, 429 rate limit),
        # trying SDKs on the exact same model will hit the same server error. Raise immediately!
        raise RuntimeError(f"HTTP {resp.status_code}: {err_msg}")
    except requests.exceptions.RequestException as e:
        errors.append(f"REST connection: {e}")
    except Exception as e:
        # Re-raise server HTTP errors directly so candidate loop can advance to next fallback model
        raise e

    # 2. google.genai SDK (used if REST connection/import had issue)
    try:
        from google import genai as gai
        client = gai.Client(api_key=key)
        gen_config = {"max_output_tokens": 2048}
        if json_mode:
            gen_config["response_mime_type"] = "application/json"
        if system_instruction:
            gen_config["system_instruction"] = system_instruction
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=gai.types.GenerateContentConfig(**gen_config),
        )
        if hasattr(response, "text") and response.text:
            return response.text.strip(), "google.genai"
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"google.genai: {e}")

    # 3. google.generativeai legacy SDK
    try:
        import google.generativeai as genai
        import warnings
        warnings.filterwarnings("ignore")
        genai.configure(api_key=key)
        gen_kwargs = {"max_output_tokens": 2048}
        if json_mode:
            gen_kwargs["response_mime_type"] = "application/json"
        model = genai.GenerativeModel(
            model_name,
            generation_config=genai.GenerationConfig(**gen_kwargs),
            system_instruction=system_instruction,
        )
        response = model.generate_content(prompt)
        if hasattr(response, "text") and response.text:
            return response.text.strip(), "google.generativeai"
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"google.generativeai: {e}")

    raise RuntimeError(" | ".join(errors) if errors else f"All methods failed for {model_name}")


def test_gemini_call(custom_prompt: str | None = None, symbol: str = "TEST") -> dict:
    """Executes an interactive test call to verify Gemini API Key and logs full query & answer."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")

    key = _get_api_key()
    if not key:
        err_msg = "GEMINI_API_KEY is not set. Please set it in .env file or environment variable."
        entry = {
            "id": _next_log_id(),
            "timestamp": ist_time,
            "symbol": symbol,
            "model": config.GEMINI_MODEL,
            "status": "NO_API_KEY",
            "prompt": custom_prompt or "Test Query: Market Status and Bias Check",
            "response_raw": None,
            "inference_time_sec": 0,
            "error": err_msg,
            "parsed_signal": None,
            "sdk_used": "none",
        }
        _log_call(entry)
        return {"status": "error", "message": err_msg, "entry": entry}

    prompt = custom_prompt or (
        "You are an expert Indian stock market options trading assistant.\n"
        "Question: NIFTY is currently trading at 23,750 with PCR of 1.22 (Bullish) and RSI at 57. "
        "What is the recommended intraday bias and key risk tip?\n\n"
        "Respond in structured JSON format with fields: signal, confidence, reasoning, key_risk, market_bias, trade_tip."
    )

    candidate_models = [config.GEMINI_MODEL] + [
        m for m in getattr(config, "GEMINI_FALLBACK_MODELS", ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash"])
        if m != config.GEMINI_MODEL
    ]

    last_err = None
    for model_name in candidate_models:
        try:
            start = time.time()
            content, sdk_used = _call_gemini_model(
                model_name=model_name,
                key=key,
                prompt=prompt,
                system_instruction=(
                    "You are an expert Indian stock market options trading assistant. "
                    "Always respond in valid JSON format."
                ),
                json_mode=True,
            )
            elapsed = round(time.time() - start, 2)
            entry = {
                "id": _next_log_id(),
                "timestamp": ist_time,
                "symbol": symbol,
                "model": model_name,
                "status": "SUCCESS",
                "prompt": prompt,
                "response_raw": content,
                "inference_time_sec": elapsed,
                "error": None,
                "parsed_signal": None,
                "sdk_used": sdk_used,
            }
            _log_call(entry)
            return {"status": "ok", "message": f"Test call successful via {sdk_used} ({model_name})", "entry": entry}
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Test call with {model_name} failed: {e}. Trying next option...")

    entry = {
        "id": _next_log_id(),
        "timestamp": ist_time,
        "symbol": symbol,
        "model": config.GEMINI_MODEL,
        "status": "ERROR",
        "prompt": prompt,
        "response_raw": None,
        "inference_time_sec": 0,
        "error": last_err or "All candidate models failed",
        "parsed_signal": None,
        "sdk_used": "failed_all_sdks",
    }
    _log_call(entry)
    return {"status": "error", "message": last_err or "All models failed", "entry": entry}



import re

def _parse_response(content: str, analysis: dict) -> dict:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    raw_json = match.group(0) if match else None
    if not raw_json and "{" in content:
        start_idx = content.find("{")
        snippet = content[start_idx:].strip()
        if snippet.count('"') % 2 != 0:
            snippet += '"'
        raw_json = snippet + "\n}"

    if raw_json:
        result = None
        # 1. Standard json loads with strict=False
        try:
            result = json.loads(raw_json, strict=False)
        except Exception:
            try:
                # 2. Convert raw literal newlines inside JSON strings to \n escape sequences
                fixed_json = re.sub(r'(?<=: ")(.*?)(?=",\n|"\n\})', lambda m: m.group(1).replace("\n", "\\n").replace("\r", ""), raw_json, flags=re.DOTALL)
                result = json.loads(fixed_json, strict=False)
            except Exception:
                pass

        if result and isinstance(result, dict) and "signal" in result:
            result["source"] = "gemini_api"
            return _enrich_signal(result, analysis)

    logger.warning(f"Could not parse Gemini JSON, falling back to rule signal. Raw: {content[:150]}")
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

    strike = signal.get("strike", analysis.get("atm_strike", 0))

    # Look up actual live option LTP for recommended strike from real NSE chain data
    chain_rows = analysis.get("chain", []) or analysis.get("chain_summary", [])
    strike_row = next((r for r in chain_rows if r.get("strike") == strike), None)

    real_live_prem = 0.0
    if strike_row:
        real_live_prem = float(strike_row.get("put_ltp" if otype == "PE" else "call_ltp", 0) or 0)

    if real_live_prem <= 0:
        real_live_prem = float(analysis.get("atm_put_ltp", 0) if otype == "PE" else analysis.get("atm_call_ltp", 0) or 0)

    # Use the real live market premium as the exact entry price
    prem = round(real_live_prem, 2) if real_live_prem > 0 else round(signal.get("entry_premium", 0) or 0, 2)
    signal["entry_premium"] = prem

    target_prem = round(prem * (1 + config.PROFIT_TARGET_PCT), 2) if prem else 0
    sl_prem = round(prem * (1 - config.STOP_LOSS_PCT), 2) if prem else 0
    signal["target_premium"] = target_prem
    signal["stop_loss_premium"] = sl_prem

    lots = budget_side.get("lots", 1)
    signal["lots_recommended"] = lots
    signal["estimated_cost_inr"] = round(prem * lot_size * lots, 2)
    signal["max_profit_inr"] = round((target_prem - prem) * lot_size * lots, 2)
    signal["max_loss_inr"] = round((prem - sl_prem) * lot_size * lots, 2)

    # Add Recommended Holding Time & Action Plan
    conf = signal.get("confidence", 50)
    sig_name = signal.get("signal", "WAIT")

    tgt_pct = round(config.PROFIT_TARGET_PCT * 100)
    sl_pct = round(config.STOP_LOSS_PCT * 100)

    # Determine holding minutes — use Gemini's suggestion if provided, else estimate from confidence
    gemini_hold_mins = signal.get("holding_minutes")
    if gemini_hold_mins and isinstance(gemini_hold_mins, (int, float)) and 5 <= int(gemini_hold_mins) <= 135:
        hold_mins = int(gemini_hold_mins)
    else:
        # Rule-based estimate: stronger signal = shorter time to reach target
        if conf >= 90:
            hold_mins = 20
        elif conf >= 80:
            hold_mins = 30
        else:
            hold_mins = 45
    signal["holding_minutes"] = hold_mins

    if sig_name == "BUY_CALL":
        signal["holding_time"] = f"~{hold_mins} Minutes (Book at +{tgt_pct}% target)"
        signal["action_summary"] = f"BUY {symbol} {strike} CE @ \u20b9{prem} on Groww"
        signal["exit_rule"] = (f"Hold {hold_mins} min or until premium hits \u20b9{target_prem} (+{tgt_pct}%). "
                               f"Exit immediately at SL \u20b9{sl_prem} (-{sl_pct}%). Trail SL to breakeven at +8%.")
    elif sig_name == "BUY_PUT":
        signal["holding_time"] = f"~{hold_mins} Minutes (Book at +{tgt_pct}% target)"
        signal["action_summary"] = f"BUY {symbol} {strike} PE @ \u20b9{prem} on Groww"
        signal["exit_rule"] = (f"Hold {hold_mins} min or until premium hits \u20b9{target_prem} (+{tgt_pct}%). "
                               f"Exit immediately at SL \u20b9{sl_prem} (-{sl_pct}%). Trail SL to breakeven at +8%.")
    else:
        signal["holding_minutes"] = 0
        signal["holding_time"] = "0 Mins \u2014 Stay on Sidelines"
        signal["action_summary"] = "HOLD CASH \u2014 Wait for 9:30\u201311:45 AM breakout"
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
    Returns (is_allowed, reason_str) based on IST time-of-day.
    TWO ACTIVE WINDOWS:
      • Morning  : 9:30 AM – 11:45 AM (institutional momentum, tight spreads)
      • Afternoon: 2:00 PM – 3:20 PM  (resumption window, pre-close moves)
    Midday 11:45 AM – 2:00 PM is skipped (low volume, lunch chop).
    Stops 10 min before 3:30 PM to avoid end-of-day whipsaws.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    h, m = now.hour, now.minute
    mins = h * 60 + m

    morning_open  = 9 * 60 + 30   # 9:30 AM
    morning_close = 11 * 60 + 45  # 11:45 AM
    afternoon_open  = 14 * 60     # 2:00 PM
    afternoon_close = 15 * 60 + 20  # 3:20 PM (10-min buffer before market close)

    if mins < morning_open:
        wait = morning_open - mins
        return False, f"Morning window opens at 9:30 AM IST ({wait} min away)"
    if mins <= morning_close:
        remaining = morning_close - mins
        return True, f"Morning window active 9:30–11:45 AM ({remaining} min remaining)"
    if mins < afternoon_open:
        wait = afternoon_open - mins
        return False, f"Midday break — afternoon window opens at 2:00 PM IST ({wait} min away)"
    if mins <= afternoon_close:
        remaining = afternoon_close - mins
        return True, f"Afternoon window active 2:00–3:20 PM ({remaining} min remaining)"
    return False, "Market closing (3:20 PM+) — no new entries in last 10 min before close"


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

    # ── Gate 3: Directional Bias Threshold ────────────────
    # Score of ±3 represents strong directional confirmation (e.g. PCR + MACD + RSI/Momentum)
    ENTRY_THRESHOLD = getattr(config, "ENTRY_THRESHOLD", 3)

    if bias_score >= ENTRY_THRESHOLD and window_ok and not loss_blocked:
        signal, otype, confidence = "BUY_CALL", "CE", min(99, 75 + bias_score * 6)
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
        signal, otype, confidence = "BUY_PUT", "PE", min(99, 75 + abs(bias_score) * 6)
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

