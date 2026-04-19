"""WebSocket endpoint for live price tick streaming."""

import asyncio
import json
from fastapi import WebSocket, WebSocketDisconnect
from Dashboard.backend.services.db import fetch_new_ticks_since, fetch_recent_ticks


class PriceFeed:
    """Manages WebSocket connections and broadcasts new ticks."""

    def __init__(self):
        self.connections: list[WebSocket] = []
        self._running = False

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

        # Send recent history as catchup
        try:
            history = fetch_recent_ticks(seconds=300)
            await ws.send_json({"type": "history", "data": history})
        except Exception:
            pass

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, ticks):
        """Send new ticks to all connected clients."""
        if not ticks or not self.connections:
            return
        msg = json.dumps({"type": "tick", "data": ticks})
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def poll_loop(self):
        """Background task: poll SQLite every second for new ticks."""
        self._running = True
        last_ts = ""
        while self._running:
            try:
                if self.connections:
                    ticks = fetch_new_ticks_since(last_ts) if last_ts else []
                    if ticks:
                        last_ts = ticks[-1]["timestamp"]
                        await self.broadcast(ticks)
                    elif not last_ts:
                        # Initialize last_ts from recent data
                        recent = fetch_recent_ticks(seconds=5)
                        if recent:
                            last_ts = recent[-1]["timestamp"]
            except Exception:
                pass
            await asyncio.sleep(1)

    def stop(self):
        self._running = False


price_feed = PriceFeed()
