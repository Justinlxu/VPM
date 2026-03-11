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
import requests
from bs4 import BeautifulSoup
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


# ── Upcoming matches (cached 5 min) ───────────────────────────────
@st.cache_data(ttl=300)
def fetch_upcoming_matches():
    try:
        headers = {"User-Agent": "ValorantPredictor/1.0 (esports research project)"}
        resp = requests.get("https://www.vlr.gg/matches", headers=headers, timeout=10)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        matches = []

        for link in soup.select("a.match-item"):
            # Team names
            team_els = link.select(".match-item-vs-team-name")
            if len(team_els) < 2:
                continue
            team_a = team_els[0].get_text(strip=True)
            team_b = team_els[1].get_text(strip=True)
            if not team_a or not team_b or team_a == "TBD" or team_b == "TBD":
                continue

            # Series score (maps won)
            score_spans = link.select(".match-item-vs-team-score")
            series_scores = [s.get_text(strip=True) for s in score_spans]
            has_score = any(s.isdigit() for s in series_scores)
            maps_a = int(series_scores[0]) if len(series_scores) > 0 and series_scores[0].isdigit() else 0
            maps_b = int(series_scores[1]) if len(series_scores) > 1 and series_scores[1].isdigit() else 0

            # Live status
            eta_el = link.select_one(".match-item-eta")
            eta_text = eta_el.get_text(strip=True).lower() if eta_el else ""
            is_live = "live" in eta_text

            # Time label
            time_label = ""
            if has_score and not is_live:
                time_label = "Finished"
            elif is_live:
                time_label = "🔴 Live"
            else:
                ts_el = link.select_one(".moment-tz-convert") or link.select_one(".match-item-time")
                if ts_el:
                    utc_ts = ts_el.get("data-utc-ts", "").strip()
                    if utc_ts:
                        try:
                            from datetime import datetime, timezone
                            dt = datetime.strptime(utc_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            time_label = dt.strftime("%b %d, %I:%M %p UTC").replace(" 0", " ")
                        except ValueError:
                            time_label = ts_el.get_text(strip=True)
                    else:
                        time_label = ts_el.get_text(strip=True)
                if not time_label:
                    raw = eta_el.get_text(strip=True) if eta_el else ""
                    time_label = raw if raw.lower() not in ("upcoming", "") else ""

            matches.append({
                "team_a": team_a,
                "team_b": team_b,
                "maps_a": maps_a,
                "maps_b": maps_b,
                "time_label": time_label,
                "is_live": is_live,
                "is_finished": has_score and not is_live,
            })

        return matches
    except Exception:
        return []


def fuzzy_match_team(name, teams):
    """Find the closest team name in our model data."""
    name_lower = name.strip().lower()
    for t in teams:
        if t.lower() == name_lower:
            return t
    matches = [t for t in teams if name_lower in t.lower() or t.lower() in name_lower]
    return matches[0] if len(matches) == 1 else None


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
st.title("Valorant Prediction Model")

try:
    model, feature_cols, team_history, teams, maps_seen = load_data()
except Exception as e:
    st.error(f"Failed to load model or data: {e}")
    st.stop()

# ── Card CSS ────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="column"] [data-testid="baseButton-secondary"] {
    background: transparent !important;
    border: 1px solid rgba(150,150,150,0.25) !important;
    border-radius: 10px !important;
    text-align: left !important;
    padding: 12px 14px !important;
    height: auto !important;
    min-height: 80px !important;
    width: 100% !important;
    white-space: pre-line !important;
    line-height: 1.7 !important;
    color: inherit !important;
    font-weight: normal !important;
}
[data-testid="column"] [data-testid="baseButton-secondary"] p {
    text-align: left !important;
    white-space: pre-line !important;
    margin: 0 !important;
}
[data-testid="column"] [data-testid="baseButton-secondary"]:hover {
    border-color: rgba(150,150,150,0.55) !important;
    background: rgba(255,255,255,0.03) !important;
    color: inherit !important;
}
</style>
""", unsafe_allow_html=True)

# ── Upcoming matches ───────────────────────────────────────────────
if "sel_team_a" not in st.session_state:
    st.session_state.sel_team_a = None
if "sel_team_b" not in st.session_state:
    st.session_state.sel_team_b = None

upcoming = fetch_upcoming_matches()
if upcoming:
    live      = [m for m in upcoming if m["is_live"]]
    scheduled = [m for m in upcoming if not m["is_live"] and not m["is_finished"]]

    def match_cards(matches, prefix):
        matches = [m for m in matches
                   if fuzzy_match_team(m["team_a"], teams) and fuzzy_match_team(m["team_b"], teams)]
        if not matches:
            return
        cols = st.columns(len(matches))
        for col, m in zip(cols, matches):
            with col:
                if m["is_live"]:
                    score_a = f"  {m['maps_a']}"
                    score_b = f"  {m['maps_b']}"
                else:
                    score_a = ""
                    score_b = ""
                label = f"{m['team_a']}{score_a}  \n{m['team_b']}{score_b}  \n{m['time_label']}"
                if st.button(label, key=f"{prefix}_{m['team_a']}_{m['team_b']}", use_container_width=True):
                    st.session_state.sel_team_a = fuzzy_match_team(m["team_a"], teams)
                    st.session_state.sel_team_b = fuzzy_match_team(m["team_b"], teams)

    if live:
        st.subheader("Live now")
        match_cards(live[:5], "live")

    if scheduled:
        st.subheader("Upcoming")
        match_cards(scheduled[:5], "up")

st.divider()

# ── Prediction form ────────────────────────────────────────────────
def team_index(name):
    if name and name in teams:
        return teams.index(name)
    return None

col1, col2 = st.columns(2)
with col1:
    team_a = st.selectbox("Team A", teams, index=team_index(st.session_state.sel_team_a), placeholder="Select team...")
with col2:
    team_b = st.selectbox("Team B", teams, index=team_index(st.session_state.sel_team_b), placeholder="Select team...")

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

        feat_dict = dict(zip(feature_cols, features_row.iloc[0]))

        st.subheader("Last 5 maps overall")
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
        st.dataframe(pd.DataFrame(rows).set_index("Stat"), use_container_width=True)

        st.subheader(f"Last 5 maps on {map_name}")
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
        st.dataframe(pd.DataFrame(rows_map).set_index("Stat"), use_container_width=True)
