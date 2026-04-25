# scoring.py

def calculate_player_score(stats: dict) -> float:
    score = 0.0

    # PPR
    score += stats.get("receptions", 0) * 1.0

    # Yardage
    score += stats.get("receiving_yards", 0) * 0.1
    score += stats.get("rushing_yards", 0) * 0.1
    score += stats.get("return_yards", 0) * 0.1
    score += stats.get("passing_yards", 0) * 0.04

    # Touchdowns
    score += stats.get("total_tds", 0) * 6.0

    # Turnovers
    score -= stats.get("fumbles_lost", 0) * 2.0
    score -= stats.get("interceptions", 0) * 2.0

    return round(score, 2)


def calculate_kicker_score(stats: dict) -> float:
    score = 0.0

    # Distance-based FG scoring (no negatives)
    for fg in stats.get("field_goals_made", []):
        distance = fg.get("distance", 0)
        if isinstance(distance, (int, float)):
            score += distance * 0.1

    return round(score, 2)


def calculate_defense_score(stats: dict) -> float:
    score = 0.0

    # Return yards
    score += stats.get("return_yards", 0) * 0.1

    # Defensive turnovers (optional / future-safe)
    score -= stats.get("return_fumbles_lost", 0) * 2.0

    return round(score, 2)


def apply_multiplier(score: float, multiplier) -> float:
    if not multiplier:
        return score

    try:
        return round(score * float(multiplier), 2)
    except (TypeError, ValueError):
        return score


def calculate_fantasy_points(player: dict, stats: dict) -> float:
    pos = player.get("pos")

    if pos == "DST":
        base_score = calculate_defense_score(stats)
    elif pos == "K":
        base_score = calculate_kicker_score(stats)
    else:
        base_score = calculate_player_score(stats)

    return apply_multiplier(base_score, player.get("multiplier"))
