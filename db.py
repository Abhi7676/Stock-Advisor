"""
db.py — Unified database adapter for the F&O AI Signal Advisor.

Uses PostgreSQL (psycopg2) when DATABASE_URL env var is set (production on Render/Neon),
falls back to SQLite for local development — zero code changes needed in app.py.
"""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def is_postgres() -> bool:
    return bool(get_database_url())


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
    - sqlite3 mode: conn.execute() works natively, rows are sqlite3.Row objects.
    - psycopg2 mode: conn.execute() proxied through cursor, rows are RealDictRow (dict-like).
    Both support row["column_name"] and dict(row).
    """

    def __init__(self):
        url = get_database_url()
        self._pg = bool(url)
        if self._pg:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(
                url,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            self._conn.autocommit = False
        else:
            import sqlite3
            import config as _cfg
            self._conn = sqlite3.connect(_cfg.DB_FILE)
            self._conn.row_factory = sqlite3.Row

    # ── sqlite3-compatible execute() ────────────────────────────────────
    def execute(self, query: str, params=()):
        if self._pg:
            cur = self._conn.cursor()
            # psycopg2 uses % as its format-string escape char.
            # Escape any literal % in the SQL first (e.g. LIKE 'ACTIVE%' → 'ACTIVE%%'),
            # then replace SQLite-style ? placeholders with PostgreSQL %s.
            pg_query = query.replace("%", "%%").replace("?", "%s")
            try:
                cur.execute(pg_query, params)
            except Exception:
                # PostgreSQL marks the whole transaction as aborted on any error.
                # Rollback immediately so the connection is reusable.
                self._conn.rollback()
                raise
            return _Cursor(cur)
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
    """Return a unified DB connection (PostgreSQL or SQLite)."""
    return Connection()


def create_table_ddl() -> str:
    """Return the correct CREATE TABLE statement for the active DB engine."""
    if is_postgres():
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

