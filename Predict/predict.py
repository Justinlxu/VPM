"""
Valorant Match Predictor

Predict the winner of a match given two team names and a map.
Uses the trained model and computes trailing features from historical data.

Usage:
    python predict.py "Fnatic" "LOUD" "Lotus"
    python predict.py                          # Interactive mode
"""

import os
import sys
import pandas as pd
import numpy as np
import joblib
import json
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Retraining"))
from player_elo import load_player_data, build_ranking_elo, RANKING_ELO_START

# ══════════════════════════════════════════════════════════════════
# CONFIG — must match feature_engineering_v3.py exactly
# ══════════════════════════════════════════════════════════════════

EXCEL_FILE = os.path.join(os.path.dirname(__file__), "..", "Scraper", "valorant_data.xlsx")
MODEL_FILE = "valorant_model.joblib"
META_FILE = "model_metadata.json"

WINDOWS = [5, 10]

RAW_STATS = [
    ("acs",           "ACS A",           "ACS B"),
    ("kast",          "KAST A",          "KAST B"),
    ("fk_fd_diff",    "FK/FD Diff A",    "FK/FD Diff B"),
    ("gun_rate",      "Gun Rate A",      "Gun Rate B"),
]


# ══════════════════════════════════════════════════════════════════
# BUILD TEAM HISTORIES FROM RAW DATA
# ══════════════════════════════════════════════════════════════════

def build_team_histories(filepath):
    """
    Read the Excel file and build a history of stats for every team,
    in chronological order. Returns team_history dict and metadata.
    """
    df = pd.read_excel(filepath, sheet_name="Model Features")

    # Filter bad data
    df = df[df["Pistols Won A"] + df["Pistols Won B"] == 2].copy()
    df = df.sort_values("Date").reset_index(drop=True)

    # Compute rates
    df["Total Rounds"] = df["Score A"] + df["Score B"]
    df["Gun Rate A"] = df["Gun Rounds Won A"] / df["Total Rounds"]
    df["Gun Rate B"] = df["Gun Rounds Won B"] / df["Total Rounds"]

    # Build histories
    team_history = defaultdict(list)

    for _, row in df.iterrows():
        for side, team in [("a", row["Team A"]), ("b", row["Team B"])]:
            entry = {"date": row["Date"], "map": row["Map"], "stats": {}}
            for stat_name, col_a, col_b in RAW_STATS:
                col = col_a if side == "a" else col_b
                entry["stats"][stat_name] = row[col]
            team_history[team].append(entry)

    teams = sorted(team_history.keys())
    maps_seen = sorted(df["Map"].unique())
    latest_date = df["Date"].max()

    return team_history, teams, maps_seen, latest_date


# ══════════════════════════════════════════════════════════════════
# PLAYER ELO FEATURES
# ══════════════════════════════════════════════════════════════════

ELO_TREND_WINDOW = 5


def build_team_elo_features(team_a, team_b, filepath):
    """
    Compute ranking Elo sum and trend for two teams using current player Elo ratings.
    Uses each team's most recent known lineup for the current Elo sum.
    Trend is the slope of team Elo total over their last ELO_TREND_WINDOW maps.
    """
    player_df = load_player_data(filepath)
    _, ranking_elo_history, _ = build_ranking_elo(player_df)

    # Final Elo ratings
    final_elos = {}
    for map_id, snap in ranking_elo_history.items():
        for player, elo in snap.items():
            final_elos[player] = elo
    # Apply final changes (last map snapshot is before, so use post-map values)
    # Re-derive from last appearance in history
    for map_id in reversed(list(ranking_elo_history.keys())):
        snap = ranking_elo_history[map_id]
        for player in snap:
            if player not in final_elos:
                final_elos[player] = snap[player]

    # Build per-team map history: {team: [(date, match_id, [player_elos])]}
    team_map_history = defaultdict(list)
    map_ids_ordered = player_df["Match ID"].unique()

    for map_id in map_ids_ordered:
        map_rows = player_df[player_df["Match ID"] == map_id]
        date = map_rows["Date"].iloc[0]
        snap = ranking_elo_history.get(map_id, {})

        teams_in_map = map_rows["Team"].unique()
        for team in teams_in_map:
            players = map_rows[map_rows["Team"] == team]["Player Name"].tolist()
            elos = [snap.get(p, RANKING_ELO_START) for p in players]
            team_map_history[team].append((date, map_id, players, elos))

    # Per-player recent FK/FD diff (last 5 maps average)
    player_fkfd = {}
    for player in player_df["Player Name"].unique():
        rows = player_df[player_df["Player Name"] == player].tail(5)
        if len(rows) > 0:
            player_fkfd[player] = float((rows["FK"] - rows["FD"]).mean())

    def get_elo_features(team):
        history = team_map_history.get(team, [])
        if not history:
            return RANKING_ELO_START * 5, 0.0

        # Current Elo sum from most recent lineup
        _, _, players, _ = history[-1]
        elo_sum = sum(final_elos.get(p, RANKING_ELO_START) for p in players)

        # Trend from last ELO_TREND_WINDOW maps
        recent = history[-ELO_TREND_WINDOW:]
        if len(recent) < 2:
            return elo_sum, 0.0

        totals = [sum(elos) for _, _, _, elos in recent]
        x = np.arange(len(totals))
        trend = float(np.polyfit(x, totals, 1)[0])
        return elo_sum, trend

    def get_player_info(team):
        history = team_map_history.get(team, [])
        if not history:
            return []
        _, _, players, _ = history[-1]
        info = [(p, final_elos.get(p, RANKING_ELO_START), player_fkfd.get(p, 0.0))
                for p in players]
        return sorted(info, key=lambda x: x[1], reverse=True)

    elo_sum_a, trend_a = get_elo_features(team_a)
    elo_sum_b, trend_b = get_elo_features(team_b)

    elo_features = {
        "ranking_elo_sum_a":      elo_sum_a,
        "ranking_elo_sum_b":      elo_sum_b,
        "ranking_elo_sum_diff":   elo_sum_a - elo_sum_b,
        "ranking_elo_trend_a":    trend_a,
        "ranking_elo_trend_b":    trend_b,
        "ranking_elo_trend_diff": trend_a - trend_b,
    }
    player_info = {
        team_a: get_player_info(team_a),
        team_b: get_player_info(team_b),
    }
    return elo_features, player_info


# ══════════════════════════════════════════════════════════════════
# COMPUTE FEATURES FOR A SINGLE MATCHUP
# ══════════════════════════════════════════════════════════════════

def compute_match_features(team_a, team_b, map_name, team_history, feature_cols, elo_features=None):
    """
    Compute the feature row for a matchup, using each team's
    full history as the lookback.
    """
    features = {}

    for side, team in [("a", team_a), ("b", team_b)]:
        history = team_history.get(team, [])
        features[f"prior_maps_{side}"] = len(history)

        map_history = [h for h in history if h["map"] == map_name]
        features[f"prior_maps_map_{side}"] = len(map_history)

        for stat_name, _, _ in RAW_STATS:
            for w in WINDOWS:
                # Overall
                recent = history[-w:] if len(history) >= w else history
                if recent:
                    vals = [h["stats"][stat_name] for h in recent]
                    features[f"{stat_name}_overall_{w}_{side}"] = np.mean(vals)
                else:
                    features[f"{stat_name}_overall_{w}_{side}"] = np.nan

                # Map-specific
                recent_map = map_history[-w:] if len(map_history) >= w else map_history
                if recent_map:
                    vals = [h["stats"][stat_name] for h in recent_map]
                    features[f"{stat_name}_map_{w}_{side}"] = np.mean(vals)
                else:
                    features[f"{stat_name}_map_{w}_{side}"] = np.nan

    # Differential features
    a_cols = [c for c in features if c.endswith("_a")]
    for a_col in a_cols:
        b_col = a_col[:-2] + "_b"
        diff_col = a_col[:-2] + "_diff"
        if b_col in features:
            a_val = features[a_col]
            b_val = features[b_col]
            if pd.isna(a_val) or pd.isna(b_val):
                features[diff_col] = np.nan
            else:
                features[diff_col] = a_val - b_val

    # Merge Elo features
    if elo_features:
        features.update(elo_features)

    # Build row in the correct column order
    row = {}
    for col in feature_cols:
        row[col] = features.get(col, np.nan)

    return pd.DataFrame([row])


# ══════════════════════════════════════════════════════════════════
# FUZZY TEAM NAME MATCHING
# ══════════════════════════════════════════════════════════════════

def find_team(query, teams):
    """Find the best matching team name. Case-insensitive partial match."""
    query_lower = query.strip().lower()

    # Exact match first
    for t in teams:
        if t.lower() == query_lower:
            return t

    # Partial match
    matches = [t for t in teams if query_lower in t.lower()]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        print(f"\n  Multiple matches for '{query}':")
        for i, m in enumerate(matches, 1):
            print(f"    {i}. {m}")
        while True:
            choice = input(f"  Pick a number (1-{len(matches)}): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(matches):
                return matches[int(choice) - 1]
    else:
        print(f"\n  No team found matching '{query}'.")
        print(f"  Available teams:")
        for t in teams:
            print(f"    {t}")
        return None


def find_map(query, maps_seen):
    """Find the best matching map name."""
    query_lower = query.strip().lower()
    for m in maps_seen:
        if m.lower() == query_lower:
            return m
    matches = [m for m in maps_seen if query_lower in m.lower()]
    if len(matches) == 1:
        return matches[0]
    print(f"\n  No map found matching '{query}'.")
    print(f"  Available maps: {', '.join(maps_seen)}")
    return None


# ══════════════════════════════════════════════════════════════════
# PREDICTION DISPLAY
# ══════════════════════════════════════════════════════════════════

def display_prediction(team_a, team_b, map_name, prob_a, elo_features, player_info):
    """Display the prediction with context."""
    prob_b = 1 - prob_a
    confidence = max(prob_a, prob_b)
    predicted_winner = team_a if prob_a > 0.5 else team_b

    print(f"\n{'═' * 55}")
    print(f"  PREDICTION")
    print(f"{'═' * 55}")
    print(f"  {team_a}  vs  {team_b}")
    print(f"  Map: {map_name}")
    print(f"{'─' * 55}")
    print(f"  Predicted winner: {predicted_winner}")
    print(f"  Confidence: {confidence:.1%}")
    print(f"{'─' * 55}")
    print(f"  {team_a:>25s}: {prob_a:.1%}")
    print(f"  {team_b:>25s}: {prob_b:.1%}")
    print(f"{'═' * 55}")

    # Team Elo summary
    elo_sum_a = elo_features["ranking_elo_sum_a"]
    elo_sum_b = elo_features["ranking_elo_sum_b"]
    trend_a   = elo_features["ranking_elo_trend_a"]
    trend_b   = elo_features["ranking_elo_trend_b"]

    print(f"\n  {'Team Elo Summary'}")
    print(f"  {'─' * 53}")
    print(f"  {'':25s}  {'Elo Sum':>8}  {'Trend':>10}")
    print(f"  {team_a:>25s}  {elo_sum_a:>8.0f}  {trend_a:>+10.1f}")
    print(f"  {team_b:>25s}  {elo_sum_b:>8.0f}  {trend_b:>+10.1f}")

    # Per-player breakdown
    for team in [team_a, team_b]:
        players = player_info.get(team, [])
        print(f"\n  {team} — Players (sorted by Elo)")
        print(f"  {'─' * 53}")
        print(f"  {'Player':>25s}  {'Elo':>7}  {'FK/FD diff (last 5)':>19}")
        if players:
            for name, elo, fkfd in players:
                print(f"  {name:>25s}  {elo:>7.0f}  {fkfd:>+19.2f}")
        else:
            print(f"  {'No data available':>25s}")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    # Load model and metadata
    print("Loading model and data...")
    model = joblib.load(MODEL_FILE)
    with open(META_FILE) as f:
        meta = json.load(f)
    feature_cols = meta["feature_cols"]

    # Build team histories
    team_history, teams, maps_seen, latest_date = build_team_histories(EXCEL_FILE)
    print(f"Loaded {len(teams)} teams, data through {latest_date}")
    print("Building player Elo ratings...")

    # Get input — command line or interactive
    if len(sys.argv) == 4:
        team_a_query, team_b_query, map_query = sys.argv[1], sys.argv[2], sys.argv[3]
    else:
        print(f"\nAvailable maps: {', '.join(maps_seen)}")
        print(f"Example teams: {', '.join(teams[:10])}, ...")
        print()
        team_a_query = input("  Team A: ").strip()
        team_b_query = input("  Team B: ").strip()
        map_query = input("  Map:    ").strip()

    # Resolve names
    team_a = find_team(team_a_query, teams)
    if not team_a:
        return
    team_b = find_team(team_b_query, teams)
    if not team_b:
        return
    map_name = find_map(map_query, maps_seen)
    if not map_name:
        return

    if team_a == team_b:
        print("\n  Teams must be different.")
        return

    # Compute features
    elo_features, player_info = build_team_elo_features(team_a, team_b, EXCEL_FILE)
    features_row = compute_match_features(
        team_a, team_b, map_name, team_history, feature_cols, elo_features
    )

    # Predict
    prob_a = model.predict_proba(features_row)[0][1]

    # Display
    display_prediction(
        team_a, team_b, map_name, prob_a,
        elo_features, player_info
    )


if __name__ == "__main__":
    main()
