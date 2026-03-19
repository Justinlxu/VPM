"""
Elo Hyperparameter Search

Grid searches over RANKING_K, RANKING_CHANGE_MIN, RANKING_CHANGE_MAX.
Uses fixed model hyperparameters (from the last full training run) to
keep each iteration fast — no CV search, just a single train/test split.

Usage:
    python elo_hyperparam_search.py
"""

import os
import sys
import itertools
import pandas as pd
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score

sys.path.insert(0, os.path.dirname(__file__))
from feature_engineering_elo import (
    load_and_filter, compute_fkfd_features, add_elo_features,
    add_differential_features, random_swap_teams,
    INPUT_FILE, KEEP_FEATURES, NON_FEATURE_COLS, CORRELATION_THRESHOLD
)
from player_elo import load_player_data

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

# Fixed model hyperparams from last full training run
FIXED_MODEL_PARAMS = {
    "min_samples_leaf": 5,
    "max_leaf_nodes":   15,
    "max_iter":         300,
    "max_depth":        5,
    "learning_rate":    0.01,
    "l2_regularization": 10.0,
    "random_state":     42,
    "early_stopping":   True,
    "validation_fraction": 0.15,
    "n_iter_no_change": 15,
}

TEST_FRACTION = 0.20
SEED = 42

# Elo param grid
K_VALUES          = [32, 48, 64, 96]
CHANGE_MIN_VALUES = [5, 10, 15]
CHANGE_MAX_VALUES = [15, 20, 30]


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def build_features(k, change_min, change_max):
    df = load_and_filter(INPUT_FILE)
    features_df = compute_fkfd_features(df)
    features_df = add_elo_features(features_df, INPUT_FILE,
                                   k=k, change_min=change_min, change_max=change_max)
    features_df = add_differential_features(features_df)
    features_df = random_swap_teams(features_df)
    features_df = features_df.drop(columns=["winner"], errors="ignore")

    # Keep only the configured features
    feature_cols_now = [c for c in features_df.columns if c not in NON_FEATURE_COLS]
    to_drop = [c for c in feature_cols_now if c not in KEEP_FEATURES]
    features_df = features_df.drop(columns=to_drop)

    return features_df


def train_and_evaluate(features_df):
    features_df = features_df.sort_values("date").reset_index(drop=True)
    feature_cols = [c for c in features_df.columns if c not in NON_FEATURE_COLS]

    X = features_df[feature_cols]
    y = features_df["target"]

    split_idx = int(len(X) * (1 - TEST_FRACTION))
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    model = HistGradientBoostingClassifier(**FIXED_MODEL_PARAMS)
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_prob)
    return acc, auc


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    combos = [
        (k, mn, mx)
        for k, mn, mx in itertools.product(K_VALUES, CHANGE_MIN_VALUES, CHANGE_MAX_VALUES)
        if mx > mn
    ]

    print(f"Running {len(combos)} Elo hyperparameter combinations...\n")
    print(f"  {'K':>4}  {'Min':>4}  {'Max':>4}  {'Accuracy':>9}  {'ROC AUC':>8}")
    print(f"  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*9}  {'─'*8}")

    results = []
    baseline_acc = None

    for i, (k, change_min, change_max) in enumerate(combos, 1):
        marker = " <-- current" if (k == 64 and change_min == 10 and change_max == 20) else ""
        try:
            features_df = build_features(k, change_min, change_max)
            acc, auc = train_and_evaluate(features_df)
            results.append((k, change_min, change_max, acc, auc))
            print(f"  {k:>4}  {change_min:>4}  {change_max:>4}  {acc:>9.1%}  {auc:>8.4f}{marker}")
            if k == 64 and change_min == 10 and change_max == 20:
                baseline_acc = acc
        except Exception as e:
            print(f"  {k:>4}  {change_min:>4}  {change_max:>4}  ERROR: {e}")

    if not results:
        return

    results.sort(key=lambda x: x[3], reverse=True)
    best = results[0]

    print(f"\n{'═' * 55}")
    print(f"  BEST COMBO")
    print(f"{'═' * 55}")
    print(f"  K={best[0]}, change_min={best[1]}, change_max={best[2]}")
    print(f"  Accuracy: {best[3]:.1%}  ROC AUC: {best[4]:.4f}")
    if baseline_acc is not None:
        print(f"  vs current (K=64, min=10, max=20): {baseline_acc:.1%}")
        print(f"  Delta: {best[3] - baseline_acc:+.1%}")


if __name__ == "__main__":
    main()
