"""
Valorant Match Predictor - Streamlit Web App
"""

import os
import sys
import streamlit as st
import pandas as pd
import numpy as np
import joblib
import json
from collections import defaultdict

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(__file__)
EXCEL_FILE  = os.path.join(BASE_DIR, "Scraper", "valorant_data.xlsx")
MODEL_FILE  = os.path.join(BASE_DIR, "Predict", "valorant_model.joblib")
META_FILE   = os.path.join(BASE_DIR, "Predict", "model_metadata.json")

WINDOWS = [5, 10]
RAW_STATS = [
    ("acs",        "ACS A",        "ACS B"),
    ("kast",       "KAST A",       "KAST B"),
    ("fk_fd_diff", "FK/FD Diff A", "FK/FD Diff B"),
    ("gun_rate",   "Gun Rate A",   "Gun Rate B"),
]


# ── Data loading (cached) ──────────────────────────────────────────
@st.cache_data
def load_data():
    model = joblib.load(MODEL_FILE)
    with open(META_FILE) as f:
        meta = json.load(f)
    feature_cols = meta["feature_cols"]

    df = pd.read_excel(EXCEL_FILE, sheet_name="Model Features")
    df = df[df["Pistols Won A"] + df["Pistols Won B"] == 2].copy()
    df = df.sort_values("Date").reset_index(drop=True)
    df["Total Rounds"] = df["Score A"] + df["Score B"]
    df["Gun Rate A"]   = df["Gun Rounds Won A"] / df["Total Rounds"]
    df["Gun Rate B"]   = df["Gun Rounds Won B"] / df["Total Rounds"]

    team_history = defaultdict(list)
    for _, row in df.iterrows():
        for side, team in [("a", row["Team A"]), ("b", row["Team B"])]:
            entry = {"date": row["Date"], "map": row["Map"], "stats": {}}
            for stat_name, col_a, col_b in RAW_STATS:
                col = col_a if side == "a" else col_b
                entry["stats"][stat_name] = row[col]
            team_history[team].append(entry)

    teams     = sorted(team_history.keys())
    maps_seen = sorted(df["Map"].unique())

    return model, feature_cols, team_history, teams, maps_seen


# ── Feature computation ────────────────────────────────────────────
def compute_features(team_a, team_b, map_name, team_history, feature_cols):
    features = {}
    for side, team in [("a", team_a), ("b", team_b)]:
        history     = team_history.get(team, [])
        map_history = [h for h in history if h["map"] == map_name]
        features[f"prior_maps_{side}"]     = len(history)
        features[f"prior_maps_map_{side}"] = len(map_history)

        for stat_name, _, _ in RAW_STATS:
            for w in WINDOWS:
                recent = history[-w:] if len(history) >= w else history
                features[f"{stat_name}_overall_{w}_{side}"] = (
                    np.mean([h["stats"][stat_name] for h in recent]) if recent else np.nan
                )
                recent_map = map_history[-w:] if len(map_history) >= w else map_history
                features[f"{stat_name}_map_{w}_{side}"] = (
                    np.mean([h["stats"][stat_name] for h in recent_map]) if recent_map else np.nan
                )

    for a_col in [c for c in features if c.endswith("_a")]:
        b_col   = a_col[:-2] + "_b"
        diff_col = a_col[:-2] + "_diff"
        if b_col in features:
            a_val, b_val = features[a_col], features[b_col]
            features[diff_col] = np.nan if (pd.isna(a_val) or pd.isna(b_val)) else a_val - b_val

    row = {col: features.get(col, np.nan) for col in feature_cols}
    return pd.DataFrame([row])


# ── UI ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="VPM", page_icon="🎯", layout="centered")
st.title("Valorant Match Predictor")

try:
    model, feature_cols, team_history, teams, maps_seen = load_data()
except Exception as e:
    st.error(f"Failed to load model or data: {e}")
    st.stop()

col1, col2 = st.columns(2)
with col1:
    team_a = st.selectbox("Team A", teams, index=None, placeholder="Select team...")
with col2:
    team_b = st.selectbox("Team B", teams, index=None, placeholder="Select team...")

map_name = st.selectbox("Map", maps_seen, index=None, placeholder="Select map...")

if st.button("Predict", type="primary", use_container_width=True):
    if not team_a or not team_b or not map_name:
        st.warning("Please select both teams and a map.")
    elif team_a == team_b:
        st.warning("Teams must be different.")
    else:
        features_row = compute_features(team_a, team_b, map_name, team_history, feature_cols)
        prob_a = model.predict_proba(features_row)[0][1]
        prob_b = 1 - prob_a
        winner = team_a if prob_a > 0.5 else team_b
        conf   = max(prob_a, prob_b)

        st.divider()
        st.subheader(f"Predicted winner: **{winner}**")
        st.caption(f"Confidence: {conf:.1%}")

        col1, col2 = st.columns(2)
        with col1:
            st.metric(team_a, f"{prob_a:.1%}")
        with col2:
            st.metric(team_b, f"{prob_b:.1%}")

        st.divider()

        # Data available
        hist_a     = team_history.get(team_a, [])
        hist_b     = team_history.get(team_b, [])
        map_hist_a = [h for h in hist_a if h["map"] == map_name]
        map_hist_b = [h for h in hist_b if h["map"] == map_name]

        with st.expander("Data available"):
            st.write(f"**{team_a}:** {len(hist_a)} total maps, {len(map_hist_a)} on {map_name}")
            st.write(f"**{team_b}:** {len(hist_b)} total maps, {len(map_hist_b)} on {map_name}")

        # Key stats table
        feat_dict = dict(zip(feature_cols, features_row.iloc[0]))
        rows = []
        for stat, label in [("acs_overall_5", "ACS"), ("kast_overall_5", "KAST"),
                             ("fk_fd_diff_overall_5", "FK/FD Diff"), ("gun_rate_overall_5", "Gun Rate")]:
            a_val = feat_dict.get(f"{stat}_a", np.nan)
            b_val = feat_dict.get(f"{stat}_b", np.nan)
            rows.append({
                "Stat": label,
                team_a: f"{a_val:.2f}" if not pd.isna(a_val) else "N/A",
                team_b: f"{b_val:.2f}" if not pd.isna(b_val) else "N/A",
            })

        with st.expander("Key stats (last 5 maps overall)"):
            st.dataframe(pd.DataFrame(rows).set_index("Stat"), use_container_width=True)

        rows_map = []
        for stat, label in [("acs_map_5", "ACS"), ("kast_map_5", "KAST"),
                             ("fk_fd_diff_map_5", "FK/FD Diff"), ("gun_rate_map_5", "Gun Rate")]:
            a_val = feat_dict.get(f"{stat}_a", np.nan)
            b_val = feat_dict.get(f"{stat}_b", np.nan)
            rows_map.append({
                "Stat": label,
                team_a: f"{a_val:.2f}" if not pd.isna(a_val) else "N/A",
                team_b: f"{b_val:.2f}" if not pd.isna(b_val) else "N/A",
            })

        with st.expander(f"Key stats (last 5 maps on {map_name})"):
            st.dataframe(pd.DataFrame(rows_map).set_index("Stat"), use_container_width=True)
