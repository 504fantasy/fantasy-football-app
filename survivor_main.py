"""
survivor_main.py — 504 Fantasy Survivor
========================================
A FastAPI application that runs alongside (or independently of) the
existing 504 Fantasy playoff game.

Game rules
----------
• Every NFL regular-season week (1-18) each team submits a lineup of
  exactly 6 players: 1 QB, 1 RB, 1 WR, 1 TE, 1 DST, 1 K.
• Any player in the league pool can be selected, but once a team uses
  a player that player is LOCKED OUT for that team for the rest of the
  season (weeks cannot be re-used).
• Multiple teams may select the same player in the same week — no
  exclusivity.
• Fantasy points are calculated with the same scoring engine used in
  the main game (scoring.py → calculate_fantasy_points).
• There is no traditional draft.

Routes
------
  GET  /survivor/                           → public home / dashboard
  GET  /survivor/login                      → login page
  POST /survivor/login
  GET  /survivor/register
  POST /survivor/register
  GET  /survivor/logout
  GET  /survivor/dashboard
  POST /survivor/league/create
  POST /survivor/league/join
  GET  /survivor/{league_id}                → league home / standings
  POST /survivor/{league_id}/create-team
  GET  /survivor/{league_id}/lineup         → submit / edit this week's lineup
  POST /survivor/{league_id}/lineup/submit
  GET  /survivor/{league_id}/scores         → weekly & season scores
  GET  /survivor/{league_id}/manage         → commissioner panel
  POST /survivor/{league_id}/manage/settings
  POST /survivor/{league_id}/manage/advance-week
  POST /survivor/{league_id}/manage/player/add
  POST /survivor/{league_id}/manage/player/edit
  POST /survivor/{league_id}/manage/player/delete
  POST /survivor/{league_id}/manage/scores/entry
  POST /survivor/{league_id}/manage/sync/roster
  POST /survivor/{league_id}/manage/sync/week
  GET  /survivor/{league_id}/audit
  GET  /api/survivor/{league_id}/used-players   → JSON: players used by team
  GET  /api/survivor/{league_id}/week-scores    → JSON: scores for a week
"""

from dotenv import load_dotenv

load_dotenv()

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext

# Reuse scoring engine and NFL sync from the main game
from scoring import calculate_fantasy_points
from survivor_db import (
    NFL_REGULAR_SEASON_WEEKS,
    REQUIRED_POSITIONS,
    adapt_sql,
    execute_returning,
    get_db,
    init_db,
)

try:
    from nfl_sync import current_nfl_week
    from nfl_sync import seed_players as _seed_players
    from nfl_sync import sync_week as _sync_week
except ImportError:

    def _seed_players(*a, **kw):
        return {"added": 0, "skipped": 0}

    def _sync_week(*a, **kw):
        return {"updated": 0}

    def current_nfl_week():
        return 1


# ──────────────────────────────────────────────────────────────────────────────
# Shared auth — reuse main app JWT config and users table
# Survivor shares the main app "session" cookie and "users" table.
# One account works for both games — no separate survivor registration.
# ──────────────────────────────────────────────────────────────────────────────

JWT_SECRET = os.environ.get(
    "JWT_SECRET", "CHANGE_ME_in_production_use_a_long_random_string"
)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "72"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def decode_access_token(token: str) -> str | None:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return data.get("sub")
    except JWTError:
        return None


def hash_password(pw: str) -> str:
    # bcrypt limit is 72 bytes — truncate to be safe
    return pwd_context.hash(pw[:72])


def verify_password(pw: str, hashed: str) -> bool:
    return pwd_context.verify(pw[:72], hashed)


# ──────────────────────────────────────────────────────────────────────────────
# DB bootstrap
# ──────────────────────────────────────────────────────────────────────────────

try:
    init_db()
    print("[survivor] survivor_db initialized OK")
except Exception as _e:
    import traceback as _tb

    print(f"[survivor] ERROR: init_db() failed: {_e}")
    _tb.print_exc()

# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI()

# ── Background schedule auto-sync (once per day) ──────────────────────────
import threading as _threading

def _auto_sync_schedules():
    """Refresh game schedules for all active leagues once per day."""
    import time as _time
    while True:
        try:
            conn = get_db()
            leagues = conn.execute(
                "SELECT id, season FROM survivor_leagues WHERE is_active=1"
            ).fetchall()
            conn.close()
            for league in leagues:
                try:
                    seed_game_schedule(league["id"], league["season"])
                    print(f"[survivor] Auto-synced schedule for league {league['id']}")
                except Exception as e:
                    print(f"[survivor] Auto-sync error league {league['id']}: {e}")
        except Exception as e:
            print(f"[survivor] Auto-sync thread error: {e}")
        _time.sleep(86400)  # 24 hours

_sync_thread = _threading.Thread(target=_auto_sync_schedules, daemon=True)
_sync_thread.start()
templates = Jinja2Templates(directory="survivor_templates")

# ──────────────────────────────────────────────────────────────────────────────
# Current-user helper
# ──────────────────────────────────────────────────────────────────────────────


def _get_main_db():
    """Open a connection to the main game's database (users table lives here)."""
    import sqlite3 as _sq

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return get_db()  # Postgres: same DATABASE_URL covers both
    main_db_path = os.environ.get("DB_PATH", "data/fantasy.db")
    conn = _sq.connect(main_db_path)
    conn.row_factory = _sq.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_current_user(request: Request):
    """
    Read the main app 'session' cookie — falls back to 'survivor_session'
    for anyone still holding an old cookie. Looks up in the shared 'users' table.
    """
    token = request.cookies.get("session") or request.cookies.get("survivor_session")
    if not token:
        return None
    username = decode_access_token(token)
    if not username:
        return None
    try:
        conn = _get_main_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[survivor] get_current_user error: {e}")
        return None


def require_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


# ──────────────────────────────────────────────────────────────────────────────
# Audit log
# ──────────────────────────────────────────────────────────────────────────────


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
        "INSERT INTO survivor_audit_log (league_id, ts, actor, action, team, player, details) "
        "VALUES (?,?,?,?,?,?,?)",
        (league_id, ts, actor, action, team, player, details),
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# User helpers
# ──────────────────────────────────────────────────────────────────────────────


def get_user(username: str):
    """Look up user in the main app's shared users table."""
    try:
        conn = _get_main_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[survivor] get_user error: {e}")
        return None


def get_user_by_id(uid: int):
    """Look up user by id in the main app's shared users table."""
    try:
        conn = _get_main_db()
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[survivor] get_user_by_id error: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# League helpers
# ──────────────────────────────────────────────────────────────────────────────


def _gen_invite() -> str:
    import secrets
    import string

    return "".join(
        secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8)
    )


def create_league(name: str, commissioner_id: int, season: int) -> int:
    invite = _gen_invite()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    lid = execute_returning(
        conn,
        "INSERT INTO survivor_leagues (name, commissioner_id, invite_code, created_at, season) "
        "VALUES (?,?,?,?,?)",
        (name, commissioner_id, invite, ts, season),
    )
    conn.execute(
        adapt_sql(
            "INSERT INTO survivor_league_members (league_id, user_id, joined_at) "
            "VALUES (?,?,?) ON CONFLICT DO NOTHING"
        ),
        (lid, commissioner_id, ts),
    )
    conn.commit()
    conn.close()
    return lid


def get_league(league_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM survivor_leagues WHERE id=?", (league_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_leagues(user_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT l.*, lm.joined_at, (l.commissioner_id = ?) as is_commissioner
        FROM survivor_leagues l
        JOIN survivor_league_members lm ON lm.league_id = l.id
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
        "SELECT * FROM survivor_leagues WHERE invite_code=?",
        (invite_code.strip().upper(),),
    ).fetchone()
    if not league:
        conn.close()
        return None, "Invalid invite code."
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO survivor_league_members (league_id, user_id, joined_at) VALUES (?,?,?)",
            (league["id"], user_id, ts),
        )
        conn.commit()
    except Exception:
        pass  # already a member
    conn.close()
    return league["id"], None


def is_league_member(league_id: int, user_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM survivor_league_members WHERE league_id=? AND user_id=?",
        (league_id, user_id),
    ).fetchone()
    conn.close()
    return row is not None


def is_commissioner(league: dict, user: dict) -> bool:
    return bool(user.get("is_superadmin")) or league["commissioner_id"] == user["id"]


def league_ctx(league_id: int, user: dict) -> dict:
    league = get_league(league_id)
    if not league:
        raise HTTPException(status_code=404, detail="League not found")
    if not is_league_member(league_id, user["id"]) and not user.get("is_superadmin"):
        raise HTTPException(status_code=403, detail="Not a member of this league")
    return league


# ──────────────────────────────────────────────────────────────────────────────
# Team helpers
# ──────────────────────────────────────────────────────────────────────────────


def get_league_teams(league_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM survivor_teams WHERE league_id=? ORDER BY id", (league_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_team_in_league(league_id: int, user_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM survivor_teams WHERE league_id=? AND owner_id=?",
        (league_id, user_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ──────────────────────────────────────────────────────────────────────────────
# Player helpers
# ──────────────────────────────────────────────────────────────────────────────


def get_league_players(league_id: int) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM survivor_players WHERE league_id=? ORDER BY position, name",
        (league_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_players_by_position(league_id: int, position: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM survivor_players WHERE league_id=? AND position=? ORDER BY name",
        (league_id, position.upper()),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_used_player_ids_for_team(team_id: int, exclude_week: int = None) -> set:
    """
    Returns the set of player_ids this team has already used in any
    PREVIOUS week (locked lineups). If exclude_week is given, that
    week's picks are excluded so a team can edit their current lineup.
    """
    conn = get_db()
    if exclude_week is not None:
        rows = conn.execute(
            "SELECT DISTINCT player_id FROM survivor_lineups "
            "WHERE team_id=? AND locked=1 AND week != ?",
            (team_id, exclude_week),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT player_id FROM survivor_lineups "
            "WHERE team_id=? AND locked=1",
            (team_id,),
        ).fetchall()
    conn.close()
    return {r["player_id"] for r in rows}


def get_available_players_for_team(league_id: int, team_id: int, week: int) -> dict:
    """
    Returns {position: [player, ...]} for players NOT yet used by this
    team in any prior locked week. Each player dict includes:
      - kicked_off: True if their game has started (player locked)
      - bye_week: True if team has no game this week
    """
    from datetime import datetime, timezone
    used = get_used_player_ids_for_team(team_id, exclude_week=week)
    all_players = get_league_players(league_id)

    # Cache kickoff times for this week to avoid repeated DB queries
    conn = get_db()
    sched_rows = conn.execute(
        "SELECT team, kickoff_utc FROM survivor_game_schedule WHERE league_id=? AND week=?",
        (league_id, week)
    ).fetchall()
    conn.close()
    kickoff_map = {r["team"].upper(): r["kickoff_utc"] for r in sched_rows}
    now_utc = datetime.now(timezone.utc)

    by_pos: dict = {}
    for p in all_players:
        if p["id"] in used:
            continue
        team = (p["nfl_team"] or "").upper()
        kickoff_str = kickoff_map.get(team)
        kicked_off = False
        bye_week   = kickoff_str is None
        if kickoff_str:
            try:
                kt = datetime.fromisoformat(kickoff_str).replace(tzinfo=timezone.utc)
                kicked_off = now_utc >= kt
            except Exception:
                pass
        player = dict(p)
        player["kicked_off"] = kicked_off
        player["bye_week"]   = bye_week
        by_pos.setdefault(p["position"], []).append(player)
    return by_pos


# ──────────────────────────────────────────────────────────────────────────────
# Lineup helpers
# ──────────────────────────────────────────────────────────────────────────────


def get_team_lineup(team_id: int, week: int) -> list:
    """Return the lineup rows for a team/week."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT sl.*, p.name as player_name, p.nfl_team, p.position as player_pos
        FROM survivor_lineups sl
        JOIN survivor_players p ON sl.player_id = p.id
        WHERE sl.team_id=? AND sl.week=?
        ORDER BY sl.position
    """,
        (team_id, week),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def lineup_is_complete(lineup: list) -> bool:
    submitted = {row["position"] for row in lineup}
    return set(REQUIRED_POSITIONS) == submitted


def lineup_is_locked(lineup: list) -> bool:
    return all(row.get("locked") for row in lineup) if lineup else False


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────


def _row_to_stats(row: dict) -> dict:
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


def get_team_week_score(team_id: int, week: int) -> dict:
    """
    Score a team's lineup for a given week.
    Returns {players: [...], total: float}.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT sl.position, sl.locked,
               p.id as player_id, p.name, p.nfl_team, p.position as player_pos,
               ps.receptions, ps.receiving_yards, ps.rushing_yards,
               ps.return_yards, ps.passing_yards, ps.total_tds,
               ps.fumbles_lost, ps.interceptions, ps.field_goals_json,
               ps.return_fumbles_lost, ps.override_points, ps.override_note
        FROM survivor_lineups sl
        JOIN survivor_players p  ON sl.player_id = p.id
        LEFT JOIN survivor_player_scores ps
               ON ps.player_id = p.id AND ps.week = ?
        WHERE sl.team_id=? AND sl.week=?
    """,
        (week, team_id, week),
    ).fetchall()
    conn.close()

    players_out = []
    total = 0.0
    for r in rows:
        r = dict(r)
        pos = r.get("position", "").upper()  # lineup slot position
        if r.get("override_points") is not None:
            pts = float(r["override_points"])
        else:
            stats = _row_to_stats(r)
            pts = calculate_fantasy_points({"pos": pos, "multiplier": None}, stats)
        total += pts
        players_out.append({**r, "final_points": round(pts, 2)})

    return {"players": players_out, "total": round(total, 2)}


def get_team_season_score(team_id: int, through_week: int) -> float:
    return round(
        sum(
            get_team_week_score(team_id, w)["total"] for w in range(1, through_week + 1)
        ),
        2,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Seed players (adapts nfl_sync.seed_players to survivor_players table)
# ──────────────────────────────────────────────────────────────────────────────



def seed_game_schedule(league_id: int, season: int) -> dict:
    """Pull NFL schedule/kickoff times and store per team per week."""
    import nfl_data_py as nfl
    from datetime import datetime, timezone
    added = skipped = 0
    try:
        sched = nfl.import_schedules([season])
        conn = get_db()
        for _, row in sched.iterrows():
            week     = int(row.get("week", 0))
            gameday  = str(row.get("gameday", "") or "")
            gametime = str(row.get("gametime", "") or "")
            if not gameday or not gametime or not week:
                continue
            # Parse kickoff as UTC (gametime is Eastern — add 5h for UTC)
            try:
                naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
                # Convert ET to UTC (approximate — doesn't handle DST perfectly)
                from datetime import timedelta
                utc_dt = naive + timedelta(hours=5)
                kickoff_utc = utc_dt.isoformat()
            except Exception:
                continue
            for team_col in ["away_team", "home_team"]:
                team = str(row.get(team_col, "") or "").upper().strip()
                if not team:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO survivor_game_schedule (league_id, season, week, team, kickoff_utc) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(league_id, week, team) DO UPDATE SET kickoff_utc=excluded.kickoff_utc",
                        (league_id, season, week, team, kickoff_utc)
                    )
                    added += 1
                except Exception:
                    skipped += 1
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[survivor] seed_game_schedule error: {e}")
    return {"added": added, "skipped": skipped}


def get_team_kickoff_utc(league_id: int, week: int, team: str) -> str | None:
    """Return kickoff UTC ISO string for a team in a given week, or None if bye/not found."""
    conn = get_db()
    row = conn.execute(
        "SELECT kickoff_utc FROM survivor_game_schedule WHERE league_id=? AND week=? AND team=?",
        (league_id, week, team.upper())
    ).fetchone()
    conn.close()
    return row["kickoff_utc"] if row else None


def team_has_kicked_off(league_id: int, week: int, team: str) -> bool:
    """Return True if the team's game has already started."""
    from datetime import datetime, timezone
    kickoff = get_team_kickoff_utc(league_id, week, team)
    if not kickoff:
        return False
    try:
        kt = datetime.fromisoformat(kickoff).replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= kt
    except Exception:
        return False


def team_has_bye(league_id: int, week: int, team: str) -> bool:
    """Return True if team has no game this week (bye)."""
    return get_team_kickoff_utc(league_id, week, team) is None

def seed_survivor_players(league_id: int, overwrite: bool = False) -> dict:
    """
    Seed survivor_players directly from nfl-data-py (same source as playoff challenge).
    Also adds all 32 NFL team DSTs.
    """
    added = skipped = 0
    try:
        import nfl_data_py as nfl
        import os as _os
        season = int(_os.environ.get("NFL_SEASON", "2024"))
        roster = nfl.import_seasonal_rosters([season])
        VALID_POS = {"QB", "RB", "WR", "TE", "K"}
        TEAM_MAP  = {"LA": "LAR", "LV": "LAS", "JAC": "JAX"}
        conn = get_db()
        for _, row in roster.iterrows():
            pos = str(row.get("position", "") or "").upper().strip()
            if pos not in VALID_POS:
                continue
            name    = str(row.get("player_name") or row.get("full_name") or "").strip()
            team    = str(row.get("team") or "").upper().strip()
            team    = TEAM_MAP.get(team, team)
            if not name or not team:
                continue
            try:
                conn.execute(
                    "INSERT INTO survivor_players (league_id, name, position, nfl_team) VALUES (?,?,?,?)",
                    (league_id, name, pos, team)
                )
                added += 1
            except Exception:
                skipped += 1
        # Add all 32 DSTs
        dst_teams = [
            ("Arizona Cardinals DST","ARI"),("Atlanta Falcons DST","ATL"),
            ("Baltimore Ravens DST","BAL"),("Buffalo Bills DST","BUF"),
            ("Carolina Panthers DST","CAR"),("Chicago Bears DST","CHI"),
            ("Cincinnati Bengals DST","CIN"),("Cleveland Browns DST","CLE"),
            ("Dallas Cowboys DST","DAL"),("Denver Broncos DST","DEN"),
            ("Detroit Lions DST","DET"),("Green Bay Packers DST","GB"),
            ("Houston Texans DST","HOU"),("Indianapolis Colts DST","IND"),
            ("Jacksonville Jaguars DST","JAX"),("Kansas City Chiefs DST","KC"),
            ("Las Vegas Raiders DST","LV"),("Los Angeles Chargers DST","LAC"),
            ("Los Angeles Rams DST","LAR"),("Miami Dolphins DST","MIA"),
            ("Minnesota Vikings DST","MIN"),("New England Patriots DST","NE"),
            ("New Orleans Saints DST","NO"),("New York Giants DST","NYG"),
            ("New York Jets DST","NYJ"),("Philadelphia Eagles DST","PHI"),
            ("Pittsburgh Steelers DST","PIT"),("San Francisco 49ers DST","SF"),
            ("Seattle Seahawks DST","SEA"),("Tampa Bay Buccaneers DST","TB"),
            ("Tennessee Titans DST","TEN"),("Washington Commanders DST","WAS"),
        ]
        for name, team in dst_teams:
            try:
                conn.execute(
                    "INSERT INTO survivor_players (league_id, name, position, nfl_team) VALUES (?,?,?,?)",
                    (league_id, name, "DST", team)
                )
                added += 1
            except Exception:
                skipped += 1
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[survivor] seed_survivor_players error: {e}")
    return {"added": added, "skipped": skipped}



# ──────────────────────────────────────────────────────────────────────────────
# AUTH ROUTES
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/", response_class=HTMLResponse)
def survivor_root(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login?next=/survivor/", status_code=303)
    leagues = get_user_leagues(user["id"])
    if len(leagues) == 1:
        return RedirectResponse(f"/survivor/{leagues[0]['id']}", status_code=303)
    return RedirectResponse("/survivor/dashboard", status_code=303)


@app.get("/survivor/login", response_class=HTMLResponse)
def login_page(request: Request):
    # Redirect to main app login — one account works for both games
    return RedirectResponse("/login", status_code=303)


@app.post("/survivor/login")
def login_post():
    return RedirectResponse("/login", status_code=303)


@app.get("/survivor/register", response_class=HTMLResponse)
def register_page(request: Request):
    # Redirect to main app register — one account works for both games
    return RedirectResponse("/register", status_code=303)


@app.post("/survivor/register")
def register_post():
    return RedirectResponse("/register", status_code=303)


@app.get("/survivor/logout")
def logout():
    resp = RedirectResponse("/logout", status_code=303)
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    leagues = get_user_leagues(user["id"])
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "leagues": leagues,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/survivor/league/create")
def league_create(
    request: Request, league_name: str = Form(...), season: int = Form(2025)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not league_name.strip():
        return RedirectResponse("/survivor/dashboard?error=empty_name", status_code=303)
    lid = create_league(league_name.strip(), user["id"], season)
    write_audit(
        actor=user["username"],
        action="LEAGUE_CREATE",
        league_id=lid,
        details=f"name={league_name.strip()} season={season}",
    )
    # Auto-seed players and schedule from NFL data on league creation
    try:
        result = seed_survivor_players(lid, overwrite=False)
        print(f"[survivor] Auto-seeded league {lid}: {result}")
    except Exception as e:
        print(f"[survivor] Auto-seed failed for league {lid}: {e}")
    try:
        sched_result = seed_game_schedule(lid, season)
        print(f"[survivor] Schedule seeded league {lid}: {sched_result}")
    except Exception as e:
        print(f"[survivor] Schedule seed failed for league {lid}: {e}")
    return RedirectResponse(f"/survivor/{lid}?msg=league_created", status_code=303)


@app.post("/survivor/league/join")
def league_join(request: Request, invite_code: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    lid, err = join_league_by_code(user["id"], invite_code)
    if err:
        return RedirectResponse(f"/survivor/dashboard?error={err}", status_code=303)
    write_audit(actor=user["username"], action="LEAGUE_JOIN", league_id=lid)
    return RedirectResponse(f"/survivor/{lid}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# LEAGUE HOME
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/{league_id}", response_class=HTMLResponse)
def league_home(league_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    teams = get_league_teams(league_id)
    current_wk = league["current_week"]
    my_team = get_user_team_in_league(league_id, user["id"])

    standings = []
    for team in teams:
        season_pts = get_team_season_score(team["id"], current_wk)
        week_pts = get_team_week_score(team["id"], current_wk)["total"]
        lineup = get_team_lineup(team["id"], current_wk)
        standings.append(
            {
                "team": team,
                "owner": get_user_by_id(team["owner_id"]),
                "season_pts": season_pts,
                "week_pts": week_pts,
                "lineup_complete": lineup_is_complete(lineup),
                "lineup_locked": lineup_is_locked(lineup),
            }
        )
    standings.sort(key=lambda x: x["season_pts"], reverse=True)

    return templates.TemplateResponse(
        "league_home.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "standings": standings,
            "current_week": current_wk,
            "total_weeks": NFL_REGULAR_SEASON_WEEKS,
            "my_team": my_team,
            "is_commissioner": is_commissioner(league, user),
            "msg": request.query_params.get("msg", ""),
            "required_positions": REQUIRED_POSITIONS,
        },
    )


@app.post("/survivor/{league_id}/create-team")
def create_team(league_id: int, request: Request, team_name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league_ctx(league_id, user)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO survivor_teams (league_id, name, owner_id) VALUES (?,?,?)",
            (league_id, team_name.strip(), user["id"]),
        )
        conn.commit()
    except Exception:
        conn.close()
        return RedirectResponse(
            f"/survivor/{league_id}?error=team_name_taken", status_code=303
        )
    conn.close()
    write_audit(
        actor=user["username"],
        action="TEAM_CREATE",
        league_id=league_id,
        team=team_name.strip(),
    )
    return RedirectResponse(f"/survivor/{league_id}?msg=team_created", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# LINEUP PAGE  (submit / edit weekly picks)
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/{league_id}/lineup", response_class=HTMLResponse)
def lineup_page(league_id: int, request: Request, week: int = None):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    my_team = get_user_team_in_league(league_id, user["id"])
    if not my_team:
        return RedirectResponse(f"/survivor/{league_id}?error=no_team", status_code=303)

    current_wk = league["current_week"]
    if week is None:
        week = current_wk

    # Only allow editing the current week (commissioners can edit any week)
    is_comm = is_commissioner(league, user)
    editable = (week == current_wk) or is_comm

    current_lineup = get_team_lineup(my_team["id"], week)
    locked = lineup_is_locked(current_lineup)
    available_by_pos = get_available_players_for_team(league_id, my_team["id"], week)
    used_ids = get_used_player_ids_for_team(my_team["id"], exclude_week=week)

    # Map current picks for the template
    current_picks = {row["position"]: row for row in current_lineup}

    # Build full pool by position (for pool-usage bars)
    all_players = get_league_players(league_id)
    all_players_by_pos: dict = {}
    for p in all_players:
        all_players_by_pos.setdefault(p["position"], []).append(p)

    # Build used-players list with the week they were used (from locked lineups)
    conn = get_db()
    used_rows = conn.execute(
        """
        SELECT DISTINCT sl.player_id, sl.week as used_week,
               p.name, p.position, p.nfl_team
        FROM survivor_lineups sl
        JOIN survivor_players p ON sl.player_id = p.id
        WHERE sl.team_id=? AND sl.locked=1 AND sl.week != ?
        ORDER BY sl.week
    """,
        (my_team["id"], week),
    ).fetchall()
    conn.close()
    used_players = [dict(r) for r in used_rows]

    return templates.TemplateResponse(
        "lineup.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "my_team": my_team,
            "week": week,
            "current_week": current_wk,
            "total_weeks": NFL_REGULAR_SEASON_WEEKS,
            "current_lineup": current_lineup,
            "current_picks": current_picks,
            "locked": locked,
            "editable": editable and not locked,
            "available_by_pos": available_by_pos,
            "all_players_by_pos": all_players_by_pos,
            "used_ids": used_ids,
            "used_players": used_players,
            "required_positions": REQUIRED_POSITIONS,
            "lineup_complete": lineup_is_complete(current_lineup),
            "is_commissioner": is_comm,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/survivor/{league_id}/lineup/submit")
def lineup_submit(
    league_id: int,
    request: Request,
    week: int = Form(...),
    qb: int = Form(...),
    rb: int = Form(...),
    wr: int = Form(...),
    te: int = Form(...),
    dst: int = Form(...),
    k: int = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    my_team = get_user_team_in_league(league_id, user["id"])
    if not my_team:
        return RedirectResponse(f"/survivor/{league_id}?error=no_team", status_code=303)

    is_comm = is_commissioner(league, user)
    current_wk = league["current_week"]

    # Non-commissioners can only submit the current week
    if week != current_wk and not is_comm:
        return RedirectResponse(
            f"/survivor/{league_id}/lineup?error=wrong_week", status_code=303
        )

    picks = {"QB": qb, "RB": rb, "WR": wr, "TE": te, "DST": dst, "K": k}
    base = f"/survivor/{league_id}/lineup?week={week}"
    from datetime import datetime, timezone as _tz
    now_utc = datetime.now(_tz.utc)

    # Load kickoff times for this week
    _sconn = get_db()
    sched_rows = _sconn.execute(
        "SELECT team, kickoff_utc FROM survivor_game_schedule WHERE league_id=? AND week=?",
        (league_id, week)
    ).fetchall()
    _sconn.close()
    kickoff_map = {r["team"].upper(): r["kickoff_utc"] for r in sched_rows}

    def _has_kicked_off(nfl_team: str) -> bool:
        k = kickoff_map.get(nfl_team.upper())
        if not k:
            return False
        try:
            return now_utc >= datetime.fromisoformat(k).replace(tzinfo=_tz.utc)
        except Exception:
            return False

    conn = get_db()
    used = get_used_player_ids_for_team(my_team["id"], exclude_week=week)
    for pos, pid in picks.items():
        row = conn.execute(
            "SELECT * FROM survivor_players WHERE id=? AND league_id=?",
            (pid, league_id),
        ).fetchone()
        if not row:
            conn.close()
            return RedirectResponse(f"{base}&error=invalid_player_{pos}", status_code=303)
        if row["position"].upper() != pos:
            conn.close()
            return RedirectResponse(f"{base}&error=wrong_position_{pos}", status_code=303)
        if pid in used:
            conn.close()
            return RedirectResponse(f"{base}&error=player_already_used_{pos}", status_code=303)
        if _has_kicked_off(row["nfl_team"] or ""):
            conn.close()
            return RedirectResponse(f"{base}&error=game_started_{pos}", status_code=303)
    if len(set(picks.values())) != len(picks):
        conn.close()
        return RedirectResponse(f"{base}&error=duplicate_players", status_code=303)
    ts = datetime.now(timezone.utc).isoformat()
    for pos, pid in picks.items():
        prow = conn.execute("SELECT nfl_team FROM survivor_players WHERE id=?", (pid,)).fetchone()
        auto_locked = 1 if (prow and _has_kicked_off(prow["nfl_team"] or "")) else 0
        conn.execute(
            adapt_sql("""
            INSERT INTO survivor_lineups
                (league_id, team_id, week, position, player_id, locked, submitted_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(team_id, week, position)
            DO UPDATE SET player_id=excluded.player_id,
                          submitted_at=excluded.submitted_at,
                          locked=excluded.locked
            """),
            (league_id, my_team["id"], week, pos, pid, auto_locked, ts),
        )
    conn.commit()
    conn.close()

    conn.close()

    write_audit(
        actor=user["username"],
        action="LINEUP_SUBMIT",
        league_id=league_id,
        team=my_team["name"],
        details=f"week={week} QB={qb} RB={rb} WR={wr} TE={te} DST={dst} K={k}",
    )
    return RedirectResponse(f"{base}&msg=lineup_saved", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# SCORES PAGE
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/{league_id}/scores", response_class=HTMLResponse)
def scores_page(league_id: int, request: Request, week: int = None):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)

    current_wk = league["current_week"]
    if week is None:
        week = current_wk

    teams = get_league_teams(league_id)

    # Per-week scores for display
    week_scores = []
    for team in teams:
        result = get_team_week_score(team["id"], week)
        owner = get_user_by_id(team["owner_id"])
        lineup = get_team_lineup(team["id"], week)
        week_scores.append(
            {
                "team": team,
                "owner": owner,
                "players": result["players"],
                "total": result["total"],
                "complete": lineup_is_complete(lineup),
            }
        )
    week_scores.sort(key=lambda x: x["total"], reverse=True)

    # Season standings
    standings = []
    for team in teams:
        season_pts = get_team_season_score(team["id"], current_wk)
        standings.append(
            {
                "team": team,
                "owner": get_user_by_id(team["owner_id"]),
                "season_pts": season_pts,
            }
        )
    standings.sort(key=lambda x: x["season_pts"], reverse=True)

    return templates.TemplateResponse(
        "scores.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "week": week,
            "current_week": current_wk,
            "total_weeks": NFL_REGULAR_SEASON_WEEKS,
            "week_scores": week_scores,
            "standings": standings,
            "is_commissioner": is_commissioner(league, user),
            "my_team": get_user_team_in_league(league_id, user["id"]),
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# COMMISSIONER PANEL
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/{league_id}/manage", response_class=HTMLResponse)
def manage_page(league_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403, detail="Commissioner only")

    conn = get_db()
    # members query needs main DB for users table
    _mconn = _get_main_db()
    member_ids = conn.execute(
        "SELECT user_id, joined_at FROM survivor_league_members WHERE league_id=?",
        (league_id,)
    ).fetchall()
    members = []
    for m in member_ids:
        u = _mconn.execute("SELECT id, username FROM users WHERE id=?", (m["user_id"],)).fetchone()
        if u:
            members.append({"id": u["id"], "username": u["username"], "joined_at": m["joined_at"]})
    _mconn.close()
    current_wk = league["current_week"]
    week_scores = {
        r["player_id"]: dict(r)
        for r in conn.execute(
            """
            SELECT ps.* FROM survivor_player_scores ps
            JOIN survivor_players p ON ps.player_id = p.id
            WHERE p.league_id=? AND ps.week=?
        """,
            (league_id, current_wk),
        ).fetchall()
    }
    conn.close()

    teams = get_league_teams(league_id)
    players = get_league_players(league_id)
    teams_with_owners = [
        {**t, "owner_name": (get_user_by_id(t["owner_id"]) or {}).get("username", "?")}
        for t in teams
    ]

    # Lineup status for each team this week
    lineup_status: dict = {}
    team_lineups: dict = {}
    for t in teams:
        lu = get_team_lineup(t["id"], current_wk)
        lineup_status[t["id"]] = {
            "complete": lineup_is_complete(lu),
            "locked": lineup_is_locked(lu),
        }
        team_lineups[t["id"]] = {row["position"]: row for row in lu}

    # Season points per team (through current week)
    team_season_pts: dict = {}
    for t in teams:
        team_season_pts[t["id"]] = get_team_season_score(t["id"], current_wk)

    # Enrich week_scores with player names
    enriched_week_scores: dict = {}
    conn2 = get_db()
    for pid, ps in week_scores.items():
        row = conn2.execute(
            "SELECT name, position FROM survivor_players WHERE id=?", (pid,)
        ).fetchone()
        enriched_week_scores[pid] = {
            **ps,
            "player_name": row["name"] if row else str(pid),
            "position": row["position"] if row else "",
        }
    conn2.close()

    return templates.TemplateResponse(
        "manage.html",
        {
            "request": request,
            "user": user,
            "league": league,
            "members": [dict(m) for m in members],
            "teams": teams_with_owners,
            "players": players,
            "current_week": current_wk,
            "total_weeks": NFL_REGULAR_SEASON_WEEKS,
            "week_scores": enriched_week_scores,
            "lineup_status": lineup_status,
            "team_lineups": team_lineups,
            "team_season_pts": team_season_pts,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "is_commissioner": True,
            "required_positions": REQUIRED_POSITIONS,
            "current_nfl_week": current_nfl_week(),
            "my_team": get_user_team_in_league(league_id, user["id"]),
        },
    )


# ── League settings ──────────────────────────────────────────────────────────


@app.post("/survivor/{league_id}/manage/settings")
def manage_settings(
    league_id: int,
    request: Request,
    league_name: str = Form(...),
    season: int = Form(2025),
    deadline_day: int = Form(0),
    deadline_hour: int = Form(13),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(
        adapt_sql(
            """
        UPDATE survivor_leagues
        SET name=?, season=?, submission_deadline_day=?, submission_deadline_hour=?
        WHERE id=?
    """
        ),
        (
            league_name.strip(),
            season,
            max(0, min(6, deadline_day)),
            max(0, min(23, deadline_hour)),
            league_id,
        ),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"],
        action="SETTINGS_UPDATE",
        league_id=league_id,
        details=f"name={league_name} season={season}",
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=settings_saved", status_code=303
    )


# ── Advance week (locks current lineups and moves to next week) ──────────────


@app.post("/survivor/{league_id}/manage/advance-week")
def advance_week(league_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    current_wk = league["current_week"]
    if current_wk >= NFL_REGULAR_SEASON_WEEKS:
        return RedirectResponse(
            f"/survivor/{league_id}/manage?error=season_over", status_code=303
        )

    conn = get_db()
    # Lock all lineups for the current week
    conn.execute(
        adapt_sql(
            """
        UPDATE survivor_lineups SET locked=1
        WHERE league_id=? AND week=?
    """
        ),
        (league_id, current_wk),
    )
    # Advance the week counter
    conn.execute(
        adapt_sql("UPDATE survivor_leagues SET current_week=? WHERE id=?"),
        (current_wk + 1, league_id),
    )
    conn.commit()
    conn.close()

    write_audit(
        actor=user["username"],
        action="ADVANCE_WEEK",
        league_id=league_id,
        details=f"week {current_wk} → {current_wk + 1}",
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=week_advanced", status_code=303
    )


# ── Lock current week's lineups without advancing ───────────────────────────


@app.post("/survivor/{league_id}/manage/lock-week")
def lock_week(league_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    conn = get_db()
    conn.execute(
        adapt_sql("UPDATE survivor_lineups SET locked=1 WHERE league_id=? AND week=?"),
        (league_id, league["current_week"]),
    )
    conn.commit()
    conn.close()

    write_audit(
        actor=user["username"],
        action="LOCK_WEEK",
        league_id=league_id,
        details=f"week={league['current_week']}",
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=week_locked", status_code=303
    )


# ── Player management ────────────────────────────────────────────────────────


@app.post("/survivor/{league_id}/manage/player/add")
def manage_add_player(
    league_id: int,
    request: Request,
    name: str = Form(...),
    position: str = Form(...),
    nfl_team: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    pos = position.strip().upper()
    if pos not in REQUIRED_POSITIONS:
        return RedirectResponse(
            f"/survivor/{league_id}/manage?error=bad_position", status_code=303
        )
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO survivor_players (league_id, name, position, nfl_team) VALUES (?,?,?,?)",
            (league_id, name.strip(), pos, nfl_team.strip().upper()),
        )
        conn.commit()
    except Exception:
        conn.close()
        return RedirectResponse(
            f"/survivor/{league_id}/manage?error=player_exists", status_code=303
        )
    conn.close()
    write_audit(
        actor=user["username"],
        action="PLAYER_ADD",
        league_id=league_id,
        player=name.strip(),
        details=f"pos={pos} team={nfl_team.upper()}",
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=player_added", status_code=303
    )


@app.post("/survivor/{league_id}/manage/player/edit")
def manage_edit_player(
    league_id: int,
    request: Request,
    player_id: int = Form(...),
    name: str = Form(...),
    position: str = Form(...),
    nfl_team: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    conn.execute(
        adapt_sql(
            "UPDATE survivor_players SET name=?, position=?, nfl_team=? WHERE id=? AND league_id=?"
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
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=player_updated", status_code=303
    )


@app.post("/survivor/{league_id}/manage/player/delete")
def manage_delete_player(
    league_id: int,
    request: Request,
    player_id: int = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM survivor_players WHERE id=? AND league_id=?",
        (player_id, league_id),
    ).fetchone()
    pname = row["name"] if row else str(player_id)
    conn.execute(
        adapt_sql("DELETE FROM survivor_lineups WHERE player_id=? AND league_id=?"),
        (player_id, league_id),
    )
    conn.execute(
        adapt_sql("DELETE FROM survivor_player_scores WHERE player_id=?"), (player_id,)
    )
    conn.execute(
        adapt_sql("DELETE FROM survivor_players WHERE id=? AND league_id=?"),
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
        f"/survivor/{league_id}/manage?msg=player_deleted", status_code=303
    )


@app.post("/survivor/{league_id}/manage/team/delete")
def manage_delete_team(
    league_id: int,
    request: Request,
    team_id: int = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM survivor_teams WHERE id=? AND league_id=?",
        (team_id, league_id),
    ).fetchone()
    tname = row["name"] if row else str(team_id)
    # Delete all lineups for the team then the team itself
    conn.execute(adapt_sql("DELETE FROM survivor_lineups WHERE team_id=?"), (team_id,))
    conn.execute(
        adapt_sql("DELETE FROM survivor_teams WHERE id=? AND league_id=?"),
        (team_id, league_id),
    )
    conn.commit()
    conn.close()
    write_audit(
        actor=user["username"], action="TEAM_DELETE", league_id=league_id, team=tname
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=team_deleted#teams", status_code=303
    )


# ── Score entry ──────────────────────────────────────────────────────────────


@app.post("/survivor/{league_id}/manage/scores/entry")
def manage_score_entry(
    league_id: int,
    request: Request,
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
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    override = float(override_points) if override_points.strip() else None
    conn = get_db()
    p_row = conn.execute(
        "SELECT name FROM survivor_players WHERE id=? AND league_id=?",
        (player_id, league_id),
    ).fetchone()
    if not p_row:
        conn.close()
        return RedirectResponse(
            f"/survivor/{league_id}/manage?error=bad_player", status_code=303
        )
    p_name = p_row["name"]

    conn.execute(
        adapt_sql(
            """
        INSERT INTO survivor_player_scores (
            player_id, week, receptions, receiving_yards, rushing_yards,
            return_yards, passing_yards, total_tds, fumbles_lost, interceptions,
            return_fumbles_lost, override_points, override_note
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(player_id, week) DO UPDATE SET
            receptions=excluded.receptions,
            receiving_yards=excluded.receiving_yards,
            rushing_yards=excluded.rushing_yards,
            return_yards=excluded.return_yards,
            passing_yards=excluded.passing_yards,
            total_tds=excluded.total_tds,
            fumbles_lost=excluded.fumbles_lost,
            interceptions=excluded.interceptions,
            return_fumbles_lost=excluded.return_fumbles_lost,
            override_points=excluded.override_points,
            override_note=excluded.override_note
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
            details=f"week={week} override={override}",
        )
    else:
        write_audit(
            actor=user["username"],
            action="SCORE_ENTRY",
            league_id=league_id,
            player=p_name,
            details=f"week={week}",
        )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=score_saved", status_code=303
    )


# ── NFL Sync ─────────────────────────────────────────────────────────────────


@app.post("/survivor/{league_id}/manage/sync/roster")
def sync_roster(league_id: int, request: Request, overwrite: bool = Form(False)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)
    try:
        result = seed_survivor_players(league_id, overwrite=overwrite)
        msg = f"sync_ok_{result.get('added', 0)}"
        write_audit(
            actor=user["username"],
            action="ROSTER_SYNC",
            league_id=league_id,
            details=f"added={result.get('added',0)}",
        )
    except Exception as e:
        msg = "sync_error"
        write_audit(
            actor=user["username"],
            action="ROSTER_SYNC_ERROR",
            league_id=league_id,
            details=str(e),
        )
    return RedirectResponse(f"/survivor/{league_id}/manage?msg={msg}", status_code=303)


@app.post("/survivor/{league_id}/manage/sync/week")
def sync_scores_now(league_id: int, request: Request, week: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    league = league_ctx(league_id, user)
    if not is_commissioner(league, user):
        raise HTTPException(status_code=403)

    conn = get_db()
    players = conn.execute(
        "SELECT id FROM survivor_players WHERE league_id=?", (league_id,)
    ).fetchall()
    conn.close()
    player_ids = [p["id"] for p in players]

    # Pull scores from main game's player_scores table if it's available
    updated = 0
    main_db_path = os.environ.get("DB_PATH", "data/fantasy.db")
    if os.path.exists(main_db_path):
        import sqlite3 as _s

        mconn = _s.connect(main_db_path)
        mconn.row_factory = _s.Row

        conn = get_db()
        for spid in player_ids:
            # Find matching player by name in the main DB
            srow = conn.execute(
                "SELECT name FROM survivor_players WHERE id=?", (spid,)
            ).fetchone()
            if not srow:
                continue
            mrow = mconn.execute(
                "SELECT ps.* FROM player_scores ps "
                "JOIN players p ON ps.player_id=p.id "
                "WHERE p.name=? AND ps.week=?",
                (srow["name"], week),
            ).fetchone()
            if not mrow:
                continue
            conn.execute(
                adapt_sql(
                    """
                INSERT INTO survivor_player_scores (
                    player_id, week, receptions, receiving_yards, rushing_yards,
                    return_yards, passing_yards, total_tds, fumbles_lost,
                    interceptions, field_goals_json, return_fumbles_lost,
                    override_points, override_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(player_id, week) DO UPDATE SET
                    receptions=excluded.receptions,
                    receiving_yards=excluded.receiving_yards,
                    rushing_yards=excluded.rushing_yards,
                    return_yards=excluded.return_yards,
                    passing_yards=excluded.passing_yards,
                    total_tds=excluded.total_tds,
                    fumbles_lost=excluded.fumbles_lost,
                    interceptions=excluded.interceptions,
                    field_goals_json=excluded.field_goals_json,
                    return_fumbles_lost=excluded.return_fumbles_lost
            """
                ),
                (
                    spid,
                    week,
                    mrow["receptions"],
                    mrow["receiving_yards"],
                    mrow["rushing_yards"],
                    mrow["return_yards"],
                    mrow["passing_yards"],
                    mrow["total_tds"],
                    mrow["fumbles_lost"],
                    mrow["interceptions"],
                    mrow["field_goals_json"] or "[]",
                    mrow["return_fumbles_lost"] or 0,
                    None,
                    None,
                ),
            )
            updated += 1
        conn.commit()
        conn.close()
        mconn.close()

    write_audit(
        actor=user["username"],
        action="SCORES_SYNC",
        league_id=league_id,
        details=f"week={week} updated={updated}",
    )
    return RedirectResponse(
        f"/survivor/{league_id}/manage?msg=scores_synced_{updated}", status_code=303
    )


# ──────────────────────────────────────────────────────────────────────────────
# AUDIT LOG
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/survivor/{league_id}/audit", response_class=HTMLResponse)
def audit_page(
    league_id: int,
    request: Request,
    action: str = None,
    search: str = None,
    limit: int = 200,
):
    user = get_current_user(request)
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
        f"SELECT * FROM survivor_audit_log {where} ORDER BY id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    action_types = [
        r["action"]
        for r in conn.execute(
            "SELECT DISTINCT action FROM survivor_audit_log WHERE league_id=? ORDER BY action",
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


# ──────────────────────────────────────────────────────────────────────────────
# JSON API
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/api/survivor/{league_id}/used-players")
def api_used_players(league_id: int, request: Request, team_id: int = None):
    """Return list of player IDs already used by a team (across all locked weeks)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)

    my_team = (
        get_user_team_in_league(league_id, user["id"]) if team_id is None else None
    )
    tid = team_id or (my_team["id"] if my_team else None)
    if not tid:
        raise HTTPException(status_code=400, detail="No team found")

    used_ids = get_used_player_ids_for_team(tid)
    conn = get_db()
    rows = (
        conn.execute(
            (
                "SELECT id, name, position, nfl_team FROM survivor_players "
                "WHERE id IN ({})".format(",".join("?" * len(used_ids)))
                if used_ids
                else "SELECT id, name, position, nfl_team FROM survivor_players WHERE 1=0"
            ),
            tuple(used_ids),
        ).fetchall()
        if used_ids
        else []
    )
    conn.close()
    return {"team_id": tid, "used_players": [dict(r) for r in rows]}


@app.get("/api/survivor/{league_id}/week-scores")
def api_week_scores(league_id: int, week: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)
    teams = get_league_teams(league_id)
    return [{"team": t, **get_team_week_score(t["id"], week)} for t in teams]


@app.get("/api/survivor/{league_id}/lineup/{team_id}/{week}")
def api_team_lineup(league_id: int, team_id: int, week: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)
    lineup = get_team_lineup(team_id, week)
    return {
        "team_id": team_id,
        "week": week,
        "lineup": lineup,
        "complete": lineup_is_complete(lineup),
        "locked": lineup_is_locked(lineup),
    }


@app.get("/api/survivor/{league_id}/available-players")
def api_available_players(league_id: int, week: int, request: Request):
    """Returns all players available (not yet used) for the requesting user's team."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    league_ctx(league_id, user)
    my_team = get_user_team_in_league(league_id, user["id"])
    if not my_team:
        raise HTTPException(status_code=404, detail="No team found")
    return get_available_players_for_team(league_id, my_team["id"], week)
