"""
Valorant Prediction Market Trading Agent

Orchestrates the full trading loop:
    1. Scrape upcoming VCT matches
    2. Sleep until pre-match
    3. Check model edge vs Polymarket prices
    4. Enter positions when edge > threshold
    5. Monitor positions with trailing floor / stop loss
    6. Rescrape between maps, repeat

Usage:
    python -m Agent.agent              # Paper trading (default)
    python -m Agent.agent --live       # Real trading
    python -m Agent.agent --bankroll 500  # Set paper bankroll
"""

import os
import sys
import csv
import json
import time
import logging
import argparse
import threading
from datetime import datetime, timezone, timedelta

# Project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from Agent.upcoming import get_upcoming_matches
from Agent.poller import poll_match_status
from PolyInt.polymatches import get_match_prices
from PolyInt.trade import PolyTrader
from Predict.predict import predict_elo_only

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

EDGE_THRESHOLD = 0.05          # 5% minimum edge to enter
MAX_CONFIDENCE = 0.70          # cap model probability at 70% (either side)
STOP_LOSS_PCT = -0.30          # -30% emergency stop loss
TRAILING_FLOORS = [            # (gain_threshold, floor_relative_to_entry)
    (0.30, 0.20),              # +30% gain → floor at +20%
    (0.20, 0.10),              # +20% gain → floor at +10%
    (0.10, 0.00),              # +10% gain → floor at entry (breakeven)
]
ENTRY_WINDOW_SECS = 5 * 60    # 5 min entry window for maps 2/3
COOLDOWN_SECS = 5 * 60         # 5 min cooldown after map final
PRE_MATCH_LEAD = 10 * 60       # wake up 10 min before match
ENTRY_LEAD = 5 * 60            # entry opens 5 min before match start
POLL_INTERVAL = 60              # VLR poll interval (seconds)
PRICE_MONITOR_INTERVAL = 3     # price check interval (seconds)
DEFAULT_PAPER_BANKROLL = 1000  # $1000 default paper bankroll

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
STATE_FILE = os.path.join(LOG_DIR, "agent_state.json")
TRADE_LOG = os.path.join(LOG_DIR, "trades.csv")
AGENT_LOG = os.path.join(LOG_DIR, "trades.log")
PAPER_TRADE_LOG = os.path.join(LOG_DIR, "papertrades.csv")
PAPER_AGENT_LOG = os.path.join(LOG_DIR, "papertrades.log")
DRYRUN_TRADE_LOG = os.path.join(LOG_DIR, "dryruntrades.csv")
DRYRUN_AGENT_LOG = os.path.join(LOG_DIR, "dryruntrades.log")

# ══════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════

os.makedirs(LOG_DIR, exist_ok=True)

TRADE_CSV_FIELDS = [
    "timestamp", "mode", "match_id", "map_num",
    "team_a", "team_b", "side",
    "model_prob", "market_price", "edge",
    "kelly_pct", "position_size_usd",
    "entry_price", "exit_price", "exit_reason", "pnl",
    "map_winner", "model_correct",
]

# Default logger (reconfigured per-instance in TradingAgent.__init__)
logger = logging.getLogger("agent")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(ch)

# Active trade log path (set by TradingAgent based on mode)
_active_trade_log = TRADE_LOG


def log_trade(row):
    """Append a row to the active trade CSV (trades.csv or papertrades.csv)."""
    path = _active_trade_log
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ══════════════════════════════════════════════════════════════
# KELLY SIZING
# ══════════════════════════════════════════════════════════════

def quarter_kelly(prob, market_price):
    """
    Compute quarter Kelly fraction for a prediction market.

    Kelly: f = (p * odds - (1-p)) / odds  where odds = (1 - price) / price
    Quarter Kelly: f / 4

    Returns fraction of bankroll to bet (0 to 1), or 0 if no edge.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    odds = (1 - market_price) / market_price
    if odds <= 0:
        return 0.0
    full_kelly = (prob * odds - (1 - prob)) / odds
    if full_kelly <= 0:
        return 0.0
    return full_kelly / 4


# ══════════════════════════════════════════════════════════════
# TRAILING FLOOR LOGIC
# ══════════════════════════════════════════════════════════════

def compute_floor(entry_price, high_price):
    """
    Compute the current exit floor based on entry price and highest price seen.

    Returns the floor price, or None if only stop loss applies.
    """
    if entry_price <= 0:
        return None

    gain_pct = round((high_price - entry_price) / entry_price, 8)

    for threshold, floor_offset in TRAILING_FLOORS:
        if gain_pct >= threshold:
            return entry_price * (1 + floor_offset)

    # Below +10% — no trailing floor, only stop loss
    return None


def compute_stop_loss(entry_price):
    """Compute the -30% stop loss price."""
    return entry_price * (1 + STOP_LOSS_PCT)


# ══════════════════════════════════════════════════════════════
# TRADING AGENT
# ══════════════════════════════════════════════════════════════

class TradingAgent:
    def __init__(self, paper=True, bankroll=DEFAULT_PAPER_BANKROLL):
        global _active_trade_log

        self.paper = paper
        self.mode = "paper" if paper else "live"
        self.trader = PolyTrader()

        # Route logs to paper or live files
        if paper:
            _active_trade_log = PAPER_TRADE_LOG
            agent_log_file = PAPER_AGENT_LOG
        else:
            _active_trade_log = TRADE_LOG
            agent_log_file = AGENT_LOG

        # Configure file handler for the correct log file
        for h in logger.handlers[:]:
            if isinstance(h, logging.FileHandler):
                logger.removeHandler(h)
        fh = logging.FileHandler(agent_log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

        if paper:
            self.bankroll = bankroll
            logger.info(f"Paper trading mode -- bankroll: ${bankroll:.2f}")
        else:
            self.bankroll = self.trader.get_balance()
            logger.info(f"LIVE trading mode -- wallet balance: ${self.bankroll:.2f}")

        # Open positions: pos_key -> position dict
        self.positions = {}
        self._positions_lock = threading.Lock()

        # Restore state from previous run (if any)
        self._load_state()

        # Price monitor thread
        self._monitor_running = False
        self._monitor_thread = None

    # ──────────────────────────────────────────────────────────
    # STATE PERSISTENCE
    # ──────────────────────────────────────────────────────────

    def _save_state(self):
        """Save open positions and bankroll to disk."""
        with self._positions_lock:
            state = {
                "mode": self.mode,
                "bankroll": self.bankroll,
                "positions": self.positions,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_state(self):
        """Restore open positions and bankroll from disk."""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("mode") != self.mode:
                logger.info(f"State file is for {state.get('mode')} mode, ignoring")
                return
            positions = state.get("positions", {})
            if positions:
                self.positions = positions
                if self.paper:
                    self.bankroll = state.get("bankroll", self.bankroll)
                logger.info(
                    f"Restored {len(positions)} open position(s) from state file "
                    f"(saved {state.get('saved_at', 'unknown')})"
                )
                for key, pos in positions.items():
                    logger.info(
                        f"  {key}: {pos['bet_team']} @ {pos['entry_price']:.2f} "
                        f"({pos['shares']:.1f} shares, ${pos['position_usd']:.2f})"
                    )
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    def _clear_state(self):
        """Remove the state file when no positions are open."""
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────

    def run(self):
        """Main agent loop — runs forever."""
        logger.info(f"Agent started ({self.mode} mode)")
        self._start_price_monitor()

        while True:
            try:
                self._run_cycle()
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self._stop_price_monitor()
                break
            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                time.sleep(60)

    def _run_cycle(self):
        """One cycle: find next match, wait, handle it."""
        logger.info("Fetching upcoming VCT matches...")
        matches = get_upcoming_matches(vct_only=True)

        if not matches:
            logger.info("No upcoming VCT matches found. Sleeping 30 min...")
            time.sleep(30 * 60)
            return

        # Filter to matches with a start time, sort by soonest
        timed = [m for m in matches if m["start_time"] is not None]
        if not timed:
            logger.info("No matches with start times. Sleeping 30 min...")
            time.sleep(30 * 60)
            return

        timed.sort(key=lambda m: m["start_time"])
        now = datetime.now(timezone.utc)

        # Find next match that hasn't started yet (or is live)
        target = None
        for m in timed:
            if m["is_live"]:
                target = m
                logger.info(f"Found LIVE match: {m['team_a']} vs {m['team_b']}")
                break
            if m["start_time"] > now - timedelta(minutes=30):
                target = m
                break

        if not target:
            logger.info("No actionable matches. Sleeping 30 min...")
            time.sleep(30 * 60)
            return

        # Sleep until T-10 min before match
        if not target["is_live"]:
            wake_time = target["start_time"] - timedelta(seconds=PRE_MATCH_LEAD)
            wait_secs = (wake_time - now).total_seconds()
            if wait_secs > 0:
                logger.info(
                    f"Next match: {target['team_a']} vs {target['team_b']} "
                    f"at {target['start_time'].strftime('%H:%M UTC')}"
                )
                logger.info(f"Sleeping {wait_secs/60:.1f} min until T-10...")
                time.sleep(wait_secs)

        self._handle_match(target)

    # ──────────────────────────────────────────────────────────
    # MATCH HANDLER
    # ──────────────────────────────────────────────────────────

    def _handle_match(self, match):
        """Handle a full match (up to 3 maps)."""
        team_a = match["team_a"]
        team_b = match["team_b"]
        match_url = match["match_url"]
        match_id = match["match_id"]

        logger.info(f"{'='*50}")
        logger.info(f"MATCH: {team_a} vs {team_b}")
        logger.info(f"URL: {match_url}")
        logger.info(f"{'='*50}")

        # ── Map 1: wait for veto (map name appears), then 5 min entry window ──
        if not match["is_live"]:
            self._wait_for_entry_window(match)
            map1_name = self._wait_for_veto(match_url)
            if map1_name:
                logger.info(f"Map 1 veto detected: {map1_name} — entry window OPEN (5 min)")
                self._enter_map(team_a, team_b, match_id, map_num=1, map_name=map1_name)
                time.sleep(ENTRY_WINDOW_SECS)
                self._close_entry_window(map_num=1)
            else:
                logger.info("Match went live before veto detected — entering without map")
                self._enter_map(team_a, team_b, match_id, map_num=1, map_name=None)
                self._close_entry_window(map_num=1)
        else:
            logger.info("Match already live — skipping Map 1 entry")

        # ── Poll through the series ──
        prev_finals = set()
        series_over = False

        while not series_over:
            status = poll_match_status(match_url)
            if status is None:
                time.sleep(POLL_INTERVAL)
                continue

            if status["is_final"]:
                logger.info("Series is FINAL")
                series_over = True
                break

            # Check for newly finalized maps
            for map_num, map_data in status["maps"].items():
                if map_data["final"] and map_num not in prev_finals:
                    score = f"{map_data['score_a']}-{map_data['score_b']}"
                    logger.info(f"Map {map_num} FINAL ({score})")
                    prev_finals.add(map_num)

                    # Record map winner on any open position for this map
                    map_winner = team_a if map_data["score_a"] > map_data["score_b"] else team_b
                    self._record_map_winner(match_id, map_num, map_winner)

                    next_map = map_num + 1
                    if next_map <= 3:
                        # Check if one team already has 2 wins (series decided)
                        wins_a = sum(
                            1 for m in status["maps"].values()
                            if m["final"] and m["score_a"] > m["score_b"]
                        )
                        wins_b = sum(
                            1 for m in status["maps"].values()
                            if m["final"] and m["score_b"] > m["score_a"]
                        )
                        if wins_a >= 2 or wins_b >= 2:
                            logger.info("Series decided — no more maps")
                            series_over = True
                            break

                        # Cooldown + rescrape + entry for next map
                        self._between_maps(
                            team_a, team_b, match_id, match_url, map_num, next_map
                        )

            if not series_over:
                time.sleep(POLL_INTERVAL)

        # ── Series complete: scrape final map ──
        logger.info("Scraping final match data...")
        self._scrape_match(match_id)
        logger.info(f"Match complete: {team_a} vs {team_b}")

    # ──────────────────────────────────────────────────────────
    # ENTRY WINDOW
    # ──────────────────────────────────────────────────────────

    def _wait_for_entry_window(self, match):
        """Sleep until T-5 min before match start."""
        if match["start_time"] is None:
            return
        entry_time = match["start_time"] - timedelta(seconds=ENTRY_LEAD)
        now = datetime.now(timezone.utc)
        wait = (entry_time - now).total_seconds()
        if wait > 0:
            logger.info(f"Waiting {wait/60:.1f} min for entry window...")
            time.sleep(wait)

    def _wait_for_live(self, match_url):
        """Poll VLR until match goes live (Map 1 started)."""
        logger.info("Waiting for match to go live...")
        while True:
            status = poll_match_status(match_url)
            if status and (status["is_live"] or status["is_final"]):
                logger.info("Match is now LIVE")
                return
            time.sleep(POLL_INTERVAL)

    def _wait_for_veto(self, match_url):
        """Poll VLR until Map 1's map name appears (veto complete) or match goes live."""
        logger.info("Waiting for map veto (Map 1 name to appear)...")
        while True:
            status = poll_match_status(match_url)
            if status is None:
                time.sleep(POLL_INTERVAL)
                continue
            # If match went live before we saw a map name, return None
            if status["is_live"] or status["is_final"]:
                map1 = status["maps"].get(1, {})
                return map1.get("map_name")
            # Check if map name appeared (veto done, match still upcoming)
            map1 = status["maps"].get(1, {})
            map_name = map1.get("map_name")
            if map_name:
                return map_name
            time.sleep(POLL_INTERVAL)

    def _close_entry_window(self, map_num):
        """Cancel any unfilled orders for this map's entry."""
        logger.info(f"Map {map_num} entry window CLOSED — cancelling unfilled orders")
        if not self.paper:
            try:
                self.trader.cancel_all()
            except Exception as e:
                logger.error(f"Error cancelling orders: {e}")

    def _between_maps(self, team_a, team_b, match_id, match_url, finished_map, next_map):
        """Cooldown, rescrape, then open entry for next map."""
        logger.info(f"Map {finished_map} done — {COOLDOWN_SECS//60} min cooldown")

        # Scrape the finished map
        self._scrape_game(match_id, finished_map)

        # Cooldown
        time.sleep(COOLDOWN_SECS)

        # Get map name for next map from poller
        next_map_name = None
        status = poll_match_status(match_url)
        if status and next_map in status["maps"]:
            next_map_name = status["maps"][next_map].get("map_name")

        map_label = f" ({next_map_name})" if next_map_name else ""
        logger.info(f"Map {next_map}{map_label} entry window OPEN (5 min)")
        self._enter_map(team_a, team_b, match_id, map_num=next_map, map_name=next_map_name)

        logger.info(f"Map {next_map} entry window — waiting 5 min...")
        time.sleep(ENTRY_WINDOW_SECS)
        self._close_entry_window(map_num=next_map)

    # ──────────────────────────────────────────────────────────
    # EDGE DETECTION & ENTRY
    # ──────────────────────────────────────────────────────────

    def _enter_map(self, team_a, team_b, match_id, map_num, map_name=None):
        """Check edge and enter a position if edge > threshold."""
        map_label = f" ({map_name})" if map_name else ""
        logger.info(f"Checking edge for Map {map_num}{map_label}: {team_a} vs {team_b}")

        # Run Elo model
        try:
            prob_a, elo_features, player_info, maps_played = predict_elo_only(team_a, team_b, map_name=map_name)
        except Exception as e:
            logger.error(f"Model prediction failed: {e}")
            return

        # Check that both teams have meaningful data (not just default Elo)
        players_a = player_info.get(list(player_info.keys())[0], []) if player_info else []
        players_b = player_info.get(list(player_info.keys())[1], []) if len(player_info) > 1 else []
        if not players_a or not players_b:
            logger.warning(f"  Missing player data for one or both teams -- skipping")
            return

        # Skip teams with too little data for reliable predictions
        MIN_MAPS = 8
        for team, count in maps_played.items():
            if count < MIN_MAPS:
                logger.warning(f"  {team} has only {count} maps in dataset (min {MIN_MAPS}) -- skipping")
                return

        # Cap confidence at MAX_CONFIDENCE (either side)
        raw_prob_a = prob_a
        prob_a = min(prob_a, MAX_CONFIDENCE)
        prob_a = max(prob_a, 1 - MAX_CONFIDENCE)
        prob_b = 1 - prob_a

        if raw_prob_a != prob_a:
            logger.info(f"  Model (raw): {team_a} {raw_prob_a:.1%}  {team_b} {1-raw_prob_a:.1%}")
            logger.info(f"  Model (capped): {team_a} {prob_a:.1%}  {team_b} {prob_b:.1%}")
        else:
            logger.info(f"  Model: {team_a} {prob_a:.1%}  {team_b} {prob_b:.1%}")

        # Get Polymarket prices
        try:
            prices = get_match_prices(team_a, team_b)
        except Exception as e:
            logger.error(f"Polymarket price fetch failed: {e}")
            return

        if not prices or map_num not in prices["maps"]:
            logger.warning(f"  No Polymarket market found for Map {map_num}")
            return

        map_market = prices["maps"][map_num]
        market_price_a = map_market["price_a"]
        market_price_b = map_market["price_b"]
        condition_id = map_market["condition_id"]
        swapped = prices["swapped"]

        logger.info(
            f"  Market: {team_a} {market_price_a:.1%}  {team_b} {market_price_b:.1%}"
        )

        # Compute edge for both sides
        edge_a = prob_a - market_price_a
        edge_b = prob_b - market_price_b

        logger.info(f"  Edge A: {edge_a:+.1%}  Edge B: {edge_b:+.1%}")

        # Pick the side with better edge (if any exceeds threshold)
        if edge_a >= EDGE_THRESHOLD and edge_a >= edge_b:
            self._place_entry(
                team_a, team_b, match_id, map_num,
                side="a", prob=prob_a, market_price=market_price_a,
                edge=edge_a, condition_id=condition_id, swapped=swapped,
            )
        elif edge_b >= EDGE_THRESHOLD:
            self._place_entry(
                team_a, team_b, match_id, map_num,
                side="b", prob=prob_b, market_price=market_price_b,
                edge=edge_b, condition_id=condition_id, swapped=swapped,
            )
        else:
            logger.info(f"  No edge above {EDGE_THRESHOLD:.0%} threshold — skipping")
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": self.mode,
                "match_id": match_id,
                "map_num": map_num,
                "team_a": team_a,
                "team_b": team_b,
                "side": "none",
                "model_prob": f"{max(prob_a, prob_b):.4f}",
                "market_price": f"{min(market_price_a, market_price_b):.4f}",
                "edge": f"{max(edge_a, edge_b):.4f}",
                "kelly_pct": "0",
                "position_size_usd": "0",
                "entry_price": "",
                "exit_price": "",
                "exit_reason": "no_edge",
                "pnl": "0",
                "map_winner": "",
                "model_correct": "",
            })

    def _place_entry(self, team_a, team_b, match_id, map_num,
                     side, prob, market_price, edge, condition_id, swapped):
        """Place an entry order (or log it in paper mode)."""
        bet_team = team_a if side == "a" else team_b
        kelly_pct = quarter_kelly(prob, market_price)

        # Get current bankroll
        if self.paper:
            bankroll = self.bankroll
        else:
            bankroll = self.trader.get_balance()

        position_usd = bankroll * kelly_pct
        if position_usd < 1:
            logger.info(f"  Position too small (${position_usd:.2f}) — skipping (min $1)")
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": self.mode,
                "match_id": match_id,
                "map_num": map_num,
                "team_a": team_a,
                "team_b": team_b,
                "side": bet_team,
                "model_prob": f"{prob:.4f}",
                "market_price": f"{market_price:.4f}",
                "edge": f"{edge:.4f}",
                "kelly_pct": f"{kelly_pct:.4f}",
                "position_size_usd": f"{position_usd:.2f}",
                "entry_price": "",
                "exit_price": "",
                "exit_reason": "below_min",
                "pnl": "0",
                "map_winner": "",
                "model_correct": "",
            })
            return

        # Calculate shares: at price p, $X buys X/p shares
        shares = position_usd / market_price

        logger.info(
            f"  BUY {bet_team} | Edge: {edge:.1%} | "
            f"Kelly: {kelly_pct:.1%} | Size: ${position_usd:.2f} | "
            f"Shares: {shares:.1f} @ {market_price:.2f}"
        )

        # Resolve token_id
        token_id = None
        tick_size = "0.01"
        neg_risk = False

        try:
            market_info = self.trader.get_market_info(condition_id)
            tick_size = market_info["tick_size"]
            neg_risk = market_info["neg_risk"]

            # Map side to token:
            # If not swapped: side "a" → token_a (outcome 0), side "b" → token_b
            # If swapped: side "a" → token_b (outcome 1), side "b" → token_a
            if side == "a":
                token_id = market_info["token_b"] if swapped else market_info["token_a"]
            else:
                token_id = market_info["token_a"] if swapped else market_info["token_b"]
        except Exception as e:
            logger.error(f"  Failed to resolve token_id: {e}")
            if not self.paper:
                return

        entry_price = market_price
        order_id = None

        if self.paper:
            logger.info(f"  [PAPER] Order logged — not placed")
            self.bankroll -= position_usd
        else:
            try:
                resp = self.trader.buy(
                    token_id=token_id,
                    price=round(market_price, 2),
                    size=round(shares, 2),
                    tick_size=tick_size,
                    neg_risk=neg_risk,
                )
                order_id = resp.get("orderID") or resp.get("id")
                logger.info(f"  Order placed: {order_id}")
            except Exception as e:
                logger.error(f"  Order failed: {e}")
                return

        # Track position
        pos_key = f"{match_id}_map{map_num}_{side}"
        with self._positions_lock:
            self.positions[pos_key] = {
                "token_id": token_id,
                "condition_id": condition_id,
                "match_id": match_id,
                "map_num": map_num,
                "team_a": team_a,
                "team_b": team_b,
                "bet_team": bet_team,
                "side": side,
                "entry_price": entry_price,
                "high_price": entry_price,
                "shares": shares,
                "position_usd": position_usd,
                "order_id": order_id,
                "tick_size": tick_size,
                "neg_risk": neg_risk,
                "swapped": swapped,
            }

        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "match_id": match_id,
            "map_num": map_num,
            "team_a": team_a,
            "team_b": team_b,
            "side": bet_team,
            "model_prob": f"{prob:.4f}",
            "market_price": f"{market_price:.4f}",
            "edge": f"{edge:.4f}",
            "kelly_pct": f"{kelly_pct:.4f}",
            "position_size_usd": f"{position_usd:.2f}",
            "entry_price": f"{entry_price:.4f}",
            "exit_price": "",
            "exit_reason": "open",
            "pnl": "",
            "map_winner": "",
            "model_correct": "",
        })

        self._save_state()

    # ──────────────────────────────────────────────────────────
    # PRICE MONITOR (trailing floor + stop loss)
    # ──────────────────────────────────────────────────────────

    def _start_price_monitor(self):
        """Start the background price monitor thread."""
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._price_monitor_loop, daemon=True
        )
        self._monitor_thread.start()
        logger.info(f"Price monitor started (every {PRICE_MONITOR_INTERVAL}s)")

    def _stop_price_monitor(self):
        self._monitor_running = False

    def _price_monitor_loop(self):
        """Check all open positions every PRICE_MONITOR_INTERVAL seconds."""
        while self._monitor_running:
            try:
                self._check_all_positions()
            except Exception as e:
                logger.error(f"Price monitor error: {e}")
            time.sleep(PRICE_MONITOR_INTERVAL)

    def _check_all_positions(self):
        """Check each open position against trailing floor and stop loss."""
        with self._positions_lock:
            keys = list(self.positions.keys())

        for key in keys:
            with self._positions_lock:
                pos = self.positions.get(key)
                if pos is None:
                    continue

            token_id = pos["token_id"]

            # Get current price
            try:
                if self.paper and token_id is None:
                    # Paper mode without real token — use Polymarket Gamma API
                    prices = get_match_prices(pos["team_a"], pos["team_b"])
                    if prices and pos["map_num"] in prices["maps"]:
                        map_data = prices["maps"][pos["map_num"]]
                        current = map_data["price_a"] if pos["side"] == "a" else map_data["price_b"]
                    else:
                        continue
                else:
                    price_data = self.trader.get_market_price(token_id)
                    current = price_data["mid"] or price_data["last_trade"]
                    if current is None:
                        continue
            except Exception:
                continue

            entry = pos["entry_price"]
            high = pos["high_price"]

            # Update high water mark
            if current > high:
                with self._positions_lock:
                    if key in self.positions:
                        self.positions[key]["high_price"] = current
                high = current

            # Check stop loss (-30%)
            stop = compute_stop_loss(entry)
            if current <= stop:
                logger.info(
                    f"  STOP LOSS triggered for {pos['bet_team']} Map {pos['map_num']} "
                    f"({current:.2f} <= {stop:.2f})"
                )
                self._exit_position(key, current, "stop_loss")
                continue

            # Check trailing floor
            floor = compute_floor(entry, high)
            if floor is not None and current <= floor:
                logger.info(
                    f"  TRAILING FLOOR triggered for {pos['bet_team']} Map {pos['map_num']} "
                    f"({current:.2f} <= floor {floor:.2f})"
                )
                self._exit_position(key, current, "trailing_floor")
                continue

    def _record_map_winner(self, match_id, map_num, winner):
        """Record the map winner on any open position for this map."""
        with self._positions_lock:
            for key, pos in self.positions.items():
                if pos["match_id"] == match_id and pos["map_num"] == map_num:
                    pos["map_winner"] = winner
                    pos["model_correct"] = 1 if winner == pos["bet_team"] else 0
                    logger.info(
                        f"  Map {map_num} winner: {winner} — "
                        f"model {'correct' if pos['model_correct'] else 'wrong'}"
                    )
                    break

    def _exit_position(self, pos_key, exit_price, reason):
        """Exit a position — sell or log paper exit."""
        with self._positions_lock:
            pos = self.positions.pop(pos_key, None)
        if pos is None:
            return

        entry = pos["entry_price"]
        shares = pos["shares"]
        pnl = (exit_price - entry) * shares

        logger.info(
            f"  EXIT {pos['bet_team']} Map {pos['map_num']} | "
            f"Entry: {entry:.2f} → Exit: {exit_price:.2f} | "
            f"PnL: ${pnl:+.2f} | Reason: {reason}"
        )

        if self.paper:
            # Return capital + pnl to bankroll
            self.bankroll += pos["position_usd"] + pnl
            logger.info(f"  [PAPER] Bankroll: ${self.bankroll:.2f}")
        else:
            try:
                self.trader.sell(
                    token_id=pos["token_id"],
                    price=round(exit_price, 2),
                    size=round(shares, 2),
                    tick_size=pos["tick_size"],
                    neg_risk=pos["neg_risk"],
                )
            except Exception as e:
                logger.error(f"  Sell order failed: {e}")

        log_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "match_id": pos["match_id"],
            "map_num": pos["map_num"],
            "team_a": pos["team_a"],
            "team_b": pos["team_b"],
            "side": pos["bet_team"],
            "model_prob": "",
            "market_price": "",
            "edge": "",
            "kelly_pct": "",
            "position_size_usd": f"{pos['position_usd']:.2f}",
            "entry_price": f"{entry:.4f}",
            "exit_price": f"{exit_price:.4f}",
            "exit_reason": reason,
            "pnl": f"{pnl:.2f}",
            "map_winner": pos.get("map_winner", ""),
            "model_correct": pos.get("model_correct", ""),
        })

        # Persist state (or clean up if no positions left)
        with self._positions_lock:
            has_positions = bool(self.positions)
        if has_positions:
            self._save_state()
        else:
            self._clear_state()

    # ──────────────────────────────────────────────────────────
    # SCRAPER HELPERS
    # ──────────────────────────────────────────────────────────

    def _scrape_game(self, match_id, game_num):
        """Rescrape a single completed map."""
        logger.info(f"Rescraping match {match_id} game {game_num}...")
        project_root = os.path.join(os.path.dirname(__file__), "..")
        scraper = os.path.join(project_root, "Scraper", "main.py")
        try:
            os.system(f'py "{scraper}" --match {match_id} --game {game_num}')
        except Exception as e:
            logger.error(f"Scrape failed: {e}")

    def _scrape_match(self, match_id):
        """Scrape full match after series ends."""
        logger.info(f"Scraping full match {match_id}...")
        project_root = os.path.join(os.path.dirname(__file__), "..")
        scraper = os.path.join(project_root, "Scraper", "main.py")
        try:
            os.system(f'py "{scraper}" --match {match_id}')
        except Exception as e:
            logger.error(f"Scrape failed: {e}")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="VPM Trading Agent")
    parser.add_argument(
        "--live", action="store_true",
        help="Enable live trading (default: paper mode)"
    )
    parser.add_argument(
        "--bankroll", type=float, default=DEFAULT_PAPER_BANKROLL,
        help=f"Paper trading bankroll (default: ${DEFAULT_PAPER_BANKROLL})"
    )
    args = parser.parse_args()

    if args.live:
        confirm = input(
            "⚠️  LIVE TRADING MODE — real money will be used. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

    agent = TradingAgent(paper=not args.live, bankroll=args.bankroll)
    agent.run()


if __name__ == "__main__":
    main()
