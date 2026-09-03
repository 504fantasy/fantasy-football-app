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
    "LAS": "LV",  # Las Vegas Raiders
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

        windows = {
            4: (18, 26),   # Friday  6pm – midnight+2 (preseason/holiday games)
            5: (13, 26),   # Saturday 1pm – midnight+2 (preseason/late-season games)
            6: (13, 26),   # Sunday  1pm – midnight+2
            0: (20, 26),   # Monday  8pm – midnight+2
            3: (20, 26),   # Thursday 8pm – midnight+2
        }

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
        headshot = row.get("headshot_url")
        headshot = str(headshot).strip() if headshot and str(headshot) != "nan" else None
        if headshot and "/upload/f_auto,q_auto/" in headshot:
            headshot = headshot.replace("/upload/f_auto,q_auto/", "/upload/f_auto,q_auto,c_thumb,g_face,w_150,h_150/")

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
            "headshot_url": headshot,
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

        passing_tds = _f("passing_tds")
        other_tds = _f("receiving_tds") + _f("rushing_tds") + _f("special_teams_tds")
        total_tds = passing_tds + other_tds
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
            "passing_tds":   passing_tds,
            "other_tds":     other_tds,
            "fumbles_lost":  fumbles_lost,
            "interceptions": _f("interceptions"),
            "field_goals_json": "[]",   # filled in for kickers below
        })

    logger.info(f"Fetched {len(results)} weekly stat rows")
    return results


def fetch_kicker_fg_data(season: int = CURRENT_SEASON,
                         week: Optional[int] = None,
                         season_type: Optional[str] = None) -> dict[tuple, str]:
    """
    Return a dict mapping (player_id_nfl, week) → field_goals_json string.
    Pulls from play-by-play data for accurate distances.
    season_type: 'PRE'/'REG'/'POST' — nflverse's own `week` column resets
    per season_type, so pass this to avoid mixing e.g. preseason week 1
    with regular-season week 1 data.
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
        if season_type and "season_type" in df.columns:
            df = df[df["season_type"] == season_type]

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


def fetch_pbp_full_stats(season: int = CURRENT_SEASON,
                          week: Optional[int] = None,
                          season_type: Optional[str] = None,
                          date_range: Optional[tuple] = None,
                          output_week: Optional[int] = None) -> tuple[dict, dict]:
    """
    Derive full per-player offensive stats AND bonus/defense stats directly
    from play-by-play data. Used for the Survivor app's auto-sync, since
    that app needs preseason coverage and the aggregate weekly-stats table
    (fetch_weekly_stats) doesn't reliably include preseason games — PBP data
    does, so this computes receptions/yards/TDs/turnovers from raw plays
    rather than depending on that table at all.

    season_type: nflverse's PBP season_type values are 'PRE', 'REG', 'POST'.
    Pass this to disambiguate — nflverse's own `week` column resets per
    season_type, so week=1 could mean preseason week 1 OR regular-season
    week 1 in the same raw dataset without this filter.

    date_range: optional (start_date, end_date) as 'YYYY-MM-DD' strings.
    When given, filters by actual game date instead of nflverse's own week
    number, and every resulting stat is tagged with output_week instead of
    nflverse's week value. Use this when nflverse's internal week numbering
    for a given season_type isn't a verified match to your own week
    labels (e.g. preseason, where only the ESPN-side offset has been
    confirmed live — nflverse's own numbering is a separate, unverified
    convention).

    50+ yard TD bonuses take priority over 40+ (never both, to avoid
    double-counting a single big play).

    Returns (player_stats, team_stats, id_to_name). player_stats/team_stats
    are each keyed by (id, week) -> dict of stat_name -> count/yards.
    id_to_name maps NFL player ID -> display name (from PBP's short-name
    columns), for apps that need to match players by name rather than ID.
    """
    nfl = _import_nfl()
    player_stats: dict[tuple, dict] = {}
    team_stats: dict[tuple, dict] = {}
    id_to_name: dict[str, str] = {}

    def _bump(store: dict, key: tuple, field: str, amt=1):
        store.setdefault(key, {})
        store[key][field] = store[key].get(field, 0) + amt

    try:
        df = nfl.import_pbp_data([season])
    except Exception as e:
        logger.error(f"Failed to fetch PBP data: {e}")
        return player_stats, team_stats, id_to_name

    if date_range:
        date_col = None
        for candidate in ("game_date", "gameday", "start_time"):
            if candidate in df.columns:
                date_col = candidate
                break
        if date_col:
            df = df[(df[date_col] >= date_range[0]) & (df[date_col] <= date_range[1])]
        else:
            logger.warning(
                "PBP data has no recognizable date column (tried game_date/"
                "gameday/start_time) — cannot filter by date_range; falling "
                "back to week-number filtering, which may be inaccurate for "
                "this season_type."
            )
            if week:
                df = df[df["week"] == week]
    elif week:
        df = df[df["week"] == week]

    if season_type:
        type_col = "season_type" if "season_type" in df.columns else (
            "game_type" if "game_type" in df.columns else None
        )
        if type_col:
            df = df[df[type_col] == season_type]
        else:
            logger.warning(
                "PBP data has neither season_type nor game_type column — "
                "cannot filter by season type, results may mix preseason/"
                "regular-season/postseason plays if week numbers overlap."
            )

    for _, row in df.iterrows():
        if output_week is not None:
            w = output_week
        else:
            w = row.get("week")
            if not w:
                continue
            try:
                w = int(w)
            except (TypeError, ValueError):
                continue

        yards = row.get("yards_gained", 0)
        try:
            yards = float(yards) if yards == yards else 0.0  # NaN check
        except (TypeError, ValueError):
            yards = 0.0

        def _pid(col):
            v = row.get(col)
            if v is None:
                return None
            try:
                if v != v:  # NaN check — NaN is the only value where this is True
                    return None
            except Exception:
                pass
            s = str(v).strip()
            if not s or s.lower() == "nan":
                return None
            # Capture id->name alongside the id, wherever a matching
            # "..._player_name" column exists — needed for apps (like
            # Survivor) that match players by name rather than NFL ID.
            if col.endswith("_player_id"):
                name_col = col[: -len("_id")] + "_name"
                nm = row.get(name_col)
                if nm is not None and nm == nm and str(nm).strip():
                    id_to_name.setdefault(s, str(nm).strip())
            return s

        # -- Base offensive stats --
        if row.get("complete_pass") == 1:
            receiver = _pid("receiver_player_id")
            if receiver:
                _bump(player_stats, (receiver, w), "receptions")
                _bump(player_stats, (receiver, w), "receiving_yards", yards)
            passer = _pid("passer_player_id")
            if passer:
                _bump(player_stats, (passer, w), "passing_yards", yards)
            if yards >= 40:
                if passer:
                    _bump(player_stats, (passer, w), "pass_40_completions")

        if row.get("rush_attempt") == 1:
            rusher = _pid("rusher_player_id")
            if rusher:
                _bump(player_stats, (rusher, w), "rushing_yards", yards)

        if row.get("interception") == 1:
            passer = _pid("passer_player_id")
            if passer:
                _bump(player_stats, (passer, w), "interceptions")

        if row.get("fumble_lost") == 1:
            fumbler = _pid("fumbled_1_player_id")
            if fumbler:
                _bump(player_stats, (fumbler, w), "fumbles_lost")

        # -- Touchdowns: comprehensive count via td_player_id, which
        # nflverse fills for the actual scorer on EVERY touchdown type —
        # rushing, receiving, fumble return, INT return (pick-six), and
        # punt/kick return TDs. This is what makes special-teams/defensive
        # return touchdowns auto-sync instead of needing manual entry.
        # The passer on a passing TD is credited separately below, since
        # td_player_id identifies the scorer (receiver), not the thrower.
        if row.get("touchdown") == 1:
            scorer = _pid("td_player_id")
            if scorer:
                _bump(player_stats, (scorer, w), "other_tds")
            if row.get("pass_touchdown") == 1:
                passer = _pid("passer_player_id")
                if passer:
                    _bump(player_stats, (passer, w), "passing_tds")

        # -- Distance bonuses: scoped specifically to actual rushing/
        # passing-and-catching plays (not return TDs — there's no bonus
        # category defined for return-TD distance). --
        if row.get("pass_touchdown") == 1:
            passer = _pid("passer_player_id")
            receiver = _pid("receiver_player_id")
            if yards >= 50:
                if passer:
                    _bump(player_stats, (passer, w), "pass_td_50")
                if receiver:
                    _bump(player_stats, (receiver, w), "rec_td_50")
            elif yards >= 40:
                if passer:
                    _bump(player_stats, (passer, w), "pass_td_40")
                if receiver:
                    _bump(player_stats, (receiver, w), "rec_td_40")

        if row.get("rush_touchdown") == 1:
            rusher = _pid("rusher_player_id")
            if rusher:
                if yards >= 50:
                    _bump(player_stats, (rusher, w), "rush_td_50")
                elif yards >= 40:
                    _bump(player_stats, (rusher, w), "rush_td_40")

        # -- 2pt conversions (credit whichever player(s) were involved) --
        if row.get("two_point_conv_result") == "success":
            for col in ("passer_player_id", "rusher_player_id", "receiver_player_id"):
                pid = _pid(col)
                if pid:
                    _bump(player_stats, (pid, w), "two_pt_conversions")

        # -- Kicking: FG made (with distance), PAT made, FG missed --
        if row.get("field_goal_result") == "made":
            pid = _pid("kicker_player_id")
            dist = row.get("kick_distance")
            try:
                dist = float(dist) if dist == dist else None  # NaN check
            except (TypeError, ValueError):
                dist = None
            if pid and dist is not None:
                player_stats.setdefault((pid, w), {})
                player_stats[(pid, w)].setdefault("field_goals_made", [])
                player_stats[(pid, w)]["field_goals_made"].append({"distance": dist})
        if row.get("extra_point_result") == "good":
            pid = _pid("kicker_player_id")
            if pid:
                _bump(player_stats, (pid, w), "pat_made")
        if row.get("field_goal_result") == "missed":
            pid = _pid("kicker_player_id")
            if pid:
                _bump(player_stats, (pid, w), "fg_missed")

        # -- Defense (team-level) --
        _dt = row.get("defteam")
        defteam_raw = "" if _dt is None or _dt != _dt else str(_dt).upper().strip()
        if defteam_raw and defteam_raw.lower() != "nan":
            defteam = TEAM_MAP.get(defteam_raw, defteam_raw)
            if row.get("sack") == 1:
                _bump(team_stats, (defteam, w), "sacks")
            if row.get("safety") == 1:
                _bump(team_stats, (defteam, w), "safeties")
            if row.get("fumble_forced") == 1:
                _bump(team_stats, (defteam, w), "forced_fumbles")
            if row.get("field_goal_result") == "blocked" or row.get("punt_blocked") == 1:
                _bump(team_stats, (defteam, w), "blocked_kicks")

    return player_stats, team_stats, id_to_name


def fetch_points_allowed(season: int, week: int, season_type: int = 2) -> dict:
    """
    Fetch each team's points allowed for a given week from ESPN's public
    scoreboard (completed games only). Returns {team_abbr: points_allowed}.
    Uses curl rather than urllib — ESPN's bot protection blocks urllib's
    TLS fingerprint even with a browser User-Agent header, but plain curl
    gets through fine.
    """
    import subprocess
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        f"?week={week}&seasontype={season_type}&dates={season}"
    )
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "8", url],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout:
            return {}
        data = json.loads(result.stdout)
    except Exception as e:
        logger.error(f"Failed to fetch points-allowed data: {e}")
        return {}

    points_allowed: dict = {}
    for event in data.get("events", []):
        status = event.get("status", {}).get("type", {})
        if not status.get("completed"):
            continue
        comps = event.get("competitions", [{}])[0].get("competitors", [])
        if len(comps) != 2:
            continue
        team_scores = {}
        for c in comps:
            abbr = str(c.get("team", {}).get("abbreviation", "")).upper()
            try:
                score = int(c.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if abbr:
                team_scores[TEAM_MAP.get(abbr, abbr)] = score
        teams = list(team_scores.keys())
        if len(teams) == 2:
            t1, t2 = teams
            points_allowed[t1] = team_scores[t2]
            points_allowed[t2] = team_scores[t1]

    return points_allowed


def fetch_espn_week_stats(season: int, week: int, season_type: int = 2) -> tuple[dict, dict]:
    """
    Fetch per-player and per-team stats for a week directly from ESPN's
    game-summary endpoints (boxscore + scoringPlays), rather than nflverse.

    This exists because nflverse doesn't reliably publish preseason data
    (their pipeline appears to only start once the regular season officially
    begins), while ESPN's endpoints have proven to have full live preseason
    coverage — confirmed to include individual defensive players' sacks,
    and every touchdown type including special-teams/defensive returns
    (e.g. a 97-yard pick-six shows up directly in the interceptions stat
    line with a TD flag).

    Column positions below are taken directly from ESPN's own `labels`/
    `keys` metadata for each stat category, not guessed.

    Known gaps — not available from this data source at all: forced
    fumbles, safety, blocked kicks. These stay at 0 and need manual entry
    if they occur. Field goal distance is exact only when a kicker made
    exactly one FG that week (using the box score's "long" value); with
    multiple makes, only the longest is known, so all makes are credited
    at that same distance as a deliberate overestimate-safe fallback
    rather than guessing individual distances.

    Returns (player_stats, team_stats):
      player_stats keyed by ESPN's own display name (e.g. "Adam Prentice")
        -> dict of stat_name -> value
      team_stats keyed by team abbreviation -> dict of stat_name -> value
    """
    import subprocess
    import re

    def _curl_json(url):
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "10", url],
                capture_output=True, text=True, timeout=12
            )
            if result.returncode != 0 or not result.stdout:
                return None
            return json.loads(result.stdout)
        except Exception as e:
            logger.error(f"ESPN fetch failed for {url}: {e}")
            return None

    scoreboard_url = (
        f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        f"?week={week}&seasontype={season_type}&dates={season}"
    )
    sb = _curl_json(scoreboard_url)
    if not sb:
        return {}, {}

    player_stats: dict[str, dict] = {}
    team_stats: dict[str, dict] = {}

    def _add_td(pname, field, amt=1):
        player_stats.setdefault(pname, {})
        player_stats[pname][field] = player_stats[pname].get(field, 0) + amt

    for event in sb.get("events", []):
        status = event.get("status", {}).get("type", {})
        if not status.get("completed"):
            continue
        event_id = event.get("id")
        if not event_id:
            continue

        summary = _curl_json(
            f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
        )
        if not summary:
            continue

        # -- Box score: core aggregate stats (column positions confirmed
        # via ESPN's own labels/keys metadata) --
        for team_block in summary.get("boxscore", {}).get("players", []):
            team_abbr = TEAM_MAP.get(
                str(team_block.get("team", {}).get("abbreviation", "")).upper(),
                str(team_block.get("team", {}).get("abbreviation", "")).upper(),
            )
            for cat in team_block.get("statistics", []):
                cat_name = cat.get("name")
                for a in cat.get("athletes", []):
                    pname = a.get("athlete", {}).get("displayName")
                    if not pname:
                        continue
                    stats = a.get("stats", [])
                    player_stats.setdefault(pname, {})
                    try:
                        if cat_name == "passing" and len(stats) >= 5:
                            player_stats[pname]["passing_yards"] = float(stats[1])
                            player_stats[pname]["passing_tds"] = int(stats[3])
                            player_stats[pname]["interceptions"] = int(stats[4])
                        elif cat_name == "rushing" and len(stats) >= 4:
                            player_stats[pname]["rushing_yards"] = float(stats[1])
                            _add_td(pname, "other_tds", int(stats[3]))
                        elif cat_name == "receiving" and len(stats) >= 4:
                            player_stats[pname]["receptions"] = float(stats[0])
                            player_stats[pname]["receiving_yards"] = float(stats[1])
                            _add_td(pname, "other_tds", int(stats[3]))
                        elif cat_name == "fumbles" and len(stats) >= 2:
                            player_stats[pname]["fumbles_lost"] = int(stats[1])
                        elif cat_name == "defensive" and len(stats) >= 7:
                            sacks = float(stats[2])
                            if sacks:
                                team_stats.setdefault(team_abbr, {})
                                team_stats[team_abbr]["sacks"] = team_stats[team_abbr].get("sacks", 0) + sacks
                            def_td = int(stats[6])
                            if def_td:
                                _add_td(pname, "other_tds", def_td)
                        elif cat_name == "interceptions" and len(stats) >= 3:
                            int_td = int(stats[2])
                            if int_td:
                                _add_td(pname, "other_tds", int_td)
                        elif cat_name == "kickReturns" and len(stats) >= 5:
                            player_stats[pname]["return_yards"] = player_stats[pname].get("return_yards", 0) + float(stats[1])
                            kr_td = int(stats[4])
                            if kr_td:
                                _add_td(pname, "other_tds", kr_td)
                        elif cat_name == "puntReturns" and len(stats) >= 5:
                            player_stats[pname]["return_yards"] = player_stats[pname].get("return_yards", 0) + float(stats[1])
                            pr_td = int(stats[4])
                            if pr_td:
                                _add_td(pname, "other_tds", pr_td)
                        elif cat_name == "kicking" and len(stats) >= 4:
                            fg_made, fg_att = (int(x) for x in stats[0].split("/"))
                            xp_made, _xp_att = (int(x) for x in stats[3].split("/"))
                            long_fg = int(stats[2]) if str(stats[2]).strip() not in ("", "-", "--") else 0
                            player_stats[pname]["pat_made"] = xp_made
                            player_stats[pname]["fg_missed"] = max(0, fg_att - fg_made)
                            if fg_made and long_fg:
                                # Exact distance only known for a single make;
                                # with multiple makes, credit each at the
                                # longest (overestimate-safe fallback — see
                                # docstring).
                                player_stats[pname]["field_goals_made"] = [
                                    {"distance": long_fg}
                                ] * fg_made
                    except (ValueError, IndexError, ZeroDivisionError):
                        continue

        # -- scoringPlays: yardage detail for 40+/50+ TD distance bonuses
        # and 2pt conversions. Best-effort text parsing — if a play's text
        # doesn't match the expected pattern, that specific bonus is
        # skipped without affecting the base stats above. --
        for sp in summary.get("scoringPlays", []):
            type_text = sp.get("type", {}).get("text", "")
            text = sp.get("text", "") or ""

            yard_match = re.search(r"(\d+)\s*Yd", text)
            yards = int(yard_match.group(1)) if yard_match else None
            scorer_match = re.match(r"([A-Za-z.'\-]+(?:\s+[A-Za-z.'\-]+)*?)\s+\d+\s*Yd", text)
            scorer = scorer_match.group(1).strip() if scorer_match else None

            if yards is None or not scorer:
                continue

            if "Rushing Touchdown" in type_text:
                if yards >= 50:
                    _add_td(scorer, "rush_td_50")
                elif yards >= 40:
                    _add_td(scorer, "rush_td_40")
            elif "Passing Touchdown" in type_text:
                # scorer here is the receiver (named first in ESPN's text)
                if yards >= 50:
                    _add_td(scorer, "rec_td_50")
                elif yards >= 40:
                    _add_td(scorer, "rec_td_40")
                from_match = re.search(r"from\s+([A-Za-z.'\-]+(?:\s+[A-Za-z.'\-]+)*?)(\s*\(|$)", text)
                if from_match:
                    passer = from_match.group(1).strip()
                    if yards >= 50:
                        _add_td(passer, "pass_td_50")
                    elif yards >= 40:
                        _add_td(passer, "pass_td_40")
            elif "Two-Point" in type_text or "two-point" in text.lower():
                _add_td(scorer, "two_pt_conversions")

    return player_stats, team_stats


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
                        "UPDATE players SET nfl_team=?, headshot_url=? WHERE id=?"
                    ), (p["nfl_team"], p.get("headshot_url"), existing["id"]))
                    skipped += 1
                    continue

            conn.execute(adapt_sql(
                "INSERT INTO players (league_id, name, position, nfl_team, headshot_url) VALUES (?,?,?,?,?)"
            ), (league_id, p["name"], p["position"], p["nfl_team"], p.get("headshot_url")))
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
    fg_data = fetch_kicker_fg_data(season, raw_nfl_week, season_type="POST")
    player_bonus, team_bonus, _ = fetch_pbp_full_stats(season, raw_nfl_week, season_type="POST")
    # Mirrors main.py's PLAYOFF_WEEK_MAP (kept local to avoid a circular import,
    # since main.py imports from this module, not the other way around).
    _playoff_espn_week = {1: 1, 2: 2, 3: 3, 4: 5}
    points_allowed_map = fetch_points_allowed(
        season, _playoff_espn_week.get(fantasy_week, fantasy_week), season_type=3
    )

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

        bonus = player_bonus.get((row["player_id_nfl"], raw_nfl_week), {})

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
                        passing_tds=?, other_tds=?,
                        two_pt_conversions=?, pass_40_completions=?,
                        pass_td_40=?, pass_td_50=?, rush_td_40=?, rush_td_50=?,
                        rec_td_40=?, rec_td_50=?, pat_made=?, fg_missed=?,
                        fumbles_lost=?, interceptions=?, field_goals_json=?,
                        return_fumbles_lost=0
                    WHERE player_id=? AND week=?
                """), (
                    row["receptions"], row["receiving_yards"], row["rushing_yards"],
                    row["return_yards"], row["passing_yards"], row["total_tds"],
                    row["passing_tds"], row["other_tds"],
                    bonus.get("two_pt_conversions", 0), bonus.get("pass_40_completions", 0),
                    bonus.get("pass_td_40", 0), bonus.get("pass_td_50", 0),
                    bonus.get("rush_td_40", 0), bonus.get("rush_td_50", 0),
                    bonus.get("rec_td_40", 0), bonus.get("rec_td_50", 0),
                    bonus.get("pat_made", 0), bonus.get("fg_missed", 0),
                    row["fumbles_lost"], row["interceptions"], fg_json,
                    player_db_id, fantasy_week
                ))
            else:
                conn.execute(adapt_sql("""
                    INSERT INTO player_scores (
                        player_id, week, receptions, receiving_yards, rushing_yards,
                        return_yards, passing_yards, total_tds, passing_tds, other_tds,
                        two_pt_conversions, pass_40_completions,
                        pass_td_40, pass_td_50, rush_td_40, rush_td_50,
                        rec_td_40, rec_td_50, pat_made, fg_missed,
                        fumbles_lost, interceptions, field_goals_json, return_fumbles_lost
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                """), (
                    player_db_id, fantasy_week,
                    row["receptions"], row["receiving_yards"], row["rushing_yards"],
                    row["return_yards"], row["passing_yards"], row["total_tds"],
                    row["passing_tds"], row["other_tds"],
                    bonus.get("two_pt_conversions", 0), bonus.get("pass_40_completions", 0),
                    bonus.get("pass_td_40", 0), bonus.get("pass_td_50", 0),
                    bonus.get("rush_td_40", 0), bonus.get("rush_td_50", 0),
                    bonus.get("rec_td_40", 0), bonus.get("rec_td_50", 0),
                    bonus.get("pat_made", 0), bonus.get("fg_missed", 0),
                    row["fumbles_lost"], row["interceptions"], fg_json
                ))
            updated += 1

        except Exception as e:
            logger.error(f"Error upserting player {player_db_id} fantasy_week {fantasy_week}: {e}")
            errors += 1

    # -- DST auto-sync: sacks, safeties, forced fumbles, blocked kicks, and
    # points allowed. This previously had no automated sync at all — DST
    # scoring depended entirely on manual entry. Return yards / return
    # fumbles lost remain manual-only (not covered by PBP defteam data here),
    # so this only touches the new automated columns. --
    dst_rows = conn.execute(adapt_sql(
        "SELECT id, nfl_team FROM players WHERE league_id=? AND position='DST'"
    ), (league_id,)).fetchall()

    for dst in dst_rows:
        team = dst["nfl_team"]
        tb = team_bonus.get((team, raw_nfl_week), {})
        pa = points_allowed_map.get(team)
        try:
            existing = conn.execute(adapt_sql(
                "SELECT id, override_points FROM player_scores WHERE player_id=? AND week=?"
            ), (dst["id"], fantasy_week)).fetchone()

            if existing:
                if existing["override_points"] is not None:
                    skipped += 1
                    continue
                conn.execute(adapt_sql("""
                    UPDATE player_scores SET
                        sacks=?, safeties=?, forced_fumbles=?, blocked_kicks=?,
                        points_allowed=?
                    WHERE player_id=? AND week=?
                """), (
                    tb.get("sacks", 0), tb.get("safeties", 0),
                    tb.get("forced_fumbles", 0), tb.get("blocked_kicks", 0),
                    pa,
                    dst["id"], fantasy_week
                ))
            else:
                conn.execute(adapt_sql("""
                    INSERT INTO player_scores (
                        player_id, week, sacks, safeties, forced_fumbles,
                        blocked_kicks, points_allowed
                    ) VALUES (?,?,?,?,?,?,?)
                """), (
                    dst["id"], fantasy_week,
                    tb.get("sacks", 0), tb.get("safeties", 0),
                    tb.get("forced_fumbles", 0), tb.get("blocked_kicks", 0),
                    pa
                ))
            updated += 1
        except Exception as e:
            logger.error(f"Error upserting DST {dst['id']} fantasy_week {fantasy_week}: {e}")
            errors += 1

    conn.commit()
    conn.close()

    logger.info(
        f"[league {league_id}] Week {fantasy_week} (NFL wk {raw_nfl_week}) sync: "
        f"updated={updated} skipped={skipped} errors={errors}"
    )
    return {"updated": updated, "skipped": skipped, "errors": errors,
            "week": week, "season": season}


_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _strip_suffix(parts: list[str]) -> list[str]:
    """Drop a trailing generational suffix (Jr./Sr./II/III/IV) if present."""
    if parts and parts[-1].lower().rstrip(".") in _NAME_SUFFIXES:
        return parts[:-1]
    return parts


def _normalize_full_name(name: str) -> str:
    """
    Normalize a full display name for exact matching (used against ESPN's
    box score data, which uses full names like 'Adam Prentice' — the same
    format survivor_players.name is stored in, unlike PBP's short-name
    format). Strips suffixes and lowercases, so 'Anthony Richardson Sr.'
    matches a roster entry stored as 'Anthony Richardson'.
    """
    if not name or not name.strip():
        return ""
    parts = _strip_suffix(name.strip().split())
    return " ".join(parts).lower()


def _normalize_name_for_match(name: str) -> tuple[str, str]:
    """Return (first_initial, last_name_lower) for fuzzy player matching."""
    if not name or not name.strip():
        return ("", "")
    name = name.strip()
    if "." in name and len(name.split(".", 1)[0]) <= 2:
        # PBP short-name format: "P.Mahomes"
        first_part, _, last = name.partition(".")
        return (first_part.strip()[:1].upper(), last.strip().lower())
    # Full name format: "Patrick Mahomes" (strip Jr./Sr./III etc. first)
    parts = _strip_suffix(name.split())
    if len(parts) >= 2:
        return (parts[0][:1].upper(), parts[-1].strip().lower())
    return ("", name.strip().lower())


def sync_survivor_week(league_id: int, week: int, season: int) -> dict:
    """
    Auto-sync one week of a Survivor league's player stats into
    survivor_player_scores.

    Tries nflverse's play-by-play data first (fetch_pbp_full_stats) — this
    is the richer source and the one that will apply automatically once
    nflverse publishes data for the season (their pipeline currently only
    starts once the regular season officially begins, so preseason and
    early-season weeks have nothing there yet: confirmed via direct HTTP
    check, play_by_play_2026.parquet 404s while play_by_play_2025.parquet
    exists). When nflverse has nothing, this automatically falls back to
    ESPN's game-summary endpoints (fetch_espn_week_stats) instead — no
    manual switch-over needed once nflverse data arrives; this file just
    starts using it again on its own.

    Regular-season leagues: filtered directly by week number + season_type
    ('REG' for nflverse / seasontype=2 for ESPN).
    Preseason leagues: nflverse path filters by actual game date (pulled
    from this league's own synced schedule) rather than trusting nflverse's
    internal preseason week-numbering, which hasn't been independently
    verified the way the ESPN-side +1 week offset was; the ESPN fallback
    path uses that verified offset directly.

    Player matching: exact full-name match (after stripping suffixes like
    Jr./Sr./III) is tried first — this is what ESPN's box score data uses
    and matches survivor_players.name directly. Falls back to fuzzy
    first-initial + last-name matching (needed for nflverse's short-name
    PBP data, e.g. "P.Mahomes") when an exact match isn't found.

    Known gap on the ESPN fallback path specifically: forced fumbles,
    safety, and blocked kicks aren't in ESPN's box score data at all and
    stay at 0 — would need manual entry if one occurs during a week
    that's using the ESPN fallback (i.e. before nflverse has real data).
    """
    import sqlite3
    from datetime import datetime, timedelta

    conn = sqlite3.connect(_os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db"))
    conn.row_factory = sqlite3.Row
    added = skipped = errors = 0

    league_row = conn.execute(
        "SELECT season_type FROM survivor_leagues WHERE id=?", (league_id,)
    ).fetchone()
    is_preseason = bool(league_row and league_row["season_type"] == "preseason")

    if is_preseason:
        espn_week = week + 1  # ESPN counts the Hall of Fame Game as preseason "Week 1"
        espn_season_type = 1
    else:
        espn_week = week
        espn_season_type = 2

    # -- Try nflverse first --
    player_stats: dict = {}
    team_stats: dict = {}
    id_to_name: dict = {}
    source = None
    try:
        if is_preseason:
            sched = conn.execute(
                "SELECT MIN(kickoff_utc) as start, MAX(kickoff_utc) as end "
                "FROM survivor_game_schedule WHERE league_id=? AND week=?",
                (league_id, week)
            ).fetchone()
            if sched and sched["start"]:
                start_dt = datetime.fromisoformat(sched["start"]) - timedelta(days=1)
                end_dt = datetime.fromisoformat(sched["end"]) + timedelta(days=1)
                date_range = (start_dt.date().isoformat(), end_dt.date().isoformat())
                player_stats, team_stats, id_to_name = fetch_pbp_full_stats(
                    season, date_range=date_range, output_week=week, season_type="PRE"
                )
        else:
            player_stats, team_stats, id_to_name = fetch_pbp_full_stats(
                season, week=week, season_type="REG"
            )
        if any(w == week for (_k, w) in player_stats.keys()):
            source = "nflverse"
    except Exception as e:
        logger.warning(
            f"[survivor] nflverse fetch failed for season={season} week={week} "
            f"(likely not published yet for this season): {e}"
        )

    # -- Fall back to ESPN if nflverse had nothing --
    if not source:
        logger.info(
            f"[survivor] No nflverse data for league={league_id} week={week} — "
            f"using ESPN box-score fallback instead"
        )
        espn_players, espn_teams = fetch_espn_week_stats(
            season, espn_week, season_type=espn_season_type
        )
        player_stats = {(name, week): stats for name, stats in espn_players.items()}
        team_stats = {(team, week): stats for team, stats in espn_teams.items()}
        id_to_name = {name: name for name in espn_players}
        source = "espn"

    points_allowed_map = fetch_points_allowed(season, espn_week, season_type=espn_season_type)

    # Build match indexes from this league's player pool. Exact full-name
    # match is tried first (works directly against ESPN's full-name data);
    # fuzzy first-initial+last-name is the fallback (needed for nflverse's
    # short-name PBP data).
    players = conn.execute(
        "SELECT id, name, position, nfl_team FROM survivor_players WHERE league_id=?",
        (league_id,)
    ).fetchall()
    by_exact_name: dict[str, list] = {}
    by_last_name: dict[str, list] = {}
    for p in players:
        by_exact_name.setdefault(_normalize_full_name(p["name"]), []).append(p)
        _, last = _normalize_name_for_match(p["name"])
        by_last_name.setdefault(last, []).append(p)

    def _match_player(pkey):
        name = id_to_name.get(pkey)
        if not name:
            return None
        exact = by_exact_name.get(_normalize_full_name(name), [])
        if len(exact) == 1:
            return exact[0]
        first_initial, last = _normalize_name_for_match(name)
        candidates = by_last_name.get(last, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        for p in candidates:
            p_first, _ = _normalize_name_for_match(p["name"])
            if p_first == first_initial:
                return p
        return None  # ambiguous — multiple same-last-name players, skip rather than guess

    # -- Offensive players --
    for (pkey, w), stats in player_stats.items():
        if w != week:
            continue
        player = _match_player(pkey)
        if not player:
            skipped += 1
            continue
        try:
            existing = conn.execute(
                "SELECT id, override_points FROM survivor_player_scores WHERE player_id=? AND week=?",
                (player["id"], week)
            ).fetchone()
            if existing and existing["override_points"] is not None:
                skipped += 1
                continue

            fg_json = json.dumps(stats.get("field_goals_made", []))
            passing_tds = stats.get("passing_tds", 0)
            other_tds = stats.get("other_tds", 0)

            fields = {
                "receptions": stats.get("receptions", 0),
                "receiving_yards": stats.get("receiving_yards", 0),
                "rushing_yards": stats.get("rushing_yards", 0),
                "passing_yards": stats.get("passing_yards", 0),
                "total_tds": passing_tds + other_tds,
                "passing_tds": passing_tds,
                "other_tds": other_tds,
                "fumbles_lost": stats.get("fumbles_lost", 0),
                "interceptions": stats.get("interceptions", 0),
                "two_pt_conversions": stats.get("two_pt_conversions", 0),
                "pass_40_completions": stats.get("pass_40_completions", 0),
                "pass_td_40": stats.get("pass_td_40", 0),
                "pass_td_50": stats.get("pass_td_50", 0),
                "rush_td_40": stats.get("rush_td_40", 0),
                "rush_td_50": stats.get("rush_td_50", 0),
                "rec_td_40": stats.get("rec_td_40", 0),
                "rec_td_50": stats.get("rec_td_50", 0),
                "pat_made": stats.get("pat_made", 0),
                "fg_missed": stats.get("fg_missed", 0),
                "field_goals_json": fg_json,
            }
            if existing:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE survivor_player_scores SET {set_clause} WHERE player_id=? AND week=?",
                    (*fields.values(), player["id"], week)
                )
            else:
                cols = ", ".join(["player_id", "week"] + list(fields.keys()))
                placeholders = ", ".join(["?"] * (2 + len(fields)))
                conn.execute(
                    f"INSERT INTO survivor_player_scores ({cols}) VALUES ({placeholders})",
                    (player["id"], week, *fields.values())
                )
            added += 1
        except Exception as e:
            logger.error(f"[survivor] sync error player_key={pkey} league={league_id}: {e}")
            errors += 1

    # -- DST (team defense) rows --
    dst_players = [p for p in players if p["position"] == "DST"]
    for dst in dst_players:
        team = dst["nfl_team"]
        tb = team_stats.get((team, week), {})
        pa = points_allowed_map.get(team)
        try:
            existing = conn.execute(
                "SELECT id, override_points FROM survivor_player_scores WHERE player_id=? AND week=?",
                (dst["id"], week)
            ).fetchone()
            if existing and existing["override_points"] is not None:
                skipped += 1
                continue
            if existing:
                conn.execute(
                    "UPDATE survivor_player_scores SET sacks=?, safeties=?, "
                    "forced_fumbles=?, blocked_kicks=?, points_allowed=? "
                    "WHERE player_id=? AND week=?",
                    (tb.get("sacks", 0), tb.get("safeties", 0),
                     tb.get("forced_fumbles", 0), tb.get("blocked_kicks", 0),
                     pa, dst["id"], week)
                )
            else:
                conn.execute(
                    "INSERT INTO survivor_player_scores "
                    "(player_id, week, sacks, safeties, forced_fumbles, blocked_kicks, points_allowed) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (dst["id"], week, tb.get("sacks", 0), tb.get("safeties", 0),
                     tb.get("forced_fumbles", 0), tb.get("blocked_kicks", 0), pa)
                )
            added += 1
        except Exception as e:
            logger.error(f"[survivor] DST sync error team={team} league={league_id}: {e}")
            errors += 1

    conn.commit()
    conn.close()
    return {"added": added, "skipped": skipped, "errors": errors,
            "week": week, "season": season, "source": source}


# ──────────────────────────────────────────────────────────────────────────────
# Background scheduler
# ──────────────────────────────────────────────────────────────────────────────

class NFLSyncScheduler:
    """
    Runs a background thread that syncs stats for all leagues on a smart schedule.

    - During NFL game windows: syncs every LIVE_INTERVAL seconds
    - Outside game windows:    syncs every IDLE_INTERVAL seconds
    - Thread is daemonized — it dies when the main process exits

    Generic/reusable: pass sync_fn / get_active_league_ids_fn / get_week_fn
    to point this at a different app's sync pipeline (e.g. the Survivor
    app). All default to the main Playoff Challenge app's behavior, so
    existing callers (`NFLSyncScheduler()`) are unaffected.

    Usage:
        scheduler = NFLSyncScheduler()
        scheduler.start()
        # app runs...
        scheduler.stop()
    """

    def __init__(self, sync_fn=None, get_active_league_ids_fn=None,
                 get_week_fn=None, thread_name="nfl-sync"):
        self._sync_fn = sync_fn or (lambda lid, wk, season: sync_week(lid, wk, season))
        self._get_active_league_ids_fn = get_active_league_ids_fn or self._default_get_active_league_ids
        # get_week_fn(league_id) -> int. Defaults to the global current week
        # (ignoring league_id), matching the main app where all leagues
        # share one NFL week. Apps where leagues can be on different weeks
        # (e.g. Survivor, where preseason vs regular-season leagues track
        # their own current_week) should pass a per-league lookup instead.
        self._get_week_fn = get_week_fn or (lambda league_id: current_nfl_week())
        self._stop_event = threading.Event()
        self._thread     = threading.Thread(target=self._loop, daemon=True, name=thread_name)
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

                # Sync regardless of window — just less often outside one.
                # (Previously this only ran inside recognized game windows,
                # which are hardcoded to Sun/Mon/Thu — any game on a day
                # outside that list, e.g. a Friday or Saturday preseason
                # game, meant zero syncs all day, not just less-frequent
                # ones.)
                league_ids = self._get_active_league_ids_fn()
                for lid in league_ids:
                    if self._stop_event.is_set():
                        break
                    last = self._last_sync.get(lid)
                    if last is None or (datetime.now(timezone.utc) - last).total_seconds() >= interval:
                        self._sync_league(lid)

                self._stop_event.wait(timeout=interval)

            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                self._stop_event.wait(timeout=60)

    def _default_get_active_league_ids(self) -> list[int]:
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
        weeks = self._get_week_fn(league_id)
        if isinstance(weeks, int):
            weeks = [weeks]
        weeks = [w for w in weeks if 1 <= w <= 18]
        if not weeks:
            return {"message": "Off-season — no sync needed"}

        results = []
        for week in weeks:
            try:
                result = self._sync_fn(league_id, week, CURRENT_SEASON)
                result["synced_at"] = datetime.now(timezone.utc).isoformat()
                result["week"] = week
                results.append(result)
            except Exception as e:
                logger.error(f"sync_league {league_id} week {week} failed: {e}")
                results.append({"error": str(e), "week": week,
                                 "synced_at": datetime.now(timezone.utc).isoformat()})

        self._last_sync[league_id] = datetime.now(timezone.utc)
        if len(results) == 1:
            # Preserve the original flat shape for single-week syncs (what
            # the main app's manage page displays: .week, .updated, .error
            # etc. directly on the status dict) — only multi-week syncs
            # (Survivor) get the new wrapped shape.
            self._last_status[league_id] = results[0]
            return results[0]

        summary = {"weeks_synced": [r.get("week") for r in results], "results": results}
        self._last_status[league_id] = summary
        return summary


# Module-level singleton — imported by main.py
sync_scheduler = NFLSyncScheduler()


# ──────────────────────────────────────────────────────────────────────────────
# Sleeper-based lineup-page projections (Survivor app)
# ──────────────────────────────────────────────────────────────────────────────
#
# The lineup page previously displayed Sleeper's own pre-computed pts_ppr
# value directly, which reflects Sleeper's generic PPR formula — completely
# disconnected from a league's actual custom scoring_settings. On top of
# that, the old client-side JS matched Sleeper's player_id directly against
# our own internal survivor_players.id, which are two unrelated numbering
# systems with no guaranteed correspondence.
#
# This rebuilds it correctly: fetch Sleeper's raw *stat* projections (not
# just their computed points), match players by name (the one thing both
# systems share) against this league's actual roster, then run the matched
# stats through this app's own scoring engine using the league's real
# scoring_settings.
#
# Known limitations (documented, not silently guessed at):
#   - 50+ yard bonus tiers aren't projectable — Sleeper's projections
#     include a 40+ yard completion/TD field but nothing distinguishing
#     50+ specifically, since no projection system predicts individual
#     play distances beyond that.
#   - Kicker FG scoring is distance-based in this app, but a projection
#     can only give an expected number of makes, not each kick's
#     distance — approximated using a league-average made-FG distance.
#   - Defense/special-teams projections (sacks, safety, forced fumbles,
#     blocked kicks, points allowed) aren't reliably available from
#     Sleeper's free projections endpoint and are left at 0 — a real gap,
#     not a wrong guess presented as a real number.

_SLEEPER_PLAYER_DIRECTORY_CACHE: dict = {"data": None, "fetched_at": None}
_SLEEPER_ASSUMED_AVG_FG_DISTANCE = 38  # league-average made-FG distance, for approximating FG scoring from a projected FG-make count


def fetch_sleeper_player_directory() -> dict:
    """
    Return {sleeper_player_id: {"name": str, "team": str, "position": str}}.
    This is a large (~5MB), slow-changing file — Sleeper's own docs say not
    to fetch it more than once a day, so this is cached in-process.
    """
    cache = _SLEEPER_PLAYER_DIRECTORY_CACHE
    if cache["data"] is not None and cache["fetched_at"] is not None:
        if (datetime.now(timezone.utc) - cache["fetched_at"]).total_seconds() < 86400:
            return cache["data"]

    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "20", "https://api.sleeper.app/v1/players/nfl"],
            capture_output=True, text=True, timeout=25
        )
        if result.returncode != 0 or not result.stdout:
            return cache["data"] or {}
        raw = json.loads(result.stdout)
    except Exception as e:
        logger.error(f"Failed to fetch Sleeper player directory: {e}")
        return cache["data"] or {}

    directory = {}
    for pid, p in raw.items():
        full_name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not full_name:
            continue
        directory[pid] = {
            "name": full_name,
            "team": p.get("team") or "",
            "position": p.get("position") or "",
        }

    cache["data"] = directory
    cache["fetched_at"] = datetime.now(timezone.utc)
    return directory


def fetch_sleeper_projections(season: int, week: int) -> dict:
    """
    Return {sleeper_player_id: raw_stats_dict} — the raw per-stat-category
    projections (not Sleeper's own pre-computed points), for regular season.
    """
    import subprocess
    url = f"https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular"
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "20", url],
            capture_output=True, text=True, timeout=25
        )
        if result.returncode != 0 or not result.stdout:
            return {}
        data = json.loads(result.stdout)
    except Exception as e:
        logger.error(f"Failed to fetch Sleeper projections: {e}")
        return {}

    projections = {}
    for entry in data:
        pid = entry.get("player_id")
        stats = entry.get("stats")
        if not pid or not stats:
            continue
        projections[pid] = stats
    return projections


def _sleeper_stats_to_our_format(stats: dict) -> dict:
    """Map Sleeper's raw projection stat field names onto this app's own
    stat dict format (the same shape calculate_fantasy_points expects)."""

    def g(key, default=0):
        v = stats.get(key, default)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    rush_td = g("rush_td")
    rec_td = g("rec_td")
    other_tds = rush_td + rec_td

    two_pt = g("pass_2pt") + g("rush_2pt") + g("rec_2pt")

    return {
        "receptions": g("rec"),
        "receiving_yards": g("rec_yd"),
        "rushing_yards": g("rush_yd"),
        "passing_yards": g("pass_yd"),
        "passing_tds": g("pass_td"),
        "other_tds": other_tds,
        "fumbles_lost": g("fum_lost"),
        "interceptions": g("pass_int"),
        "two_pt_conversions": two_pt,
        "pass_40_completions": g("pass_cmp_40p"),
        "pass_td_40": g("pass_td_40p"),
        "pass_td_50": 0,      # not distinguishable from projections — see module docstring
        "rush_td_40": 0,      # no equivalent Sleeper field confirmed available
        "rush_td_50": 0,
        "rec_td_40": 0,
        "rec_td_50": 0,
        "pat_made": g("xp_made", g("xpm")),
        "fg_missed": g("fgmiss", g("fg_miss")),
        "field_goals_made": (
            [{"distance": _SLEEPER_ASSUMED_AVG_FG_DISTANCE}] * int(g("fgm"))
            if g("fgm") > 0 else []
        ),
        # Defense/special-teams categories intentionally left at 0 — not
        # reliably available from this data source. See module docstring.
        "return_yards": 0,
        "sacks": 0,
        "safeties": 0,
        "forced_fumbles": 0,
        "blocked_kicks": 0,
        "points_allowed": None,
    }


def compute_survivor_lineup_projections(league_id: int, week: int, season: int) -> dict:
    """
    Return {our_player_id: projected_points}, computed using this league's
    actual scoring_settings — the real fix for the old Sleeper-pts_ppr /
    mismatched-ID lineup-page projections.
    """
    import sqlite3
    conn = sqlite3.connect(_os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db"))
    conn.row_factory = sqlite3.Row

    league_row = conn.execute(
        "SELECT scoring_settings FROM survivor_leagues WHERE id=?", (league_id,)
    ).fetchone()
    try:
        overrides = json.loads(league_row["scoring_settings"]) if league_row and league_row["scoring_settings"] else None
    except Exception:
        overrides = None
    from scoring import resolve_scoring_settings, calculate_fantasy_points
    settings = resolve_scoring_settings(overrides)

    players = conn.execute(
        "SELECT id, name, position FROM survivor_players WHERE league_id=?", (league_id,)
    ).fetchall()
    conn.close()

    directory = fetch_sleeper_player_directory()
    projections = fetch_sleeper_projections(season, week)
    if not directory or not projections:
        return {}

    # Build a name-match index from Sleeper's directory, scoped to players
    # who actually have a projection this week (avoids matching against
    # inactive/irrelevant entries in the ~11k-player directory).
    by_last_name: dict[str, list] = {}
    for sid, stats in projections.items():
        info = directory.get(sid)
        if not info:
            continue
        _, last = _normalize_name_for_match(info["name"])
        by_last_name.setdefault(last, []).append((sid, info))

    results = {}
    for p in players:
        exact_norm = _normalize_full_name(p["name"])
        first_initial, last = _normalize_name_for_match(p["name"])
        candidates = by_last_name.get(last, [])
        if not candidates:
            continue

        match = None
        if len(candidates) == 1:
            match = candidates[0]
        else:
            exact_matches = [c for c in candidates if _normalize_full_name(c[1]["name"]) == exact_norm]
            if len(exact_matches) == 1:
                match = exact_matches[0]
            else:
                initial_matches = [c for c in candidates if _normalize_name_for_match(c[1]["name"])[0] == first_initial]
                if len(initial_matches) == 1:
                    match = initial_matches[0]
        if not match:
            continue  # ambiguous — skip rather than guess wrong

        sid, info = match
        stats = _sleeper_stats_to_our_format(projections[sid])
        pos = p["position"].upper()
        pts = calculate_fantasy_points({"pos": pos, "multiplier": None}, stats, settings)
        results[p["id"]] = round(pts, 1)

    return results
