"""
db.py — Unified database adapter for the F&O AI Signal Advisor.

Uses PostgreSQL (psycopg2) when DATABASE_URL env var is set (production on Render/Neon),
falls back to SQLite for local development — zero code changes needed in app.py.
"""
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return bool(DATABASE_URL)


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
        self._pg = is_postgres()
        if self._pg:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(
                DATABASE_URL,
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
            cur.execute(query.replace("?", "%s"), params)
            return _Cursor(cur)
        return self._conn.execute(query, params)

    def commit(self):
        self._conn.commit()

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
