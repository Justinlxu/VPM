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
# COMPUTE FEATURES FOR A SINGLE MATCHUP
# ══════════════════════════════════════════════════════════════════

def compute_match_features(team_a, team_b, map_name, team_history, feature_cols):
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

def display_prediction(team_a, team_b, map_name, prob_a, features_row,
                       feature_cols, team_history):
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

    # Show key stats used
    hist_a = team_history.get(team_a, [])
    hist_b = team_history.get(team_b, [])
    map_hist_a = [h for h in hist_a if h["map"] == map_name]
    map_hist_b = [h for h in hist_b if h["map"] == map_name]

    print(f"\n  Data available:")
    print(f"    {team_a}: {len(hist_a)} total maps, {len(map_hist_a)} on {map_name}")
    print(f"    {team_b}: {len(hist_b)} total maps, {len(map_hist_b)} on {map_name}")

    # Show key feature values
    feat_dict = dict(zip(feature_cols, features_row.iloc[0]))
    print(f"\n  Key stats (last 5 maps overall):")
    for stat, label in [("acs_overall_5", "ACS"),
                        ("kast_overall_5", "KAST"),
                        ("fk_fd_diff_overall_5", "FK/FD Diff"),
                        ("gun_rate_overall_5", "Gun Rate")]:
        a_val = feat_dict.get(f"{stat}_a", np.nan)
        b_val = feat_dict.get(f"{stat}_b", np.nan)
        a_str = f"{a_val:.1f}" if not pd.isna(a_val) else "N/A"
        b_str = f"{b_val:.1f}" if not pd.isna(b_val) else "N/A"
        print(f"    {label:>12s}:  {a_str:>8s}  vs  {b_str:<8s}")

    print(f"\n  Key stats (last 5 maps on {map_name}):")
    for stat, label in [("acs_map_5", "ACS"),
                        ("kast_map_5", "KAST"),
                        ("fk_fd_diff_map_5", "FK/FD Diff"),
                        ("gun_rate_map_5", "Gun Rate")]:
        a_val = feat_dict.get(f"{stat}_a", np.nan)
        b_val = feat_dict.get(f"{stat}_b", np.nan)
        a_str = f"{a_val:.1f}" if not pd.isna(a_val) else "N/A"
        b_str = f"{b_val:.1f}" if not pd.isna(b_val) else "N/A"
        print(f"    {label:>12s}:  {a_str:>8s}  vs  {b_str:<8s}")


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
    features_row = compute_match_features(
        team_a, team_b, map_name, team_history, feature_cols
    )

    # Predict
    prob_a = model.predict_proba(features_row)[0][1]

    # Display
    display_prediction(
        team_a, team_b, map_name, prob_a,
        features_row, feature_cols, team_history
    )


if __name__ == "__main__":
    main()
