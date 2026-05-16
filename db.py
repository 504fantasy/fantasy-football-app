"""
db.py — Database abstraction layer
===================================
Switches between SQLite (dev) and PostgreSQL (production) based on
the DATABASE_URL environment variable.

  DATABASE_URL unset or empty  →  SQLite at DB_PATH
  DATABASE_URL=postgresql://…  →  PostgreSQL via psycopg2

All callers go through get_connection() and use the returned
connection object.  The wrapper normalises the two most important
behavioural differences so main.py doesn't need to know which DB is
running:

  1. Paramstyle  — SQLite uses ?   PostgreSQL uses %s
                   We expose a adapt_sql() helper and patch it in at
                   module load time so all SQL is written in SQLite
                   style and automatically translated for Postgres.

  2. Row access  — SQLite rows support dict-style access by column
                   name out of the box.  The Postgres wrapper adds a
                   RealDictCursor so rows behave the same way.

  3. lastrowid   — PostgreSQL cursors don't set lastrowid; we use
                   RETURNING id and expose it through a thin
                   execute_returning() helper.

  4. AUTOINCREMENT / SERIAL — handled in init_db() which has separate
                   DDL branches for each backend.

Usage
-----
    from db import get_connection, adapt_sql, execute_returning

    conn = get_connection()
    rows = conn.execute(adapt_sql("SELECT * FROM users WHERE id=?"), (uid,)).fetchall()
    conn.close()
"""

import os
import re
import sqlite3
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Backend detection
# ──────────────────────────────────────────────────────────────────────────────

DATABASE_URL: str | None = os.environ.get("DATABASE_URL", "").strip() or None
DB_PATH: str             = os.environ.get("DB_PATH", "data/fantasy.db")

USING_POSTGRES = bool(DATABASE_URL)


# ──────────────────────────────────────────────────────────────────────────────
# SQL dialect translation
# ──────────────────────────────────────────────────────────────────────────────

def adapt_sql(sql: str) -> str:
    """
    Convert SQLite-style ? placeholders to %s for PostgreSQL.
    Also replaces AUTOINCREMENT → (no-op, handled in DDL).
    Idempotent — calling twice is safe.
    """
    if not USING_POSTGRES:
        return sql
    # Replace ? with %s (only standalone ?, not ?> or similar)
    sql = re.sub(r"\?", "%s", sql)
    # SQLite-only keywords → Postgres equivalents
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("AUTOINCREMENT", "")
    return sql


# ──────────────────────────────────────────────────────────────────────────────
# SQLite connection (dev default)
# ──────────────────────────────────────────────────────────────────────────────

def _sqlite_connection():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # better concurrent reads
    return conn


# ──────────────────────────────────────────────────────────────────────────────
# PostgreSQL connection (production)
# ──────────────────────────────────────────────────────────────────────────────

def _pg_connection():
    """
    Returns a psycopg2 connection with a RealDictCursor factory so
    rows support column-name access identically to sqlite3.Row.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 is required for PostgreSQL mode.  "
            "Install it with:  pip install psycopg2-binary"
        ) from exc

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False

    # Monkey-patch .execute() onto the connection so callers can do
    # conn.execute(sql, params) like sqlite3 (convenience shim).
    # Real callers that need cursor.fetchall() should use get_cursor().
    _patch_pg_connection(conn)
    return conn


class _PgCursorProxy:
    """
    Wraps a psycopg2 RealDictCursor to mimic sqlite3's connection.execute()
    interface — returns self from execute() so .fetchone()/.fetchall()
    chain naturally.
    """
    def __init__(self, conn):
        import psycopg2.extras
        self._cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self.lastrowid: int | None = None

    def execute(self, sql: str, params: tuple = ()):
        self._cur.execute(adapt_sql(sql), params)
        return self

    def executemany(self, sql: str, seq):
        self._cur.executemany(adapt_sql(sql), seq)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _patch_pg_connection(conn):
    """Add .execute() and .cursor() helpers to a raw psycopg2 connection."""
    import psycopg2.extras

    def _execute(sql: str, params: tuple = ()):
        proxy = _PgCursorProxy(conn)
        proxy.execute(sql, params)
        return proxy

    def _cursor():
        return _PgCursorProxy(conn)

    def _commit():
        conn.commit()

    def _close():
        conn.close()

    conn.execute  = _execute   # type: ignore[attr-defined]
    conn.cursor   = _cursor    # type: ignore[attr-defined]
    conn.commit   = _commit    # type: ignore[attr-defined]
    conn.close    = _close     # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_connection():
    """Return a ready-to-use DB connection (SQLite or Postgres)."""
    if USING_POSTGRES:
        return _pg_connection()
    return _sqlite_connection()


def execute_returning(conn, sql: str, params: tuple = ()) -> int:
    """
    Execute an INSERT and return the new row's id.
    For Postgres, appends RETURNING id.  For SQLite, uses lastrowid.

    Example:
        lid = execute_returning(conn,
            "INSERT INTO leagues (name, commissioner_id) VALUES (?, ?)",
            (name, uid))
    """
    if USING_POSTGRES:
        returning_sql = adapt_sql(sql.rstrip(";")) + " RETURNING id"
        row = conn.execute(returning_sql, params).fetchone()
        return row["id"]
    else:
        cur = conn.execute(sql, params)
        # sqlite3 .execute() returns a cursor; lastrowid is on that object
        # But our conn.execute returns the raw cursor for sqlite3
        return cur.lastrowid


# ──────────────────────────────────────────────────────────────────────────────
# Schema initialisation
# ──────────────────────────────────────────────────────────────────────────────

def init_db(conn=None):
    """
    Create all tables, indexes, and run migrations.
    Called once at startup from main.py.
    Pass an existing connection to reuse it (useful in tests).
    """
    close_after = conn is None
    if conn is None:
        conn = get_connection()

    # SQLite pragmas are no-ops on Postgres
    if not USING_POSTGRES:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")

    pk = "SERIAL PRIMARY KEY" if USING_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    text_pk = "INTEGER PRIMARY KEY" if not USING_POSTGRES else "INTEGER PRIMARY KEY"

    # ── USERS ──────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS users (
        id            {pk},
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_superadmin INTEGER DEFAULT 0
    )""")

    # ── LEAGUES ────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS leagues (
        id                  {pk},
        name                TEXT NOT NULL,
        commissioner_id     INTEGER NOT NULL,
        invite_code         TEXT UNIQUE NOT NULL,
        created_at          TEXT NOT NULL,
        picks_per_team      INTEGER DEFAULT 15,
        max_ponies_per_team INTEGER DEFAULT 4,
        multiplier_lock_ts  TEXT DEFAULT '2099-01-01T00:00:00+00:00',
        pony_locked         INTEGER DEFAULT 0,
        pick_timer_seconds  INTEGER DEFAULT 0,
        FOREIGN KEY (commissioner_id) REFERENCES users(id)
    )""")

    # ── LEAGUE MEMBERS ─────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS league_members (
        id        {pk},
        league_id INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE CASCADE,
        FOREIGN KEY (user_id)   REFERENCES users(id),
        UNIQUE(league_id, user_id)
    )""")

    # ── TEAMS ──────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS teams (
        id        {pk},
        league_id INTEGER NOT NULL,
        name      TEXT NOT NULL,
        owner_id  INTEGER NOT NULL,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE CASCADE,
        FOREIGN KEY (owner_id)  REFERENCES users(id),
        UNIQUE(league_id, name)
    )""")

    # ── PLAYERS ────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS players (
        id        {pk},
        league_id INTEGER NOT NULL,
        name      TEXT NOT NULL,
        position  TEXT,
        nfl_team  TEXT,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE CASCADE,
        UNIQUE(league_id, name)
    )""")

    # ── TEAM ROSTER ────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS team_roster (
        id         {pk},
        team_id    INTEGER NOT NULL,
        player_id  INTEGER NOT NULL,
        is_pony    INTEGER DEFAULT 0,
        pony_revealed INTEGER DEFAULT 0,
        multiplier REAL,
        FOREIGN KEY (team_id)  REFERENCES teams(id)   ON DELETE CASCADE,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
        UNIQUE(team_id, player_id)
    )""")

    # ── DRAFT STATE ────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS draft_state (
        league_id       {text_pk},
        current_round   INTEGER DEFAULT 1,
        current_pick    INTEGER DEFAULT 0,
        is_complete     INTEGER DEFAULT 0,
        pick_started_at TEXT,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE CASCADE
    )""")

    # ── PLAYER SCORES ──────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS player_scores (
        id                  {pk},
        player_id           INTEGER NOT NULL,
        week                INTEGER NOT NULL,
        receptions          REAL DEFAULT 0,
        receiving_yards     REAL DEFAULT 0,
        rushing_yards       REAL DEFAULT 0,
        return_yards        REAL DEFAULT 0,
        passing_yards       REAL DEFAULT 0,
        total_tds           INTEGER DEFAULT 0,
        fumbles_lost        INTEGER DEFAULT 0,
        interceptions       INTEGER DEFAULT 0,
        field_goals_json    TEXT DEFAULT '[]',
        return_fumbles_lost INTEGER DEFAULT 0,
        override_points     REAL,
        override_note       TEXT,
        FOREIGN KEY (player_id) REFERENCES players(id),
        UNIQUE(player_id, week)
    )""")


    # ── ROSTER SNAPSHOTS ───────────────────────────────────────────────────
    # Records each player's multiplier at the moment multipliers are locked
    # for a given playoff week.  Scoring for completed weeks always uses the
    # snapshot so retroactive changes are impossible.
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS roster_snapshots (
        id          {pk},
        league_id   INTEGER NOT NULL,
        week        INTEGER NOT NULL,
        team_id     INTEGER NOT NULL,
        player_id   INTEGER NOT NULL,
        multiplier  REAL,
        snapshotted_at TEXT NOT NULL,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE CASCADE,
        UNIQUE(league_id, week, team_id, player_id)
    )""")

    # ── AUDIT LOG ──────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS audit_log (
        id        {pk},
        league_id INTEGER,
        ts        TEXT NOT NULL,
        actor     TEXT NOT NULL,
        action    TEXT NOT NULL,
        team      TEXT,
        player    TEXT,
        details   TEXT,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE SET NULL
    )""")

    # ── MIGRATIONS (idempotent column additions) ─────────────────────────
    for col_sql in [
        "ALTER TABLE team_roster ADD COLUMN pony_revealed INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass  # column already exists
    # Backfill: any pony picks added before the private-pony feature should be visible
    try:
        conn.execute("UPDATE team_roster SET pony_revealed=1 WHERE is_pony=1 AND pony_revealed=0")
        conn.commit()
    except Exception:
        pass

    # ── INDEXES ────────────────────────────────────────────────────────────
    _idx = [
        ("idx_roster_team",        "team_roster(team_id)"),
        ("idx_roster_player",      "team_roster(player_id)"),
        ("idx_scores_player_week", "player_scores(player_id, week)"),
        ("idx_audit_league",       "audit_log(league_id)"),
        ("idx_members_user",       "league_members(user_id)"),
        ("idx_members_league",     "league_members(league_id)"),
    ]
    for name, cols in _idx:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {cols}")

    # ── DRAFT CHAT / PICK LOG ─────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS draft_chat (
        id        {pk},
        league_id INTEGER NOT NULL,
        ts        TEXT NOT NULL,
        username  TEXT NOT NULL,
        msg_type  TEXT NOT NULL DEFAULT 'chat',
        message   TEXT NOT NULL,
        FOREIGN KEY (league_id) REFERENCES leagues(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_league ON draft_chat(league_id, ts)")

    # ── PASSWORD RESET TOKENS ──────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id         {pk},
        user_id    INTEGER NOT NULL,
        token      TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL,
        used       INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reset_token ON password_reset_tokens(token)")

    # ── MIGRATIONS (additive, safe to re-run) ──────────────────────────────
    _safe_alter(conn, "ALTER TABLE leagues     ADD COLUMN pick_timer_seconds  INTEGER DEFAULT 0")
    _safe_alter(conn, "ALTER TABLE draft_state ADD COLUMN pick_started_at     TEXT")
    _safe_alter(conn, "ALTER TABLE users       ADD COLUMN email               TEXT")
    _safe_alter(conn, "ALTER TABLE users       ADD COLUMN email_verified      INTEGER DEFAULT 0")

    conn.commit()
    if close_after:
        conn.close()


def _safe_alter(conn, sql: str):
    """Run an ALTER TABLE … ADD COLUMN, ignoring errors if column already exists."""
    try:
        conn.execute(sql)
        conn.commit()
    except Exception:
        pass  # column already exists


# ──────────────────────────────────────────────────────────────────────────────
# Convenience re-export so main.py can do:  from db import get_db
# ──────────────────────────────────────────────────────────────────────────────
get_db = get_connection
