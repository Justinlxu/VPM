"""Dashboard backend configuration — paths and constants."""

import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AGENT_LOGS = os.path.join(PROJECT_ROOT, "Agent", "logs")

DB_FILE = os.path.join(AGENT_LOGS, "agent.db")
STATE_FILE = os.path.join(AGENT_LOGS, "agent_state.json")

# Trailing floor ladder (mirrored from Agent/agent.py)
TRAILING_FLOOR_LADDER = (
    (0.16, 0.09),
    (0.27, 0.21),
    (0.38, 0.34),
)
TRAILING_FLOOR_STEP = 0.06


def compute_floor(baseline, high):
    """Compute trailing floor price given entry baseline and high watermark."""
    if baseline <= 0:
        return None
    gain = (high - baseline) / baseline
    if gain < TRAILING_FLOOR_LADDER[0][0]:
        return None
    floor_offset = 0
    for thresh, offset in TRAILING_FLOOR_LADDER:
        if gain >= thresh:
            floor_offset = offset
    last_thresh, last_offset = TRAILING_FLOOR_LADDER[-1]
    if gain > last_thresh:
        extra_steps = int((gain - last_thresh) / TRAILING_FLOOR_STEP)
        floor_offset = last_offset + extra_steps * TRAILING_FLOOR_STEP
    return baseline * (1 + floor_offset)
