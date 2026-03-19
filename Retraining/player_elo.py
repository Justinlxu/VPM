"""
Player Elo System

Computes two features per team for use in match prediction:
  - ranking_elo  : sum of 5 players' ranking Elo (absolute skill)
  - ranking_trend: slope of team ranking Elo total over last 5 maps (form)

Ranking Elo is updated each map by ranking all 10 players 1-N on a
role-normalized composite score (ACS 40%, KAST 40%, FK/FD diff 20%).
Top half gain Elo, bottom half lose, scaled by margin and opposing team Elo.
"""

import os
import pandas as pd
import numpy as np

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "Scraper", "valorant_data.xlsx")

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

RANKING_ELO_START = 1000
RANKING_ELO_FLOOR = 100
RANKING_K = 64
RANKING_CHANGE_MIN = 15
RANKING_CHANGE_MAX = 30

DUELISTS = {"jett", "raze", "reyna", "phoenix", "neon", "iso", "yoru", "waylay"}

WEIGHTS = (0.40, 0.40, 0.20)  # ACS, KAST, FK/FD diff

TREND_WINDOW = 5  # Maps to use for team Elo trend calculation

# Fallback defaults used before enough history has accumulated
_ROLE_DEFAULTS = {
    "duelist": {"acs": 200.0, "kast": 68.0, "fkfd": 0.0},
    "support": {"acs": 160.0, "kast": 72.0, "fkfd": 0.0},
}
_ROLE_DEFAULT_STDS = {
    "duelist": {"acs": 40.0, "kast": 10.0, "fkfd": 2.0},
    "support": {"acs": 35.0, "kast": 10.0, "fkfd": 2.0},
}
_MIN_SAMPLES = 20


# ══════════════════════════════════════════════════════════════════
# ROLLING ROLE AVERAGES
# ══════════════════════════════════════════════════════════════════

def init_role_stats():
    return {
        "duelist": {"acs": [], "kast": [], "fkfd": []},
        "support": {"acs": [], "kast": [], "fkfd": []},
    }


def get_role_avgs_stds(role_stats):
    avgs, stds = {}, {}
    for role in ("duelist", "support"):
        avgs[role], stds[role] = {}, {}
        for stat in ("acs", "kast", "fkfd"):
            vals = role_stats[role][stat]
            if len(vals) >= _MIN_SAMPLES:
                avgs[role][stat] = float(np.mean(vals))
                stds[role][stat] = float(np.std(vals))
            else:
                avgs[role][stat] = _ROLE_DEFAULTS[role][stat]
                stds[role][stat] = _ROLE_DEFAULT_STDS[role][stat]
    return avgs, stds


def update_role_stats(role_stats, map_df):
    for _, row in map_df.iterrows():
        role = "duelist" if str(row["Agent"]).lower() in DUELISTS else "support"
        role_stats[role]["acs"].append(row["ACS"])
        role_stats[role]["kast"].append(row["KAST"])
        role_stats[role]["fkfd"].append(row["FK"] - row["FD"])


# ══════════════════════════════════════════════════════════════════
# COMPOSITE SCORE AND RANKING
# ══════════════════════════════════════════════════════════════════

def composite_score(row, role_avgs, role_stds):
    role = "duelist" if str(row["Agent"]).lower() in DUELISTS else "support"
    avgs = role_avgs[role]
    stds = role_stds[role]
    fk_fd_diff = row["FK"] - row["FD"]

    def normalize(val, avg, std):
        return (val - avg) / std if std > 0 else 0.0

    acs_norm  = normalize(row["ACS"],  avgs["acs"],  stds["acs"])
    kast_norm = normalize(row["KAST"], avgs["kast"], stds["kast"])
    fkfd_norm = normalize(fk_fd_diff,  avgs["fkfd"], stds["fkfd"])

    return WEIGHTS[0] * acs_norm + WEIGHTS[1] * kast_norm + WEIGHTS[2] * fkfd_norm


def rank_map_players(map_df, role_avgs, role_stds):
    map_df = map_df.copy()
    map_df["composite_score"] = map_df.apply(
        lambda row: composite_score(row, role_avgs, role_stds), axis=1
    )
    map_df = map_df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    map_df["rank"] = range(1, len(map_df) + 1)

    n = len(map_df)
    split = n // 2
    boundary = (map_df.iloc[split - 1]["composite_score"] + map_df.iloc[split]["composite_score"]) / 2

    score_range = map_df["composite_score"].max() - map_df["composite_score"].min()
    if score_range == 0:
        map_df["margin_to_boundary"] = 0.0
    else:
        map_df["margin_to_boundary"] = (map_df["composite_score"] - boundary).abs() / score_range

    return map_df


# ══════════════════════════════════════════════════════════════════
# ELO GAIN/LOSS
# ══════════════════════════════════════════════════════════════════

def compute_ranking_elo_changes(ranked_df, ranking_elos, k=None, change_min=None, change_max=None):
    k          = k          if k          is not None else RANKING_K
    change_min = change_min if change_min is not None else RANKING_CHANGE_MIN
    change_max = change_max if change_max is not None else RANKING_CHANGE_MAX

    teams = ranked_df["Team"].unique()
    if len(teams) != 2:
        return {row["Player Name"]: 0.0 for _, row in ranked_df.iterrows()}

    team_a, team_b = teams[0], teams[1]

    def avg_elo(team):
        players = ranked_df[ranked_df["Team"] == team]["Player Name"].tolist()
        return np.mean([ranking_elos.get(p, RANKING_ELO_START) for p in players])

    avg_elo_a = avg_elo(team_a)
    avg_elo_b = avg_elo(team_b)
    field_avg_elo = np.mean([ranking_elos.get(p, RANKING_ELO_START)
                             for p in ranked_df["Player Name"]])
    split = len(ranked_df) // 2

    changes = {}
    for _, row in ranked_df.iterrows():
        player = row["Player Name"]
        rank = row["rank"]
        margin = row["margin_to_boundary"]
        team = row["Team"]

        current_elo = ranking_elos.get(player, RANKING_ELO_START)
        opponent_avg_elo = avg_elo_b if team == team_a else avg_elo_a
        opponent_factor = opponent_avg_elo / RANKING_ELO_START
        own_factor = (current_elo / max(field_avg_elo, 1.0)) ** 2

        if rank <= split:
            raw = k * margin * opponent_factor / own_factor
            changes[player] = float(np.clip(raw, change_min, change_max))
        else:
            raw = k * margin * own_factor / opponent_factor
            loss = float(np.clip(raw, change_min, change_max))
            changes[player] = max(-loss, RANKING_ELO_FLOOR - current_elo)

    return changes


# ══════════════════════════════════════════════════════════════════
# FULL CHRONOLOGICAL LOOP
# ══════════════════════════════════════════════════════════════════

def build_ranking_elo(df, k=None, change_min=None, change_max=None):
    """
    Process all maps chronologically and compute ranking Elo for every player.

    Returns:
      ranking_elos        : dict {player_name: final_ranking_elo}
      ranking_elo_history : dict {match_id: {player_name: elo_before_map}}
      player_elo_log      : dict {player_name: [(date, map_id, elo_after)]}
    """
    ranking_elos = {}
    ranking_elo_history = {}
    player_elo_log = {}
    role_stats = init_role_stats()

    map_ids = df["Match ID"].unique()
    print(f"\nBuilding ranking Elo across {len(map_ids)} maps...")

    for i, map_id in enumerate(map_ids):
        map_df = df[df["Match ID"] == map_id].copy()
        date = map_df["Date"].iloc[0]

        for player in map_df["Player Name"]:
            if player not in ranking_elos:
                ranking_elos[player] = RANKING_ELO_START
            if player not in player_elo_log:
                player_elo_log[player] = []

        ranking_elo_history[map_id] = {p: ranking_elos[p] for p in map_df["Player Name"]}

        role_avgs, role_stds = get_role_avgs_stds(role_stats)
        ranked = rank_map_players(map_df, role_avgs, role_stds)

        changes = compute_ranking_elo_changes(ranked, ranking_elos, k=k, change_min=change_min, change_max=change_max)
        for player, delta in changes.items():
            ranking_elos[player] = ranking_elos[player] + delta
            player_elo_log[player].append((date, map_id, ranking_elos[player]))

        update_role_stats(role_stats, map_df)

        if (i + 1) % 200 == 0:
            print(f"  Processed {i + 1} / {len(map_ids)} maps...")

    print(f"  Processed {len(map_ids)} / {len(map_ids)} maps")
    print(f"  Tracked {len(ranking_elos)} unique players")
    return ranking_elos, ranking_elo_history, player_elo_log


# ══════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════

def load_player_data(filepath):
    """Load player stats and join dates from map-level sheet."""
    maps_df = pd.read_excel(filepath, sheet_name="Model Features", usecols=["Match ID", "Date"])
    players_df = pd.read_excel(filepath, sheet_name="Player Stats")
    players_df = players_df.merge(maps_df, on="Match ID", how="left")

    missing_dates = players_df["Date"].isna().sum()
    if missing_dates > 0:
        print(f"  Warning: {missing_dates} player rows could not be matched to a map date")

    players_df["Date"] = pd.to_datetime(players_df["Date"])
    players_df = players_df.sort_values(["Date", "Match ID"]).reset_index(drop=True)

    print(f"  Loaded {len(players_df)} player rows across {players_df['Match ID'].nunique()} maps")
    print(f"  Date range: {players_df['Date'].min().date()} to {players_df['Date'].max().date()}")
    print(f"  Unique players: {players_df['Player Name'].nunique()}")

    return players_df


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Player Elo system")
    parser.add_argument("--elohistory", type=str, metavar="PLAYER",
                        help="Show ranking Elo history for a player (last 10 maps)")
    args = parser.parse_args()

    df = load_player_data(DATA_FILE)
    ranking_elos, ranking_elo_history, player_elo_log = build_ranking_elo(df)

    if args.elohistory:
        query = args.elohistory.strip().lower()
        matches = [p for p in player_elo_log if query in p.lower()]

        if not matches:
            print(f"\n  No player found matching '{args.elohistory}'")
        elif len(matches) > 1:
            print(f"\n  Multiple matches: {', '.join(matches)}")
        else:
            player = matches[0]
            log = player_elo_log[player][-10:]
            print(f"\n  Elo history — {player} (last {len(log)} maps):")
            print(f"  {'Date':<12s}  {'Map ID':<25s}  {'Elo':>7s}  {'Change':>8s}")
            print(f"  {'─' * 58}")
            prev = player_elo_log[player][-len(log) - 1][2] if len(player_elo_log[player]) > len(log) else RANKING_ELO_START
            for date, map_id, elo in log:
                change = elo - prev
                sign = "+" if change >= 0 else ""
                print(f"  {str(date.date()):<12s}  {map_id:<25s}  {elo:>7.1f}  {sign}{change:>7.1f}")
                prev = elo
            print(f"\n  Current Elo: {ranking_elos[player]:.1f}")
    else:
        sorted_ranking = sorted(ranking_elos.items(), key=lambda x: x[1], reverse=True)
        print(f"\nTop 10 players by ranking Elo:")
        for player, elo in sorted_ranking[:10]:
            print(f"  {player:<20s} {elo:.1f}")
        print(f"\nBottom 10 players by ranking Elo:")
        for player, elo in sorted_ranking[-10:]:
            print(f"  {player:<20s} {elo:.1f}")
