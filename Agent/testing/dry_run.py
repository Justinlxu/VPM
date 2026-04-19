"""
Dry Run -- simulate matches to test the agent pipeline end-to-end.

Uses real teams from the dataset, runs the actual Elo model, and simulates
Polymarket prices with randomized edge. Walks through entry, then simulates
price movement to test trailing floor / stop loss.

Usage:
    python -m Agent.testing.dry_run
    python -m Agent.testing.dry_run --team-a "Fnatic" --team-b "LOUD"
    python -m Agent.testing.dry_run --team-a "Sentinels" --team-b "LOUD" --market-price 0.50
    python -m Agent.testing.dry_run --batch 1000
"""

import os
import sys
import time
import random
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from Agent.agent import (
    quarter_kelly, compute_floor, compute_stop_loss,
    EDGE_THRESHOLD, TRAILING_FLOOR_START, TRAILING_FLOOR_STEP, log_trade,
    DRYRUN_TRADE_LOG, DRYRUN_AGENT_LOG,
    logger as agent_logger,
)
import Agent.agent as _agent_mod
import logging
from Predict.predict import predict_elo_only, build_team_histories, EXCEL_FILE


# ══════════════════════════════════════════════════════════════
# CORE SIMULATION (quiet — no printing, used by batch mode)
# ══════════════════════════════════════════════════════════════

def simulate_one(team_a, team_b, prob_a, market_price_a=None,
                 bankroll=1000, seed=None):
    """
    Run a single simulated trade. Returns a result dict or None if no edge.

    Parameters
    ----------
    team_a, team_b : str    Resolved team names.
    prob_a : float          Model probability for team_a.
    market_price_a : float  Simulated market price for team_a (None = auto).
    bankroll : float        Starting bankroll.
    seed : int              Random seed for price path (None = random).
    """
    prob_b = 1 - prob_a

    # Simulate market price if not given
    if market_price_a is None:
        # Random edge between 5% and 8% for the favored team
        edge_size = random.uniform(0.05, 0.08)
        if prob_a > prob_b:
            market_price_a = prob_a - edge_size
        else:
            market_price_a = prob_a + edge_size
        market_price_a = max(0.05, min(0.95, market_price_a))

    market_b = round(1 - market_price_a, 4)
    market_a = round(market_price_a, 4)

    edge_a = prob_a - market_a
    edge_b = prob_b - market_b

    # Pick side
    if edge_a >= EDGE_THRESHOLD and edge_a >= edge_b:
        side, bet_team, prob, market_price, edge = "a", team_a, prob_a, market_a, edge_a
    elif edge_b >= EDGE_THRESHOLD:
        side, bet_team, prob, market_price, edge = "b", team_b, prob_b, market_b, edge_b
    else:
        return None  # No edge

    kelly_pct = quarter_kelly(prob, market_price)
    position_usd = bankroll * kelly_pct
    if position_usd < 1:
        return None
    shares = position_usd / market_price
    entry_price = market_price

    # Log entry
    log_trade({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "match_id": "DRY_RUN",
        "map_num": 1,
        "team_a": team_a, "team_b": team_b, "side": bet_team,
        "model_prob": f"{prob:.4f}",
        "market_price": f"{market_price:.4f}",
        "edge": f"{edge:.4f}",
        "kelly_pct": f"{kelly_pct:.4f}",
        "position_size_usd": f"{position_usd:.2f}",
        "entry_price": f"{entry_price:.4f}",
        "exit_price": "", "exit_reason": "open", "pnl": "",
    })

    # Simulate price path
    rng = random.Random(seed)
    current = entry_price
    high = entry_price
    stop = compute_stop_loss(entry_price)
    exit_price = None
    exit_reason = None

    # Random price path: drift direction and volatility vary
    drift_up = rng.uniform(0.003, 0.012)
    drift_down = rng.uniform(-0.015, -0.005)
    volatility = rng.uniform(0.003, 0.010)
    peak_tick = rng.randint(8, 25)

    for tick in range(1, 41):
        drift = drift_up if tick <= peak_tick else drift_down
        noise = rng.gauss(0, volatility)
        current = max(0.01, min(0.99, current + drift + noise))
        current = round(current, 4)

        if current > high:
            high = current

        if current <= stop:
            exit_price = current
            exit_reason = "stop_loss"
            break

        floor = compute_floor(entry_price, high)
        if floor is not None and current <= floor:
            exit_price = current
            exit_reason = "trailing_floor"
            break

    if exit_price is None:
        exit_price = current
        exit_reason = "simulation_end"

    pnl = (exit_price - entry_price) * shares

    # Log exit
    log_trade({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "match_id": "DRY_RUN",
        "map_num": 1,
        "team_a": team_a, "team_b": team_b, "side": bet_team,
        "model_prob": "", "market_price": "", "edge": "", "kelly_pct": "",
        "position_size_usd": f"{position_usd:.2f}",
        "entry_price": f"{entry_price:.4f}",
        "exit_price": f"{exit_price:.4f}",
        "exit_reason": exit_reason,
        "pnl": f"{pnl:.2f}",
    })

    return {
        "team_a": team_a, "team_b": team_b, "bet_team": bet_team,
        "prob": prob, "market_price": market_price, "edge": edge,
        "kelly_pct": kelly_pct, "position_usd": position_usd,
        "entry_price": entry_price, "exit_price": exit_price,
        "exit_reason": exit_reason, "pnl": pnl,
    }


# ══════════════════════════════════════════════════════════════
# BATCH MODE
# ══════════════════════════════════════════════════════════════

def _setup_dryrun_logging():
    """Route trade CSV and agent log to dryrun files."""
    _agent_mod._active_trade_log = DRYRUN_TRADE_LOG
    for h in agent_logger.handlers[:]:
        if isinstance(h, logging.FileHandler):
            agent_logger.removeHandler(h)
    fh = logging.FileHandler(DRYRUN_AGENT_LOG, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    agent_logger.addHandler(fh)


def run_batch(n=1000, starting_bankroll=1000.0):
    """Run n simulated trades with random matchups and market prices."""
    _setup_dryrun_logging()

    print(f"Loading dataset and building Elo ratings (one-time)...")

    # Load model and Elo data once, then reuse for all predictions
    import joblib, json
    import numpy as np
    from Predict.predict import (
        build_team_elo_features, compute_match_features, find_team,
    )
    from collections import defaultdict

    model_dir = os.path.join(os.path.dirname(__file__), "..", "..", "Predict")
    model = joblib.load(os.path.join(model_dir, "valorant_model.joblib"))
    with open(os.path.join(model_dir, "model_metadata.json")) as f:
        meta = json.load(f)
    feature_cols = meta["feature_cols"]

    team_history, teams, _, _ = build_team_histories(EXCEL_FILE)

    # Build Elo features once (this is the slow part)
    print("Building Elo ratings...")
    from Predict.predict import load_player_data, build_ranking_elo, RANKING_ELO_START
    player_df = load_player_data(EXCEL_FILE)
    _, ranking_elo_history, _ = build_ranking_elo(player_df)

    # Cache all final elos and team map history for fast lookups
    final_elos = {}
    for map_id in ranking_elo_history:
        for player, elo in ranking_elo_history[map_id].items():
            final_elos[player] = elo

    team_map_history = defaultdict(list)
    map_ids_ordered = player_df["Match ID"].unique()
    for map_id in map_ids_ordered:
        map_rows = player_df[player_df["Match ID"] == map_id]
        snap = ranking_elo_history.get(map_id, {})
        for team in map_rows["Team"].unique():
            players = map_rows[map_rows["Team"] == team]["Player Name"].tolist()
            elos = [snap.get(p, RANKING_ELO_START) for p in players]
            team_map_history[team].append((None, map_id, players, elos))

    ELO_TREND_WINDOW = 5

    def fast_predict(team_a, team_b):
        """Fast prediction reusing pre-loaded model and Elo data."""
        def get_elo(team):
            history = team_map_history.get(team, [])
            if not history:
                return RANKING_ELO_START * 5, 0.0
            _, _, players, _ = history[-1]
            elo_sum = sum(final_elos.get(p, RANKING_ELO_START) for p in players)
            recent = history[-ELO_TREND_WINDOW:]
            if len(recent) < 2:
                return elo_sum, 0.0
            totals = [sum(elos) for _, _, _, elos in recent]
            x = np.arange(len(totals))
            trend = float(np.polyfit(x, totals, 1)[0])
            return elo_sum, trend

        sum_a, trend_a = get_elo(team_a)
        sum_b, trend_b = get_elo(team_b)
        elo_features = {
            "ranking_elo_sum_diff": sum_a - sum_b,
            "ranking_elo_trend_diff": trend_a - trend_b,
            "ranking_elo_sum_a": sum_a, "ranking_elo_sum_b": sum_b,
            "ranking_elo_trend_a": trend_a, "ranking_elo_trend_b": trend_b,
        }
        features_row = compute_match_features(
            team_a, team_b, "", team_history, feature_cols, elo_features
        )
        return float(model.predict_proba(features_row)[0][1])

    # Build matchup pool using fast predictions
    pool_size = min(50, len(teams) * (len(teams) - 1) // 2)
    matchup_pool = []
    used = set()

    print(f"Building matchup pool ({pool_size} unique matchups)...")
    while len(matchup_pool) < pool_size:
        a, b = random.sample(teams, 2)
        key = tuple(sorted([a, b]))
        if key in used:
            continue
        used.add(key)
        try:
            prob_a = fast_predict(a, b)
            matchup_pool.append((a, b, prob_a))
        except Exception:
            continue
        if len(matchup_pool) % 10 == 0:
            print(f"  {len(matchup_pool)}/{pool_size} matchups loaded...")

    print(f"\nRunning {n} sequential trades (compounding bankroll)...")
    start = time.time()

    bankroll = starting_bankroll
    peak_bankroll = bankroll
    trough_bankroll = bankroll
    max_drawdown = 0.0

    results = []
    skipped = 0
    for i in range(n):
        team_a, team_b, prob_a = random.choice(matchup_pool)
        result = simulate_one(team_a, team_b, prob_a, bankroll=bankroll, seed=i)
        if result:
            results.append(result)
            bankroll += result["pnl"]
            if bankroll > peak_bankroll:
                peak_bankroll = bankroll
            if bankroll < trough_bankroll:
                trough_bankroll = bankroll
            dd = (peak_bankroll - bankroll) / peak_bankroll
            if dd > max_drawdown:
                max_drawdown = dd
        else:
            skipped += 1

        if (i + 1) % 200 == 0:
            elapsed = time.time() - start
            print(f"  {i+1}/{n} done ({elapsed:.1f}s) — bankroll: ${bankroll:.2f}")

    elapsed = time.time() - start

    # Summary stats
    total = len(results)
    wins = sum(1 for r in results if r["pnl"] > 0)
    losses = sum(1 for r in results if r["pnl"] < 0)
    flat = sum(1 for r in results if r["pnl"] == 0)
    total_pnl = sum(r["pnl"] for r in results)
    avg_pnl = total_pnl / total if total else 0
    avg_edge = sum(r["edge"] for r in results) / total if total else 0
    avg_kelly = sum(r["kelly_pct"] for r in results) / total if total else 0

    by_reason = {}
    for r in results:
        reason = r["exit_reason"]
        by_reason[reason] = by_reason.get(reason, 0) + 1

    print(f"\n{'='*50}")
    print(f"  BATCH RESULTS -- {n} sequential trades")
    print(f"{'='*50}")
    print(f"  Completed in:    {elapsed:.1f}s")
    print(f"  Trades entered:  {total}")
    print(f"  Skipped (no edge): {skipped}")
    print(f"  {'-'*40}")
    print(f"  Wins:            {wins} ({wins/total*100:.1f}%)" if total else "")
    print(f"  Losses:          {losses} ({losses/total*100:.1f}%)" if total else "")
    print(f"  Flat:            {flat}")
    print(f"  {'-'*40}")
    print(f"  Starting bankroll: ${starting_bankroll:.2f}")
    print(f"  Final bankroll:    ${bankroll:.2f}")
    print(f"  Total return:      {(bankroll/starting_bankroll - 1)*100:+.1f}%")
    print(f"  Peak bankroll:     ${peak_bankroll:.2f}")
    print(f"  Trough bankroll:   ${trough_bankroll:.2f}")
    print(f"  Max drawdown:      {max_drawdown*100:.1f}%")
    print(f"  {'-'*40}")
    print(f"  Total PnL:       ${total_pnl:+.2f}")
    print(f"  Avg PnL/trade:   ${avg_pnl:+.2f}")
    print(f"  Avg edge:        {avg_edge:.1%}")
    print(f"  Avg Kelly:       {avg_kelly:.1%}")
    print(f"  {'-'*40}")
    print(f"  Exit reasons:")
    for reason, count in sorted(by_reason.items()):
        print(f"    {reason:20s} {count:>5}  ({count/total*100:.1f}%)")

    if results:
        best = max(results, key=lambda r: r["pnl"])
        worst = min(results, key=lambda r: r["pnl"])
        print(f"  {'-'*40}")
        print(f"  Best trade:   ${best['pnl']:+.2f}  ({best['bet_team']})")
        print(f"  Worst trade:  ${worst['pnl']:+.2f}  ({worst['bet_team']})")

    print(f"\n  Logged to Agent/logs/dryruntrades.csv")


# ══════════════════════════════════════════════════════════════
# SINGLE INTERACTIVE DRY RUN (verbose)
# ══════════════════════════════════════════════════════════════

def run_single(team_a_query, team_b_query, market_price_override=None):
    """Run one verbose dry run with full output."""
    _setup_dryrun_logging()

    print("=" * 60)
    print("  DRY RUN -- End-to-end pipeline test")
    print("=" * 60)

    print("\n[1/6] Resolving teams from dataset...")
    _, teams, _, _ = build_team_histories(EXCEL_FILE)

    def find(query):
        q = query.lower()
        for t in teams:
            if t.lower() == q:
                return t
        matches = [t for t in teams if q in t.lower()]
        return matches[0] if matches else None

    team_a = find(team_a_query)
    team_b = find(team_b_query)
    if not team_a:
        print(f"  Team A '{team_a_query}' not found in dataset.")
        print(f"  Available: {', '.join(teams[:20])}...")
        return
    if not team_b:
        print(f"  Team B '{team_b_query}' not found in dataset.")
        return
    print(f"  Team A: {team_a}")
    print(f"  Team B: {team_b}")

    print("\n[2/6] Running Elo model...")
    prob_a, elo_features, player_info, maps_played, _all_elos = predict_elo_only(team_a, team_b)
    prob_b = 1 - prob_a
    print(f"  {team_a}: {prob_a:.1%}")
    print(f"  {team_b}: {prob_b:.1%}")

    print("\n[3/6] Simulating Polymarket prices...")
    if market_price_override is not None:
        market_a = market_price_override
    else:
        if prob_a > prob_b:
            market_a = prob_a - 0.08
        else:
            market_a = prob_a + 0.08
        market_a = max(0.05, min(0.95, market_a))
    market_b = round(1 - market_a, 4)
    market_a = round(market_a, 4)

    print(f"  Simulated market: {team_a} {market_a:.1%}  {team_b} {market_b:.1%}")
    edge_a = prob_a - market_a
    edge_b = prob_b - market_b
    print(f"  Edge A: {edge_a:+.1%}  Edge B: {edge_b:+.1%}")

    print("\n[4/6] Running entry logic...")

    if edge_a >= EDGE_THRESHOLD and edge_a >= edge_b:
        side, bet_team, prob, market_price, edge = "a", team_a, prob_a, market_a, edge_a
    elif edge_b >= EDGE_THRESHOLD:
        side, bet_team, prob, market_price, edge = "b", team_b, prob_b, market_b, edge_b
    else:
        print(f"  No edge above {EDGE_THRESHOLD:.0%} threshold. Try --market-price.")
        print(f"  (Best edge: {max(edge_a, edge_b):+.1%})")
        return

    kelly_pct = quarter_kelly(prob, market_price)
    position_usd = 1000 * kelly_pct
    shares = position_usd / market_price
    entry_price = market_price

    print(f"  Betting on: {bet_team}")
    print(f"  Kelly fraction: {kelly_pct:.2%}")
    print(f"  Position size: ${position_usd:.2f}")
    print(f"  Shares: {shares:.1f} @ {entry_price:.4f}")
    print(f"  Stop loss at: {compute_stop_loss(entry_price):.4f}")
    print(f"  Trailing floors:")
    for pct in range(int(TRAILING_FLOOR_START * 100), int(TRAILING_FLOOR_START * 100) + 40, int(TRAILING_FLOOR_STEP * 100)):
        thresh = pct / 100
        trigger_price = entry_price * (1 + thresh)
        floor_price = compute_floor(entry_price, trigger_price)
        print(f"    +{thresh:.0%} gain ({trigger_price:.4f}) -> floor at {floor_price:.4f}")

    result = simulate_one(team_a, team_b, prob_a,
                          market_price_a=market_a, seed=42)

    if result:
        print(f"\n[5/6] Price simulation complete")
        print(f"\n[6/6] Results")
        print(f"  {'-'*40}")
        print(f"  Entry:       {result['entry_price']:.4f}")
        print(f"  Exit:        {result['exit_price']:.4f}")
        print(f"  Reason:      {result['exit_reason']}")
        pnl_pct = (result['exit_price'] - result['entry_price']) / result['entry_price']
        print(f"  PnL:         ${result['pnl']:+.2f} ({pnl_pct:+.1%})")
        print(f"\n  Logs written to Agent/logs/dryruntrades.csv")
    print("  Done!")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Dry run -- test the trading pipeline")
    parser.add_argument("--team-a", default="Fnatic", help="Team A name")
    parser.add_argument("--team-b", default="LOUD", help="Team B name")
    parser.add_argument("--market-price", type=float, default=None,
                        help="Override market price for team A (0-1)")
    parser.add_argument("--batch", type=int, default=None,
                        help="Run N simulations with random matchups (e.g. --batch 1000)")
    parser.add_argument("--bankroll", type=float, default=1000.0,
                        help="Starting bankroll (default: $1000)")
    args = parser.parse_args()

    if args.batch:
        run_batch(args.batch, starting_bankroll=args.bankroll)
    else:
        run_single(args.team_a, args.team_b, market_price_override=args.market_price)


if __name__ == "__main__":
    main()
