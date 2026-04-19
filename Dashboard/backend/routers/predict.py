"""Prediction endpoint."""

import sys
import os
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

# Ensure project root is on sys.path for Predict imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from Predict.predict import predict_elo_only

router = APIRouter(prefix="/api", tags=["predict"])


class PredictRequest(BaseModel):
    team_a: str
    team_b: str
    map_name: Optional[str] = None


@router.post("/predict")
def predict(req: PredictRequest):
    """Run Elo model prediction for a team matchup."""
    prob_a, elo_features, player_info, maps_played, _ = predict_elo_only(
        req.team_a, req.team_b, map_name=req.map_name
    )

    # Format player info for response
    teams = {}
    for team, players in player_info.items():
        teams[team] = [
            {"player": p[0], "elo": round(p[1], 1), "fkfd": round(p[2], 2)}
            for p in players
        ]

    return {
        "prob_a": round(prob_a, 4),
        "prob_b": round(1 - prob_a, 4),
        "team_a": list(player_info.keys())[0] if player_info else req.team_a,
        "team_b": list(player_info.keys())[1] if len(player_info) > 1 else req.team_b,
        "elo_features": {k: round(v, 1) for k, v in elo_features.items()},
        "players": teams,
        "maps_played": maps_played,
    }
