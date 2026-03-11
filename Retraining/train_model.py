"""
Valorant Match Prediction — Model Training Pipeline

Uses HistGradientBoostingClassifier (scikit-learn's gradient boosted trees,
functionally equivalent to XGBoost — handles NaNs natively).

Steps:
1. Load training-ready CSV
2. Chronological train/test split
3. Hyperparameter search via cross-validation
4. Train final model with best params
5. Evaluate: accuracy, calibration, feature importance
"""

import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import RandomizedSearchCV, TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, log_loss, brier_score_loss,
    classification_report, roc_auc_score
)
from sklearn.calibration import calibration_curve
import joblib
import json

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

INPUT_FILE  = os.path.join(os.path.dirname(__file__), "training_data.csv")
OUTPUT_DIR  = os.path.dirname(__file__)
PREDICT_DIR = os.path.join(os.path.dirname(__file__), "..", "Predict")

# Chronological split: use most recent 20% as test set
TEST_FRACTION = 0.20

# Columns that are NOT features
NON_FEATURE_COLS = ["date", "map", "team_a", "team_b", "target"]

# Hyperparameter search space
PARAM_GRID = {
    "max_depth": [3, 4, 5, 6],
    "learning_rate": [0.01, 0.03, 0.05, 0.1],
    "max_iter": [100, 200, 300, 500],
    "min_samples_leaf": [5, 10, 20, 50],
    "max_leaf_nodes": [15, 31, 63],
    "l2_regularization": [0.0, 0.1, 1.0, 10.0],
}

# Number of random combinations to try
N_SEARCH_ITER = 80

SEED = 42


# ══════════════════════════════════════════════════════════════════
# LOAD DATA
# ══════════════════════════════════════════════════════════════════

def load_data(filepath):
    """Load CSV and split into features and target."""
    df = pd.read_csv(filepath)

    # Sort by date to ensure chronological order
    df = df.sort_values("date").reset_index(drop=True)

    feature_cols = [c for c in df.columns if c not in NON_FEATURE_COLS]

    X = df[feature_cols]
    y = df["target"]
    meta = df[NON_FEATURE_COLS]

    print(f"Loaded {len(df)} rows, {len(feature_cols)} features")
    return X, y, meta, feature_cols


# ══════════════════════════════════════════════════════════════════
# CHRONOLOGICAL SPLIT
# ══════════════════════════════════════════════════════════════════

def chrono_split(X, y, meta, test_fraction=0.20):
    """Split data chronologically — last test_fraction% as test set."""
    split_idx = int(len(X) * (1 - test_fraction))

    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    meta_train, meta_test = meta.iloc[:split_idx], meta.iloc[split_idx:]

    print(f"\nChronological split:")
    print(f"  Train: {len(X_train)} rows ({meta_train['date'].min()} to {meta_train['date'].max()})")
    print(f"  Test:  {len(X_test)} rows ({meta_test['date'].min()} to {meta_test['date'].max()})")
    print(f"  Train target balance: {y_train.mean():.1%} Team A wins")
    print(f"  Test target balance:  {y_test.mean():.1%} Team A wins")

    return X_train, X_test, y_train, y_test, meta_train, meta_test


# ══════════════════════════════════════════════════════════════════
# HYPERPARAMETER SEARCH
# ══════════════════════════════════════════════════════════════════

def hyperparameter_search(X_train, y_train):
    """
    Run randomized search over hyperparameters using time-series
    cross-validation (respects chronological order within training set).
    """
    print(f"\nRunning hyperparameter search ({N_SEARCH_ITER} combinations)...")

    base_model = HistGradientBoostingClassifier(
        random_state=SEED,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=15,
    )

    # TimeSeriesSplit ensures we always train on past, validate on future
    cv = TimeSeriesSplit(n_splits=5)

    search = RandomizedSearchCV(
        base_model,
        param_distributions=PARAM_GRID,
        n_iter=N_SEARCH_ITER,
        cv=cv,
        scoring="neg_log_loss",  # Optimize for probability quality
        random_state=SEED,
        n_jobs=-1,
        verbose=1,
    )

    search.fit(X_train, y_train)

    print(f"\nBest parameters:")
    for k, v in search.best_params_.items():
        print(f"  {k}: {v}")
    print(f"Best CV log-loss: {-search.best_score_:.4f}")

    return search.best_estimator_, search.best_params_, search.cv_results_


# ══════════════════════════════════════════════════════════════════
# EVALUATION
# ══════════════════════════════════════════════════════════════════

def evaluate_model(model, X_test, y_test, meta_test):
    """Evaluate model on test set — accuracy, log-loss, calibration."""

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]  # P(Team A wins)

    # Core metrics
    acc = accuracy_score(y_test, y_pred)
    logloss = log_loss(y_test, y_prob)
    brier = brier_score_loss(y_test, y_prob)
    auc = roc_auc_score(y_test, y_prob)

    print(f"\n{'═' * 50}")
    print(f"  TEST SET RESULTS ({len(y_test)} matches)")
    print(f"{'═' * 50}")
    print(f"  Accuracy:    {acc:.1%}")
    print(f"  Log-loss:    {logloss:.4f}")
    print(f"  Brier score: {brier:.4f}")
    print(f"  ROC AUC:     {auc:.4f}")
    print(f"\n  (Baseline accuracy from always picking majority: {max(y_test.mean(), 1-y_test.mean()):.1%})")

    # Classification report
    print(f"\n{classification_report(y_test, y_pred, target_names=['Team B wins', 'Team A wins'])}")

    # Confidence breakdown
    print(f"\n  Confidence breakdown:")
    bins = [(0.5, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
            (0.70, 0.80), (0.80, 1.0)]
    for lo, hi in bins:
        # Model confidence = max(prob, 1-prob) regardless of which team
        confidence = np.maximum(y_prob, 1 - y_prob)
        mask = (confidence >= lo) & (confidence < hi)
        if mask.sum() > 0:
            # Correct if (prob > 0.5 and team A won) or (prob < 0.5 and team B won)
            correct = ((y_prob[mask] > 0.5) == (y_test.values[mask] == 1))
            n = mask.sum()
            pct = correct.mean()
            print(f"    {lo:.0%}-{hi:.0%} confidence: {n:4d} matches, {pct:.1%} correct")

    return {
        "accuracy": acc,
        "log_loss": logloss,
        "brier_score": brier,
        "roc_auc": auc,
        "n_test": len(y_test),
    }


# ══════════════════════════════════════════════════════════════════
# CALIBRATION PLOT
# ══════════════════════════════════════════════════════════════════

def plot_calibration(model, X_test, y_test):
    """Generate a calibration plot — predicted probability vs actual win rate."""
    y_prob = model.predict_proba(X_test)[:, 1]

    prob_true, prob_pred = calibration_curve(y_test, y_prob, n_bins=10, strategy="uniform")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # Calibration curve
    ax1.plot([0, 1], [0, 1], "k--", label="Perfectly calibrated", alpha=0.5)
    ax1.plot(prob_pred, prob_true, "o-", color="#4A90D9", linewidth=2,
             markersize=8, label="Model")
    ax1.set_xlabel("Predicted probability (Team A wins)", fontsize=11)
    ax1.set_ylabel("Actual win rate", fontsize=11)
    ax1.set_title("Calibration Curve", fontsize=13, fontweight="bold")
    ax1.legend(fontsize=10)
    ax1.set_xlim(0, 1)
    ax1.set_ylim(0, 1)
    ax1.grid(True, alpha=0.3)

    # Prediction distribution
    ax2.hist(y_prob[y_test == 1], bins=30, alpha=0.6, color="#4CAF50",
             label="Team A actually won", density=True)
    ax2.hist(y_prob[y_test == 0], bins=30, alpha=0.6, color="#F44336",
             label="Team B actually won", density=True)
    ax2.set_xlabel("Predicted probability (Team A wins)", fontsize=11)
    ax2.set_ylabel("Density", fontsize=11)
    ax2.set_title("Prediction Distribution", fontsize=13, fontweight="bold")
    ax2.legend(fontsize=10)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    path = f"{OUTPUT_DIR}/calibration_plot.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\n  Calibration plot saved to {path}")


# ══════════════════════════════════════════════════════════════════
# FEATURE IMPORTANCE
# ══════════════════════════════════════════════════════════════════

def plot_feature_importance(model, feature_cols, X_test, y_test, top_n=25):
    """Plot top N most important features using permutation importance."""
    from sklearn.inspection import permutation_importance
    print("\n  Computing permutation importance (this may take a moment)...")
    result = permutation_importance(model, X_test, y_test, n_repeats=10,
                                    random_state=SEED, scoring="neg_log_loss")
    importances = result.importances_mean
    indices = np.argsort(importances)[::-1][:top_n]

    fig, ax = plt.subplots(figsize=(10, 8))
    names = [feature_cols[i] for i in indices]
    values = importances[indices]

    bars = ax.barh(range(len(names)), values[::-1], color="#4A90D9", alpha=0.8)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names[::-1], fontsize=9)
    ax.set_xlabel("Feature Importance", fontsize=11)
    ax.set_title(f"Top {top_n} Most Important Features", fontsize=13, fontweight="bold")
    ax.grid(True, axis="x", alpha=0.3)

    plt.tight_layout()
    path = f"{OUTPUT_DIR}/feature_importance.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Feature importance plot saved to {path}")

    # Print top features
    print(f"\n  Top {top_n} features:")
    for rank, idx in enumerate(indices, 1):
        print(f"    {rank:2d}. {feature_cols[idx]:40s} {importances[idx]:.4f}")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    # Load
    X, y, meta, feature_cols = load_data(INPUT_FILE)

    # Split
    X_train, X_test, y_train, y_test, meta_train, meta_test = chrono_split(
        X, y, meta, TEST_FRACTION
    )

    # Hyperparameter search
    best_model, best_params, cv_results = hyperparameter_search(X_train, y_train)

    # Evaluate
    metrics = evaluate_model(best_model, X_test, y_test, meta_test)

    # Plots
    plot_calibration(best_model, X_test, y_test)
    plot_feature_importance(best_model, feature_cols, X_test, y_test)

    # Save model and metadata
    model_path = os.path.join(PREDICT_DIR, "valorant_model.joblib")
    joblib.dump(best_model, model_path)
    print(f"\n  Model saved to {model_path}")

    # Save metadata
    meta_out = {
        "best_params": best_params,
        "metrics": {k: float(v) for k, v in metrics.items()},
        "feature_cols": feature_cols,
        "train_date_range": [meta_train["date"].min(), meta_train["date"].max()],
        "test_date_range": [meta_test["date"].min(), meta_test["date"].max()],
    }
    meta_path = os.path.join(PREDICT_DIR, "model_metadata.json")
    with open(meta_path, "w") as f:
        json.dump(meta_out, f, indent=2)
    print(f"  Metadata saved to {meta_path}")


if __name__ == "__main__":
    main()
