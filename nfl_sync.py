"""
nfl_sync.py — NFL roster and weekly stats sync
================================================
Uses nfl-data-py (backed by nflfastR) to:

  1. Seed a league's player pool from current NFL rosters
  2. Sync weekly player stats into player_scores

Stat mapping (nfl_data_py → our schema)
----------------------------------------
  receptions           → receptions
  receiving_yards      → receiving_yards
  rushing_yards        → rushing_yards
  passing_yards        → passing_yards
  receiving_tds +
  rushing_tds +
  passing_tds +
  special_teams_tds    → total_tds
  interceptions        → interceptions
  sack_fumbles_lost +
  receiving_fumbles_lost +
  rushing_fumbles_lost → fumbles_lost
  return_yards         → return_yards  (from PBP — kicker/ST only)

Kickers
--------
  Field goal distances come from play-by-play data
  (import_pbp_data filtered to field_goal_attempt == 1).
  Each made FG is stored as {"distance": N} in field_goals_json.
  Missed FGs are ignored — our scoring only rewards makes.

Live schedule awareness
------------------------
  The sync engine knows NFL game windows and only hits the API
  when games are actually being played.  Outside those windows
  it skips the network call entirely.

  Game windows (all times US/Eastern):
    Sunday    13:00 – 23:59
    Monday    20:00 – 23:59
    Thursday  20:00 – 23:59
    Saturday  13:00 – 23:59  (late-season only, weeks 15-18)

  During a game window:  sync every LIVE_INTERVAL seconds (default 60)
  Outside game window:   sync every IDLE_INTERVAL seconds (default 3600)
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("nfl_sync")

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Set NFL_SEASON in .env to override (e.g. NFL_SEASON=2025 when season starts)
import os as _os

CURRENT_SEASON  = int(_os.environ.get("NFL_SEASON", "2024"))
# NFL calendar week → fantasy playoff week (1-4).
# 2024-25 season: Wild Card=18, Divisional=19, Conf Champ=20, Super Bowl=22
# (week 21 = Pro Bowl — skipped).  Adjust for future seasons if needed.
# We store fantasy week (1-4) in player_scores.week so season totals
# never accidentally include regular-season data.
NFL_WEEK_TO_FANTASY = {
    18: 1,  # Wild Card
    19: 2,  # Divisional
    20: 3,  # Conference Championships
    22: 4,  # Super Bowl  (21 = Pro Bowl, skipped)
}
FANTASY_WEEKS = {1, 2, 3, 4}

LIVE_INTERVAL   = 60    # seconds between syncs during game windows
IDLE_INTERVAL   = 3600  # seconds between syncs outside game windows

# Positions we care about — filter out long snappers, practice squad etc.
VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST"}

# NFL team abbreviation normalisation — nfl_data_py uses some non-standard ones
TEAM_MAP = {
    "LA":  "LAR",
    "LV":  "LAS",  # Las Vegas Raiders
    "JAC": "JAX",
}


# ──────────────────────────────────────────────────────────────────────────────
# Schedule awareness
# ──────────────────────────────────────────────────────────────────────────────

def _in_game_window() -> bool:
    """
    Return True if we're currently inside a likely NFL game window.
    Times are US Eastern.  We add a 2-hour buffer after the window ends
    to catch late games running long.
    """
    try:
        # Convert UTC to US/Eastern (UTC-5 standard, UTC-4 daylight)
        # Simple approximation: EDT Mar–Nov, EST Nov–Mar
        now_utc  = datetime.now(timezone.utc)
        month    = now_utc.month
        offset   = timedelta(hours=-4) if 3 <= month <= 11 else timedelta(hours=-5)
        now_et   = now_utc + offset
        dow      = now_et.weekday()   # 0=Mon … 6=Sun
        hour     = now_et.hour
        week_num = _estimate_nfl_week(now_utc)

        windows = {
            6: (13, 26),   # Sunday  1pm – midnight+2
            0: (20, 26),   # Monday  8pm – midnight+2
            3: (20, 26),   # Thursday 8pm – midnight+2
        }
        # Late-season Saturday games (weeks 15-18)
        if 15 <= week_num <= 18:
            windows[5] = (13, 26)  # Saturday

        if dow in windows:
            start, end = windows[dow]
            if start <= hour < end:
                return True
    except Exception:
        pass
    return False


def _estimate_nfl_week(now_utc: datetime) -> int:
    """
    Rough estimate of the current NFL week number.
    Week 1 of 2025 season starts ~Sep 4, 2025.
    """
    season_start = datetime(2025, 9, 4, tzinfo=timezone.utc)
    if now_utc < season_start:
        return 0
    delta_days = (now_utc - season_start).days
    return min(18, max(1, delta_days // 7 + 1))


def current_nfl_week() -> int:
    return _estimate_nfl_week(datetime.now(timezone.utc))


# ──────────────────────────────────────────────────────────────────────────────
# Data fetching (nfl-data-py wrappers)
# ──────────────────────────────────────────────────────────────────────────────

def _import_nfl():
    """Lazy import so the app starts even if nfl-data-py isn't installed."""
    try:
        import nfl_data_py as nfl
        return nfl
    except ImportError:
        raise RuntimeError(
            "nfl-data-py is not installed.  Run:  pip install nfl-data-py"
        )


def fetch_rosters(season: int = CURRENT_SEASON) -> list[dict]:
    """
    Return a list of active NFL players for the given season.
    Uses import_seasonal_rosters (available in nfl-data-py 0.3.3).
    Each dict has: player_id, name, position, nfl_team
    """
    nfl = _import_nfl()
    logger.info(f"Fetching NFL rosters for {season} season...")

    try:
        # import_seasonal_rosters returns one row per player per season
        df = nfl.import_seasonal_rosters([season])
    except Exception as e:
        logger.error(f"Failed to fetch rosters: {e}")
        return []

    # Column names in import_seasonal_rosters:
    #   player_name, position, team, status, player_id (gsis_id)
    players = []
    seen = set()
    for _, row in df.iterrows():
        pos    = str(row.get("position") or "").strip().upper()
        # seasonal rosters uses 'player_name' or 'full_name' depending on version
        name   = str(row.get("player_name") or row.get("full_name") or "").strip()
        team   = str(row.get("team") or row.get("recent_team") or "").strip().upper()
        pid    = str(row.get("player_id") or row.get("gsis_id") or "").strip()
        status = str(row.get("status") or "").strip().upper()

        if not name or not pos:
            continue
        if pos not in VALID_POSITIONS:
            continue
        # Skip injured reserve / practice squad / retired
        if status in ("IR", "PUP", "NFI", "RET", "CUT", "UFA"):
            continue

        team = TEAM_MAP.get(team, team)
        key  = (name, pos, team)
        if key in seen:
            continue
        seen.add(key)

        players.append({
            "player_id": pid,
            "name":      name,
            "position":  pos,
            "nfl_team":  team,
        })

    logger.info(f"Fetched {len(players)} roster players")
    return players


def fetch_weekly_stats(season: int = CURRENT_SEASON,
                       week: Optional[int] = None) -> list[dict]:
    """
    Return per-player weekly stats for the given season (and optionally week).
    Each dict maps to our player_scores schema.
    """
    nfl = _import_nfl()
    weeks = [week] if week else list(range(1, 19))
    logger.info(f"Fetching weekly stats season={season} weeks={weeks}...")

    COLS = [
        "player_id", "player_display_name", "position", "recent_team",
        "week", "season",
        "receptions", "receiving_yards", "receiving_tds",
        "rushing_yards", "rushing_tds",
        "passing_yards", "passing_tds",
        "interceptions",
        "sack_fumbles_lost", "receiving_fumbles_lost", "rushing_fumbles_lost",
        "special_teams_tds",
    ]

    try:
        df = nfl.import_weekly_data([season])
        # Only keep columns that actually exist in this version
        available = [c for c in COLS if c in df.columns]
        df = df[available]
    except Exception as e:
        logger.error(f"Failed to fetch weekly stats: {e}")
        return []

    if week:
        df = df[df["week"] == week]

    results = []
    for _, row in df.iterrows():
        w   = int(row.get("week") or 0)
        pid = str(row.get("player_id") or "").strip()
        name = str(row.get("player_display_name") or "").strip()
        pos  = str(row.get("position") or "").strip().upper()
        team = TEAM_MAP.get(str(row.get("recent_team") or "").upper(),
                            str(row.get("recent_team") or "").upper())

        if not pid or not w:
            continue

        def _f(col):
            v = row.get(col, 0.0)
            try:
                f = float(v)
                return 0.0 if (f != f) else f  # handle NaN
            except (TypeError, ValueError):
                return 0.0

        total_tds = (
            _f("receiving_tds") + _f("rushing_tds") +
            _f("passing_tds")   + _f("special_teams_tds")
        )
        fumbles_lost = (
            _f("sack_fumbles_lost") +
            _f("receiving_fumbles_lost") +
            _f("rushing_fumbles_lost")
        )

        results.append({
            "player_id_nfl": pid,
            "player_name":   name,
            "position":      pos,
            "nfl_team":      team,
            "week":          w,
            "season":        int(row.get("season") or season),
            "receptions":    _f("receptions"),
            "receiving_yards": _f("receiving_yards"),
            "rushing_yards": _f("rushing_yards"),
            "return_yards":  0.0,   # filled in by PBP pass if needed
            "passing_yards": _f("passing_yards"),
            "total_tds":     total_tds,
            "fumbles_lost":  fumbles_lost,
            "interceptions": _f("interceptions"),
            "field_goals_json": "[]",   # filled in for kickers below
        })

    logger.info(f"Fetched {len(results)} weekly stat rows")
    return results


def fetch_kicker_fg_data(season: int = CURRENT_SEASON,
                         week: Optional[int] = None) -> dict[tuple, str]:
    """
    Return a dict mapping (player_id_nfl, week) → field_goals_json string.
    Pulls from play-by-play data for accurate distances.
    """
    nfl = _import_nfl()
    logger.info(f"Fetching PBP kicker data season={season} week={week}...")

    try:
        df = nfl.import_pbp_data([season])
    except Exception as e:
        logger.error(f"Failed to fetch PBP data: {e}")
        return {}

    try:
        # Only keep rows that are field goal attempts
        needed = ["kicker_player_id", "kick_distance", "field_goal_result", "week"]
        missing = [c for c in needed if c not in df.columns]
        if missing:
            logger.warning(f"PBP missing columns {missing} — skipping FG data")
            return {}

        df = df[df["field_goal_result"].notna()]
        df = df[df["field_goal_result"] == "made"]
        if week:
            df = df[df["week"] == week]

        result: dict[tuple, list] = {}
        for _, row in df.iterrows():
            pid  = str(row.get("kicker_player_id") or "").strip()
            dist = row.get("kick_distance")
            w    = row.get("week")
            if not pid or not w:
                continue
            try:
                dist = float(dist)
                w    = int(w)
            except (TypeError, ValueError):
                continue
            key = (pid, w)
            result.setdefault(key, []).append({"distance": dist})

        return {k: json.dumps(v) for k, v in result.items()}

    except Exception as e:
        logger.error(f"Error processing PBP FG data: {e}")
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Database operations
# ──────────────────────────────────────────────────────────────────────────────

def seed_players(league_id: int, season: int = CURRENT_SEASON,
                 overwrite: bool = False) -> dict:
    """
    Populate a league's player pool from NFL rosters.
    Returns {"added": N, "skipped": N, "errors": N}
    """
    from db import adapt_sql, get_db

    players = fetch_rosters(season)
    if not players:
        return {"added": 0, "skipped": 0, "errors": 1,
                "message": "Could not fetch roster data from nfl-data-py"}

    conn    = get_db()
    added   = 0
    skipped = 0
    errors  = 0

    for p in players:
        try:
            if overwrite:
                # Update name/team if player already exists
                existing = conn.execute(adapt_sql(
                    "SELECT id FROM players WHERE league_id=? AND name=? AND position=?"
                ), (league_id, p["name"], p["position"])).fetchone()
                if existing:
                    conn.execute(adapt_sql(
                        "UPDATE players SET nfl_team=? WHERE id=?"
                    ), (p["nfl_team"], existing["id"]))
                    skipped += 1
                    continue

            conn.execute(adapt_sql(
                "INSERT INTO players (league_id, name, position, nfl_team) VALUES (?,?,?,?)"
            ), (league_id, p["name"], p["position"], p["nfl_team"]))
            added += 1

        except Exception:
            # UNIQUE constraint — player already in pool
            skipped += 1

    conn.commit()
    conn.close()

    logger.info(f"[league {league_id}] Roster seed: added={added} skipped={skipped} errors={errors}")
    # Add all 32 NFL team DSTs
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
                "INSERT INTO players (league_id, name, position, nfl_team) VALUES (?,?,?,?)",
                (league_id, name, "DST", team)
            )
            added += 1
        except Exception:
            skipped += 1
    return {"added": added, "skipped": skipped, "errors": errors}


def sync_week(league_id: int, week: int,
              season: int = CURRENT_SEASON) -> dict:
    """
    Sync player stats for a given week into player_scores.
    `week` may be either the raw NFL calendar week (e.g. 18-22) or
    a fantasy playoff week (1-4).  We always store fantasy week (1-4)
    in player_scores so season totals stay playoff-only.
    Only updates players that exist in this league's player pool.
    Returns {"updated": N, "skipped": N, "errors": N}
    """
    from db import adapt_sql, get_db

    # Translate NFL calendar week → fantasy week if needed
    nfl_week      = NFL_WEEK_TO_FANTASY.get(week, week if week in FANTASY_WEEKS else None)
    if nfl_week is None:
        return {"updated": 0, "skipped": 0, "errors": 0,
                "message": f"Week {week} is not a playoff week — skipped"}
    fantasy_week  = nfl_week   # the value written to player_scores.week

    # fetch_weekly_stats needs the actual NFL calendar week
    raw_nfl_week  = next((k for k, v in NFL_WEEK_TO_FANTASY.items() if v == fantasy_week), week)

    weekly  = fetch_weekly_stats(season, raw_nfl_week)
    fg_data = fetch_kicker_fg_data(season, raw_nfl_week)

    if not weekly:
        return {"updated": 0, "skipped": 0, "errors": 1,
                "message": f"No stat data returned for week {week}"}

    conn = get_db()

    # Build a lookup: player name+position → our internal player id
    # nfl_data_py player_id ≠ our DB id, so we match on name+position
    league_players = conn.execute(adapt_sql(
        "SELECT id, name, position FROM players WHERE league_id=?"
    ), (league_id,)).fetchall()

    name_to_id: dict[tuple, int] = {}
    for lp in league_players:
        key = (str(lp["name"]).strip().lower(), str(lp["position"]).strip().upper())
        name_to_id[key] = lp["id"]

    updated = 0
    skipped = 0
    errors  = 0

    for row in weekly:
        if row["season"] != season or row["week"] != week:
            continue

        lookup_key = (row["player_name"].strip().lower(), row["position"].strip().upper())
        player_db_id = name_to_id.get(lookup_key)
        if player_db_id is None:
            skipped += 1
            continue

        # Inject FG data for kickers
        fg_json = row["field_goals_json"]
        if row["position"] == "K":
            fg_key  = (row["player_id_nfl"], week)
            fg_json = fg_data.get(fg_key, "[]")

        try:
            # Upsert — update if exists, insert if not
            # Check if override_points is set — don't overwrite manual overrides
            existing = conn.execute(adapt_sql(
                "SELECT id, override_points FROM player_scores WHERE player_id=? AND week=?"
            ), (player_db_id, fantasy_week)).fetchone()

            if existing:
                if existing["override_points"] is not None:
                    skipped += 1  # manual override in place — preserve it
                    continue
                conn.execute(adapt_sql("""
                    UPDATE player_scores SET
                        receptions=?, receiving_yards=?, rushing_yards=?,
                        return_yards=?, passing_yards=?, total_tds=?,
                        fumbles_lost=?, interceptions=?, field_goals_json=?,
                        return_fumbles_lost=0
                    WHERE player_id=? AND week=?
                """), (
                    row["receptions"], row["receiving_yards"], row["rushing_yards"],
                    row["return_yards"], row["passing_yards"], row["total_tds"],
                    row["fumbles_lost"], row["interceptions"], fg_json,
                    player_db_id, fantasy_week
                ))
            else:
                conn.execute(adapt_sql("""
                    INSERT INTO player_scores (
                        player_id, week, receptions, receiving_yards, rushing_yards,
                        return_yards, passing_yards, total_tds, fumbles_lost,
                        interceptions, field_goals_json, return_fumbles_lost
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
                """), (
                    player_db_id, fantasy_week,
                    row["receptions"], row["receiving_yards"], row["rushing_yards"],
                    row["return_yards"], row["passing_yards"], row["total_tds"],
                    row["fumbles_lost"], row["interceptions"], fg_json
                ))
            updated += 1

        except Exception as e:
            logger.error(f"Error upserting player {player_db_id} fantasy_week {fantasy_week}: {e}")
            errors += 1

    conn.commit()
    conn.close()

    logger.info(
        f"[league {league_id}] Week {fantasy_week} (NFL wk {raw_nfl_week}) sync: "
        f"updated={updated} skipped={skipped} errors={errors}"
    )
    return {"updated": updated, "skipped": skipped, "errors": errors,
            "week": week, "season": season}


# ──────────────────────────────────────────────────────────────────────────────
# Background scheduler
# ──────────────────────────────────────────────────────────────────────────────

class NFLSyncScheduler:
    """
    Runs a background thread that syncs stats for all leagues on a smart schedule.

    - During NFL game windows: syncs every LIVE_INTERVAL seconds
    - Outside game windows:    syncs every IDLE_INTERVAL seconds
    - Thread is daemonized — it dies when the main process exits

    Usage:
        scheduler = NFLSyncScheduler()
        scheduler.start()
        # app runs...
        scheduler.stop()
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread     = threading.Thread(target=self._loop, daemon=True, name="nfl-sync")
        self._last_sync: dict[int, datetime] = {}   # league_id → last sync time
        self._last_status: dict[int, dict]   = {}   # league_id → last result

    def start(self):
        logger.info("NFLSyncScheduler starting")
        self._thread.start()

    def stop(self):
        logger.info("NFLSyncScheduler stopping")
        self._stop_event.set()

    def last_status(self, league_id: int) -> dict:
        return self._last_status.get(league_id, {})

    def sync_now(self, league_id: int) -> dict:
        """Trigger an immediate sync for a single league (called from API)."""
        return self._sync_league(league_id)

    # ── internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                in_window = _in_game_window()
                interval  = LIVE_INTERVAL if in_window else IDLE_INTERVAL

                if in_window:
                    league_ids = self._get_active_league_ids()
                    for lid in league_ids:
                        if self._stop_event.is_set():
                            break
                        last = self._last_sync.get(lid)
                        if last is None or (datetime.now(timezone.utc) - last).total_seconds() >= LIVE_INTERVAL:
                            self._sync_league(lid)

                self._stop_event.wait(timeout=interval)

            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                self._stop_event.wait(timeout=60)

    def _get_active_league_ids(self) -> list[int]:
        """Return IDs of all leagues whose draft is not complete."""
        try:
            from db import adapt_sql, get_db
            conn = get_db()
            rows = conn.execute(
                "SELECT l.id FROM leagues l "
                "LEFT JOIN draft_state ds ON ds.league_id = l.id "
                "WHERE ds.is_complete = 1 OR ds.league_id IS NOT NULL"
            ).fetchall()
            conn.close()
            return [r["id"] for r in rows]
        except Exception as e:
            logger.error(f"Could not get league IDs: {e}")
            return []

    def _sync_league(self, league_id: int) -> dict:
        week = current_nfl_week()
        if week < 1 or week > 18:
            return {"message": "Off-season — no sync needed"}

        try:
            result = sync_week(league_id, week, CURRENT_SEASON)
            result["synced_at"] = datetime.now(timezone.utc).isoformat()
            result["week"]      = week
            self._last_sync[league_id]  = datetime.now(timezone.utc)
            self._last_status[league_id] = result
            return result
        except Exception as e:
            logger.error(f"sync_league {league_id} week {week} failed: {e}")
            err = {"error": str(e), "synced_at": datetime.now(timezone.utc).isoformat()}
            self._last_status[league_id] = err
            return err


# Module-level singleton — imported by main.py
sync_scheduler = NFLSyncScheduler()
