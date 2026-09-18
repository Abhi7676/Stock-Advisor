"""
app.py — Indian F&O AI Signal Advisor
Flask REST API backend serving live Nifty/BankNifty signals,
options chain data, and market analysis.
"""

import json
import logging
import os
import db
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

# India Standard Time (UTC+5:30) — used for all signal timestamps
_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> str:
    """Current time in IST as ISO string (no timezone suffix for DB compatibility)."""
    return datetime.now(_IST).strftime("%Y-%m-%dT%H:%M:%S")

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import config
from analysis_engine import analyze_index
from gemini_advisor import (
    get_signal,
    is_gemini_available,
    is_gemini_paused,
    set_gemini_paused,
    get_gemini_model_name,
    get_last_error,
    get_gemini_call_logs,
    test_gemini_call,
)

# ─── App Setup ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", "fao-signal-secret-2026-xyz")
CORS(app)

# ─── In-Memory Cache ──────────────────────────────────────────────────────────
_cache = {
    "NIFTY": {"data": None, "signal": None, "last_refresh": 0},
    "BANKNIFTY": {"data": None, "signal": None, "last_refresh": 0},
}
_cache_lock = threading.Lock()


def _is_fresh(symbol: str, ttl: int = config.NSE_REFRESH_INTERVAL) -> bool:
    return (time.time() - _cache[symbol]["last_refresh"]) < ttl


def _refresh(symbol: str, force: bool = False):
    """Runs analysis + LLM signal generation and stores in cache."""
    if not force and _is_fresh(symbol):
        return
    logger.info(f"Refreshing analysis for {symbol}...")
    try:
        analysis = analyze_index(symbol)
        signal = get_signal(analysis)
        with _cache_lock:
            _cache[symbol]["data"] = analysis
            _cache[symbol]["signal"] = signal
            _cache[symbol]["last_refresh"] = time.time()
        _log_trade_signal(symbol, signal, analysis)
    except Exception as e:
        logger.error(f"Refresh failed for {symbol}: {e}")


def _background_refresh():
    """Background thread that keeps cache warm during market hours."""
    while True:
        for sym in ["NIFTY", "BANKNIFTY"]:
            _refresh(sym)
        time.sleep(config.NSE_REFRESH_INTERVAL)


# ─── Signal History DB ────────────────────────────────────────────────────────

def _init_db():
    conn = db.connect()
    # Step 1: Create table and commit immediately — so subsequent rollbacks
    # (from failed ALTER TABLE on existing columns) don't undo the creation.
    conn.execute(db.create_table_ddl())
    conn.commit()

    # Step 2: Schema migration — add new columns if they don't exist yet.
    # Uses IF NOT EXISTS (PostgreSQL 9.6+, SQLite 3.37+) to avoid errors.
    # Each column is committed independently so one failure can't abort the rest.
    for col, col_type in [
        ("exit_premium", "REAL"), ("pnl_pct", "REAL"), ("pnl_amount", "REAL"),
        ("status", "TEXT DEFAULT 'ACTIVE'"), ("closed_at", "TEXT")
    ]:
        try:
            conn.execute(f"ALTER TABLE signal_history ADD COLUMN IF NOT EXISTS {col} {col_type}")
            conn.commit()
        except Exception:
            conn.rollback()

    # Step 3: Clean up old WAIT rows so history only contains actual trade recommendations
    try:
        conn.execute("DELETE FROM signal_history WHERE signal = 'WAIT'")
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()


def _log_trade_signal(symbol: str, signal: dict, analysis: dict):
    """Tracks active trade signals (BUY_CALL / BUY_PUT), updates live PnL.
    Closes trades on: 13% profit target, stop-loss hit, or target_premium hit."""
    sig_name = signal.get("signal")
    try:
        conn = db.connect()

        # 1. Update existing ACTIVE trades for this symbol
        spot_now = analysis.get("ltp", 0)
        chain = analysis.get("chain", [])
        active_rows = conn.execute("""
            SELECT * FROM signal_history
            WHERE symbol = ? AND status LIKE 'ACTIVE%'
        """, (symbol,)).fetchall()

        for row in active_rows:
            row_id = row["id"]
            strike = row["strike"]
            otype = row["option_type"]
            entry_p = row["entry_premium"] or 1.0
            entry_spot = row["ltp"] or spot_now
            target_p = row["target_premium"] or (entry_p * 1.3)
            sl_p = row["sl_premium"] or (entry_p * 0.6)
            created_str = row["created_at"]

            # Calculate current option LTP from live NSE option chain
            curr_p = None
            for c in chain:
                if c.get("strike") == strike:
                    curr_p = float(c.get("call_ltp") if otype == "CE" else c.get("put_ltp") or 0)
                    break

            if not curr_p or curr_p <= 0:
                from nse_data import black_scholes
                T = (4.25 / 365.0) if symbol == "NIFTY" else (26.0 / 365.0)
                if symbol == "NIFTY":
                    iv = 0.1215 if otype == "CE" else 0.088
                else:
                    iv = 0.115 if otype == "CE" else 0.113
                curr_p = black_scholes(spot_now, strike, T, 0.07, iv, otype)

            lot_size = config.INDICES[symbol]["lot_size"]
            pnl_pct = round(((curr_p - entry_p) / entry_p) * 100.0, 2)
            pnl_amt = round((curr_p - entry_p) * lot_size, 2)

            new_status = "ACTIVE"
            now_iso = _now_ist()
            closed_at = None

            # ── Trailing Stop Loss Logic ─────────────────────
            # Once trade reaches +10% profit, trail SL to breakeven (entry price)
            effective_sl = sl_p
            if pnl_pct >= 10.0:
                effective_sl = entry_p  # Move SL to breakeven
                new_status = "ACTIVE (SL→BE)"  # Show trailing status in UI

            is_win = False
            if pnl_pct >= 13.0:
                # ── 13% Profit Cap — save & close trade immediately ──
                new_status = f"✅ PROFIT TARGET HIT +{pnl_pct}% 🎯"
                closed_at = now_iso
                is_win = True
            elif curr_p >= target_p:
                new_status = f"PROFIT BOOKED ({'+' if pnl_pct >= 0 else ''}{pnl_pct}%) 🎉"
                closed_at = now_iso
                is_win = True
            elif curr_p <= effective_sl:
                if effective_sl == entry_p and pnl_pct >= -0.5:
                    new_status = f"BREAKEVEN EXIT (SL trailed) {pnl_pct:+}%"
                    is_win = True  # Breakeven is not a loss
                else:
                    new_status = f"STOP LOSS HIT ({pnl_pct}%) 🛑"
                closed_at = now_iso

            # Track consecutive wins/losses for capital protection
            if closed_at:
                from gemini_advisor import record_trade_result
                record_trade_result(symbol, is_win)

            conn.execute("""
                UPDATE signal_history
                SET exit_premium = ?, pnl_pct = ?, pnl_amount = ?, status = ?, closed_at = ?
                WHERE id = ?
            """, (curr_p, pnl_pct, pnl_amt, new_status, closed_at, row_id))

        # 2. Log NEW trade if BUY_CALL or BUY_PUT and no active trade in progress
        if sig_name in ("BUY_CALL", "BUY_PUT"):
            recent_trade = conn.execute("""
                SELECT id FROM signal_history
                WHERE symbol = ? AND signal = ? AND status LIKE 'ACTIVE%'
            """, (symbol, sig_name)).fetchone()

            if not recent_trade:
                entry_p = signal.get("entry_premium", 0)
                conn.execute("""
                    INSERT INTO signal_history
                    (symbol, signal, confidence, strike, option_type, entry_premium,
                     target_premium, sl_premium, exit_premium, pnl_pct, pnl_amount,
                     ltp, bias_score, reasoning, source, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    symbol,
                    sig_name,
                    signal.get("confidence"),
                    signal.get("strike"),
                    signal.get("option_type"),
                    entry_p,
                    signal.get("target_premium"),
                    signal.get("stop_loss_premium"),
                    entry_p,
                    0.0,
                    0.0,
                    analysis.get("ltp"),
                    signal.get("bias_score"),
                    signal.get("reasoning"),
                    signal.get("source"),
                    "ACTIVE",
                    _now_ist(),
                ))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Trade log tracking failed: {e}")


# ─── REST API Routes ──────────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/gemini-monitor", methods=["GET"])
@app.route("/gemini_monitor.html", methods=["GET"])
def route_gemini_monitor():
    """Serves the Gemini API Call Inspector & Monitor page."""
    return send_from_directory(".", "gemini_monitor.html")


@app.route("/api/signal/<symbol>", methods=["GET"])
def api_signal(symbol: str):
    """Returns AI-generated CALL/PUT signal for NIFTY or BANKNIFTY."""
    symbol = symbol.upper()
    if symbol not in config.INDICES:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 404

    force = request.args.get("refresh", "false").lower() == "true"
    _refresh(symbol, force=force)

    with _cache_lock:
        signal = _cache[symbol]["signal"]
        analysis = _cache[symbol]["data"]
        last_refresh = _cache[symbol]["last_refresh"]

    if not signal:
        return jsonify({"error": "Signal not ready yet, please try again"}), 503

    return jsonify({
        "status": "ok",
        "symbol": symbol,
        "signal": signal,
        "analysis_summary": {
            "ltp": analysis.get("ltp"),
            "change_pct": analysis.get("change_pct"),
            "pcr": analysis.get("pcr"),
            "pcr_signal": analysis.get("pcr_signal"),
            "bias_score": analysis.get("bias_score"),
            "preliminary_bias": analysis.get("preliminary_bias"),
            "ta": analysis.get("ta"),
        },
        "last_refresh": datetime.fromtimestamp(last_refresh).strftime("%H:%M:%S") if last_refresh else "N/A",
        "gemini_active": bool(config.GEMINI_API_KEY),
    })


@app.route("/api/options-chain/<symbol>", methods=["GET"])
def api_options_chain(symbol: str):
    """Returns live options chain around ATM."""
    symbol = symbol.upper()
    if symbol not in config.INDICES:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 404

    _refresh(symbol)
    with _cache_lock:
        analysis = _cache[symbol]["data"]

    if not analysis:
        return jsonify({"error": "Data not ready"}), 503

    return jsonify({
        "status": "ok",
        "symbol": symbol,
        "underlying_value": analysis["ltp"],
        "atm_strike": analysis["atm_strike"],
        "nearest_expiry": analysis["nearest_expiry"],
        "pcr": analysis["pcr"],
        "pcr_signal": analysis["pcr_signal"],
        "max_pain": analysis["max_pain"],
        "chain": analysis["chain_summary"],
        "oi_analysis": analysis["oi_analysis"],
        "data_source": analysis["data_source"],
        "timestamp": analysis["timestamp"],
    })


@app.route("/api/market-pulse", methods=["GET"])
def api_market_pulse():
    """Summary of both indices for the dashboard overview."""
    result = {}
    for sym in ["NIFTY", "BANKNIFTY"]:
        _refresh(sym)
        with _cache_lock:
            a = _cache[sym]["data"]
            s = _cache[sym]["signal"]

        if a and s:
            result[sym] = {
                "display_name": config.INDICES[sym]["display_name"],
                "ltp": a["ltp"],
                "change": a["change"],
                "change_pct": a["change_pct"],
                "pcr": a["pcr"],
                "pcr_signal": a["pcr_signal"],
                "max_pain": a["max_pain"],
                "atm_strike": a["atm_strike"],
                "bias_score": a["bias_score"],
                "preliminary_bias": a["preliminary_bias"],
                "signal": s["signal"],
                "confidence": s["confidence"],
                "option_type": s["option_type"],
                "ta_rsi": a["ta"]["rsi"],
                "ta_macd_bias": a["ta"]["macd_bias"],
                "supertrend": a["ta"]["supertrend_signal"],
                "nearest_expiry": a["nearest_expiry"],
                "data_source": a["data_source"],
            }
    return jsonify({"status": "ok", "data": result})


@app.route("/api/signal-history", methods=["GET"])
def api_signal_history():
    """Returns recent trade signals with their outcome (13% profit target / SL / active)."""
    limit = int(request.args.get("limit", 20))
    try:
        conn = db.connect()
        # Fetch all trade signals, newest first
        rows = conn.execute(
            "SELECT * FROM signal_history WHERE signal IN ('BUY_CALL','BUY_PUT') ORDER BY id DESC LIMIT 200"
        ).fetchall()
        conn.close()

        result = []
        for r in rows:
            row_dict = dict(r)

            # Build a clear human-readable outcome for the UI
            status = row_dict.get("status", "ACTIVE")
            pnl = row_dict.get("pnl_pct") or 0
            entry_p = row_dict.get("entry_premium") or 0
            exit_p = row_dict.get("exit_premium") or 0

            if "ACTIVE" in status:
                row_dict["outcome"] = "⏳ ACTIVE"
                row_dict["outcome_class"] = "active"
            elif pnl > 0:
                row_dict["outcome"] = f"✅ PROFIT +{pnl:.1f}%  (Entry ₹{entry_p} → Exit ₹{exit_p:.1f})"
                row_dict["outcome_class"] = "profit"
            elif pnl < 0:
                row_dict["outcome"] = f"❌ LOSS {pnl:.1f}%  (Entry ₹{entry_p} → Exit ₹{exit_p:.1f})"
                row_dict["outcome_class"] = "loss"
            else:
                row_dict["outcome"] = f"➖ BREAKEVEN {pnl:.1f}%"
                row_dict["outcome_class"] = "neutral"

            result.append(row_dict)
            if len(result) >= limit:
                break

        return jsonify({"status": "ok", "count": len(result), "data": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ticker", methods=["GET"])
def api_ticker():
    """Returns live market tickers for the marquee bar (Nifty, BankNifty, Sensex, VIX, Commodities)."""
    from nse_data import get_all_market_tickers
    try:
        tickers = get_all_market_tickers()
        return jsonify({"status": "ok", "data": tickers})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/gemini-status", methods=["GET"])
def api_gemini_status():
    """Check if Gemini API is configured and available."""
    available = is_gemini_available()
    model = get_gemini_model_name() if available else None
    has_key = bool(config.GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', ''))
    last_err = get_last_error()
    return jsonify({
        "running": available and has_key,
        "model": model,
        "engine": "Google Gemini API (Free Tier)",
        "api_key_set": has_key,
        "last_error": last_err,
        "paused": is_gemini_paused(),
        "get_key_url": "https://aistudio.google.com/app/apikey",
    })


@app.route("/api/gemini-calls", methods=["GET"])
def api_gemini_calls():
    """Returns recent Gemini API calls including exact prompts and responses."""
    limit = int(request.args.get("limit", 50))
    logs = get_gemini_call_logs(limit)
    return jsonify({
        "status": "ok",
        "count": len(logs),
        "data": logs,
        "active_model": get_gemini_model_name(),
        "is_available": is_gemini_available(),
    })

@app.route("/api/gemini-pause", methods=["POST", "GET"])
def api_gemini_pause():
    """GET: returns current pause state. POST: toggles or sets pause state."""
    if request.method == "GET":
        return jsonify({"paused": is_gemini_paused()})

    # POST — body can optionally send {"paused": true/false}; omit for toggle
    payload = request.get_json(silent=True) or {}
    if "paused" in payload:
        new_state = bool(payload["paused"])
    else:
        new_state = not is_gemini_paused()  # toggle
    set_gemini_paused(new_state)
    logger.info(f"Gemini API calls {'PAUSED' if new_state else 'RESUMED'} by user request.")
    return jsonify({"paused": new_state, "message": "Gemini API calls paused — token save mode active." if new_state else "Gemini API calls resumed."})



@app.route("/api/gemini-test-call", methods=["POST"])
def api_gemini_test_call():
    """Triggers an interactive test call to Gemini and returns immediate prompt & response."""
    payload = request.get_json(silent=True) or {}
    custom_prompt = payload.get("prompt")
    symbol = payload.get("symbol", "TEST")
    result = test_gemini_call(custom_prompt, symbol)
    return jsonify(result)


@app.route("/api/refresh/<symbol>", methods=["POST"])
def api_force_refresh(symbol: str):
    """Force-refreshes analysis and signal for a symbol."""
    symbol = symbol.upper()
    if symbol not in config.INDICES:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 404
    _refresh(symbol, force=True)
    return jsonify({"status": "ok", "message": f"Refreshed {symbol}"})


@app.route("/api/debug", methods=["GET"])
def api_debug():
    """Returns raw live data for debugging — compare with Groww."""
    from nse_data import get_index_quote as giq, _nse
    result = {}
    for sym in ["NIFTY", "BANKNIFTY"]:
        q = giq(sym)
        with _cache_lock:
            cached = _cache[sym]
        result[sym] = {
            "live_quote": q,
            "cache_ltp": cached["data"]["ltp"] if cached["data"] else None,
            "cache_banknifty_ltp": cached["data"]["ltp"] if (cached["data"] and sym == "BANKNIFTY") else None,
            "signal": cached["signal"]["signal"] if cached["signal"] else None,
            "data_source": cached["data"]["data_source"] if cached["data"] else None,
        }
    return jsonify(result)


@app.route("/<path:path>")
def static_files(path):
    if os.path.exists(os.path.join(config.BASE_DIR, path)):
        return send_from_directory(".", path)
    return send_from_directory(".", "index.html")


# ─── Startup ──────────────────────────────────────────────────────────────────

# Initialize DB and start background threads at import time
# (works for both `python app.py` and gunicorn)
_init_db()
logger.info("Starting initial data fetch for Nifty & BankNifty...")
for _sym in ["NIFTY", "BANKNIFTY"]:
    _t = threading.Thread(target=_refresh, args=(_sym, True), daemon=True)
    _t.start()
_bg = threading.Thread(target=_background_refresh, daemon=True)
_bg.start()


if __name__ == "__main__":
    logger.info(f"🚀 F&O AI Advisor running at http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)
