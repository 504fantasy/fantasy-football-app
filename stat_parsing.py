"""
stat_parsing.py
---------------
Parses ESPN-style boxscore JSON payloads into the stats dict format
used by the scoring engine.

This module is intentionally free of web framework dependencies so it
can be imported in tests, CLI scripts, and the FastAPI app alike.

Expected input shape (ESPN boxscore JSON):
    {
      "gameId": "...",
      "players": [
        {
          "playerId": "3054211",
          "name": "Josh Allen",
          "position": "QB",
          "team": "BUF",
          "stats": {
            "passing_yards": 320,
            "rushing_yards": 35,
            "receiving_yards": 0,
            "receptions": 0,
            "return_yards": 0,
            "total_tds": 3,
            "interceptions": 1,
            "fumbles_lost": 0,
            "field_goals_made": []
          }
        },
        ...
      ]
    }

Output shape:
    {
      "Josh Allen": {
        "name": "Josh Allen",
        "position": "QB",
        "team": "BUF",
        "passing_yards": 320,
        "rushing_yards": 35,
        "receiving_yards": 0,
        "receptions": 0,
        "return_yards": 0,
        "total_tds": 3,
        "interceptions": 1,
        "fumbles_lost": 0,
        "return_fumbles_lost": 0,
        "field_goals_made": []
      },
      ...
    }
"""

# Canonical set of stat keys the scoring engine reads.
# Any key missing from the raw ESPN payload is filled with its default.
_STAT_DEFAULTS: dict = {
    "passing_yards":       0,
    "rushing_yards":       0,
    "receiving_yards":     0,
    "receptions":          0,
    "return_yards":        0,
    "total_tds":           0,
    "interceptions":       0,
    "fumbles_lost":        0,
    "return_fumbles_lost": 0,
    "field_goals_made":    [],
}


def parse_team_stats(data: dict) -> dict:
    """
    Parse one ESPN boxscore payload into a player-keyed stats dict.

    Parameters
    ----------
    data : dict
        Parsed JSON from an ESPN boxscore endpoint.

    Returns
    -------
    dict
        Keys are player names (str).  Values are stats dicts with all
        keys in _STAT_DEFAULTS guaranteed to be present, plus
        "name", "position", and "team".

    Notes
    -----
    - Players with no name are skipped.
    - Duplicate names: last occurrence wins (mirrors ESPN behaviour for
      same-name players on different teams — include ``team`` in your
      lookups if this matters).
    - Unknown stat keys in the raw payload are silently ignored.
    """
    result: dict = {}

    for player in data.get("players", []):
        name = (player.get("name") or "").strip()
        if not name:
            continue

        raw = player.get("stats") or {}

        # Start from defaults so every downstream key is always present
        parsed: dict = {k: (list(v) if isinstance(v, list) else v)
                        for k, v in _STAT_DEFAULTS.items()}

        for key in _STAT_DEFAULTS:
            if key in raw:
                # Coerce numeric fields to the right type; leave lists as-is
                val = raw[key]
                if isinstance(_STAT_DEFAULTS[key], list):
                    parsed[key] = val if isinstance(val, list) else []
                else:
                    try:
                        parsed[key] = float(val)
                    except (TypeError, ValueError):
                        pass  # keep default

        parsed["name"]     = name
        parsed["position"] = (player.get("position") or "").strip().upper()
        parsed["team"]     = (player.get("team") or "").strip().upper()

        result[name] = parsed

    return result
