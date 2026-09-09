"""
Monte Carlo Trailing Floor Optimization

Simulates realistic Valorant map price paths (round-by-round, ~25 rounds
per map, p*(1-p) volatility scaling) and evaluates hundreds of trailing
floor configurations to find the optimal exit strategy.

Price paths use the exact negative-binomial map-win-probability formula
so round swings scale naturally with the current price level -- close
games swing more per round than blowouts, matching real VCT per-map
market behavior.

Usage:
    py -m Agent.testing.floor_search
    py -m Agent.testing.floor_search --configs 500 --trades 2000
    py -m Agent.testing.floor_search --seed 42
"""

import os
import sys
import math
import random
import time
import argparse
from functools import lru_cache

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from Agent.agent import (
    quarter_kelly, EDGE_THRESHOLD, STOP_LOSS_PCT,
    TRAILING_FLOOR_LADDER, TRAILING_FLOOR_STEP,
)
from Agent.testing.replay_paths import load_paths as _load_real_paths


# ══════════════════════════════════════════════════════════════
# MAP WIN PROBABILITY (exact)
# ══════════════════════════════════════════════════════════════

@lru_cache(maxsize=None)
def map_win_prob(score_a, score_b, p_x1000):
    """
    Exact probability of team A winning a first-to-13 Valorant map
    from score (score_a, score_b) with per-round win probability
    p = p_x1000 / 1000.

    Handles overtime (both >= 12) via closed-form win-by-2 solution.
    p_x1000 is an int for lru_cache hashability.
    """
    p = p_x1000 / 1000.0
    q = 1.0 - p
    a, b = score_a, score_b

    # Overtime: both >= 12, need to win by 2
    if a >= 12 and b >= 12:
        if a - b >= 2:
            return 1.0
        if b - a >= 2:
            return 0.0
        denom = p * p + q * q
        ot_tied = (p * p) / denom if denom > 0 else 0.5
        if a == b:
            return ot_tied
        elif a == b + 1:
            return p + q * ot_tied
        else:  # b == a + 1
            return p * ot_tied

    # Regulation: race to 13
    w = 13 - a  # A needs w more wins
    l = 13 - b  # B needs l more wins
    if w <= 0:
        return 1.0
    if l <= 0:
        return 0.0

    prob = 0.0
    for j in range(l):
        prob += math.comb(w - 1 + j, j) * (p ** w) * (q ** j)
    return prob


def _p_round_from_map_prob(target, tol=0.0005):
    """Binary search: find p_round so that map_win_prob(0, 0, p) ~ target."""
    lo, hi = 0.01, 0.99
    for _ in range(60):
        mid = (lo + hi) / 2
        mp = map_win_prob(0, 0, round(mid * 1000))
        if mp < target:
            lo = mid
        else:
            hi = mid
        if abs(mp - target) < tol:
            break
    return (lo + hi) / 2


# ══════════════════════════════════════════════════════════════
# PRICE PATH SIMULATION
# ══════════════════════════════════════════════════════════════

# Model accuracy: the model is right ~60% of the time.  When right, the
# true probability is close to model_prob (small noise).  When wrong, the
# team is actually an underdog -- the model completely misjudged the
# matchup (roster sub, map veto surprise, meta shift).
MODEL_RIGHT_RATE  = 0.60   # fraction of trades where model has genuine edge
NOISE_WHEN_RIGHT  = 0.03   # small calibration noise when model is correct
# When wrong: true prob is 25-42%, team is genuinely an underdog
WRONG_PROB_RANGE  = (0.25, 0.42)

# Economy: winning pistol gives a big advantage for the next 2 rounds.
# The winner buys full while the loser ecos.  ECO_BOOST is the extra
# per-round win probability for the team with the economy advantage.
# In real Valorant, pistol winners go on to win rounds 2-3 about 80% of
# the time, so a ~15% boost on top of base ~52% makes sense.
ECO_BOOST = 0.15
ECO_ROUNDS_AFTER_PISTOL = 2  # rounds of eco advantage after winning pistol

# Bayesian market learning: the market updates its per-round probability
# estimate from the observed score.  MARKET_PRIOR_STRENGTH controls how
# quickly the market learns -- it's the number of "phantom rounds" the
# market's initial estimate is worth.  Lower = market learns faster and
# the model's pre-map edge decays faster.  10 means the prior is worth
# about 10 rounds of score data, so by mid-map the market has mostly
# incorporated the actual in-game performance and the Elo-based edge is
# largely priced in.
MARKET_PRIOR_STRENGTH = 20


def simulate_price_path(entry_price, model_prob, rng):
    """
    Simulate a full Valorant map round-by-round with a Bayesian market.

    The market starts with a prior (from entry_price) and updates its
    per-round probability estimate as it observes the score.  This makes
    the model's edge strongest at the start and decay through the map --
    matching real per-map markets where pre-match analysis matters most
    early and round-by-round data dominates late.

    Returns (prices, final_outcome):
        prices        -- list of floats, starting at entry_price, one per round.
                         Does NOT include the resolution price.
        final_outcome -- 1.0 if our team won the map, 0.0 if lost.
    """
    # True probability: model is right 60% of the time, wrong 40%.
    # When right: actual prob ~ model_prob (genuine edge over market).
    # When wrong: actual prob < entry_price (no edge, model misjudged).
    if rng.random() < MODEL_RIGHT_RATE:
        actual_map_prob = model_prob + rng.gauss(0, NOISE_WHEN_RIGHT)
    else:
        # Model is wrong -- team is actually an underdog (25-42% true prob)
        actual_map_prob = rng.uniform(*WRONG_PROB_RANGE)
    actual_map_prob = max(0.05, min(0.95, actual_map_prob))
    actual_p = _p_round_from_map_prob(actual_map_prob)

    # Market's Bayesian prior: Beta(alpha0, beta0)
    market_p_init = _p_round_from_map_prob(max(0.05, min(0.95, entry_price)))
    alpha0 = market_p_init * MARKET_PRIOR_STRENGTH
    beta0 = (1 - market_p_init) * MARKET_PRIOR_STRENGTH

    prices = [entry_price]
    sa, sb = 0, 0

    # Economy tracking: who has eco advantage and for how many more rounds
    # Positive = team A has advantage, negative = team B has advantage
    eco_advantage_a = 0  # rounds remaining of eco boost for team A
    eco_advantage_b = 0

    while True:
        total_rounds = sa + sb
        # Detect pistol rounds: round 1 (total=0) and round 13 (total=12, start of 2nd half)
        is_pistol = total_rounds == 0 or total_rounds == 12

        # Compute effective p for this round based on economy
        p_this_round = actual_p
        if eco_advantage_a > 0:
            p_this_round = min(0.95, actual_p + ECO_BOOST)
        elif eco_advantage_b > 0:
            p_this_round = max(0.05, actual_p - ECO_BOOST)

        # Play the round
        a_wins = rng.random() < p_this_round
        if a_wins:
            sa += 1
        else:
            sb += 1

        # Update economy state
        if is_pistol:
            # Pistol winner gets eco advantage for next rounds
            if a_wins:
                eco_advantage_a = ECO_ROUNDS_AFTER_PISTOL
                eco_advantage_b = 0
            else:
                eco_advantage_b = ECO_ROUNDS_AFTER_PISTOL
                eco_advantage_a = 0
        else:
            # Tick down eco advantage
            if eco_advantage_a > 0:
                if a_wins:
                    eco_advantage_a -= 1  # expected win, advantage continues
                else:
                    eco_advantage_a = 0   # eco upset, economy resets
            if eco_advantage_b > 0:
                if not a_wins:
                    eco_advantage_b -= 1
                else:
                    eco_advantage_b = 0   # eco upset, economy resets

        # Map over?
        over = (sa >= 13 and sa - sb >= 2) or (sb >= 13 and sb - sa >= 2)
        if over or sa + sb >= 36:
            break

        # Market updates its p_round estimate from observed score
        # Market sees the score but doesn't know the true economy state --
        # it just updates its belief about p_round from the score.
        posterior_p = (alpha0 + sa) / (alpha0 + beta0 + sa + sb)
        pp_x1000 = max(10, min(990, round(posterior_p * 1000)))

        mp = map_win_prob(sa, sb, pp_x1000)
        micro_noise = rng.gauss(0, 0.008)
        prices.append(max(0.01, min(0.99, round(mp + micro_noise, 4))))

    final = 1.0 if sa > sb else 0.0
    return prices, final


def generate_paths(n, seed=42):
    """Generate n price paths with diverse entry conditions."""
    rng = random.Random(seed)
    paths = []
    wins = 0

    while len(paths) < n:
        # Sample entry conditions
        entry_price = round(rng.uniform(0.25, 0.70), 4)
        edge = round(rng.uniform(0.05, 0.10), 4)
        model_prob = min(entry_price + edge, 0.70)  # confidence cap
        edge = round(model_prob - entry_price, 4)
        if edge < EDGE_THRESHOLD:
            continue

        prices, final = simulate_price_path(entry_price, model_prob, rng)
        paths.append((entry_price, model_prob, edge, prices, final))
        if final == 1.0:
            wins += 1

    return paths, wins


# ══════════════════════════════════════════════════════════════
# REAL PATH REPLAY (from agent.db price_ticks)
# ══════════════════════════════════════════════════════════════

def load_real_paths(dedupe=False, seed=42, event_only=False):
    """Load real recorded paths from agent.db. Returns (paths, counts)."""
    return _load_real_paths(dedupe=dedupe, dedupe_seed=seed, event_only=event_only)


def bootstrap_real_paths(real_paths, n, seed):
    """Bootstrap n paths from real_paths and synthesize Kelly inputs.

    model_prob/edge are sampled in the same range as generate_paths so that
    bankroll dynamics and per-trade sizing are comparable across modes; the
    only thing that changes is the price trajectory after entry.
    """
    rng = random.Random(seed)
    out = []
    wins = 0
    while len(out) < n:
        p = rng.choice(real_paths)
        entry = p["entry_price"]
        edge = round(rng.uniform(0.05, 0.10), 4)
        model_prob = min(entry + edge, 0.70)
        edge = round(model_prob - entry, 4)
        if edge < EDGE_THRESHOLD:
            continue
        prices = [entry] + [px for _, px in p["ticks"]]
        out.append((entry, model_prob, edge, prices, p["final_outcome"]))
        if p["final_outcome"] == 1.0:
            wins += 1
    return out, wins


# ══════════════════════════════════════════════════════════════
# FLOOR CONFIGURATIONS
# ══════════════════════════════════════════════════════════════

def _build_ladder(floor_start, initial_gap, final_gap, tier_step, n_tiers):
    """Build a trailing floor ladder from parameters."""
    ladder = []
    for i in range(n_tiers):
        threshold = floor_start + i * tier_step
        t = i / max(n_tiers - 1, 1)
        gap = initial_gap + t * (final_gap - initial_gap)
        offset = max(0.0, threshold - gap)
        ladder.append((round(threshold, 4), round(offset, 4)))
    return tuple(ladder)


def current_config():
    """The current production trailing floor config."""
    return {
        "stop_loss": STOP_LOSS_PCT,
        "ladder": TRAILING_FLOOR_LADDER,
        "step": TRAILING_FLOOR_STEP,
        "label": "current",
    }


def reference_configs():
    """Named reference configs for comparison."""
    return [
        current_config(),
        {   # Stop loss only, hold to resolution
            "stop_loss": -0.30,
            "ladder": (),
            "step": 0.10,
            "label": "stop_only",
        },
        {   # Floor only, no stop loss
            "stop_loss": -99.0,
            "ladder": TRAILING_FLOOR_LADDER,
            "step": TRAILING_FLOOR_STEP,
            "label": "floor_only",
        },
        {   # Pure hold to resolution
            "stop_loss": -99.0,
            "ladder": (),
            "step": 0.10,
            "label": "hold_all",
        },
    ]


def generate_random_config(rng, fixed_stop_loss=None):
    """Sample a random floor configuration from the search space."""
    stop_loss = fixed_stop_loss if fixed_stop_loss is not None else -rng.uniform(0.15, 0.50)
    floor_start = rng.uniform(0.15, 0.50)
    initial_gap = rng.uniform(0.05, min(floor_start, 0.30))
    final_gap = rng.uniform(0.03, initial_gap)
    tier_step = rng.uniform(0.08, 0.15)
    n_tiers = rng.randint(3, 6)
    cont_step = rng.uniform(0.05, 0.15)

    ladder = _build_ladder(floor_start, initial_gap, final_gap, tier_step, n_tiers)

    return {
        "stop_loss": round(stop_loss, 4),
        "ladder": ladder,
        "step": round(cont_step, 4),
        "label": None,
    }


# ══════════════════════════════════════════════════════════════
# EVALUATION
# ══════════════════════════════════════════════════════════════

def _compute_floor(entry_price, high_price, cfg):
    """Compute trailing floor price with a custom config."""
    if entry_price <= 0 or not cfg["ladder"]:
        return None

    gain_pct = (high_price - entry_price) / entry_price
    ladder = cfg["ladder"]
    step = cfg["step"]

    if gain_pct < ladder[0][0]:
        return None

    eps = 1e-9
    floor_offset = None
    for threshold, offset in ladder:
        if gain_pct + eps >= threshold:
            floor_offset = offset
        else:
            break

    last_threshold, last_offset = ladder[-1]
    if gain_pct + eps >= last_threshold + step:
        extra = int((gain_pct - last_threshold + eps) / step)
        floor_offset = last_offset + extra * step

    return entry_price * (1 + floor_offset)


def evaluate_config(cfg, paths, starting_bankroll=1000.0):
    """
    Run all price paths through the exit logic with this config.

    Uses compounding (Kelly of current bankroll) for realistic dynamics.
    Scores on Sharpe ratio (avg PnL / std PnL) for risk-adjusted quality.
    """
    bankroll = starting_bankroll
    peak_bankroll = bankroll
    max_drawdown = 0.0
    pnl_list = []
    reasons = []

    for entry_price, model_prob, edge, prices, final_outcome in paths:
        kelly_pct = quarter_kelly(model_prob, entry_price)
        position_usd = bankroll * kelly_pct
        if position_usd < 1.0:
            continue

        shares = position_usd / entry_price
        stop = entry_price * (1 + cfg["stop_loss"]) if cfg["stop_loss"] is not None else None
        high = entry_price
        exit_price = None
        exit_reason = None

        for price in prices[1:]:  # skip entry price
            if price > high:
                high = price

            if stop is not None and price <= stop:
                exit_price = price
                exit_reason = "stop_loss"
                break

            floor = _compute_floor(entry_price, high, cfg)
            if floor is not None and price <= floor:
                exit_price = price
                exit_reason = "trailing_floor"
                break

        if exit_price is None:
            exit_price = final_outcome
            exit_reason = "resolution"

        pnl = (exit_price - entry_price) * shares
        bankroll += pnl
        if bankroll <= 0:
            bankroll = 0.01
        pnl_list.append(pnl)
        reasons.append(exit_reason)

        if bankroll > peak_bankroll:
            peak_bankroll = bankroll
        dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

    if not pnl_list:
        return None

    total = len(pnl_list)
    total_pnl = sum(pnl_list)
    avg_pnl = total_pnl / total
    std_pnl = (sum((p - avg_pnl) ** 2 for p in pnl_list) / total) ** 0.5
    sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0.0
    wins = sum(1 for p in pnl_list if p > 0)
    roi = (bankroll - starting_bankroll) / starting_bankroll

    by_reason = {}
    for r in reasons:
        by_reason[r] = by_reason.get(r, 0) + 1

    return {
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "sharpe": sharpe,
        "roi": roi,
        "win_rate": wins / total,
        "max_dd_pct": max_drawdown,
        "n_trades": total,
        "by_reason": by_reason,
    }


def _average_metrics(metric_list):
    """Average metrics across multiple seasons for stable rankings."""
    n = len(metric_list)
    if n == 0:
        return None

    avg_sharpe = sum(m["sharpe"] for m in metric_list) / n
    avg_roi = sum(m["roi"] for m in metric_list) / n
    avg_win = sum(m["win_rate"] for m in metric_list) / n
    worst_dd = max(m["max_dd_pct"] for m in metric_list)

    rois = [m["roi"] for m in metric_list]
    roi_std = (sum((r - avg_roi) ** 2 for r in rois) / n) ** 0.5

    # Merge exit reasons across seasons
    merged_reasons = {}
    total_trades = 0
    for m in metric_list:
        total_trades += m["n_trades"]
        for r, c in m["by_reason"].items():
            merged_reasons[r] = merged_reasons.get(r, 0) + c

    return {
        "sharpe": avg_sharpe,
        "roi": avg_roi,
        "roi_min": min(rois),
        "roi_max": max(rois),
        "roi_std": roi_std,
        "win_rate": avg_win,
        "max_dd_pct": worst_dd,
        "n_trades": total_trades,
        "by_reason": merged_reasons,
        "n_seasons": n,
    }


# ══════════════════════════════════════════════════════════════
# OUTPUT FORMATTING
# ══════════════════════════════════════════════════════════════

def _fmt_gaps(ladder):
    """Short string like '20->10%' summarizing the ladder gaps."""
    if not ladder:
        return "none"
    gaps = [round((t - o) * 100) for t, o in ladder]
    if len(gaps) >= 2 and gaps[0] != gaps[-1]:
        return f"{gaps[0]}->{gaps[-1]}%"
    return f"{gaps[0]}%"


def _fmt_start(ladder):
    if not ladder:
        return "n/a"
    return f"+{ladder[0][0]:.0%}"


def _fmt_stop(stop_loss):
    if stop_loss is None:
        return "off"
    return f"{stop_loss:+.0%}"


def _fmt_ladder_full(cfg):
    """Full ladder for the best config display."""
    ladder = cfg["ladder"]
    lines = []
    for threshold, offset in ladder:
        gap = threshold - offset
        lines.append(
            f"    +{threshold:.0%} peak -> floor +{offset:.0%}  (gap {gap:.0%})"
        )
    if ladder:
        lines.append(f"  Past +{ladder[-1][0]:.0%}: {cfg['step']:.0%} steps")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Monte Carlo trailing floor optimization"
    )
    parser.add_argument(
        "--configs", type=int, default=500,
        help="Random configs to search (default: 500)",
    )
    parser.add_argument(
        "--trades", type=int, default=300,
        help="Trades per season (default: 300, ~1 VCT season)",
    )
    parser.add_argument(
        "--seasons", type=int, default=5,
        help="Independent seasons to average over (default: 5)",
    )
    parser.add_argument(
        "--bankroll", type=float, default=1000.0,
        help="Starting bankroll per season (default: $1000)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--stop", type=float, default=None,
        help="Fix stop loss for all random configs (e.g. -0.50)",
    )
    parser.add_argument(
        "--paths", choices=["real", "synthetic"], default="real",
        help="Path source: real (replay agent.db ticks, default) or synthetic",
    )
    parser.add_argument(
        "--dedupe", action="store_true",
        help="When --paths real: keep one path per (match,map). For bet maps, keeps "
             "only the bet side; for unbet maps, picks one side at random. Removes "
             "complement-mirror double-counting that biases bootstrap toward 50%% wins.",
    )
    parser.add_argument(
        "--event-only", dest="event_only", action="store_true",
        help="When --paths real: keep ONLY event-anchored paths (real bets the agent "
             "placed). Strictest unbiased sample at the cost of N.",
    )
    args = parser.parse_args()

    # ── Step 1: Build all configs ─────────────────────────
    configs = reference_configs()
    rng = random.Random(args.seed + 1)
    for _ in range(args.configs):
        configs.append(generate_random_config(rng, fixed_stop_loss=args.stop))
    n_configs = len(configs)
    print(f"Configs: {len(reference_configs())} reference + {args.configs} random = {n_configs}")

    # ── Step 2: Generate paths and evaluate per season ────
    # Each season gets its own seed, paths, and fresh bankroll
    all_metrics = [[] for _ in range(n_configs)]

    real_paths = None
    if args.paths == "real":
        real_paths, counts = load_real_paths(
            dedupe=args.dedupe, seed=args.seed, event_only=args.event_only,
        )
        mode = []
        if args.event_only:
            mode.append("event-only")
        if args.dedupe:
            mode.append("deduped")
        mode_str = f" ({', '.join(mode)})" if mode else ""
        print(f"\nReal paths from agent.db: {len(real_paths)} usable{mode_str}")
        print(f"  event-anchored:      {counts['event']}")
        print(f"  heuristic-anchored:  {counts['heuristic']}")
        print(f"  skipped (no window):       {counts['skip_no_window']}, "
              f"(short post-entry): {counts['skip_short_post']}, "
              f"(unresolved): {counts['skip_unresolved']}")
        if args.dedupe:
            print(f"  dedupe dropped:      {counts.get('dedupe_dropped', 0)}")
        # Pool win rate sanity print
        if real_paths:
            pwins = sum(1 for p in real_paths if p["final_outcome"] == 1.0)
            print(f"  pool win rate:       {pwins}/{len(real_paths)} = "
                  f"{pwins/len(real_paths):.1%}")
        if not real_paths:
            print("\nNo real paths available -- falling back to synthetic.")
            args.paths = "synthetic"

    print(f"\nRunning {args.seasons} seasons x {args.trades} trades "
          f"(bankroll ${args.bankroll:.0f}, paths={args.paths})...")
    t0 = time.time()

    for s in range(args.seasons):
        seed = args.seed + s * 10000
        if args.paths == "real":
            paths, wins = bootstrap_real_paths(real_paths, args.trades, seed=seed)
        else:
            paths, wins = generate_paths(args.trades, seed=seed)
        wr = wins / len(paths)
        print(f"  Season {s+1}/{args.seasons} (seed {seed}): "
              f"{wins}/{len(paths)} wins ({wr:.1%})", end="")

        for i, cfg in enumerate(configs):
            m = evaluate_config(cfg, paths, args.bankroll)
            if m:
                all_metrics[i].append(m)

        print(f"  [{time.time() - t0:.1f}s]")

    t_total = time.time() - t0
    print(f"  Done in {t_total:.1f}s")

    # ── Step 3: Average across seasons ────────────────────
    all_results = []
    for i, cfg in enumerate(configs):
        avg = _average_metrics(all_metrics[i])
        if avg:
            all_results.append((cfg, avg))

    all_results.sort(key=lambda x: x[1]["sharpe"], reverse=True)

    # ── Reference config summary ──────────────────────────
    print(f"\nReference configs (averaged over {args.seasons} seasons):")
    for cfg, m in all_results:
        if cfg.get("label"):
            print(f"  {cfg['label']:12s}  "
                  f"ROI {m['roi']:>+7.1%} [{m['roi_min']:+.0%} to {m['roi_max']:+.0%}]  "
                  f"Sharpe {m['sharpe']:>+5.3f}  "
                  f"MaxDD {m['max_dd_pct']:>5.1%}  WinRate {m['win_rate']:.1%}")

    # ── Top 20 ────────────────────────────────────────────
    print(f"\n{'=' * 115}")
    print(f"  TOP 20 CONFIGS (by avg Sharpe over {args.seasons} seasons)")
    print(f"{'=' * 115}")
    print(f"  {'#':>3}  {'Label':>12}  {'StopLoss':>8}  {'Start':>6}  "
          f"{'Gaps':>10}  {'AvgROI':>8}  {'ROI Range':>16}  {'Sharpe':>7}  "
          f"{'MaxDD':>6}  {'Win%':>5}")
    print(f"  {'-'*3}  {'-'*12}  {'-'*8}  {'-'*6}  "
          f"{'-'*10}  {'-'*8}  {'-'*16}  {'-'*7}  "
          f"{'-'*6}  {'-'*5}")

    for rank, (cfg, m) in enumerate(all_results[:20], 1):
        label = cfg.get("label") or ""
        marker = "  <--" if label == "current" else ""
        roi_range = f"[{m['roi_min']:+.0%} to {m['roi_max']:+.0%}]"
        print(f"  {rank:>3}  {label:>12}  {_fmt_stop(cfg['stop_loss']):>8}  "
              f"{_fmt_start(cfg['ladder']):>6}  {_fmt_gaps(cfg['ladder']):>10}  "
              f"{m['roi']:>+7.1%}  {roi_range:>16}  "
              f"{m['sharpe']:>+6.3f}  {m['max_dd_pct']:>5.1%}  "
              f"{m['win_rate']:>4.0%}{marker}")

    # Show current rank if outside top 20
    current_rank = None
    current_m = None
    for rank, (cfg, m) in enumerate(all_results, 1):
        if cfg.get("label") == "current":
            current_rank = rank
            current_m = m
            break
    if current_rank and current_rank > 20:
        cfg, m = all_results[current_rank - 1]
        roi_range = f"[{m['roi_min']:+.0%} to {m['roi_max']:+.0%}]"
        print(f"  ...")
        print(f"  {current_rank:>3}  {'current':>12}  {_fmt_stop(cfg['stop_loss']):>8}  "
              f"{_fmt_start(cfg['ladder']):>6}  {_fmt_gaps(cfg['ladder']):>10}  "
              f"{m['roi']:>+7.1%}  {roi_range:>16}  "
              f"{m['sharpe']:>+6.3f}  {m['max_dd_pct']:>5.1%}  "
              f"{m['win_rate']:>4.0%}  <--")

    # ── Best config details ───────────────────────────────
    best_cfg, best_m = all_results[0]
    print(f"\n{'=' * 115}")
    print(f"  BEST CONFIG{'  (rank #' + str(current_rank) + ' = current)' if current_rank == 1 else ''}")
    print(f"{'=' * 115}")
    print(f"  Stop loss: {_fmt_stop(best_cfg['stop_loss'])}")
    if best_cfg["ladder"]:
        print(f"  Floor activation: +{best_cfg['ladder'][0][0]:.0%} gain")
        print(f"  Ladder:")
        print(_fmt_ladder_full(best_cfg))
    else:
        print(f"  No trailing floor (hold to resolution)")

    print()
    print(f"  Avg ROI:      {best_m['roi']:>+.1%}  "
          f"(range: {best_m['roi_min']:+.1%} to {best_m['roi_max']:+.1%})")
    print(f"  Sharpe:       {best_m['sharpe']:+.3f}")
    print(f"  Max drawdown: {best_m['max_dd_pct']:.1%}")
    print(f"  Win rate:     {best_m['win_rate']:.1%}")
    print(f"  Trades:       {best_m['n_trades']} ({args.seasons} seasons)")
    print(f"  Exit reasons:")
    for reason, count in sorted(best_m["by_reason"].items()):
        pct = count / best_m["n_trades"] * 100
        print(f"    {reason:20s} {count:>5}  ({pct:.1f}%)")

    if current_m and current_rank != 1:
        print()
        print(f"  vs current (rank #{current_rank}):")
        print(f"    ROI:     {best_m['roi']:+.1%} vs {current_m['roi']:+.1%}")
        print(f"    Sharpe:  {best_m['sharpe']:+.3f} vs "
              f"{current_m['sharpe']:+.3f}")
        print(f"    Max DD:  {best_m['max_dd_pct']:.1%} vs "
              f"{current_m['max_dd_pct']:.1%}")
        print(f"    Win:     {best_m['win_rate']:.1%} vs "
              f"{current_m['win_rate']:.1%}")

    # ── Per-trade diagnostics on floor_only ───────────────
    # Show the per-exit-reason PnL to validate intuition
    print(f"\n{'=' * 115}")
    print(f"  PER-TRADE DIAGNOSTICS (floor_only, single season seed {args.seed})")
    print(f"{'=' * 115}")
    floor_cfg = None
    for cfg in configs:
        if cfg.get("label") == "floor_only":
            floor_cfg = cfg
            break
    if floor_cfg:
        if args.paths == "real":
            seed0_paths, _ = bootstrap_real_paths(real_paths, args.trades, seed=args.seed)
        else:
            seed0_paths, _ = generate_paths(args.trades, seed=args.seed)
        # Detailed per-trade tracking
        by_reason_pnl = {}
        for entry_price, model_prob, edge, prices, final_outcome in seed0_paths:
            kelly_pct = quarter_kelly(model_prob, entry_price)
            shares = 1.0 / entry_price  # normalize to $1 position for readability
            high = entry_price
            exit_price = None
            exit_reason = None
            for price in prices[1:]:
                if price > high:
                    high = price
                floor = _compute_floor(entry_price, high, floor_cfg)
                if floor is not None and price <= floor:
                    exit_price = price
                    exit_reason = "trailing_floor"
                    break
            if exit_price is None:
                exit_price = final_outcome
                exit_reason = "resolution_win" if final_outcome == 1.0 else "resolution_loss"
            pnl_pct = (exit_price - entry_price) / entry_price
            if exit_reason not in by_reason_pnl:
                by_reason_pnl[exit_reason] = []
            by_reason_pnl[exit_reason].append(pnl_pct)

        print(f"  {'Exit Reason':>20}  {'Count':>6}  {'Avg PnL%':>9}  {'Min':>7}  {'Max':>7}")
        print(f"  {'-'*20}  {'-'*6}  {'-'*9}  {'-'*7}  {'-'*7}")
        total_ev = 0
        total_n = 0
        for reason in ["trailing_floor", "resolution_win", "resolution_loss"]:
            pnls = by_reason_pnl.get(reason, [])
            if not pnls:
                continue
            avg = sum(pnls) / len(pnls)
            total_ev += sum(pnls)
            total_n += len(pnls)
            print(f"  {reason:>20}  {len(pnls):>6}  {avg:>+8.1%}  "
                  f"{min(pnls):>+6.0%}  {max(pnls):>+6.0%}")
        if total_n:
            print(f"  {'OVERALL':>20}  {total_n:>6}  {total_ev/total_n:>+8.1%}")
            print(f"\n  Interpretation: each $1 bet returns ${1 + total_ev/total_n:.2f} on average")


if __name__ == "__main__":
    main()
