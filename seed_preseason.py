"""
2026 NFL Preseason Schedule Seeder
====================================
Source: Official NFL preseason schedule release (media.nfl.com), team sites, CBS Sports.
All times are Eastern Time (ET) as published, converted to UTC (+4h for August EDT).

HOW TO UPDATE THIS LATER:
  - Some games show time=None below because the NFL listed them as "TBD" at publish time.
  - When the real times are announced, just fill in the "TBD" entries with the actual
    "HH:MM" (24-hr ET) and re-run this script with overwrite=True.
  - Run via: python3 seed_preseason.py <league_id>

Game tuple format: (week, gameday "YYYY-MM-DD", gametime_et "HH:MM" or "TBD", away_team, home_team)
Team abbreviations match the same set used in nfl_data_py (ARI, ATL, BAL, BUF, CAR, CHI,
CIN, CLE, DAL, DEN, DET, GB, HOU, IND, JAX, KC, LV, LAC, LAR, MIA, MIN, NE, NO, NYG, NYJ,
PHI, PIT, SF, SEA, TB, TEN, WAS).
"""

PRESEASON_GAMES = [
    # ── Hall of Fame Game ──
    (1, "2026-08-06", "20:00", "CAR", "ARI"),

    # ── Week 1 ──
    (1, "2026-08-13", "19:00", "DET", "CIN"),
    (1, "2026-08-13", "19:00", "GB",  "PIT"),
    (1, "2026-08-13", "19:30", "IND", "NE"),
    (1, "2026-08-13", "20:00", "LAC", "HOU"),
    (1, "2026-08-13", "20:00", "ARI", "LV"),
    (1, "2026-08-13", "21:00", "TEN", "SF"),
    (1, "2026-08-14", "19:00", "DEN", "ATL"),
    (1, "2026-08-14", "19:00", "TB",  "NYJ"),
    (1, "2026-08-14", "19:00", "MIA", "WAS"),
    (1, "2026-08-15", "13:00", "CAR", "BUF"),
    (1, "2026-08-15", "13:00", "CLE", "CHI"),
    (1, "2026-08-15", "13:00", "MIN", "NYG"),
    (1, "2026-08-15", "16:00", "LAR", "KC"),
    (1, "2026-08-15", "16:00", "JAX", "NO"),
    (1, "2026-08-15", "19:00", "PHI", "BAL"),
    (1, "2026-08-15", "20:00", "DAL", "SEA"),

    # ── Week 2 ──
    (2, "2026-08-20", "20:00", "LV",  "HOU"),
    (2, "2026-08-20", "22:00", "SF",  "LAC"),
    (2, "2026-08-21", "19:00", "NYJ", "PIT"),
    (2, "2026-08-21", "19:30", "CAR", "JAX"),
    (2, "2026-08-21", "21:00", "GB",  "DEN"),
    (2, "2026-08-22", "12:00", "WAS", "DET"),
    (2, "2026-08-22", "13:00", "BUF", "CLE"),
    (2, "2026-08-22", "13:00", "ATL", "IND"),
    (2, "2026-08-22", "13:00", "BAL", "MIN"),
    (2, "2026-08-22", "16:00", "NO",  "LAR"),
    (2, "2026-08-22", "16:00", "NYG", "MIA"),
    (2, "2026-08-22", "19:00", "CHI", "CIN"),
    (2, "2026-08-22", "19:00", "PHI", "NE"),
    (2, "2026-08-22", "19:30", "KC",  "TB"),
    (2, "2026-08-22", "22:00", "DAL", "ARI"),
    (2, "2026-08-23", "20:00", "SEA", "TEN"),

    # ── Week 3 ──
    (3, "2026-08-27", "19:00", "PIT", "BUF"),
    (3, "2026-08-27", "20:00", "NE",  "CLE"),
    (3, "2026-08-27", "20:00", "SF",  "LV"),
    (3, "2026-08-27", "22:00", "LAR", "LAC"),
    (3, "2026-08-28", "18:00", "WAS", "BAL"),
    (3, "2026-08-28", "19:00", "HOU", "CAR"),
    (3, "2026-08-28", "19:00", "ATL", "MIA"),
    (3, "2026-08-28", "19:30", "TB",  "JAX"),
    # Remaining Week 3 games confirmed via team sources but kickoff TBD at publish time:
    (3, "2026-08-29", "TBD",  "CHI", "TEN"),
    (3, "2026-08-29", "TBD",  "GB",  "NYJ"),
    (3, "2026-08-29", "TBD",  "MIN", "DEN"),
    (3, "2026-08-29", "TBD",  "ARI", "PIT"),  # Cardinals 4th game (extra due to HOF Game)
    (3, "2026-08-28", "TBD",  "IND", "DET"),
    (3, "2026-08-28", "TBD",  "NO",  "TB"),
]


def seed_preseason_schedule(league_id: int, overwrite: bool = False):
    """
    Insert/update preseason kickoff times into survivor_game_schedule for the
    given league. ET times are converted to UTC by adding 4 hours (EDT in August).
    Games marked "TBD" are skipped until a real time is filled in above.
    """
    import sqlite3
    from datetime import datetime, timedelta

    conn = sqlite3.connect('/root/fantasy-football-app/data/survivor.db')
    added = skipped = tbd_skipped = 0

    for week, gameday, gametime_et, away, home in PRESEASON_GAMES:
        if gametime_et == "TBD":
            tbd_skipped += 1
            continue
        try:
            naive_et = datetime.strptime(f"{gameday} {gametime_et}", "%Y-%m-%d %H:%M")
            kickoff_utc = (naive_et + timedelta(hours=4)).isoformat()  # EDT -> UTC
        except Exception:
            continue

        for team in (away, home):
            try:
                conn.execute(
                    "INSERT INTO survivor_game_schedule (league_id, season, week, team, kickoff_utc) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(league_id, week, team) DO " +
                    ("UPDATE SET kickoff_utc=excluded.kickoff_utc" if overwrite else "NOTHING"),
                    (league_id, 2026, week, team, kickoff_utc)
                )
                added += 1
            except Exception:
                skipped += 1

    conn.commit()
    conn.close()
    print(f"Preseason schedule seed: added/updated={added} skipped={skipped} tbd_pending={tbd_skipped}")
    return {"added": added, "skipped": skipped, "tbd_pending": tbd_skipped}


if __name__ == "__main__":
    import sys
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if not lid:
        print("Usage: python3 seed_preseason.py <league_id> [--overwrite]")
        sys.exit(1)
    overwrite = "--overwrite" in sys.argv
    seed_preseason_schedule(lid, overwrite=overwrite)
