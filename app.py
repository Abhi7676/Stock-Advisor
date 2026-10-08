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


def _is_market_closed_ist() -> bool:
    """
    Returns True if current IST time is outside trading hours.
    Trading closes strictly at 3:00 PM IST (15:00) on weekdays.
    All open trades square off / round up at 3:00 PM IST.
    """
    now = datetime.now(_IST)
    if now.weekday() >= 5:  # Weekend
        return True
    mins = now.hour * 60 + now.minute
    # Trading hours: 9:15 AM (555 min) to 3:00 PM (900 min)
    return mins < (9 * 60 + 15) or mins >= (15 * 60)


def _auto_square_off_closed_trades(conn=None):
    """Squares off any lingering ACTIVE trades when market is closed (past 3:00 PM IST or past days)."""
    should_close_conn = False
    if conn is None:
        conn = db.connect()
        should_close_conn = True
    try:
        now_dt = datetime.now(_IST)
        now_iso = _now_ist()
        is_closed = _is_market_closed_ist()
        today_str = now_dt.strftime("%Y-%m-%d")

        active_rows = conn.execute("""
            SELECT * FROM signal_history WHERE status LIKE 'ACTIVE%'
        """).fetchall()

        for row in active_rows:
            row_id = row["id"]
            created_at = row["created_at"] or ""
            created_date = created_at[:10]
            # Square off if market is currently closed OR trade was entered on an earlier day
            if is_closed or (created_date and created_date < today_str):
                entry_p = row["entry_premium"] or 1.0
                curr_p = row["exit_premium"] or entry_p
                symbol = row["symbol"]
                lot_size = config.INDICES.get(symbol, {}).get("lot_size", 50)
                pnl_pct = round(((curr_p - entry_p) / entry_p) * 100.0, 2)
                pnl_amt = round((curr_p - entry_p) * lot_size, 2)
                sign = "+" if pnl_pct >= 0 else ""
                status_str = f"⏱️ SQUARED OFF AT 3:00 PM CLOSE ({sign}{pnl_pct}%)"
                conn.execute("""
                    UPDATE signal_history
                    SET exit_premium = ?, pnl_pct = ?, pnl_amount = ?, status = ?, closed_at = ?
                    WHERE id = ?
                """, (curr_p, pnl_pct, pnl_amt, status_str, now_iso, row_id))
        conn.commit()
    except Exception as e:
        logger.warning(f"Auto square-off sweep failed: {e}")
    finally:
        if should_close_conn:
            conn.close()

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import config
import gemini_advisor
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
from notifier import send_signal_alert, test_telegram_connection

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
_refresh_lock = {
    "NIFTY": threading.Lock(),
    "BANKNIFTY": threading.Lock(),
}


def _is_fresh(symbol: str, ttl: int = config.NSE_REFRESH_INTERVAL) -> bool:
    return (time.time() - _cache[symbol]["last_refresh"]) < ttl


def _refresh(symbol: str, force: bool = False, timeout: float = 0.0):
    """Runs analysis + LLM signal generation and stores in cache. Thread-safe."""
    if not force and _is_fresh(symbol):
        return
    lock = _refresh_lock.get(symbol)
    if not lock:
        return
    if timeout > 0:
        acquired = lock.acquire(blocking=True, timeout=timeout)
    else:
        acquired = lock.acquire(blocking=False)
    if not acquired:
        # Refresh already underway for this symbol — avoid duplicate concurrent requests
        return
    try:
        logger.info(f"Refreshing analysis for {symbol}...")
        analysis = analyze_index(symbol)
        signal = get_signal(analysis)
        with _cache_lock:
            _cache[symbol]["data"] = analysis
            _cache[symbol]["signal"] = signal
            _cache[symbol]["last_refresh"] = time.time()
        _log_trade_signal(symbol, signal, analysis)
    except Exception as e:
        logger.error(f"Refresh failed for {symbol}: {e}")
    finally:
        lock.release()


def _background_refresh():
    """Background thread that keeps cache warm during market hours with staggered requests."""
    time.sleep(1)  # Brief pause on startup so web server binds and listens instantly
    while True:
        for sym in ["NIFTY", "BANKNIFTY"]:
            try:
                _refresh(sym)
            except Exception as e:
                logger.error(f"Background refresh error for {sym}: {e}")
            time.sleep(2)  # Stagger indices by 2s so they don't hammer NSE/Yahoo simultaneously
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

    # Step 4: Repair any glitched rows where stop-loss exceeded the ₹1300 max cap
    try:
        rows_to_fix = conn.execute("""
            SELECT id, symbol, entry_premium FROM signal_history
            WHERE pnl_amount < -1350 AND status LIKE '%STOP LOSS%'
        """).fetchall()
        for r in rows_to_fix:
            rid = r["id"]
            sym = r["symbol"]
            ep = float(r["entry_premium"] or 100.0)
            lot = 65 if sym == "NIFTY" else 30
            fixed_exit = round(max(0.5, ep - (1300.0 / lot)), 2)
            fixed_pct = round(((fixed_exit - ep) / ep) * 100.0, 1)
            conn.execute("""
                UPDATE signal_history
                SET pnl_amount = -1300.0,
                    exit_premium = ?,
                    pnl_pct = ?,
                    status = ?
                WHERE id = ?
            """, (fixed_exit, fixed_pct, f"🛑 STOP LOSS EXIT (₹1300 loss, {fixed_pct}%)", rid))
        conn.commit()
    except Exception as e:
        logger.warning(f"Error repairing glitched SL rows: {e}")
        conn.rollback()

    conn.close()


# ₹1300 hard stop-loss: square off if live loss on a trade exceeds this
STOP_LOSS_AMOUNT_INR = 1300


def _log_trade_signal(symbol: str, signal: dict, analysis: dict):
    """Tracks active trade signals (BUY_CALL / BUY_PUT), updates live PnL.
    Closes trades on:
      1. 13% profit target hit → ✅ PROFIT TARGET HIT +13.0%
      2. ₹1300 stop loss hit  → 🛑 STOP LOSS EXIT (loss ≥ ₹1300)
      3. Auto square-off at 3:00 PM IST market close"""
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

        market_closed = _is_market_closed_ist()

        for row in active_rows:
            row_id = row["id"]
            strike = row["strike"]
            otype = row["option_type"]
            entry_p = row["entry_premium"] or 1.0
            entry_spot = row["ltp"] or spot_now
            target_p = row["target_premium"] or round(entry_p * (1.0 + config.PROFIT_TARGET_PCT), 2)
            sl_p = row["sl_premium"] or round(entry_p * (1.0 - config.STOP_LOSS_PCT), 2)
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

            is_win = False
            exit_p = curr_p

            if pnl_pct >= 13.0 or (target_p > 0 and curr_p >= target_p):
                # ── 13% Profit Target Hit — Book Profit Immediately ──
                exit_p = target_p if target_p > 0 else round(entry_p * 1.13, 2)
                pnl_pct = 13.0
                pnl_amt = round((exit_p - entry_p) * lot_size, 2)
                new_status = "✅ PROFIT TARGET HIT +13.0% 🎯"
                closed_at = now_iso
                is_win = True
            elif pnl_amt <= -STOP_LOSS_AMOUNT_INR:
                # ── ₹1300 Hard Stop Loss — Exit to Protect Capital ──
                # Order executed at stop-loss price; loss is strictly capped at ₹1300
                sl_exit_p = round(max(0.5, entry_p - (STOP_LOSS_AMOUNT_INR / lot_size)), 2)
                exit_p = sl_exit_p
                pnl_amt = -float(STOP_LOSS_AMOUNT_INR)
                pnl_pct = round(((exit_p - entry_p) / entry_p) * 100.0, 1)
                new_status = f"🛑 STOP LOSS EXIT (₹{STOP_LOSS_AMOUNT_INR} loss, {pnl_pct}%)"
                closed_at = now_iso
                is_win = False
                logger.info(f"Stop loss triggered for {symbol} row {row_id}: ₹{STOP_LOSS_AMOUNT_INR} loss ({pnl_pct}%)")
            elif market_closed:
                # ── Market Close (3:00 PM IST) — Auto Square-off ──
                exit_p = curr_p
                sign = "+" if pnl_pct >= 0 else ""
                new_status = f"⏱️ SQUARED OFF AT 3:00 PM CLOSE ({sign}{pnl_pct}%)"
                closed_at = now_iso
                is_win = (pnl_pct >= 0)

            # Track consecutive wins/losses for capital protection
            if closed_at:
                from gemini_advisor import record_trade_result
                record_trade_result(symbol, is_win)

            conn.execute("""
                UPDATE signal_history
                SET exit_premium = ?, pnl_pct = ?, pnl_amount = ?, status = ?, closed_at = ?
                WHERE id = ?
            """, (exit_p, pnl_pct, pnl_amt, new_status, closed_at, row_id))

        # 2. Log NEW trade only during market hours (before 3:00 PM IST) if BUY_CALL/BUY_PUT and no active trade
        if not market_closed and sig_name in ("BUY_CALL", "BUY_PUT"):
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
                # ★ Fire instant alert (Telegram + Email) in background threads
                send_signal_alert(signal)
                logger.info(f"New signal logged + alerts triggered: {symbol} {sig_name}")

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
    """Returns AI-generated CALL/PUT signal for NIFTY or BANKNIFTY without blocking worker."""
    symbol = symbol.upper()
    if symbol not in config.INDICES:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 404

    force = request.args.get("refresh", "false").lower() == "true"
    if force:
        # User requested manual refresh — wait up to 6 seconds so fresh tick & analysis return immediately
        _refresh(symbol, force=True, timeout=6.0)
    elif not _is_fresh(symbol):
        threading.Thread(target=_refresh, args=(symbol, False), daemon=True).start()

    with _cache_lock:
        signal = _cache[symbol]["signal"]
        analysis = _cache[symbol]["data"]
        last_refresh = _cache[symbol]["last_refresh"]

    if not signal or not analysis:
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
        "last_refresh": datetime.fromtimestamp(last_refresh, _IST).strftime("%H:%M:%S") if last_refresh else "N/A",
        "gemini_active": bool(gemini_advisor._get_api_keys()),
    })


@app.route("/api/options-chain/<symbol>", methods=["GET"])
def api_options_chain(symbol: str):
    """Returns live options chain around ATM without blocking worker."""
    symbol = symbol.upper()
    if symbol not in config.INDICES:
        return jsonify({"error": f"Unknown symbol: {symbol}"}), 404

    if not _is_fresh(symbol):
        threading.Thread(target=_refresh, args=(symbol,), daemon=True).start()

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
        if not _is_fresh(sym):
            threading.Thread(target=_refresh, args=(sym,), daemon=True).start()
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
                "ta_rsi": a["ta"]["rsi"] if a.get("ta") else 50,
                "ta_macd_bias": a["ta"]["macd_bias"] if a.get("ta") else "NEUTRAL",
                "supertrend": a["ta"]["supertrend_signal"] if a.get("ta") else "NEUTRAL",
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
        # Auto square-off any lingering active trades if market is closed (past 3:00 PM IST or past days)
        _auto_square_off_closed_trades(conn)
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


@app.route("/api/signal-history/clear", methods=["POST", "DELETE"])
def api_clear_signal_history():
    """Clears signal history on user request."""
    try:
        conn = db.connect()
        conn.execute("DELETE FROM signal_history")
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "message": "Signal history cleared successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/repair-glitch", methods=["GET", "POST"])
def api_repair_glitch():
    """Fixes or deletes the glitched -₹6032 trade row directly in Neon database."""
    action = request.args.get("action", "repair")
    try:
        conn = db.connect()
        if action == "delete":
            conn.execute("DELETE FROM signal_history WHERE pnl_amount < -1350")
            msg = "Glitched -₹6032 trade successfully deleted from database!"
        else:
            rows = conn.execute("SELECT id, entry_premium FROM signal_history WHERE pnl_amount < -1350").fetchall()
            for r in rows:
                ep = float(r["entry_premium"] or 714.0)
                exit_p = round(ep - (1300.0 / 30.0), 2)
                pct = round(((exit_p - ep) / ep) * 100.0, 1)
                conn.execute("""
                    UPDATE signal_history
                    SET pnl_amount = -1300.0,
                        exit_premium = ?,
                        pnl_pct = ?,
                        status = ?
                    WHERE id = ?
                """, (exit_p, pct, f"🛑 STOP LOSS EXIT (₹1300 loss, {pct}%)", r["id"]))
            msg = f"Repaired {len(rows)} glitched trade row(s) to strictly ₹1300 stop loss."
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/performance", methods=["GET"])
def api_performance():
    """Returns aggregated performance metrics from trade signal history."""
    try:
        conn = db.connect()
        _auto_square_off_closed_trades(conn)
        rows = conn.execute(
            "SELECT * FROM signal_history WHERE signal IN ('BUY_CALL','BUY_PUT') ORDER BY id DESC"
        ).fetchall()
        conn.close()

        total_trades = len(rows)
        active_trades = 0
        wins = 0
        losses = 0
        breakeven = 0
        total_pnl_amt = 0.0
        total_profit_amt = 0.0
        total_loss_amt = 0.0
        pnl_pcts = []

        by_sym = {
            "NIFTY": {"trades": 0, "wins": 0, "losses": 0, "pnl_amt": 0.0, "win_rate": 0.0},
            "BANKNIFTY": {"trades": 0, "wins": 0, "losses": 0, "pnl_amt": 0.0, "win_rate": 0.0},
        }

        for r in rows:
            sym = r["symbol"]
            status = r["status"] or ""
            pnl_p = r["pnl_pct"] or 0.0
            pnl_a = r["pnl_amount"] or 0.0

            if "ACTIVE" in status:
                active_trades += 1
                continue

            if pnl_p > 0:
                wins += 1
                total_profit_amt += pnl_a
                if sym in by_sym:
                    by_sym[sym]["wins"] += 1
            elif pnl_p < 0:
                losses += 1
                total_loss_amt += abs(pnl_a)
                if sym in by_sym:
                    by_sym[sym]["losses"] += 1
            else:
                breakeven += 1

            total_pnl_amt += pnl_a
            pnl_pcts.append(pnl_p)
            if sym in by_sym:
                by_sym[sym]["trades"] += 1
                by_sym[sym]["pnl_amt"] += pnl_a

        closed_trades = wins + losses + breakeven
        win_rate = round((wins / closed_trades * 100), 1) if closed_trades > 0 else 0.0
        loss_rate = round((losses / closed_trades * 100), 1) if closed_trades > 0 else 0.0
        profit_factor = round(total_profit_amt / total_loss_amt, 2) if total_loss_amt > 0 else (99.0 if total_profit_amt > 0 else 0.0)
        avg_return = round(sum(pnl_pcts) / len(pnl_pcts), 1) if pnl_pcts else 0.0
        best_trade = max(pnl_pcts) if pnl_pcts else 0.0
        worst_trade = min(pnl_pcts) if pnl_pcts else 0.0

        for sym in by_sym:
            s_trades = by_sym[sym]["trades"]
            by_sym[sym]["win_rate"] = round((by_sym[sym]["wins"] / s_trades * 100), 1) if s_trades > 0 else 0.0
            by_sym[sym]["pnl_amt"] = round(by_sym[sym]["pnl_amt"], 2)

        return jsonify({
            "status": "ok",
            "total_trades": total_trades,
            "closed_trades": closed_trades,
            "active_trades": active_trades,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "loss_rate": loss_rate,
            "net_pnl_inr": round(total_pnl_amt, 2),
            "total_profit_inr": round(total_profit_amt, 2),
            "total_loss_inr": round(total_loss_amt, 2),
            "profit_factor": profit_factor,
            "avg_return_pct": avg_return,
            "best_trade_pct": round(best_trade, 1),
            "worst_trade_pct": round(worst_trade, 1),
            "by_symbol": by_sym,
        })
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
    keys = gemini_advisor._get_api_keys()
    has_key = bool(keys)
    last_err = get_last_error()
    return jsonify({
        "running": available and has_key,
        "model": model,
        "engine": "Google Gemini API (Free Tier)",
        "api_key_set": has_key,
        "keys_count": len(keys),
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


@app.route("/api/telegram/status", methods=["GET"])
def api_telegram_status():
    """Returns Telegram configuration status (without exposing secrets)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return jsonify({
        "configured": bool(token and chat_id),
        "bot_token_set": bool(token),
        "chat_id_set": bool(chat_id),
        "masked_token": (token[:4] + "..." + token[-4:]) if len(token) > 8 else ("Set" if token else "Not Set"),
        "masked_chat_id": (chat_id[:2] + "..." + chat_id[-2:]) if len(chat_id) > 4 else ("Set" if chat_id else "Not Set"),
    })


@app.route("/api/telegram/test", methods=["GET", "POST"])
def api_telegram_test():
    """Sends a sample test alert to the configured Telegram chat."""
    result = test_telegram_connection()
    status_code = 200 if result.get("status") == "ok" else 400
    return jsonify(result), status_code


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

# Initialize DB and start single background refresh loop at import time
_init_db()
logger.info("Starting background market refresh thread for Nifty & BankNifty...")
_bg = threading.Thread(target=_background_refresh, daemon=True)
_bg.start()


if __name__ == "__main__":
    logger.info(f"🚀 F&O AI Advisor running at http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)
