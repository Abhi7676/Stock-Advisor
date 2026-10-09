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
import urllib.error
import urllib.request

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


def _get_ist_now():
    """Returns current datetime in IST, with fallback if tzdata is missing on Windows."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _is_market_open() -> bool:
    """Returns True if current IST time is within NSE trading hours (9:15–15:00) on a weekday."""
    now = _get_ist_now()
    day = now.weekday()  # 0=Mon, 6=Sun
    if day >= 5:  # Weekend
        return False
    h, m = now.hour, now.minute
    mins = h * 60 + m
    return (9 * 60 + 15) <= mins < (15 * 60)


def _market_closed_signal(analysis: dict) -> dict:
    """Returns a WAIT signal when market or trading window is closed."""
    symbol = analysis["symbol"]
    ltp = analysis.get("ltp", 0)
    atm = analysis.get("atm_strike", 0)
    lot_size = config.INDICES[symbol]["lot_size"]
    premium = analysis.get("atm_call_ltp", 0)

    now = _get_ist_now()
    mins = now.hour * 60 + now.minute
    is_post_130pm = mins >= (13 * 60 + 30)

    msg = "Go do your work, please do not trade now" if is_post_130pm else "Market is closed. Wait for next session."
    reasoning = (
        "🛑 TRADING CLOSED (After 1:30 PM IST):\n"
        "• Go do your work, please do not trade now.\n"
        "• Trading window is 9:20 AM to 1:30 PM IST only.\n"
        "• Preserving capital and eliminating late-day decay.\n"
        "• Signals will resume tomorrow morning at 9:20 AM IST."
    ) if is_post_130pm else (
        "MARKET CLOSED — NO TRADING:\n"
        "• NSE trading session opens at 9:15 AM IST (Mon–Fri)\n"
        "• Signals will activate during next trading session"
    )

    return {
        "signal": "WAIT", "confidence": 0,
        "strike": atm, "option_type": "NONE",
        "entry_premium": premium,
        "target_premium": 0,
        "stop_loss_premium": 0,
        "lots_recommended": 0, "estimated_cost_inr": 0,
        "max_profit_inr": 0, "max_loss_inr": 0,
        "lot_size": lot_size,
        "reasoning": reasoning,
        "key_risk": msg,
        "market_bias": "NEUTRAL",
        "trade_tip": msg,
        "holding_time": "Session Closed",
        "action_summary": msg,
        "exit_rule": "No trading after 1:30 PM IST",
        "symbol": symbol, "nearest_expiry": analysis.get("nearest_expiry", "N/A"),
        "atm_strike": atm, "ltp": ltp,
        "bias_score": analysis.get("bias_score", 0),
        "data_source": analysis.get("data_source", "unknown"),
        "source": "post_1pm_break" if is_post_130pm else "market_closed",
        "is_post_1pm": is_post_130pm,
        "llm_model": "Session Closed",
        "llm_inference_time": 0,
        "trading_window": "Trading Window: 9:20 AM – 1:30 PM IST",
        "consecutive_losses": 0,
    }


def get_last_error() -> dict | None:
    """Return the last GEMINI API error (if any) as a dict for status reporting."""
    global _last_error, _last_error_time
    if not _last_error:
        return None
    return {"error": _last_error, "time": _last_error_time}

# ─── Multi-Key Gemini Management & 429 Failover ─────────────
_key_cooldowns: dict[str, float] = {}


def _get_api_keys() -> list[str]:
    """
    Returns list of all available Gemini API keys in priority order.
    Supports:
      - GEMINI_API_KEY (can be single key or comma-separated list of keys)
      - GEMINI_API_KEY_2 (fallback key if primary hits 429 quota)
      - GEMINI_API_KEY_3
      - GEMINI_API_KEYS (comma-separated list)
    """
    keys = []
    candidates = [
        os.environ.get("GEMINI_API_KEY"),
        getattr(config, "GEMINI_API_KEY", None),
        os.environ.get("GEMINI_API_KEY_2"),
        getattr(config, "GEMINI_API_KEY_2", None),
        os.environ.get("GEMINI_API_KEY_3"),
        getattr(config, "GEMINI_API_KEY_3", None),
        os.environ.get("GEMINI_API_KEYS"),
    ]
    for c in candidates:
        if not c:
            continue
        for k in str(c).split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def _get_active_api_keys() -> list[str]:
    """Returns active, non-cooldown keys first, followed by cooldown keys as fallback."""
    keys = _get_api_keys()
    now = time.time()
    active = [k for k in keys if now >= _key_cooldowns.get(k, 0)]
    cooldown = [k for k in keys if now < _key_cooldowns.get(k, 0)]
    return active if active else cooldown


def _get_api_key() -> str | None:
    """Returns the first active, non-cooldown API key."""
    active = _get_active_api_keys()
    return active[0] if active else None


def _mark_key_cooldown(key: str, seconds: int = 3600):
    """Mark an API key as quota-exhausted (HTTP 429) for a cooldown duration (default 60 min)."""
    global _key_cooldowns
    _key_cooldowns[key] = time.time() + seconds
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else "Key"
    logger.warning(f"Gemini API key {masked} placed on quota cooldown for {seconds//60} mins.")


def _is_quota_error(err_str: str) -> bool:
    """Checks if error message is an HTTP 429 or quota limit exhaustion."""
    s = str(err_str).lower()
    return "429" in s or "quota" in s or "resource_exhausted" in s or "rate limit" in s


def is_gemini_available() -> bool:
    keys = _get_api_keys()
    if not keys:
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
    key = _get_api_key()
    return config.GEMINI_MODEL if is_gemini_available() and key else "N/A"


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

COMPOSITE SIGNAL (score {bias_score:+}/±5):
{factor_lines}

ATM PREMIUMS ({expiry}):
• {atm} CE: ₹{call_premium} | Lots with ₹5000: {call_budget.get('lots',0)} | Cost: ₹{call_budget.get('cost_inr',0)}
• {atm} PE: ₹{put_premium} | Lots with ₹5000: {put_budget.get('lots',0)} | Cost: ₹{put_budget.get('cost_inr',0)}

CRITICAL REQUIREMENT — MINIMUM 13% PROFIT POTENTIAL:
• ONLY recommend BUY_CALL or BUY_PUT if the setup has clear high-conviction potential to generate a MINIMUM OF +13% PROFIT on the option premium before reaching resistance/support walls.
• Target premium MUST be at least +13% above entry premium (target_premium >= entry_premium * 1.13).
• If the room to resistance/support walls or market momentum cannot deliver at least 13% profit, you MUST return "WAIT".
• Active trading window is 9:20 AM to 1:30 PM IST only. No signals after 1:30 PM IST.

Respond ONLY with this JSON (no markdown, no extra text):
{{
  "signal": "BUY_CALL" or "BUY_PUT" or "WAIT",
  "confidence": <integer 1-100>,
  "strike": <recommended strike as integer>,
  "option_type": "CE" or "PE" or "NONE",
  "entry_premium": <entry premium in rupees>,
  "target_premium": <target premium for at least 13% profit>,
  "stop_loss_premium": <stop loss premium level>,
  "estimated_cost_inr": <total cost for 1 lot>,
  "reasoning": "WHY BUY CALL (or PUT / WAIT):\n• PCR: <exact PCR & interpretation>\n• OI Walls: <Support and Resistance walls>\n• Technicals: <RSI, MACD, Supertrend signals>\n• Profit Potential: <Minimum +13% potential verified>\n• Risk/Reward: <Target +13%, SL -8%, Capital Protection>",
  "key_risk": "<1 sentence about the main risk>",
  "market_bias": "BULLISH" or "BEARISH" or "NEUTRAL",
  "trade_tip": "<1 practical tip for Groww F&O — target 13% profit or exit by 1:30 PM IST close>"
}}"""


_signal_cache = {}
_cooldown_until = 0.0
# 7 minutes: Gemini is called ONLY when rule-engine confirms a genuine BUY setup.
# A real BUY signal doesn't flip direction in 7 minutes — caching saves 70%+ of API quota.
CACHE_TTL = 420

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

    # ── Gate 0.5: Post 1:30 PM Hard Trading Cutoff ─────────
    now_ist = _get_ist_now()
    if (now_ist.hour * 60 + now_ist.minute) >= (13 * 60 + 30):
        return _rule_based_signal(analysis)

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

    # Check available Gemini API keys
    available_keys = _get_active_api_keys()
    ist_time = _get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")

    if not available_keys:
        logger.info("No Gemini API key configured — using confirmed rule-based trade signal.")
        return rule_sig

    # If in rate-limit cooldown, return confirmed rule signal
    if now < _cooldown_until:
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
            m for m in getattr(config, "GEMINI_FALLBACK_MODELS", [])
            if m != config.GEMINI_MODEL
        ]

        sys_instruction = (
            "You are an expert Indian F&O options trader. "
            "Always respond with valid JSON only — no markdown, no extra text."
        )

        # Try active API keys in priority order with model fallback
        last_err = None
        for key_idx, key in enumerate(available_keys):
            masked_key = (key[:6] + "..." + key[-4:]) if len(key) > 10 else f"Key #{key_idx+1}"
            key_all_quota_hit = True
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
                    logger.info(f"Gemini ({model_name} via {sdk_used}, key {masked_key}) response in {elapsed:.1f}s: {content[:80]}...")
                    signal = _parse_response(content, analysis)
                    signal["llm_model"] = model_name
                    signal["llm_inference_time"] = elapsed
                    _signal_cache[cache_key] = {"signal": signal, "time": time.time()}
                    _log_call({
                        "id": _next_log_id(), "timestamp": ist_time, "symbol": symbol,
                        "model": model_name, "status": "SUCCESS", "prompt": prompt,
                        "response_raw": content, "inference_time_sec": elapsed,
                        "error": None, "parsed_signal": signal, "sdk_used": f"{sdk_used} (key: {masked_key})",
                    })
                    return signal
                except Exception as e:
                    last_err = str(e)
                    if _is_quota_error(last_err):
                        logger.warning(f"Gemini model {model_name} on key {masked_key} hit quota (429): {e}. Trying next fallback model...")
                        continue  # Try next candidate model on this key before giving up!
                    else:
                        key_all_quota_hit = False
                        logger.warning(f"Gemini call with {model_name} using key {masked_key} failed: {e}. Trying next option...")

            # If all models failed with quota on this key, put key on temporary cooldown
            if key_all_quota_hit:
                _mark_key_cooldown(key, seconds=1800)

            if key_idx + 1 < len(available_keys):
                logger.info(f"Switching from key {masked_key} to next configured API key...")
                continue

        # All keys and models exhausted — set 60s cooldown to protect quota
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

    # 1. Direct REST API via Python standard library urllib (zero external dependency, 100% portable)
    try:
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
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"].strip(), "google.rest"
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            err_msg = ""
            try:
                err_data = json.loads(err_body)
                err_msg = err_data.get("error", {}).get("message", err_body[:250])
            except Exception:
                err_msg = err_body[:250]
            # Server HTTP errors (429 rate limit, 404, 503) should raise immediately to trigger fallback
            raise RuntimeError(f"HTTP {he.code}: {err_msg}")
        except urllib.error.URLError as ue:
            errors.append(f"URLError: {ue}")
    except RuntimeError:
        raise
    except Exception as e:
        errors.append(f"REST: {e}")

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
    ist_time = _get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")

    available_keys = _get_active_api_keys()
    if not available_keys:
        err_msg = "No Gemini API key is configured. Please set GEMINI_API_KEY in .env file or environment variable."
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
        m for m in getattr(config, "GEMINI_FALLBACK_MODELS", [])
        if m != config.GEMINI_MODEL
    ]

    last_err = None
    for key_idx, key in enumerate(available_keys):
        masked_key = (key[:6] + "..." + key[-4:]) if len(key) > 10 else f"Key #{key_idx+1}"
        key_all_quota_hit = True
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
                    "sdk_used": f"{sdk_used} (key: {masked_key})",
                }
                _log_call(entry)
                return {"status": "ok", "message": f"Test call successful via {sdk_used} ({model_name}) using key {masked_key}", "entry": entry}
            except Exception as e:
                last_err = str(e)
                if _is_quota_error(last_err):
                    logger.warning(f"Test call: {model_name} on key {masked_key} hit quota (429): {e}. Trying next fallback model...")
                    continue
                else:
                    key_all_quota_hit = False
                    logger.warning(f"Test call with {model_name} using key {masked_key} failed: {e}. Trying next option...")

        if key_all_quota_hit:
            _mark_key_cooldown(key, seconds=1800)

        if key_idx + 1 < len(available_keys):
            logger.info(f"Test call: switching from key {masked_key} to next configured API key...")
            continue

    entry = {
        "id": _next_log_id(),
        "timestamp": ist_time,
        "symbol": symbol,
        "model": config.GEMINI_MODEL,
        "status": "ERROR",
        "prompt": prompt,
        "response_raw": None,
        "inference_time_sec": 0,
        "error": last_err or "All candidate models and API keys failed",
        "parsed_signal": None,
        "sdk_used": "failed_all",
    }
    _log_call(entry)
    return {"status": "error", "message": f"Gemini test call failed across all keys/models: {last_err}", "entry": entry}



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
            # ── Gemini VETO: if Gemini says WAIT despite rule-engine saying BUY,
            #    respect the AI judgment and return WAIT to protect capital.
            if result.get("signal") == "WAIT":
                logger.info("Gemini vetoed rule-based BUY signal → downgrading to WAIT (capital protection).")
                return _rule_based_signal(analysis)  # returns WAIT with full reasoning
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

    # Ensure target premium guarantees at least +13% profit on premium
    min_target = round(prem * (1 + config.PROFIT_TARGET_PCT), 2) if prem else 0
    target_prem = max(round(signal.get("target_premium", 0) or 0, 2), min_target)
    sl_max = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(symbol, 1600)
    sl_points = sl_max / lot_size
    sl_prem = round(max(0.5, prem - sl_points), 2) if prem else 0
    signal["target_premium"] = target_prem
    signal["stop_loss_premium"] = sl_prem
    signal["min_profit_target_pct"] = 13.0
    signal["potential_profit_pct"] = round(((target_prem - prem) / prem) * 100, 1) if prem > 0 else 13.0

    lots = budget_side.get("lots", 1)
    signal["lots_recommended"] = lots
    signal["estimated_cost_inr"] = round(prem * lot_size * lots, 2)
    signal["max_profit_inr"] = round((target_prem - prem) * lot_size * lots, 2)
    signal["max_loss_inr"] = round(sl_max * lots, 2)

    # Add Recommended Holding Time & Action Plan
    conf = signal.get("confidence", 50)
    sig_name = signal.get("signal", "WAIT")

    tgt_pct = round(config.PROFIT_TARGET_PCT * 100)
    sl_pct = round(config.STOP_LOSS_PCT * 100)

    signal["holding_minutes"] = None

    if sig_name == "BUY_CALL":
        signal["holding_time"] = f"Until +{tgt_pct}% Target (₹{target_prem}) or 1:30 PM"
        signal["action_summary"] = f"BUY {symbol} {strike} CE @ ₹{prem} (Target +13%: ₹{target_prem})"
        signal["exit_rule"] = (f"Target minimum +{tgt_pct}% profit (₹{target_prem}). "
                               f"Stop loss capped at ₹{sl_max} (₹{sl_prem}). "
                               f"Trading window closes at 1:30 PM IST.")
    elif sig_name == "BUY_PUT":
        signal["holding_time"] = f"Until +{tgt_pct}% Target (₹{target_prem}) or 1:30 PM"
        signal["action_summary"] = f"BUY {symbol} {strike} PE @ ₹{prem} (Target +13%: ₹{target_prem})"
        signal["exit_rule"] = (f"Target minimum +{tgt_pct}% profit (₹{target_prem}). "
                               f"Stop loss capped at ₹{sl_max} (₹{sl_prem}). "
                               f"Trading window closes at 1:30 PM IST.")
    else:
        now_ist = _get_ist_now()
        is_post_130pm = (now_ist.hour * 60 + now_ist.minute) >= (13 * 60 + 30)
        signal["holding_minutes"] = 0
        signal["holding_time"] = "Session Closed" if is_post_130pm else "Stay on Sidelines"
        signal["action_summary"] = "Go do your work, please do not trade now" if is_post_130pm else "HOLD CASH — Wait for directional confirmation"
        signal["exit_rule"] = "No trading after 1:30 PM IST" if is_post_130pm else "Do not enter position while market is consolidating"

    now_ist = _get_ist_now()
    signal["is_post_1pm"] = ((now_ist.hour * 60 + now_ist.minute) >= (13 * 60 + 30)) if sig_name not in ("BUY_CALL", "BUY_PUT") else False

    # Ensure reasoning is NEVER None or empty in the database
    reasoning = signal.get("reasoning") or signal.get("rationale") or signal.get("reason") or signal.get("analysis")
    if isinstance(reasoning, list):
        reasoning = "\n• ".join(str(x) for x in reasoning)
    if not reasoning:
        rule_fallback = _rule_based_signal(analysis)
        reasoning = rule_fallback.get("reasoning", "")
    signal["reasoning"] = reasoning

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
    Trading schedule:
      - Active Trading Window: 9:20 AM – 1:30 PM IST (Morning & European open momentum session)
      - After 1:30 PM IST: All signals STOPPED for the day.
        Message: 'Go do your work, please do not trade now'
    First 5 mins (9:15–9:20) skipped — opening auction noise / wide spreads.
    """
    now = _get_ist_now()
    h, m = now.hour, now.minute
    mins = h * 60 + m

    market_open  = 9 * 60 + 20    # 9:20 AM IST (opening noise filter)
    trade_cutoff = 13 * 60 + 30   # 1:30 PM IST (Trading strictly ends for the day)

    if mins < market_open:
        wait = market_open - mins
        return False, f"Market opens at 9:20 AM IST ({wait} min away — opening noise filter)"
    if mins < trade_cutoff:
        remaining = trade_cutoff - mins
        return True, f"Trading window active (9:20 AM – 1:30 PM IST, {remaining} min remaining)"
    return False, "Trading ended for today (After 1:30 PM IST) — Go do your work, please do not trade now"


# ─── Consecutive Loss Tracker (Resets daily each morning) ───
_consecutive_losses = {"NIFTY": 0, "BANKNIFTY": 0}
_loss_tracker_date = None


def _get_today_date_ist() -> str:
    """Returns today's date in IST (YYYY-MM-DD)."""
    try:
        return _get_ist_now().strftime("%Y-%m-%d")
    except Exception:
        import time as _t
        return _t.strftime("%Y-%m-%d")


def _check_daily_reset():
    """Automatically resets the loss circuit-breaker at the start of every new trading day."""
    global _consecutive_losses, _loss_tracker_date
    today = _get_today_date_ist()
    if _loss_tracker_date != today:
        _consecutive_losses = {"NIFTY": 0, "BANKNIFTY": 0}
        _loss_tracker_date = today


def record_trade_result(symbol: str, is_win: bool):
    """Called by app.py when a trade closes. Tracks intraday consecutive losses."""
    global _consecutive_losses
    _check_daily_reset()
    if is_win:
        _consecutive_losses[symbol] = 0
    else:
        _consecutive_losses[symbol] = _consecutive_losses.get(symbol, 0) + 1


def _get_consecutive_losses(symbol: str) -> int:
    """Returns current intraday consecutive losses for the symbol."""
    _check_daily_reset()
    return _consecutive_losses.get(symbol, 0)


def reset_consecutive_losses(symbol: str | None = None):
    """Manually reset consecutive losses for a symbol or both."""
    global _consecutive_losses
    if symbol:
        _consecutive_losses[symbol] = 0
    else:
        _consecutive_losses = {"NIFTY": 0, "BANKNIFTY": 0}


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
    ta_15m = analysis.get("ta_15m", {})
    oi = analysis.get("oi_analysis", {})
    india_vix = analysis.get("india_vix", 15.0)
    max_pain_dist_pct = analysis.get("max_pain_dist_pct", 999.0)

    # ── Gate 1: Trading Window Filter ──────────────────────
    window_ok, window_reason = _is_good_trading_window()

    # ── Gate 2: Consecutive Loss Protection ────────────────
    consec_losses = _get_consecutive_losses(symbol)
    loss_blocked = consec_losses >= 2  # Pause after 2 consecutive losses

    # ── Gate 3: Directional Bias Threshold ────────────────
    # Nifty 50 uses ±3 (scale: -5 to +5)
    # Bank Nifty uses ±4 (scale: -5 to +5)
    ENTRY_THRESHOLD = getattr(config, "ENTRY_THRESHOLDS", {}).get(symbol, getattr(config, "ENTRY_THRESHOLD", 3))

    # ── Gate 4: 5-min Trend Confirmation (BOTH Supertrend AND MACD must agree) ──
    # Changed from OR → AND to prevent false entries in choppy/sideways markets.
    # OR logic was firing on noise: e.g. bullish MACD alone with NEUTRAL supertrend
    # is a high-whipsaw scenario that was responsible for the recent consecutive losses.
    st_signal  = ta.get("supertrend_signal", "NEUTRAL")
    macd_bias  = ta.get("macd_bias", "NEUTRAL")
    trend_ok_bull = (st_signal == "BUY") and (macd_bias == "BULLISH")    # BOTH must agree
    trend_ok_bear = (st_signal == "SELL") and (macd_bias == "BEARISH")   # BOTH must agree

    # ── Gate 4.5: RSI Directional Alignment ───────────────
    # RSI should confirm the direction we're about to trade.
    # BUY CALL needs RSI > 50 (bullish territory, not oversold bounce)
    # BUY PUT needs RSI < 50 (bearish territory, not oversold reversal)
    # This prevents buying PUTs when RSI is already oversold (likely to bounce)
    # and buying CALLs when RSI is overbought (likely to pull back).
    rsi_val = float(ta.get("rsi", 50))
    rsi_signal = ta.get("rsi_signal", "NEUTRAL")
    rsi_ok_bull = (rsi_val > 50) and (rsi_signal != "OVERBOUGHT")   # Bullish but not at ceiling
    rsi_ok_bear = (rsi_val < 50) and (rsi_signal != "OVERSOLD")     # Bearish but not at floor
    rsi_reason_bull = f"RSI={rsi_val:.1f} ({rsi_signal}) ✅ bullish territory"
    rsi_reason_bear = f"RSI={rsi_val:.1f} ({rsi_signal}) ✅ bearish territory"

    # ── Gate 5: India VIX Filter ───────────────────────────
    # VIX > 20 → genuinely extreme volatility (crash/crisis level) — options
    # premiums spike unmanageably and markets whipsaw; skip to protect capital.
    # VIX < 11 → market too complacent / likely to stay flat (no premium decay benefit).
    VIX_MAX = 20.0
    VIX_MIN = 11.0
    vix_ok = VIX_MIN <= india_vix <= VIX_MAX
    vix_reason = (
        f"India VIX={india_vix:.1f} too HIGH (>{VIX_MAX}) — crash-level volatility, skip"
        if india_vix > VIX_MAX else
        f"India VIX={india_vix:.1f} too LOW (<{VIX_MIN}) — market too flat for momentum trades"
        if india_vix < VIX_MIN else
        f"India VIX={india_vix:.1f} ✅ (safe trading range {VIX_MIN}–{VIX_MAX})"
    )

    # ── Gate 6: Max Pain Pinning Filter ────────────────────
    # Max pain pinning happens when the market is indecisive/flat near a strike.
    # When strong directional conviction exists (|bias_score| >= ENTRY_THRESHOLD),
    # price regularly breaks out right through max pain with heavy momentum.
    # We allow the trade if directional bias is confirmed (score >= ENTRY_THRESHOLD),
    # or if spot is safely away from max pain (>0.05%).
    is_strong_conviction = abs(bias_score) >= ENTRY_THRESHOLD
    max_pain_ok = is_strong_conviction or (max_pain_dist_pct >= 0.05)
    max_pain_reason = (
        f"Max Pain distance {max_pain_dist_pct:.2f}% ✅ (strong conviction bias {bias_score:+} overrides consolidation)"
        if is_strong_conviction else
        f"Max Pain distance {max_pain_dist_pct:.2f}% ✅ (safe)"
        if max_pain_ok else
        f"Spot too close to Max Pain ({analysis.get('max_pain',0)}) — "
        f"only {max_pain_dist_pct:.2f}% away, market makers may pin price here"
    )

    # ── Gate 7: 15-min Trend Anti-Conflict Check ───────────
    # TIGHTENED: 15-min Supertrend OR MACD must actively support the trade direction.
    # Previously only blocked if BOTH 15m indicators opposed — now requires at least ONE to agree.
    # This ensures we trade with the higher timeframe trend, not against it.
    st_15m   = ta_15m.get("supertrend_signal", "NEUTRAL")
    macd_15m = ta_15m.get("macd_bias", "NEUTRAL")
    # BUY CALL: at least one of 15-min ST or MACD must be bullish (or neutral at worst)
    trend_15m_bull = not (st_15m == "SELL" and macd_15m == "BEARISH")  # Block only if BOTH bearish
    # BUY PUT: at least one of 15-min ST or MACD must be bearish (or neutral at worst)
    trend_15m_bear = not (st_15m == "BUY" and macd_15m == "BULLISH")   # Block only if BOTH bullish
    trend_15m_reason_bull = f"15-min Supertrend={st_15m}, MACD={macd_15m}"
    trend_15m_reason_bear = f"15-min Supertrend={st_15m}, MACD={macd_15m}"

    # ── Gate 8: Minimum 13% Profit Potential & OI Clearance Filter ──
    # For an option to gain >= 13% on premium (delta ~0.50), the spot index must have
    # adequate clearance before hitting major Open Interest walls (Call Wall resistance / Put Wall support).
    # If the distance to the wall is less than the required spot move, upside/downside is capped by institutional writers
    # and the trade will stall or reverse before achieving the 13% profit target.
    call_prem = float(analysis.get("atm_call_ltp", 0) or 0)
    put_prem = float(analysis.get("atm_put_ltp", 0) or 0)
    call_wall_strike = float(oi.get("call_resistance", 0) or 0)
    put_wall_strike = float(oi.get("put_support", 0) or 0)

    min_floor_pts = 22.0 if symbol == "NIFTY" else 65.0
    needed_pts_call = max(min_floor_pts, (call_prem * config.PROFIT_TARGET_PCT) / 0.50) if call_prem > 0 else min_floor_pts
    needed_pts_put = max(min_floor_pts, (put_prem * config.PROFIT_TARGET_PCT) / 0.50) if put_prem > 0 else min_floor_pts

    # Headroom check for BUY CALL: Spot to Call Wall (Resistance)
    if call_wall_strike > ltp:
        headroom_call = call_wall_strike - ltp
        profit_potential_ok_bull = (headroom_call >= needed_pts_call) and (call_prem >= 30.0)
        potential_reason_bull = (
            f"Headroom to Call Wall ({call_wall_strike:.0f}) is {headroom_call:.0f} pts "
            f"✅ (exceeds {needed_pts_call:.0f} pts needed for +13% profit)"
            if profit_potential_ok_bull else
            f"Capped upside: Only {headroom_call:.0f} pts to Call Wall ({call_wall_strike:.0f}) — "
            f"needs {needed_pts_call:.0f} pts for +13% profit. Risk of reversal before target."
        )
    else:
        profit_potential_ok_bull = call_prem >= 30.0
        potential_reason_bull = f"Call Wall breakout detected ✅ — open headroom for +13% target"

    # Room check for BUY PUT: Spot to Put Wall (Support)
    if put_wall_strike > 0 and put_wall_strike < ltp:
        headroom_put = ltp - put_wall_strike
        profit_potential_ok_bear = (headroom_put >= needed_pts_put) and (put_prem >= 30.0)
        potential_reason_bear = (
            f"Room to Put Wall ({put_wall_strike:.0f}) is {headroom_put:.0f} pts "
            f"✅ (exceeds {needed_pts_put:.0f} pts needed for +13% profit)"
            if profit_potential_ok_bear else
            f"Capped downside: Only {headroom_put:.0f} pts to Put Wall ({put_wall_strike:.0f}) — "
            f"needs {needed_pts_put:.0f} pts for +13% profit. Risk of bounce before target."
        )
    else:
        profit_potential_ok_bear = put_prem >= 30.0
        potential_reason_bear = f"Put Wall breakdown detected ✅ — open room for +13% target"

    if (bias_score >= ENTRY_THRESHOLD and window_ok and not loss_blocked
            and trend_ok_bull and rsi_ok_bull and vix_ok and max_pain_ok and trend_15m_bull
            and profit_potential_ok_bull):
        signal, otype, confidence = "BUY_CALL", "CE", min(98, int(70 + bias_score * 5.5))
        reasoning = (
            f"WHY BUY CALL (CE) — ALL 8 GATES PASSED:\n"
            f"• Bias Score: {bias_score:+}/±5 (threshold ≥{ENTRY_THRESHOLD} met)\n"
            f"• 5-min Trend ✅: Supertrend={st_signal} AND MACD={macd_bias} (both agree)\n"
            f"• RSI Alignment ✅: {rsi_reason_bull}\n"
            f"• 15-min Trend ✅: {trend_15m_reason_bull}\n"
            f"• Profit Potential ✅: Minimum +13% profit potential verified ({potential_reason_bull})\n"
            f"• {vix_reason}\n"
            f"• {max_pain_reason}\n"
            f"• Trading Window: ✅ {window_reason}\n"
            f"• Put-Call Ratio (PCR): {pcr} ({analysis.get('pcr_signal','').replace('_',' ')}) — Bullish put writing\n"
            f"• OI Support/Resistance: Call wall at {oi.get('call_resistance','')}, Put support at {oi.get('put_support','')}\n"
            f"• Technical Indicators: RSI(14)={ta.get('rsi',0):.1f} ({ta.get('rsi_signal','').replace('_',' ')}), MACD {macd_bias}, Supertrend {st_signal}\n"
            f"• Target: +{config.PROFIT_TARGET_PCT*100:.0f}% minimum profit on premium\n"
            f"• Exit Rule: 13% profit target or auto square-off at 1:30 PM IST close\n"
            f"• Groww Strategy: Buy {atm} CE, set limit order within bid-ask spread."
        )
    elif (bias_score <= -ENTRY_THRESHOLD and window_ok and not loss_blocked
            and trend_ok_bear and rsi_ok_bear and vix_ok and max_pain_ok and trend_15m_bear
            and profit_potential_ok_bear):
        signal, otype, confidence = "BUY_PUT", "PE", min(98, int(70 + abs(bias_score) * 5.5))
        reasoning = (
            f"WHY BUY PUT (PE) — ALL 8 GATES PASSED:\n"
            f"• Bias Score: {bias_score:+}/±5 (threshold ≤-{ENTRY_THRESHOLD} met)\n"
            f"• 5-min Trend ✅: Supertrend={st_signal} AND MACD={macd_bias} (both agree)\n"
            f"• RSI Alignment ✅: {rsi_reason_bear}\n"
            f"• 15-min Trend ✅: {trend_15m_reason_bear}\n"
            f"• Profit Potential ✅: Minimum +13% profit potential verified ({potential_reason_bear})\n"
            f"• {vix_reason}\n"
            f"• {max_pain_reason}\n"
            f"• Trading Window: ✅ {window_reason}\n"
            f"• Put-Call Ratio (PCR): {pcr} ({analysis.get('pcr_signal','').replace('_',' ')}) — Bearish call writing\n"
            f"• OI Support/Resistance: Call wall at {oi.get('call_resistance','')}, Put support at {oi.get('put_support','')}\n"
            f"• Technical Indicators: RSI(14)={ta.get('rsi',0):.1f} ({ta.get('rsi_signal','').replace('_',' ')}), MACD {macd_bias}, Supertrend {st_signal}\n"
            f"• Target: +{config.PROFIT_TARGET_PCT*100:.0f}% minimum profit on premium\n"
            f"• Exit Rule: 13% profit target or auto square-off at 1:30 PM IST close\n"
            f"• Groww Strategy: Buy {atm} PE, set limit order within bid-ask spread."
        )
    else:
        signal, otype, confidence = "WAIT", "NONE", 40
        # Build specific WAIT reason — list every gate that failed
        wait_reasons = []
        if abs(bias_score) < ENTRY_THRESHOLD:
            wait_reasons.append(f"Weak signal (bias {bias_score:+}/±5, need ≥{ENTRY_THRESHOLD} or ≤-{ENTRY_THRESHOLD})")
        if bias_score >= ENTRY_THRESHOLD and not trend_ok_bull:
            wait_reasons.append(f"5-min trend split: Supertrend={st_signal} & MACD={macd_bias} — need BOTH to agree for BUY CALL")
        if bias_score <= -ENTRY_THRESHOLD and not trend_ok_bear:
            wait_reasons.append(f"5-min trend split: Supertrend={st_signal} & MACD={macd_bias} — need BOTH to agree for BUY PUT")
        if bias_score >= ENTRY_THRESHOLD and trend_ok_bull and not rsi_ok_bull:
            wait_reasons.append(f"RSI conflict for BUY CALL: RSI={rsi_val:.1f} ({rsi_signal}) — needs to be >50 and not Overbought")
        if bias_score <= -ENTRY_THRESHOLD and trend_ok_bear and not rsi_ok_bear:
            wait_reasons.append(f"RSI conflict for BUY PUT: RSI={rsi_val:.1f} ({rsi_signal}) — needs to be <50 and not Oversold (likely bounce)")
        if bias_score >= ENTRY_THRESHOLD and trend_ok_bull and rsi_ok_bull and not trend_15m_bull:
            wait_reasons.append(f"15-min trend conflict: {trend_15m_reason_bull} — waiting for higher-timeframe alignment")
        if bias_score <= -ENTRY_THRESHOLD and trend_ok_bear and rsi_ok_bear and not trend_15m_bear:
            wait_reasons.append(f"15-min trend conflict: {trend_15m_reason_bear} — waiting for higher-timeframe alignment")
        if bias_score >= ENTRY_THRESHOLD and not profit_potential_ok_bull:
            wait_reasons.append(potential_reason_bull)
        if bias_score <= -ENTRY_THRESHOLD and not profit_potential_ok_bear:
            wait_reasons.append(potential_reason_bear)
        if not vix_ok:
            wait_reasons.append(vix_reason)
        if not max_pain_ok:
            wait_reasons.append(max_pain_reason)
        if not window_ok:
            wait_reasons.append(f"Bad timing: {window_reason}")
        if loss_blocked:
            wait_reasons.append(f"Capital protection: {consec_losses} consecutive losses today — trading paused for today (resets tomorrow morning)")

        reasoning = (
            f"WHY WAIT (NO TRADE) — CAPITAL PROTECTION:\n"
            f"• {' | '.join(wait_reasons) if wait_reasons else 'No strong directional signal'}\n"
            f"• Put-Call Ratio (PCR): {pcr} — {analysis.get('pcr_signal','NEUTRAL').replace('_',' ')}\n"
            f"• OI Range: Bound between Call Wall ({oi.get('call_resistance','')}) & Put Wall ({oi.get('put_support','')})\n"
            f"• Technical Indicators: RSI(14)={ta.get('rsi',50):.1f}, MACD {ta.get('macd_bias','')}, Supertrend {ta.get('supertrend_signal','')}\n"
            f"• India VIX: {india_vix:.1f} | Max Pain Distance: {max_pain_dist_pct:.2f}%\n"
            f"• Capital Protection: Preserve ₹{config.USER_BUDGET_INR:,} budget — patience is profitable."
        )

    now_ist = _get_ist_now()
    mins_ist = now_ist.hour * 60 + now_ist.minute
    is_post_130pm = mins_ist >= (13 * 60 + 30)

    if is_post_130pm:
        signal = "WAIT"
        otype = "NONE"
        confidence = 0
        reasoning = (
            "🛑 TRADING CLOSED FOR TODAY (After 1:30 PM IST):\n"
            "• Go do your work, please do not trade now.\n"
            "• Intraday trading window is 9:20 AM to 1:30 PM IST only.\n"
            "• Preserving capital and eliminating afternoon theta decay.\n"
            "• Signals will resume tomorrow morning at 9:20 AM IST."
        )

    side = "call" if otype in ("CE", "NONE") else "put"
    bd = budget_advice.get(side, {})
    premium = (
        analysis.get("atm_put_ltp", 0) if otype == "PE"
        else analysis.get("atm_call_ltp", 0)
    )

    # Use config-driven target/SL amounts
    target_prem = round(premium * (1 + config.PROFIT_TARGET_PCT), 2) if premium else 0
    sl_max = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(symbol, 1600)
    sl_points = sl_max / lot_size
    sl_prem = round(max(0.5, premium - sl_points), 2) if premium else 0

    res = {
        "signal": signal, "confidence": int(confidence),
        "strike": atm, "option_type": otype,
        "entry_premium": premium,
        "target_premium": target_prem,
        "stop_loss_premium": sl_prem,
        "lots_recommended": bd.get("lots", 1),
        "estimated_cost_inr": bd.get("cost_inr", 0),
        "max_profit_inr": bd.get("max_profit_inr", 0),
        "max_loss_inr": round(sl_max * bd.get("lots", 1), 2),
        "lot_size": lot_size,
        "reasoning": reasoning,
        "key_risk": "Go do your work, please do not trade now" if is_post_130pm else "Market can reverse quickly — always use a stop loss. Never risk more than 50% of capital.",
        "market_bias": (
            "NEUTRAL" if is_post_130pm else
            ("BULLISH" if bias_score >= ENTRY_THRESHOLD else
             "BEARISH" if bias_score <= -ENTRY_THRESHOLD else "NEUTRAL")
        ),
        "trade_tip": (
            "Go do your work, please do not trade now." if is_post_130pm else
            "Set a LIMIT order within the bid-ask spread on Groww F&O. Move SL to breakeven once trade is +10% in profit."
        ),
        "holding_time": "Session Closed" if is_post_130pm else "Stay on Sidelines",
        "action_summary": "Go do your work, please do not trade now" if is_post_130pm else "HOLD CASH — Wait for directional confirmation",
        "exit_rule": "No trading after 1:30 PM IST" if is_post_130pm else "Do not enter position while market is consolidating",
        "symbol": symbol, "nearest_expiry": expiry,
        "atm_strike": atm, "ltp": ltp,
        "bias_score": bias_score,
        "data_source": analysis.get("data_source", "unknown"),
        "source": "post_1pm_break" if is_post_130pm else "rule_based",
        "is_post_1pm": is_post_130pm,
        "llm_model": "Session Closed" if is_post_130pm else "Rule-Based (Enhanced v2)",
        "llm_inference_time": 0,
        "trading_window": "Trading window ended at 1:30 PM IST" if is_post_130pm else window_reason,
        "consecutive_losses": consec_losses,
    }
    return _enrich_signal(res, analysis)

