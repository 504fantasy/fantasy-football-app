"""
2026 NFL Preseason Schedule Seeder (v3 - ALL TIMES CONFIRMED)
====================================
Source: Official NFL.com schedule page (nfl.com/schedules/2026/by-week/preseason-week-3)
All kickoff times confirmed from PDF screenshot taken 6/30/2026.
All times Eastern Time (ET), converted to UTC by adding 4 hours (EDT in August).

Game tuple: (week, gameday "YYYY-MM-DD", gametime_et "HH:MM", away_team, home_team)
"""

PRESEASON_GAMES = [
    # ── Hall of Fame Game ──
    (1, "2026-08-06", "20:00", "CAR", "ARI"),

    # ── Week 1 (Aug 13-15) ──
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

    # ── Week 2 (Aug 20-23) ──
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

    # ── Week 3 (Aug 27-29) — ALL TIMES NOW CONFIRMED ──
    # Thursday August 27
    (3, "2026-08-27", "18:00", "PIT", "BUF"),   # 6:00 PM ET
    (3, "2026-08-27", "19:00", "NE",  "CLE"),   # 7:00 PM ET
    (3, "2026-08-27", "19:00", "SF",  "LV"),    # 7:00 PM ET
    (3, "2026-08-27", "21:00", "LAR", "LAC"),   # 9:00 PM ET
    # Friday August 28
    (3, "2026-08-28", "17:00", "WAS", "BAL"),   # 5:00 PM ET
    (3, "2026-08-28", "18:00", "HOU", "CAR"),   # 6:00 PM ET
    (3, "2026-08-28", "18:00", "ATL", "MIA"),   # 6:00 PM ET
    (3, "2026-08-28", "18:30", "TB",  "JAX"),   # 6:30 PM ET
    (3, "2026-08-28", "18:30", "NYG", "NYJ"),   # 6:30 PM ET
    (3, "2026-08-28", "19:00", "NO",  "DAL"),   # 7:00 PM ET
    (3, "2026-08-28", "19:00", "SEA", "KC"),    # 7:00 PM ET
    (3, "2026-08-28", "19:00", "CIN", "PHI"),   # 7:00 PM ET (CBS)
    (3, "2026-08-28", "19:00", "ARI", "GB"),    # 7:00 PM ET
    (3, "2026-08-28", "20:00", "MIN", "DEN"),   # 8:00 PM ET
    # Saturday August 29
    (3, "2026-08-29", "12:00", "DET", "IND"),   # 12:00 PM ET
    (3, "2026-08-29", "17:00", "CHI", "TEN"),   # 5:00 PM ET
]


def seed_preseason_schedule(league_id: int, overwrite: bool = False):
    """
    Insert/update preseason kickoff times into survivor_game_schedule.
    ET times converted to UTC by adding 4 hours (EDT in August).
    """
    import sqlite3
    from datetime import datetime, timedelta

    conn = sqlite3.connect('/root/fantasy-football-app/data/survivor.db')
    added = skipped = 0

    for week, gameday, gametime_et, away, home in PRESEASON_GAMES:
        try:
            naive_et = datetime.strptime(f"{gameday} {gametime_et}", "%Y-%m-%d %H:%M")
            kickoff_utc = (naive_et + timedelta(hours=4)).isoformat()
        except Exception:
            continue

        for team in (away, home):
            try:
                conn.execute(
                    "INSERT INTO survivor_game_schedule "
                    "(league_id, season, week, team, kickoff_utc) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(league_id, week, team) DO " +
                    ("UPDATE SET kickoff_utc=excluded.kickoff_utc" if overwrite else "NOTHING"),
                    (league_id, 2026, week, team, kickoff_utc)
                )
                added += 1
            except Exception:
                skipped += 1

    conn.commit()
    conn.close()
    print(f"Preseason schedule seed: added/updated={added} skipped={skipped}")
    print(f"Total games: {len(PRESEASON_GAMES)}, Teams covered: "
          f"{len(set(t for _, _, _, a, h in PRESEASON_GAMES for t in (a, h)))}")
    return {"added": added, "skipped": skipped}


if __name__ == "__main__":
    import sys
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if not lid:
        print("Usage: python3 seed_preseason.py <league_id> [--overwrite]")
        sys.exit(1)
    overwrite = "--overwrite" in sys.argv
    seed_preseason_schedule(lid, overwrite=overwrite)
