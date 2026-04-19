"""Read agent state and compute live position data."""

import sys
import json
import os
from Dashboard.backend.config import STATE_FILE, PROJECT_ROOT, compute_floor
from Dashboard.backend.services.db import fetch_latest_tick

sys.path.insert(0, PROJECT_ROOT)
from PolyInt.trade import PolyTrader

_cached_state = None
_trader = None


def _get_trader():
    global _trader
    if _trader is None:
        _trader = PolyTrader()
    return _trader


def get_live_balance():
    """Query on-chain USDC balance. Falls back to state file on error."""
    try:
        return _get_trader().get_balance()
    except Exception:
        state = read_agent_state()
        return state.get("bankroll", 0)


def read_agent_state():
    """Read agent_state.json safely. Returns cached value on read error."""
    global _cached_state
    if not os.path.exists(STATE_FILE):
        return {"mode": "live", "bankroll": 0, "positions": {}, "saved_at": None}
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        _cached_state = state
        return state
    except (json.JSONDecodeError, IOError):
        # File mid-write — return last good read
        if _cached_state:
            return _cached_state
        return {"mode": "live", "bankroll": 0, "positions": {}, "saved_at": None}


def get_dashboard_data():
    """Build the full dashboard payload with live prices and PnL."""
    state = read_agent_state()
    positions = []
    total_unrealized = 0.0

    for key, pos in state.get("positions", {}).items():
        entry_price = pos.get("entry_price", 0)
        high_price = pos.get("high_price", entry_price)
        shares = pos.get("shares", 0)
        position_usd = pos.get("position_usd", 0)

        # Get latest price from SQLite
        tick = fetch_latest_tick(pos.get("match_id"), pos.get("map_num"))
        current_price = tick["price"] if tick else None

        # Compute unrealized PnL
        current_pnl = None
        pnl_pct = None
        if current_price is not None and entry_price > 0:
            current_pnl = (current_price - entry_price) * shares
            pnl_pct = (current_price - entry_price) / entry_price
            total_unrealized += current_pnl

        # Compute current trailing floor
        floor = compute_floor(entry_price, high_price)

        positions.append({
            "key": key,
            "match_id": pos.get("match_id"),
            "map_num": pos.get("map_num"),
            "team_a": pos.get("team_a"),
            "team_b": pos.get("team_b"),
            "bet_team": pos.get("bet_team"),
            "entry_price": entry_price,
            "high_price": high_price,
            "shares": shares,
            "position_usd": position_usd,
            "current_price": current_price,
            "current_pnl": round(current_pnl, 2) if current_pnl is not None else None,
            "unrealized_pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
            "trailing_floor": round(floor, 4) if floor is not None else None,
            "hold_to_resolution": pos.get("hold_to_resolution", False),
        })

    mode = state.get("mode", "live")
    bankroll = get_live_balance() if mode == "live" else state.get("bankroll", 0)

    return {
        "mode": mode,
        "bankroll": round(bankroll, 2),
        "positions": positions,
        "total_unrealized_pnl": round(total_unrealized, 2),
        "active_count": len(positions),
        "saved_at": state.get("saved_at"),
    }
