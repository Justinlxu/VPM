"""
Feature Engineering — Elo + FK/FD only model

Features:
  - Ranking Elo sum and trend per team (+ differentials)
  - FK/FD diff rolling averages: 5 and 10 windows, overall and map-specific
  - Differential features for all of the above

Used to test whether the Elo system alone (with FK/FD) can match or beat
the full feature set model.
"""

import os
import sys
import pandas as pd
import numpy as np
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from player_elo import load_player_data, build_ranking_elo, RANKING_ELO_START

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

INPUT_FILE  = os.path.join(os.path.dirname(__file__), "..", "Scraper", "valorant_data.xlsx")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "training_data_elo.csv")

WINDOWS = [5, 10]
ELO_TREND_WINDOW = 5
CORRELATION_THRESHOLD = 0.95
SEED = 42

NON_FEATURE_COLS = ["date", "map", "match_id", "team_a", "team_b", "target", "winner"]

# Only keep these features — everything else is dropped before training
KEEP_FEATURES = [
    "ranking_elo_sum_diff",
    "ranking_elo_trend_diff",
    "fk_fd_diff_overall_5_diff",
]


# ══════════════════════════════════════════════════════════════════
# LOAD AND FILTER
# ══════════════════════════════════════════════════════════════════

def load_and_filter(filepath):
    df = pd.read_excel(filepath, sheet_name="Model Features")

    n_before = len(df)
    df = df[df["Pistols Won A"] + df["Pistols Won B"] == 2].copy()
    n_after = len(df)
    print(f"Pistol filter: {n_before} → {n_after} rows ({n_before - n_after} removed)")

    n_before = len(df)
    if "Match ID" in df.columns:
        df = df.drop_duplicates(subset=["Match ID"]).copy()
    else:
        df = df.drop_duplicates().copy()
    n_after = len(df)
    print(f"Dedup filter:  {n_before} → {n_after} rows ({n_before - n_after} removed)")

    df = df.sort_values("Date").reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════════
# FK/FD TRAILING FEATURES
# ══════════════════════════════════════════════════════════════════

def compute_fkfd_features(df):
    team_history = defaultdict(list)
    output_rows = []

    for idx, row in df.iterrows():
        team_a   = row["Team A"]
        team_b   = row["Team B"]
        map_name = row["Map"]
        date     = row["Date"]

        features = {
            "date":     date,
            "map":      map_name,
            "match_id": row["Match ID"],
            "team_a":   team_a,
            "team_b":   team_b,
            "winner":   row["Winner"],
        }

        for side, team in [("a", team_a), ("b", team_b)]:
            history     = team_history[team]
            map_history = [h for h in history if h["map"] == map_name]

            for w in WINDOWS:
                # Overall
                recent = history[-w:] if len(history) >= w else history
                if recent:
                    features[f"fk_fd_diff_overall_{w}_{side}"] = np.mean(
                        [h["fkfd"] for h in recent]
                    )
                else:
                    features[f"fk_fd_diff_overall_{w}_{side}"] = np.nan

                # Map-specific
                recent_map = map_history[-w:] if len(map_history) >= w else map_history
                if recent_map:
                    features[f"fk_fd_diff_map_{w}_{side}"] = np.mean(
                        [h["fkfd"] for h in recent_map]
                    )
                else:
                    features[f"fk_fd_diff_map_{w}_{side}"] = np.nan

        output_rows.append(features)

        for side, team in [("a", team_a), ("b", team_b)]:
            col = "FK/FD Diff A" if side == "a" else "FK/FD Diff B"
            team_history[team].append({"map": map_name, "fkfd": row[col]})

        if (idx + 1) % 500 == 0:
            print(f"  Processed {idx + 1} / {len(df)} rows...")

    print(f"  Processed {len(df)} / {len(df)} rows")
    return pd.DataFrame(output_rows)


# ══════════════════════════════════════════════════════════════════
# ELO FEATURES
# ══════════════════════════════════════════════════════════════════

def add_elo_features(features_df, filepath, k=None, change_min=None, change_max=None):
    print("  Building player Elo ratings...")
    player_df = load_player_data(filepath)
    _, ranking_elo_history, _ = build_ranking_elo(player_df, k=k, change_min=change_min, change_max=change_max)

    # Build per-team map history of Elo totals
    team_elos_by_map = {}
    for match_id, snap in ranking_elo_history.items():
        map_players = player_df[player_df["Match ID"] == match_id][["Player Name", "Team"]]
        team_map = {}
        for _, row in map_players.iterrows():
            team_map.setdefault(row["Team"], []).append(
                snap.get(row["Player Name"], RANKING_ELO_START)
            )
        team_elos_by_map[match_id] = team_map

    team_elo_history = defaultdict(list)
    elo_rows = []

    for _, row in features_df.iterrows():
        match_id = row["match_id"]
        team_a   = row["team_a"]
        team_b   = row["team_b"]

        team_map = team_elos_by_map.get(match_id, {})
        elo_a    = sum(team_map.get(team_a, [RANKING_ELO_START] * 5))
        elo_b    = sum(team_map.get(team_b, [RANKING_ELO_START] * 5))

        def compute_trend(team):
            history = team_elo_history[team][-ELO_TREND_WINDOW:]
            if len(history) < 2:
                return 0.0
            return float(np.polyfit(np.arange(len(history)), history, 1)[0])

        trend_a = compute_trend(team_a)
        trend_b = compute_trend(team_b)

        elo_rows.append({
            "ranking_elo_sum_a":   elo_a,
            "ranking_elo_sum_b":   elo_b,
            "ranking_elo_trend_a": trend_a,
            "ranking_elo_trend_b": trend_b,
        })

        team_elo_history[team_a].append(elo_a)
        team_elo_history[team_b].append(elo_b)

    elo_df = pd.DataFrame(elo_rows, index=features_df.index)
    print(f"  Added Elo features")
    return pd.concat([features_df, elo_df], axis=1)


# ══════════════════════════════════════════════════════════════════
# DIFFERENTIAL FEATURES
# ══════════════════════════════════════════════════════════════════

def add_differential_features(df):
    a_cols = [c for c in df.columns if c.endswith("_a") and c != "team_a"]
    added = 0
    for a_col in a_cols:
        b_col = a_col[:-2] + "_b"
        if b_col in df.columns:
            df[a_col[:-2] + "_diff"] = df[a_col] - df[b_col]
            added += 1
    print(f"  Added {added} differential features")
    return df


# ══════════════════════════════════════════════════════════════════
# RANDOM A/B SWAP
# ══════════════════════════════════════════════════════════════════

def random_swap_teams(df):
    rng = np.random.RandomState(SEED)
    swap_mask = rng.rand(len(df)) > 0.5
    df = df.copy()

    a_cols = [c for c in df.columns if c.endswith("_a") and c != "team_a"]
    for a_col in a_cols:
        b_col = a_col[:-2] + "_b"
        if b_col in df.columns:
            df.loc[swap_mask, [a_col, b_col]] = df.loc[swap_mask, [b_col, a_col]].values

    diff_cols = [c for c in df.columns if c.endswith("_diff")]
    for diff_col in diff_cols:
        df.loc[swap_mask, diff_col] = -df.loc[swap_mask, diff_col]

    df.loc[swap_mask, ["team_a", "team_b"]] = df.loc[swap_mask, ["team_b", "team_a"]].values
    df["target"] = (df["winner"] == df["team_a"]).astype(int)
    return df


# ══════════════════════════════════════════════════════════════════
# TRIM CORRELATED FEATURES
# ══════════════════════════════════════════════════════════════════

def trim_correlated(df, threshold=0.95):
    feature_cols = [c for c in df.columns if c not in NON_FEATURE_COLS]
    feature_df   = df[feature_cols].dropna()

    if len(feature_df) == 0:
        return df, feature_cols

    corr_matrix = feature_df.corr().abs()
    upper = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))

    to_drop = {col for col in upper.columns if (upper[col] > threshold).any()}
    remaining = [c for c in feature_cols if c not in to_drop]
    print(f"  Trimmed {len(to_drop)} correlated features (r > {threshold})")
    print(f"  Features: {len(feature_cols)} → {len(remaining)}")

    return df.drop(columns=list(to_drop)), remaining


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    print("Loading and filtering data...")
    df = load_and_filter(INPUT_FILE)

    print(f"\nComputing FK/FD trailing features for {len(df)} maps...")
    features_df = compute_fkfd_features(df)

    print(f"\nAdding player Elo features...")
    features_df = add_elo_features(features_df, INPUT_FILE)

    print(f"\nAdding differential features...")
    features_df = add_differential_features(features_df)

    print(f"\nRandomly swapping Team A/B...")
    features_df = random_swap_teams(features_df)
    features_df = features_df.drop(columns=["winner"])

    # Keep only the top features
    feature_cols_now = [c for c in features_df.columns if c not in NON_FEATURE_COLS]
    to_drop = [c for c in feature_cols_now if c not in KEEP_FEATURES]
    features_df = features_df.drop(columns=to_drop)
    print(f"\nKept {len(KEEP_FEATURES)} features, dropped {len(to_drop)}")

    print(f"\nTrimming correlated features...")
    features_df, remaining_features = trim_correlated(features_df, CORRELATION_THRESHOLD)

    # Round floats
    float_cols = features_df.select_dtypes(include=["float64"]).columns
    features_df[float_cols] = features_df[float_cols].round(3)

    feature_cols  = [c for c in features_df.columns if c not in NON_FEATURE_COLS]
    n_total       = len(features_df)
    n_with_nans   = features_df[feature_cols].isna().any(axis=1).sum()

    print(f"\n{'═' * 50}")
    print(f"  DATASET SUMMARY")
    print(f"{'═' * 50}")
    print(f"  Total rows:              {n_total}")
    print(f"  Features:                {len(feature_cols)}")
    print(f"  Rows with NaN:           {n_with_nans}")
    print(f"  Rows fully populated:    {n_total - n_with_nans}")
    print(f"  Target balance:          {features_df['target'].mean():.1%} Team A wins")
    print(f"\n  All features:")
    for c in sorted(feature_cols):
        print(f"    {c}")

    features_df.to_csv(OUTPUT_FILE, index=False)
    print(f"\n  Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
