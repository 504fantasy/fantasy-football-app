"""
One-time backfill: populate survivor_players.espn_id and .sleeper_id for
every existing player, by matching team + full name against ESPN's team
rosters and Sleeper's player directory.

Matching on team+name together (rather than name alone) safely
disambiguates cases where two different real players share a full name
(e.g. two different "Justin Jefferson"s across the league) -- within a
single team, a full-name collision is exceptionally rare, unlike across
the whole NFL where thousands of players make surname collisions common.

DST rows are skipped entirely -- ESPN's roster endpoint only lists
individual athletes, not "team defense" as its own entity, so there's no
espn_id that applies to a DST row the same way. DST already matches
safely by team abbreviation and doesn't need this.

Usage:
    ./venv/bin/python3 backfill_player_ids.py --dry-run   # preview only, no writes
    ./venv/bin/python3 backfill_player_ids.py             # actually apply
"""
import sys
import sqlite3
import argparse

sys.path.insert(0, ".")
import nfl_sync


def normalize_team(team, team_map):
    team = (team or "").upper()
    return team_map.get(team, team)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview results without writing to the database")
    args = parser.parse_args()

    import os
    conn = sqlite3.connect(os.environ.get("SURVIVOR_DB_PATH", "data/survivor.db"))
    conn.row_factory = sqlite3.Row

    print("Fetching ESPN team rosters...")
    espn_rosters = nfl_sync.fetch_espn_team_rosters()
    print(f"  -> {len(espn_rosters)} teams fetched")
    if len(espn_rosters) < 32:
        print("  WARNING: expected 32 teams -- some team roster fetches may have failed. Check the logs above.")

    print("Fetching Sleeper player directory...")
    sleeper_directory = nfl_sync.fetch_sleeper_player_directory()
    print(f"  -> {len(sleeper_directory)} players in directory")

    # Sleeper's directory is keyed by sleeper_id, not name -- build a
    # (team, normalized_name) -> [sleeper_id, ...] index so we can look
    # players up the same way we look up ESPN's roster data.
    sleeper_by_team_name: dict = {}
    for sid, info in sleeper_directory.items():
        team = normalize_team(info.get("team"), nfl_sync.TEAM_MAP)
        name_norm = nfl_sync._normalize_full_name(info.get("name", ""))
        if not name_norm:
            continue
        sleeper_by_team_name.setdefault((team, name_norm), []).append(sid)

    # Same normalization for ESPN's roster data, keyed by (team, name) too,
    # so a mismatched abbreviation (LA/LAR, LV/LAS, JAX/JAC) on either side
    # doesn't cause a real match to be missed.
    espn_by_team_name: dict = {}
    for team, players in espn_rosters.items():
        team_norm = normalize_team(team, nfl_sync.TEAM_MAP)
        for name, eid in players.items():
            name_norm = nfl_sync._normalize_full_name(name)
            espn_by_team_name.setdefault((team_norm, name_norm), []).append(eid)

    # Global (team-agnostic) name indexes, for the fallback below --
    # scoped by team catches same-surname collisions like the two
    # different Justin Jeffersons, but is too strict when OUR OWN
    # roster's team field has simply gone stale (a player traded,
    # released, or signed elsewhere since they were originally added).
    # A name that's unique across the ENTIRE dataset poses no real
    # collision risk regardless of which team is on file for them.
    sleeper_by_name_global: dict = {}
    for sid, info in sleeper_directory.items():
        name_norm = nfl_sync._normalize_full_name(info.get("name", ""))
        if not name_norm:
            continue
        sleeper_by_name_global.setdefault(name_norm, []).append(sid)

    espn_by_name_global: dict = {}
    for team, players in espn_rosters.items():
        for name, eid in players.items():
            name_norm = nfl_sync._normalize_full_name(name)
            espn_by_name_global.setdefault(name_norm, []).append(eid)

    players = conn.execute(
        "SELECT id, league_id, name, position, nfl_team, espn_id, sleeper_id FROM survivor_players"
    ).fetchall()
    print(f"\nTotal players in database: {len(players)}")

    stats = {"both": 0, "espn_only": 0, "sleeper_only": 0, "neither": 0, "dst_skipped": 0, "already_set": 0}
    unmatched = []
    ambiguous = []
    stale_team = []

    for p in players:
        if p["espn_id"] is not None and p["sleeper_id"] is not None:
            stats["already_set"] += 1
            continue

        if p["position"].upper() == "DST":
            stats["dst_skipped"] += 1
            continue

        team_norm = normalize_team(p["nfl_team"], nfl_sync.TEAM_MAP)
        name_norm = nfl_sync._normalize_full_name(p["name"])

        espn_matches = espn_by_team_name.get((team_norm, name_norm), [])
        sleeper_matches = sleeper_by_team_name.get((team_norm, name_norm), [])

        if len(espn_matches) > 1 or len(sleeper_matches) > 1:
            ambiguous.append((p["id"], p["name"], p["position"], p["nfl_team"], len(espn_matches), len(sleeper_matches)))

        espn_id = espn_matches[0] if len(espn_matches) == 1 else None
        sleeper_id = sleeper_matches[0] if len(sleeper_matches) == 1 else None
        used_fallback = False

        # Fallback: if the team-scoped match came up empty (not
        # ambiguous -- genuinely zero candidates), try a name-only
        # match instead, but only trust it if that name is globally
        # unique. This is what catches "our roster says MIN, but
        # they're actually on CAR now" cases like Adam Thielen, without
        # reopening the door to genuine same-name collisions like the
        # two different Justin Jeffersons (that case has 2+ global
        # candidates, so it correctly stays unresolved here too).
        if espn_id is None and len(espn_matches) == 0:
            global_matches = espn_by_name_global.get(name_norm, [])
            if len(global_matches) == 1:
                espn_id = global_matches[0]
                used_fallback = True
        if sleeper_id is None and len(sleeper_matches) == 0:
            global_matches = sleeper_by_name_global.get(name_norm, [])
            if len(global_matches) == 1:
                sleeper_id = global_matches[0]
                used_fallback = True

        if used_fallback:
            stale_team.append((p["id"], p["name"], p["position"], p["nfl_team"]))

        if espn_id and sleeper_id:
            stats["both"] += 1
        elif espn_id:
            stats["espn_only"] += 1
        elif sleeper_id:
            stats["sleeper_only"] += 1
        else:
            stats["neither"] += 1
            unmatched.append((p["id"], p["name"], p["position"], p["nfl_team"]))
            continue

        if not args.dry_run:
            conn.execute(
                "UPDATE survivor_players SET espn_id=COALESCE(?, espn_id), sleeper_id=COALESCE(?, sleeper_id) WHERE id=?",
                (espn_id, sleeper_id, p["id"])
            )

    if not args.dry_run:
        conn.commit()

    print()
    print("=== DRY RUN -- NOTHING WAS WRITTEN ===" if args.dry_run else "=== BACKFILL APPLIED ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    if ambiguous:
        print(f"\n=== {len(ambiguous)} AMBIGUOUS MATCHES (multiple candidates even with team -- skipped, not guessed) ===")
        for pid, name, pos, team, n_espn, n_sleeper in ambiguous:
            print(f"  id={pid} {name} ({pos}, {team}) -- {n_espn} ESPN candidates, {n_sleeper} Sleeper candidates")

    if stale_team:
        print(f"\n=== {len(stale_team)} MATCHED VIA NAME ONLY (roster's team field looks stale -- worth a separate update) ===")
        for pid, name, pos, team in stale_team:
            print(f"  id={pid} {name} ({pos}, {team} on file)")

    if unmatched:
        print(f"\n=== {len(unmatched)} PLAYERS COULD NOT BE MATCHED AT ALL (review manually) ===")
        for pid, name, pos, team in unmatched:
            print(f"  id={pid} {name} ({pos}, {team})")

    conn.close()


if __name__ == "__main__":
    main()
