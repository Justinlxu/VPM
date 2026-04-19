"""Read-only SQLite access for the dashboard."""

import sqlite3
from contextlib import contextmanager
from Dashboard.backend.config import DB_FILE


@contextmanager
def get_db():
    """Yield a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def fetch_price_ticks(match_id, map_num, since=None, side=None):
    """Return price ticks for a match/map, optionally filtered by side and/or timestamp."""
    with get_db() as conn:
        query = "SELECT timestamp, price, side FROM price_ticks WHERE match_id = ? AND map_num = ?"
        params = [str(match_id), map_num]
        if side:
            query += " AND side = ?"
            params.append(side)
        if since:
            query += " AND timestamp > ?"
            params.append(since)
        query += " ORDER BY timestamp"
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def fetch_events(match_id, map_num):
    """Return entry/exit events for a match/map."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, event_type, side, price, reason FROM events "
            "WHERE match_id = ? AND map_num = ? ORDER BY timestamp",
            (str(match_id), map_num),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_recent_ticks(seconds=300):
    """Return all ticks from the last N seconds (for WebSocket catchup)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, match_id, map_num, side, price FROM price_ticks "
            "WHERE timestamp > datetime('now', ?)",
            (f"-{seconds} seconds",),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_latest_tick(match_id, map_num):
    """Return the most recent price tick for a position."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT timestamp, price FROM price_ticks "
            "WHERE match_id = ? AND map_num = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (str(match_id), map_num),
        ).fetchone()
    return dict(row) if row else None


def fetch_current_elos():
    """Return the latest Elo rating for every player (most recent snapshot)."""
    with get_db() as conn:
        # Get the most recent snapshot timestamp
        latest = conn.execute(
            "SELECT MAX(timestamp) FROM elo_snapshots"
        ).fetchone()[0]
        if not latest:
            return []
        rows = conn.execute(
            "SELECT player, team, elo FROM elo_snapshots WHERE timestamp = ?",
            (latest,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_elo_history(player=None, team=None):
    """Return Elo over time for a player or team."""
    with get_db() as conn:
        if player:
            rows = conn.execute(
                "SELECT timestamp, elo FROM elo_snapshots "
                "WHERE player = ? ORDER BY timestamp",
                (player,),
            ).fetchall()
            return [dict(r) for r in rows]
        elif team:
            # Team Elo = sum of players on that team per snapshot
            rows = conn.execute(
                "SELECT timestamp, SUM(elo) as elo FROM elo_snapshots "
                "WHERE team = ? GROUP BY timestamp ORDER BY timestamp",
                (team,),
            ).fetchall()
            return [dict(r) for r in rows]
    return []


def fetch_new_ticks_since(timestamp):
    """Return ticks newer than the given ISO timestamp."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT timestamp, match_id, map_num, side, price FROM price_ticks "
            "WHERE timestamp > ? ORDER BY timestamp",
            (timestamp,),
        ).fetchall()
    return [dict(r) for r in rows]
