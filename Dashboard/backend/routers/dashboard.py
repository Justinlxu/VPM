"""Dashboard (live) endpoints."""

from fastapi import APIRouter
from Dashboard.backend.services.state import get_dashboard_data
from Dashboard.backend.services.db import fetch_price_ticks, fetch_events

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def dashboard():
    """Current balance, positions, unrealized PnL."""
    return get_dashboard_data()


@router.get("/prices/{match_id}/{map_num}")
def position_prices(match_id: str, map_num: int, since: str = None, side: str = None):
    """Price ticks for a specific active position."""
    ticks = fetch_price_ticks(match_id, map_num, since=since, side=side)
    events = fetch_events(match_id, map_num)
    return {"ticks": ticks, "events": events}
