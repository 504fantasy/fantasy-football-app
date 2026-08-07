"""
survivor_db.py — Database layer for 504 Fantasy Survivor
=========================================================
Mirrors the pattern from db.py but with a schema designed for the
Survivor-style game:

  • No draft — teams submit a weekly lineup instead.
  • Each team picks 1 QB, 1 RB, 1 WR, 1 TE, 1 DST, 1 K per week.
  • Once a player is used by a team, that player is LOCKED OUT for
    all future weeks for that team (but other teams may still use them).
  • Covers all 18 regular-season weeks.
  • Scoring reuses the same calculate_fantasy_points engine.

Switches between SQLite (dev) and PostgreSQL (prod) via DATABASE_URL
exactly like db.py.
"""

import os
import re
import sqlite3
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Backend detection
# ──────────────────────────────────────────────────────────────────────────────

DATABASE_URL: str | None = os.environ.get("SURVIVOR_DATABASE_URL", "") or \
                           os.environ.get("DATABASE_URL", "").strip() or None
DB_PATH: str             = os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db")

USING_POSTGRES = bool(DATABASE_URL)


# ──────────────────────────────────────────────────────────────────────────────
# SQL dialect translation  (identical to db.py)
# ──────────────────────────────────────────────────────────────────────────────

def adapt_sql(sql: str) -> str:
    if not USING_POSTGRES:
        return sql
    sql = re.sub(r"\?", "%s", sql)
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("AUTOINCREMENT", "")
    return sql


# ──────────────────────────────────────────────────────────────────────────────
# Connections
# ──────────────────────────────────────────────────────────────────────────────

def _sqlite_connection():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _pg_connection():
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 is required for PostgreSQL mode. "
            "Install: pip install psycopg2-binary"
        ) from exc

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    _patch_pg_connection(conn)
    return conn


class _PgCursorProxy:
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
    import psycopg2.extras

    def _execute(sql, params=()):
        proxy = _PgCursorProxy(conn)
        proxy.execute(sql, params)
        return proxy

    conn.execute  = _execute
    conn.cursor   = lambda: _PgCursorProxy(conn)
    conn.commit   = conn.commit
    conn.close    = conn.close


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_connection():
    if USING_POSTGRES:
        return _pg_connection()
    return _sqlite_connection()

get_db = get_connection


def execute_returning(conn, sql: str, params: tuple = ()) -> int:
    if USING_POSTGRES:
        returning_sql = adapt_sql(sql.rstrip(";")) + " RETURNING id"
        row = conn.execute(returning_sql, params).fetchone()
        return row["id"]
    else:
        cur = conn.execute(sql, params)
        return cur.lastrowid


# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

REQUIRED_POSITIONS = ("QB", "RB", "WR", "TE", "DST", "K")

def get_league_slots(league_id: int) -> dict:
    """Return {position: count} for how many slots each position has."""
    conn = get_connection()
    row = conn.execute(
        "SELECT slots_qb, slots_rb, slots_wr, slots_te, slots_dst, slots_k FROM survivor_leagues WHERE id=?",
        (league_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {p: 1 for p in REQUIRED_POSITIONS}
    return {
        "QB":  row["slots_qb"]  or 1,
        "RB":  row["slots_rb"]  or 1,
        "WR":  row["slots_wr"]  or 1,
        "TE":  row["slots_te"]  or 1,
        "DST": row["slots_dst"] or 1,
        "K":   row["slots_k"]   or 1,
    }
NFL_REGULAR_SEASON_WEEKS = 18
NFL_PRESEASON_WEEKS = 3

def get_total_weeks(league_id: int) -> int:
    """Return total weeks for a league based on its season_type."""
    conn = get_connection()
    row = conn.execute(
        "SELECT season_type FROM survivor_leagues WHERE id=?", (league_id,)
    ).fetchone()
    conn.close()
    if row and row["season_type"] == "preseason":
        return NFL_PRESEASON_WEEKS
    return NFL_REGULAR_SEASON_WEEKS


def init_db(conn=None):
    """
    Create all survivor tables and indexes.
    Safe to call repeatedly (CREATE IF NOT EXISTS everywhere).
    """
    close_after = conn is None
    if conn is None:
        conn = get_connection()

    if not USING_POSTGRES:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")

    pk = "SERIAL PRIMARY KEY" if USING_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

    # ── USERS ─────────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_users (
        id            {pk},
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_superadmin INTEGER DEFAULT 0
    )""")

    # ── LEAGUES ───────────────────────────────────────────────────────────────
    # submission_deadline_day  : 0=Sun … 6=Sat  (day of week picks lock)
    # submission_deadline_hour : 0-23 UTC
    # season                   : NFL season year (e.g. 2025)
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_leagues (
        id                       {pk},
        name                     TEXT NOT NULL,
        commissioner_id          INTEGER NOT NULL,
        invite_code              TEXT UNIQUE NOT NULL,
        created_at               TEXT NOT NULL,
        season                   INTEGER NOT NULL DEFAULT 2025,
        current_week             INTEGER NOT NULL DEFAULT 1,
        submission_deadline_day  INTEGER NOT NULL DEFAULT 0,
        submission_deadline_hour INTEGER NOT NULL DEFAULT 13,
        is_active                INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (commissioner_id) REFERENCES survivor_users(id)
    )""")

    # ── LEAGUE MEMBERS ────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_league_members (
        id        {pk},
        league_id INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        FOREIGN KEY (league_id) REFERENCES survivor_leagues(id) ON DELETE CASCADE,
        FOREIGN KEY (user_id)   REFERENCES survivor_users(id),
        UNIQUE(league_id, user_id)
    )""")

    # ── TEAMS ─────────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_teams (
        id        {pk},
        league_id INTEGER NOT NULL,
        name      TEXT NOT NULL,
        owner_id  INTEGER NOT NULL,
        FOREIGN KEY (league_id) REFERENCES survivor_leagues(id) ON DELETE CASCADE,
        FOREIGN KEY (owner_id)  REFERENCES survivor_users(id),
        UNIQUE(league_id, name)
    )""")

    # ── PLAYERS ───────────────────────────────────────────────────────────────
    # Shared pool per league (seeded from NFL rosters via nfl_sync, same as
    # the main game).  position must be one of QB/RB/WR/TE/DST/K.
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_players (
        id        {pk},
        league_id INTEGER NOT NULL,
        name      TEXT NOT NULL,
        position  TEXT NOT NULL,
        nfl_team  TEXT NOT NULL,
        FOREIGN KEY (league_id) REFERENCES survivor_leagues(id) ON DELETE CASCADE,
        UNIQUE(league_id, name)
    )""")

    # ── WEEKLY LINEUPS ────────────────────────────────────────────────────────
    # Each row = one position slot in a team's lineup for a given week.
    # A team may only have ONE entry per (team_id, week, position).
    # The same player may appear in multiple teams' lineups in the same week.
    # Once submitted for week W, that player_id is USED and cannot appear
    # in a lineup for weeks > W for the same team.
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_lineups (
        id         {pk},
        league_id  INTEGER NOT NULL,
        team_id    INTEGER NOT NULL,
        week       INTEGER NOT NULL,
        position   TEXT    NOT NULL,   -- QB / RB / WR / TE / DST / K
        player_id  INTEGER NOT NULL,
        locked     INTEGER NOT NULL DEFAULT 0,  -- 1 once the deadline passes
        submitted_at TEXT NOT NULL,
        FOREIGN KEY (league_id)  REFERENCES survivor_leagues(id) ON DELETE CASCADE,
        FOREIGN KEY (team_id)    REFERENCES survivor_teams(id)   ON DELETE CASCADE,
        FOREIGN KEY (player_id)  REFERENCES survivor_players(id),
        UNIQUE(team_id, week, position)
    )""")

    # ── PLAYER SCORES ─────────────────────────────────────────────────────────
    # Same structure as the main game's player_scores; scoring engine is shared.
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_player_scores (
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
        FOREIGN KEY (player_id) REFERENCES survivor_players(id),
        UNIQUE(player_id, week)
    )""")

    # ── AUDIT LOG ─────────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_audit_log (
        id        {pk},
        league_id INTEGER,
        ts        TEXT NOT NULL,
        actor     TEXT NOT NULL,
        action    TEXT NOT NULL,
        team      TEXT,
        player    TEXT,
        details   TEXT,
        FOREIGN KEY (league_id) REFERENCES survivor_leagues(id) ON DELETE SET NULL
    )""")

    # ── GAME SCHEDULE ─────────────────────────────────────────────────────────
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS survivor_game_schedule (
        id         {pk},
        league_id  INTEGER NOT NULL,
        season     INTEGER NOT NULL,
        week       INTEGER NOT NULL,
        team       TEXT NOT NULL,
        kickoff_utc TEXT NOT NULL,
        FOREIGN KEY (league_id) REFERENCES survivor_leagues(id) ON DELETE CASCADE,
        UNIQUE(league_id, week, team)
    )""")

    # ── INDEXES ───────────────────────────────────────────────────────────────
    _idx = [
        ("sidx_lineups_team_week",    "survivor_lineups(team_id, week)"),
        ("sidx_lineups_player",       "survivor_lineups(player_id)"),
        ("sidx_scores_player_week",   "survivor_player_scores(player_id, week)"),
        ("sidx_audit_league",         "survivor_audit_log(league_id)"),
        ("sidx_members_user",         "survivor_league_members(user_id)"),
        ("sidx_members_league",       "survivor_league_members(league_id)"),
        ("sidx_players_league_pos",   "survivor_players(league_id, position)"),
    ]
    for name, cols in _idx:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {cols}")

    # ── MIGRATIONS (additive, safe to re-run) ─────────────────────────────────
    _safe_alter(conn, "ALTER TABLE survivor_leagues ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    _safe_alter(conn, "ALTER TABLE survivor_leagues ADD COLUMN submission_deadline_day  INTEGER NOT NULL DEFAULT 0")
    _safe_alter(conn, "ALTER TABLE survivor_leagues ADD COLUMN submission_deadline_hour INTEGER NOT NULL DEFAULT 13")
    _safe_alter(conn, "ALTER TABLE survivor_game_schedule ADD COLUMN opponent TEXT")
    _safe_alter(conn, "ALTER TABLE survivor_game_schedule ADD COLUMN is_home INTEGER")

    conn.commit()
    if close_after:
        conn.close()


def _safe_alter(conn, sql: str):
    try:
        conn.execute(sql)
        conn.commit()
    except Exception:
        pass
