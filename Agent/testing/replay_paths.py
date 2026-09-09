"""
Real-data path loader for floor_search.

Pulls recorded (match_id, map_num, side) tick sequences from agent.db and
anchors each one to an "entry tick":

  1. Preferred: an entry event in the events table -- the timestamp and
     price of the bet the agent actually placed.
  2. Fallback: the 3c / 10s stability heuristic -- the last tick of the
     first window where price stays within 3c for >=10s. Approximates the
     pre-map quiet period right before round 1.

Paths are skipped when no stable window exists, when the post-entry tick
stream is shorter than min_post_entry_secs, or when the last tick is in
[0.05, 0.95] (map didn't resolve clearly).

Usage:
    py -m Agent.testing.replay_paths
"""

import os
import random
import sqlite3
from datetime import datetime
from collections import defaultdict


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "agent.db")

STABILITY_BAND = 0.03
STABILITY_SECS = 10
MIN_POST_ENTRY_SECS = 300   # need >=5 min of price action after entry
RESOLVE_LO = 0.05
RESOLVE_HI = 0.95


def _parse_ts(s):
    return datetime.fromisoformat(s).timestamp()


def _heuristic_entry_idx(ticks):
    """First index where price has stayed within STABILITY_BAND for >=STABILITY_SECS.
    Returns the LAST tick of that stable window (the moment before movement starts)."""
    n = len(ticks)
    if n < 2:
        return None

    for i in range(n):
        t0, p0 = ticks[i]
        pmin = pmax = p0
        j = i
        qualified = False
        while j + 1 < n:
            j += 1
            tj, pj = ticks[j]
            new_min = min(pmin, pj)
            new_max = max(pmax, pj)
            if new_max - new_min > STABILITY_BAND:
                break
            pmin, pmax = new_min, new_max
            if tj - t0 >= STABILITY_SECS:
                qualified = True
                # extend through any further stable ticks before returning
                continue
        if qualified:
            return j
    return None


def load_paths(db_path=None, min_post_entry_secs=MIN_POST_ENTRY_SECS,
               dedupe=False, dedupe_seed=42, event_only=False):
    """Returns (paths, counts).

    paths: list of dicts with keys
        match_id, map_num, side, entry_source ("event"|"heuristic"),
        entry_price, ticks (list of (t_seconds_since_entry, price)),
        final_outcome (1.0 if our side won, 0.0 if lost)

    counts: breakdown of what was loaded vs skipped.

    dedupe=True applies one-side-per-map filtering AFTER loading:
        - For (match, map) with an event entry: keep ONLY the bet side
          (drop the complement-side mirror path)
        - For (match, map) with no event entry: keep one side at random
        Eliminates mirror double-counting that biases bootstrap toward 50%.
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()

    entry_events = {}
    for ts, mid, mn, side, price in cur.execute(
        "SELECT timestamp, match_id, map_num, side, price FROM events "
        "WHERE event_type='entry'"
    ):
        entry_events[(mid, mn, side)] = (_parse_ts(ts), float(price))

    grouped = defaultdict(list)
    for ts, mid, mn, side, price in cur.execute(
        "SELECT timestamp, match_id, map_num, side, price FROM price_ticks "
        "ORDER BY timestamp"
    ):
        grouped[(mid, mn, side)].append((_parse_ts(ts), float(price)))

    conn.close()

    paths = []
    counts = {
        "event": 0,
        "heuristic": 0,
        "skip_no_window": 0,
        "skip_short_post": 0,
        "skip_unresolved": 0,
        "skip_anchor_after_ticks": 0,
    }

    for key, raw in grouped.items():
        if not raw:
            continue

        if key in entry_events:
            anchor_ts, entry_price = entry_events[key]
            post = [(t - anchor_ts, p) for t, p in raw if t >= anchor_ts]
            if not post:
                counts["skip_anchor_after_ticks"] += 1
                continue
            source = "event"
        else:
            idx = _heuristic_entry_idx(raw)
            if idx is None:
                counts["skip_no_window"] += 1
                continue
            anchor_ts, entry_price = raw[idx]
            post = [(t - anchor_ts, p) for t, p in raw[idx:]]
            source = "heuristic"

        if post[-1][0] < min_post_entry_secs:
            counts["skip_short_post"] += 1
            continue

        last_price = post[-1][1]
        if RESOLVE_LO <= last_price <= RESOLVE_HI:
            counts["skip_unresolved"] += 1
            continue

        paths.append({
            "match_id": key[0],
            "map_num": key[1],
            "side": key[2],
            "entry_source": source,
            "entry_price": entry_price,
            "ticks": post,
            "final_outcome": 1.0 if last_price > 0.5 else 0.0,
        })
        counts[source] += 1

    if event_only:
        before = len(paths)
        paths = [p for p in paths if p["entry_source"] == "event"]
        counts["event_only_dropped"] = before - len(paths)

    if dedupe:
        rng = random.Random(dedupe_seed)
        by_map = defaultdict(list)
        for p in paths:
            by_map[(p["match_id"], p["map_num"])].append(p)
        kept = []
        dropped = 0
        for (mid, mn), group in by_map.items():
            event_paths = [p for p in group if p["entry_source"] == "event"]
            if event_paths:
                # Keep only the bet side; drop complement mirrors
                kept.append(event_paths[0])
                dropped += len(group) - 1
            else:
                # Unbet map -- keep one side at random
                kept.append(rng.choice(group))
                dropped += len(group) - 1
        counts["dedupe_dropped"] = dropped
        paths = kept

    return paths, counts


def main():
    paths, counts = load_paths()
    print(f"Loaded {len(paths)} paths from {DB_PATH}")
    print(f"  event-anchored:      {counts['event']}")
    print(f"  heuristic-anchored:  {counts['heuristic']}")
    print(f"  skipped (no stable window):     {counts['skip_no_window']}")
    print(f"  skipped (post-entry < 5 min):   {counts['skip_short_post']}")
    print(f"  skipped (unresolved last tick): {counts['skip_unresolved']}")
    print(f"  skipped (anchor after ticks):   {counts['skip_anchor_after_ticks']}")

    if not paths:
        return

    print(f"\nSample of first 5 paths:")
    print(f"  {'match':>8}  {'map':>3}  {'side':<22}  {'src':>9}  "
          f"{'entry':>6}  {'ticks':>5}  {'mins':>5}  {'final':>5}")
    for p in paths[:5]:
        mins = p['ticks'][-1][0] / 60
        print(f"  {p['match_id']:>8}  {p['map_num']:>3}  {p['side'][:22]:<22}  "
              f"{p['entry_source']:>9}  {p['entry_price']:>6.3f}  "
              f"{len(p['ticks']):>5}  {mins:>5.1f}  {p['final_outcome']:>5.1f}")

    wins = sum(1 for p in paths if p["final_outcome"] == 1.0)
    print(f"\nWin rate: {wins}/{len(paths)} = {wins/len(paths):.1%}")


if __name__ == "__main__":
    main()
