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
import math
import time
import sqlite3
import logging
import argparse
import threading
import requests
from datetime import datetime, timezone, timedelta

# Load .env from project root
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from Agent.upcoming import get_upcoming_matches
from Agent.poller import poll_match_status
from PolyInt.polymatches import get_match_prices
from PolyInt.trade import PolyTrader
from Predict.predict import predict_elo_only, precompute_elo_snapshot, EXCEL_FILE as _PREDICT_EXCEL

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

EDGE_THRESHOLD = 0.05          # 5% minimum edge to enter
MAX_CONFIDENCE = 0.70          # cap model probability at 70% (either side)
STOP_LOSS_PCT = None           # disabled 2026-05-16 -- floor_search on N=89 (50.6% win rate)
# Trailing floor ladder: (gain_threshold, floor_offset) pairs.
# Activates later (+38%) with wider ~25% gaps -- top config from event-only
# floor_search on real recorded ticks (29 paths, 37.9% pool win rate).
# Pattern: clip losers with the stop loss, let winners run further before
# the floor engages, then trail in wide steps so a single round swing
# doesn't chop-exit a real winner.
TRAILING_FLOOR_LADDER = (
    (0.38, 0.13),   # +38% peak → floor +13% (gap 25%)
    (0.50, 0.26),   # +50% peak → floor +26% (gap 25%)
    (0.62, 0.39),   # +62% peak → floor +39% (gap 24%)
    (0.75, 0.52),   # +75% peak → floor +52% (gap 23%)
    (0.87, 0.64),   # +87% peak → floor +64% (gap 23%)
    (0.99, 0.77),   # +99% peak → floor +77% (gap 22%)
)
TRAILING_FLOOR_START = TRAILING_FLOOR_LADDER[0][0]
TRAILING_FLOOR_STEP = 0.08     # ladder granularity past the last explicit tier
ENTRY_WINDOW_SECS = 5 * 60    # 5 min entry window for maps 2/3
COOLDOWN_SECS = 5 * 60         # 5 min cooldown after map final
POST_MATCH_REPOLL_DELAY = 5 * 60  # wait 5 min after a match ends before repolling (VLR ETA lag)
PRE_MATCH_LEAD = 10 * 60       # wake up 10 min before match
POLL_INTERVAL = 60              # VLR poll interval (seconds)
PRICE_MONITOR_INTERVAL = 1     # price check interval (seconds)
FILL_POLL_INTERVAL = 2         # order fill check interval (seconds)
FILL_POLL_TIMEOUT = 30         # max seconds to wait for a fill
MIN_CLOB_SHARES = 5            # Polymarket minimum order size in shares
MIN_BUY_SHARES = 5.5           # Buy above 5 so post-fee balance stays >= MIN_CLOB_SHARES
DEFAULT_PAPER_BANKROLL = 1000  # $1000 default paper bankroll

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
STATE_FILE = os.path.join(LOG_DIR, "agent_state.json")
TRADE_LOG = os.path.join(LOG_DIR, "trades.csv")
AGENT_LOG = os.path.join(LOG_DIR, "trades.log")
PAPER_TRADE_LOG = os.path.join(LOG_DIR, "papertrades.csv")
PAPER_AGENT_LOG = os.path.join(LOG_DIR, "papertrades.log")
DB_FILE = os.path.join(LOG_DIR, "agent.db")
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

# Module-level reference to agent instance for SQLite writes (set by TradingAgent.__init__)
_agent_instance = None


def log_trade(row):
    """Append a row to the active trade CSV and SQLite trades table."""
    path = _active_trade_log
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    # Also write to SQLite
    if _agent_instance is not None:
        try:
            _agent_instance._record_trade(row)
        except Exception:
            pass


def notify(msg):
    """Send a Discord notification via webhook. Fails silently."""
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
    except Exception:
        pass


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

    Uses TRAILING_FLOOR_LADDER for the explicit tiers, then tracks in
    TRAILING_FLOOR_STEP increments past the last explicit tier.

    Returns the floor price, or None if gain hasn't reached the first tier.
    """
    if entry_price <= 0:
        return None

    gain_pct = (high_price - entry_price) / entry_price

    eps = 1e-9
    if gain_pct + eps < TRAILING_FLOOR_START:
        return None

    floor_offset = None
    for threshold, offset in TRAILING_FLOOR_LADDER:
        if gain_pct + eps >= threshold:
            floor_offset = offset
        else:
            break

    last_threshold, last_offset = TRAILING_FLOOR_LADDER[-1]
    if gain_pct + eps >= last_threshold + TRAILING_FLOOR_STEP:
        extra_tiers = int((gain_pct - last_threshold + eps) / TRAILING_FLOOR_STEP)
        floor_offset = last_offset + extra_tiers * TRAILING_FLOOR_STEP

    return entry_price * (1 + floor_offset)


def compute_stop_loss(entry_price):
    """Compute the stop loss price, or None if disabled."""
    if STOP_LOSS_PCT is None:
        return None
    return entry_price * (1 + STOP_LOSS_PCT)


# ══════════════════════════════════════════════════════════════
# TRADING AGENT
# ══════════════════════════════════════════════════════════════

class TradingAgent:
    def __init__(self, paper=True, bankroll=DEFAULT_PAPER_BANKROLL):
        global _active_trade_log, _agent_instance

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


        # Balance lock: prevents concurrent entries from double-spending
        self._balance_lock = threading.Lock()

        # Active match threads: match_id -> Thread
        self._active_matches = {}
        self._active_matches_lock = threading.Lock()

        # match_id -> finish_timestamp (unix). Prevents re-dispatching a match
        # that just completed if VLR still lists it as live (e.g. a poller bug
        # falsely flags is_final and we re-fetch /matches 5 min later).
        self._recently_completed = {}
        self._recently_completed_ttl = 6 * 60 * 60  # 6 hours

        # Event to wake the main loop when a match thread finishes
        self._match_done = threading.Event()

        # Pending entry order IDs: map_num -> order_id (for targeted cancellation)
        self._pending_entry_orders = {}

        # Restore state from previous run (if any)
        self._load_state()

        # Price monitor thread
        self._monitor_running = False
        self._monitor_thread = None

        # Price watchers: keep recording ticks after position exit until map resolves
        # {pos_key: {"match_id", "map_num", "token_id", "side"}}
        self._price_watchers = {}

        # SQLite database
        self._init_db()
        _agent_instance = self

        # Pre-warm the joblib model so the first edge check doesn't pay the
        # cold-unpickle cost. On Windows this can stall for minutes when AV
        # scans sklearn DLLs or the dashboard process hits the same file.
        from Predict.predict import _get_model_meta
        _preload_start = time.time()
        _get_model_meta()
        logger.info(f"Model pre-loaded in {time.time()-_preload_start:.1f}s")

    # ──────────────────────────────────────────────────────────
    # SQLITE PRICE TICK DB
    # ──────────────────────────────────────────────────────────

    def _init_db(self):
        """Create the SQLite database and price_ticks table if needed."""
        # WAL mode lets the Dashboard's read-only connections coexist with the
        # agent's writes without blocking. Busy timeout bounds how long any
        # individual SQLite call can wait for a lock before raising.
        self._db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db_lock = threading.Lock()
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS price_ticks (
                timestamp   TEXT,
                match_id    TEXT,
                map_num     INTEGER,
                token_id    TEXT,
                side        TEXT,
                price       REAL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_match_map_ts
            ON price_ticks (match_id, map_num, timestamp)
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS elo_snapshots (
                timestamp   TEXT,
                player      TEXT,
                team        TEXT,
                elo         REAL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_elo_ts
            ON elo_snapshots (timestamp)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_elo_player
            ON elo_snapshots (player)
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                timestamp   TEXT,
                match_id    TEXT,
                map_num     INTEGER,
                event_type  TEXT,
                side        TEXT,
                price       REAL,
                reason      TEXT
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_match_map
            ON events (match_id, map_num)
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                timestamp       TEXT,
                mode            TEXT,
                match_id        TEXT,
                map_num         INTEGER,
                team_a          TEXT,
                team_b          TEXT,
                side            TEXT,
                model_prob      REAL,
                market_price    REAL,
                edge            REAL,
                kelly_pct       REAL,
                position_size_usd REAL,
                entry_price     REAL,
                exit_price      REAL,
                exit_reason     TEXT,
                pnl             REAL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_match_map
            ON trades (match_id, map_num)
        """)
        self._db.commit()
        logger.info(f"SQLite DB ready: {DB_FILE}")

    def _record_tick(self, match_id, map_num, token_id, side, price):
        """Write a single price tick to SQLite."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._db_lock:
            self._db.execute(
                "INSERT INTO price_ticks VALUES (?, ?, ?, ?, ?, ?)",
                (ts, str(match_id), map_num, token_id, side, price),
            )
            self._db.commit()

    def _record_trade(self, row):
        """Write a trade row to SQLite (mirrors log_trade CSV write)."""
        def _f(v):
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        with self._db_lock:
            self._db.execute(
                "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.get("timestamp"),
                    row.get("mode"),
                    row.get("match_id"),
                    int(row["map_num"]) if row.get("map_num") not in (None, "") else None,
                    row.get("team_a"),
                    row.get("team_b"),
                    row.get("side"),
                    _f(row.get("model_prob")),
                    _f(row.get("market_price")),
                    _f(row.get("edge")),
                    _f(row.get("kelly_pct")),
                    _f(row.get("position_size_usd")),
                    _f(row.get("entry_price")),
                    _f(row.get("exit_price")),
                    row.get("exit_reason"),
                    _f(row.get("pnl")),
                ),
            )
            self._db.commit()

    def _record_event(self, match_id, map_num, event_type, side, price, reason=None):
        """Write an entry/exit event to SQLite."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._db_lock:
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, str(match_id), map_num, event_type, side, price, reason),
            )
            self._db.commit()

    def _record_elo_snapshot(self, all_player_elos):
        """Write per-player Elo ratings to SQLite.

        all_player_elos: {player: (team, elo), ...}
        """
        ts = datetime.now(timezone.utc).isoformat()
        rows = [(ts, player, team, elo)
                for player, (team, elo) in all_player_elos.items()]
        if not rows:
            return
        with self._db_lock:
            self._db.executemany(
                "INSERT INTO elo_snapshots VALUES (?, ?, ?, ?)", rows
            )
            self._db.commit()

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
        """Restore open positions and bankroll from disk, dropping any with zero balance."""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if state.get("mode") != self.mode:
                logger.info(f"State file is for {state.get('mode')} mode, ignoring")
                return
            positions = state.get("positions", {})
            if not positions:
                return

            # In live mode, verify each position still has shares on-chain
            if not self.paper:
                verified = {}
                for key, pos in positions.items():
                    try:
                        balance = self.trader.get_position(pos["token_id"])
                        if balance > 0:
                            verified[key] = pos
                        else:
                            logger.info(f"  Dropping {key}: no shares on-chain (resolved or sold)")
                    except Exception as e:
                        logger.warning(f"  Could not verify {key}, keeping: {e}")
                        verified[key] = pos
                positions = verified

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
            else:
                logger.info("All saved positions resolved — starting fresh")
                self._clear_state()
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
        notify(f"**Agent started** ({self.mode} mode) | Bankroll: ${self.bankroll:.2f}")
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
                notify(f"**Error** in main loop: {e}")
                time.sleep(60)

    def _run_cycle(self):
        """One cycle: find actionable matches, dispatch threads for each."""
        # Clean up finished match threads
        with self._active_matches_lock:
            done = [mid for mid, t in self._active_matches.items() if not t.is_alive()]
            for mid in done:
                self._active_matches.pop(mid)

        # Expire stale entries from the recently-completed dedupe map
        now_ts = time.time()
        with self._active_matches_lock:
            expired = [mid for mid, ts in self._recently_completed.items()
                       if now_ts - ts > self._recently_completed_ttl]
            for mid in expired:
                self._recently_completed.pop(mid)

        logger.info("Fetching upcoming VCT matches...")
        matches = get_upcoming_matches(vct_only=True)

        self._match_done.clear()

        if not matches:
            logger.info("No upcoming VCT matches found. Sleeping 30 min...")
            self._match_done.wait(timeout=30 * 60)
            return

        now = datetime.now(timezone.utc)

        # Collect all actionable matches (live or starting within 10 min)
        actionable = []
        for m in matches:
            with self._active_matches_lock:
                if m["match_id"] in self._active_matches:
                    continue  # Already being handled
                if m["match_id"] in self._recently_completed:
                    continue  # Just finished — VLR hasn't dropped it from /matches yet
            if m["is_live"]:
                actionable.append(m)
            elif m["start_time"] is not None:
                if m["start_time"] > now - timedelta(minutes=30):
                    wake_time = m["start_time"] - timedelta(seconds=PRE_MATCH_LEAD)
                    if wake_time <= now:
                        actionable.append(m)

        if not actionable:
            # Find next upcoming match and sleep until T-10
            timed = [m for m in matches if m["start_time"] is not None
                     and m["start_time"] > now - timedelta(minutes=30)]
            if not timed:
                logger.info("No actionable matches. Sleeping 30 min...")
                self._match_done.wait(timeout=30 * 60)
                return

            timed.sort(key=lambda m: m["start_time"])
            nxt = timed[0]
            wake_time = nxt["start_time"] - timedelta(seconds=PRE_MATCH_LEAD)
            wait_secs = (wake_time - now).total_seconds()
            if wait_secs > 0:
                logger.info(
                    f"Next match: {nxt['team_a']} vs {nxt['team_b']} "
                    f"at {nxt['start_time'].astimezone().strftime('%a %b %d %H:%M %Z')}"
                )
                logger.info(f"Sleeping {wait_secs/60:.1f} min until T-10 (or until current match ends)...")
                self._match_done.wait(timeout=wait_secs)
            return

        # Dispatch a thread for each actionable match
        for m in actionable:
            mid = m["match_id"]
            logger.info(f"Dispatching match thread: {m['team_a']} vs {m['team_b']} ({mid})")
            t = threading.Thread(
                target=self._handle_match_safe, args=(m,),
                name=f"match-{mid}", daemon=True,
            )
            with self._active_matches_lock:
                self._active_matches[mid] = t
            t.start()

        # Sleep until the next non-active match is approaching, so we pick up
        # overlapping matches (e.g. one region running long into another's slot).
        # Match threads already running handle their own maps independently.
        # Uses _match_done event so a finishing match thread wakes us immediately
        # (VCT matches start when the previous one ends, not at the scheduled time).
        self._match_done.clear()
        with self._active_matches_lock:
            active_ids = set(self._active_matches.keys())
        timed = [m for m in matches if m["start_time"] is not None
                 and m["start_time"] > now - timedelta(minutes=30)
                 and m["match_id"] not in active_ids
                 and m not in actionable]
        if timed:
            timed.sort(key=lambda m: m["start_time"])
            nxt = timed[0]
            wake_time = nxt["start_time"] - timedelta(seconds=PRE_MATCH_LEAD)
            wait_secs = max((wake_time - now).total_seconds(), 60)
            logger.info(
                f"Next match: {nxt['team_a']} vs {nxt['team_b']} "
                f"at {nxt['start_time'].astimezone().strftime('%a %b %d %H:%M %Z')}"
            )
            logger.info(f"Sleeping {wait_secs/60:.1f} min until T-10 (or until current match ends)...")
            self._match_done.wait(timeout=wait_secs)
        else:
            # No upcoming matches beyond what's active — check back in 30 min
            self._match_done.wait(timeout=30 * 60)

    def _handle_match_safe(self, match):
        """Wrapper around _handle_match with error handling for threads."""
        try:
            self._handle_match(match)
        except Exception as e:
            logger.exception(f"Error handling match {match['team_a']} vs {match['team_b']}: {e}")
            notify(f"**Error** in match {match['team_a']} vs {match['team_b']}: {e}")
        finally:
            with self._active_matches_lock:
                self._active_matches.pop(match["match_id"], None)
                self._recently_completed[match["match_id"]] = time.time()
            # VLR doesn't update the next match's ETA the instant this one ends.
            # Give it a buffer so the repoll sees the refreshed relative countdown.
            time.sleep(POST_MATCH_REPOLL_DELAY)
            self._match_done.set()

    # ──────────────────────────────────────────────────────────
    # MATCH HANDLER
    # ──────────────────────────────────────────────────────────

    def _handle_match(self, match):
        """Handle a full match (Bo3: up to 3 maps, Bo5: up to 5)."""
        team_a = match["team_a"]
        team_b = match["team_b"]
        match_url = match["match_url"]
        match_id = match["match_id"]

        logger.info(
            f"\n{'='*50}\n"
            f"MATCH: {team_a} vs {team_b}\n"
            f"URL: {match_url}\n"
            f"{'='*50}"
        )
        notify(f"**Match starting** | {team_a} vs {team_b}")

        # Kick off the Elo snapshot pre-compute in the background. By the time
        # veto is detected (usually minutes away), the cache will be warm and
        # the Map 1 edge check runs in ~5s instead of ~100s.
        self._trigger_elo_precompute("match dispatch")

        # ── Map 1: poll for veto, then retry entry until filled or window expires.
        # No pre-veto wait — Map 1's entry window is gated by the veto itself, not
        # a cooldown from a prior map. Start polling VLR immediately on dispatch.
        if not match["is_live"]:
            map1_name = self._wait_for_veto(match_url)
            logger.info(f"Map 1 veto detected: {map1_name} — entry window OPEN (5 min)")
            # Format unknown at this point; default Bo3. Moneyline preference
            # for the decider map gets the real value once we poll below.
            self._retry_entry_until_filled(team_a, team_b, match_id, map_num=1, map_name=map1_name, max_maps=3)
        else:
            logger.info("Match already live — skipping Map 1 entry")

        # ── Poll through the series ──
        # Seed prev_finals with maps already final (skip cooldown for those)
        prev_finals = set()
        initial_status = poll_match_status(match_url)
        # Bo3 = 2 wins / max 3 maps, Bo5 = 3 wins / max 5 maps. Default Bo3 if
        # the format note couldn't be parsed.
        maps_to_win = initial_status["maps_to_win"] if initial_status else 2
        max_maps = 2 * maps_to_win - 1
        if initial_status:
            for map_num, map_data in initial_status["maps"].items():
                if map_data["final"]:
                    score = f"{map_data['score_a']}-{map_data['score_b']}"
                    logger.info(f"Map {map_num} already FINAL ({score}) — skipping cooldown")
                    prev_finals.add(map_num)

            # If maps are already done but series isn't over, enter the next map
            if prev_finals and not initial_status["is_final"]:
                wins_a = sum(
                    1 for m in initial_status["maps"].values()
                    if m["final"] and m["score_a"] > m["score_b"]
                )
                wins_b = sum(
                    1 for m in initial_status["maps"].values()
                    if m["final"] and m["score_b"] > m["score_a"]
                )
                if wins_a >= maps_to_win or wins_b >= maps_to_win:
                    logger.info("Series already decided — no more maps")
                else:
                    next_map = max(prev_finals) + 1
                    if next_map <= max_maps:
                        # Skip if the next map is already live (rounds being played)
                        next_map_data = initial_status["maps"].get(next_map)
                        if next_map_data and (next_map_data["score_a"] + next_map_data["score_b"]) > 0:
                            logger.info(f"Map {next_map} already live — skipping entry")
                        else:
                            next_map_name = next_map_data.get("map_name") if next_map_data else None
                            map_label = f" ({next_map_name})" if next_map_name else ""
                            logger.info(f"Map {next_map}{map_label} entry window OPEN (5 min)")
                            self._retry_entry_until_filled(team_a, team_b, match_id, map_num=next_map, map_name=next_map_name, max_maps=max_maps)

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
                    if next_map <= max_maps:
                        # Check if either team has clinched (series decided)
                        wins_a = sum(
                            1 for m in status["maps"].values()
                            if m["final"] and m["score_a"] > m["score_b"]
                        )
                        wins_b = sum(
                            1 for m in status["maps"].values()
                            if m["final"] and m["score_b"] > m["score_a"]
                        )
                        if wins_a >= maps_to_win or wins_b >= maps_to_win:
                            logger.info("Series decided — no more maps")
                            series_over = True
                            break

                        # Cooldown + rescrape + entry for next map
                        self._between_maps(
                            team_a, team_b, match_id, match_url, map_num, next_map, max_maps=max_maps
                        )

            if not series_over:
                time.sleep(POLL_INTERVAL)

        # ── Record winners for any maps that finalized in the last poll ──
        if status:
            for map_num, map_data in status["maps"].items():
                if map_data["final"] and map_data["score_a"] != map_data["score_b"]:
                    map_winner = team_a if map_data["score_a"] > map_data["score_b"] else team_b
                    self._record_map_winner(match_id, map_num, map_winner)

        # ── Resolve any positions still open (e.g. held to market resolution) ──
        self._resolve_remaining_positions(match_id)

        # ── Series complete: scrape final map ──
        logger.info("Scraping final match data...")
        self._scrape_match(match_id)
        logger.info(f"Match complete: {team_a} vs {team_b}")

        # Find next match for Discord notification
        next_match_str = "none found"
        try:
            upcoming = get_upcoming_matches(vct_only=True)
            upcoming = [m for m in upcoming if m["match_id"] != match_id]
            if upcoming:
                timed = [m for m in upcoming if m["start_time"] is not None]
                live = [m for m in upcoming if m["is_live"]]
                if live:
                    nxt = live[0]
                    next_match_str = f"{nxt['team_a']} vs {nxt['team_b']} (LIVE)"
                elif timed:
                    timed.sort(key=lambda m: m["start_time"])
                    nxt = timed[0]
                    next_match_str = f"{nxt['team_a']} vs {nxt['team_b']} @ <t:{int(nxt['start_time'].timestamp())}:t>"
        except Exception:
            pass

        notify(f"**Match complete** | {team_a} vs {team_b}\n**Next match:** {next_match_str}")

    # ──────────────────────────────────────────────────────────
    # ENTRY WINDOW
    # ──────────────────────────────────────────────────────────

    def _retry_entry_until_filled(self, team_a, team_b, match_id, map_num, map_name=None, max_maps=3):
        """Retry _enter_map with fresh prices until filled or entry window expires."""
        deadline = time.time() + ENTRY_WINDOW_SECS
        cached_prediction = None
        while time.time() < deadline:
            result, prediction = self._enter_map(team_a, team_b, match_id, map_num, map_name=map_name, cached_prediction=cached_prediction, max_maps=max_maps)
            if result == "no_edge":
                logger.info(f"  No edge detected — skipping retries, waiting for next map")
                break
            if result:
                break
            # Cache prediction from first run so retries only refresh prices
            if cached_prediction is None and prediction is not None:
                cached_prediction = prediction
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            logger.info(f"  Retrying entry in {FILL_POLL_TIMEOUT}s ({remaining:.0f}s left in window)...")
            time.sleep(min(FILL_POLL_TIMEOUT, remaining))
        self._close_entry_window(map_num=map_num)

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
        """Poll VLR until Map 1's map name appears (veto complete)."""
        logger.info("Waiting for map veto (Map 1 name to appear)...")
        while True:
            status = poll_match_status(match_url)
            if status is None:
                time.sleep(POLL_INTERVAL)
                continue
            map1 = status["maps"].get(1, {})
            map_name = map1.get("map_name")
            if map_name:
                return map_name
            time.sleep(POLL_INTERVAL)

    def _close_entry_window(self, map_num):
        """Cancel the unfilled entry order for this map (if any)."""
        logger.info(f"Map {map_num} entry window CLOSED")
        order_id = self._pending_entry_orders.pop(map_num, None)
        if order_id and not self.paper:
            try:
                self.trader.cancel(order_id)
                logger.info(f"  Cancelled unfilled entry order {order_id}")
            except Exception as e:
                logger.error(f"Error cancelling order {order_id}: {e}")

    def _between_maps(self, team_a, team_b, match_id, match_url, finished_map, next_map, max_maps=3):
        """Cooldown, rescrape, then open entry for next map."""
        logger.info(f"Map {finished_map} done — {COOLDOWN_SECS//60} min cooldown")

        # Scrape the finished map (synchronous; Excel mtime bumps on completion)
        self._scrape_game(match_id, finished_map)

        # Scraper just rewrote Excel — trigger an Elo precompute so the next
        # map's edge check uses post-scrape ratings without paying the 34s
        # recompute inline. Runs during the cooldown window in parallel.
        self._trigger_elo_precompute(f"post-scrape Map {finished_map}")

        # Cooldown
        time.sleep(COOLDOWN_SECS)

        # Poll for next map name (up to 60s, then enter without it)
        next_map_name = None
        logger.info(f"Polling for Map {next_map} name...")
        poll_deadline = time.time() + 60
        while time.time() < poll_deadline:
            status = poll_match_status(match_url)
            if status and next_map in status["maps"]:
                next_map_name = status["maps"][next_map].get("map_name")
                if next_map_name:
                    break
            time.sleep(POLL_INTERVAL)

        map_label = f" ({next_map_name})" if next_map_name else " (unknown map)"
        logger.info(f"Map {next_map}{map_label} entry window OPEN (5 min)")
        self._retry_entry_until_filled(team_a, team_b, match_id, map_num=next_map, map_name=next_map_name, max_maps=max_maps)

    # ──────────────────────────────────────────────────────────
    # EDGE DETECTION & ENTRY
    # ──────────────────────────────────────────────────────────

    def _enter_map(self, team_a, team_b, match_id, map_num, map_name=None, cached_prediction=None, max_maps=3):
        """Check edge and enter a position if edge > threshold. Returns (result, prediction) tuple."""
        map_label = f" ({map_name})" if map_name else ""
        logger.info(f"Checking edge for Map {map_num}{map_label}: {team_a} vs {team_b}")

        if cached_prediction:
            prob_a = cached_prediction["prob_a"]
            prob_b = cached_prediction["prob_b"]
            prediction = cached_prediction
            logger.info(f"  Model (cached): {team_a} {prob_a:.1%}  {team_b} {prob_b:.1%}")
        else:
            # Run Elo model
            try:
                prob_a, elo_features, player_info, maps_played, all_player_elos = predict_elo_only(team_a, team_b, map_name=map_name)
            except Exception as e:
                logger.error(f"Model prediction failed: {e}")
                return False, None

            # Log full Elo snapshot to SQLite (all players, all teams)
            try:
                self._record_elo_snapshot(all_player_elos)
            except Exception as e:
                logger.debug(f"  Elo snapshot write error: {e}")

            # Check that both teams have meaningful data (not just default Elo)
            players_a = player_info.get(list(player_info.keys())[0], []) if player_info else []
            players_b = player_info.get(list(player_info.keys())[1], []) if len(player_info) > 1 else []
            if not players_a or not players_b:
                logger.warning(f"  Missing player data for one or both teams -- skipping")
                return False, None

            # Skip teams with too little data for reliable predictions
            MIN_MAPS = 8
            for team, count in maps_played.items():
                if count < MIN_MAPS:
                    logger.warning(f"  {team} has only {count} maps in dataset (min {MIN_MAPS}) -- skipping")
                    return False, None

            # Cap confidence at MAX_CONFIDENCE (either side)
            raw_prob_a = prob_a
            prob_a = min(prob_a, MAX_CONFIDENCE)
            prob_a = max(prob_a, 1 - MAX_CONFIDENCE)
            prob_b = 1 - prob_a
            prediction = {"prob_a": prob_a, "prob_b": prob_b}

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
            return False, prediction

        if not prices:
            logger.warning(f"  No Polymarket markets found")
            return False, prediction

        # On the series-deciding map (Map 3 in Bo3, Map 5 in Bo5), prefer
        # moneyline — same bet as the map winner since the series ends here,
        # but moneyline has higher volume. Earlier maps in a Bo5 must use the
        # per-map market because moneyline ≠ that map's winner.
        map_market = None
        market_source = f"Map {map_num}"
        is_moneyline = False
        if map_num == max_maps and prices.get("moneyline"):
            map_market = prices["moneyline"]
            market_source = "Moneyline"
            is_moneyline = True
            logger.info(f"  Using moneyline market for Map {map_num} decider (higher volume)")
        elif map_num in prices["maps"]:
            map_market = prices["maps"][map_num]

        if not map_market:
            logger.warning(f"  No Polymarket market found for Map {map_num}")
            return False, prediction

        condition_id = map_market["condition_id"]
        swapped = prices["swapped"]

        market_price_a = map_market["price_a"]
        market_price_b = map_market["price_b"]
        token_a = None
        token_b = None
        try:
            market_info = self.trader.get_market_info(condition_id)
            token_a = market_info["token_b"] if swapped else market_info["token_a"]
            token_b = market_info["token_a"] if swapped else market_info["token_b"]
        except Exception as e:
            logger.warning(f"  CLOB market info fetch failed for watchers: {e}")

        if not self.paper and token_a and token_b:
            try:
                ask_a = self.trader.get_market_price(token_a)["best_ask"]
                ask_b = self.trader.get_market_price(token_b)["best_ask"]
                if ask_a is not None and ask_b is not None:
                    market_price_a = ask_a
                    market_price_b = ask_b
            except Exception as e:
                logger.debug(f"  Ask price fetch failed, using Gamma: {e}")

        # Register price watchers for both sides so ticks are recorded
        # regardless of whether a bet is placed (for trailing floor analysis)
        if token_a and token_b:
            wkey_a = f"{match_id}_{map_num}_{team_a}"
            wkey_b = f"{match_id}_{map_num}_{team_b}"
            registered = []
            if wkey_a not in self._price_watchers:
                self._price_watchers[wkey_a] = {
                    "match_id": match_id, "map_num": map_num,
                    "token_id": token_a, "side": team_a,
                }
                registered.append(team_a)
            if wkey_b not in self._price_watchers:
                self._price_watchers[wkey_b] = {
                    "match_id": match_id, "map_num": map_num,
                    "token_id": token_b, "side": team_b,
                }
                registered.append(team_b)
            if registered:
                logger.info(f"  Price watchers registered for Map {map_num}: {', '.join(registered)}")
        else:
            logger.warning(f"  No price watchers registered for Map {map_num} — tokens unavailable")

        logger.info(
            f"  Market ({market_source}): {team_a} {market_price_a:.1%}  {team_b} {market_price_b:.1%}"
        )

        # Compute edge for both sides
        edge_a = prob_a - market_price_a
        edge_b = prob_b - market_price_b

        logger.info(f"  Edge A: {edge_a:+.1%}  Edge B: {edge_b:+.1%}")

        # Pick the side with better edge (if any exceeds threshold)
        if edge_a >= EDGE_THRESHOLD and edge_a >= edge_b:
            return self._place_entry(
                team_a, team_b, match_id, map_num,
                side="a", prob=prob_a, market_price=market_price_a,
                edge=edge_a, condition_id=condition_id, swapped=swapped,
                is_moneyline=is_moneyline,
            ), prediction
        elif edge_b >= EDGE_THRESHOLD:
            return self._place_entry(
                team_a, team_b, match_id, map_num,
                side="b", prob=prob_b, market_price=market_price_b,
                edge=edge_b, condition_id=condition_id, swapped=swapped,
                is_moneyline=is_moneyline,
            ), prediction
        else:
            logger.info(f"  No edge above {EDGE_THRESHOLD:.0%} threshold — skipping")
            notify(f"**No edge** | {team_a} vs {team_b} Map {map_num} | Edge A: {edge_a:+.1%} Edge B: {edge_b:+.1%}")
            log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": self.mode,
                "match_id": match_id,
                "map_num": map_num,
                "team_a": team_a,
                "team_b": team_b,
                "side": "none",
                "model_prob": f"{prob_a:.4f}",
                "market_price": f"{market_price_a:.4f}",
                "edge": f"{edge_a:.4f}",
                "kelly_pct": "0",
                "position_size_usd": "0",
                "entry_price": "",
                "exit_price": "",
                "exit_reason": "no_edge",
                "pnl": "0",
            })
            return "no_edge", prediction

    def _place_entry(self, team_a, team_b, match_id, map_num,
                     side, prob, market_price, edge, condition_id, swapped,
                     is_moneyline=False):
        """Place an entry order (or log it in paper mode). Returns True if filled or permanently skipped."""
        bet_team = team_a if side == "a" else team_b
        kelly_pct = quarter_kelly(prob, market_price)

        # Balance lock prevents concurrent match threads from double-spending
        try:
            self._balance_lock.acquire()

            # Get current bankroll
            if self.paper:
                bankroll = self.bankroll
            else:
                bankroll = self.trader.get_balance()

            position_usd = bankroll * kelly_pct

            # Ensure we buy at least MIN_BUY_SHARES so post-fee balance stays >= MIN_CLOB_SHARES (sellable)
            min_position_usd = MIN_BUY_SHARES * market_price
            if not self.paper and position_usd < min_position_usd:
                if bankroll >= min_position_usd:
                    logger.info(f"  Position ${position_usd:.2f} below min {MIN_BUY_SHARES} shares (${min_position_usd:.2f}) — bumping to minimum")
                    position_usd = min_position_usd
                else:
                    logger.info(f"  Position too small (${position_usd:.2f}) and bankroll can't cover min {MIN_BUY_SHARES} shares (${min_position_usd:.2f}) — skipping")
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
                    })
                    return True

            if self.paper and position_usd < 1:
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
                })
                return True

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
                    return True

            entry_price = market_price
            order_id = None

            if self.paper:
                logger.info(f"  [PAPER] Order logged — not placed")
                self.bankroll -= position_usd
            else:
                # Snapshot USDC balance before buy to compute actual cost
                try:
                    balance_before = self.trader.get_balance()
                except Exception:
                    balance_before = None

                try:
                    buy_price = round(market_price, 2)
                    resp = self.trader.buy(
                        token_id=token_id,
                        price=buy_price,
                        size=round(shares, 2),
                        tick_size=tick_size,
                        neg_risk=neg_risk,
                    )
                    order_id = resp.get("orderID") or resp.get("id")
                    self._pending_entry_orders[map_num] = order_id
                    logger.info(f"  Order placed: {order_id} — polling for fill...")
                except Exception as e:
                    logger.error(f"  Order failed: {e}")
                    notify(f"**BUY FAILED** {bet_team} Map {map_num} | {e}")
                    return True

                # Poll for fill
                fill = self._poll_fill(order_id, token_id)
                self._pending_entry_orders.pop(map_num, None)
                if not fill["filled"]:
                    logger.warning(f"  Order not filled within {FILL_POLL_TIMEOUT}s — cancelling")
                    try:
                        self.trader.cancel(order_id)
                    except Exception:
                        pass
                    notify(f"**BUY UNFILLED** {bet_team} Map {map_num} — retrying")
                    return False

                # Query actual on-chain shares (fees reduce tokens received)
                try:
                    actual_shares = self.trader.get_position(token_id)
                    if actual_shares > 0:
                        shares = actual_shares
                except Exception as e:
                    logger.warning(f"  Could not verify on-chain balance: {e}")
                    shares = fill["shares"]

                # Compute actual entry price from USDC spent / shares received
                try:
                    balance_after = self.trader.get_balance()
                    if balance_before is not None and shares > 0:
                        usdc_spent = balance_before - balance_after
                        if usdc_spent > 0:
                            entry_price = usdc_spent / shares
                            position_usd = usdc_spent
                            logger.info(
                                f"  Order FILLED: {shares:.2f} shares | "
                                f"Spent: ${usdc_spent:.2f} | Avg price: {entry_price:.4f}"
                            )
                        else:
                            # Fallback to trade history
                            if fill["avg_price"] > 0:
                                entry_price = fill["avg_price"]
                            position_usd = shares * entry_price
                            logger.info(f"  Order FILLED: {shares:.2f} shares @ {entry_price:.2f} (${position_usd:.2f})")
                    else:
                        if fill["avg_price"] > 0:
                            entry_price = fill["avg_price"]
                        position_usd = shares * entry_price
                        logger.info(f"  Order FILLED: {shares:.2f} shares @ {entry_price:.2f} (${position_usd:.2f})")
                except Exception as e:
                    logger.warning(f"  Could not verify entry price from balance: {e}")
                    if fill["avg_price"] > 0:
                        entry_price = fill["avg_price"]
                    position_usd = shares * entry_price
                    logger.info(f"  Order FILLED: {shares:.2f} shares @ {entry_price:.2f} (${position_usd:.2f})")

                # Approve token for selling
                try:
                    self.trader.approve_token(token_id)
                    logger.debug(f"  Token approved for trading")
                except Exception as e:
                    logger.error(f"  Token approval failed: {e}")
        finally:
            self._balance_lock.release()

        # Snapshot best_bid at entry to use as the baseline for floor/stop math.
        # Entry fill is at best_ask; the monitor compares against best_bid, so
        # the ladder needs a bid-space baseline to avoid a 1-2 tick skew.
        entry_bid = entry_price
        if not self.paper and self.trader:
            try:
                bid = self.trader.get_market_price(token_id)["best_bid"]
                if bid is not None and bid > 0:
                    entry_bid = bid
            except Exception as e:
                logger.debug(f"  entry_bid snapshot failed, using entry_price: {e}")

        notify(
            f"**BUY {bet_team}** Map {map_num} | Edge: {edge:.1%} | "
            f"Filled: {shares:.1f} shares @ {entry_price:.2f} (${position_usd:.2f})"
        )

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
                "entry_bid": entry_bid,
                "high_price": entry_price,
                "shares": shares,
                "position_usd": position_usd,
                "order_id": order_id,
                "tick_size": tick_size,
                "neg_risk": neg_risk,
                "swapped": swapped,
                "is_moneyline": is_moneyline,
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
        })

        try:
            self._record_event(match_id, map_num, "entry", bet_team, entry_price, "edge")
        except Exception as e:
            logger.debug(f"  Event write error: {e}")

        self._save_state()
        return True

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

    def _poll_fill(self, order_id, token_id, timeout=FILL_POLL_TIMEOUT):
        """
        Poll until an order fills (or times out).

        Returns
        -------
        dict: {"filled": bool, "shares": float, "avg_price": float}
            avg_price is the volume-weighted average from actual trade history.
        """
        if order_id is None:
            return {"filled": False, "shares": 0, "avg_price": 0}

        start = time.time()
        while time.time() - start < timeout:
            try:
                order = self.trader.get_order(order_id)
                status = order.get("status", "").lower()
                size_matched = float(order.get("size_matched", 0))
                original_size = float(order.get("original_size", order.get("size", 0)))

                if status == "matched" or (original_size > 0 and size_matched >= original_size):
                    avg_price = self._get_avg_fill_price(order_id, size_matched)
                    return {"filled": True, "shares": size_matched, "avg_price": avg_price}

                if status in ("cancelled", "expired", "rejected", "failed", "error"):
                    logger.warning(f"  Order {order_id} status: {status}")
                    return {"filled": False, "shares": size_matched, "avg_price": 0}

                if status not in ("live", "open", "matched", ""):
                    logger.warning(f"  Order {order_id} unexpected status: {status}")
                    return {"filled": False, "shares": size_matched, "avg_price": 0}

            except Exception as e:
                logger.debug(f"  Fill poll error: {e}")

            time.sleep(FILL_POLL_INTERVAL)

        return {"filled": False, "shares": 0, "avg_price": 0}

    def _get_avg_fill_price(self, order_id, fallback_shares):
        """Compute volume-weighted average fill price from trade history."""
        try:
            trades = self.trader.get_trades_for_order(order_id)
            if not trades:
                return 0
            total_size = 0
            total_cost = 0
            for t in trades:
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
                total_size += size
                total_cost += size * price
            if total_size > 0:
                return total_cost / total_size
        except Exception as e:
            logger.debug(f"  Could not fetch trade history for order {order_id}: {e}")
        return 0

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

            # Skip positions held to resolution
            if pos.get("hold_to_resolution"):
                continue

            # Check if pending sell order has filled
            pending_order = pos.get("pending_sell")
            if pending_order:
                try:
                    order = self.trader.get_order(pending_order)
                    status = order.get("status", "").lower()
                    if status == "matched":
                        size_matched = float(order.get("size_matched", 0))
                        avg_price = self._get_avg_fill_price(pending_order, size_matched)
                        # Trade history API can lag behind order status — fall back to the
                        # limit price on the sell order (e.g. 0.29) rather than reporting 0.
                        if avg_price == 0:
                            avg_price = float(order.get("price", 0))
                        entry = pos["entry_price"]
                        pnl = (avg_price - entry) * size_matched
                        logger.info(
                            f"  Pending sell FILLED for {pos['bet_team']} Map {pos['map_num']} | "
                            f"{size_matched:.2f} shares @ {avg_price:.4f} | PnL: ${pnl:+.2f}"
                        )
                        log_trade({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "mode": self.mode, "match_id": pos["match_id"],
                            "map_num": pos["map_num"],
                            "team_a": pos["team_a"], "team_b": pos["team_b"],
                            "side": pos["bet_team"],
                            "model_prob": "", "market_price": "", "edge": "",
                            "kelly_pct": "", "position_size_usd": f"{pos['position_usd']:.2f}",
                            "entry_price": f"{entry:.4f}",
                            "exit_price": f"{avg_price:.4f}",
                            "exit_reason": pos.get("exit_reason", "trailing_floor"),
                            "pnl": f"{pnl:.2f}",
                        })
                        try:
                            self._record_event(pos["match_id"], pos["map_num"], "exit", pos["bet_team"], avg_price, pos.get("exit_reason", "trailing_floor"))
                        except Exception:
                            pass
                        with self._positions_lock:
                            self.positions.pop(key, None)
                        self._save_state()
                        notify(
                            f"**SOLD** {pos['bet_team']} Map {pos['map_num']} | "
                            f"{size_matched:.1f} shares @ {avg_price:.2f} | PnL: ${pnl:+.2f}"
                        )
                except Exception as e:
                    logger.debug(f"  Pending sell check error: {e}")
                continue

            try:
                price = self.trader.get_market_price(pos["token_id"])
                current = price["best_bid"]
                if current is None:
                    logger.debug(f"  {pos['bet_team']} Map {pos['map_num']} — empty bid side")
                    continue
            except Exception as e:
                logger.debug(f"  Price fetch error for {pos['bet_team']}: {e}")
                continue

            # Log price tick to SQLite
            try:
                self._record_tick(
                    pos["match_id"], pos["map_num"],
                    pos["token_id"], pos["bet_team"], current,
                )
            except Exception as e:
                logger.debug(f"  Tick write error: {e}")

            baseline = pos["entry_price"]
            high = pos["high_price"]

            if current > high:
                with self._positions_lock:
                    if key in self.positions:
                        self.positions[key]["high_price"] = current
                high = current

            stop = compute_stop_loss(baseline)
            if stop is not None and current <= stop:
                logger.info(
                    f"  STOP LOSS triggered for {pos['bet_team']} Map {pos['map_num']} "
                    f"({current:.2f} <= {stop:.2f}, high {high:.2f})"
                )
                self._exit_position(key, current, "stop_loss")
                continue

            floor = compute_floor(baseline, high)
            if floor is not None and current <= floor:
                logger.info(
                    f"  TRAILING FLOOR triggered for {pos['bet_team']} Map {pos['map_num']} "
                    f"({current:.2f} <= floor {floor:.2f}, high {high:.2f})"
                )
                self._exit_position(key, current, "trailing_floor")
                continue

        # Continue recording ticks for exited positions until map resolves
        now = time.time()
        expired = []
        for wkey, w in list(self._price_watchers.items()):
            if w.get("expires_at") and now >= w["expires_at"]:
                expired.append(wkey)
                continue
            try:
                price = self.trader.get_market_price(w["token_id"])
                bid = price["best_bid"]
                if bid is not None:
                    self._record_tick(
                        w["match_id"], w["map_num"],
                        w["token_id"], w["side"], bid,
                    )
            except Exception:
                pass
        for wkey in expired:
            logger.info(f"  Watcher expired: {self._price_watchers[wkey]['side']} Map {self._price_watchers[wkey]['map_num']}")
            del self._price_watchers[wkey]

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

        # Schedule watcher expiry 5 min from now for this map
        expiry = time.time() + 5 * 60
        for wkey, w in self._price_watchers.items():
            if w["match_id"] == match_id and w["map_num"] == map_num:
                w["expires_at"] = expiry
                logger.info(f"  Watcher for {w['side']} Map {map_num} expires in 5 min")

    def _resolve_remaining_positions(self, match_id):
        """
        After a series ends, log exit rows for positions never sold
        (e.g. sub-minimum positions held to market resolution).
        No Polymarket action — just logs to the trade CSV.
        """
        with self._positions_lock:
            match_keys = [
                k for k, p in self.positions.items() if p["match_id"] == match_id
            ]

        for key in match_keys:
            with self._positions_lock:
                pos = self.positions.get(key)
                if pos is None:
                    continue

            model_correct = pos.get("model_correct", "")
            if model_correct == 1:
                exit_price = 1.00
            elif model_correct == 0:
                exit_price = 0.00
            else:
                logger.warning(f"  Position {pos['bet_team']} Map {pos['map_num']} has no map winner — skipping resolution log")
                continue

            entry = pos["entry_price"]
            shares = pos["shares"]
            pnl = (exit_price - entry) * shares

            logger.info(
                f"  RESOLVED {pos['bet_team']} Map {pos['map_num']} | "
                f"Entry: {entry:.2f} → Resolution: {exit_price:.2f} | "
                f"PnL: ${pnl:+.2f}"
            )

            if self.paper:
                self.bankroll += pos["position_usd"] + pnl

            try:
                self._record_event(pos["match_id"], pos["map_num"], "exit", pos["bet_team"], exit_price, "resolution")
            except Exception:
                pass

            with self._positions_lock:
                self.positions.pop(key, None)

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
                "exit_reason": "resolution",
                "pnl": f"{pnl:.2f}",
            })

        # Stop watching prices for this match — maps have resolved
        self._price_watchers = {
            k: w for k, w in self._price_watchers.items()
            if w["match_id"] != match_id
        }

        with self._positions_lock:
            has_positions = bool(self.positions)
        if not has_positions:
            self._clear_state()
        else:
            self._save_state()

    def _exit_position(self, pos_key, exit_price, reason):
        """Exit a position — sell or log paper exit."""
        with self._positions_lock:
            pos = self.positions.get(pos_key)
        if pos is None:
            return

        entry = pos["entry_price"]
        shares = pos["shares"]

        if not self.paper:
            # Query actual shares held (may differ from stored due to partial fills)
            try:
                actual_shares = self.trader.get_position(pos["token_id"])
                if actual_shares <= 0:
                    logger.warning(f"  No shares held for {pos['bet_team']} Map {pos['map_num']} — clearing position")
                    with self._positions_lock:
                        self.positions.pop(pos_key, None)
                    return
                if abs(actual_shares - shares) > 0.01:
                    logger.info(f"  Actual shares: {actual_shares:.2f} (stored: {shares:.2f})")
                shares = actual_shares
            except Exception as e:
                logger.error(f"  Failed to query position balance: {e}")
                return  # Don't sell blind — retry next cycle

            # Polymarket requires minimum 5 shares per order — can't sell sub-minimum positions
            if shares < MIN_CLOB_SHARES:
                logger.warning(
                    f"  Can't sell {shares:.2f} shares (min {MIN_CLOB_SHARES}) — "
                    f"holding to market resolution"
                )
                # Update stored shares so this doesn't re-log every cycle
                with self._positions_lock:
                    if pos_key in self.positions:
                        self.positions[pos_key]["shares"] = shares
                        self.positions[pos_key]["hold_to_resolution"] = True
                self._save_state()
                notify(
                    f"**HOLD TO RESOLUTION** {pos['bet_team']} Map {pos['map_num']} | "
                    f"{shares:.1f} shares < min {MIN_CLOB_SHARES} | {reason} triggered but can't sell"
                )
                return

        if self.paper:
            pnl = (exit_price - entry) * shares
            logger.info(
                f"  EXIT {pos['bet_team']} Map {pos['map_num']} | "
                f"Entry: {entry:.2f} → Exit: {exit_price:.2f} | "
                f"PnL: ${pnl:+.2f} | Reason: {reason}"
            )
            # Return capital + pnl to bankroll
            self.bankroll += pos["position_usd"] + pnl
            logger.info(f"  [PAPER] Bankroll: ${self.bankroll:.2f}")
        else:
            # Snapshot USDC balance before sell to compute actual proceeds
            try:
                balance_before = self.trader.get_balance()
            except Exception:
                balance_before = None

            tick = float(pos.get("tick_size", "0.01"))
            sell_price = max(round(exit_price, 2), tick)
            try:
                # Floor to 6 decimals (1e6 raw units) so we never exceed on-chain balance
                sell_shares = math.floor(shares * 1e6) / 1e6
                resp = self.trader.sell(
                    token_id=pos["token_id"],
                    price=sell_price,
                    size=sell_shares,
                    tick_size=pos["tick_size"],
                    neg_risk=pos["neg_risk"],
                )
                logger.debug(f"  Sell response: {resp}")
                order_id = resp.get("orderID") or resp.get("id")
                if not order_id:
                    logger.error(f"  Sell order returned no order ID: {resp}")
                    notify(f"**SELL FAILED** {pos['bet_team']} Map {pos['map_num']} | no order ID")
                    return
                logger.info(f"  Sell order placed @ {sell_price:.2f}: {order_id} — polling for fill...")
            except Exception as e:
                logger.error(f"  Sell order failed: {e}")
                notify(f"**SELL FAILED** {pos['bet_team']} Map {pos['map_num']} | {e}")
                return  # Keep position — retry next cycle

            # Poll for fill — if not filled in 30s, leave order on the book
            # (all trailing floors are above entry, so GTC will fill eventually)
            fill = self._poll_fill(order_id, pos["token_id"])
            if not fill["filled"]:
                logger.info(f"  Sell not filled within {FILL_POLL_TIMEOUT}s — leaving GTC order on book: {order_id}")
                # Mark position so the price monitor doesn't re-trigger exits
                with self._positions_lock:
                    if pos_key in self.positions:
                        self.positions[pos_key]["pending_sell"] = order_id
                        self.positions[pos_key]["exit_reason"] = reason
                self._save_state()
                return

            # Compute actual exit price from USDC received / shares sold
            try:
                balance_after = self.trader.get_balance()
                if balance_before is not None and sell_shares > 0:
                    usdc_received = balance_after - balance_before
                    if usdc_received > 0:
                        exit_price = usdc_received / sell_shares
                    elif fill["avg_price"] > 0:
                        exit_price = fill["avg_price"]
                elif fill["avg_price"] > 0:
                    exit_price = fill["avg_price"]
            except Exception:
                if fill["avg_price"] > 0:
                    exit_price = fill["avg_price"]

            pnl = (exit_price - entry) * shares
            logger.info(
                f"  EXIT {pos['bet_team']} Map {pos['map_num']} | "
                f"Entry: {entry:.2f} → Exit: {exit_price:.4f} | "
                f"PnL: ${pnl:+.2f} | Reason: {reason}"
            )

        notify(
            f"**EXIT {pos['bet_team']}** Map {pos['map_num']} | "
            f"{entry:.2f} → {exit_price:.2f} | PnL: ${pnl:+.2f} | {reason}"
        )

        try:
            self._record_event(pos["match_id"], pos["map_num"], "exit", pos["bet_team"], exit_price, reason)
        except Exception as e:
            logger.debug(f"  Event write error: {e}")

        with self._positions_lock:
            self.positions.pop(pos_key, None)

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
        })

        # Persist state (or clean up if no positions left)
        with self._positions_lock:
            has_positions = bool(self.positions)
        if has_positions:
            self._save_state()
        else:
            self._clear_state()

    # ──────────────────────────────────────────────────────────
    # ELO PRECOMPUTE
    # ──────────────────────────────────────────────────────────

    def _trigger_elo_precompute(self, reason):
        """Kick off a background Elo snapshot rebuild.

        Fire-and-forget: the Predict cache is keyed by Excel mtime, so the
        thread reads whatever state Excel is in right now and populates the
        cache. If the cache is already current, the thread returns fast.
        """
        def _run():
            start = time.time()
            try:
                precompute_elo_snapshot(_PREDICT_EXCEL)
                logger.info(f"Elo precompute ({reason}) done in {time.time()-start:.1f}s")
            except Exception as e:
                logger.warning(f"Elo precompute ({reason}) failed: {e}")
        threading.Thread(target=_run, daemon=True, name=f"elo-precompute").start()

    # ──────────────────────────────────────────────────────────
    # SCRAPER HELPERS
    # ──────────────────────────────────────────────────────────

    def _scrape_game(self, match_id, game_num):
        """Rescrape a single completed map."""
        logger.info(f"Rescraping match {match_id} game {game_num}...")
        scraper_dir = os.path.join(os.path.dirname(__file__), "..", "Scraper")
        try:
            os.system(f'cd /d "{scraper_dir}" && py main.py --match {match_id} --game {game_num}')
        except Exception as e:
            logger.error(f"Scrape failed: {e}")

    def _scrape_match(self, match_id):
        """Scrape full match after series ends."""
        logger.info(f"Scraping full match {match_id}...")
        scraper_dir = os.path.join(os.path.dirname(__file__), "..", "Scraper")
        try:
            os.system(f'cd /d "{scraper_dir}" && py main.py --match {match_id}')
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
