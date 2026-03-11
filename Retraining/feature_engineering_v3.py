"""
Feature Engineering Pipeline v3 for Valorant Match Prediction

Changes from v2:
- Dropped bonus_rate, win_rate, pistol_rate, eco_rate entirely
- Dropped 15 and 20 game windows
- Kept: ACS, KAST, FK/FD diff, gun_rate at 5 and 10 windows
- Kept: prior_maps, differential features
- Dropped map encoding (11/12 were noise)
"""

import os
import pandas as pd
import numpy as np
from collections import defaultdict

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

INPUT_FILE = os.path.join(os.path.dirname(__file__), "..", "Scraper", "valorant_data.xlsx")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "training_data.csv")

WINDOWS = [5, 10]

CORRELATION_THRESHOLD = 0.95
SEED = 42


# ══════════════════════════════════════════════════════════════════
# LOAD AND FILTER
# ══════════════════════════════════════════════════════════════════

def load_and_filter(filepath):
    df = pd.read_excel(filepath, sheet_name="Model Features")

    n_before = len(df)
    df = df[df["Pistols Won A"] + df["Pistols Won B"] == 2].copy()
    n_after = len(df)
    print(f"Pistol filter: {n_before} → {n_after} rows ({n_before - n_after} removed)")

    # Deduplicate: prefer match_id column if present, otherwise full-row dedup
    n_before = len(df)
    if "Match ID" in df.columns:
        df = df.drop_duplicates(subset=["Match ID"]).copy()
    else:
        df = df.drop_duplicates().copy()
    n_after = len(df)
    print(f"Dedup filter:  {n_before} → {n_after} rows ({n_before - n_after} removed)")

    df = df.sort_values("Date").reset_index(drop=True)

    df["Total Rounds"] = df["Score A"] + df["Score B"]

    # Gun round rate
    df["Gun Rate A"] = df["Gun Rounds Won A"] / df["Total Rounds"]
    df["Gun Rate B"] = df["Gun Rounds Won B"] / df["Total Rounds"]

    return df


# ══════════════════════════════════════════════════════════════════
# TRAILING AVERAGES
# ══════════════════════════════════════════════════════════════════

# Only the stats that showed signal
RAW_STATS = [
    ("acs",           "ACS A",           "ACS B"),
    ("kast",          "KAST A",          "KAST B"),
    ("fk_fd_diff",    "FK/FD Diff A",    "FK/FD Diff B"),
    ("gun_rate",      "Gun Rate A",      "Gun Rate B"),
]


def compute_trailing_features(df):
    team_history = defaultdict(list)
    output_rows = []

    for idx, row in df.iterrows():
        team_a = row["Team A"]
        team_b = row["Team B"]
        map_name = row["Map"]
        date = row["Date"]

        features = {
            "date": date,
            "map": map_name,
            "team_a": team_a,
            "team_b": team_b,
            "winner": row["Winner"],
        }

        for side, team in [("a", team_a), ("b", team_b)]:
            history = team_history[team]
            features[f"prior_maps_{side}"] = len(history)

            map_history = [h for h in history if h["map"] == map_name]
            features[f"prior_maps_map_{side}"] = len(map_history)

            for stat_name, col_a, col_b in RAW_STATS:
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

        output_rows.append(features)

        # Add to history
        for side, team in [("a", team_a), ("b", team_b)]:
            entry = {"date": date, "map": map_name, "stats": {}}
            for stat_name, col_a, col_b in RAW_STATS:
                col = col_a if side == "a" else col_b
                entry["stats"][stat_name] = row[col]
            team_history[team].append(entry)

        if (idx + 1) % 500 == 0:
            print(f"  Processed {idx + 1} / {len(df)} rows...")

    print(f"  Processed {len(df)} / {len(df)} rows")
    return pd.DataFrame(output_rows)


# ══════════════════════════════════════════════════════════════════
# DIFFERENTIAL FEATURES
# ══════════════════════════════════════════════════════════════════

def add_differential_features(df):
    a_cols = [c for c in df.columns if c.endswith("_a") and c != "team_a"]
    added = 0
    for a_col in a_cols:
        b_col = a_col[:-2] + "_b"
        if b_col in df.columns:
            diff_col = a_col[:-2] + "_diff"
            df[diff_col] = df[a_col] - df[b_col]
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

def trim_correlated(df, non_feature_cols, threshold=0.95):
    feature_cols = [c for c in df.columns if c not in non_feature_cols]
    feature_df = df[feature_cols].dropna()

    if len(feature_df) == 0:
        return df, feature_cols

    corr_matrix = feature_df.corr().abs()
    upper = corr_matrix.where(
        np.triu(np.ones(corr_matrix.shape), k=1).astype(bool)
    )

    to_drop = set()
    for col in upper.columns:
        correlated = upper.index[upper[col] > threshold].tolist()
        if correlated:
            to_drop.add(col)

    remaining = [c for c in feature_cols if c not in to_drop]
    print(f"  Trimmed {len(to_drop)} correlated features (r > {threshold})")
    print(f"  Features: {len(feature_cols)} → {len(remaining)}")

    df = df.drop(columns=list(to_drop))
    return df, remaining


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

NON_FEATURE_COLS = ["date", "map", "team_a", "team_b", "target", "winner"]


def main():
    print("Loading and filtering data...")
    df = load_and_filter(INPUT_FILE)

    print(f"\nComputing trailing features for {len(df)} maps...")
    features_df = compute_trailing_features(df)

    print(f"\nAdding differential features...")
    features_df = add_differential_features(features_df)

    print(f"\nRandomly swapping Team A/B...")
    features_df = random_swap_teams(features_df)

    features_df = features_df.drop(columns=["winner"])

    print(f"\nTrimming correlated features...")
    non_feat = [c for c in NON_FEATURE_COLS if c in features_df.columns]
    features_df, remaining_features = trim_correlated(
        features_df, non_feat, CORRELATION_THRESHOLD
    )

    # Round floats
    float_cols = features_df.select_dtypes(include=["float64"]).columns
    features_df[float_cols] = features_df[float_cols].round(3)

    # Report
    feature_cols = [c for c in features_df.columns if c not in non_feat]
    n_total = len(features_df)
    n_with_nans = features_df[feature_cols].isna().any(axis=1).sum()

    print(f"\n{'═' * 50}")
    print(f"  DATASET SUMMARY")
    print(f"{'═' * 50}")
    print(f"  Total rows: {n_total}")
    print(f"  Features: {len(feature_cols)}")
    print(f"  Rows with NaN (cold start): {n_with_nans}")
    print(f"  Rows fully populated: {n_total - n_with_nans}")
    print(f"  Target balance: {features_df['target'].mean():.1%} Team A wins")

    print(f"\n  All features:")
    for c in sorted(feature_cols):
        print(f"    {c}")

    features_df.to_csv(OUTPUT_FILE, index=False)
    print(f"\n  Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
