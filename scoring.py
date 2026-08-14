# scoring.py

# Default point values. The original 12 categories match the previous
# hardcoded formula exactly, so any league that hasn't customized scoring
# behaves identically to before. Everything past that is new — since these
# categories never existed before, there's no "must match legacy" constraint;
# defaults below follow common fantasy-scoring conventions.
DEFAULT_SCORING = {
    # -- original categories --
    "reception":        1.0,   # PPR
    "receiving_yard":   0.1,
    "rushing_yard":     0.1,
    "return_yard":      0.1,
    "passing_yard":     0.04,
    "passing_td":       6.0,
    "other_td":         6.0,   # rushing / receiving / special-teams TDs
    "fumble_lost":     -2.0,
    "interception":    -2.0,
    "fg_yard":          0.1,   # kicker: points per yard of made-FG distance
    "def_return_yard":  0.1,
    "def_fumble_lost": -2.0,

    # -- passing bonuses --
    "two_pt_conversion":  2.0,
    "pass_40_completion": 1.0,
    "pass_td_40_bonus":   1.0,
    "pass_td_50_bonus":   2.0,

    # -- rushing bonuses --
    "rush_td_40_bonus": 1.0,
    "rush_td_50_bonus": 2.0,

    # -- receiving bonuses --
    "rec_td_40_bonus": 1.0,
    "rec_td_50_bonus": 2.0,

    # -- kicking --
    "pat_made":  1.0,
    "fg_missed": -1.0,

    # -- defense / special teams --
    "sack":           1.0,
    "safety":         2.0,
    "forced_fumble":  1.0,
    "blocked_kick":   2.0,

    # -- points allowed tiers (defense) --
    "points_allowed_0":      10.0,
    "points_allowed_1_6":     7.0,
    "points_allowed_7_13":    4.0,
    "points_allowed_14_20":   1.0,
    "points_allowed_21_27":   0.0,
    "points_allowed_28_34":  -1.0,
    "points_allowed_35_plus": -4.0,
}


def resolve_scoring_settings(overrides: dict = None) -> dict:
    """Merge a league's saved overrides (if any) on top of the defaults."""
    settings = dict(DEFAULT_SCORING)
    if overrides:
        for k, v in overrides.items():
            if k in settings:
                try:
                    settings[k] = float(v)
                except (TypeError, ValueError):
                    pass
    return settings


def _td_points(stats: dict, settings: dict) -> float:
    """
    Score touchdowns using the split passing/other columns when present.
    Falls back to the legacy combined total_tds field (scored at the
    'other_td' rate) for rows written before the passing/other split existed.
    """
    if "passing_tds" in stats or "other_tds" in stats:
        return (
            stats.get("passing_tds", 0) * settings["passing_td"]
            + stats.get("other_tds", 0) * settings["other_td"]
        )
    return stats.get("total_tds", 0) * settings["other_td"]


def calculate_player_score(stats: dict, settings: dict = None) -> float:
    settings = settings or DEFAULT_SCORING
    score = 0.0

    score += stats.get("receptions", 0) * settings["reception"]
    score += stats.get("receiving_yards", 0) * settings["receiving_yard"]
    score += stats.get("rushing_yards", 0) * settings["rushing_yard"]
    score += stats.get("return_yards", 0) * settings["return_yard"]
    score += stats.get("passing_yards", 0) * settings["passing_yard"]
    score += _td_points(stats, settings)
    score -= stats.get("fumbles_lost", 0) * abs(settings["fumble_lost"])
    score -= stats.get("interceptions", 0) * abs(settings["interception"])

    # Bonuses (all default to 0 for rows that predate these columns)
    score += stats.get("two_pt_conversions", 0) * settings["two_pt_conversion"]
    score += stats.get("pass_40_completions", 0) * settings["pass_40_completion"]
    score += stats.get("pass_td_40", 0) * settings["pass_td_40_bonus"]
    score += stats.get("pass_td_50", 0) * settings["pass_td_50_bonus"]
    score += stats.get("rush_td_40", 0) * settings["rush_td_40_bonus"]
    score += stats.get("rush_td_50", 0) * settings["rush_td_50_bonus"]
    score += stats.get("rec_td_40", 0) * settings["rec_td_40_bonus"]
    score += stats.get("rec_td_50", 0) * settings["rec_td_50_bonus"]

    return round(score, 2)


def calculate_kicker_score(stats: dict, settings: dict = None) -> float:
    settings = settings or DEFAULT_SCORING
    score = 0.0

    # Distance-based FG scoring (no negatives)
    for fg in stats.get("field_goals_made", []):
        distance = fg.get("distance", 0)
        if isinstance(distance, (int, float)):
            score += distance * settings["fg_yard"]

    score += stats.get("pat_made", 0) * settings["pat_made"]
    score += stats.get("fg_missed", 0) * settings["fg_missed"]

    return round(score, 2)


def _points_allowed_bonus(points_allowed, settings: dict) -> float:
    if points_allowed is None:
        return 0.0
    try:
        pa = int(points_allowed)
    except (TypeError, ValueError):
        return 0.0
    if pa == 0:
        return settings["points_allowed_0"]
    elif pa <= 6:
        return settings["points_allowed_1_6"]
    elif pa <= 13:
        return settings["points_allowed_7_13"]
    elif pa <= 20:
        return settings["points_allowed_14_20"]
    elif pa <= 27:
        return settings["points_allowed_21_27"]
    elif pa <= 34:
        return settings["points_allowed_28_34"]
    else:
        return settings["points_allowed_35_plus"]


def calculate_defense_score(stats: dict, settings: dict = None) -> float:
    settings = settings or DEFAULT_SCORING
    score = 0.0

    score += stats.get("return_yards", 0) * settings["def_return_yard"]
    score -= stats.get("return_fumbles_lost", 0) * abs(settings["def_fumble_lost"])
    score += stats.get("sacks", 0) * settings["sack"]
    score += stats.get("safeties", 0) * settings["safety"]
    score += stats.get("forced_fumbles", 0) * settings["forced_fumble"]
    score += stats.get("blocked_kicks", 0) * settings["blocked_kick"]
    score += _points_allowed_bonus(stats.get("points_allowed"), settings)

    return round(score, 2)


def apply_multiplier(score: float, multiplier) -> float:
    if not multiplier:
        return score

    try:
        return round(score * float(multiplier), 2)
    except (TypeError, ValueError):
        return score


def calculate_fantasy_points(player: dict, stats: dict, settings: dict = None) -> float:
    settings = settings or DEFAULT_SCORING
    pos = player.get("pos")

    if pos == "DST":
        base_score = calculate_defense_score(stats, settings)
    elif pos == "K":
        base_score = calculate_kicker_score(stats, settings)
    else:
        base_score = calculate_player_score(stats, settings)

    return apply_multiplier(base_score, player.get("multiplier"))
