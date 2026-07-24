from dotenv import load_dotenv

load_dotenv()  # load .env before anything else reads os.environ

import json
import os
import secrets
import sqlite3  # still needed for IntegrityError
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext

from db import adapt_sql, execute_returning, get_db
from db import init_db as _init_db
from nfl_sync import current_nfl_week, seed_players, sync_scheduler, sync_week
from scoring import calculate_fantasy_points
from stat_parsing import parse_team_stats  # noqa: F401 — re-exported for convenience

# ======================================================
# CONFIG
# ======================================================

# DB_PATH and DATABASE_URL are read by db.py; just reference them here for logging
DB_PATH = os.environ.get("DB_PATH", "data/fantasy.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ======================================================
# JWT CONFIG
# ======================================================

# Set JWT_SECRET in your environment (or .env). Falls back to a
# dev-only default — NEVER use the default in production.
JWT_SECRET = os.environ.get(
    "JWT_SECRET", "CHANGE_ME_in_production_use_a_long_random_string"
)
if JWT_SECRET == "CHANGE_ME_in_production_use_a_long_random_string":
    raise RuntimeError(
        "JWT_SECRET is not set! Add a strong random JWT_SECRET to your .env file. "
        "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
if len(JWT_SECRET) < 32:
    raise RuntimeError(
        f"JWT_SECRET is too short ({len(JWT_SECRET)} chars). Use at least 32 random characters."
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "72"))


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """Return username on valid token, None on any failure."""
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return data.get("sub")
    except JWTError:
        return None


# ======================================================
# PASSWORD
# ======================================================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    # bcrypt limit is 72 bytes — truncate to be safe
    return pwd_context.hash(password[:72])


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password[:72], password_hash)


# ======================================================
# DATABASE  (see db.py for schema and Postgres/SQLite abstraction)
# ======================================================

# get_db is imported from db.py — already done above


_init_db()  # create tables + run migrations via db.py
sync_scheduler.start()  # background NFL stat sync thread

# ======================================================
# DRAFT TIMER  — server-side auto-skip
# ======================================================

# Maps league_id → (pick_token, threading.Timer).  The pick_token is a
# simple counter; when a real pick lands it bumps the counter, making
# any in-flight timer's callback a no-op.
_timer_lock: threading.Lock = threading.Lock()
_active_timers: dict[int, tuple[int, threading.Timer]] = {}


def _timer_key(league_id: int, round_: int, pick: int) -> int:
    """Deterministic token: changes on every new pick."""
    return hash((league_id, round_, pick))


def arm_pick_timer(league_id: int, timer_seconds: int, round_: int, pick: int) -> None:
    """Start (or restart) the countdown for the current pick."""
    if not timer_seconds:
        return
    token = _timer_key(league_id, round_, pick)

    def _expire():
        with _timer_lock:
            entry = _active_timers.get(league_id)
            if not entry or entry[0] != token:
                return  # a real pick already fired; stale callback
            _active_timers.pop(league_id, None)
        _auto_skip(league_id, round_, pick)

    with _timer_lock:
        # Cancel any existing timer for this league
        old = _active_timers.get(league_id)
        if old:
            old[1].cancel()
        t = threading.Timer(timer_seconds, _expire)
        t.daemon = True
        t.start()
        _active_timers[league_id] = (token, t)


def cancel_pick_timer(league_id: int) -> None:
    with _timer_lock:
        entry = _active_timers.pop(league_id, None)
        if entry:
            entry[1].cancel()


def get_timer_remaining(
    league_id: int, timer_seconds: int, pick_started_at: str | None
) -> int | None:
    """Seconds remaining on the current pick clock, or None if disabled."""
    if not timer_seconds or not pick_started_at:
        return None
    try:
        started = datetime.fromisoformat(pick_started_at)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return max(0, int(timer_seconds - elapsed))
    except Exception:
        return None


def _auto_skip(league_id: int, expected_round: int, expected_pick: int) -> None:
    """Auto-pick next available player when timer expires."""
    conn = get_db()
    conn.isolation_level = None
    player_name = None
    team_name = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT current_round, current_pick, is_complete FROM draft_state WHERE league_id=?",
            (league_id,),
        ).fetchone()
        if not state:
            conn.execute("ROLLBACK"); return
        if state["current_round"] != expected_round or state["current_pick"] != expected_pick:
            conn.execute("ROLLBACK"); return
        if state["is_complete"]:
            conn.execute("ROLLBACK"); return
        teams = conn.execute(
            "SELECT * FROM teams WHERE league_id=? ORDER BY id", (league_id,)
        ).fetchall()
        league = conn.execute(
            "SELECT * FROM leagues WHERE id=?", (league_id,)
        ).fetchone()
        if not teams or not league:
            conn.execute("ROLLBACK"); return
        teams = sort_teams_by_draft_order(list(teams), dict(league))
        ordered = get_snake_order(teams, state["current_round"])
        team_on_clock = ordered[state["current_pick"] % len(ordered)]
        team_name = team_on_clock["name"]
        drafted_ids = {
            r[0] for r in conn.execute(
                "SELECT player_id FROM team_roster WHERE team_id IN "
                "(SELECT id FROM teams WHERE league_id=?)", (league_id,)
            ).fetchall()
        }
        available = conn.execute(
            "SELECT * FROM players WHERE league_id=? ORDER BY name ASC", (league_id,)
        ).fetchall()
        # Check team's queue first
        queue_rows = conn.execute(
            """SELECT dq.player_id FROM draft_queue dq
               WHERE dq.team_id=?
               ORDER BY dq.sort_order ASC""",
            (team_on_clock["id"],)
        ).fetchall()
        auto_player = None
        # Try queue players first
        for qrow in queue_rows:
            if qrow[0] not in drafted_ids:
                auto_player = conn.execute(
                    "SELECT * FROM players WHERE id=?", (qrow[0],)
                ).fetchone()
                if auto_player:
                    # Remove from queue since we're picking them
                    conn.execute(
                        "DELETE FROM draft_queue WHERE team_id=? AND player_id=?",
                        (team_on_clock["id"], qrow[0])
                    )
                    break
        # Fall back to alphabetical if no queue picks available
        if not auto_player:
            auto_player = next((p for p in available if p["id"] not in drafted_ids), None)
        picks_per_team = league["picks_per_team"] or 15
        picks_made = (state["current_round"] - 1) * len(teams) + state["current_pick"] + 1
        new_pick = state["current_pick"] + 1
        new_round = state["current_round"]
        if new_pick >= len(teams):
            new_pick = 0
            new_round += 1
        draft_now_complete = picks_made >= picks_per_team * len(teams)
        now_iso = datetime.now(timezone.utc).isoformat()
        if auto_player:
            conn.execute(
                "INSERT INTO team_roster (team_id, player_id, is_pony) VALUES (?,?,0)",
                (team_on_clock["id"], auto_player["id"]),
            )
            player_name = auto_player["name"]
        conn.execute(
            "UPDATE draft_state SET current_round=?, current_pick=?, is_complete=?, "
            "pick_started_at=? WHERE league_id=?",
            (new_round, new_pick, int(draft_now_complete), now_iso, league_id),
        )
        conn.execute("COMMIT")
        if not draft_now_complete and league["pick_timer_seconds"]:
            arm_pick_timer(league_id, league["pick_timer_seconds"], new_round, new_pick)
    except Exception as e:
        try: conn.execute("ROLLBACK")
        except: pass
    finally:
        conn.close()
    if player_name:
        write_audit(actor="system", action="DRAFT_PICK", league_id=league_id,
            team=team_name, player=player_name,
            details=f"AUTO-PICK R{expected_round} P{expected_pick+1}")
    else:
        write_audit(actor="system", action="DRAFT_SKIP", league_id=league_id,
            details=f"Timer expired R{expected_round} P{expected_pick+1}")


# ── CSRF Protection ────────────────────────────────────────────────────────────
def generate_csrf_token() -> str:
    return secrets.token_hex(32)

def get_csrf_token(request: Request) -> str:
    """Get or create a CSRF token stored in the session cookie."""
    token = request.cookies.get("csrf_token")
    if not token:
        token = generate_csrf_token()
    return token

def validate_csrf(request: Request, csrf_token: str = Form(None)) -> bool:
    """Validate CSRF token from form matches cookie."""
    cookie_token = request.cookies.get("csrf_token")
    if not cookie_token or not csrf_token:
        return False
    return secrets.compare_digest(cookie_token, csrf_token)

# ======================================================
# AUDIT LOG
# ======================================================


def write_audit(
    actor: str,
    action: str,
    league_id: int = None,
    team: str = None,
    player: str = None,
    details: str = None,
):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (league_id, ts, actor, action, team, player, details) VALUES (?,?,?,?,?,?,?)",
        (league_id, ts, actor, action, team, player, details),
    )
    conn.commit()
    conn.close()


# ======================================================
# SCORING ENGINE
# ======================================================


def row_to_stats(row: dict) -> dict:
    try:
        fgs = json.loads(row.get("field_goals_json") or "[]")
    except Exception:
        fgs = []
    return {
        "receptions": row.get("receptions", 0) or 0,
        "receiving_yards": row.get("receiving_yards", 0) or 0,
        "rushing_yards": row.get("rushing_yards", 0) or 0,
        "return_yards": row.get("return_yards", 0) or 0,
        "passing_yards": row.get("passing_yards", 0) or 0,
        "total_tds": row.get("total_tds", 0) or 0,
        "fumbles_lost": row.get("fumbles_lost", 0) or 0,
        "interceptions": row.get("interceptions", 0) or 0,
        "field_goals_made": fgs,
        "return_fumbles_lost": row.get("return_fumbles_lost", 0) or 0,
    }


def get_team_week_score(
    team_id: int, week: int, league_id: int = None, show_hidden_ponies: bool = False
) -> dict:
    """
    Score a team for a given week.  If a roster snapshot exists for the week
    (i.e. multipliers were locked before that round), the snapshot multipliers
    are used instead of the current live values, making past scores immutable.
    Pass league_id to enable snapshot lookup; if omitted it falls back to the
    live multiplier on the roster row.
    """
    conn = get_db()
    roster = conn.execute(
        """
        SELECT tr.id AS roster_id, tr.team_id, tr.player_id, tr.is_pony, tr.pony_revealed, tr.multiplier,
               p.name, p.position, p.nfl_team, p.headshot_url,
               ps.receptions, ps.receiving_yards, ps.rushing_yards,
               ps.return_yards, ps.passing_yards, ps.total_tds,
               ps.fumbles_lost, ps.interceptions, ps.field_goals_json,
               ps.return_fumbles_lost, ps.override_points, ps.override_note
        FROM team_roster tr
        JOIN players p ON tr.player_id = p.id
        LEFT JOIN player_scores ps ON ps.player_id = tr.player_id AND ps.week = ?
        WHERE tr.team_id = ?
    """,
        (week, team_id),
    ).fetchall()
    conn.close()

    # Load snapshot multipliers if available — these override live values
    pony_locked = False
    snap: dict = {}
    if league_id:
        snap = get_snapshot_multipliers(league_id, week)
        lconn = get_db()
        lr = lconn.execute("SELECT pony_locked FROM leagues WHERE id=?", (league_id,)).fetchone()
        lconn.close()
        if lr: pony_locked = bool(lr["pony_locked"])

    players_out = []
    total = 0.0
    for r in roster:
        r = dict(r)
        # Hide unrevealed pony picks from public view
        if r.get("is_pony") and not r.get("pony_revealed") and not show_hidden_ponies and not pony_locked:
            continue
        pos = (r.get("position") or "").upper()
        # Use snapshotted multiplier if present, else fall back to live value
        snap_key = (r["team_id"], r["player_id"])
        if snap_key in snap:
            mult = snap[snap_key]  # may be None (no multiplier was set)
        else:
            mult = r["multiplier"] if r["multiplier"] else None

        if r.get("override_points") is not None:
            base = float(r["override_points"])
            final = base
        else:
            stats = row_to_stats(r)
            base = calculate_fantasy_points({"pos": pos, "multiplier": None}, stats)
            final = calculate_fantasy_points({"pos": pos, "multiplier": mult}, stats)

        total += final
        players_out.append(
            {
                **r,
                "base_points": round(base, 2),
                "final_points": round(final, 2),
                "multiplier": mult,
            }
        )

    return {"players": players_out, "total": round(total, 2)}


# ======================================================
# SNAKE DRAFT
# ======================================================


def get_snake_order(teams: list, round_num: int) -> list:
    return list(reversed(teams)) if round_num % 2 == 0 else list(teams)

def sort_teams_by_draft_order(teams: list, league: dict) -> list:
    """Sort teams according to the commissioner-set draft order if one exists."""
    draft_order = league.get("draft_order") if league else None
    if not draft_order:
        return teams
    order_ids = [int(x) for x in draft_order.split(",") if x.strip().isdigit()]
    if not order_ids:
        return teams
    id_to_team = {t["id"]: t for t in teams}
    ordered = [id_to_team[oid] for oid in order_ids if oid in id_to_team]
    # Add any teams not in the order list at the end
    ordered += [t for t in teams if t["id"] not in {oid for oid in order_ids}]
    return ordered


def get_draft_state(league_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM draft_state WHERE league_id=?", (league_id,)
    ).fetchone()
    conn.close()
    return (
        dict(row)
        if row
        else {
            "league_id": league_id,
            "current_round": 1,
            "current_pick": 0,
            "is_complete": 0,
        }
    )


def get_team_on_clock(league_id: int):
    conn = get_db()
    state = conn.execute(
        "SELECT current_round, current_pick FROM draft_state WHERE league_id=?",
        (league_id,),
    ).fetchone()
    teams = conn.execute(
        "SELECT * FROM teams WHERE league_id=? ORDER BY id", (league_id,)
    ).fetchall()
    conn.close()
    if not teams or not state:
        return None
    ordered = get_snake_order(teams, state["current_round"])
    return dict(ordered[state["current_pick"] % len(ordered)])


# ======================================================
# USER HELPERS
# ======================================================


def create_user(username: str, password: str, is_superadmin: bool = False) -> bool:
    conn = get_db()
    try:
        conn.execute(
            adapt_sql(
                "INSERT INTO users (username, password_hash, is_superadmin) VALUES (?,?,?)"
            ),
            (username, hash_password(password), int(is_superadmin)),
        )
        conn.commit()
        return True
    except Exception as e:
        err = str(e).lower()
        if "unique" in err or "duplicate" in err:
            return False  # genuine duplicate username
        import traceback

        print(f"[main] create_user FAILED for '{username}': {e}")
        traceback.print_exc()
        return False
    finally:
        conn.close()


def get_user(username: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(uid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def ensure_superadmin():
    """
    Creates the default superadmin account on first run if it doesn't exist.
    Password comes from ADMIN_PASSWORD env var — falls back to a dev-only
    default that prints a loud warning. Change it immediately in production.
    """
    if not get_user("admin"):
        pw = os.environ.get("ADMIN_PASSWORD", "")
        if not pw:
            pw = "changeme_set_ADMIN_PASSWORD_env"
            print("=" * 60)
            print("WARNING: ADMIN_PASSWORD env var not set.")
            print(f"  Default superadmin password: {pw}")
            print("  Set ADMIN_PASSWORD in your .env file immediately.")
            print("=" * 60)
        create_user("admin", pw, is_superadmin=True)


ensure_superadmin()

# ======================================================
# LEAGUE HELPERS
# ======================================================


def generate_invite_code() -> str:
    import secrets
    import string

    return "".join(
        secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8)
    )


def create_league(name: str, commissioner_id: int) -> int:
    invite = generate_invite_code()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    lid = execute_returning(
        conn,
        "INSERT INTO leagues (name, commissioner_id, invite_code, created_at) VALUES (?,?,?,?)",
        (name, commissioner_id, invite, ts),
    )
    conn.execute(adapt_sql("INSERT INTO draft_state (league_id) VALUES (?)"), (lid,))
    # INSERT OR IGNORE → ON CONFLICT DO NOTHING (works on both SQLite and Postgres)
    conn.execute(
        adapt_sql(
            "INSERT INTO league_members (league_id, user_id, joined_at) "
            "VALUES (?,?,?) ON CONFLICT DO NOTHING"
        ),
        (lid, commissioner_id, ts),
    )
    conn.commit()
    conn.close()
    return lid


def get_league(league_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM leagues WHERE id=?", (league_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_leagues(user_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT l.*, lm.joined_at, (l.commissioner_id = ?) as is_commissioner
        FROM leagues l
        JOIN league_members lm ON lm.league_id = l.id
        WHERE lm.user_id = ?
        ORDER BY l.created_at DESC
    """,
        (user_id, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def join_league_by_code(user_id: int, invite_code: str):
    conn = get_db()
    league = conn.execute(
        "SELECT * FROM leagues WHERE invite_code=?", (invite_code.strip().upper(),)
    ).fetchone()
    if not league:
        conn.close()
        return None, "Invalid invite code."
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO league_members (league_id, user_id, joined_at) VALUES (?,?,?)",
            (league["id"], user_id, ts),
        )
        conn.commit()
    except Exception:  # duplicate / constraint violation
        pass  # already a member
    conn.close()
    return league["id"], None


def is_league_member(league_id: int, user_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM league_members WHERE league_id=? AND user_id=?",
        (league_id, user_id),
    ).fetchone()
    conn.close()
    return row is not None


def is_commissioner(league: dict, user: dict) -> bool:
    return league["commissioner_id"] == user["id"]

def is_admin_or_commissioner(league: dict, user: dict) -> bool:
    """Superadmin OR commissioner — use this for admin-only actions like deleting."""
    return bool(user["is_superadmin"]) or league["commissioner_id"] == user["id"]


# ======================================================
# TEAM / PLAYER / ROSTER HELPERS
# ======================================================


def get_league_teams(league_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM teams WHERE league_id=? ORDER BY id", (league_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_league_players(league_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM players WHERE league_id=? ORDER BY position, name", (league_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_team_by_name_in_league(league_id: int, name: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM teams WHERE league_id=? AND name=?", (league_id, name)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_team_in_league(league_id: int, user_id: int) -> int | None:
    """Return the team id owned by user in this league, or None."""
    conn = get_db()
    row = conn.execute(
        adapt_sql("SELECT id FROM teams WHERE league_id=? AND owner_id=?"),
        (league_id, user_id),
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def get_player_by_name_in_league(league_id: int, name: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM players WHERE league_id=? AND name=?", (league_id, name)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_team_roster(team_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT tr.id AS roster_id, tr.id, p.id as player_id, p.name, p.position, p.nfl_team, p.headshot_url,
               tr.multiplier, tr.is_pony, tr.pony_revealed
        FROM team_roster tr
        JOIN players p ON tr.player_id = p.id
        WHERE tr.team_id=?
        ORDER BY tr.is_pony, p.position, p.name
    """,
        (team_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_drafted_player_ids(league_id: int) -> set:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT tr.player_id FROM team_roster tr
        JOIN teams t ON tr.team_id = t.id
        WHERE t.league_id=? AND tr.is_pony=0
    """,
        (league_id,),
    ).fetchall()
    conn.close()
    return {r["player_id"] for r in rows}


def get_current_week(league_id: int) -> int:
    """
    Returns the highest playoff week (1-4) that has any score data.
    Scores are always stored as fantasy weeks 1-4 so this never
    bleeds into regular-season week numbers.
    """
    conn = get_db()
    row = conn.execute(
        """
        SELECT MAX(ps.week) as w FROM player_scores ps
        JOIN players p ON ps.player_id = p.id
        WHERE p.league_id=? AND ps.week BETWEEN 1 AND 4
    """,
        (league_id,),
    ).fetchone()
    conn.close()
    w = (row["w"] or 1) if row else 1
    return min(w, 4)  # hard cap — never exceed 4 playoff weeks


def snapshot_multipliers_for_week(league_id: int, week: int):
    """
    Called when the commissioner locks multipliers for a round.
    Copies the current team_roster.multiplier for every player in
    the league into roster_snapshots(league_id, week, ...).
    Existing rows for this week are replaced (upsert) so re-locking
    is safe and idempotent.
    """
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """
        SELECT tr.team_id, tr.player_id, tr.multiplier
        FROM   team_roster tr
        JOIN   teams t ON tr.team_id = t.id
        WHERE  t.league_id = ?
    """,
        (league_id,),
    ).fetchall()
    for r in rows:
        conn.execute(
            adapt_sql(
                """
            INSERT INTO roster_snapshots
                (league_id, week, team_id, player_id, multiplier, snapshotted_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(league_id, week, team_id, player_id)
            DO UPDATE SET multiplier=excluded.multiplier,
                          snapshotted_at=excluded.snapshotted_at
        """
            ),
            (league_id, week, r["team_id"], r["player_id"], r["multiplier"], now),
        )
    conn.commit()
    conn.close()


def get_snapshot_multipliers(league_id: int, week: int) -> dict:
    """
    Returns {(team_id, player_id): multiplier} for a snapshotted week.
    Returns empty dict if no snapshot exists yet (falls back to live values).
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT team_id, player_id, multiplier
        FROM   roster_snapshots
        WHERE  league_id=? AND week=?
    """,
        (league_id, week),
    ).fetchall()
    conn.close()
    return {(r["team_id"], r["player_id"]): r["multiplier"] for r in rows}


def week_has_snapshot(league_id: int, week: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM roster_snapshots WHERE league_id=? AND week=? LIMIT 1",
        (league_id, week),
    ).fetchone()
    conn.close()
    return row is not None


def multipliers_locked_for(league: dict) -> bool:
    try:
        lock_ts = datetime.fromisoformat(league["multiplier_lock_ts"])
        return datetime.now(timezone.utc) >= lock_ts
    except Exception:
        return False


# ======================================================
# APP
# ======================================================

app = FastAPI()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda req, exc: __import__('fastapi').responses.JSONResponse(
    status_code=429, content={"detail": "Too many requests. Please wait and try again."}
))
templates = Jinja2Templates(directory="templates")




def get_current_user(request: Request):
    """Validate the JWT cookie and return the user row, or None."""
    token = request.cookies.get("session")
    if not token:
        return None
    username = decode_access_token(token)
    return get_user(username) if username else None


def league_ctx(league_id: int, user: dict) -> dict:
    """Validate league access; raise HTTP errors if not found / not a member."""
    league = get_league(league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")
    if not is_league_member(league_id, user["id"]) and not user["is_superadmin"]:
        raise HTTPException(status_code=403, detail="Not a member of this league")
    return league


# ======================================================
# AUTH
# ======================================================


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    user = get_current_user(request)
    if not user:
        return templates.TemplateResponse("home_public.html", {"request": request})
    leagues = get_user_leagues(user["id"])
    if len(leagues) == 1:
        return RedirectResponse(f"/league/{leagues[0]['id']}/draft", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/login")
@limiter.limit("5/minute")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=1", status_code=303)
    token = create_access_token(user["username"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        "session",
        token,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("SECURE_COOKIES", "0") == "1",
        max_age=JWT_EXPIRE_HOURS * 3600,
    )
    resp.set_cookie(
        "csrf_token",
        generate_csrf_token(),
        httponly=False,
        samesite="lax",
        secure=os.environ.get("SECURE_COOKIES", "0") == "1",
        max_age=JWT_EXPIRE_HOURS * 3600,
    )
    return resp


# ======================================================
# EMAIL HELPER  (Resend API)
# ======================================================

def send_email(to: str, subject: str, html: str) -> bool:
    """Send an email via Resend API. Returns True on success."""
    import urllib.error
    api_key  = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("RESEND_FROM", "support@504fantasy.com")
    if not api_key:
        print("[email] RESEND_API_KEY not set — email not sent")
        return False
    payload = json.dumps({
        "from": from_addr, "to": [to],
        "subject": subject, "html": html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "python-urllib/3.12"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except urllib.error.HTTPError as e:
        print(f"[email] Resend error {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        print(f"[email] send_email failed: {e}")
        return False


def create_reset_token(user_id: int) -> str:
    import secrets
    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    conn = get_db()
    conn.execute(adapt_sql("DELETE FROM password_reset_tokens WHERE user_id=?"), (user_id,))
    conn.execute(adapt_sql(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)"
    ), (user_id, token, expires_at))
    conn.commit()
    conn.close()
    return token


def get_reset_token_user(token: str):
    conn = get_db()
    row  = conn.execute(adapt_sql(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0"
    ), (token,)).fetchone()
    conn.close()
    if not row:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expires:
        return None
    return get_user_by_id(row["user_id"])


def mark_token_used(token: str):
    conn = get_db()
    conn.execute(adapt_sql("UPDATE password_reset_tokens SET used=1 WHERE token=?"), (token,))
    conn.commit()
    conn.close()


def get_user_by_email(email: str):
    conn = get_db()
    row  = conn.execute(adapt_sql(
        "SELECT * FROM users WHERE LOWER(email)=LOWER(?)"
    ), (email.strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


# ======================================================
# AUTH ROUTES (register with email, forgot/reset password)
# ======================================================

@app.post("/register")
@limiter.limit("3/minute")
def register(request: Request, 
    username: str = Form(...),
    password: str = Form(...),
    email:    str = Form(...),
):
    if len(password) < 8:
        return RedirectResponse("/register?error=short", status_code=303)
    if not any(ch.isupper() for ch in password):
        return RedirectResponse("/register?error=no_upper", status_code=303)
    if not any(ch.isdigit() for ch in password):
        return RedirectResponse("/register?error=no_number", status_code=303)
    username = username.strip()
    email    = email.strip().lower()
    if not username:
        return RedirectResponse("/register?error=empty", status_code=303)
    if not email or "@" not in email:
        return RedirectResponse("/register?error=bad_email", status_code=303)
    if get_user_by_email(email):
        return RedirectResponse("/register?error=email_taken", status_code=303)
    conn = get_db()
    try:
        conn.execute(adapt_sql(
            "INSERT INTO users (username, password_hash, email) VALUES (?,?,?)"
        ), (username, hash_password(password), email))
        conn.commit()
    except Exception as e:
        conn.close()
        err = str(e).lower()
        if "unique" in err or "duplicate" in err:
            return RedirectResponse("/register?error=taken", status_code=303)
        return RedirectResponse("/register?error=db_error", status_code=303)
    conn.close()
    send_email(
        to=email,
        subject="Welcome to 504 Fantasy! 🏈",
        html=f"""<div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;background:#080a0e;color:#eaecf2;padding:2rem;border-radius:12px;">
          <h1 style="color:#e8b84b;">Welcome to 504 Fantasy! 🏈</h1>
          <p style="color:#8898b0;">Hey <strong style="color:#eaecf2">{username}</strong>, your account is all set up. Jump in and join or create a league!</p>
          <a href="https://504fantasy.com/login" style="display:inline-block;margin-top:1.5rem;padding:.75rem 1.75rem;background:#e8b84b;color:#080a0e;border-radius:8px;text-decoration:none;font-weight:800;">Go to 504 Fantasy</a>
          <p style="margin-top:2rem;font-size:.8rem;color:#5e6e88;">Questions? <a href="mailto:support@504fantasy.com" style="color:#e8b84b;">support@504fantasy.com</a></p>
        </div>""",
    )
    return RedirectResponse("/login?registered=1", status_code=303)


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "msg":   request.query_params.get("msg", ""),
        "error": request.query_params.get("error", ""),
    })


@app.post("/forgot-password")
def forgot_password_submit(email: str = Form(...)):
    email = email.strip().lower()
    user  = get_user_by_email(email)
    if user:
        token     = create_reset_token(user["id"])
        base_url  = os.environ.get("BASE_URL", "https://504fantasy.com")
        reset_url = f"{base_url}/reset-password?token={token}"
        send_email(
            to=email,
            subject="Reset your 504 Fantasy password",
            html=f"""<div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;background:#080a0e;color:#eaecf2;padding:2rem;border-radius:12px;">
              <h1 style="color:#e8b84b;">Password Reset 🏈</h1>
              <p style="color:#8898b0;">Hey <strong style="color:#eaecf2">{user['username']}</strong>, click below to reset your password. Link expires in <strong style="color:#eaecf2">1 hour</strong>.</p>
              <a href="{reset_url}" style="display:inline-block;margin-top:1.5rem;padding:.75rem 1.75rem;background:#e8b84b;color:#080a0e;border-radius:8px;text-decoration:none;font-weight:800;">Reset My Password</a>
              <p style="margin-top:1.5rem;font-size:.8rem;color:#5e6e88;">If you didn't request this, ignore this email.</p>
              <p style="font-size:.8rem;color:#5e6e88;">Or copy: <a href="{reset_url}" style="color:#e8b84b;word-break:break-all;">{reset_url}</a></p>
            </div>""",
        )
    return RedirectResponse("/forgot-password?msg=sent", status_code=303)


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    user = get_reset_token_user(token)
    return templates.TemplateResponse("reset_password.html", {
        "request": request,
        "token":   token,
        "invalid": not user,
        "error":   request.query_params.get("error", ""),
    })


@app.post("/reset-password")
def reset_password_submit(
    token:    str = Form(...),
    password: str = Form(...),
    confirm:  str = Form(...),
):
    if len(password) < 6:
        return RedirectResponse(f"/reset-password?token={token}&error=short", status_code=303)
    if password != confirm:
        return RedirectResponse(f"/reset-password?token={token}&error=mismatch", status_code=303)
    user = get_reset_token_user(token)
    if not user:
        return RedirectResponse(f"/reset-password?token={token}&error=invalid", status_code=303)
    conn = get_db()
    conn.execute(adapt_sql("UPDATE users SET password_hash=? WHERE id=?"),
                 (hash_password(password), user["id"]))
    conn.commit()
    conn.close()
    mark_token_used(token)
    write_audit(actor=user["username"], action="PASSWORD_RESET",
                details="Password reset via email token")
    return RedirectResponse("/login?reset=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    # JWT is stateless — logout is just clearing the cookie.
    # For immediate revocation at scale add a Redis token blocklist.
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ======================================================
# DASHBOARD  (league list)
# ======================================================



@app.get("/research", response_class=HTMLResponse)
def research_page(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("research.html", {
        "request": request,
        "user": user,
        "nfl_season": int(os.environ.get("NFL_SEASON", "2026")),
    })

@app.get("/dashboard")
def dashboard(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    leagues = get_user_leagues(user["id"])

    # Load survivor leagues for same user
    survivor_leagues = []
    try:
        from survivor_db import get_connection as _surv_conn
        sconn = _surv_conn()
        rows = sconn.execute("""
            SELECT l.*, slm.joined_at,
                   (l.commissioner_id = ?) as is_commissioner
            FROM survivor_leagues l
            JOIN survivor_league_members slm ON slm.league_id = l.id
            WHERE slm.user_id = ?
            ORDER BY l.created_at DESC
        """, (user["id"], user["id"])).fetchall()
        sconn.close()
        survivor_leagues = [dict(r) for r in rows]
    except Exception as e:
        print(f"[dashboard] survivor leagues error: {e}")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "leagues": leagues,
            "survivor_leagues": survivor_leagues,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/league/create")
def league_create(league_name: str = Form(...), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not league_name.strip():
        return RedirectResponse("/dashboard?error=empty_name", status_code=303)
    lid = create_league(league_name.strip(), user["id"])
    write_audit(
        actor=user["username"],
        action="LEAGUE_CREATE",
        league_id=lid,
        details=f"name={league_name.strip()}",
    )
    return RedirectResponse(f"/league/{lid}/draft?msg=league_created", status_code=303)


@app.post("/league/join")
def league_join(invite_code: str = Form(...), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    lid, err = join_league_by_code(user["id"], invite_code)
    if err:
        return RedirectResponse(f"/dashboard?error={err}", status_code=303)
    # Auto-create a team for the user if they don't have one yet
    conn = get_db()
    existing = conn.execute(
        adapt_sql("SELECT id FROM teams WHERE league_id=? AND owner_id=?"),
        (lid, user["id"])
    ).fetchone()
    if not existing:
        conn.execute(
            adapt_sql("INSERT INTO teams (league_id, name, owner_id) VALUES (?,?,?)"),
            (lid, user["username"], user["id"])
        )
        conn.commit()
    conn.close()
    write_audit(actor=user["username"], action="LEAGUE_JOIN", league_id=lid)
    return RedirectResponse(f"/league/{lid}/draft", status_code=303)


# ======================================================
# LEAGUE: TEAM CREATION
# ======================================================


@app.post("/league/{league_id}/create-team")
def create_team(
    league_id: int, team_name: str = Form(...), user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league_ctx(league_id, user)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO teams (league_id, name, owner_id) VALUES (?,?,?)",
            (league_id, team_name.strip(), user["id"]),
        )
        conn.commit()
    except Exception:  # duplicate / constraint violation
        pass
    finally:
        conn.close()
    return RedirectResponse(f"/league/{league_id}/draft", status_code=303)


# ======================================================
# LEAGUE: DRAFT
# ======================================================

# ======================================================
# LEAGUE HOME PAGE
# ======================================================


@app.get("/league/{league_id}", response_class=HTMLResponse)
def league_home(league_id: int, request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    teams = get_league_teams(league_id)
    current_week = get_current_week(league_id)
    draft_state = get_draft_state(league_id)
    my_team_id = get_user_team_in_league(league_id, user["id"])

    # Standings
    standings = []
    for team in teams:
        pts = sum(
            get_team_week_score(team["id"], w, league_id)["total"]
            for w in range(1, current_week + 1)
        )
        standings.append(
            {
                "team": team,
                "owner": get_user_by_id(team["owner_id"]),
                "total_points": round(pts, 2),
            }
        )
    standings.sort(key=lambda x: x["total_points"], reverse=True)

    # Recent picks (last 10)
    recent_picks = get_chat(league_id, limit=20)
    recent_picks = [m for m in recent_picks if m["msg_type"] == "pick"][-10:]

    import os as _os

    nfl_season = int(_os.environ.get("NFL_SEASON", "2024"))

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "standings": standings,
            "draft_state": draft_state,
            "current_week": current_week,
            "my_team_id": my_team_id,
            "recent_picks": recent_picks,
            "team_count": len(teams),
            "is_commissioner": is_commissioner(league, user),
            "season": nfl_season,
        },
    )


@app.get("/league/{league_id}/draft")
def draft_page(league_id: int, request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    teams = sort_teams_by_draft_order(get_league_teams(league_id), league)
    rosters = {t["name"]: get_team_roster(t["id"]) for t in teams}
    draft_state = get_draft_state(league_id)
    team_on_clock = get_team_on_clock(league_id)
    drafted_ids = get_drafted_player_ids(league_id)
    all_players = get_league_players(league_id)
    available = [p for p in all_players if p["id"] not in drafted_ids]

    by_position: dict = {}
    for p in available:
        pos = p["position"] or "UNK"
        by_position.setdefault(pos, []).append(p)

    snake_order = (
        get_snake_order(teams, draft_state.get("current_round", 1)) if teams else []
    )

    return templates.TemplateResponse(
        "draft.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "teams": teams,
            "rosters": rosters,
            "pony_locked": bool(league["pony_locked"]),
            "draft_state": draft_state,
            "team_on_clock": team_on_clock,
            "available_players": available,
            "by_position": by_position,
            "snake_order": [dict(t) for t in snake_order],
            "multipliers_locked": multipliers_locked_for(league),
            "is_commissioner": is_commissioner(league, user),
            "msg": request.query_params.get("msg", ""),
            "now_utc": datetime.now(timezone.utc).isoformat(),
            "my_team_id": get_user_team_in_league(league_id, user["id"]),
        },
    )




# ──────────────────────────────────────────────────────────────────────────────
# DRAFT QUEUE
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/league/{league_id}/draft/queue/add")
def queue_add(league_id: int, player_id: int = Form(...), user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401)
    conn = get_db()
    my_team = conn.execute(
        "SELECT id FROM teams WHERE league_id=? AND owner_id=?", (league_id, user["id"])
    ).fetchone()
    if not my_team:
        conn.close()
        raise HTTPException(status_code=403)
    # Get max sort order
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM draft_queue WHERE team_id=?",
        (my_team["id"],)
    ).fetchone()[0]
    ts = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO draft_queue (team_id, player_id, sort_order, added_at) VALUES (?,?,?,?)",
            (my_team["id"], player_id, max_order + 1, ts)
        )
        conn.commit()
    except Exception:
        pass  # already in queue
    conn.close()
    return {"status": "ok"}


@app.post("/league/{league_id}/draft/queue/remove")
def queue_remove(league_id: int, player_id: int = Form(...), user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401)
    conn = get_db()
    my_team = conn.execute(
        "SELECT id FROM teams WHERE league_id=? AND owner_id=?", (league_id, user["id"])
    ).fetchone()
    if not my_team:
        conn.close()
        raise HTTPException(status_code=403)
    conn.execute(
        "DELETE FROM draft_queue WHERE team_id=? AND player_id=?",
        (my_team["id"], player_id)
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/league/{league_id}/draft/queue/reorder")
def queue_reorder(league_id: int, player_ids: str = Form(...), user=Depends(get_current_user)):
    """Reorder queue — player_ids is comma-separated list in new order."""
    if not user:
        raise HTTPException(status_code=401)
    conn = get_db()
    my_team = conn.execute(
        "SELECT id FROM teams WHERE league_id=? AND owner_id=?", (league_id, user["id"])
    ).fetchone()
    if not my_team:
        conn.close()
        raise HTTPException(status_code=403)
    ids = [int(x) for x in player_ids.split(",") if x.strip().isdigit()]
    for i, pid in enumerate(ids):
        conn.execute(
            "UPDATE draft_queue SET sort_order=? WHERE team_id=? AND player_id=?",
            (i + 1, my_team["id"], pid)
        )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/league/{league_id}/draft/queue")
def queue_get(league_id: int, user=Depends(get_current_user)):
    """Get current user's queue for this league."""
    if not user:
        raise HTTPException(status_code=401)
    conn = get_db()
    my_team = conn.execute(
        "SELECT id FROM teams WHERE league_id=? AND owner_id=?", (league_id, user["id"])
    ).fetchone()
    if not my_team:
        conn.close()
        return {"queue": []}
    rows = conn.execute(
        """SELECT dq.player_id, dq.sort_order, p.name, p.position, p.nfl_team
           FROM draft_queue dq
           JOIN players p ON p.id = dq.player_id
           WHERE dq.team_id=?
           ORDER BY dq.sort_order ASC""",
        (my_team["id"],)
    ).fetchall()
    conn.close()
    return {"queue": [dict(r) for r in rows]}



@app.get("/api/nfl-schedule/{week}")
def api_nfl_schedule(week: int, season: int = None, user=Depends(get_current_user)):
    """Return all games for a given week from survivor schedule data."""
    from datetime import datetime, timezone
    import sqlite3 as _sq
    if not season:
        season = int(os.environ.get("NFL_SEASON", "2026"))
    try:
        sconn = _sq.connect(os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db"))
        sconn.row_factory = _sq.Row
        # Pick the league with the latest week 1 kickoff = regular season (Sept vs Aug)
        best_league = sconn.execute("""
            SELECT league_id FROM survivor_game_schedule
            WHERE week=1 AND season=?
            GROUP BY league_id
            ORDER BY MAX(kickoff_utc) DESC
            LIMIT 1
        """, (season,)).fetchone()
        best_lid = best_league[0] if best_league else 1
        rows = sconn.execute("""
            SELECT kickoff_utc, team
            FROM survivor_game_schedule
            WHERE week=? AND season=? AND league_id=?
            ORDER BY kickoff_utc ASC, team ASC
        """, (week, season, best_lid)).fetchall()
        sconn.close()
        # Group teams by kickoff time - show timeslots not matchups
        # since we don't store away/home distinction
        from collections import defaultdict
        timeslots = defaultdict(list)
        for r in rows:
            timeslots[r["kickoff_utc"]].append(r["team"])

        games = []
        for kickoff, teams in sorted(timeslots.items()):
            games.append({
                "kickoff_utc": kickoff,
                "teams": teams,
            })
        return {"week": week, "season": season, "games": games}
    except Exception as e:
        return {"week": week, "season": season, "games": [], "error": str(e)}

@app.get("/api/team-schedule/{nfl_team}")
def api_team_schedule(nfl_team: str, user=Depends(get_current_user)):
    """Return upcoming schedule entries for an NFL team from any active league."""
    from datetime import datetime, timezone
    conn = get_db()
    # Get schedule from survivor DB
    try:
        import sqlite3 as _sq
        sconn = _sq.connect(os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db"))
        sconn.row_factory = _sq.Row
        now_utc = datetime.now(timezone.utc).isoformat()
        rows = sconn.execute("""
            SELECT week, team, kickoff_utc, season
            FROM survivor_game_schedule
            WHERE team=? AND kickoff_utc >= ?
            ORDER BY kickoff_utc ASC
            LIMIT 5
        """, (nfl_team.upper(), now_utc)).fetchall()
        sconn.close()
        return {"games": [dict(r) for r in rows]}
    except Exception as e:
        return {"games": [], "error": str(e)}
    finally:
        conn.close()

@app.post("/league/{league_id}/draft/autopick")
def draft_autopick(league_id: int, user=Depends(get_current_user)):
    """Auto-pick the next available player when the timer expires."""
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    # Only commissioner or the team on clock can trigger autopick
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT current_round, current_pick, is_complete, pick_started_at FROM draft_state WHERE league_id=?",
            (league_id,),
        ).fetchone()
        if not state or state["is_complete"]:
            conn.execute("ROLLBACK")
            return {"status": "complete"}
        # Check timer actually expired
        import time as _time
        if state["pick_started_at"] and league.get("pick_timer_seconds"):
            try:
                elapsed = _time.time() - float(state["pick_started_at"])
                if elapsed < float(league["pick_timer_seconds"]):
                    conn.execute("ROLLBACK")
                    return {"status": "timer_not_expired"}
            except Exception:
                pass
        teams = conn.execute(
            "SELECT * FROM teams WHERE league_id=? ORDER BY id", (league_id,)
        ).fetchall()
        ordered = get_snake_order(teams, state["current_round"])
        team_on_clock = ordered[state["current_pick"] % len(ordered)]
        # Get already-drafted player IDs
        drafted_ids = {
            r[0] for r in conn.execute(
                "SELECT player_id FROM team_roster WHERE team_id IN "
                "(SELECT id FROM teams WHERE league_id=?)", (league_id,)
            ).fetchall()
        }
        # Pick first available player alphabetically
        available = conn.execute(
            "SELECT * FROM players WHERE league_id=? ORDER BY name ASC",
            (league_id,)
        ).fetchall()
        auto_player = None
        for p in available:
            if p["id"] not in drafted_ids:
                auto_player = p
                break
        if not auto_player:
            conn.execute("ROLLBACK")
            return {"status": "no_players"}
        import time as _time2
        now = int(_time2.time())
        # Insert roster pick
        conn.execute(
            "INSERT INTO team_roster (team_id, player_id, is_pony) VALUES (?,?,0)",
            (team_on_clock["id"], auto_player["id"]),
        )
        new_pick = state["current_pick"] + 1
        new_round = state["current_round"]
        is_complete = 0
        if new_pick >= len(teams):
            new_pick = 0
            new_round += 1
        conn.execute(
            "UPDATE draft_state SET current_round=?, current_pick=?, pick_started_at=? WHERE league_id=?",
            (new_round, new_pick, now, league_id),
        )
        conn.execute("COMMIT")
        write_audit(
            actor="system",
            action="DRAFT_PICK",
            league_id=league_id,
            team=team_on_clock["name"],
            player=auto_player["name"],
            details=f"AUTO-PICK (timer expired) Rd {state['current_round']} Pk {state['current_pick']+1}",
        )
        return {"status": "ok", "player": auto_player["name"], "team": team_on_clock["name"]}
    except Exception as e:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/league/{league_id}/draft/pick")
def draft_pick(
    league_id: int, player_name: str = Form(...), user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    is_comm = is_commissioner(league, user)
    # Block picks until draft start time (superadmin can always override for testing)
    if not user.get("is_superadmin"):
        draft_start = league.get("draft_start_time")
        if draft_start:
            from datetime import datetime, timezone as _tz
            try:
                start_dt = datetime.fromisoformat(draft_start).replace(tzinfo=_tz.utc)
                if datetime.now(_tz.utc) < start_dt:
                    return RedirectResponse(
                        f"/league/{league_id}/draft?error=draft_not_started",
                        status_code=303
                    )
            except Exception:
                pass
    # Block unpaid teams from drafting after payment deadline
    if not is_comm and league.get("entry_fee") and league["entry_fee"] > 0:
        my_team = conn2 = None
        try:
            conn2 = get_db()
            my_team = conn2.execute(
                "SELECT paid FROM teams WHERE league_id=? AND owner_id=?",
                (league_id, user["id"])
            ).fetchone()
        finally:
            if conn2: conn2.close()
        if my_team and not my_team["paid"]:
            deadline = league.get("payment_deadline")
            if deadline:
                from datetime import date as _date
                try:
                    if _date.today() > _date.fromisoformat(deadline):
                        return RedirectResponse(
                            f"/league/{league_id}/team?error=payment_required",
                            status_code=303
                        )
                except Exception:
                    pass
    base = f"/league/{league_id}"

    conn = get_db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Re-read state inside the transaction for consistency
        state = conn.execute(
            "SELECT current_round, current_pick, is_complete FROM draft_state WHERE league_id=?",
            (league_id,),
        ).fetchone()
        teams = conn.execute(
            "SELECT * FROM teams WHERE league_id=? ORDER BY id", (league_id,)
        ).fetchall()
        teams = sort_teams_by_draft_order([dict(t) for t in teams], league)

        if not teams:
            conn.execute("ROLLBACK")
            return RedirectResponse(f"{base}/draft", status_code=303)

        # Fix 4: Block picks once draft is marked complete
        if state["is_complete"]:
            conn.execute("ROLLBACK")
            return RedirectResponse(
                f"{base}/draft?error=draft_complete", status_code=303
            )

        ordered = get_snake_order(teams, state["current_round"])
        team_on_clock = ordered[state["current_pick"] % len(ordered)]

        if user["id"] != team_on_clock["owner_id"]:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=403, detail="Not your pick")

        # Fix 3: Enforce picks_per_team cap — count existing non-pony roster entries
        roster_count = conn.execute(
            "SELECT COUNT(*) FROM team_roster WHERE team_id=? AND is_pony=0",
            (team_on_clock["id"],),
        ).fetchone()[0]
        picks_per_team = league["picks_per_team"] or 15
        if roster_count >= picks_per_team:
            conn.execute("ROLLBACK")
            return RedirectResponse(f"{base}/draft?error=roster_full", status_code=303)

        player = conn.execute(
            "SELECT * FROM players WHERE league_id=? AND name=?",
            (league_id, player_name),
        ).fetchone()
        if not player:
            conn.execute("ROLLBACK")
            return RedirectResponse(f"{base}/draft", status_code=303)

        # Block if already drafted as a regular pick in this league
        already = conn.execute(
            """
            SELECT tr.id FROM team_roster tr JOIN teams t ON tr.team_id=t.id
            WHERE t.league_id=? AND tr.player_id=? AND tr.is_pony=0
        """,
            (league_id, player["id"]),
        ).fetchone()
        if already:
            conn.execute("ROLLBACK")
            return RedirectResponse(f"{base}/draft?error=taken", status_code=303)

        conn.execute(
            "INSERT INTO team_roster (team_id, player_id, is_pony) VALUES (?,?,0)",
            (team_on_clock["id"], player["id"]),
        )

        # Advance the snake cursor
        new_pick = state["current_pick"] + 1
        new_round = state["current_round"]
        if new_pick >= len(teams):
            new_pick = 0
            new_round += 1

        # Fix 3+4: Auto-complete when every team has reached picks_per_team
        # total_picks so far = (completed rounds × teams) + picks in current round
        picks_made = (
            (state["current_round"] - 1) * len(teams) + state["current_pick"] + 1
        )
        total_picks_needed = picks_per_team * len(teams)
        draft_now_complete = picks_made >= total_picks_needed

        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE draft_state SET current_round=?, current_pick=?, is_complete=?, "
            "pick_started_at=? WHERE league_id=?",
            (new_round, new_pick, int(draft_now_complete), now_iso, league_id),
        )
        conn.execute("COMMIT")

        # Cancel the previous pick's timer and arm a fresh one
        cancel_pick_timer(league_id)
        if not draft_now_complete and league.get("pick_timer_seconds"):
            arm_pick_timer(league_id, league["pick_timer_seconds"], new_round, new_pick)

        write_audit(
            actor=user["username"],
            action="DRAFT_PICK",
            league_id=league_id,
            team=team_on_clock["name"],
            player=player_name,
            details=f"Round {state['current_round']}, Pick {state['current_pick']+1}",
        )
        pick_msg = (
            f"{team_on_clock['name']} selected {player_name} "
            f"(Round {state['current_round']}, Pick {state['current_pick']+1})"
        )
        write_chat(league_id, "system", pick_msg, "pick")

        if draft_now_complete:
            write_audit(
                actor="system",
                action="DRAFT_COMPLETE",
                league_id=league_id,
                details=f"All {len(teams)} teams filled {picks_per_team} picks",
            )

    except HTTPException:
        try:
            conn.execute("ROLLBACK")
        except:
            pass
        conn.close()
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except:
            pass
        conn.close()
        return RedirectResponse(f"{base}/draft", status_code=303)

    conn.close()
    return RedirectResponse(f"{base}/draft", status_code=303)


# ======================================================
# LEAGUE: MULTIPLIERS
# ======================================================


def _apply_multiplier_by_roster_id(
    conn,
    team_id: int,
    roster_id: int,
    is_pony: int,
    value: str,
    league_id: int,
    user_name: str,
    team_name: str,
    player_name: str,
):
    """
    Inner helper used by both set_multiplier and set_pony_multiplier.
    Uses roster_id (stable PK) rather than player name (mutable string).
    Multiplier limits per team:
      regular players — 1.5×: 2 slots,  2×: 1 slot
      pony players    — 1.5×: 1 slot,   2×: 1 slot
    """
    if is_pony:
        limits = {"1.5": 1, "2": 1}
        action = "PONY_MULTIPLIER_SET"
    else:
        limits = {"1.5": 2, "2": 1}
        action = "MULTIPLIER_SET"

    if value != "remove":
        # Exclude the target row from the count so updating in-place is allowed
        count = conn.execute(
            "SELECT COUNT(*) FROM team_roster WHERE team_id=? AND multiplier=? AND is_pony=? AND id!=?",
            (team_id, float(value), is_pony, roster_id),
        ).fetchone()[0]
        if count >= limits.get(value, 0):
            return False  # limit hit
        conn.execute(
            adapt_sql("UPDATE team_roster SET multiplier=? WHERE id=?"),
            (float(value), roster_id),
        )
    else:
        conn.execute(
            adapt_sql("UPDATE team_roster SET multiplier=NULL WHERE id=?"), (roster_id,)
        )

    conn.commit()
    write_audit(
        actor=user_name,
        action=action,
        league_id=league_id,
        team=team_name,
        player=player_name,
        details=f"value={value}",
    )
    return True


@app.post("/league/{league_id}/multiplier")
def set_multiplier(
    league_id: int,
    request: Request,
    roster_id: str = Form(None),
    team: str = Form(None),
    player_name: str = Form(None),
    value: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    base = f"/league/{league_id}"
    if multipliers_locked_for(league):
        return RedirectResponse(f"{base}/draft", status_code=303)
    # Coerce roster_id: empty string or None → None
    try:
        roster_id = int(roster_id) if roster_id and str(roster_id).strip() else None
    except (ValueError, TypeError):
        roster_id = None
    # Validate multiplier value
    # Empty string from the "—" option means clear the multiplier
    if not value or value.strip() == "":
        value = "remove"
    elif value != "remove":
        try:
            value_f = float(value)
        except (ValueError, TypeError):
            referer = request.headers.get("referer", "")
            dest = (
                referer
                if referer and f"/league/{league_id}" in referer
                else f"{base}/draft"
            )
            return RedirectResponse(f"{dest}?error=bad_mult", status_code=303)
        if value_f not in (1.5, 2.0):
            referer = request.headers.get("referer", "")
            dest = (
                referer
                if referer and f"/league/{league_id}" in referer
                else f"{base}/draft"
            )
            return RedirectResponse(f"{dest}?error=bad_mult", status_code=303)

    conn = get_db()
    if roster_id:
        # Fast path: look up by stable PK, verify ownership in the same query
        row = conn.execute(
            """
            SELECT tr.id, tr.team_id, t.owner_id, t.name AS team_name,
                   p.name AS player_name
            FROM team_roster tr
            JOIN teams t  ON tr.team_id   = t.id
            JOIN players p ON tr.player_id = p.id
            WHERE tr.id=? AND t.league_id=? AND tr.is_pony=0
        """,
            (roster_id, league_id),
        ).fetchone()
    elif team and player_name:
        # Legacy path: look up by name pair
        row = conn.execute(
            """
            SELECT tr.id, tr.team_id, t.owner_id, t.name AS team_name,
                   p.name AS player_name
            FROM team_roster tr
            JOIN teams   t ON tr.team_id   = t.id
            JOIN players p ON tr.player_id = p.id
            WHERE t.league_id=? AND t.name=? AND p.name=? AND tr.is_pony=0
        """,
            (league_id, team, player_name),
        ).fetchone()
    else:
        conn.close()
        return RedirectResponse(f"{base}/draft", status_code=303)

    if not row or (row["owner_id"] != user["id"] and not is_admin_or_commissioner(league, user)):
        conn.close()
        return RedirectResponse(f"{base}/draft", status_code=303)

    ok = _apply_multiplier_by_roster_id(
        conn,
        row["team_id"],
        row["id"],
        0,
        value,
        league_id,
        user["username"],
        row["team_name"],
        row["player_name"],
    )
    conn.close()
    # Return to team page if that's where the form came from
    referer = request.headers.get("referer", "")
    if f"/team/{row['team_id']}" in referer:
        return RedirectResponse(
            f"{base}/team/{row['team_id']}?msg=mult_saved", status_code=303
        )
    return RedirectResponse(f"{base}/draft", status_code=303)


@app.post("/league/{league_id}/pony/multiplier")
def set_pony_multiplier(
    league_id: int,
    request: Request,
    roster_id: str = Form(None),
    team: str = Form(None),
    player_name: str = Form(None),
    value: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    base = f"/league/{league_id}"
    if multipliers_locked_for(league):
        return RedirectResponse(f"{base}/draft", status_code=303)
    try:
        roster_id = int(roster_id) if roster_id and str(roster_id).strip() else None
    except (ValueError, TypeError):
        roster_id = None
    if not value or value.strip() == "":
        value = "remove"
    elif value != "remove":
        try:
            value_f = float(value)
        except (ValueError, TypeError):
            referer = request.headers.get("referer", "")
            dest = (
                referer
                if referer and f"/league/{league_id}" in referer
                else f"{base}/draft"
            )
            return RedirectResponse(f"{dest}?error=bad_mult", status_code=303)
        if value_f not in (1.5, 2.0):
            referer = request.headers.get("referer", "")
            dest = (
                referer
                if referer and f"/league/{league_id}" in referer
                else f"{base}/draft"
            )
            return RedirectResponse(f"{dest}?error=bad_mult", status_code=303)

    conn = get_db()
    if roster_id:
        row = conn.execute(
            """
            SELECT tr.id, tr.team_id, t.owner_id, t.name AS team_name,
                   p.name AS player_name
            FROM team_roster tr
            JOIN teams   t ON tr.team_id   = t.id
            JOIN players p ON tr.player_id = p.id
            WHERE tr.id=? AND t.league_id=? AND tr.is_pony=1
        """,
            (roster_id, league_id),
        ).fetchone()
    elif team and player_name:
        row = conn.execute(
            """
            SELECT tr.id, tr.team_id, t.owner_id, t.name AS team_name,
                   p.name AS player_name
            FROM team_roster tr
            JOIN teams   t ON tr.team_id   = t.id
            JOIN players p ON tr.player_id = p.id
            WHERE t.league_id=? AND t.name=? AND p.name=? AND tr.is_pony=1
        """,
            (league_id, team, player_name),
        ).fetchone()
    else:
        conn.close()
        return RedirectResponse(f"{base}/draft", status_code=303)

    if not row or (row["owner_id"] != user["id"] and not is_admin_or_commissioner(league, user)):
        conn.close()
        return RedirectResponse(f"{base}/draft", status_code=303)

    ok = _apply_multiplier_by_roster_id(
        conn,
        row["team_id"],
        row["id"],
        1,
        value,
        league_id,
        user["username"],
        row["team_name"],
        row["player_name"],
    )
    conn.close()
    referer = request.headers.get("referer", "")
    if f"/team/{row['team_id']}" in referer:
        return RedirectResponse(
            f"{base}/team/{row['team_id']}?msg=mult_saved", status_code=303
        )
    return RedirectResponse(f"{base}/draft", status_code=303)


@app.post("/league/{league_id}/team/{team_id}/pony/add")
def team_add_pony(
    league_id: int,
    team_id: int,
    player_name: str = Form(...),
    user=Depends(get_current_user),
):
    """Add a pony pick privately from My Team page (draft must be complete)."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    base = f"/league/{league_id}/team/{team_id}"

    if not get_draft_state(league_id).get("is_complete"):
        return RedirectResponse(f"{base}?error=draft_not_complete", status_code=303)
    if league["pony_locked"]:
        return RedirectResponse(f"{base}?error=pony_locked", status_code=303)

    conn = get_db()
    team = conn.execute(
        adapt_sql("SELECT * FROM teams WHERE id=? AND league_id=?"),
        (team_id, league_id),
    ).fetchone()
    conn.close()
    if not team:
        raise HTTPException(status_code=404)
    if user["id"] != team["owner_id"] and not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)

    roster = get_team_roster(team_id)
    max_p = league.get("max_ponies_per_team") or 4
    if sum(1 for p in roster if p["is_pony"]) >= max_p:
        return RedirectResponse(f"{base}?error=pony_max", status_code=303)

    player = get_player_by_name_in_league(league_id, player_name)
    if not player:
        return RedirectResponse(f"{base}?error=player_not_found", status_code=303)

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO team_roster (team_id, player_id, is_pony, pony_revealed) VALUES (?,?,1,0)",
            (team_id, player["id"]),
        )
        conn.commit()
    except Exception:
        conn.close()
        return RedirectResponse(f"{base}?error=already_ponied", status_code=303)
    conn.close()

    # Auto-lock when all teams are at max ponies
    conn = get_db()
    incomplete = conn.execute(
        """
        SELECT COUNT(*) FROM teams t WHERE t.league_id=?
        AND (SELECT COUNT(*) FROM team_roster tr WHERE tr.team_id=t.id AND tr.is_pony=1) < ?
    """,
        (league_id, max_p),
    ).fetchone()[0]
    conn.close()
    if incomplete == 0:
        conn = get_db()
        conn.execute(
            adapt_sql("UPDATE leagues SET pony_locked=1 WHERE id=?"), (league_id,)
        )
        conn.commit()
        conn.close()
        write_audit(
            actor="system",
            action="PONY_LOCK",
            league_id=league_id,
            details="Auto-locked: all teams at max ponies",
        )

    write_audit(
        actor=user["username"],
        action="PONY_ADD",
        league_id=league_id,
        team=team["name"],
        player=player_name,
        details="private (unrevealed)",
    )
    return RedirectResponse(f"{base}?msg=pony_added", status_code=303)


@app.post("/league/{league_id}/team/{team_id}/pony/reveal")
def team_reveal_pony(
    league_id: int,
    team_id: int,
    roster_id: int = Form(...),
    user=Depends(get_current_user),
):
    """Owner manually reveals a single pony pick to all teams."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    base = f"/league/{league_id}/team/{team_id}"
    conn = get_db()
    team = conn.execute(
        adapt_sql("SELECT * FROM teams WHERE id=? AND league_id=?"),
        (team_id, league_id),
    ).fetchone()
    if not team or (
        user["id"] != team["owner_id"] and not is_commissioner(league, user)
    ):
        conn.close()
        raise HTTPException(status_code=403)
    conn.execute(
        adapt_sql(
            "UPDATE team_roster SET pony_revealed=1 WHERE id=? AND team_id=? AND is_pony=1"
        ),
        (roster_id, team_id),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="PONY_REVEAL",
        league_id=league_id,
        team=team["name"],
    )
    return RedirectResponse(f"{base}?msg=pony_revealed", status_code=303)


# ======================================================
# LEAGUE: PONY
# ======================================================


@app.post("/league/{league_id}/pony/add")
def add_pony(
    league_id: int,
    team: str = Form(...),
    player_name: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    base = f"/league/{league_id}"
    if league["pony_locked"]:
        return RedirectResponse(f"{base}/draft", status_code=303)

    team_obj = get_team_by_name_in_league(league_id, team)
    if not team_obj or (
        user["id"] != team_obj["owner_id"] and not is_commissioner(league, user)
    ):
        return RedirectResponse(f"{base}/draft", status_code=303)

    roster = get_team_roster(team_obj["id"])
    max_p = league.get("max_ponies_per_team") or 4
    if sum(1 for p in roster if p["is_pony"]) >= max_p:
        return RedirectResponse(f"{base}/draft", status_code=303)

    player = get_player_by_name_in_league(league_id, player_name)
    if not player:
        return RedirectResponse(f"{base}/draft", status_code=303)

    conn = get_db()

    # Ponies are open: any player in the league pool qualifies regardless of
    # whether they were drafted, and multiple teams can share the same pony.
    # The only uniqueness constraint is that THIS team cannot pony the same
    # player twice (enforced by the UNIQUE(team_id, player_id) index on team_roster).

    try:
        conn.execute(
            "INSERT INTO team_roster (team_id, player_id, is_pony, pony_revealed) VALUES (?,?,1,1)",
            (team_obj["id"], player["id"]),
        )
        conn.commit()
    except Exception:  # IntegrityError on duplicate roster entry
        conn.close()
        return RedirectResponse(f"{base}/draft", status_code=303)
    conn.close()

    # Auto-lock when all teams hit max ponies
    conn = get_db()
    incomplete = conn.execute(
        """
        SELECT COUNT(*) FROM teams t WHERE t.league_id=?
        AND (SELECT COUNT(*) FROM team_roster tr WHERE tr.team_id=t.id AND tr.is_pony=1) < ?
    """,
        (league_id, max_p),
    ).fetchone()[0]
    conn.close()
    if incomplete == 0:
        conn = get_db()
        conn.execute(
            adapt_sql("UPDATE leagues SET pony_locked=1 WHERE id=?"), (league_id,)
        )
        conn.commit()
        conn.close()
        write_audit(
            actor="system",
            action="PONY_LOCK",
            league_id=league_id,
            details="Auto-locked: all teams at max ponies",
        )

    write_audit(
        actor=user["username"],
        action="PONY_ADD",
        league_id=league_id,
        team=team,
        player=player_name,
    )
    return RedirectResponse(f"{base}/draft", status_code=303)


# ======================================================
# LEAGUE: SCORES
# ======================================================


@app.get("/league/{league_id}/scores")
def scores_page(
    league_id: int, request: Request, week: int = None, user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    current_week = get_current_week(league_id)
    if week is None:
        week = current_week

    teams = get_league_teams(league_id)
    team_scores = []
    for team in teams:
        result = get_team_week_score(team["id"], week, league_id)
        owner = get_user_by_id(team["owner_id"])
        team_scores.append({"team": team, "owner": owner, **result})
    team_scores.sort(key=lambda x: x["total"], reverse=True)

    standings = []
    for team in teams:
        pts = sum(
            get_team_week_score(team["id"], w, league_id)["total"]
            for w in range(1, current_week + 1)
        )
        standings.append(
            {
                "team": team,
                "owner": get_user_by_id(team["owner_id"]),
                "total_points": round(pts, 2),
            }
        )
    standings.sort(key=lambda x: x["total_points"], reverse=True)

    import os as _os

    nfl_season = int(_os.environ.get("NFL_SEASON", "2024"))

    return templates.TemplateResponse(
        "scores.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "week": week,
            "current_week": current_week,
            "team_scores": team_scores,
            "standings": standings,
            "season": nfl_season,
            "is_commissioner": is_commissioner(league, user),
            "my_team_id": get_user_team_in_league(league_id, user["id"]),
        },
    )


# ======================================================
# LEAGUE: COMMISSIONER PANEL  (/league/{id}/manage)
# ======================================================


@app.get("/league/{league_id}/manage")
def manage_page(league_id: int, request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403, detail="Commissioner only")

    conn = get_db()
    members = conn.execute(
        """
        SELECT u.id, u.username, lm.joined_at FROM league_members lm
        JOIN users u ON lm.user_id=u.id WHERE lm.league_id=?
    """,
        (league_id,),
    ).fetchall()
    cw = get_current_week(league_id)
    week_scores = {
        r["player_id"]: dict(r)
        for r in conn.execute(
            """
            SELECT ps.* FROM player_scores ps JOIN players p ON ps.player_id=p.id
            WHERE p.league_id=? AND ps.week=?
        """,
            (league_id, cw),
        ).fetchall()
    }
    conn.close()

    teams = get_league_teams(league_id)
    players = get_league_players(league_id)
    teams_with_owners = [
        {**t, "owner_name": (get_user_by_id(t["owner_id"]) or {}).get("username", "?")}
        for t in teams
    ]

    # Build roster list for commissioner move tool
    conn2 = get_db()
    roster_rows = conn2.execute(
        adapt_sql(
            """
        SELECT tr.id as roster_id, tr.team_id, tr.is_pony,
               p.name as player_name, p.position, p.nfl_team,
               t.name as team_name
        FROM team_roster tr
        JOIN players p ON tr.player_id = p.id
        JOIN teams t   ON tr.team_id   = t.id
        WHERE t.league_id=?
        ORDER BY t.name, p.position, p.name
    """
        ),
        (league_id,),
    ).fetchall()
    conn2.close()
    rosters_for_move = [dict(r) for r in roster_rows]

    return templates.TemplateResponse(
        "manage.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "members": [dict(m) for m in members],
            "teams": teams_with_owners,
            "players": players,
            "draft_state": get_draft_state(league_id),
            "current_week": cw,
            "multipliers_locked": multipliers_locked_for(league),
            "week_scores": week_scores,
            "msg": request.query_params.get("msg", ""),
            "is_commissioner": True,
            "sync_status": sync_scheduler.last_status(league_id),
            "current_nfl_week": current_nfl_week(),
            "my_team_id": get_user_team_in_league(league_id, user["id"]),
            "rosters_for_move": rosters_for_move,
            "snapshotted_weeks": [
                w for w in range(1, 5) if week_has_snapshot(league_id, w)
            ],
        },
    )


# -- Players --


@app.post("/league/{league_id}/manage/player/add")
def manage_add_player(
    league_id: int,
    name: str = Form(...),
    position: str = Form(...),
    nfl_team: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO players (league_id, name, position, nfl_team) VALUES (?,?,?,?)",
            (
                league_id,
                name.strip(),
                position.strip().upper(),
                nfl_team.strip().upper(),
            ),
        )
        conn.commit()
    except Exception:  # duplicate / constraint violation
        pass
    finally:
        conn.close()
    write_audit(
        actor=user["username"],
        action="PLAYER_ADD",
        league_id=league_id,
        player=name.strip(),
        details=f"pos={position.upper()} team={nfl_team.upper()}",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=player_added", status_code=303
    )


@app.post("/league/{league_id}/manage/player/edit")
def manage_edit_player(
    league_id: int,
    player_id: int = Form(...),
    name: str = Form(...),
    position: str = Form(...),
    nfl_team: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(
        adapt_sql(
            "UPDATE players SET name=?, position=?, nfl_team=? WHERE id=? AND league_id=?"
        ),
        (
            name.strip(),
            position.strip().upper(),
            nfl_team.strip().upper(),
            player_id,
            league_id,
        ),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="PLAYER_EDIT",
        league_id=league_id,
        player=name.strip(),
        details=f"id={player_id}",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=player_updated", status_code=303
    )


@app.post("/league/{league_id}/manage/pony/delete")
def manage_delete_pony(
    league_id: int, roster_id: int = Form(...), user=Depends(get_current_user)
):
    """Commissioner removes a pony pick from a team roster."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    row = conn.execute(
        adapt_sql(
            """
        SELECT tr.id, t.name AS team_name, p.name AS player_name
        FROM team_roster tr
        JOIN teams t ON tr.team_id = t.id
        JOIN players p ON tr.player_id = p.id
        WHERE tr.id = ? AND t.league_id = ? AND tr.is_pony = 1
    """
        ),
        (roster_id, league_id),
    ).fetchone()
    if not row:
        conn.close()
        return RedirectResponse(
            f"/league/{league_id}/manage?error=pony_not_found#moves", status_code=303
        )
    conn.execute(
        adapt_sql("DELETE FROM team_roster WHERE id=? AND is_pony=1"), (roster_id,)
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="PONY_DELETE",
        league_id=league_id,
        team=row["team_name"],
        player=row["player_name"],
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=pony_deleted#moves", status_code=303
    )


@app.post("/league/{league_id}/manage/player/delete")
def manage_delete_player(
    league_id: int, player_id: int = Form(...), user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM players WHERE id=? AND league_id=?", (player_id, league_id)
    ).fetchone()
    pname = row["name"] if row else str(player_id)
    conn.execute(adapt_sql("DELETE FROM team_roster WHERE player_id=?"), (player_id,))
    conn.execute(adapt_sql("DELETE FROM player_scores WHERE player_id=?"), (player_id,))
    conn.execute(
        adapt_sql("DELETE FROM players WHERE id=? AND league_id=?"),
        (player_id, league_id),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="PLAYER_DELETE",
        league_id=league_id,
        player=pname,
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=player_deleted", status_code=303
    )


# -- Teams --


@app.post("/league/{league_id}/manage/team/add")
def manage_add_team(
    league_id: int,
    name: str = Form(...),
    owner_username: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    owner = get_user(owner_username)
    if not owner:
        return RedirectResponse(
            f"/league/{league_id}/manage?error=no_user", status_code=303
        )
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO teams (league_id, name, owner_id) VALUES (?,?,?)",
            (league_id, name.strip(), owner["id"]),
        )
        conn.commit()
    except Exception:  # duplicate / constraint violation
        pass
    finally:
        conn.close()
    write_audit(
        actor=user["username"],
        action="TEAM_ADD",
        league_id=league_id,
        team=name.strip(),
        details=f"owner={owner_username}",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=team_added", status_code=303
    )


@app.post("/league/{league_id}/manage/team/edit")
def manage_edit_team(
    league_id: int,
    team_id: int = Form(...),
    name: str = Form(...),
    owner_username: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    owner = get_user(owner_username)
    if not owner:
        return RedirectResponse(
            f"/league/{league_id}/manage?error=no_user", status_code=303
        )
    conn = get_db()
    conn.execute(
        adapt_sql("UPDATE teams SET name=?, owner_id=? WHERE id=? AND league_id=?"),
        (name.strip(), owner["id"], team_id, league_id),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="TEAM_EDIT",
        league_id=league_id,
        team=name.strip(),
        details=f"new_owner={owner_username}",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=team_updated", status_code=303
    )


@app.post("/league/{league_id}/manage/team/delete")
def manage_delete_team(
    league_id: int, team_id: int = Form(...), user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM teams WHERE id=? AND league_id=?", (team_id, league_id)
    ).fetchone()
    tname = row["name"] if row else str(team_id)
    conn.execute(adapt_sql("DELETE FROM team_roster WHERE team_id=?"), (team_id,))
    conn.execute(
        adapt_sql("DELETE FROM teams WHERE id=? AND league_id=?"), (team_id, league_id)
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"], action="TEAM_DELETE", league_id=league_id, team=tname
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=team_deleted", status_code=303
    )


# -- Draft controls --


@app.post("/league/{league_id}/manage/draft/reset")
def manage_reset_draft(league_id: int, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(
        adapt_sql(
            "UPDATE draft_state SET current_round=1, current_pick=0, is_complete=0 WHERE league_id=?"
        ),
        (league_id,),
    )
    conn.execute(
        adapt_sql(
            """
        DELETE FROM team_roster WHERE is_pony=0 AND team_id IN (
            SELECT id FROM teams WHERE league_id=?
        )
    """
        ),
        (league_id,),
    )
    conn.execute(adapt_sql("DELETE FROM draft_chat WHERE league_id=?"), (league_id,))
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="DRAFT_RESET",
        league_id=league_id,
        details="All non-pony picks cleared",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=draft_reset", status_code=303
    )


@app.post("/league/{league_id}/manage/draft/rewind")
def manage_rewind_draft(league_id: int, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    state = conn.execute(
        "SELECT current_round, current_pick FROM draft_state WHERE league_id=?",
        (league_id,),
    ).fetchone()
    teams = conn.execute(
        "SELECT * FROM teams WHERE league_id=? ORDER BY id", (league_id,)
    ).fetchall()
    if not teams:
        conn.close()
        return RedirectResponse(
            f"/league/{league_id}/manage?msg=no_teams", status_code=303
        )

    new_pick = state["current_pick"] - 1
    new_round = state["current_round"]
    if new_pick < 0:
        new_round = max(1, new_round - 1)
        new_pick = len(teams) - 1

    ordered = get_snake_order(teams, new_round)
    last_team = ordered[new_pick % len(ordered)]

    last = conn.execute(
        """
        SELECT tr.id FROM team_roster tr WHERE tr.team_id=? AND tr.is_pony=0
        ORDER BY tr.id DESC LIMIT 1
    """,
        (last_team["id"],),
    ).fetchone()
    if last:
        conn.execute(adapt_sql("DELETE FROM team_roster WHERE id=?"), (last["id"],))

    conn.execute(
        adapt_sql(
            "UPDATE draft_state SET current_round=?, current_pick=? WHERE league_id=?"
        ),
        (new_round, new_pick, league_id),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="DRAFT_REWIND",
        league_id=league_id,
        team=last_team["name"] if last else None,
        details=f"Rewound to R{new_round} P{new_pick+1}",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=draft_rewound", status_code=303
    )


# -- Pony lock --


@app.post("/league/{league_id}/manage/pony/lock")
def manage_pony_lock(
    league_id: int, lock: int = Form(...), user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(
        adapt_sql("UPDATE leagues SET pony_locked=? WHERE id=?"), (lock, league_id)
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="PONY_LOCK" if lock else "PONY_UNLOCK",
        league_id=league_id,
        details=f"Manually {'locked' if lock else 'unlocked'}",
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=pony_lock_updated", status_code=303
    )


# -- League settings --


@app.post("/league/{league_id}/manage/settings")
def manage_settings(
    league_id: int,
    league_name: str = Form(...),
    picks_per_team: int = Form(15),
    max_ponies_per_team: int = Form(4),
    multiplier_lock_ts: str = Form(...),
    pick_timer_seconds: int = Form(0),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    pick_timer_seconds = max(0, min(pick_timer_seconds, 600))  # cap at 10 min
    conn = get_db()
    conn.execute(
        adapt_sql(
            """
        UPDATE leagues
        SET name=?, picks_per_team=?, max_ponies_per_team=?,
            multiplier_lock_ts=?, pick_timer_seconds=?
        WHERE id=?
    """
        ),
        (
            league_name.strip(),
            picks_per_team,
            max_ponies_per_team,
            multiplier_lock_ts,
            pick_timer_seconds,
            league_id,
        ),
    )
    conn.commit()
    conn.close()
    # If timer changed while draft is in progress, re-arm
    cancel_pick_timer(league_id)
    if pick_timer_seconds:
        state = get_draft_state(league_id)
        if not state.get("is_complete"):
            arm_pick_timer(
                league_id,
                pick_timer_seconds,
                state["current_round"],
                state["current_pick"],
            )
    write_audit(
        actor=user["username"],
        action="LEAGUE_SETTINGS_UPDATE",
        league_id=league_id,
        details=(
            f"name={league_name} picks={picks_per_team} ponies={max_ponies_per_team}"
            f" timer={pick_timer_seconds}s"
        ),
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=settings_saved", status_code=303
    )


# -- Score entry --


@app.post("/league/{league_id}/manage/scores/entry")
def manage_score_entry(
    league_id: int,
    player_id: int = Form(...),
    week: int = Form(...),
    receptions: float = Form(0),
    receiving_yards: float = Form(0),
    rushing_yards: float = Form(0),
    return_yards: float = Form(0),
    passing_yards: float = Form(0),
    total_tds: int = Form(0),
    fumbles_lost: int = Form(0),
    interceptions: int = Form(0),
    return_fumbles_lost: int = Form(0),
    override_points: str = Form(""),
    override_note: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    override = float(override_points) if override_points.strip() else None
    conn = get_db()
    p_row = conn.execute(
        "SELECT name FROM players WHERE id=? AND league_id=?", (player_id, league_id)
    ).fetchone()
    if not p_row:
        conn.close()
        return RedirectResponse(
            f"/league/{league_id}/manage?error=bad_player", status_code=303
        )
    p_name = p_row["name"]

    conn.execute(
        adapt_sql(
            """
        INSERT INTO player_scores (
            player_id, week, receptions, receiving_yards, rushing_yards,
            return_yards, passing_yards, total_tds, fumbles_lost, interceptions,
            return_fumbles_lost, override_points, override_note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(player_id, week) DO UPDATE SET
            receptions=excluded.receptions, receiving_yards=excluded.receiving_yards,
            rushing_yards=excluded.rushing_yards, return_yards=excluded.return_yards,
            passing_yards=excluded.passing_yards, total_tds=excluded.total_tds,
            fumbles_lost=excluded.fumbles_lost, interceptions=excluded.interceptions,
            return_fumbles_lost=excluded.return_fumbles_lost,
            override_points=excluded.override_points, override_note=excluded.override_note
    """
        ),
        (
            player_id,
            week,
            receptions,
            receiving_yards,
            rushing_yards,
            return_yards,
            passing_yards,
            total_tds,
            fumbles_lost,
            interceptions,
            return_fumbles_lost,
            override,
            override_note.strip() or None,
        ),
    )
    conn.commit()
    conn.close()

    if override is not None:
        write_audit(
            actor=user["username"],
            action="SCORE_OVERRIDE",
            league_id=league_id,
            player=p_name,
            details=f"week={week} override={override} note={override_note.strip()}",
        )
    else:
        write_audit(
            actor=user["username"],
            action="SCORE_ENTRY",
            league_id=league_id,
            player=p_name,
            details=f"week={week} rec={receptions} recYd={receiving_yards} "
            f"rushYd={rushing_yards} passYd={passing_yards} tds={total_tds}",
        )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=score_saved", status_code=303
    )


# ======================================================
# LEAGUE: AUDIT LOG
# ======================================================


@app.get("/league/{league_id}/audit")
def audit_log_page(
    league_id: int,
    request: Request,
    action: str = None,
    search: str = None,
    limit: int = 200,
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    conn = get_db()
    conditions = ["league_id=?"]
    params: list = [league_id]
    if action and action != "ALL":
        conditions.append("action=?")
        params.append(action)
    if search:
        conditions.append(
            "(actor LIKE ? OR team LIKE ? OR player LIKE ? OR details LIKE ?)"
        )
        s = f"%{search}%"
        params.extend([s, s, s, s])

    where = "WHERE " + " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", params + [limit]
    ).fetchall()
    action_types = [
        r["action"]
        for r in conn.execute(
            "SELECT DISTINCT action FROM audit_log WHERE league_id=? ORDER BY action",
            (league_id,),
        ).fetchall()
    ]
    conn.close()

    return templates.TemplateResponse(
        "audit_log.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "logs": [dict(r) for r in rows],
            "action_types": action_types,
            "filter_action": action or "ALL",
            "filter_search": search or "",
            "limit": limit,
            "total": len(rows),
            "is_commissioner": True,
        },
    )


# ======================================================
# TEAM PAGE HELPERS
# ======================================================


def generate_schedule(teams: list, total_weeks: int = 18) -> dict[tuple, list]:
    """
    Generate a round-robin schedule for all weeks.
    Returns dict: {week: [(home_id, away_id), ...]}
    Each team plays once per week; odd number of teams gets a bye.
    """
    ids = [t["id"] for t in teams]
    if len(ids) < 2:
        return {}

    # Pad to even number with a bye (None)
    if len(ids) % 2 != 0:
        ids.append(None)

    n = len(ids)
    half = n // 2
    fixed = ids[0]
    rotate = ids[1:]
    schedule: dict[int, list] = {}

    for week in range(1, total_weeks + 1):
        matchups = []
        round_ids = [fixed] + rotate
        for i in range(half):
            home = round_ids[i]
            away = round_ids[n - 1 - i]
            if home is not None and away is not None:
                matchups.append((home, away))
        schedule[week] = matchups
        # Rotate for next week
        rotate = [rotate[-1]] + rotate[:-1]

    return schedule


def get_team_matchup_history(team_id: int, league_id: int) -> list[dict]:
    """
    Returns a list of weekly matchup records for the given team.
    Each entry: {week, opponent_id, opponent_name, my_pts, opp_pts, result}
    """
    teams = get_league_teams(league_id)
    if len(teams) < 2:
        return []

    schedule = generate_schedule(teams)
    cw = get_current_week(league_id)
    history = []

    # Build id→name map
    id_to_name = {t["id"]: t["name"] for t in teams}

    for week in range(1, cw + 1):
        matchups = schedule.get(week, [])
        opponent_id = None
        for home, away in matchups:
            if home == team_id:
                opponent_id = away
                break
            if away == team_id:
                opponent_id = home
                break

        if opponent_id is None:
            history.append(
                {"week": week, "bye": True, "my_pts": 0, "opp_pts": 0, "result": "BYE"}
            )
            continue

        my_pts = get_team_week_score(team_id, week)["total"]
        opp_pts = get_team_week_score(opponent_id, week)["total"]
        result = "W" if my_pts > opp_pts else ("L" if my_pts < opp_pts else "T")

        history.append(
            {
                "week": week,
                "bye": False,
                "opponent_id": opponent_id,
                "opponent_name": id_to_name.get(opponent_id, "?"),
                "my_pts": my_pts,
                "opp_pts": opp_pts,
                "result": result,
            }
        )

    return history


# ======================================================
# TEAM PAGE ROUTE
# ======================================================


@app.get("/league/{league_id}/team/{team_id}", response_class=HTMLResponse)
def team_page(
    league_id: int,
    team_id: int,
    request: Request,
    week: int = None,
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    # Load team — must belong to this league
    conn = get_db()
    team = conn.execute(
        adapt_sql("SELECT * FROM teams WHERE id=? AND league_id=?"),
        (team_id, league_id),
    ).fetchone()
    conn.close()

    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    team = dict(team)

    # Only the team owner or commissioner can view
    is_comm = is_commissioner(league, user)
    is_owner = team["owner_id"] == user["id"]
    if not is_comm and not is_owner:
        raise HTTPException(status_code=403, detail="Not your team")

    current_week = get_current_week(league_id)
    if week is None:
        week = current_week

    # Roster with this week's scores
    week_data = get_team_week_score(team_id, week, league_id, show_hidden_ponies=True)
    roster = get_team_roster(team_id)

    # Season totals per player across all scored weeks
    all_weeks = list(range(1, current_week + 1))
    player_season_pts: dict[int, float] = {}
    for w in all_weeks:
        wd = get_team_week_score(team_id, w, league_id, show_hidden_ponies=True)
        for p in wd["players"]:
            pid = p["player_id"]
            player_season_pts[pid] = round(
                player_season_pts.get(pid, 0.0) + p["final_points"], 2
            )

    # Weekly totals (for sparkline / history)
    weekly_totals = [
        {"week": w, "total": get_team_week_score(team_id, w, league_id)["total"]}
        for w in all_weeks
    ]
    season_total = round(sum(x["total"] for x in weekly_totals), 2)

    # Owner info
    owner = get_user_by_id(team["owner_id"])

    return templates.TemplateResponse(
        "team.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "team": team,
            "owner": owner,
            "is_owner": is_owner,
            "is_commissioner": is_comm,
            "week": week,
            "current_week": current_week,
            "week_data": week_data,
            "roster": roster,
            "player_season_pts": player_season_pts,
            "weekly_totals": weekly_totals,
            "season_total": season_total,
            "multipliers_locked": multipliers_locked_for(league),
            "now_utc": datetime.now(timezone.utc).isoformat(),
            "msg": request.query_params.get("msg", ""),
            "my_team_id": team_id,
            "season": int(__import__("os").environ.get("NFL_SEASON", "2024")),
            "pony_locked": bool(league["pony_locked"]),
            "draft_complete": bool(get_draft_state(league_id).get("is_complete")),
            "max_ponies": league.get("max_ponies_per_team") or 4,
            "pony_count": sum(1 for p in roster if p.get("is_pony")),
            "league_players": get_league_players(league_id),
        },
    )


@app.get("/league/{league_id}/audit/export.csv")
def audit_export_csv(
    league_id: int,
    action: str = None,
    search: str = None,
    user=Depends(get_current_user),
):
    """Download the audit log as a CSV file (commissioner only, no row limit)."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    conn = get_db()
    conditions = ["league_id=?"]
    params: list = [league_id]
    if action and action != "ALL":
        conditions.append("action=?")
        params.append(action)
    if search:
        conditions.append(
            "(actor LIKE ? OR team LIKE ? OR player LIKE ? OR details LIKE ?)"
        )
        s = f"%{search}%"
        params.extend([s, s, s, s])

    where = "WHERE " + " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT id, ts, actor, action, team, player, details FROM audit_log {where} ORDER BY id DESC",
        params,
    ).fetchall()
    conn.close()

    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "timestamp", "actor", "action", "team", "player", "details"])
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["ts"],
                r["actor"] or "",
                r["action"] or "",
                r["team"] or "",
                r["player"] or "",
                r["details"] or "",
            ]
        )

    league_slug = (league["name"] or "league").lower().replace(" ", "_")
    filename = f"audit_{league_slug}_{league_id}.csv"

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ======================================================
# DRAFT CHAT / PICK LOG
# ======================================================


def write_chat(league_id: int, username: str, message: str, msg_type: str = "chat"):
    conn = get_db()
    conn.execute(
        adapt_sql(
            "INSERT INTO draft_chat (league_id, ts, username, msg_type, message) VALUES (?,?,?,?,?)"
        ),
        (
            league_id,
            datetime.now(timezone.utc).isoformat(),
            username,
            msg_type,
            message,
        ),
    )
    conn.commit()
    conn.close()


def get_chat(league_id: int, limit: int = 60) -> list:
    conn = get_db()
    rows = conn.execute(
        adapt_sql(
            """
        SELECT id, ts, username, msg_type, message
        FROM draft_chat WHERE league_id=?
        ORDER BY ts DESC LIMIT ?
    """
        ),
        (league_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


@app.post("/league/{league_id}/draft/chat")
def post_chat(league_id: int, message: str = Form(...), user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)  # membership check
    msg = message.strip()[:280]
    if msg:
        write_chat(league_id, user["username"], msg, "chat")
    return {"ok": True}


@app.get("/api/league/{league_id}/draft/chat")
def get_chat_api(league_id: int, since_id: int = 0, user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)
    conn = get_db()
    rows = conn.execute(
        adapt_sql(
            """
        SELECT id, ts, username, msg_type, message
        FROM draft_chat WHERE league_id=? AND id > ?
        ORDER BY ts ASC LIMIT 80
    """
        ),
        (league_id, since_id),
    ).fetchall()
    conn.close()
    return {"messages": [dict(r) for r in rows]}


@app.post("/league/{league_id}/manage/roster/move")
def manage_move_player(
    league_id: int,
    roster_id: int = Form(...),
    to_team_id: int = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    conn = get_db()
    row = conn.execute(
        adapt_sql(
            """
        SELECT tr.id, tr.team_id, tr.player_id, tr.is_pony,
               p.name as player_name, t.name as from_team
        FROM team_roster tr
        JOIN players p ON tr.player_id = p.id
        JOIN teams t   ON tr.team_id   = t.id
        WHERE tr.id=? AND t.league_id=?
    """
        ),
        (roster_id, league_id),
    ).fetchone()

    to_team = conn.execute(
        adapt_sql("SELECT * FROM teams WHERE id=? AND league_id=?"),
        (to_team_id, league_id),
    ).fetchone()

    if not row or not to_team:
        conn.close()
        return RedirectResponse(
            f"/league/{league_id}/manage?error=not_found", status_code=303
        )

    # Check not already on that team
    if row["team_id"] == to_team_id:
        conn.close()
        return RedirectResponse(
            f"/league/{league_id}/manage?error=same_team", status_code=303
        )

    conn.execute(
        adapt_sql("UPDATE team_roster SET team_id=? WHERE id=?"),
        (to_team_id, row["id"]),
    )
    conn.commit()
    conn.close()

    write_audit(
        actor=user["username"],
        action="ROSTER_MOVE",
        league_id=league_id,
        team=to_team["name"],
        player=row["player_name"],
        details=f"Moved from {row['from_team']} to {to_team['name']}",
    )
    write_chat(
        league_id,
        "system",
        f"Commissioner moved {row['player_name']} from {row['from_team']} to {to_team['name']}",
        "system",
    )

    return RedirectResponse(
        f"/league/{league_id}/manage?msg=player_moved", status_code=303
    )


@app.get("/api/nfl-eliminated")
def api_nfl_eliminated(season: int = None):
    """
    Returns the set of NFL team abbreviations that have been eliminated
    from the current playoffs.  Fetches all four rounds from ESPN and
    collects the losing team of every completed game.
    Result is cached for 5 minutes to avoid hammering ESPN.
    """
    import os
    import time

    if season is None:
        season = int(os.environ.get("NFL_SEASON", "2024"))

    # Simple in-process cache: (season, timestamp, result)
    cache = getattr(api_nfl_eliminated, "_cache", None)
    if cache and cache[0] == season and time.time() - cache[1] < 300:
        return cache[2]

    eliminated: set = set()

    # ESPN team abbrev aliases (same as client-side)
    ALIAS = {
        "WSH": "WSH",
        "WAS": "WSH",
        "LV": "LV",
        "LAS": "LV",
        "LA": "LAR",
        "JAC": "JAX",
    }

    def norm(t):
        return ALIAS.get(t.upper(), t.upper())

    for fantasy_week in [1, 2, 3, 4]:
        espn_week = PLAYOFF_WEEK_MAP.get(fantasy_week, fantasy_week)
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
            f"?week={espn_week}&seasontype=3&dates={season}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue  # network error — skip this round

        for event in data.get("events", []):
            comps = event.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]
            status_type = event.get("status", {}).get("type", {})
            if not status_type.get("completed", False):
                continue  # game not finished yet

            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = next(
                (c for c in competitors if c.get("homeAway") == "home"), competitors[0]
            )
            away = next(
                (c for c in competitors if c.get("homeAway") == "away"), competitors[1]
            )

            try:
                hs = int(home.get("score", 0))
                as_ = int(away.get("score", 0))
            except (ValueError, TypeError):
                continue

            home_abbr = norm(home.get("team", {}).get("abbreviation", ""))
            away_abbr = norm(away.get("team", {}).get("abbreviation", ""))

            if hs > as_:
                eliminated.add(away_abbr)
            elif as_ > hs:
                eliminated.add(home_abbr)
            # tie = ignore (shouldn't happen in NFL playoffs)

    result = {"eliminated": sorted(eliminated), "season": season}
    api_nfl_eliminated._cache = (season, time.time(), result)
    return result


@app.post("/league/{league_id}/manage/multiplier-lock")
def manage_multiplier_lock(
    league_id: int,
    action: str = Form(...),  # "lock" | "unlock" | "set"
    lock_ts: str = Form(None),  # ISO timestamp, used when action="set"
    user=Depends(get_current_user),
):
    """Commissioner lock/unlock multipliers. Actions:
    lock   → sets lock_ts to now (immediately locked)
    unlock → sets lock_ts to 2099 (open indefinitely)
    set    → sets lock_ts to the provided ISO timestamp
    """
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    if action == "lock":
        ts = datetime.now(timezone.utc).isoformat()
        label = "Locked immediately"
    elif action == "unlock":
        ts = "2099-01-01T00:00:00+00:00"
        label = "Unlocked (open indefinitely)"
    elif action == "set" and lock_ts:
        try:
            datetime.fromisoformat(lock_ts)  # validate
            ts = lock_ts
            label = f"Auto-lock set to {lock_ts}"
        except ValueError:
            return RedirectResponse(
                f"/league/{league_id}/manage?error=bad_ts", status_code=303
            )
    else:
        return RedirectResponse(f"/league/{league_id}/manage", status_code=303)

    conn = get_db()
    conn.execute(
        adapt_sql("UPDATE leagues SET multiplier_lock_ts=? WHERE id=?"), (ts, league_id)
    )
    conn.commit()
    conn.close()

    # When locking, snapshot multipliers AND auto-reveal all pony picks.
    if action in ("lock", "set"):
        cw = get_current_week(league_id)
        snapshot_multipliers_for_week(league_id, cw)
        conn2 = get_db()
        conn2.execute(
            adapt_sql(
                """
            UPDATE team_roster SET pony_revealed=1
            WHERE is_pony=1 AND pony_revealed=0
              AND team_id IN (SELECT id FROM teams WHERE league_id=?)
        """
            ),
            (league_id,),
        )
        conn2.commit()
        conn2.close()
        write_audit(
            actor=user["username"],
            action="PONY_REVEAL_ALL",
            league_id=league_id,
            details="Auto-revealed at multiplier lock",
        )

    write_audit(
        actor=user["username"],
        action="MULTIPLIER_LOCK",
        league_id=league_id,
        details=label,
    )
    return RedirectResponse(
        f"/league/{league_id}/manage?msg=lock_updated#settings", status_code=303
    )


@app.get("/api/league/{league_id}/playoff-kickoffs")
def api_playoff_kickoffs(league_id: int, user=Depends(get_current_user)):
    """
    Returns the first scheduled kickoff time for each of the 4 playoff rounds
    by querying ESPN for each round.  Used to auto-populate the multiplier lock
    time.  Results are cached 10 min.
    """
    import os
    import time as _time

    cache = getattr(api_playoff_kickoffs, "_cache", None)
    season = int(os.environ.get("NFL_SEASON", "2024"))
    if cache and cache[0] == season and _time.time() - cache[1] < 600:
        return cache[2]

    rounds = {}
    for fantasy_week in [1, 2, 3, 4]:
        espn_week = PLAYOFF_WEEK_MAP.get(fantasy_week, fantasy_week)
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
            f"?week={espn_week}&seasontype=3&dates={season}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            rounds[fantasy_week] = {"error": str(e)}
            continue

        kickoffs = []
        for event in data.get("events", []):
            dt = event.get("date")  # ESPN ISO8601 UTC e.g. "2025-01-11T18:00Z"
            if dt:
                kickoffs.append(dt)

        kickoffs.sort()
        first = kickoffs[0] if kickoffs else None
        rounds[fantasy_week] = {
            "round_name": PLAYOFF_ROUND_NAMES.get(fantasy_week, f"Week {fantasy_week}"),
            "first_kickoff": first,
            "game_count": len(kickoffs),
        }

    result = {"rounds": rounds, "season": season}
    api_playoff_kickoffs._cache = (season, _time.time(), result)
    return result


# ======================================================
# NFL SYNC ROUTES
# ======================================================


@app.post("/league/{league_id}/manage/sync/roster")
def sync_roster(
    league_id: int, overwrite: bool = Form(False), user=Depends(get_current_user)
):
    """Seed the league player pool from current NFL rosters."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    try:
        result = seed_players(league_id, overwrite=overwrite)
        added = result.get("added", 0)
        msg = f"sync_ok_{added}"
        write_audit(
            actor=user["username"],
            action="ROSTER_SYNC",
            league_id=league_id,
            details=f"added={added} skipped={result.get('skipped',0)}",
        )
    except Exception as e:
        msg = "sync_error"
        write_audit(
            actor=user["username"],
            action="ROSTER_SYNC_ERROR",
            league_id=league_id,
            details=str(e),
        )

    return RedirectResponse(
        f"/league/{league_id}/manage?msg={msg}#sync", status_code=303
    )


@app.post("/league/{league_id}/manage/sync/week")
def sync_scores_now(
    league_id: int, week: int = Form(...), user=Depends(get_current_user)
):
    """Manually trigger a stat sync for a specific week."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    try:
        result = (
            sync_scheduler.sync_now(league_id)
            if week == current_nfl_week()
            else sync_week(league_id, week)
        )
        updated = result.get("updated", 0)
        msg = f"scores_synced_{updated}"
        write_audit(
            actor=user["username"],
            action="SCORES_SYNC",
            league_id=league_id,
            details=f"week={week} updated={updated}",
        )
    except Exception as e:
        msg = "sync_error"
        write_audit(
            actor=user["username"],
            action="SCORES_SYNC_ERROR",
            league_id=league_id,
            details=str(e),
        )

    return RedirectResponse(
        f"/league/{league_id}/manage?msg={msg}#sync", status_code=303
    )


@app.get("/api/league/{league_id}/sync/status")
def api_sync_status(league_id: int, user=Depends(get_current_user)):
    """Return the last sync result for this league (JSON)."""
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)
    status = sync_scheduler.last_status(league_id)
    return {
        "league_id": league_id,
        "current_week": current_nfl_week(),
        "last_sync": status,
    }


# ======================================================
# JSON API
# ======================================================


def build_draft_state_payload(league_id: int, league: dict) -> dict:
    """Full snapshot used by polling and SSE alike."""
    state = get_draft_state(league_id)
    team_on_clock = get_team_on_clock(league_id)
    timer_secs = league.get("pick_timer_seconds") or 0
    remaining = get_timer_remaining(league_id, timer_secs, state.get("pick_started_at"))
    teams = sort_teams_by_draft_order(get_league_teams(league_id), league)
    rosters = {t["name"]: get_team_roster(t["id"]) for t in teams}
    return {
        "state": state,
        "team_on_clock": team_on_clock,
        "timer_seconds": timer_secs,
        "timer_remaining": remaining,
        "rosters": rosters,
        "is_complete": bool(state.get("is_complete")),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/league/{league_id}/draft/state")
def api_draft_state(league_id: int, user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    return build_draft_state_payload(league_id, dict(league))


@app.get("/api/league/{league_id}/draft/stream")
def api_draft_stream(league_id: int, user=Depends(get_current_user)):
    """
    Server-Sent Events endpoint.  Pushes a full state snapshot every
    second; only sends a data frame when state actually changed so
    the client can detect changes without diffing.  A bare ': ping'
    comment is sent on idle ticks to keep the TCP connection alive
    through proxies and load balancers.

    Each connected client holds one thread.  For >500 concurrent
    drafts, switch to an async generator with asyncio.sleep().
    """
    if not user:
        raise HTTPException(status_code=401)
    league = dict(league_ctx(league_id, user))

    def event_generator():
        last_sig = None
        idle_ticks = 0
        while True:
            try:
                payload = build_draft_state_payload(league_id, league)
                # Use (round, pick, is_complete) as a cheap change signal
                sig = (
                    payload["state"].get("current_round"),
                    payload["state"].get("current_pick"),
                    payload["is_complete"],
                    payload.get("timer_remaining"),
                )
                if sig != last_sig:
                    yield f"data: {json.dumps(payload, default=str)}\n\n"
                    last_sig = sig
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    if idle_ticks >= 15:  # ping every 15 s
                        yield ": ping\n\n"
                        idle_ticks = 0
            except GeneratorExit:
                break
            except Exception:
                break
            time.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # prevent nginx output buffering
            "Connection": "keep-alive",
        },
    )


# Playoff fantasy week → ESPN postseason week
# ESPN skips week 4 (Pro Bowl), so Super Bowl = week 5
PLAYOFF_WEEK_MAP = {1: 1, 2: 2, 3: 3, 4: 5}

PLAYOFF_ROUND_NAMES = {
    1: "Wild Card",
    2: "Divisional Round",
    3: "Conference Championships",
    4: "Super Bowl",
}


@app.get("/api/nfl-games")
def api_nfl_games(week: int, season: int = None):
    """
    Fetch NFL playoff game scores from ESPN's public postseason scoreboard.
    week = fantasy playoff week (1=Wild Card, 2=Divisional, 3=Conf Champ, 4=Super Bowl)
    Returns games keyed by team abbreviation + a games_list for display.
    """
    import os

    if season is None:
        season = int(os.environ.get("NFL_SEASON", "2024"))

    espn_week = PLAYOFF_WEEK_MAP.get(week, week)
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        f"?week={espn_week}&seasontype=3&dates={season}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return {"error": str(e), "games": {}}

    games_by_team: dict = {}
    for event in data.get("events", []):
        comps = event.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = next(
            (c for c in competitors if c.get("homeAway") == "home"), competitors[0]
        )
        away = next(
            (c for c in competitors if c.get("homeAway") == "away"), competitors[1]
        )

        home_abbr = home.get("team", {}).get("abbreviation", "").upper()
        away_abbr = away.get("team", {}).get("abbreviation", "").upper()
        home_score = home.get("score", "0")
        away_score = away.get("score", "0")

        status_obj = event.get("status", {})
        status_type = status_obj.get("type", {})
        status_desc = status_type.get("description", "Scheduled")
        completed = status_type.get("completed", False)

        # Live game info
        situation = comp.get("situation", {})
        period = situation.get("period", 0)
        clock = situation.get("displayClock", "")
        quarter_str = ""
        if not completed and period > 0:
            q_names = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4", 5: "OT"}
            quarter_str = q_names.get(period, f"P{period}")
            if clock:
                quarter_str += f" {clock}"

        game = {
            "home": home_abbr,
            "away": away_abbr,
            "home_score": home_score,
            "away_score": away_score,
            "status": status_desc,
            "completed": completed,
            "live": quarter_str,
            "name": event.get("name", ""),
        }
        games_by_team[home_abbr] = game
        games_by_team[away_abbr] = game

    # Ordered list for display in the scores sidebar
    games_list = list({id(g): g for g in games_by_team.values()}.values())

    return {
        "games": games_by_team,
        "games_list": games_list,
        "week": week,
        "season": season,
        "round_name": PLAYOFF_ROUND_NAMES.get(week, f"Week {week}"),
    }


@app.get("/api/league/{league_id}/scores/week/{week}")
def api_week_scores(league_id: int, week: int, user=Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)
    return [
        {"team": t, **get_team_week_score(t["id"], week, league_id)}
        for t in get_league_teams(league_id)
    ]


@app.get("/live-data")
def live_data():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ======================================================
# SITE ADMIN  (/admin)
# ======================================================

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user=Depends(get_current_user)):
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403, detail="Superadmin only")
    conn = get_db()
    total_users     = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    playoff_leagues = conn.execute("SELECT COUNT(*) FROM leagues").fetchone()[0]
    total_leagues   = playoff_leagues
    survivor_leagues_count = 0
    try:
        from survivor_db import get_connection as _surv_conn
        sconn = _surv_conn()
        survivor_leagues_count = sconn.execute("SELECT COUNT(*) FROM survivor_leagues").fetchone()[0]
        sconn.close()
        total_leagues = playoff_leagues + survivor_leagues_count
    except Exception:
        pass
    total_teams     = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    total_players   = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    total_picks     = conn.execute("SELECT COUNT(*) FROM team_roster WHERE is_pony=0").fetchone()[0]
    total_ponies    = conn.execute("SELECT COUNT(*) FROM team_roster WHERE is_pony=1").fetchone()[0]
    total_scores    = conn.execute("SELECT COUNT(*) FROM player_scores").fetchone()[0]
    total_audits    = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    drafts_complete = conn.execute("SELECT COUNT(*) FROM draft_state WHERE is_complete=1").fetchone()[0]
    drafts_active   = conn.execute("SELECT COUNT(*) FROM draft_state WHERE is_complete=0").fetchone()[0]
    users = conn.execute("""
        SELECT u.id, u.username, u.email, u.is_superadmin,
               COUNT(DISTINCT lm.league_id) AS league_count,
               COUNT(DISTINCT t.id)         AS team_count
        FROM users u
        LEFT JOIN league_members lm ON lm.user_id = u.id
        LEFT JOIN teams t           ON t.owner_id  = u.id
        GROUP BY u.id ORDER BY u.id DESC
    """).fetchall()
    leagues = conn.execute("""
        SELECT l.id, l.name, l.created_at, l.invite_code,
               u.username AS commissioner,
               COUNT(DISTINCT lm.user_id) AS member_count,
               COUNT(DISTINCT t.id)       AS team_count,
               COUNT(DISTINCT p.id)       AS player_count,
               ds.is_complete             AS draft_complete
        FROM leagues l
        JOIN users u                ON l.commissioner_id = u.id
        LEFT JOIN league_members lm ON lm.league_id = l.id
        LEFT JOIN teams t           ON t.league_id  = l.id
        LEFT JOIN players p         ON p.league_id  = l.id
        LEFT JOIN draft_state ds    ON ds.league_id = l.id
        GROUP BY l.id ORDER BY l.created_at DESC
    """).fetchall()
    recent_users = conn.execute(
        "SELECT id, username, is_superadmin FROM users ORDER BY id DESC LIMIT 10"
    ).fetchall()
    recent_audit = conn.execute("""
        SELECT al.ts, al.actor, al.action, al.team, al.player,
               al.details, l.name AS league_name
        FROM audit_log al
        LEFT JOIN leagues l ON al.league_id = l.id
        ORDER BY al.id DESC LIMIT 20
    """).fetchall()
    conn.close()

    # Survivor leagues for admin
    survivor_leagues_list = []
    try:
        from survivor_db import get_connection as _surv_conn
        import sqlite3 as _sq
        sconn = _surv_conn()
        mconn = _sq.connect(os.environ.get("DB_PATH", "data/fantasy.db"))
        mconn.row_factory = _sq.Row
        srows = sconn.execute("""
            SELECT l.id, l.name, l.created_at, l.invite_code,
                   l.commissioner_id, l.season, l.current_week,
                   COUNT(DISTINCT lm.user_id) AS member_count,
                   COUNT(DISTINCT t.id)       AS team_count,
                   COUNT(DISTINCT p.id)       AS player_count
            FROM survivor_leagues l
            LEFT JOIN survivor_league_members lm ON lm.league_id = l.id
            LEFT JOIN survivor_teams t           ON t.league_id  = l.id
            LEFT JOIN survivor_players p         ON p.league_id  = l.id
            GROUP BY l.id ORDER BY l.created_at DESC
        """).fetchall()
        for row in srows:
            row = dict(row)
            u = mconn.execute("SELECT username FROM users WHERE id=?", (row["commissioner_id"],)).fetchone()
            row["commissioner"] = u["username"] if u else "?"
            survivor_leagues_list.append(row)

        # Also fetch survivor teams with owner emails
        survivor_teams_list = []
        steam_rows = sconn.execute("""
            SELECT t.id, t.name, t.owner_id, t.league_id, t.paid, t.payment_date,
                   l.name as league_name, l.entry_fee
            FROM survivor_teams t
            JOIN survivor_leagues l ON l.id = t.league_id
            ORDER BY l.id, t.name
        """).fetchall()
        for tr in steam_rows:
            tr = dict(tr)
            u = mconn.execute("SELECT username, email FROM users WHERE id=?", (tr["owner_id"],)).fetchone()
            tr["username"] = u["username"] if u else "?"
            tr["email"] = u["email"] if u else ""
            survivor_teams_list.append(tr)
        sconn.close()
        mconn.close()
    except Exception as e:
        print(f"[admin] survivor leagues error: {e}")
        survivor_teams_list = []
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user,
        "total_users": total_users, "total_leagues": total_leagues, "playoff_leagues": playoff_leagues, "survivor_leagues_count": survivor_leagues_count, "survivor_leagues_list": survivor_leagues_list,
        "survivor_teams_list": survivor_teams_list,
        "total_teams": total_teams, "total_players": total_players,
        "total_picks": total_picks, "total_ponies": total_ponies,
        "total_scores": total_scores, "total_audits": total_audits,
        "drafts_complete": drafts_complete, "drafts_active": drafts_active,
        "users": [dict(r) for r in users], "leagues": [dict(r) for r in leagues],
        "recent_users": [dict(r) for r in recent_users],
        "recent_audit": [dict(r) for r in recent_audit],
        "msg": request.query_params.get("msg", ""),
        "error": request.query_params.get("error", ""),
    })


@app.post("/admin/user/delete")
def admin_delete_user(user_id: int = Form(...), user=Depends(get_current_user)):
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403)
    conn = get_db()
    target = conn.execute(adapt_sql("SELECT * FROM users WHERE id=?"), (user_id,)).fetchone()
    if not target:
        conn.close()
        return RedirectResponse("/admin?error=user_not_found", status_code=303)
    if target["is_superadmin"]:
        conn.close()
        return RedirectResponse("/admin?error=cannot_delete_superadmin", status_code=303)
    if target["id"] == user["id"]:
        conn.close()
        return RedirectResponse("/admin?error=cannot_delete_self", status_code=303)
    username = target["username"]
    owned_leagues = conn.execute(
        adapt_sql("SELECT id FROM leagues WHERE commissioner_id=?"), (user_id,)
    ).fetchall()
    for league in owned_leagues:
        conn.execute(adapt_sql("DELETE FROM audit_log WHERE league_id=?"), (league["id"],))
        conn.execute(adapt_sql("DELETE FROM leagues WHERE id=?"), (league["id"],))
    conn.execute(adapt_sql("DELETE FROM league_members WHERE user_id=?"), (user_id,))
    conn.execute(adapt_sql("DELETE FROM users WHERE id=?"), (user_id,))
    conn.commit()
    conn.close()
    write_audit(actor=user["username"], action="ADMIN_USER_DELETE",
                details=f"Deleted user '{username}' (id={user_id})")
    return RedirectResponse("/admin?msg=user_deleted", status_code=303)


@app.post("/admin/league/delete")
def admin_delete_league(league_id: int = Form(...), user=Depends(get_current_user)):
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403)
    import sqlite3 as _sq, time as _time
    # Pause the sync scheduler during delete to avoid DB lock conflicts
    db_path = os.environ.get("DB_PATH", "data/fantasy.db")
    # Get league name first with a quick read connection
    rconn = _sq.connect(db_path, timeout=30)
    rconn.row_factory = _sq.Row
    league = rconn.execute("SELECT * FROM leagues WHERE id=?", (league_id,)).fetchone()
    rconn.close()
    if not league:
        return RedirectResponse("/admin?error=league_not_found", status_code=303)
    league_name = league["name"]
    # Retry delete up to 5 times if DB is locked
    last_err = None
    for attempt in range(15):
        try:
            conn = _sq.connect(db_path, timeout=60)
            conn.execute("PRAGMA busy_timeout=60000")
            conn.row_factory = _sq.Row
            conn.isolation_level = None
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("DELETE FROM team_roster WHERE team_id IN (SELECT id FROM teams WHERE league_id=?)", (league_id,))
            conn.execute("DELETE FROM teams WHERE league_id=?", (league_id,))
            conn.execute("DELETE FROM league_members WHERE league_id=?", (league_id,))
            conn.execute("DELETE FROM players WHERE league_id=?", (league_id,))
            conn.execute("DELETE FROM draft_state WHERE league_id=?", (league_id,))
            conn.execute("DELETE FROM draft_chat WHERE league_id=?", (league_id,))
            conn.execute("DELETE FROM player_scores WHERE player_id IN (SELECT id FROM players WHERE league_id=?)", (league_id,))
            conn.execute("DELETE FROM leagues WHERE id=?", (league_id,))
            conn.execute("COMMIT")
            conn.close()
            last_err = None
            break
        except _sq.OperationalError as e:
            last_err = e
            try: conn.execute("ROLLBACK"); conn.close()
            except: pass
            _time.sleep(2)
    if last_err:
        return RedirectResponse("/admin?error=delete_failed", status_code=303)
    write_audit(actor=user["username"], action="ADMIN_LEAGUE_DELETE",
                details=f"Deleted league '{league_name}' (id={league_id})")
    return RedirectResponse("/admin?msg=league_deleted", status_code=303)


# ======================================================
# TEAM: RENAME (owner self-service)
# ======================================================


@app.post("/league/{league_id}/manage/team/payment")
def manage_team_payment(
    league_id: int,
    team_id: int = Form(...),
    paid: int = Form(...),
    payment_note: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    from datetime import date
    payment_date = date.today().isoformat() if paid else None
    conn.execute(adapt_sql(
        "UPDATE teams SET paid=?, payment_date=?, payment_note=? WHERE id=? AND league_id=?"
    ), (paid, payment_date, payment_note.strip(), team_id, league_id))
    conn.commit()
    conn.close()
    write_audit(actor=user["username"], action="PAYMENT_UPDATE", league_id=league_id,
                details=f"Team {team_id} marked {'paid' if paid else 'unpaid'}")
    return RedirectResponse(f"/league/{league_id}/manage?msg=payment_updated", status_code=303)




@app.post("/league/{league_id}/manage/draft-order/randomize")
def draft_order_randomize(league_id: int, user=Depends(get_current_user)):
    if not user: raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user): raise HTTPException(status_code=403)
    import random
    teams = get_league_teams(league_id)
    random.shuffle(teams)
    order = ",".join(str(t["id"]) for t in teams)
    conn = get_db()
    conn.execute(adapt_sql("UPDATE leagues SET draft_order=? WHERE id=?"), (order, league_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_order_randomized#tab-settings", status_code=303)

@app.post("/league/{league_id}/manage/draft-order/clear")
def draft_order_clear(league_id: int, user=Depends(get_current_user)):
    if not user: raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user): raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(adapt_sql("UPDATE leagues SET draft_order=NULL WHERE id=?"), (league_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_order_cleared#tab-settings", status_code=303)

def update_draft_time(
    league_id: int,
    draft_start_time: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    # Store as UTC — the datetime-local input sends local time, store as-is
    conn.execute(adapt_sql(
        "UPDATE leagues SET draft_start_time=? WHERE id=?"
    ), (draft_start_time.strip() or None, league_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_time_updated", status_code=303)

@app.post("/league/{league_id}/manage/settings/payment")
def update_payment_settings(
    league_id: int,
    entry_fee: float = Form(0),
    payment_deadline: str = Form(""),
    venmo_handle: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(adapt_sql(
        "UPDATE leagues SET entry_fee=?, payment_deadline=?, venmo_handle=? WHERE id=?"
    ), (max(0, entry_fee), payment_deadline.strip() or None, venmo_handle.strip() or None, league_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=settings_updated", status_code=303)

@app.post("/league/{league_id}/team/{team_id}/rename")
def team_rename(
    league_id: int,
    team_id: int,
    team_name: str = Form(...),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    team_name = team_name.strip()
    if not team_name:
        return RedirectResponse(f"/league/{league_id}/team/{team_id}?error=empty_name", status_code=303)
    conn = get_db()
    team = conn.execute(adapt_sql(
        "SELECT * FROM teams WHERE id=? AND league_id=?"
    ), (team_id, league_id)).fetchone()
    if not team or (team["owner_id"] != user["id"] and not is_commissioner(league, user)):
        conn.close()
        raise HTTPException(status_code=403)
    conn.execute(adapt_sql(
        "UPDATE teams SET name=? WHERE id=? AND league_id=?"
    ), (team_name, team_id, league_id))
    conn.commit()
    conn.close()
    write_audit(actor=user["username"], action="TEAM_RENAME", league_id=league_id,
                team=team_name, details=f"team_id={team_id} new_name={team_name}")
    return RedirectResponse(f"/league/{league_id}/team/{team_id}?msg=team_renamed", status_code=303)


# ======================================================
# TEAM RENAME
# ======================================================


@app.post("/league/{league_id}/manage/team/payment")
def manage_team_payment(
    league_id: int,
    team_id: int = Form(...),
    paid: int = Form(...),
    payment_note: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    from datetime import date
    payment_date = date.today().isoformat() if paid else None
    conn.execute(adapt_sql(
        "UPDATE teams SET paid=?, payment_date=?, payment_note=? WHERE id=? AND league_id=?"
    ), (paid, payment_date, payment_note.strip(), team_id, league_id))
    conn.commit()
    conn.close()
    write_audit(actor=user["username"], action="PAYMENT_UPDATE", league_id=league_id,
                details=f"Team {team_id} marked {'paid' if paid else 'unpaid'}")
    return RedirectResponse(f"/league/{league_id}/manage?msg=payment_updated", status_code=303)




@app.post("/league/{league_id}/manage/draft-order/randomize")
def draft_order_randomize(league_id: int, user=Depends(get_current_user)):
    if not user: raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user): raise HTTPException(status_code=403)
    import random
    teams = get_league_teams(league_id)
    random.shuffle(teams)
    order = ",".join(str(t["id"]) for t in teams)
    conn = get_db()
    conn.execute(adapt_sql("UPDATE leagues SET draft_order=? WHERE id=?"), (order, league_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_order_randomized#tab-settings", status_code=303)

@app.post("/league/{league_id}/manage/draft-order/clear")
def draft_order_clear(league_id: int, user=Depends(get_current_user)):
    if not user: raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user): raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(adapt_sql("UPDATE leagues SET draft_order=NULL WHERE id=?"), (league_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_order_cleared#tab-settings", status_code=303)


@app.post("/league/{league_id}/manage/draft-order/set")
async def draft_order_set(league_id: int, request: Request, user=Depends(get_current_user)):
    if not user: raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user): raise HTTPException(status_code=403)
    form = await request.form()
    team_ids = form.getlist("team_order")
    if team_ids:
        order = ",".join(team_ids)
        conn = get_db()
        conn.execute(adapt_sql("UPDATE leagues SET draft_order=? WHERE id=?"), (order, league_id))
        conn.commit()
        conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_order_saved", status_code=303)

@app.post("/league/{league_id}/manage/settings/draft-time")
def update_draft_time(
    league_id: int,
    draft_start_time: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    # Store as UTC — the datetime-local input sends local time, store as-is
    conn.execute(adapt_sql(
        "UPDATE leagues SET draft_start_time=? WHERE id=?"
    ), (draft_start_time.strip() or None, league_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=draft_time_updated", status_code=303)

@app.post("/league/{league_id}/manage/settings/payment")
def update_payment_settings(
    league_id: int,
    entry_fee: float = Form(0),
    payment_deadline: str = Form(""),
    venmo_handle: str = Form(""),
    user=Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401)
    league = league_ctx(league_id, user)
    if not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(adapt_sql(
        "UPDATE leagues SET entry_fee=?, payment_deadline=?, venmo_handle=? WHERE id=?"
    ), (max(0, entry_fee), payment_deadline.strip() or None, venmo_handle.strip() or None, league_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/league/{league_id}/manage?msg=settings_updated", status_code=303)

@app.post("/league/{league_id}/team/{team_id}/rename")
def rename_team(
    league_id: int, team_id: int,
    team_name: str = Form(...),
    user=Depends(get_current_user)
):
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    conn = get_db()
    team = conn.execute(adapt_sql(
        "SELECT * FROM teams WHERE id=? AND league_id=?"
    ), (team_id, league_id)).fetchone()
    conn.close()
    if not team:
        raise HTTPException(status_code=404)
    if user["id"] != team["owner_id"] and not is_admin_or_commissioner(league, user):
        raise HTTPException(status_code=403)
    name = team_name.strip()[:40]
    if not name:
        return RedirectResponse(f"/league/{league_id}/team/{team_id}?error=empty_name", status_code=303)
    conn = get_db()
    try:
        conn.execute(adapt_sql(
            "UPDATE teams SET name=? WHERE id=? AND league_id=?"
        ), (name, team_id, league_id))
        conn.commit()
    except Exception:
        conn.close()
        return RedirectResponse(f"/league/{league_id}/team/{team_id}?error=name_taken", status_code=303)
    conn.close()
    write_audit(actor=user["username"], action="TEAM_RENAME", league_id=league_id,
                team=name, details=f"Renamed from '{team['name']}' to '{name}'")
    return RedirectResponse(f"/league/{league_id}/team/{team_id}?msg=team_renamed", status_code=303)


@app.post("/admin/survivor/league/delete")
def admin_delete_survivor_league(league_id: int = Form(...), user=Depends(get_current_user)):
    if not user or not user["is_superadmin"]:
        raise HTTPException(status_code=403)
    import sqlite3 as _sq2, time as _time2
    surv_db_path = os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db")
    # Read league name first
    rconn2 = _sq2.connect(surv_db_path, timeout=30)
    rconn2.row_factory = _sq2.Row
    league = rconn2.execute("SELECT * FROM survivor_leagues WHERE id=?", (league_id,)).fetchone()
    rconn2.close()
    if not league:
        return RedirectResponse("/admin?error=league_not_found", status_code=303)
    league_name = league["name"]
    last_err2 = None
    for attempt in range(15):
        try:
            sconn = _sq2.connect(surv_db_path, timeout=60)
            sconn.execute("PRAGMA journal_mode=WAL")
            sconn.execute("PRAGMA busy_timeout=60000")
            sconn.row_factory = _sq2.Row
            sconn.execute("DELETE FROM survivor_lineups WHERE league_id=?", (league_id,))
            sconn.execute("DELETE FROM survivor_teams WHERE league_id=?", (league_id,))
            sconn.execute("DELETE FROM survivor_league_members WHERE league_id=?", (league_id,))
            sconn.execute("DELETE FROM survivor_players WHERE league_id=?", (league_id,))
            sconn.execute("DELETE FROM survivor_player_scores WHERE player_id IN (SELECT id FROM survivor_players WHERE league_id=?)", (league_id,))
            sconn.execute("DELETE FROM survivor_game_schedule WHERE league_id=?", (league_id,))
            sconn.execute("DELETE FROM survivor_leagues WHERE id=?", (league_id,))
            sconn.commit()
            sconn.close()
            last_err2 = None
            break
        except _sq2.OperationalError as e:
            last_err2 = e
            try: sconn.close()
            except: pass
            _time2.sleep(2)
    if last_err2:
        return RedirectResponse(f"/admin?error=delete_failed_{last_err2}", status_code=303)
    write_audit(actor=user["username"], action="ADMIN_SURVIVOR_LEAGUE_DELETE",
                details=f"Deleted survivor league '{league_name}' (id={league_id})")
    return RedirectResponse("/admin?msg=league_deleted", status_code=303)


@app.get("/research", response_class=HTMLResponse)
def research_page(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    season = int(os.environ.get("NFL_SEASON", "2026"))
    return templates.TemplateResponse("research.html", {
        "request": request,
        "user": user,
        "season": season,
    })

@app.get("/api/player-headshot")
def api_player_headshot(name: str, user=Depends(get_current_user)):
    """Return headshot URL from our DB for a player by name."""
    conn = get_db()
    row = conn.execute(
        "SELECT headshot_url FROM players WHERE LOWER(name)=LOWER(?) AND headshot_url IS NOT NULL LIMIT 1",
        (name,)
    ).fetchone()
    conn.close()
    if row:
        return {"headshot_url": row["headshot_url"]}
    return {"headshot_url": None}

@app.get("/api/player-headshot")
def api_player_headshot(name: str, user=Depends(get_current_user)):
    """Return headshot URL from our DB for a player by name."""
    conn = get_db()
    row = conn.execute(
        "SELECT headshot_url FROM players WHERE LOWER(name)=LOWER(?) AND headshot_url IS NOT NULL LIMIT 1",
        (name,)
    ).fetchone()
    conn.close()
    if row:
        return {"headshot_url": row["headshot_url"]}
    return {"headshot_url": None}
