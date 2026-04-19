"""Trade history endpoints."""

from fastapi import APIRouter, Query
from Dashboard.backend.services.trades import get_trades, get_trade_summary, get_matches
from Dashboard.backend.services.db import fetch_price_ticks, fetch_events

router = APIRouter(prefix="/api/trades", tags=["history"])


@router.get("")
def trades(
    mode: str = "live",
    team: str = None,
    exit_reason: str = None,
    date_from: str = None,
    date_to: str = None,
):
    """All closed trades with optional filters."""
    return get_trades(mode, team, exit_reason, date_from, date_to)


@router.get("/summary")
def trade_summary(mode: str = "live"):
    """Aggregate trade stats."""
    return get_trade_summary(mode)


@router.get("/matches")
def matches(mode: str = "live"):
    """Every evaluated match grouped by map and side."""
    return get_matches(mode)


@router.get("/{match_id}/{map_num}/chart")
def trade_chart(match_id: str, map_num: int, side: str = None):
    """Price ticks + events for a closed trade's chart."""
    ticks = fetch_price_ticks(match_id, map_num, side=side)
    events = fetch_events(match_id, map_num)
    return {"ticks": ticks, "events": events}
