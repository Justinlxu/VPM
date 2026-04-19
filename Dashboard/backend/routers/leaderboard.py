"""Elo leaderboard endpoints."""

from fastapi import APIRouter, Query
from Dashboard.backend.services.db import fetch_current_elos, fetch_elo_history

router = APIRouter(prefix="/api/elo", tags=["leaderboard"])


@router.get("/players")
def player_elos():
    """Current Elo for all players (latest snapshot)."""
    players = fetch_current_elos()
    players.sort(key=lambda p: p["elo"], reverse=True)
    return players


@router.get("/teams")
def team_elos():
    """Current team Elo (sum of players per team, latest snapshot)."""
    players = fetch_current_elos()
    teams = {}
    for p in players:
        team = p["team"]
        if team not in teams:
            teams[team] = {"team": team, "elo": 0, "players": []}
        teams[team]["elo"] += p["elo"]
        teams[team]["players"].append({"player": p["player"], "elo": p["elo"]})

    result = list(teams.values())
    result.sort(key=lambda t: t["elo"], reverse=True)
    return result


@router.get("/history")
def elo_history(player: str = None, team: str = None):
    """Elo over time for a player or team."""
    return fetch_elo_history(player=player, team=team)
