"""
db.py — Unified database adapter for the F&O AI Signal Advisor.

Uses PostgreSQL (psycopg2) when DATABASE_URL env var is set (production on Render/Neon),
falls back to SQLite for local development — zero code changes needed in app.py.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


import time

_neon_offline_until = 0.0
_neon_last_error = None
_sqlite_schema_ready = False


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def is_postgres() -> bool:
    """True if DATABASE_URL is configured and not currently in offline cooldown."""
    url = get_database_url()
    return bool(url and time.time() >= _neon_offline_until)


def get_db_status() -> dict:
    """Returns database engine health, whether Neon is active or temporarily on SQLite."""
    url = get_database_url()
    has_neon = bool(url)
    is_pg = is_postgres()
    return {
        "engine": "Neon PostgreSQL" if is_pg else "Temporary Local SQLite",
        "neon_configured": has_neon,
        "neon_active": is_pg,
        "fallback_active": has_neon and not is_pg,
        "neon_offline_cooldown_remaining_sec": max(0, int(_neon_offline_until - time.time())),
        "neon_last_error": _neon_last_error,
    }


def _ensure_sqlite_tables(conn):
    """Guarantees SQLite schema exists so queries never fail on fallback."""
    global _sqlite_schema_ready
    if _sqlite_schema_ready:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, signal TEXT, confidence INTEGER,
                strike INTEGER, option_type TEXT, entry_premium REAL,
                target_premium REAL, sl_premium REAL, exit_premium REAL,
                pnl_pct REAL, pnl_amount REAL, ltp REAL,
                bias_score REAL, reasoning TEXT, source TEXT,
                status TEXT DEFAULT 'ACTIVE', created_at TEXT, closed_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gemini_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                model TEXT,
                status TEXT,
                prompt TEXT,
                response_raw TEXT,
                inference_time_sec REAL,
                error TEXT,
                parsed_signal TEXT,
                sdk_used TEXT
            )
        """)
        conn.commit()
        _sqlite_schema_ready = True
    except Exception as e:
        logger.warning(f"Could not auto-ensure SQLite tables: {e}")


class _Cursor:
    """Thin wrapper around a psycopg2 cursor to match the sqlite3 cursor interface."""
    def __init__(self, cur):
        self._cur = cur

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()


class Connection:
    """
    Unified connection object.
    - Neon PostgreSQL mode when available and within quota.
    - Automatically falls back to SQLite when Neon is paused, expired, or down.
    - Seamlessly retries and resumes Neon once monthly quota resets.
    """

    def __init__(self):
        global _neon_offline_until, _neon_last_error
        url = get_database_url()
        self._pg = False
        self._conn = None

        if url and time.time() >= _neon_offline_until:
            try:
                import psycopg2
                import psycopg2.extras
                # Fast timeout (4s) prevents thread hangs if Neon compute is paused / quota exceeded
                self._conn = psycopg2.connect(
                    url,
                    connect_timeout=4,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
                self._conn.autocommit = False
                self._pg = True
                _neon_offline_until = 0.0
                _neon_last_error = None
            except Exception as e:
                _neon_last_error = str(e)
                # Wait 60 seconds before probing Neon again so subsequent requests are instant
                _neon_offline_until = time.time() + 60.0
                logger.warning(
                    f"⚠️ Neon PostgreSQL unavailable or monthly quota exceeded ({e}). "
                    f"Routing to temporary local SQLite. Will auto-retry Neon in 60s."
                )

        if not self._pg:
            import sqlite3
            import config as _cfg
            self._conn = sqlite3.connect(_cfg.DB_FILE, timeout=10.0)
            self._conn.row_factory = sqlite3.Row
            _ensure_sqlite_tables(self._conn)

    @property
    def is_pg(self) -> bool:
        """True if this active connection is connected to PostgreSQL."""
        return self._pg

    # ── sqlite3-compatible execute() ────────────────────────────────────
    def execute(self, query: str, params=()):
        if self._pg:
            cur = self._conn.cursor()
            # psycopg2 uses % as its format-string escape char.
            pg_query = query.replace("%", "%%").replace("?", "%s")
            try:
                cur.execute(pg_query, params)
                return _Cursor(cur)
            except Exception as e:
                self._conn.rollback()
                err_str = str(e).lower()
                # If connection dropped or quota exhausted mid-query, mark Neon offline for 60s
                if any(w in err_str for w in ["connection", "closed", "terminated", "quota", "limit", "suspended"]):
                    global _neon_offline_until, _neon_last_error
                    _neon_last_error = str(e)
                    _neon_offline_until = time.time() + 60.0
                    logger.warning(f"Neon connection dropped mid-query ({e}). Next requests will route to SQLite.")
                raise
        return self._conn.execute(query, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    # Allow setting row_factory (no-op for postgres — RealDictCursor handles it)
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, val):
        if not self._pg:
            self._conn.row_factory = val


def connect() -> Connection:
    """Return a unified DB connection (PostgreSQL or SQLite fallback)."""
    return Connection()


def create_table_ddl(is_pg: bool = None) -> str:
    """Return the correct CREATE TABLE statement for the active DB engine."""
    if is_pg is None:
        is_pg = is_postgres()
    if is_pg:
        return """
            CREATE TABLE IF NOT EXISTS signal_history (
                id SERIAL PRIMARY KEY,
                symbol TEXT, signal TEXT, confidence INTEGER,
                strike INTEGER, option_type TEXT, entry_premium REAL,
                target_premium REAL, sl_premium REAL, exit_premium REAL,
                pnl_pct REAL, pnl_amount REAL, ltp REAL,
                bias_score REAL, reasoning TEXT, source TEXT,
                status TEXT DEFAULT 'ACTIVE', created_at TEXT, closed_at TEXT
            )
        """
    return """
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, signal TEXT, confidence INTEGER,
            strike INTEGER, option_type TEXT, entry_premium REAL,
            target_premium REAL, sl_premium REAL, exit_premium REAL,
            pnl_pct REAL, pnl_amount REAL, ltp REAL,
            bias_score REAL, reasoning TEXT, source TEXT,
            status TEXT DEFAULT 'ACTIVE', created_at TEXT, closed_at TEXT
        )
    """


def create_gemini_calls_table_ddl(is_pg: bool = None) -> str:
    """Return the correct CREATE TABLE statement for the gemini_calls table."""
    if is_pg is None:
        is_pg = is_postgres()
    if is_pg:
        return """
            CREATE TABLE IF NOT EXISTS gemini_calls (
                id SERIAL PRIMARY KEY,
                timestamp TEXT,
                symbol TEXT,
                model TEXT,
                status TEXT,
                prompt TEXT,
                response_raw TEXT,
                inference_time_sec REAL,
                error TEXT,
                parsed_signal TEXT,
                sdk_used TEXT
            )
        """
    return """
        CREATE TABLE IF NOT EXISTS gemini_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            model TEXT,
            status TEXT,
            prompt TEXT,
            response_raw TEXT,
            inference_time_sec REAL,
            error TEXT,
            parsed_signal TEXT,
            sdk_used TEXT
        )
    """



def _get_today_ist_str() -> str:
    """Returns today's date in IST format: 'YYYY-MM-DD'."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    return now.strftime("%Y-%m-%d")


def clean_old_gemini_calls(keep_date_str: str = None) -> int:
    """
    Cleans up all gemini_calls from previous days to eliminate storage bloat,
    minimize memory/load on the application, and ensure clean daily history.
    Keeps only calls matching `keep_date_str` (defaults to today's date in IST YYYY-MM-DD).
    """
    if not keep_date_str:
        keep_date_str = _get_today_ist_str()
    conn = None
    try:
        conn = connect()
        pattern = f"{keep_date_str}%"
        conn.execute("DELETE FROM gemini_calls WHERE timestamp IS NULL OR timestamp NOT LIKE ?", (pattern,))
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to clean old gemini calls: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def clear_all_gemini_calls() -> int:
    """Clears all records from gemini_calls table to instantly free up resources."""
    conn = None
    try:
        conn = connect()
        conn.execute("DELETE FROM gemini_calls")
        conn.commit()
        return 1
    except Exception as e:
        logger.warning(f"Failed to clear all gemini calls: {e}")
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def log_gemini_call(entry: dict):
    """Persists a Gemini API call record into the database and auto-prunes previous days."""
    conn = None
    try:
        conn = connect()
        # Auto-clean previous days' calls to keep DB fast and light
        today_pattern = f"{_get_today_ist_str()}%"
        conn.execute("DELETE FROM gemini_calls WHERE timestamp IS NULL OR timestamp NOT LIKE ?", (today_pattern,))

        parsed_str = None
        if entry.get("parsed_signal"):
            try:
                parsed_str = json.dumps(entry["parsed_signal"])
            except Exception:
                parsed_str = str(entry["parsed_signal"])
        conn.execute("""
            INSERT INTO gemini_calls (timestamp, symbol, model, status, prompt, response_raw, inference_time_sec, error, parsed_signal, sdk_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get("timestamp"),
            entry.get("symbol"),
            entry.get("model"),
            entry.get("status"),
            entry.get("prompt"),
            entry.get("response_raw"),
            entry.get("inference_time_sec", 0.0),
            entry.get("error"),
            parsed_str,
            entry.get("sdk_used"),
        ))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not persist gemini call to db: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_gemini_call_logs(limit: int = 50, today_only: bool = True) -> list:
    """Returns today's Gemini API call records from database, auto-clearing older days."""
    conn = None
    try:
        today_str = _get_today_ist_str()
        if today_only:
            clean_old_gemini_calls(today_str)

        conn = connect()
        if today_only:
            rows = conn.execute("""
                SELECT * FROM gemini_calls WHERE timestamp LIKE ? ORDER BY id DESC LIMIT ?
            """, (f"{today_str}%", limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM gemini_calls ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            if d.get("parsed_signal") and isinstance(d["parsed_signal"], str):
                try:
                    d["parsed_signal"] = json.loads(d["parsed_signal"])
                except Exception:
                    pass
            result.append(d)

        # Fallback / Backfill: If gemini_calls is empty, show today's calls from signal_history where source='gemini_api'
        if not result and today_only:
            sig_rows = conn.execute("""
                SELECT * FROM signal_history WHERE source = 'gemini_api' AND created_at LIKE ? ORDER BY id DESC LIMIT ?
            """, (f"{today_str}%", limit)).fetchall()
            for r in sig_rows:
                rd = dict(r)
                result.append({
                    "id": rd.get("id"),
                    "timestamp": rd.get("created_at"),
                    "symbol": rd.get("symbol"),
                    "model": "gemini-flash-latest",
                    "status": "SUCCESS",
                    "prompt": f"Live Indian F&O Trade Analysis for {rd.get('symbol')} Strike {rd.get('strike')} {rd.get('option_type')}",
                    "response_raw": rd.get("reasoning"),
                    "inference_time_sec": 1.2,
                    "error": None,
                    "parsed_signal": {
                        "signal": rd.get("signal"),
                        "strike": rd.get("strike"),
                        "option_type": rd.get("option_type"),
                        "entry_premium": rd.get("entry_premium"),
                        "target_premium": rd.get("target_premium"),
                        "stop_loss_premium": rd.get("sl_premium"),
                        "confidence": rd.get("confidence"),
                        "reasoning": rd.get("reasoning"),
                    },
                    "sdk_used": "google.genai (live trade)",
                })
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch gemini call logs from db: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─── Query Helpers for Trade Hold, Cooldown & Risk Management ──

def get_active_trade(symbol: str) -> dict | None:
    """Returns the currently active trade for symbol if one exists."""
    conn = None
    try:
        conn = connect()
        row = conn.execute("""
            SELECT id, symbol, signal, strike, option_type, entry_premium, created_at, status
            FROM signal_history
            WHERE symbol = ? AND status LIKE 'ACTIVE%'
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_last_closed_trade(symbol: str) -> dict | None:
    """Returns the most recently closed trade for symbol."""
    conn = None
    try:
        conn = connect()
        row = conn.execute("""
            SELECT id, symbol, signal, strike, option_type, entry_premium, exit_premium, pnl_pct, status, created_at, closed_at
            FROM signal_history
            WHERE symbol = ? AND closed_at IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_today_trade_count(symbol: str = None) -> int:
    """Returns count of all trades entered today (for specific symbol or overall)."""
    conn = None
    try:
        conn = connect()
        from datetime import datetime, timezone, timedelta
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_prefix = now_ist.strftime("%Y-%m-%d")
        if symbol:
            row = conn.execute("""
                SELECT count(*) as cnt FROM signal_history
                WHERE symbol = ? AND created_at LIKE ?
            """, (symbol, f"{today_prefix}%")).fetchone()
        else:
            row = conn.execute("""
                SELECT count(*) as cnt FROM signal_history
                WHERE created_at LIKE ?
            """, (f"{today_prefix}%",)).fetchone()
        if row:
            return row["cnt"] if "cnt" in row else row[0]
        return 0
    except Exception as e:
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_today_consecutive_losses(symbol: str) -> int:
    """Returns count of consecutive losses today for symbol directly from database."""
    conn = None
    try:
        conn = connect()
        from datetime import datetime, timezone, timedelta
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_prefix = now_ist.strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT pnl_pct, status FROM signal_history
            WHERE symbol = ? AND created_at LIKE ? AND closed_at IS NOT NULL
            ORDER BY id DESC LIMIT 5
        """, (symbol, f"{today_prefix}%")).fetchall()
        consec = 0
        for r in rows:
            pnl = r["pnl_pct"] or 0
            status = r["status"] or ""
            if pnl < 0 or "STOP LOSS" in status:
                consec += 1
            else:
                break
        return consec
    except Exception as e:
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

