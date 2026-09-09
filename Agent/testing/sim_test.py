"""
End-to-End Agent Simulation Test

Runs the full TradingAgent loop against a fake match with mocked external
dependencies (VLR scraper, VLR poller, Polymarket API, Elo model).
No real API calls, no real sleeps — the entire match lifecycle completes
in seconds so you can verify the pipeline works before going live.

Simulates:
    - Schedule detection (upcoming match in 1 minute)
    - Map 1: entry window -> edge found -> buy -> price monitor -> trailing floor exit
    - Map 2: cooldown -> entry -> buy -> stop loss exit
    - Map 3: cooldown -> entry -> no edge -> skip
    - Series end -> scrape

Usage:
    python -m Agent.testing.sim_test
    python -m Agent.testing.sim_test --scenario 2-0
    python -m Agent.testing.sim_test --scenario no-edge
    python -m Agent.testing.sim_test --verbose
"""

import os
import sys
import time
import threading

# Save real sleep before any patching
_real_sleep = time.sleep
import argparse
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from Agent.agent import (
    TradingAgent, quarter_kelly, compute_floor, compute_stop_loss,
    EDGE_THRESHOLD, TRAILING_FLOOR_START, TRAILING_FLOOR_STEP, TRADE_CSV_FIELDS,
    logger as agent_logger,
)

# ══════════════════════════════════════════════════════════════
# SIMULATED MATCH TIMELINE
# ══════════════════════════════════════════════════════════════

# Each scenario defines a sequence of poller states the fake VLR returns.
# The agent polls repeatedly; each call advances to the next state.

def _make_scenario_2_1():
    """Full 3-map series: team_a wins 2-1."""
    return {
        "name": "2-1 series (3 maps, trailing floor + stop loss + no edge)",
        "team_a": "Fnatic",
        "team_b": "LOUD",
        # Model returns 62% for team_a
        "model_prob_a": 0.62,
        # Polymarket prices per map (what the mock API returns)
        "market_prices": {
            1: {"price_a": 0.55, "price_b": 0.45},  # 7% edge on A
            2: {"price_a": 0.52, "price_b": 0.48},   # 10% edge on A
            3: {"price_a": 0.58, "price_b": 0.42},   # 4% edge — below threshold
        },
        # Simulated price paths for the price monitor (per map)
        # Each list is a sequence of prices the monitor sees over time
        "price_paths": {
            1: _rising_then_drop(entry=0.55, peak=0.85, drop_to=0.65),
            2: _falling(entry=0.52, bottom=0.32),
            3: [],  # no position, no prices needed
        },
        # Poller states: list of dicts returned by poll_match_status
        "poller_sequence": [
            # Pre-match: not live yet
            {"is_live": False, "is_final": False, "maps": {}},
            # Match goes live (Map 1 starts)
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": False, "score_a": 3, "score_b": 2, "map_name": "Ascent"},
            }},
            # Map 1 in progress
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": False, "score_a": 8, "score_b": 7, "map_name": "Ascent"},
            }},
            # Map 1 final (13-10)
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Ascent"},
            }},
            # Map 2 in progress (consumed by _between_maps to find map name)
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Ascent"},
                2: {"final": False, "score_a": 5, "score_b": 6, "map_name": "Bind"},
            }},
            # Map 2 still in progress (held by main poll loop -> monitor pumps -> stop loss fires)
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Ascent"},
                2: {"final": False, "score_a": 7, "score_b": 9, "map_name": "Bind"},
            }},
            # Map 2 final (10-13, team_b wins)
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Ascent"},
                2: {"final": True, "score_a": 10, "score_b": 13, "map_name": "Bind"},
            }},
            # Map 3 in progress
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Ascent"},
                2: {"final": True, "score_a": 10, "score_b": 13, "map_name": "Bind"},
                3: {"final": False, "score_a": 7, "score_b": 5, "map_name": "Haven"},
            }},
            # Map 3 final (13-9, series over)
            {"is_live": True, "is_final": True, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Ascent"},
                2: {"final": True, "score_a": 10, "score_b": 13, "map_name": "Bind"},
                3: {"final": True, "score_a": 13, "score_b": 9, "map_name": "Haven"},
            }},
        ],
    }


def _make_scenario_2_0():
    """Quick 2-0 series: team_a dominates, both maps have edge."""
    return {
        "name": "2-0 sweep (2 maps, both profitable)",
        "team_a": "Sentinels",
        "team_b": "G2 Esports",
        "model_prob_a": 0.68,
        "market_prices": {
            1: {"price_a": 0.58, "price_b": 0.42},  # 10% edge
            2: {"price_a": 0.60, "price_b": 0.40},   # 8% edge
        },
        "price_paths": {
            1: _rising_then_drop(entry=0.58, peak=0.90, drop_to=0.70),
            2: _rising_then_drop(entry=0.60, peak=0.93, drop_to=0.72),
        },
        "poller_sequence": [
            {"is_live": False, "is_final": False, "maps": {}},
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": False, "score_a": 5, "score_b": 2, "map_name": "Lotus"},
            }},
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 6, "map_name": "Lotus"},
            }},
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 6, "map_name": "Lotus"},
                2: {"final": False, "score_a": 8, "score_b": 4, "map_name": "Split"},
            }},
            {"is_live": True, "is_final": True, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 6, "map_name": "Lotus"},
                2: {"final": True, "score_a": 13, "score_b": 8, "map_name": "Split"},
            }},
        ],
    }


def _make_scenario_no_edge():
    """Match where the model never finds edge above threshold."""
    return {
        "name": "No edge (model agrees with market, all maps skipped)",
        "team_a": "DRX",
        "team_b": "T1",
        "model_prob_a": 0.55,
        "market_prices": {
            1: {"price_a": 0.54, "price_b": 0.46},   # 1% edge — below 5%
            2: {"price_a": 0.53, "price_b": 0.47},
        },
        "price_paths": {1: [], 2: []},
        "poller_sequence": [
            {"is_live": False, "is_final": False, "maps": {}},
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": False, "score_a": 6, "score_b": 5, "map_name": "Icebox"},
            }},
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 11, "map_name": "Icebox"},
            }},
            {"is_live": True, "is_final": False, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 11, "map_name": "Icebox"},
                2: {"final": False, "score_a": 4, "score_b": 7, "map_name": "Fracture"},
            }},
            {"is_live": True, "is_final": True, "maps": {
                1: {"final": True, "score_a": 13, "score_b": 11, "map_name": "Icebox"},
                2: {"final": True, "score_a": 13, "score_b": 10, "map_name": "Fracture"},
            }},
        ],
    }


SCENARIOS = {
    "2-1": _make_scenario_2_1,
    "2-0": _make_scenario_2_0,
    "no-edge": _make_scenario_no_edge,
}


# ══════════════════════════════════════════════════════════════
# PRICE PATH GENERATORS
# ══════════════════════════════════════════════════════════════

def _rising_then_drop(entry, peak, drop_to, steps=6):
    """Generate a short price path: rises to peak, then drops to drop_to (triggers trailing floor)."""
    path = []
    rise_steps = steps // 2
    fall_steps = steps - rise_steps

    for i in range(rise_steps):
        t = (i + 1) / rise_steps
        price = entry + (peak - entry) * t
        path.append(round(price, 4))

    for i in range(fall_steps):
        t = (i + 1) / fall_steps
        price = peak - (peak - drop_to) * t
        path.append(round(price, 4))

    return path


def _falling(entry, bottom, steps=4):
    """Generate a short price path that drops steadily (triggers stop loss)."""
    path = []
    for i in range(steps):
        t = (i + 1) / steps
        price = entry - (entry - bottom) * t
        path.append(round(price, 4))
    return path


# ══════════════════════════════════════════════════════════════
# MOCK WIRING
# ══════════════════════════════════════════════════════════════

class MockState:
    """Holds shared mutable state for all mocks during one simulation."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.poller_index = 0
        self.price_path_index = {}  # token_id -> current index (per-token ladder)
        self.events = []  # log of significant events for verification
        self.agent = None   # set after agent construction; lets the mock poller
                            # pump the price monitor in lockstep with polling
        self.hold_calls = {}  # poller_index -> times held (caps runaway holds)

    def log(self, msg):
        self.events.append(msg)
        print(f"  [SIM] {msg}")


def build_mocks(state):
    """Return a dict of mock functions keyed by the import path to patch."""
    scenario = state.scenario

    # -- Mock upcoming matches: returns one match starting in 1 minute --
    def mock_get_upcoming_matches(vct_only=True):
        start = datetime.now(timezone.utc) + timedelta(seconds=5)
        state.log(f"upcoming: returning {scenario['team_a']} vs {scenario['team_b']}")
        return [{
            "match_id": "SIM_001",
            "match_url": "https://www.vlr.gg/sim/001",
            "team_a": scenario["team_a"],
            "team_b": scenario["team_b"],
            "start_time": start,
            "event": "VCT 2026: EMEA Stage 1",
            "stage": "Group Stage",
            "is_live": False,
        }]

    # -- Mock poller: returns next state in sequence --
    # Hold logic: if the current state has an in_progress map with an open
    # position, pump the price monitor and DON'T advance. The price path
    # clamps at its last value, so eventually the position monitor either
    # sees the trough and fires the floor/stop, or the hard cap (50 holds
    # per state) advances anyway.
    HOLD_CAP = 50

    def mock_poll_match_status(match_url, session=None):
        idx = min(state.poller_index, len(scenario["poller_sequence"]) - 1)
        status = scenario["poller_sequence"][idx]
        status.setdefault("maps_to_win", 2)

        should_hold = False
        if state.agent is not None and state.hold_calls.get(idx, 0) < HOLD_CAP:
            for map_num, map_info in status["maps"].items():
                if map_info["final"]:
                    continue
                has_position = any(
                    p.get("map_num") == map_num
                    for p in state.agent.positions.values()
                )
                if has_position:
                    should_hold = True
                    break

        if should_hold:
            state.agent._check_all_positions()
            state.hold_calls[idx] = state.hold_calls.get(idx, 0) + 1
        else:
            state.poller_index += 1

        return status

    # -- Mock model: returns configured probability --
    def mock_predict_elo_only(team_a, team_b, map_name=None):
        prob_a = scenario["model_prob_a"]
        state.log(f"model: {team_a} {prob_a:.1%} vs {team_b} {1-prob_a:.1%}")
        # Fake player info (just needs to be non-empty)
        player_info = {
            team_a: [{"name": "p1"}, {"name": "p2"}, {"name": "p3"}, {"name": "p4"}, {"name": "p5"}],
            team_b: [{"name": "p6"}, {"name": "p7"}, {"name": "p8"}, {"name": "p9"}, {"name": "p10"}],
        }
        elo_features = {"ranking_elo_sum_diff": 50, "ranking_elo_trend_diff": 5}
        maps_played = {team_a: 50, team_b: 50}
        all_player_elos = {f"p{i}": (team_a if i <= 5 else team_b, 1000.0) for i in range(1, 11)}
        return prob_a, elo_features, player_info, maps_played, all_player_elos

    # -- Mock Polymarket prices --
    def mock_get_match_prices(team_a, team_b, verbose=False):
        maps = {}
        for map_num, prices in scenario["market_prices"].items():
            maps[map_num] = {
                "market_id": f"SIM_MKT_{map_num}",
                "condition_id": f"SIM_COND_{map_num}",
                "question": f"Map {map_num} Winner",
                "price_a": prices["price_a"],
                "price_b": prices["price_b"],
            }
        state.log(f"polymarket: returning prices for {len(maps)} maps")
        return {
            "event_id": "SIM_EVT_001",
            "event_title": f"Valorant: {team_a} vs {team_b}",
            "team_a": team_a,
            "team_b": team_b,
            "swapped": False,
            "maps": maps,
        }

    # -- Mock PolyTrader --
    class MockTrader:
        def __init__(self):
            self._market_cache = {}

        def get_balance(self):
            return 1000.0

        def get_market_info(self, condition_id):
            return {
                "token_a": f"TOKEN_A_{condition_id}",
                "token_b": f"TOKEN_B_{condition_id}",
                "tick_size": "0.01",
                "neg_risk": False,
            }

        def get_market_price(self, token_id):
            # Per-token ladder: each token_id has its own index. TOKEN_A reads
            # from the configured price path; TOKEN_B returns the binary
            # complement (1 - price). Once the path exhausts, the last value
            # is returned indefinitely so the position monitor keeps seeing
            # the trough until the floor/stop fires.
            for map_num, prices in scenario["market_prices"].items():
                expected_a = f"TOKEN_A_SIM_COND_{map_num}"
                expected_b = f"TOKEN_B_SIM_COND_{map_num}"
                if token_id not in (expected_a, expected_b):
                    continue
                path = scenario["price_paths"].get(map_num, [])
                if not path:
                    return {"mid": None, "last_trade": None, "best_bid": None, "best_ask": None}
                idx = state.price_path_index.get(token_id, 0)
                clamped = min(idx, len(path) - 1)
                raw = path[clamped]
                state.price_path_index[token_id] = idx + 1
                price = raw if token_id == expected_a else round(1 - raw, 4)
                return {
                    "mid": price,
                    "last_trade": price,
                    "best_bid": round(max(0.01, price - 0.01), 4),
                    "best_ask": round(min(0.99, price + 0.01), 4),
                }
            return {"mid": None, "last_trade": None, "best_bid": None, "best_ask": None}

        def buy(self, **kwargs):
            state.log(f"BUY order: token={kwargs.get('token_id','?')}, price={kwargs.get('price','?')}, size={kwargs.get('size','?')}")
            return {"orderID": "SIM_ORDER_001"}

        def sell(self, **kwargs):
            state.log(f"SELL order: token={kwargs.get('token_id','?')}, price={kwargs.get('price','?')}, size={kwargs.get('size','?')}")
            return {"orderID": "SIM_ORDER_002"}

        def cancel_all(self):
            state.log("cancel_all called")

    return {
        "upcoming": mock_get_upcoming_matches,
        "poller": mock_poll_match_status,
        "model": mock_predict_elo_only,
        "prices": mock_get_match_prices,
        "trader_class": MockTrader,
    }


# ══════════════════════════════════════════════════════════════
# SIMULATION RUNNER
# ══════════════════════════════════════════════════════════════

def run_simulation(scenario_name="2-1", verbose=False):
    """Run one full agent simulation with mocked dependencies."""
    if scenario_name not in SCENARIOS:
        print(f"Unknown scenario: {scenario_name}")
        print(f"Available: {', '.join(SCENARIOS.keys())}")
        return False

    scenario = SCENARIOS[scenario_name]()
    state = MockState(scenario)
    mocks = build_mocks(state)

    print("=" * 60)
    print(f"  SIM TEST: {scenario['name']}")
    print(f"  {scenario['team_a']} vs {scenario['team_b']}")
    print("=" * 60)

    # Patch all external calls
    patches = [
        patch("Agent.agent.get_upcoming_matches", side_effect=mocks["upcoming"]),
        patch("Agent.agent.poll_match_status", side_effect=mocks["poller"]),
        patch("Agent.agent.predict_elo_only", side_effect=mocks["model"]),
        patch("Agent.agent.get_match_prices", side_effect=mocks["prices"]),
        # Replace real sleeps with tiny sleeps (allows thread scheduling)
        patch("Agent.agent.time.sleep", side_effect=lambda _: _real_sleep(0.005)),
        # Patch os.system to skip actual scraping
        patch("os.system", side_effect=lambda cmd: state.log(f"scrape: {cmd}")),
        # Suppress Discord notifications during tests
        patch("Agent.agent.notify", side_effect=lambda msg: None),
    ]

    for p in patches:
        p.start()

    # Capture trade log entries instead of writing to file
    trade_log = []
    original_log_trade = None

    import Agent.agent as agent_mod
    original_log_trade = agent_mod.log_trade

    def capture_trade(row):
        trade_log.append(row)
        if verbose:
            side = row.get("side", "?")
            reason = row.get("exit_reason", "?")
            pnl = row.get("pnl", "")
            entry = row.get("entry_price", "")
            exit_p = row.get("exit_price", "")
            print(f"  [TRADE] side={side} entry={entry} exit={exit_p} reason={reason} pnl={pnl}")

    agent_mod.log_trade = capture_trade

    success = True
    try:
        # Create agent in paper mode with mock trader
        agent = TradingAgent(paper=True, bankroll=1000)
        agent.trader = mocks["trader_class"]()
        state.agent = agent  # let the mock poller pump the price monitor

        # Run one cycle (not the infinite loop). The mock poller pumps the
        # price monitor in lockstep with polling, so floor/stop fire on the
        # configured price path before maps go final. Background monitor stays
        # off for determinism.
        agent._monitor_running = False
        agent._run_cycle()

        # Safety net: any positions still open (e.g. bug in hold logic) get
        # cleared synchronously so the test doesn't hang.
        for _ in range(50):
            if not agent.positions:
                break
            agent._check_all_positions()

        # ── Verify results ──
        print(f"\n{'='*60}")
        print(f"  RESULTS")
        print(f"{'='*60}")

        print(f"\n  Events ({len(state.events)}):")
        for i, e in enumerate(state.events, 1):
            print(f"    {i:2d}. {e}")

        print(f"\n  Trade log ({len(trade_log)} entries):")
        for i, t in enumerate(trade_log, 1):
            side = t.get("side", "?")
            map_num = t.get("map_num", "?")
            reason = t.get("exit_reason", "?")
            pnl = t.get("pnl", "")
            entry = t.get("entry_price", "")
            exit_p = t.get("exit_price", "")
            edge = t.get("edge", "")
            print(f"    {i}. Map {map_num} | {side:12s} | entry={entry:>8s} exit={exit_p:>8s} | {reason:16s} | pnl={pnl}")

        print(f"\n  Final bankroll: ${agent.bankroll:.2f}")
        print(f"  Open positions: {len(agent.positions)}")

        # ── Assertions ──
        checks_passed = 0
        checks_total = 0

        def check(name, condition):
            nonlocal checks_passed, checks_total
            checks_total += 1
            if condition:
                checks_passed += 1
                print(f"    PASS  {name}")
            else:
                print(f"    FAIL  {name}")

        print(f"\n  Checks:")

        if scenario_name == "2-1":
            # Map 1: entry + exit (trailing floor)
            # Map 2: entry + exit (resolution loss -- stop loss disabled, falling
            #        price path now rides to map resolution where team_b wins)
            # Map 3: no edge -> skipped
            entries = [t for t in trade_log if t.get("exit_reason") == "open"]
            exits = [t for t in trade_log if t.get("exit_reason") not in ("open", "no_edge")]
            no_edges = [t for t in trade_log if t.get("exit_reason") == "no_edge"]
            floor_exits = [t for t in exits if t.get("exit_reason") == "trailing_floor"]
            resolution_exits = [t for t in exits if t.get("exit_reason") == "resolution"]

            check("Map 1+2 entered (2 entries)", len(entries) == 2)
            check("Map 1+2 exited (2 exits)", len(exits) >= 2)
            check("Map 1 exited via trailing_floor", len(floor_exits) >= 1)
            check("Map 2 exited via resolution", len(resolution_exits) >= 1)
            check("Map 3 skipped (no edge)", len(no_edges) >= 1)
            check("No open positions remain", len(agent.positions) == 0)
            check("Bankroll changed from $1000", agent.bankroll != 1000)

        elif scenario_name == "2-0":
            entries = [t for t in trade_log if t.get("exit_reason") == "open"]
            exits = [t for t in trade_log if t.get("exit_reason") not in ("open", "no_edge")]
            floor_exits = [t for t in exits if t.get("exit_reason") == "trailing_floor"]

            check("Both maps entered (2 entries)", len(entries) == 2)
            check("Both maps exited (2 exits)", len(exits) >= 2)
            check("Both exits via trailing_floor", len(floor_exits) >= 2)
            check("No open positions remain", len(agent.positions) == 0)

        elif scenario_name == "no-edge":
            no_edges = [t for t in trade_log if t.get("exit_reason") == "no_edge"]
            entries = [t for t in trade_log if t.get("exit_reason") == "open"]

            check("No positions entered", len(entries) == 0)
            check("All maps logged as no_edge", len(no_edges) >= 1)
            check("Bankroll unchanged ($1000)", agent.bankroll == 1000)

        print(f"\n  Result: {checks_passed}/{checks_total} checks passed")
        if checks_passed < checks_total:
            success = False

    except Exception as e:
        print(f"\n  ERROR: {e}")
        import traceback
        traceback.print_exc()
        success = False
    finally:
        # Restore
        agent_mod.log_trade = original_log_trade
        for p in patches:
            p.stop()

    return success


def run_all():
    """Run all scenarios and report summary."""
    print("\n" + "=" * 60)
    print("  RUNNING ALL SCENARIOS")
    print("=" * 60 + "\n")

    results = {}
    for name in SCENARIOS:
        ok = run_simulation(name)
        results[name] = ok
        print()

    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    all_pass = True
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  {status}  {name}")

    print(f"\n  Overall: {'ALL PASSED' if all_pass else 'SOME FAILED'}")
    return all_pass


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="End-to-end agent simulation test")
    parser.add_argument(
        "--scenario", default=None,
        help=f"Run a specific scenario ({', '.join(SCENARIOS.keys())}). Default: run all."
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print trade entries as they happen"
    )
    args = parser.parse_args()

    if args.scenario:
        ok = run_simulation(args.scenario, verbose=args.verbose)
        sys.exit(0 if ok else 1)
    else:
        ok = run_all()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
